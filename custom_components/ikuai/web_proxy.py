"""iKuai 原版 Web 界面的 Home Assistant 同源反向代理（悬浮窗内核）。

为什么是"代理"而不是"重写 UI"
------------------------------
iKuai 管理后台是 Vue SPA（170 个 webpack chunk，134 个 func_name 模块、
113 种 action），包含拨号增删改、分流、VPN、AC 无线、虚拟机/VNC、文件管理等
几百个页面。逐页重放成 HA 卡片既不现实也会随固件升级失效。因此本模块把**原版
界面整体反向代理**到 HA 同源路径下：

    浏览器（HA 设备页悬浮 iframe）
        → /ikuai/web/<任意路径>
        → 本视图（鉴权 + 登录态托管 + 内容改写）
        → http://路由器/<原路径>

最终在悬浮窗里得到的就是登录后的完整官方界面，固件升级后页面自动跟随。

鉴权模型（与 esxi_stats 控制台同一套思路）
------------------------------------------
顶层导航到代理页面时浏览器手上没有 HA Bearer 令牌，所以设备
``configuration_url`` 用 ``async_sign_path`` 签发 24 小时有效的 ``authSig``。
首次带签名打开时，视图：

1. 校验 authSig（JWT + HA 签名密钥 + refresh token 仍有效）；
2. 下发自己的 HttpOnly Cookie ``ikuai_web_sess``（entry_id + HMAC），
   之后 iframe 内所有同源请求（含 POST 上传、WebSocket 握手）都靠它鉴权，
   绕开"HA 新版中间件只对 GET/HEAD 接受 authSig"的限制；
3. 用配置条目里已保存的管理员凭据向路由器登录，把 ``sess_key / username /
   login`` 三个 Cookie（非 HttpOnly，path 限定在代理前缀下）种给浏览器，
   满足 SPA 路由守卫的登录态检查（它只认这三个 Cookie）。

路由器的账号密码和 sess_key 都不会出现在前端代码里。

内容改写
--------
SPA 的 ``apiRootUrl`` 为空串、全部 API/静态资源都是同源绝对路径（``/Action/``、
``/static/``、``/disk/`` 等），路由为 hash 模式。代理只需把文本响应中的这些
绝对路径加上 ``/ikuai/web`` 前缀；二进制响应（上传/下载/字体）原样透传；
302 的 Location 同步改写；实时告警的 WebSocket（``/Action/rtcollect/websocket``）
走双向桥接。
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import time
from collections import OrderedDict
from datetime import timedelta
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web
from aiohttp.http_websocket import WSMsgType

from homeassistant.components.http import HomeAssistantView
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_HOST,
    CONF_PASS,
    CONF_PASSWD,
    CONF_USERNAME,
    DOMAIN,
    EMBED_ASSET_VERSION,
    PROXY_COOKIE_MAX_AGE,
    PROXY_COOKIE_NAME,
    PROXY_ORIGIN_PREFIX,
    PROXY_PAGE_PREFIX,
    PROXY_SIGN_EXPIRY_HOURS,
    PROXY_STATIC_PREFIX,
    VNC_NATIVE_PAGE_TAIL,
)

_LOGGER = logging.getLogger(__name__)

_DATA_KEY = f"{DOMAIN}_web_proxy"
_SIGN_EXPIRY = timedelta(hours=PROXY_SIGN_EXPIRY_HOURS)
_RESIGN_INTERVAL = timedelta(hours=12)

# HA 路径签名（2024.2+）；老版本退化为不签名（仅限受信内网，登录仍由本视图校验）
try:  # pragma: no cover - 取决于 HA 版本
    from homeassistant.components.http.auth import async_sign_path
except ImportError:  # pragma: no cover
    async_sign_path = None

try:  # pragma: no cover
    from homeassistant.auth import jwt_wrapper
    from homeassistant.components.http.auth import (
        DATA_SIGN_SECRET,
        SAFE_QUERY_PARAMS,
        SIGN_QUERY_PARAM,
    )
except ImportError:  # pragma: no cover
    jwt_wrapper = None
    DATA_SIGN_SECRET = "http.auth.sign_secret"
    SAFE_QUERY_PARAMS = frozenset()
    SIGN_QUERY_PARAM = "authSig"

# ---------------------------------------------------------------------------
# 路径改写
# ---------------------------------------------------------------------------
_PREFIX = PROXY_PAGE_PREFIX  # /ikuai/web

# JS 字符串字面量里出现的同源绝对路径（覆盖单引号/双引号/反引号）
_JS_PATH_ROOTS = (
    "/Action/",
    "/static/",
    "/disk/",
    "/pcap/",
    "/audit_url/",
    "/www/",
    "/json/",       # /json/menu.json 左侧导航菜单树
    "/dist/",       # 虚拟机/KVM 等内嵌 webpack 子应用的 publicPath
    "/plugins/",    # Docker 插件 iframe（代码里与 location.host 拼接）
    "/notice/",     # 公告确认回调页
    "/login",
)
# 精确改写的根路径跳转（避免误伤值为 "/" 的普通字段）
_JS_EXACT = (
    ('location.href="/"', f'location.href="{_PREFIX}/"'),
    ("location.href='/'", f"location.href='{_PREFIX}/'"),
    # webpack 运行时 publicPath：c.p="/"（动态 JS/CSS chunk 都靠它拼接，
    # 不改写则全部 404 → SPA 白屏）。两种引号都覆盖。
    ('.p="/"', f'.p="{_PREFIX}/"'),
    (".p='/'", f".p='{_PREFIX}/'"),
)

# HTML 无引号/带引号属性：href=/static/x、src="/static/x"、action="/"
_HTML_ATTR_RE = re.compile(
    r'''(\s(?:href|src|action|poster|data-src)\s*=\s*)(["']?)/(?!/|ikuai/)''',
    re.I,
)
# CSS url(/...) / url("/...")
_CSS_URL_RE = re.compile(r'''(url\(\s*)(["']?)/(?!/|ikuai/)''', re.I)

# 上游响应头中需要逐跳剔除/由我们重算的字段
_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-encoding",
    "content-length",
}

_TEXT_MIME_STARTS = ("text/",)
_TEXT_MIME_CONTAINS = (
    "javascript",
    "ecmascript",
    "json",
    "xml",
    "css",
    "html",
)


def _rewrite_js(text: str) -> str:
    """改写 JS/HTML 内联脚本中的同源绝对路径。"""
    for root in _JS_PATH_ROOTS:
        for quote in ('"', "'", "`"):
            text = text.replace(f"{quote}{root}", f"{quote}{_PREFIX}{root}")
    for old, new in _JS_EXACT:
        text = text.replace(old, new)
    return text


def _rewrite_html(text: str) -> str:
    """改写 HTML 文档：属性路径 + 内联脚本。"""
    text = _HTML_ATTR_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_PREFIX}/", text)
    return _rewrite_js(text)


def _rewrite_css(text: str) -> str:
    return _CSS_URL_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{_PREFIX}/", text)


def _inject_native_vnc(hass, entry_id: str, query, text: str) -> str:
    """向爱快原生虚拟机控制台页注入引导脚本。

    页面 URL 带 ``_vmport=<VNC端口>``（由 console 视图 302 过来）。脚本把
    noVNC 的 WebSocket 指向 HA 同源代理，并在虚拟机设有 VNC 密码时自动填入。
    「远程控制」开关关闭、取不到虚拟机时一律不注入（页面停在官方连接面板）。
    """
    try:
        port = int(str(query.get("_vmport") or "0"))
    except (TypeError, ValueError):
        return text
    if port <= 0:
        return text
    password = ""
    try:
        from .console import get_manager, native_vnc_bootstrap

        manager = get_manager(hass, entry_id)
        if manager is None or not manager.enabled:
            return text
        vm = manager.vm_by_port(port)
        if vm is None:
            return text
        password = str(vm.get("vnc_pwd") or "")
        bootstrap = native_vnc_bootstrap(port, password)
    except Exception:  # pylint: disable=broad-except - 注入失败不应拖垮整个代理
        _LOGGER.debug("iKuai 原生 VNC 页引导脚本注入失败", exc_info=True)
        return text
    if re.search(r"</head>", text, re.I):
        return re.sub(r"</head>", bootstrap + "</head>", text, count=1, flags=re.I)
    return bootstrap + text


def _is_text(content_type: str, url_path: str) -> bool:
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    if ct.startswith(_TEXT_MIME_STARTS):
        return True
    if any(k in ct for k in _TEXT_MIME_CONTAINS):
        return True
    # 上游偶发不给正确类型，按后缀兜底
    if url_path.endswith((".js", ".css", ".html", ".json", ".svg")):
        return True
    return False


# --------------------------------------------------------------------------- #
# 静态资源缓存（浏览器强/协商缓存 + 每设备服务端 LRU）
# --------------------------------------------------------------------------- #
# iKuai 后台是 170-chunk 的 webpack SPA，弱 CPU 嵌入式路由每次全量回源+逐 chunk
# 解压改写极慢。webpack 产物文件名带 contenthash（app.8a3f2c1d.js），内容随名
# 不可变，可放心永久缓存；固定文件名的静态资源用短 TTL + ETag 协商缓存；
# HTML 入口与 /Action/、/json/ 等动态响应一律不缓存（固件升级后 index.html
# 引用的新 hash 文件名会自然缓存未命中而回源，无需主动失效）。
_STATIC_DIR_PREFIXES = ("static/", "dist/")
_STATIC_EXT = (".js", ".css", ".map", ".woff", ".woff2", ".ttf", ".eot", ".otf",
               ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp")
# app.8a3f2c1d.js / chunk-xxx.7a3b9c2d.css / logo.abcd1234.png / x.1a2b3c.js.map
_HASH_FILE_RE = re.compile(r"(?:[._-]|^)[0-9a-f]{6,32}\.[a-z0-9.]+$", re.I)
# fontello.woff2?v=4.7.0 这类版本号 query：版本变 URL 即变，等同 immutable
_VER_QUERY_RE = re.compile(r"(?:^|[&?])(?:v|ver|version)=", re.I)

_CC_IMMUTABLE = "public, max-age=31536000, immutable"
_CC_MUTABLE = "no-cache"
# 固定文件名静态资源的服务端 TTL：期内浏览器协商一律由代理回 304，不碰路由器
_MUTABLE_TTL = 3600.0
# 单设备内存缓存上限（LRU 按总字节+条目数双约束淘汰）
_CACHE_BYTES_PER_ENTRY = 48 * 1024 * 1024
_CACHE_ITEMS_PER_ENTRY = 800


def _static_cache_kind(tail: str) -> str | None:
    """静态资源缓存分级。

    返回 ``"immutable"``（内容哈希/版本号，可一年强缓存）、``"mutable"``
    （固定文件名，短 TTL + 协商缓存）或 ``None``（不缓存）。
    """
    path, _, query = tail.partition("?")
    lower = path.lower()
    if not lower.startswith(_STATIC_DIR_PREFIXES):
        return None
    if not lower.endswith(_STATIC_EXT):
        return None
    fname = lower.rsplit("/", 1)[-1]
    if _HASH_FILE_RE.search(fname) or (query and _VER_QUERY_RE.search(query)):
        return "immutable"
    return "mutable"


def _asset_etag(body: bytes) -> str:
    """代理基于"改写后字节"自签 ETag（上游 ETag 对应压缩原文，不能沿用）。"""
    return f'"{len(body):x}-{hashlib.md5(body).hexdigest()[:16]}"'


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    return etag in {v.strip() for v in header.split(",")}


class _CachedAsset:
    __slots__ = ("status", "body", "content_type", "etag", "cache_control", "ts")

    def __init__(self, status, body, content_type, etag, cache_control, ts):
        self.status = status
        self.body = body
        self.content_type = content_type
        self.etag = etag
        self.cache_control = cache_control
        self.ts = ts


def _asset_response(asset: _CachedAsset, method: str, inm: str | None):
    """用缓存条目构造响应；If-None-Match 命中时返回 304。"""
    if method != "HEAD" and _etag_matches(inm, asset.etag):
        return web.Response(status=304, headers={
            "ETag": asset.etag,
            "Cache-Control": asset.cache_control,
            "Vary": "Accept-Encoding",
        })
    return web.Response(
        status=asset.status,
        body=b"" if method == "HEAD" else asset.body,
        headers={
            "Content-Type": asset.content_type,
            "ETag": asset.etag,
            "Cache-Control": asset.cache_control,
            "Vary": "Accept-Encoding",
        })


# --------------------------------------------------------------------------- #
# 上游会话（每个配置条目一个：独立 cookie jar，登录态自管）
# --------------------------------------------------------------------------- #
class UpstreamSession:
    """与单台 iKuai 路由器的 HTTP 会话池。"""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        data = entry.data
        self.base = str(data[CONF_HOST]).rstrip("/")
        self.username = data[CONF_USERNAME]
        self.passwd = data[CONF_PASSWD]
        self.pas = data[CONF_PASS]
        # 独立 session：与数据轮询互不干扰；verify_ssl=False 兼容自签证书。
        # unsafe=True 至关重要：aiohttp 默认 CookieJar 出于 RFC 6265 严格性会
        # **静默丢弃 IP 地址主机（192.168.x.x）下发的 cookie**，sess_key 就存不住。
        connector = aiohttp.TCPConnector(ssl=False, limit=32, force_close=False)
        cookie_jar = aiohttp.CookieJar(unsafe=True)
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=120)
        self.session = aiohttp.ClientSession(
            connector=connector, timeout=timeout, cookie_jar=cookie_jar)
        self.sess_key: str | None = None
        self._login_lock = asyncio.Lock()
        self._closed = False
        # 静态资源改写副本的内存缓存（仅本设备可见；OrderedDict 即 LRU 顺序）
        self._cache: OrderedDict[str, _CachedAsset] = OrderedDict()
        self._cache_bytes = 0

    @property
    def entry_id(self) -> str:
        return self.entry.entry_id

    def _sess_key_from_jar(self) -> str | None:
        for cookie in self.session.cookie_jar:
            if cookie.key == "sess_key" and cookie.value:
                return cookie.value
        return None

    async def login(self) -> bool:
        """向路由器登录，成功后 cookie jar 自动持有 sess_key。"""
        async with self._login_lock:
            if self.sess_key and self._sess_key_from_jar():
                return True
            try:
                async with self.session.post(
                    self.base + "/Action/login",
                    json={"username": self.username,
                          "passwd": self.passwd, "pass": self.pas},
                    headers={"Content-Type": "application/json;charset=UTF-8"},
                ) as resp:
                    body = await resp.json(content_type=None)
            except Exception as exc:  # pylint: disable=broad-except
                _LOGGER.warning("iKuai Web 代理登录失败 %s: %r", self.base, exc)
                return False
            if not isinstance(body, dict) or body.get("Result") != 10000:
                _LOGGER.warning("iKuai Web 代理登录被拒 %s: %s", self.base, body)
                return False
            self.sess_key = self._sess_key_from_jar()
            if not self.sess_key:
                # 个别固件不在 Set-Cookie 回，兜底无 cookie 场景
                _LOGGER.warning("iKuai 登录成功但未取得 sess_key cookie")
                return False
            _LOGGER.info("iKuai Web 代理登录成功：%s", self.base)
            return True

    async def close(self):
        self._closed = True
        await self.session.close()
        self._cache.clear()
        self._cache_bytes = 0

    def invalidate(self):
        self.sess_key = None
        self.session.cookie_jar.clear()
        # 会话失效不清静态缓存：hash 文件名内容与会话无关，固件不升级就不变

    # ------------------------------------------------------------------ #
    # 静态资源 LRU 缓存（key = 上游 path?query）
    # ------------------------------------------------------------------ #
    def get_cached(self, key: str) -> _CachedAsset | None:
        asset = self._cache.get(key)
        if asset is None:
            return None
        if asset.cache_control != _CC_IMMUTABLE \
                and time.monotonic() - asset.ts > _MUTABLE_TTL:
            self._drop_cached(key)
            return None
        self._cache.move_to_end(key)
        return asset

    def store_cached(self, key: str, kind: str, status: int,
                     body: bytes, content_type: str) -> _CachedAsset:
        asset = _CachedAsset(
            status=status, body=body, content_type=content_type,
            etag=_asset_etag(body),
            cache_control=_CC_IMMUTABLE if kind == "immutable" else _CC_MUTABLE,
            ts=time.monotonic())
        old = self._cache.get(key)
        if old is not None:
            self._cache_bytes -= len(old.body)
        self._cache[key] = asset
        self._cache_bytes += len(body)
        while len(self._cache) > _CACHE_ITEMS_PER_ENTRY \
                or self._cache_bytes > _CACHE_BYTES_PER_ENTRY:
            self._drop_cached(next(iter(self._cache)))
        return asset

    def _drop_cached(self, key: str):
        old = self._cache.pop(key, None)
        if old is not None:
            self._cache_bytes -= len(old.body)


def _store(hass) -> dict:
    return hass.data.setdefault(_DATA_KEY, {})


def get_upstream(hass, entry) -> UpstreamSession:
    store = _store(hass)
    sessions = store.setdefault("sessions", {})
    sess = sessions.get(entry.entry_id)
    if sess is None:
        sess = UpstreamSession(hass, entry)
        sessions[entry.entry_id] = sess
    return sess


# --------------------------------------------------------------------------- #
# 浏览器实际访问基地址学习（端口转发/域名场景，方案同 esxi_stats 控制台）
# --------------------------------------------------------------------------- #
# 核心原理：浏览器请求的 Host 头就是地址栏 host:port，aiohttp 据此重建
# request.url，后端在心跳视图里直接读 request.url.origin() 即可，前端无需
# 在 body 里传任何东西。authSig 只签 path+query 不签 host，学到的 origin
# 拼上同一条签名路径，在端口转发/域名的任意外部地址上都有效。
_BASE_STORE_KEY = f"{DOMAIN}.web_base_urls"
_BASE_SAVE_DELAY = 2.0
_RELINK_DELAY = 1.5


def _normalize_base(value) -> str:
    """规范成 scheme://host[:port]；非法返回空串。"""
    if not value:
        return ""
    try:
        parsed = urlsplit(str(value))
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _learned_store(hass) -> dict:
    return _store(hass).setdefault("learned_bases", {})


