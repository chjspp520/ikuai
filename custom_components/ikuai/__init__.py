"""The ikuai integration."""
from __future__ import annotations
from async_timeout import timeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.core_config import Config
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .data_fetcher import DataFetcher
from .logs import IKuaiLogHub
from . import api
from . import web_proxy
from .console import async_setup_console, async_unload_console
from .entity import (
    cleanup_stale_auth_registry,
    cleanup_stale_container_devices,
    ensure_host_device,
)
from .const import (
    DOMAIN,
    CONF_USERNAME,
    CONF_PASSWD,
    CONF_PASS,
    CONF_HOST,
    CONF_UPDATE_INTERVAL,
    CONF_LOG_INTERVAL,
    COORDINATOR,
    LOGHUB,
    UNDO_UPDATE_LISTENER,
    CONF_TRACKER_CONFIG,
    CONF_SOURCE_MODE,
    MODE_CONST,
    CONF_CUSTOM_SWITCHES,
    SERVICE_API_CALL,
    SERVICE_WAKE_ON_LAN,
    SERVICE_WAN_RECONNECT,
    SERVICE_TERMINAL_DISCONNECT,
    SERVICE_MAC_BLOCK,
    SERVICE_MAC_UNBLOCK,
    SERVICE_LOG_GET,
    SERVICE_LOG_CLEAR,
    SERVICE_ALARMS_READ_ALL,
    SERVICE_WAN_LINE_ADD,
    SERVICE_WAN_LINE_EDIT,
    SERVICE_WAN_LINE_DEL,
    SERVICE_STREAM_UPDOWN_ADD,
    SERVICE_STREAM_UPDOWN_DEL,
)
from homeassistant.exceptions import ConfigEntryNotReady
import voluptuous as vol
import homeassistant.helpers.config_validation as cv
import time
import datetime
import logging
import asyncio

try:  # 2024.2+ 才支持服务响应
    from homeassistant.core import SupportsResponseOnly
except ImportError:  # pragma: no cover
    SupportsResponseOnly = None

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.TEXT,
    Platform.DEVICE_TRACKER,
    # 虚拟机「屏幕画面」image 实体（按需从 VNC 抓一帧）
    Platform.IMAGE,
    # 固件更新实体（设备页原生「有新版本」提示）
    Platform.UPDATE,
]


# Configuration schema for custom switches
CUSTOM_SWITCH_SCHEMA = vol.Schema({
    vol.Required("label"): cv.string,
    vol.Required("name"): cv.string,
    vol.Optional("icon", default="mdi:toggle-switch"): cv.string,
    vol.Required("turn_on_body"): dict,
    vol.Required("turn_off_body"): dict,
    vol.Optional("show_body"): dict,
    vol.Optional("show_on"): dict,
    vol.Optional("show_off"): dict,
})

