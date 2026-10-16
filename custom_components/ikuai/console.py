"""iKuai 虚拟机远程控制台：直接复用爱快固件自带的 noVNC 控制台。

为什么放弃早期的"HA 自建 RFB 字节桥 + 自带 noVNC"方案
----------------------------------------------------
爱快固件本身就内置了一套完整、与官方后台同源迭代的 noVNC 控制台：

* 页面：``/static/vnc/index.html?<vnc_port>``（标准 noVNC ES module 版，
  自带工具栏/虚拟键盘/剪贴板/电源按钮/触屏手势，随固件升级持续维护）；
* 通道：页面内的 noVNC 直接连主机同源的 ``ws://<路由器>/Action/vnc?port=<端口>``，
  爱快 nginx 校验后台登录 Cookie 后把 WebSocket 桥到 QEMU 的 VNC 端口。

早期集成自己直连 QEMU VNC 端口、自配 noVNC 精简构建并搬 RFB 字节，流畅度与
兼容性都不如官方实现。现在改为：

    浏览器（HA 设备页悬浮 iframe）
        → /ikuai/console/<页面令牌>?authSig=…        （本模块，校验后 302）
        → /ikuai/web/static/vnc/index.html?_vmport=…&autoconnect=1
        → web_proxy 反向代理（登录态托管 + 页面注入引导脚本）
        → 路由器 /static/vnc/index.html
    页面内 WebSocket：
        ws://<HA同源>/ikuai/web/Action/vnc?port=<端口>
        → web_proxy._bridge_websocket（带路由器登录 Cookie）
        → 路由器 /Action/vnc → QEMU VNC

注入的引导脚本（见 :func:`native_vnc_bootstrap`）只做两件事：
1. 劫持 ``window.WebSocket``，把固件页面默认连向"页面所在主机 /Action/vnc"的
   地址改成 HA 同源代理路径 ``/ikuai/web/Action/vnc?port=<端口>``；
2. noVNC 弹出 VNC 密码框时自动填入该虚拟机的 ``vnc_pwd`` 并提交。

于是画面编解码、输入、手势全部走固件原版 noVNC，HA 只做登录态托管与同源代理。

鉴权与「远程控制」开关
----------------------
* 设备页「访问」= HA 官方 ``async_sign_path`` 签发 24 小时有效的 ``authSig``
  控制台地址（``/ikuai/console/<令牌>``）；
* 全局「远程控制」开关（``switch.<主机>_远程控制``）关闭时：不再签发控制台
  地址、清空虚拟机设备的「访问」链接、控制台视图拒绝跳转（403）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import timedelta
from typing import Any, Callable

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers import device_registry as dr

from .const import (
    CONSOLE_ASSET_VERSION,
    CONSOLE_FRONTEND_DIR,
    CONSOLE_PAGE_PREFIX,
    # 仅用于清理旧版本注册过的侧边栏面板（面板入口已移除）
    CONSOLE_PANEL_URL_PATH,
    CONSOLE_SIGN_EXPIRY_HOURS,
    DOMAIN,
    PROXY_PAGE_PREFIX,
    REMOTE_CONTROL_DEFAULT,
    VNC_NATIVE_PAGE_TAIL,
)
from .entity import device_by_identifier, vm_identifier
from .web_proxy import _hmac_secret, _verify_signature, sign_browser_path

_LOGGER = logging.getLogger(__name__)

CONSOLE_MANAGER_KEY = "console_manager"
_CONSOLE_DATA_KEY = "console_manager_global"  # hass.data[DOMAIN] 下的全局状态
_SIGN_EXPIRY = timedelta(hours=CONSOLE_SIGN_EXPIRY_HOURS)
_CONFIG_PLACEHOLDER = "__IKUAI_CONSOLE_CONFIG__"
_TOKEN_RE = r"[A-Za-z0-9._-]+"


# --------------------------------------------------------------------------- #
# 令牌
# --------------------------------------------------------------------------- #
def _global_store(hass) -> dict:
    """本模块在 hass.data 里的全局状态（视图注册标记等）。"""
    return hass.data.setdefault(DOMAIN, {}).setdefault(_CONSOLE_DATA_KEY, {})


def vm_token(host: str, vm_id: Any) -> str:
    """虚拟机页面令牌：由 主机+VM ID 稳定推导（重启后不变，无需持久化）。"""
    raw = f"{host}|{vm_id}".encode()
    return hashlib.sha256(raw).hexdigest()[:16]


# 侧边栏面板的令牌签发/校验（issue_panel_token / verify_panel_token）已随面板
# 功能一并移除：控制台不再有侧边栏入口，页面与数据视图也都不再注册。


# --------------------------------------------------------------------------- #
# 虚拟机数据小工具
# --------------------------------------------------------------------------- #
def _vnc_port(vm: dict) -> int | None:
    """虚拟机 VNC 端口；未开启 VNC 时（0/空）返回 None。"""
    try:
        port = int(str((vm or {}).get("vnc_port") or "0"))
    except (TypeError, ValueError):
        return None
    return port if port > 0 else None


def _vm_running(vm: dict) -> bool:
    return (vm or {}).get("status") == 1


def _frontend_file(*parts: str) -> str:
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), CONSOLE_FRONTEND_DIR, *parts
    )


def native_vnc_page_path(entry_id: str, port: int) -> str:
    """经 web 代理访问爱快原生 VNC 页的路径（带自动连接与端口标记，待签名）。"""
    return (f"{PROXY_PAGE_PREFIX}/{VNC_NATIVE_PAGE_TAIL}"
            f"?entry={entry_id}&_vmport={port}&autoconnect=1")


def native_vnc_bootstrap(port: int, password: str = "") -> str:
    """注入到爱快原生 VNC 页 ``<head>`` 里的引导脚本（IIFE，模块脚本执行前生效）。

    * WebSocket 改址：固件页默认连 ``ws://页面主机/Action/vnc?port=…``，改为
      HA 同源 ``/ikuai/web/Action/vnc?port=<port>``（web_proxy 带登录态桥接）；
    * 自动密码：noVNC 弹密码框时自动填入并提交（VNC 密码与后台登录无关，
      是 QEMU 自己的 VNC 口令，页面本身经 authSig 保护）。
    """
    cfg = json.dumps({"port": int(port), "password": str(password or "")},
                     ensure_ascii=False)
    return (
        "<script>(function(){"
        "var CFG=" + cfg.replace("</", "<\\/") + ";"
        # ---- WebSocket 改址（必须在 app/ui.js 模块执行前定义；内联脚本先于 module）
        "var NativeWS=window.WebSocket;"
        "function ProxyWS(url,protocols){"
        "var u=String(url);"
        "if(u.indexOf('/Action/vnc')!==-1){"
        "u=location.origin.replace(/^http/,'ws')+'/ikuai/web/Action/vnc?port='+CFG.port;"
        "}"
        "return arguments.length>1?new NativeWS(u,protocols):new NativeWS(u);"
        "}"
        "try{ProxyWS.prototype=NativeWS.prototype;"
        "['CONNECTING','OPEN','CLOSING','CLOSED'].forEach(function(k){"
        "ProxyWS[k]=NativeWS[k];ProxyWS[k+'_READY']=NativeWS[k+'_READY'];});}catch(e){}"
        "window.WebSocket=ProxyWS;"
        # ---- 自动填 VNC 密码
        "if(CFG.password){"
        "function fill(){"
        "var dlg=document.getElementById('noVNC_password_dlg');"
        "var inp=document.getElementById('noVNC_password_input');"
        "if(dlg&&dlg.classList.contains('noVNC_open')&&inp&&!inp.value){"
        "inp.value=CFG.password;"
        "var btn=document.getElementById('noVNC_password_button');"
        "if(btn){btn.click();}"
        "}}"
        "new MutationObserver(fill).observe(document.documentElement,"
        "{subtree:true,attributes:true,attributeFilter:['class']});"
        "setTimeout(fill,300);"
        "}"
        "})();</script>"
    )


# 侧边栏控制台面板的承载页渲染（_render_panel）已随面板功能移除：
# 控制台只在实体/卡片的悬浮窗里打开，不再有 HA 侧边栏入口。


# --------------------------------------------------------------------------- #
# 管理器：开关状态 + 签名链接 + 设备「访问」链接
# --------------------------------------------------------------------------- #
class IKuaiConsoleManager:
    """每个配置条目一个实例。"""

    def __init__(self, hass, entry, coordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.enabled: bool = REMOTE_CONTROL_DEFAULT
        self._urls: dict[str, str] = {}
        self._listeners: list[Callable[[], None]] = []
        self._unsub_coordinator: Callable[[], None] | None = None

    # ---------------------------------------------------------------- 虚拟机
    @property
    def host(self) -> str:
        return self.coordinator.host

    @property
    def vms(self) -> list[dict]:
        """当前虚拟机列表（数据在 ext.qemu 里，随协调器刷新）。"""
        data = self.coordinator.data
        if not isinstance(data, dict):
            return []
        rows = (data.get("ext") or {}).get("qemu")
        return rows if isinstance(rows, list) else []

    def vm_by_token(self, token: str) -> dict | None:
        for vm in self.vms:
            if vm_token(self.host, vm.get("id")) == token:
                return vm
        return None

    def vm_by_port(self, port: int) -> dict | None:
        """按 VNC 端口找虚拟机（web_proxy 注入页面密码时用）。"""
        for vm in self.vms:
            if _vnc_port(vm) == int(port):
                return vm
        return None

    # ------------------------------------------------------------ 签名与控制
    def _desired_tokens(self) -> dict[str, dict]:
        """当前"该有控制台"的虚拟机（开了 VNC 的）。"""
        return {vm_token(self.host, vm.get("id")): vm
                for vm in self.vms if _vnc_port(vm) is not None}

    def console_url(self, vm: dict) -> str | None:
        """该虚拟机当前可用的控制台地址（设备页「访问」用）。

        「远程控制」开关关闭、或该虚拟机没有 VNC 时返回 None；虚拟机关机时仍给
        链接 —— 点开后是爱快原生 noVNC 的重连界面，开机后会自动连上。
        """
        if not self.enabled or _vnc_port(vm) is None:
            return None
        return self._urls.get(vm_token(self.host, (vm or {}).get("id")))

    async def async_sign_urls(self) -> None:
        """（重新）签发所有虚拟机的控制台地址（稳定的 /ikuai/console/<令牌>）。"""
        urls: dict[str, str] = {}
        for token in self._desired_tokens():
            path = f"{CONSOLE_PAGE_PREFIX}/{token}"
            signed = sign_browser_path(self.hass, self.entry.entry_id, path, _SIGN_EXPIRY)
            if signed:
                urls[token] = signed
        self._urls = urls

    def async_watch_coordinator(self) -> None:
        """扩展数据（虚拟机列表）分频抓取，可能比实体晚到或之后新增虚拟机，
        每次刷新后检查"该有控制台"的集合是否变化，变了就补签并刷新设备链接。"""
        if self._unsub_coordinator is not None:
            return

        def _on_update() -> None:
            if set(self._desired_tokens()) != set(self._urls):
                self.hass.async_create_task(self._async_sync_links())

        self._unsub_coordinator = self.coordinator.async_add_listener(_on_update)
        _on_update()

    async def _async_sync_links(self) -> None:
        await self.async_sign_urls()
        await self.async_refresh_device_links()
        self._notify()

    async def async_refresh_device_links(self) -> None:
        """把虚拟机设备的「访问」刷成当前签名地址；关闭开关时清空。"""
        dev_reg = dr.async_get(self.hass)
        if hasattr(dev_reg, "__await__"):  # pragma: no cover - 老版本兼容
            dev_reg = await dev_reg
        for vm in self.vms:
            device = device_by_identifier(
                dev_reg, vm_identifier(self.coordinator, vm.get("id")),
                getattr(self.entry, "entry_id", None))
            if device is None:
                continue
            current = device.configuration_url or None
            want = self.console_url(vm)
            if want and current != want:
                dev_reg.async_update_device(device.id, configuration_url=want)
            elif not want and current and CONSOLE_PAGE_PREFIX in current:
                dev_reg.async_update_device(device.id, configuration_url=None)

    # ---------------------------------------------------------------- 开关
    @property
    def session_count(self) -> int:
        """兼容旧属性：新方案不代管 WebSocket，活动连接数恒为 0。"""
        return 0

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """登记"开关变化时要立即刷新状态"的回调（实体用）。"""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.debug("iKuai %s：刷新远程控制状态失败 %s", self.host, err)

    async def async_apply_restored(self, enabled: bool) -> None:
        """启动时按开关实体的历史状态初始化。"""
        self.enabled = enabled
        if enabled:
            await self.async_sign_urls()
        await self.async_refresh_device_links()
        self._notify()

    async def async_set_enabled(self, enabled: bool) -> None:
        """开关切换：关闭时清空所有控制台地址并拒绝新跳转。"""
        self.enabled = enabled
        if enabled:
            await self.async_sign_urls()
        await self.async_refresh_device_links()
        self._notify()
        _LOGGER.info(
            "iKuai %s：虚拟机远程控制已%s",
            self.host, "打开" if enabled else "关闭（控制台地址已清空、拒绝新连接）",
        )

    async def async_shutdown(self) -> None:
        """条目卸载：停止通知与协调器监听。"""
        self._listeners.clear()
        if self._unsub_coordinator is not None:
            try:
                self._unsub_coordinator()
            except Exception:  # pylint: disable=broad-except
                pass
            self._unsub_coordinator = None


# --------------------------------------------------------------------------- #
# 视图：控制台入口（302 到原生代理页）/ 面板 / 面板数据 / 面板静态资源
# --------------------------------------------------------------------------- #
def _resolve(hass, page_token: str) -> "tuple[IKuaiConsoleManager | None, dict | None]":
    """按页面令牌找到（管理器, 虚拟机数据）。"""
    for entry in hass.config_entries.async_entries(DOMAIN):
        manager = get_manager(hass, entry.entry_id)
        if manager is None:
            continue
        vm = manager.vm_by_token(page_token)
        if vm is not None:
            return manager, vm
    return None, None


class IKuaiConsolePageView(HomeAssistantView):
    """虚拟机控制台入口（签名链接）：校验后 302 到 web 代理下的爱快原生 VNC 页。"""

    requires_auth = False
    url = CONSOLE_PAGE_PREFIX + r"/{page_token:" + _TOKEN_RE + r"}"
    name = "ikuai:console_page"

    async def get(self, request, page_token):
        hass = request.app["hass"]
        if not await _verify_signature(hass, request):
            raise web.HTTPUnauthorized(
                text="iKuai 控制台：链接无效或已过期，请在 HA 设备页重新点击「访问」。"
            )
        manager, vm = _resolve(hass, page_token)
        if manager is None or vm is None:
            raise web.HTTPNotFound(text="iKuai 控制台：该虚拟机已不存在。")
        if not manager.enabled:
            raise web.HTTPForbidden(
                text="iKuai 控制台：远程控制已关闭，请在 HA 中打开「远程控制」开关。")
        port = _vnc_port(vm)
        if port is None:
            raise web.HTTPConflict(text="iKuai 控制台：该虚拟机未开启 VNC。")
        target = sign_browser_path(
            hass, manager.entry.entry_id,
            native_vnc_page_path(manager.entry.entry_id, port), _SIGN_EXPIRY)
        if not target:
            raise web.HTTPBadGateway(text="iKuai 控制台：无法签发代理地址。")
        raise web.HTTPFound(target)


class IKuaiConsoleAssetView(HomeAssistantView):
    """控制台面板前端资源（只服务面板自定义元素 panel.js 等，无秘密）。"""

    requires_auth = False
    url = f"{CONSOLE_PAGE_PREFIX}/assets" + r"/{file_path:[A-Za-z0-9_./-]+}"
    name = "ikuai:console_assets"

    _CONTENT_TYPES = {
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
    }

    async def get(self, request, file_path):
        root = os.path.normpath(_frontend_file())
        local = os.path.normpath(os.path.join(root, file_path.replace("/", os.sep)))
        # 目录穿越防护
        if (
            not (local == root or local.startswith(root + os.sep))
            or not os.path.isfile(local)
        ):
            raise web.HTTPNotFound()
        content_type = self._CONTENT_TYPES.get(
            os.path.splitext(local)[1].lower(), "application/octet-stream"
        )
        return web.FileResponse(
            local,
            headers={"Content-Type": content_type,
                     "Cache-Control": f"public, max-age=3600, v={CONSOLE_ASSET_VERSION}"},
        )


# --------------------------------------------------------------------------- #
# 对外接口（实体侧 / __init__ / web_proxy 用）
# --------------------------------------------------------------------------- #
def get_manager(hass, entry_id: str) -> "IKuaiConsoleManager | None":
    """取某配置条目的控制台管理器（可能为 None，例如还没初始化）。"""
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if isinstance(entry_data, dict):
        return entry_data.get(CONSOLE_MANAGER_KEY)
    return None


def manager_for(coordinator) -> "IKuaiConsoleManager | None":
    """实体侧取本条目对应的控制台管理器（初始化期间可能还没有）。"""
    return getattr(coordinator, CONSOLE_MANAGER_KEY, None)


def console_url_for(coordinator, vm: dict) -> str | None:
    """实体侧取"当前可用的控制台地址"（开关关闭 / 该虚拟机无 VNC 时返回 None）。"""
    manager = manager_for(coordinator)
    return manager.console_url(vm) if manager is not None else None


def remote_enabled_for(coordinator) -> bool:
    """实体侧读「远程控制」总开关（管理器还没建好时按默认值）。"""
    manager = manager_for(coordinator)
    return bool(manager.enabled) if manager is not None else REMOTE_CONTROL_DEFAULT


def add_manager_listener(coordinator,
                         listener: Callable[[], None]) -> Callable[[], None] | None:
    manager = manager_for(coordinator)
    return manager.add_listener(listener) if manager is not None else None


async def async_refresh_console_links(hass, entry_id: str) -> None:
    """重签控制台链接并刷新虚拟机设备「访问」（由 web_proxy 在基地址学习到
    新值 / 12 小时重签 / 条目启动时调用）。"""
    manager = get_manager(hass, entry_id)
    if manager is None:
        return
    await manager.async_sign_urls()
    await manager.async_refresh_device_links()


def _remove_legacy_panel(hass) -> None:
    """确保 HA 侧边栏里没有早期的「iKuai 控制台」面板（幂等清理）。

    旧版本把控制台注册成侧边栏面板，集成每次重载都会「出现一下又消失」，
    侧边栏还平白多一个入口。面板入口已整体移除；这里在加载/卸载时各清一次，
    把仍残留在 HA 面板注册表里的那一条摘掉（不存在时静默忽略）。
    """
    try:
        from homeassistant.components import frontend
    except ImportError:  # pragma: no cover
        return
    try:
        frontend.async_remove_panel(hass, CONSOLE_PANEL_URL_PATH)
    except Exception:  # pylint: disable=broad-except - 面板不存在时忽略
        _LOGGER.debug("iKuai 侧边栏面板不存在，无需移除", exc_info=True)


async def async_setup_console(hass, entry, coordinator) -> IKuaiConsoleManager:
    """注册视图（全局一次）+ 建立本条目管理器。控制台不再注册侧边栏面板。"""
    store = _global_store(hass)

    if not store.get("views_registered"):
        hass.http.register_view(IKuaiConsolePageView)
        hass.http.register_view(IKuaiConsoleAssetView)
        store["views_registered"] = True

    manager = IKuaiConsoleManager(hass, entry, coordinator)
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    entry_data[CONSOLE_MANAGER_KEY] = manager
    # 实体侧（sensor / switch）通过 coordinator 取管理器
    setattr(coordinator, CONSOLE_MANAGER_KEY, manager)

    # 控制台只在实体/卡片的悬浮窗里打开，不在 HA 侧边栏留入口；
    # 顺手清掉旧版本注册过的面板条目（幂等，不存在时忽略）
    _remove_legacy_panel(hass)

    await manager.async_sign_urls()
    await manager.async_refresh_device_links()
    # 扩展数据（虚拟机列表）是分频抓取的，可能比实体晚到；之后也可能新开虚拟机
    manager.async_watch_coordinator()
    _LOGGER.debug(
        "iKuai %s：远程控制台就绪（固件原生 noVNC，%d 台虚拟机，远程控制%s）",
        manager.host, len(manager.vms), "已开启" if manager.enabled else "已关闭",
    )
    return manager


async def async_unload_console(hass, entry) -> None:
    """条目卸载：移除管理器（视图全局保留）。"""
    manager = get_manager(hass, entry.entry_id)
    if manager is not None:
        coordinator = manager.coordinator
        await manager.async_shutdown()
        # 实体侧通过 coordinator 上的属性取管理器，卸载时一并摘掉
        if hasattr(coordinator, CONSOLE_MANAGER_KEY):
            try:
                delattr(coordinator, CONSOLE_MANAGER_KEY)
            except AttributeError:  # pragma: no cover
                pass
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if isinstance(entry_data, dict):
        entry_data.pop(CONSOLE_MANAGER_KEY, None)

    # 侧边栏不保留控制台入口：卸载时再清一次历史残留面板
    _remove_legacy_panel(hass)