def _ha_detected_base(hass) -> str:
    """HA 自探测地址（学习值缺失时的兜底）。"""
    configured = hass.config.external_url or hass.config.internal_url or ""
    normalized = _normalize_base(configured)
    if normalized:
        return normalized
    try:
        from homeassistant.helpers.network import get_url

        detected = get_url(hass, prefer_external=False, allow_ip=True)
    except Exception:  # pylint: disable=broad-except
        detected = ""
    return _normalize_base(detected) or detected.rstrip("/")


def _configured_base(hass, entry_id: str) -> str:
    """设备链接使用的绝对基地址：学习值优先，兜底 HA 自探测。"""
    learned = _normalize_base(_learned_store(hass).get(entry_id))
    if learned:
        return learned
    return _ha_detected_base(hass)


async def _persist_learned_bases(hass):
    """防抖落盘学到的基地址。"""
    store = _store(hass)
    if store.get("base_persist_task") is not None:
        return

    async def _do_save():
        try:
            await asyncio.sleep(_BASE_SAVE_DELAY)
            from homeassistant.helpers.storage import Store

            await Store(hass, 1, _BASE_STORE_KEY,
                        private=True).async_save(
                            {"bases": dict(_learned_store(hass))})
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning("iKuai 访问基地址落盘失败", exc_info=True)
        finally:
            store["base_persist_task"] = None

    store["base_persist_task"] = hass.loop.create_task(_do_save())