# Configuration schema for the domain
CONFIG_SCHEMA = vol.Schema({
    DOMAIN: vol.Schema({
        vol.Optional(CONF_CUSTOM_SWITCHES, default={}): vol.Schema({
            cv.string: CUSTOM_SWITCH_SCHEMA
        })
    })
}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: Config) -> bool:
    """Set up the iKuai component."""
    hass.data.setdefault(DOMAIN, {})

    conf = config.get(DOMAIN, {})
    custom_switches_config = conf.get(CONF_CUSTOM_SWITCHES, {})
    if custom_switches_config:
        _LOGGER.info("Loaded %d custom switches from configuration.yaml", len(custom_switches_config))
    else:
        _LOGGER.info("No custom switches configured in configuration.yaml.")
    hass.data[DOMAIN]["custom_switches"] = custom_switches_config

    _async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up iKuai from a config entry."""
    host = entry.data[CONF_HOST]
    username = entry.data[CONF_USERNAME]
    passwd = entry.data[CONF_PASSWD]
    pas = entry.data[CONF_PASS]
    update_interval_seconds = entry.options.get(CONF_UPDATE_INTERVAL, 10)
    log_interval_seconds = entry.options.get(CONF_LOG_INTERVAL, 60)

    if entry.data.get(CONF_SOURCE_MODE) == MODE_CONST:
        try:
            from .const import DEVICE_TRACKERS
            tracker_config = {}
            for key, info in DEVICE_TRACKERS.items():
                target = info.get("mac_address") or info.get("ip_address")
                if target:
                    tracker_config[target] = {
                        "name": info.get("name", key),
                        "buffer": info.get("disconnect_refresh_times", 2),
                        "type": "mac" if ":" in target else "ip",
                        "mac_address": info.get("mac_address"),
                        "ip_address": info.get("ip_address"),
                    }
        except ImportError:
            _LOGGER.warning("DEVICE_TRACKERS not found in const.py, skipping tracker setup")
            tracker_config = {}
    else:
        tracker_config = entry.data.get(CONF_TRACKER_CONFIG, {})

    custom_switches_config = hass.data[DOMAIN].get("custom_switches", {})

    coordinator = IKUAIDataUpdateCoordinator(
        hass, host, username, passwd, pas,
        update_interval_seconds, tracker_config, custom_switches_config)
    coordinator.entry_id = entry.entry_id
    loghub = IKuaiLogHub(hass, coordinator._fetcher, log_interval_seconds,
                         entry.entry_id)
    coordinator.loghub = loghub

    await coordinator.async_refresh()
    if not coordinator.last_update_success:
        raise ConfigEntryNotReady

    undo_listener = entry.add_update_listener(update_listener)

    hass.data[DOMAIN][entry.entry_id] = {
        COORDINATOR: coordinator,
        LOGHUB: loghub,
        UNDO_UPDATE_LISTENER: undo_listener,
    }

    # 先把爱快主机设备建出来：虚拟机 / Docker / 认证账号设备都用它的注册表 id
    # （via_device_id）挂到它下面，而子设备要先有父设备的 id 才能建
    ensure_host_device(hass, entry, coordinator)

    # 清理早期「一个认证账号一台设备」遗留的注册项/设备（必须在平台加载之前，
    # 否则新旧实体抢同一个固定实体 ID，新版会被挤成 xxx_2）
    cleanup_stale_auth_registry(hass, entry, coordinator)

    # 虚拟机远程控制台：注册视图（控制台入口/侧边栏面板/静态资源）、建立管理器
    # 并签发控制台链接。必须在转发平台之前完成 —— 实体初始化时就要用管理器算 console_url
    console_manager = await async_setup_console(hass, entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 实体已全部重新归属设备：删掉早期「一个容器一台设备」遗留的空 Docker 设备
    # （所有容器现在共用 <host>::docker 那一台）
    cleanup_stale_container_devices(hass, entry, coordinator)

    # 再来一遍认证账号清理：这次新版实体也已经建好，能把还挂在老设备上的搬回公用设备
    cleanup_stale_auth_registry(hass, entry, coordinator)

    # 原版 Web 反向代理 + 设备"访问"悬浮窗（设备链接签名注册）
    await web_proxy.async_setup_proxy(hass, entry)

    # 按需查询 API（可选功能，默认关闭）：只注册视图，开关关闭时直接 403、
    # 不产生任何上游请求；开关实体在 switch 平台（switch.ikuai_api）
    await api.async_setup_api(hass)

    # 平台已建好实体（虚拟机设备已存在），把设备页「访问」刷成最新签名链接
    await console_manager.async_refresh_device_links()
    return True


async def async_unload_entry(hass, entry):
    """Unload a config entry."""
    unload_ok = all(
        await asyncio.gather(
            *[hass.config_entries.async_forward_entry_unload(entry, component)
              for component in PLATFORMS]
        )
    )

    hass.data[DOMAIN][entry.entry_id][UNDO_UPDATE_LISTENER]()

    # 先切断所有控制台会话（远程控制不能因重载/卸载残留）
    await async_unload_console(hass, entry)

    if unload_ok:
        await web_proxy.async_unload_proxy(hass, entry)
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def update_listener(hass, entry):
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


class IKUAIDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching iKuai data."""

    def __init__(self, hass, host, username, passwd, pas, update_interval_seconds,
                 tracker_config, custom_switches_config):
        update_interval = datetime.timedelta(seconds=update_interval_seconds)
        _LOGGER.debug("%s Data will be update every %s", host, update_interval)
        self._token = ""
        self._token_expire_time = 0
        self._allow_login = True
        self.loghub = None

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=update_interval)

        self._fetcher = DataFetcher(
            hass, host, username, passwd, pas, tracker_config, custom_switches_config)
        self.host = host

    async def get_access_token(self):
        """Get or refresh the access token."""
        if time.time() < self._token_expire_time:
            return self._token
        if self._allow_login:
            self._token = await self._fetcher._login_ikuai()
            if not isinstance(self._token, str):
                if self._token == 10001:
                    self._allow_login = False
                return None
            self._token_expire_time = time.time() + 60 * 60 * 2
            return self._token
        _LOGGER.error("The username or password has been incorrect, please reconfigure the ikuai integration.")
        return None

    async def _async_update_data(self):
        """Update data via DataFetcher."""
        if not self._allow_login:
            raise UpdateFailed("authentication failed")
        sess_key = await self.get_access_token()
        if not sess_key:
            raise UpdateFailed("Failed to get access token")

        try:
            async with timeout(150):
                data = await self._fetcher.get_data(sess_key)
                if data == 401:
                    self._token_expire_time = 0
                    return None
                if self.loghub is not None:
                    await self.loghub.async_maybe_update(sess_key)
                    data["logs"] = self.loghub.snapshot()
                if not data:
                    raise UpdateFailed("failed in getting data")
                return data
        except UpdateFailed:
            raise
        except Exception as error:
            raise UpdateFailed(error) from error

    async def _check_result(self, result):
        """统一处理写操作响应的会话失效。"""
        if isinstance(result, dict) and result.get("Result") == 10014:
            self._token_expire_time = 0
            return False
        return True

    async def async_control_device(self, action_body):
        """Execute action for iKuai devices."""
        if not self._allow_login:
            return None
        sess_key = await self.get_access_token()
        if not sess_key:
            return None
        result = await self._fetcher.async_execute_action(sess_key, action_body)
        await self._check_result(result)
        return result

    async def async_set_service_switch(self, key, turn_on):
        """服务类开关切换。"""
        if not self._allow_login:
            return None
        sess_key = await self.get_access_token()
        if not sess_key:
            return None
        result = await self._fetcher.async_set_service_switch(sess_key, key, turn_on)
        await self._check_result(result)
        if isinstance(result, dict) and result.get("Result") not in (30000, None) \
                and result.get("code") not in (0, None):
            _LOGGER.error("iKuai 开关操作失败: %s", result.get("ErrMsg") or result)
        await self.async_request_refresh()
        return result

    async def async_api_call(self, func_name, action, param=None):
        """通用 API 调用（服务逃生舱），返回解包后的 data block。"""
        if not self._allow_login:
            return None
        sess_key = await self.get_access_token()
        if not sess_key:
            return None
        result = await self._fetcher.async_execute_action(
            sess_key, {"func_name": func_name, "action": action, "param": param or {}})
        await self._check_result(result)
        return self._fetcher._get_data_block(result)


