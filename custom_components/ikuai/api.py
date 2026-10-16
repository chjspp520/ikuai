"""按需查询 API：把爱快 Web 接口**原样透传**出去（可选功能，默认关闭）。

定位：纯通道
------------
不封装、不改写、不脱敏：

* 请求体就是爱快原生载荷（``func_name`` / ``action`` / ``param``）；
* 响应体就是爱快的原始响应（``Result`` / ``ErrMsg`` / ``Data`` 原样返回）。

所以爱快原生**所有**接口都能用（200+ 个 func_name），固件升级也不会过期。

四种访问方式
------------
1. **浏览器地址栏**（最直接）::

       http://<你的HA>/ikuai/api/call?key=<密钥>&func_name=homepage&action=show&TYPE=sysstat
       http://<你的HA>/ikuai/api/call?key=<密钥>&func_name=monitor_lanip&action=show&TYPE=data,total&limit=0,100

   除 ``key`` / ``entry`` / ``func_name`` / ``action`` 之外的参数会被原样收进
   ``param``。自带 key 的来源见 ``sensor.ikuai_api_key``（1 小时自动轮换）。

2. **POST 单次**：body 就是载荷本体。

3. **POST 批量**：``{"calls": [{...}, {...}]}``，一次最多 ``API_CALLS_MAX`` 个。
   分页也靠它：爱快 ``limit`` 单次最多 100 条，想拿全就把 ``0,100`` /
   ``100,100`` / ``200,100`` 放进同一个 ``calls`` 一次性取回。

4. **HA 登录态**（``hass.fetchWithAuth``）：与 key 二选一，前端卡片用这种。

安全模型（按使用者的选择）
--------------------------
* 开关（``switch.ikuai_api``）关闭时一律 403，一个上游请求都不发；
* **写操作不限制**：``action`` 原样透传（rebuild / reboot / del 等都能发）；
* **不脱敏**：爱快返回什么就返回什么（含 passwd / token 的接口也原样返回）。

也就是说：**这个 key 等价于爱快后台的操作权限**，只在本机/内网用，别外发。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

from .const import (
    ACTION_URL,
    API_CALL_PATH,
    API_CALLS_MAX,
    API_ENABLED_DEFAULT,
    API_KEY_LEN,
    API_KEY_TTL_SECONDS,
    API_VIEW_NAME,
    COORDINATOR,
    DOMAIN,
)

try:  # HA 用它把已鉴权用户挂在请求上
    from homeassistant.components.http.consts import KEY_HASS_USER
except ImportError:  # pragma: no cover
    KEY_HASS_USER = "hass_user"

_LOGGER = logging.getLogger(__name__)

# hass.data[DOMAIN][API_STATE_KEY]：本集成的 API 运行时状态
API_STATE_KEY = "api_state"
# GET 查询串里的保留参数，其余一律进 param
_QUERY_RESERVED = ("key", "entry", "func_name", "action")
# GET 里显式指定"整段 JSON 载荷"的参数名（不写参数名、直接跟在 ? 后也认）
_BODY_PARAMS = ("payload", "body")


class ApiCallError(Exception):
    """单次透传失败（只影响它自己）。"""


# --------------------------------------------------------------------------- #
# 运行时状态：开关 / 统计 / 密钥
# --------------------------------------------------------------------------- #
def _empty_stats() -> dict:
    return {"calls": 0, "results": 0, "errors": 0, "key_hits": 0, "last_call": None}


def api_store(hass) -> dict:
    """取（并按需初始化）本集成的 API 运行时状态。"""
    domain_data = hass.data.setdefault(DOMAIN, {})
    store = domain_data.setdefault(API_STATE_KEY, {})
    store.setdefault("enabled", {})      # entry_id -> bool
    store.setdefault("keys", {})         # entry_id -> {hour, key, prev_key, cpu, created}
    store.setdefault("stats", {})        # entry_id -> 调用统计（多台爱快各自独立）
    if "calls" in store["stats"]:        # 兼容早期「全局一份」的结构
        store["stats"] = {}
    return store


def api_stats(hass, entry_id) -> dict:
    """某台爱快的调用统计（多台各自独立计数，互不干扰）。"""
    return api_store(hass)["stats"].setdefault(entry_id or "", _empty_stats())


def is_api_enabled(hass, entry_id) -> bool:
    """该条目是否启用了查询 API（没有记录时按默认值：关闭）。"""
    return bool(api_store(hass)["enabled"].get(entry_id, API_ENABLED_DEFAULT))


def set_api_enabled(hass, entry_id, enabled: bool) -> None:
    """开关实体切换时登记状态。"""
    api_store(hass)["enabled"][entry_id] = bool(enabled)


def entry_ids(hass) -> list:
    """本集成当前已加载的条目 id（装了几台爱快就有几个）。"""
    domain_data = hass.data.get(DOMAIN) or {}
    return [key for key, value in domain_data.items()
            if isinstance(value, dict) and COORDINATOR in value]


def resolve_entry(hass, requested=None, candidate=None):
    """定位本次请求要打哪台路由器。

    顺序：显式 ``entry`` → **key 命中的那一条** → 只有一台时的它。

    key 里带着条目 id（每台的 key 互不相同），所以装了多台爱快时，只给 key 也能
    自动定位到对应那台；都定位不到就返回明确的 ``entry_required`` —— 早期多条目会
    直接回一个笼统的 unauthorized，很容易被误判成"密钥错了"。

    返回 ``(entry_id, problem)``，``problem`` 非空表示没能定位。
    """
    ids = entry_ids(hass)
    if requested:
        return (requested, None) if requested in ids else (None, "entry_unknown")
    if not ids:
        return None, "entry_not_loaded"
    if len(ids) == 1:
        return ids[0], None
    if candidate:
        hits = [entry_id for entry_id in ids
                if key_matches(hass, entry_id, candidate)]
        if len(hits) == 1:
            return hits[0], None
    return None, "entry_required"


def _context(hass, entry_id):
    """取某条目的取数上下文（coordinator / fetcher / 当前快照）。"""
    entry_data = (hass.data.get(DOMAIN) or {}).get(entry_id) or {}
    coordinator = entry_data.get(COORDINATOR)
    if coordinator is None:
        return None
    return {
        "hass": hass,
        "entry_id": entry_id,
        "coordinator": coordinator,
        "fetcher": getattr(coordinator, "_fetcher", None),
        "data": coordinator.data if isinstance(coordinator.data, dict) else {},
    }


# --------------------------------------------------------------------------- #
# 密钥：整点换新，旧 key 宽限一小时
# --------------------------------------------------------------------------- #
def _hour_index(now=None):
    """当前小时序号（UTC 整点，用作密钥轮换刻度）。"""
    return int((now if now is not None else time.time()) // API_KEY_TTL_SECONDS)


def _salt(hass) -> str:
    """集成私有盐：随机生成、只存在内存，用于让 key 无法被别人从 CPU 值反推。"""
    return api_store(hass).setdefault("salt", os.urandom(16).hex())


def _cpu_fingerprint(data) -> str:
    """取一份"当时"的 CPU 指纹（占用 + 温度），作为密钥的熵源。"""
    data = data if isinstance(data, dict) else {}
    cpu = str(data.get("ikuai_cpu") or "").strip()
    temp = str(data.get("ikuai_cputemp") or "").strip()
    return f"{cpu}:{temp}" if (cpu or temp) else "unknown"


def _make_key(hass, hour, cpu, entry_id="") -> str:
    """key = md5(集成盐 | 小时序号 | CPU 指纹 | 条目 id)，取前 API_KEY_LEN 位。

    加盐是为了：别人即使能在爱快后台看到你的 CPU 值，也算不出 key。
    末尾带条目 id：装了多台爱快时，每台的 key 互不相同 —— 请求只给 key 也能定位到
    是哪一台（见 :func:`resolve_entry`），且两台机器 CPU 占用/温度相同时也不会撞 key。
    """
    raw = f"{_salt(hass)}|{hour}|{cpu}|{entry_id}".encode()
    return hashlib.md5(raw).hexdigest()[:API_KEY_LEN]


def ensure_key(hass, entry_id, data=None):
    """保证当前小时有 key，返回该条目的密钥状态（供视图校验与传感器展示）。"""
    store = api_store(hass)
    state = store["keys"].get(entry_id) or {}
    hour = _hour_index()
    if state.get("hour") != hour:
        cpu = _cpu_fingerprint(data if data is not None
                               else (_context(hass, entry_id) or {}).get("data"))
        state = {
            "hour": hour,
            "key": _make_key(hass, hour, cpu, entry_id),
            "prev_key": state.get("key"),      # 上一小时的 key 再宽限一小时
            "cpu": cpu,
            "created": time.time(),
        }
        store["keys"][entry_id] = state
    return state


def key_matches(hass, entry_id, candidate) -> bool:
    """校验 key：当前小时的、以及上一小时的（宽限期内）都算有效。"""
    candidate = str(candidate or "").strip()
    if not candidate:
        return False
    state = ensure_key(hass, entry_id)
    valid = {state.get("key"), state.get("prev_key")} - {None}
    return candidate in valid


def key_info(hass, entry_id, data=None):
    """传感器要展示的密钥信息（含剩余有效期）。"""
    state = ensure_key(hass, entry_id, data)
    expires_at = (state["hour"] + 1) * API_KEY_TTL_SECONDS
    remaining = max(0, int(expires_at - time.time()))
    return {
        "key": state["key"],
        "hour": state["hour"],
        "cpu": state.get("cpu"),
        "valid_until": time.strftime("%Y-%m-%d %H:%M:%S",
                                     time.localtime(expires_at)),
        "remaining_seconds": remaining,
        "remaining_minutes": max(1, (remaining + 59) // 60),
        "created": state.get("created"),
    }


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
def authorized(hass, request, entry_id, candidate=None) -> bool:
    """两种鉴权任一通过即可：HA 登录态，或 API key（``?key=`` / ``X-IKuai-Key``）。"""
    if request.get(KEY_HASS_USER) is not None:
        return True
    if candidate is None:
        candidate = (request.query.get("key")
                     or request.headers.get("X-IKuai-Key") or "")
    if not candidate or not entry_id:
        return False
    if key_matches(hass, entry_id, candidate):
        api_stats(hass, entry_id)["key_hits"] += 1
        return True
    return False


# --------------------------------------------------------------------------- #
# 透传
# --------------------------------------------------------------------------- #
def call_from_query(query):
    """把 GET 查询串拼成爱快载荷（地址栏用）。两种写法都认：

    1. **平铺**（推荐，最像普通查询串）::

           ?key=..&func_name=url_black&action=show&TYPE=url_white_refer

    2. **整段 JSON 塞进来**（省略参数名，写起来最省事）::

           ?key=..&{"func_name":"url_black","action":"show","param":{...}}

       也可以显式命名 ``payload=`` / ``body=``。JSON 里的值含 ``&`` ``#`` ``+``
       空格或中文时必须先做 URL 编码，否则会被查询串规则截断 —— 复杂载荷建议
       直接用 POST。
    """
    for name, value in query.items():
        raw = None
        if name in _BODY_PARAMS:
            raw = value
        elif not value and name.lstrip().startswith("{"):
            raw = name          # 省略参数名：整段 JSON 自己就是"键"
        if raw:
            try:
                parsed = json.loads(raw)
            except ValueError as err:
                raise ApiCallError(f"invalid_payload_json: {err}") from err
            if not isinstance(parsed, dict):
                raise ApiCallError("payload_must_be_object")
            if isinstance(parsed.get("calls"), list):
                raise ApiCallError("use_post_for_batch")
            return parsed

    param = {key: value for key, value in query.items()
             if key not in _QUERY_RESERVED}
    call = {"func_name": query.get("func_name") or "",
            "action": query.get("action") or "show"}
    if param:
        call["param"] = param
    return call


def normalize_call(raw):
    """规整一份载荷（POST 用）：只认 func_name / action / param。"""
    if not isinstance(raw, dict):
        raise ApiCallError("call_must_be_object")
    func = str(raw.get("func_name") or "").strip()
    if not func:
        raise ApiCallError("missing_func_name")
    param = raw.get("param")
    call = {"func_name": func,
            "action": str(raw.get("action") or "show").strip()}
    if isinstance(param, dict):
        call["param"] = param
    return call


def _is_session_invalid(response) -> bool:
    return isinstance(response, dict) and response.get("Result") == 10014


async def _relogin(ctx):
    """会话失效时清掉 token 缓存，下次取号会重新登录。"""
    try:
        ctx["coordinator"]._token_expire_time = 0  # noqa: SLF001
    except Exception:  # pylint: disable=broad-except
        pass


async def _post_raw(ctx, sess_key, body):
    """把载荷发给爱快，返回**原始响应**（dict 或 None）。"""
    fetcher = ctx["fetcher"]
    return await fetcher.requestpost_json(  # noqa: SLF001
        fetcher._host + ACTION_URL, fetcher._header(sess_key), body)  # noqa: SLF001


async def execute_call(ctx, call):
    """透传一次调用，返回 ``(body, error)`` —— body 是爱快原始响应。"""
    fetcher = ctx["fetcher"]
    if fetcher is None:
        return None, "no_fetcher"
    try:
        sess_key = await ctx["coordinator"].get_access_token()
    except Exception:  # pylint: disable=broad-except
        sess_key = None
    if not sess_key:
        return None, "no_session"

    response = await _post_raw(ctx, sess_key, call)
    if _is_session_invalid(response):
        # 会话失效：清缓存重登一次再试（对调用者透明）
        await _relogin(ctx)
        try:
            sess_key = await ctx["coordinator"].get_access_token()
        except Exception:  # pylint: disable=broad-except
            sess_key = None
        if sess_key:
            response = await _post_raw(ctx, sess_key, call)
    if response is None:
        return None, "no_response"
    if not isinstance(response, dict):
        return {"Result": 30000, "ErrMsg": "Success", "Data": response}, None
    return response, None


# --------------------------------------------------------------------------- #
# 常用请求体（纯文档：开关属性里列出来，复制就能用；不是接口契约）
# --------------------------------------------------------------------------- #
API_PRESETS = (
    {"name": "系统状态", "desc": "CPU/内存/温度/在线终端数/连接数",
     "call": {"func_name": "homepage", "action": "show",
              "param": {"TYPE": "sysstat,ac_status"}}},
    {"name": "在线终端列表", "desc": "monitor_lanip（爱快 limit 单次最多 100 条）",
     "call": {"func_name": "monitor_lanip", "action": "show",
              "param": {"TYPE": "data,total", "limit": "0,100"}}},
    {"name": "单台终端·应用流量", "desc": "把 ip 换成你的终端",
     "call": {"func_name": "monitor_lanip", "action": "show",
              "param": {"TYPE": "terminal_flow", "ip": "192.168.5.14",
                        "limit": "0,100", "ORDER_BY": "", "ORDER": ""}}},
    {"name": "单台终端·连接", "desc": "把 ip 换成你的终端",
     "call": {"func_name": "monitor_lanip", "action": "show",
              "param": {"TYPE": "conn,conn_num", "ip": "192.168.5.14",
                        "interface": "all", "proto": "all", "maxnum": 500,
                        "limit": "0,100", "ORDER_BY": "", "ORDER": ""}}},
    {"name": "WAN 线路", "desc": "每口 IP / 网关 / 上下行累计",
     "call": {"func_name": "wan", "action": "show",
              "param": {"TYPE": "data,total"}}},
    {"name": "LAN 口信息", "desc": "网口 / 地址 / 掩码",
     "call": {"func_name": "lan", "action": "show",
              "param": {"TYPE": "snapshoot,snapshoot_lan"}}},
    {"name": "ARP 表", "desc": "全部 ARP 条目",
     "call": {"func_name": "arp", "action": "show",
              "param": {"TYPE": "total,data", "limit": "0,100"}}},
    {"name": "DHCP 租约", "desc": "动态租约（可能超过一页，用 calls 批量翻页）",
     "call": {"func_name": "dhcp_lease", "action": "show",
              "param": {"TYPE": "data,total", "limit": "0,100"}}},
    {"name": "磁盘信息", "desc": "每块盘的分区与容量",
     "call": {"func_name": "disk_mgmt", "action": "show",
              "param": {"TYPE": "data"}}},
    {"name": "规则条数（示例：ACL）", "desc": "把 func_name 换成 acl/nat_rule/dnat…",
     "call": {"func_name": "acl", "action": "show",
              "param": {"TYPE": "total,data", "limit": "0,100"}}},
    {"name": "网址黑白名单（多参数示例）",
     "desc": "param 里有几项，GET 里就平铺几个查询参数：TYPE=url_white_refer",
     "call": {"func_name": "url_black", "action": "show",
              "param": {"TYPE": "url_white_refer"}}},
    {"name": "⚠ 写操作示例：重启", "desc": "这个通道能写 —— 别外发 key",
     "call": {"func_name": "reboot", "action": "call", "param": {}}},
)


def _flatten(query: dict) -> str:
    """把保留参数之外的内容拼成 GET 查询串（示例用，不做 URL 编码）。"""
    return "&".join(f"{key}={value}" for key, value in query.items())


def _base_url(hass, entry_id) -> str:
    """本集成的完整基地址（复用 web_proxy 那套：学到的浏览器地址 → HA 自探测）。"""
    if hass is None:
        return ""
    try:
        from .web_proxy import _configured_base  # 局部导入，避免模块级耦合

        return (_configured_base(hass, entry_id) or "").rstrip("/")
    except Exception:  # pylint: disable=broad-except
        return ""


def preset_entries(hass=None, entry_id=None, key=None):
    """开关属性里的「常用请求体 + 完整请求地址」。

    * ``get`` 给的是**完整可粘贴地址**：基地址取本集成学到的浏览器访问地址
      （端口转发/域名自动跟随），兜底 HA 自探测地址；给了 ``key`` 就把密钥一起
      嵌进去，复制到地址栏即可用（没给则用 ``<密钥>`` 占位）；
    * ``post`` 给多行 JSON 原文，直接当请求体。
    """
    base = _base_url(hass, entry_id)
    token = key or "<密钥>"
    entries = []
    for item in API_PRESETS:
        call = item["call"]
        # param 里的键值在 GET 里一律平铺（key/entry 之外都是爱快的 param 字段）
        get_query = {"key": token}
        if entry_id:
            # 带上条目 id：装了多台爱快时指名打哪台；单台带上也无害 ——
            # 以后再加一台路由器，今天复制出去的地址依然有效
            get_query["entry"] = entry_id
        get_query.update({"func_name": call["func_name"],
                          "action": call.get("action", "show"),
                          **(call.get("param") or {})})
        entries.append({
            "name": item["name"],
            "desc": item.get("desc", ""),
            # 完整地址：浏览器地址栏直接粘这条
            "get": f"{base}{API_CALL_PATH}?{_flatten(get_query)}",
            # POST body（多行 JSON 原样给）
            "post": json.dumps(call, ensure_ascii=False, indent=2),
        })
    return entries


# --------------------------------------------------------------------------- #
# HTTP 视图：GET 走地址栏，POST 走脚本/前端
# --------------------------------------------------------------------------- #
class IKuaiCallView(HomeAssistantView):
    """``/ikuai/api/call``：GET（地址栏）/ POST（单个或批量）原样透传爱快接口。"""

    requires_auth = False        # 自己校验：HA 登录态 或 API key
    url = API_CALL_PATH
    name = API_VIEW_NAME

    async def get(self, request):
        hass = request.app["hass"]
        candidate = (request.query.get("key")
                     or request.headers.get("X-IKuai-Key") or "")
        entry_id, problem = resolve_entry(hass, request.query.get("entry"), candidate)
        if problem:
            return _entry_error(hass, request, problem)
        if not authorized(hass, request, entry_id, candidate):
            return _unauthorized(hass)
        if not is_api_enabled(hass, entry_id):
            return _disabled()
        ctx = _context(hass, entry_id)
        if ctx is None:
            return web.json_response({"error": "entry_not_loaded"}, status=503)
        try:
            call = normalize_call(call_from_query(request.query))
        except ApiCallError as err:
            message = str(err)
            return web.json_response(
                {"error": message,
                 "hint": _ERROR_HINTS.get(message.split(":", 1)[0], _GET_HINT)},
                status=400)
        return await self._run(ctx, [call], single=True)

    async def post(self, request):
        hass = request.app["hass"]
        try:
            body = await request.json()
        except Exception:  # pylint: disable=broad-except
            return web.json_response({"error": "invalid_json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "invalid_body"}, status=400)

        candidate = (request.query.get("key")
                     or request.headers.get("X-IKuai-Key") or "")
        requested = (body.get("entry") or body.get("entry_id")
                     or request.query.get("entry"))
        entry_id, problem = resolve_entry(hass, requested, candidate)
        if problem:
            return _entry_error(hass, request, problem)
        if not authorized(hass, request, entry_id, candidate):
            return _unauthorized(hass)
        if not is_api_enabled(hass, entry_id):
            return _disabled()
        ctx = _context(hass, entry_id)
        if ctx is None:
            return web.json_response({"error": "entry_not_loaded"}, status=503)

        raw_calls = body.get("calls")
        single = not isinstance(raw_calls, list)
        raw_calls = [body] if single else raw_calls
        truncated = False
        if len(raw_calls) > API_CALLS_MAX:
            raw_calls, truncated = raw_calls[:API_CALLS_MAX], True
        calls = []
        for raw in raw_calls:
            try:
                calls.append(normalize_call(raw))
            except ApiCallError as err:
                calls.append({"__error__": str(err)})
        return await self._run(ctx, calls, single=single, truncated=truncated)

    async def _run(self, ctx, calls, single, truncated=False):
        """并发执行（总量受 fetcher 内的并发信号量约束，见 const.HTTP_CONCURRENCY），原样回包。"""
        hass = ctx["hass"]
        stats = api_stats(hass, ctx["entry_id"])       # 统计按条目分开，多台不混
        stats["calls"] += 1
        started = time.time()
        tasks = []
        for call in calls:
            if "__error__" in call:
                tasks.append(_failed(call["__error__"]))
            else:
                tasks.append(execute_call(ctx, call))
        results = await asyncio.gather(*tasks)

        if single:
            body, error = results[0]
            if error:
                stats["errors"] += 1
                return web.json_response({"error": error,
                                          "hint": _ERROR_HINTS.get(error, "")},
                                         status=_ERROR_STATUS.get(error, 502))
            stats["results"] += 1
            stats["last_call"] = {"time": _now(), "call": calls[0],
                                  "took_ms": _took(started)}
            # 原样返回爱快响应体
            return web.json_response(body)

        out = []
        for call, (body, error) in zip(calls, results):
            if error:
                stats["errors"] += 1
                out.append({"ok": False, "error": error,
                            "hint": _ERROR_HINTS.get(error, "")})
            else:
                stats["results"] += 1
                out.append({"ok": True, "call": call, "body": body})
        stats["last_call"] = {"time": _now(), "calls": len(calls),
                              "took_ms": _took(started)}
        return web.json_response({
            "results": out,
            "meta": {"took_ms": _took(started), "count": len(out),
                     "calls_max": API_CALLS_MAX, "truncated": truncated},
        })


async def _failed(message):
    return None, message


_GET_HINT = ("GET 两种写法：平铺 ?func_name=homepage&action=show&TYPE=sysstat，"
             '或直接塞整段 JSON ?{"func_name":"homepage","action":"show","param":{...}}')

_ERROR_HINTS = {
    "no_session": "无法登录路由器（账号密码或网络），检查集成状态",
    "no_fetcher": "集成尚未就绪，稍后重试",
    "no_response": "路由器没有响应（超时/断连），稍后重试",
    "missing_func_name": "载荷里必须有 func_name",
    "call_must_be_object": "calls 里每项必须是对象，例如 "
                           '{"func_name":"homepage","action":"show","param":{...}}',
    "invalid_payload_json": ("塞进来的 JSON 不合法；JSON 里的值含 & # + 空格或中文时"
                             "必须先 URL 编码，复杂载荷建议改用 POST"),
    "payload_must_be_object": "载荷必须是一个 JSON 对象",
    "use_post_for_batch": "批量 calls 请用 POST（GET 只能跑单次载荷）",
}
_ERROR_STATUS = {"missing_func_name": 400, "call_must_be_object": 400,
                 "no_session": 502, "no_fetcher": 503, "no_response": 504}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _took(started):
    return int((time.time() - started) * 1000)


def _unauthorized(hass=None):
    hint = ("带上 ?key=<密钥>（见 sensor.ikuai_api_key），"
            "或请求头 X-IKuai-Key；前端也可以用 HA 登录态 "
            "(hass.fetchWithAuth) 调用")
    if hass is not None and len(entry_ids(hass)) > 1:
        hint += ("；装了多台爱快时可显式加 &entry=<条目 id>"
                 "（见各台 switch.ikuai_api 属性 entry）")
    return web.json_response({"error": "unauthorized", "hint": hint}, status=401)


def _entry_error(hass, request, problem):
    """没能定位到是哪台爱快时的明确提示（多条目是主要来源）。

    早期多条目时这里会直接回笼统的 unauthorized，很容易被误判成密钥错误。
    """
    ids = entry_ids(hass)
    if problem == "entry_not_loaded":
        return web.json_response(
            {"error": "entry_not_loaded",
             "hint": "集成条目还没加载完成（正在初始化或已卸载），稍后重试"},
            status=503)
    if problem == "entry_unknown":
        hint = ("entry 不是本集成的条目 id —— 见对应那台 switch.ikuai_api 属性 "
                "entry（sensor.ikuai_api_key 属性 entry 同值）")
    else:
        hint = ("本集成装了 %d 台爱快，请求没指明是哪一台：在 URL 里加 "
                "&entry=<条目 id>（见各台 switch.ikuai_api 属性 entry），或直接用"
                "该台 sensor.ikuai_api_key 里的 key（key 自带归属，可自动定位）"
                % len(ids))
    body = {"error": problem, "hint": hint}
    if request is not None and request.get(KEY_HASS_USER) is not None:
        # 只有用 HA 登录态（浏览器/前端调试）才回列条目 id，未鉴权的调用者不给
        body["entries"] = ids
    return web.json_response(body, status=400)


def _disabled():
    return web.json_response({
        "error": "api_disabled",
        "hint": "请在 HA 里打开 switch.ikuai_api（查询 API 开关）",
    }, status=403)


async def async_setup_api(hass) -> None:
    """注册透传视图（全局一次）。开关关闭时直接 403，不产生任何上游请求。"""
    store = api_store(hass)
    if store.get("views_registered"):
        return
    hass.http.register_view(IKuaiCallView)
    store["views_registered"] = True
    _LOGGER.info("iKuai 透传 API 已注册：GET/POST %s（需打开 switch.ikuai_api）",
                 API_CALL_PATH)