def _schedule_relink(hass, entry_id: str):
    """基地址变化后防抖重签该条目的设备链接。"""
    tasks = _store(hass).setdefault("relink_tasks", {})
    old = tasks.get(entry_id)
    if old is not None and not old.done():
        return

    async def _do_relink():
        await asyncio.sleep(_RELINK_DELAY)
        try:
            await _link_entry(hass, entry_id)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("iKuai 访问地址变化后重签 %s 失败", entry_id)

    tasks[entry_id] = hass.loop.create_task(_do_relink())


async def remember_origin_for_all_entries(hass, request) -> bool:
    """前端心跳：按浏览器当前地址栏 origin 更新全部条目（变化才落盘/重签）。"""
    origin = _normalize_base(str(request.url.origin()))
    if not origin:
        return False
    learned = _learned_store(hass)
    changed = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        if learned.get(entry.entry_id) == origin:
            continue
        learned[entry.entry_id] = origin
        changed.append(entry.entry_id)
    if not changed:
        return False
    await _persist_learned_bases(hass)
    for entry_id in changed:
        _schedule_relink(hass, entry_id)
    _LOGGER.info("iKuai 访问地址跟随浏览器地址栏更新为 %s（%s 个条目）",
                 origin, len(changed))
    return True


async def load_learned_bases(hass):
    """启动时恢复学到的访问基地址。"""
    try:
        from homeassistant.helpers.storage import Store

        data = await Store(hass, 1, _BASE_STORE_KEY,
                           private=True).async_load()
        bases = (data or {}).get("bases") or {}
        if isinstance(bases, dict):
            for k, v in bases.items():
                if _normalize_base(v):
                    _learned_store(hass)[k] = _normalize_base(v)
    except Exception:  # pylint: disable=broad-except
        _LOGGER.warning("iKuai 访问基地址记录读取失败", exc_info=True)