# ----------------------------------------------------------------------
# 服务注册
# ----------------------------------------------------------------------

def _get_coordinators(hass, call: ServiceCall):
    """选出本次服务调用要作用到的 coordinator 列表。

    给了 `entry_id` 就只用那一条；**没给则返回所有已加载的条目** ——
    多台爱快时服务会对每一台都执行一遍（不是"只用第一台"）。
    """
    entry_id = (call.data or {}).get("entry_id")
    entries = hass.config_entries.async_entries(DOMAIN)
    out = []
    for entry in entries:
        if entry_id and entry.entry_id != entry_id:
            continue
        bundle = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if bundle and bundle.get(COORDINATOR):
            out.append((entry, bundle[COORDINATOR], bundle.get(LOGHUB)))
    return out


def _api_schema(extra):
    return vol.Schema({
        vol.Optional("entry_id"): cv.string,
        **extra,
    })


def _async_register_services(hass: HomeAssistant):
    """注册 iKuai 域服务（只注册一次）。"""
    if hass.data.get(DOMAIN, {}).get("services_registered"):
        return
    hass.data[DOMAIN]["services_registered"] = True

    async def api_call(call: ServiceCall):
        results = []
        for entry, coord, _hub in _get_coordinators(hass, call):
            block = await coord.async_api_call(
                call.data["func_name"], call.data["action"],
                call.data.get("param") or {})
            results.append({"entry_id": entry.entry_id, "host": coord.host, "result": block})
        return {"results": results}

    async def wake_on_lan(call: ServiceCall):
        mac = str(call.data["mac"]).replace("-", ":").lower()
        for _e, coord, _h in _get_coordinators(hass, call):
            await coord.async_control_device(
                {"func_name": "wakeup", "action": "wake_mac", "param": {"mac": mac}})

    async def wan_reconnect(call: ServiceCall):
        line_id = int(call.data.get("id", 1))
        action = "link_pppoe_reconnect" if call.data.get("mode", "pppoe") == "pppoe" \
            else "link_dhcp_reconnect"
        for _e, coord, _h in _get_coordinators(hass, call):
            await coord.async_control_device(
                {"func_name": "wan", "action": action, "param": {"id": line_id}})

    async def terminal_disconnect(call: ServiceCall):
        for _e, coord, _h in _get_coordinators(hass, call):
            await coord.async_control_device(
                {"func_name": "monitor_lanip", "action": "del_conn",
                 "param": {"ip": call.data["ip"]}})

    async def _mac_change(call, block_it):
        mac = str(call.data["mac"]).replace("-", ":").lower()
        for _e, coord, _h in _get_coordinators(hass, call):
            for body in block_it(mac, str(call.data.get("comment") or "HA")):
                await coord.async_control_device(body)
            await coord.async_request_refresh()

    async def mac_block(call: ServiceCall):
        def bodies(mac, comment):
            return [
                {"func_name": "mac_comment", "action": "add",
                 "param": {"mac": mac, "comment": comment}},
                {"func_name": "acl_mac", "action": "add",
                 "param": {"mac": mac, "enabled": "yes",
                           "time": "00:00-23:59", "week": "1234567"}},
            ]
        await _mac_change(call, bodies)

    async def mac_unblock(call: ServiceCall):
        def bodies(mac, _comment):
            return [{"func_name": "acl_mac", "action": "del", "param": {"mac": mac}}]
        await _mac_change(call, bodies)

    async def log_get(call: ServiceCall):
        from .const import LOG_CATEGORIES
        funcs = {c["key"]: c["func"] for c in LOG_CATEGORIES}
        category = call.data["category"]
        limit = int(call.data.get("limit", 50))
        results = []
        for entry, coord, hub in _get_coordinators(hass, call):
            block = await coord.async_api_call(
                funcs.get(category, category), "show",
                {"TYPE": "total,data", "ORDER_BY": "id", "ORDER": "desc",
                 "limit": f"0,{limit}"})
            results.append({"entry_id": entry.entry_id, "host": coord.host,
                            "category": category, "result": block})
            if hub is not None:
                await hub.async_force_update(await coord.get_access_token())
        return {"results": results}

    async def log_clear(call: ServiceCall):
        from .const import LOG_CATEGORIES
        funcs = {c["key"]: c["func"] for c in LOG_CATEGORIES}
        category = call.data["category"]
        targets = funcs if category == "all" else {category: funcs.get(category, category)}
        for _e, coord, hub in _get_coordinators(hass, call):
            for func in targets.values():
                await coord.async_control_device(
                    {"func_name": func, "action": "clean"})
            if hub is not None:
                await hub.async_force_update(await coord.get_access_token())

    async def alarms_read_all(call: ServiceCall):
        for _e, coord, _h in _get_coordinators(hass, call):
            await coord.async_control_device(
                {"func_name": "rt_collect", "action": "reading_all"})
            await coord.async_request_refresh()

    # ---- 多拨线路（VLAN 拨号）增删改 ----
    # 与原版 Web 表单一致：vlan_internet 0=静态IP / 1=DHCP / 2=PPPoE
    def _yn(value) -> str:
        return "yes" if value else "no"

    def _enc(value: str) -> str:
        # 复刻 Web 端 copyObj("encode")：仅对 * ( ) ^ < > [ ] # 百分号编码
        table = {"*": "%2A", "(": "%28", ")": "%29", "^": "%5E",
                 "<": "%3C", ">": "%3E", "[": "%5B", "]": "%5D", "#": "%23"}
        return "".join(table.get(ch, ch) for ch in str(value))

    def _build_wan_line_param(call: ServiceCall, edit: bool) -> dict:
        d = call.data
        internet = int(d["internet"])
        param = {
            "vlan_id": str(d["vlan_id"]),
            "vlan_name": d["vlan_name"],
            "vlan_internet": internet,
            "comment": d.get("comment", ""),
            "interface": d["interface"],
            "enabled": _yn(d.get("enabled", True)),
            "mac": d.get("mac", ""),
            "upload": 0,
            "download": 0,
            "link_time": d.get("link_time", "00:00-00:00"),
            "default_route": 1 if d.get("default_route") else 0,
            "disc_auto_switch": 1 if d.get("disc_auto_switch") else 0,
            "check_link_mode": int(d.get("check_link_mode", 1)),
            "check_link_host": d.get("check_link_host", ""),
        }
        if internet == 2:
            param.update({
                "gateway": d.get("gateway", ""),
                "username": d.get("username", ""),
                "passwd": _enc(d.get("passwd", "")),
                "pppoe_service": d.get("pppoe_service", ""),
                "pppoe_ac": d.get("pppoe_ac", ""),
                "cycle_rst_time": int(d.get("cycle_rst_time", 0)),
                "timing_rst_switch": 1 if d.get("timing_rst_switch") else 0,
                "timing_rst_week": d.get("timing_rst_week", "1234567"),
                "timing_rst_time": d.get("timing_rst_time", "00:00"),
                "mtu": int(d.get("mtu", 1480)),
                "mru": int(d.get("mru", 1480)),
                "pppoe_check_errip_switch":
                    1 if d.get("pppoe_check_errip_switch") else 0,
                "pppoe_check_errip_list": d.get("pppoe_check_errip_list", ""),
            })
        else:
            param["ip_mask"] = d.get("ip_mask", "")
            param["gateway"] = d.get("gateway", "")
            if internet == 1:
                param.update({
                    "vendorclass": d.get("vendorclass", ""),
                    "clientid": d.get("clientid", ""),
                    "hostname": d.get("hostname", ""),
                    "opt_type12": int(d.get("opt_type12", 0)),
                    "opt_type60": int(d.get("opt_type60", 0)),
                    "opt_type61": int(d.get("opt_type61", 0)),
                })
        if edit:
            param["id"] = int(d["id"])
        return param

    async def wan_line_add(call: ServiceCall):
        param = _build_wan_line_param(call, edit=False)
        results = []
        for entry, coord, _h in _get_coordinators(hass, call):
            result = await coord.async_control_device(
                {"func_name": "wan", "action": "vlan_add", "param": param})
            await coord.async_request_refresh()
            results.append({"entry_id": entry.entry_id, "host": coord.host,
                            "result": coord._fetcher._get_data_block(result)
                            if isinstance(result, dict) else result})
        return {"results": results}

    async def wan_line_edit(call: ServiceCall):
        param = _build_wan_line_param(call, edit=True)
        results = []
        for entry, coord, _h in _get_coordinators(hass, call):
            result = await coord.async_control_device(
                {"func_name": "wan", "action": "vlan_edit", "param": param})
            await coord.async_request_refresh()
            results.append({"entry_id": entry.entry_id, "host": coord.host,
                            "result": coord._fetcher._get_data_block(result)
                            if isinstance(result, dict) else result})
        return {"results": results}

    async def wan_line_del(call: ServiceCall):
        for _e, coord, _h in _get_coordinators(hass, call):
            await coord.async_control_device(
                {"func_name": "wan", "action": "vlan_del",
                 "param": {"id": int(call.data["id"])}})
            await coord.async_request_refresh()

    # ---- 上下行分离（上传/下载走不同线路）----
    async def stream_updown_add(call: ServiceCall):
        d = call.data
        protocol = d.get("protocol", "any")
        port = "" if protocol in ("any", "icmp") else None
        param = {
            "upiface": d["upiface"],
            "downiface": d["downiface"],
            "protocol": protocol,
            "src_addr": _enc(d.get("src_addr", "")),
            "dst_addr": _enc(d.get("dst_addr", "")),
            "src_port": port if port is not None else str(d.get("src_port", "")),
            "dst_port": port if port is not None else str(d.get("dst_port", "")),
            "comment": _enc(d.get("comment", "")),
            "enabled": _yn(d.get("enabled", True)),
        }
        if "id" in d:
            param["id"] = int(d["id"])
        action = "edit" if "id" in d else "add"
        results = []
        for entry, coord, _h in _get_coordinators(hass, call):
            result = await coord.async_control_device(
                {"func_name": "stream_updown", "action": action, "param": param})
            await coord.async_request_refresh()
            results.append({"entry_id": entry.entry_id, "host": coord.host,
                            "result": coord._fetcher._get_data_block(result)
                            if isinstance(result, dict) else result})
        return {"results": results}

    async def stream_updown_del(call: ServiceCall):
        for _e, coord, _h in _get_coordinators(hass, call):
            await coord.async_control_device(
                {"func_name": "stream_updown", "action": "del",
                 "param": {"id": int(call.data["id"])}})
            await coord.async_request_refresh()

    services = [
        (SERVICE_API_CALL, api_call,
         _api_schema({vol.Required("func_name"): cv.string,
                      vol.Required("action"): cv.string,
                      vol.Optional("param", default={}): dict}), True),
        (SERVICE_WAKE_ON_LAN, wake_on_lan,
         _api_schema({vol.Required("mac"): cv.string}), False),
        (SERVICE_WAN_RECONNECT, wan_reconnect,
         _api_schema({vol.Optional("id", default=1): vol.Coerce(int),
                      vol.Optional("mode", default="pppoe"):
                          vol.In(["pppoe", "dhcp"])}), False),
        (SERVICE_TERMINAL_DISCONNECT, terminal_disconnect,
         _api_schema({vol.Required("ip"): cv.string}), False),
        (SERVICE_MAC_BLOCK, mac_block,
         _api_schema({vol.Required("mac"): cv.string,
                      vol.Optional("comment", default="HA"): cv.string}), False),
        (SERVICE_MAC_UNBLOCK, mac_unblock,
         _api_schema({vol.Required("mac"): cv.string}), False),
        (SERVICE_LOG_GET, log_get,
         _api_schema({vol.Required("category"): cv.string,
                      vol.Optional("limit", default=50): vol.Coerce(int)}), True),
        (SERVICE_LOG_CLEAR, log_clear,
         _api_schema({vol.Required("category"): cv.string}), False),
        (SERVICE_ALARMS_READ_ALL, alarms_read_all,
         _api_schema({}), False),
    ]

    # 多拨线路 add/edit 参数字段（对齐原版 Web 表单）
    _wan_line_fields = {
        vol.Required("interface"): cv.string,          # 物理 WAN，如 wan1
        vol.Required("vlan_id"): cv.string,            # 普通整数；QINQ 形如 100.10
        vol.Required("vlan_name"): cv.string,          # 拨号名，如 adsl1
        vol.Optional("internet", default=2): vol.In([0, 1, 2, "0", "1", "2"]),  # 0静态 1DHCP 2PPPoE
        vol.Optional("enabled", default=True): bool,
        vol.Optional("comment"): cv.string,
        vol.Optional("mac"): cv.string,
        vol.Optional("link_time"): cv.string,          # 00:00-24:00
        vol.Optional("default_route", default=False): bool,
        vol.Optional("disc_auto_switch", default=False): bool,
        vol.Optional("check_link_mode"): vol.Coerce(int),
        vol.Optional("check_link_host"): cv.string,
        # PPPoE（internet=2）
        vol.Optional("username"): cv.string,
        vol.Optional("passwd"): cv.string,
        vol.Optional("gateway"): cv.string,
        vol.Optional("pppoe_service"): cv.string,
        vol.Optional("pppoe_ac"): cv.string,
        vol.Optional("mtu"): vol.Coerce(int),
        vol.Optional("mru"): vol.Coerce(int),
        vol.Optional("cycle_rst_time"): vol.Coerce(int),
        vol.Optional("timing_rst_switch"): bool,
        vol.Optional("timing_rst_week"): cv.string,
        vol.Optional("timing_rst_time"): cv.string,
        vol.Optional("pppoe_check_errip_switch"): bool,
        vol.Optional("pppoe_check_errip_list"): cv.string,
        # 静态/DHCP（internet=0/1）
        vol.Optional("ip_mask"): cv.string,            # 192.168.1.2/24，多个逗号分隔
        vol.Optional("hostname"): cv.string,
        vol.Optional("vendorclass"): cv.string,
        vol.Optional("clientid"): cv.string,
        vol.Optional("opt_type12"): vol.Coerce(int),
        vol.Optional("opt_type60"): vol.Coerce(int),
        vol.Optional("opt_type61"): vol.Coerce(int),
    }
    services += [
        (SERVICE_WAN_LINE_ADD, wan_line_add,
         _api_schema(dict(_wan_line_fields)), True),
        (SERVICE_WAN_LINE_EDIT, wan_line_edit,
         _api_schema({**_wan_line_fields, vol.Required("id"): vol.Coerce(int)}), True),
        (SERVICE_WAN_LINE_DEL, wan_line_del,
         _api_schema({vol.Required("id"): vol.Coerce(int)}), False),
        (SERVICE_STREAM_UPDOWN_ADD, stream_updown_add,
         _api_schema({
             vol.Required("upiface"): cv.string,       # 上传出口线路，如 wan1/adsl1
             vol.Required("downiface"): cv.string,     # 下载回传线路
             vol.Optional("protocol", default="any"):
                 vol.In(["any", "tcp", "udp", "tcp_udp", "icmp"]),
             vol.Optional("src_addr"): cv.string,
             vol.Optional("dst_addr"): cv.string,
             vol.Optional("src_port"): cv.string,
             vol.Optional("dst_port"): cv.string,
             vol.Optional("comment"): cv.string,
             vol.Optional("enabled", default=True): bool,
             vol.Optional("id"): vol.Coerce(int),      # 带 id 时执行 edit
         }), True),
        (SERVICE_STREAM_UPDOWN_DEL, stream_updown_del,
         _api_schema({vol.Required("id"): vol.Coerce(int)}), False),
    ]

    for name, handler, schema, with_response in services:
        kwargs = dict(schema=schema)
        if with_response and SupportsResponseOnly is not None:
            kwargs["supports_response"] = SupportsResponseOnly.OPTIONAL
        hass.services.async_register(DOMAIN, name, handler, **kwargs)