# --------------------------------------------------------------------------- #
# 代理自己的鉴权票（authSig 入口 + HttpOnly cookie 续用）
# --------------------------------------------------------------------------- #
def _hmac_secret(hass) -> bytes:
    secret = hass.data.get(DATA_SIGN_SECRET)
    if isinstance(secret, str):
        secret = secret.encode()
    if not secret:
        store = _store(hass)
        secret = store.get("fallback_secret")
        if not secret:
            secret = os.urandom(32)
            store["fallback_secret"] = secret
    return secret


def _issue_proxy_cookie(hass, entry_id: str) -> str:
    issued = str(int(time.time()))
    sig = hmac.new(_hmac_secret(hass), f"{entry_id}.{issued}".encode(),
                   hashlib.sha256).hexdigest()
    return f"{entry_id}.{issued}.{sig}"


def _verify_proxy_cookie(hass, value: str | None) -> str | None:
    """校验代理会话 cookie，返回 entry_id；失败返回 None。"""
    if not value or value.count(".") != 2:
        return None
    entry_id, issued, sig = value.split(".")
    expect = hmac.new(_hmac_secret(hass), f"{entry_id}.{issued}".encode(),
                      hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        if time.time() - int(issued) > PROXY_COOKIE_MAX_AGE:
            return None
    except ValueError:
        return None
    return entry_id


async def _verify_signature(hass, request) -> bool:
    """按 HA 官方规则验证 authSig（GET/HEAD）。"""
    if async_sign_path is None or jwt_wrapper is None:
        # 老版本无签名能力：仅放行同浏览器带 cookie 的请求，裸请求拒绝
        return False
    secret = hass.data.get(DATA_SIGN_SECRET)
    signature = request.query.get(SIGN_QUERY_PARAM)
    if not secret or not signature:
        return False
    try:
        claims = jwt_wrapper.verify_and_decode(
            signature, secret, algorithms=["HS256"], options={"verify_iss": False})
    except Exception:  # pylint: disable=broad-except
        return False
    if claims.get("path") != request.path:
        return False
    params = [list(item) for item in request.query.items()
              if item[0] not in SAFE_QUERY_PARAMS and item[0] != SIGN_QUERY_PARAM]
    if claims.get("params") != params:
        return False
    if hass.auth.async_get_refresh_token(claims.get("iss")) is None:
        return False
    return True


def _proxy_cookie_headers(hass, request, entry_id: str) -> list[str]:
    """生成下发给浏览器的 cookie 头（鉴权票 + SPA 需要的三件套）。"""
    secure = "; Secure" if request.scheme == "https" else ""
    base = (
        f"{PROXY_COOKIE_NAME}={_issue_proxy_cookie(hass, entry_id)}; "
        f"Path={_PREFIX}/; Max-Age={PROXY_COOKIE_MAX_AGE}; HttpOnly; "
        f"SameSite=Lax{secure}"
    )
    return [base]


def _spa_cookie_headers(request, sess: UpstreamSession) -> list[str]:
    """SPA 路由守卫要求的三个非 HttpOnly cookie（值为路由器真实会话）。"""
    secure = "; Secure" if request.scheme == "https" else ""
    common = f"Path={_PREFIX}/; Max-Age={PROXY_COOKIE_MAX_AGE}; SameSite=Lax{secure}"
    return [
        f"sess_key={sess.sess_key}; {common}",
        f"username={sess.username}; {common}",
        f"login=1; {common}",
    ]


# --------------------------------------------------------------------------- #
# 主代理视图
# --------------------------------------------------------------------------- #
class IKuaiWebProxyView(HomeAssistantView):
    """把 iKuai 整个 Web 反向代理到 /ikuai/web/<tail>。"""

    requires_auth = False
    url = _PREFIX + r"/{tail:.*}"
    name = "ikuai:web"

    async def _auth_entry_id(self, request, tail: str) -> str | None:
        """解析本次请求所属配置条目（cookie 优先，首次入口用 authSig）。"""
        hass = request.app["hass"]
        cookie_val = request.cookies.get(PROXY_COOKIE_NAME)
        entry_id = _verify_proxy_cookie(hass, cookie_val)
        if entry_id:
            return entry_id
        if request.method in ("GET", "HEAD") and SIGN_QUERY_PARAM in request.query:
            if await _verify_signature(hass, request):
                return request.query.get("entry")
        return None

    async def _handle(self, request, tail: str):
        hass = request.app["hass"]
        entry_id = await self._auth_entry_id(request, tail)
        if not entry_id:
            raise web.HTTPUnauthorized(
                text="iKuai proxy: missing or invalid session, "
                     "please open the link from the device page again.")
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise web.HTTPForbidden(text="iKuai proxy: config entry unavailable")
        sess = get_upstream(hass, entry)

        # WebSocket 升级（实时告警通道；VNC 通道为同源透传预留）
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await _bridge_websocket(request, sess, tail)

        if not sess.sess_key and not await sess.login():
            raise web.HTTPBadGateway(text="iKuai proxy: upstream login failed")

        return await _forward_http(request, sess, tail, retried=False)

    async def get(self, request, tail=""):
        return await self._handle(request, tail)

    async def post(self, request, tail=""):
        return await self._handle(request, tail)

    async def head(self, request, tail=""):
        return await self._handle(request, tail)

    async def put(self, request, tail=""):
        return await self._handle(request, tail)

    async def delete(self, request, tail=""):
        return await self._handle(request, tail)

    async def patch(self, request, tail=""):
        return await self._handle(request, tail)

    # 注意：不要自定义 options() —— aiohttp 会为每个资源自动注册 OPTIONS
    # 处理器（CORS/405），重复注册会抛 "already has OPTIONS handler"。
    # 代理全程 HA 同源，也不需要处理跨域预检。


class IKuaiWebEntryView(HomeAssistantView):
    """/ikuai/web（无尾斜杠）→ 签名入口跳转到带斜杠路径。"""

    requires_auth = False
    url = _PREFIX
    name = "ikuai:web_entry"

    async def get(self, request):
        target = _PREFIX + "/"
        if request.query_string:
            target += "?" + request.query_string
        raise web.HTTPFound(target)


class IKuaiOriginView(HomeAssistantView):
    """前端心跳：登记浏览器地址栏 origin，让设备链接跟随外部访问地址。

    requires_auth=True：本视图随 HA 前端页面加载，用浏览器已有的 HA 登录态
    （hass.fetchWithAuth 自带 Bearer），绝不能裸 fetch —— HA 会把未鉴权请求
    记为失败登录，攒够次数封 IP。后端直接读 request.url.origin()（由 Host
    头重建），无需请求体，天然就是地址栏的 host:port。
    """

    requires_auth = True
    url = PROXY_ORIGIN_PREFIX
    name = "ikuai:origin"

    async def get(self, request):
        hass = request.app["hass"]
        try:
            changed = await remember_origin_for_all_entries(hass, request)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("iKuai 访问地址心跳处理失败")
            return web.json_response({"ok": False}, status=500)
        return web.json_response({"ok": True, "changed": bool(changed)})


class IKuaiProxyStaticView(HomeAssistantView):
    """悬浮窗自身的前端资源（注入脚本 + 选项卡原生操作页）。"""

    requires_auth = False
    url = PROXY_STATIC_PREFIX + r"/{file_path:[a-zA-Z0-9_./-]+}"
    name = "ikuai:static"

    _CONTENT_TYPES = {
        ".js": "application/javascript; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }

    async def get(self, request, file_path: str):
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www_ikuai")
        # normpath + 前缀校验，杜绝 ../ 路径穿越
        local = os.path.normpath(os.path.join(root, file_path.replace("/", os.sep)))
        if not (local == root or local.startswith(root + os.sep)) \
                or not os.path.isfile(local):
            raise web.HTTPNotFound(text="embed frontend asset missing")
        ct = self._CONTENT_TYPES.get(
            os.path.splitext(local)[1].lower(), "application/octet-stream")
        return web.FileResponse(
            local, headers={"Content-Type": ct, "Cache-Control": "no-store"})


# --------------------------------------------------------------------------- #
# HTTP 转发
# --------------------------------------------------------------------------- #
def _upstream_url(sess: UpstreamSession, tail: str, query: str, websocket: bool = False):
    scheme, host_path = sess.base.split("://", 1)
    path = "/" + tail
    if websocket:
        scheme = "wss" if scheme == "https" else "ws"
    url = f"{scheme}://{host_path}{path}"
    if query:
        url += "?" + query
    return url


def _filtered_request_headers(request) -> dict:
    """构造上游请求头：丢弃浏览器侧 cookie/鉴权与逐跳头。"""
    skip = {"host", "cookie", "content-length", "authorization",
            # 不向上游做协商缓存：代理对文本有改写，必须始终拿全量重新处理，
            # 否则上游回 304 时浏览器会继续使用旧改写副本。
            "if-none-match", "if-modified-since"} | _HOP_HEADERS
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in skip and not k.lower().startswith("x-forwarded")}
    # 该固件静态资源只在 gzip 预压缩文件存在时才 200
    headers["Accept-Encoding"] = "gzip"
    return headers


def _rewrite_location(value: str, sess: UpstreamSession) -> str:
    """把上游 302 Location 改写成代理前缀路径（外站地址原样返回）。"""
    if not value:
        return value
    if value.startswith("/"):
        return value if value.startswith(_PREFIX) else _PREFIX + value
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    base = urlsplit(sess.base)
    if parts.netloc and parts.netloc != base.netloc:
        return value  # 外站链接不动
    path = parts.path
    if not path.startswith(_PREFIX):
        path = _PREFIX + path
    from urllib.parse import urlunsplit

    return urlunsplit(("", "", path, parts.query, parts.fragment))


# 超过该大小（字节）的请求体一律流式转发，不读入内存（ISO/备份上传）
_STREAM_THRESHOLD = 512 * 1024


async def _forward_http(request, sess: UpstreamSession, tail: str, retried: bool):
    """把单个 HTTP 请求转发到路由器并处理响应。"""
    query = request.query_string
    # authSig 只用于入口鉴权，绝不转发给路由器
    if SIGN_QUERY_PARAM in request.query:
        query = "&".join(f"{k}={v}" for k, v in request.query.items()
                         if k != SIGN_QUERY_PARAM)

    upstream_url = _upstream_url(sess, tail, query)
    method = request.method
    headers = _filtered_request_headers(request)

    # 静态资源命中本设备内存缓存：直接由代理应答（含 304），完全不碰路由器
    cache_kind = _static_cache_kind(tail) if method == "GET" else None
    # 爱快原生虚拟机 VNC 页要按"具体虚拟机"注入端口/密码引导脚本，属于动态响应，
    # 绝不能进静态资源缓存（否则不同虚拟机都会拿到同一台的注入副本）
    if cache_kind and tail == VNC_NATIVE_PAGE_TAIL:
        cache_kind = None
    cache_key = None
    if cache_kind:
        cache_key = tail + ("?" + query if query else "")
        cached = sess.get_cached(cache_key)
        if cached is not None:
            return _asset_response(cached, method,
                                   request.headers.get("If-None-Match"))

    # 请求体：小 JSON 读入内存（过期可重放）；multipart/大文件直接流式管道
    body = None
    stream_payload = None
    content_length = request.headers.get("Content-Length")
    is_upload = method in ("POST", "PUT", "PATCH") and (
        int(content_length or 0) > _STREAM_THRESHOLD
        or "multipart/" in request.headers.get("Content-Type", ""))
    if is_upload:
        stream_payload = request.content
    elif method in ("POST", "PUT", "PATCH"):
        body = await request.read()

    try:
        upstream = await sess.session.request(
            method, upstream_url, headers=headers,
            data=stream_payload if stream_payload is not None else body,
            allow_redirects=False)
    except aiohttp.ClientError as exc:
        _LOGGER.warning("iKuai 代理上游连接失败 %s: %r", upstream_url, exc)
        raise web.HTTPBadGateway(text="upstream connection error") from exc

    async with upstream:
        status = upstream.status
        location = upstream.headers.get("Location", "")
        payload = await upstream.read()

        # 登出：不做"过期重登"（否则会立刻重新登录），清本代理会话后原样返回，
        # 302 Location 改写为代理前缀（SPA 随后显示登录页）
        if tail == "Action/logout" or tail.endswith("/Action/logout"):
            sess.invalidate()

        # 会话过期：302 跳 /login 或 Action 返回 10014 → 重新登录并重放一次。
        # 流式上传不重放（请求体已消费），直接 401 让前端重试。
        elif not retried and not stream_payload:
            need_relogin = False
            if status in (301, 302, 303, 307, 308) and "/login" in location:
                need_relogin = True
            elif tail.startswith("Action/") and 200 <= status < 300:
                if "10014" in payload[:256].decode("utf-8", "replace"):
                    need_relogin = True
            if need_relogin:
                sess.invalidate()
                if await sess.login():
                    return await _forward_http(request, sess, tail, retried=True)
                raise web.HTTPBadGateway(text="iKuai session expired, re-login failed")

        # 登录请求成功：同步 cookie jar 里的新 sess_key（用户在 iframe 里手输密码）
        if (tail == "Action/login" or tail.endswith("/Action/login")) and 200 <= status < 300:
            new_key = sess._sess_key_from_jar()  # pylint: disable=protected-access
            if new_key:
                sess.sess_key = new_key

        if status == 304:
            return web.Response(status=304)

        content_type = upstream.headers.get("Content-Type", "")
        headers_out = _passthrough_headers(upstream.headers)
        if location:
            headers_out["Location"] = _rewrite_location(location, sess)

        # 非文本响应：字节原样透传（下载文件/图片/字体/分片数据）
        if not _is_text(content_type, tail):
            if cache_kind and status == 200:
                # 二进制静态资源不做改写，缓存原始字节（206 分片不缓存）
                asset = sess.store_cached(
                    cache_key, cache_kind, status, payload,
                    content_type or "application/octet-stream")
                return _asset_response(asset, method, None)
            return web.Response(status=status, body=payload, headers=headers_out)

        # 文本响应：解压已由 aiohttp 自动完成，按类型改写绝对路径
        charset = "utf-8"
        if "charset=" in content_type:
            charset = content_type.split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        text = payload.decode(charset, "replace")

        url_path = "/" + tail
        if "html" in content_type.lower() or url_path.endswith(("/", "/login", ".html")):
            text = _rewrite_html(text)
        elif "css" in content_type.lower():
            text = _rewrite_css(text)
        else:
            text = _rewrite_js(text)

        # 爱快原生虚拟机 VNC 页：注入"WS 走 HA 同源代理 + 自动填 VNC 密码"引导脚本
        if tail == VNC_NATIVE_PAGE_TAIL:
            text = _inject_native_vnc(
                request.app["hass"], sess.entry_id, request.query, text)

        data = text.encode(charset, "replace")
        if cache_kind and status == 200:
            # 静态 JS/CSS/SVG：缓存"改写后"副本，后续请求零回源、零重写；
            # ETag 由代理基于改写字节自签，缓存策略由 _asset_response 统一发出
            asset_path = tail.lower().split("?", 1)[0]
            if not content_type:
                content_type = ("text/css; charset=utf-8" if asset_path.endswith(".css")
                                else "image/svg+xml" if asset_path.endswith(".svg")
                                else "application/javascript; charset=utf-8")
            asset = sess.store_cached(cache_key, cache_kind, status, data, content_type)
            return _asset_response(asset, method, None)
        headers_out["Content-Type"] = content_type or "text/html; charset=utf-8"
        # 非缓存文本（HTML 入口、动态 JSON 等）不能沿用上游 Etag/协商缓存：
        # 否则上游回 304 时浏览器会继续用本地"旧改写规则"生成的副本。
        # 静态 chunk 已在上面短路，这里统一 no-store。
        headers_out.pop("etag", None)
        headers_out.pop("Etag", None)
        headers_out.pop("ETag", None)
        headers_out.pop("last-modified", None)
        headers_out.pop("Last-Modified", None)
        headers_out["Cache-Control"] = "no-store"
        resp = web.Response(status=status, body=data, headers=headers_out)

        # HTML 文档：补/续鉴权 cookie + SPA 守卫三件套
        is_doc = ("html" in content_type.lower()
                  or url_path.endswith(("/", "/login"))
                  or (not tail and status == 200))
        if is_doc:
            for cookie in _proxy_cookie_headers(request.app["hass"], request, sess.entry_id):
                resp.headers.add("Set-Cookie", cookie)
            if sess.sess_key:
                for cookie in _spa_cookie_headers(request, sess):
                    resp.headers.add("Set-Cookie", cookie)

        # 登录 JSON：也下发 SPA 三件套 cookie（保证手输密码后路由守卫通过）
        if (tail == "Action/login" or tail.endswith("/Action/login")) and sess.sess_key \
                and b"10000" in payload:
            for cookie in _spa_cookie_headers(request, sess):
                resp.headers.add("Set-Cookie", cookie)
        return resp


def _passthrough_headers(upstream_headers) -> dict:
    """透传上游响应头：剔除逐跳头、上游 Set-Cookie 与 Location（单独处理）。"""
    out = {}
    for k, v in upstream_headers.items():
        lk = k.lower()
        if lk in _HOP_HEADERS or lk in ("set-cookie", "location"):
            continue
        out[k] = v
    return out


# --------------------------------------------------------------------------- #
# WebSocket 桥接（/Action/rtcollect/websocket 实时告警）
# --------------------------------------------------------------------------- #
async def _bridge_websocket(request, sess: UpstreamSession, tail: str):
    if not sess.sess_key and not await sess.login():
        raise web.HTTPBadGateway(text="iKuai proxy: upstream login failed")

    client_ws = web.WebSocketResponse(protocols=tuple(
        p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()),
        max_msg_size=0)
    await client_ws.prepare(request)
    subprotocol = getattr(client_ws, "ws_protocol", None)
    if not isinstance(subprotocol, str):
        legacy = getattr(client_ws, "protocol", None)
        subprotocol = legacy if isinstance(legacy, str) and legacy else None

    query = "&".join(f"{k}={v}" for k, v in request.query.items()
                     if k != SIGN_QUERY_PARAM)
    upstream_url = _upstream_url(sess, tail, query, websocket=True)
    protocols = (subprotocol,) if subprotocol else ()

    try:
        async with sess.session.ws_connect(
            upstream_url, protocols=protocols, max_msg_size=0,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
        ) as upstream_ws:

            async def pump_up():
                async for msg in client_ws:
                    if msg.type == WSMsgType.BINARY:
                        await upstream_ws.send_bytes(msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        await upstream_ws.send_str(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break

            async def pump_down():
                async for msg in upstream_ws:
                    if msg.type == WSMsgType.BINARY:
                        await client_ws.send_bytes(msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        await client_ws.send_str(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break

            task_up = asyncio.create_task(pump_up())
            task_down = asyncio.create_task(pump_down())
            try:
                await asyncio.wait({task_up, task_down},
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                task_up.cancel()
                task_down.cancel()
    except aiohttp.ClientError as exc:
        _LOGGER.warning("iKuai WebSocket 代理失败 %s: %r", tail, exc)
        if not client_ws.closed:
            await client_ws.close(code=1011, message=b"upstream ws failed")
    return client_ws


# --------------------------------------------------------------------------- #
# 设备 configuration_url 签名管理
# --------------------------------------------------------------------------- #
def sign_browser_path(hass, entry_id: str, path: str, expiry) -> str | None:
    """给一条本集成的路径签 authSig，并拼上浏览器可达的基地址。

    host 部分取学到的浏览器访问基地址（端口转发/域名下自动跟随地址栏，
    见模块前部的基地址学习），兜底 HA 自探测地址。签名固定使用 Home
    Assistant Content 系统用户的持久 refresh token（use_content_user=True），
    避免误签成会随浏览器登出失效的用户会话令牌。

    设备「访问」链接（Web 悬浮窗、虚拟机远程控制台页面）都走这里，
    保证两处永远跟随同一个基地址、同一套签名有效期。
    """
    signed = path
    if async_sign_path is not None:
        try:
            signed = async_sign_path(hass, path, expiry, use_content_user=True)
        except TypeError:
            # 老版本 async_sign_path 无 use_content_user 关键字
            try:
                signed = async_sign_path(hass, path, expiry)
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("iKuai 路径签名失败: %s", path)
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("iKuai 路径签名失败: %s", path)
    base_url = _configured_base(hass, entry_id)
    return (base_url + signed) if base_url else signed


def signed_entry_url(hass, entry_id: str) -> str | None:
    """生成主机设备"访问"链接：<浏览器可达 origin>/ikuai/web/?entry=&authSig=（24h）。"""
    return sign_browser_path(hass, entry_id, f"{_PREFIX}/?entry={entry_id}", _SIGN_EXPIRY)


def get_signed_url(hass, entry_id: str) -> str | None:
    """实体 device_info 使用的当前签名链接（12 小时重签一次）。"""
    return _store(hass).get("urls", {}).get(entry_id)


async def _link_entry(hass, entry_id: str):
    """把设备 configuration_url 刷新为签名代理地址。"""
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None:
        return
    url = signed_entry_url(hass, entry_id)
    if not url:
        return
    _store(hass).setdefault("urls", {})[entry_id] = url
    dev_reg = dr.async_get(hass)
    # HA 2026：模块级函数（旧版 DeviceRegistry.async_entries_for_config_entry 已移除）
    for device in dr.async_entries_for_config_entry(dev_reg, entry_id):
        if {(DOMAIN, entry.data[CONF_HOST])} & set(device.identifiers):
            dev_reg.async_update_device(device.id, configuration_url=url)

    # 虚拟机远程控制台的链接共用同一套基地址与重签节奏（懒导入避免循环依赖）：
    # 基地址学习到新值 / 12 小时定时重签 / 条目启动，都会顺手把控制台链接一起刷新。
    try:
        from .console import async_refresh_console_links

        await async_refresh_console_links(hass, entry_id)
    except Exception:  # pylint: disable=broad-except - 控制台不可用不影响 Web 代理
        _LOGGER.debug("iKuai 远程控制台链接刷新失败", exc_info=True)


def async_link_entry(hass, entry_id: str):
    """同步安全地刷新一个条目的设备链接。"""
    hass.async_create_task(_link_entry(hass, entry_id))


async def async_setup_proxy(hass, entry):
    """注册代理视图（全局一次）并刷新本条目设备链接。"""
    store = _store(hass)

    if not store.get("views_registered"):
        hass.http.register_view(IKuaiWebEntryView)
        hass.http.register_view(IKuaiWebProxyView)
        hass.http.register_view(IKuaiProxyStaticView)
        hass.http.register_view(IKuaiOriginView)
        store["views_registered"] = True

        # 恢复历史学到的浏览器访问地址（重启后设备链接/远程地址传感器仍跟随）
        await load_learned_bases(hass)

        # 注入悬浮窗拦截脚本（官方 frontend API）；带版本号避免浏览器用缓存的旧脚本
        embed_url = f"{PROXY_STATIC_PREFIX}/frontend/embed.js?v={EMBED_ASSET_VERSION}"
        try:
            from homeassistant.components import frontend as _frontend

            _frontend.add_extra_js_url(hass, embed_url)
            store["embed_url"] = embed_url
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning("iKuai 悬浮窗前端模块注册失败（不影响直接访问代理页）",
                            exc_info=True)

        # 12 小时全量重签，保证 24 小时链接不断档
        async def _on_interval(_now):
            for ent in hass.config_entries.async_entries(DOMAIN):
                await _link_entry(hass, ent.entry_id)

        unsub = async_track_time_interval(hass, _on_interval, _RESIGN_INTERVAL)

        async def _on_stop(_event):
            unsub()
            embed = store.get("embed_url")
            if embed:
                try:
                    from homeassistant.components import frontend as _frontend

                    _frontend.remove_extra_js_url(hass, embed)
                except Exception:  # pylint: disable=broad-except
                    pass
            for sess in list(store.get("sessions", {}).values()):
                await sess.close()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _on_stop)
        _LOGGER.debug("iKuai Web 代理视图已注册")

    await _link_entry(hass, entry.entry_id)


async def async_unload_proxy(hass, entry):
    """条目卸载：关闭上游会话并清签名缓存；视图/JS 全局保留（可能还有别的条目）。"""
    store = _store(hass)
    sess = store.get("sessions", {}).pop(entry.entry_id, None)
    if sess:
        await sess.close()
    store.get("urls", {}).pop(entry.entry_id, None)
