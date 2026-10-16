"""iKuai 开关实体：内置开关 / 自定义开关 / MAC黑名单 / 服务开关 / Docker/VM 动态开关。"""
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.storage import Store

from .api import api_stats, ensure_key, entry_ids, preset_entries, set_api_enabled
from .console import (
    add_manager_listener,
    console_url_for,
    manager_for,
    remote_enabled_for,
)
from .const import (
    API_CALL_PATH, API_CALLS_MAX, API_ENABLED_DEFAULT, API_KEY_TTL_SECONDS,
    API_SWITCH_NAME, API_SWITCH_SUFFIX,
    COORDINATOR, DOMAIN, REMOTE_CONTROL_DEFAULT, REMOTE_CONTROL_NAME,
    SWITCH_TYPES, EXT_SWITCH_TYPES,
    LOG_CATEGORIES, LOG_POLL_DEFAULT, LOG_SWITCH_NAME, LOG_SWITCH_SUFFIX,
)
from .logs import log_poll_enabled, set_log_poll_enabled
from .entity import (
    DynamicEntityManager,
    IKuaiEntity,
    category,
    container_web_url,
    docker_device_info,
    entity_id_slug,
    internal_key_slug,
    mac_acl_entry,
    vm_device_info,
    vnc_host,
)

_LOGGER = logging.getLogger(__name__)


def _console_state(coordinator, vm) -> str:
    """虚拟机控制台可用性（原「远程控制台」传感器的 state）。

    合并到电源开关后作为 ``console_state`` 属性保留，避免那条信息丢失。
    """
    if not remote_enabled_for(coordinator):
        return "已关闭"
    try:
        port = int(str((vm or {}).get("vnc_port") or "0"))
    except (TypeError, ValueError):
        port = 0
    if not port:
        return "未开启 VNC"
    if (vm or {}).get("status") != 1:
        return "未开机"
    return "可用"

# kind -> (ext 数据块键, 状态字段)；字段嵌在块内子表时由 spec["section"] 指定子表名
EXT_SOURCE = {
    "ikuai_sw_snmp": ("netsnmp", "enabled"),
    "ikuai_sw_samba": ("smbd", "enabled"),
    "ikuai_sw_ftp": ("ftp", "open_ftp"),
    "ikuai_sw_upnp": ("upnpd", "enabled"),
    "ikuai_sw_webauth": ("webauth", "enabled"),
    "ikuai_sw_igmp": ("igmp", "enabled"),
    "ikuai_sw_iptv": ("iptv", "enabled"),
    "ikuai_sw_watchdog": ("watchdog", "watchdog"),
    "ikuai_sw_nat": ("basic", "switch_nat"),
    "ikuai_sw_dpi": ("basic", "switch_dpi"),
    "ikuai_sw_ntp": ("basic", "switch_ntp"),
    "ikuai_sw_alg_ftp": ("alg", "support_ftp"),
    "ikuai_sw_alg_tftp": ("alg", "support_tftp"),
    "ikuai_sw_alg_sip": ("alg", "support_sip"),
    "ikuai_sw_alg_h323": ("alg", "support_h323"),
    "ikuai_sw_wanweb": ("remote", "open_wanweb"),
    "ikuai_sw_sshd": ("remote", "open_sshd"),
    "ikuai_sw_telnetd": ("remote", "open_telnetd"),
    "ikuai_sw_auto_upgrade": ("upgrade", "auto_upgrade_sec"),
}

_TRUE = {1, "1", "yes", "on", "true", True}


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up iKuai switch entities from a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]

    switches = [IKUAISwitch(hass, kind, coordinator, is_custom=False)
                for kind in SWITCH_TYPES]

    # 全局「远程控制」开关（挂爱快主机设备）：控制所有虚拟机控制台的访问权限
    switches.append(IKUAIRemoteControlSwitch(hass, coordinator))

    # 「查询 API」总开关（默认关闭）：打开后 /ikuai/api/query 才可用；
    # 开关自身的属性就是这套 API 的目录（每个 key 的用法）与调用统计
    switches.append(IKuaiApiSwitch(hass, coordinator))

    # 「采集日志」开关（默认开启）：关掉就停止日志定时采集（12 类 × 每分钟）
    switches.append(IKuaiLogPollSwitch(hass, coordinator))

    custom_switches_config = hass.data[DOMAIN].get("custom_switches", {})
    for switch_key, switch_config in custom_switches_config.items():
        switches.append(IKUAISwitch(hass, switch_key, coordinator,
                                    is_custom=True, custom_config=switch_config))

    for kind in EXT_SWITCH_TYPES:
        switches.append(IKuaiExtServiceSwitch(hass, kind, coordinator))

    if isinstance(coordinator.data.get("mac_control"), list):
        for mac in coordinator.data["mac_control"]:
            switches.append(IKUAISwitchmac(hass, coordinator, mac["id"]))

    async_add_entities(switches, False)

    # Docker 容器 / 虚拟机 动态启停开关
    def docker_items(data):
        rows = (data.get("ext") or {}).get("docker") or []
        return {str(c.get("id")): c for c in rows if c.get("id") is not None}

    def qemu_items(data):
        rows = (data.get("ext") or {}).get("qemu") or []
        return {str(v.get("id")): v for v in rows if v.get("id") is not None}

    DynamicEntityManager(
        coordinator, async_add_entities,
        lambda cd, sfx, item: IKuaiContainerSwitch(hass, cd, "docker", sfx, item),
        docker_items)
    DynamicEntityManager(
        coordinator, async_add_entities,
        lambda cd, sfx, item: IKuaiContainerSwitch(hass, cd, "qemu", sfx, item),
        qemu_items)


class IKUAIBaseSwitch(IKuaiEntity, SwitchEntity):
    """Base class for iKuai switches."""

    _attr_has_entity_name = True

    def __init__(self, hass, coordinator):
        super().__init__(coordinator)
        self._hass = hass


class IKUAISwitch(IKUAIBaseSwitch):
    """Define a static iKuai switch entity."""

    def __init__(self, hass, kind, coordinator, is_custom=False, custom_config=None):
        super().__init__(hass, coordinator)
        self.kind = kind
        if is_custom and custom_config:
            self._attr_icon = custom_config.get("icon", "mdi:toggle-switch")
            self._name = custom_config["name"]
            self._turn_on_body = custom_config["turn_on_body"]
            self._turn_off_body = custom_config["turn_off_body"]
            self._attr_entity_category = category("config")
        else:
            self._attr_icon = SWITCH_TYPES[self.kind]["icon"]
            self._name = SWITCH_TYPES[self.kind]["name"]
            self._turn_on_body = SWITCH_TYPES[self.kind]["turn_on_body"]
            self._turn_off_body = SWITCH_TYPES[self.kind]["turn_off_body"]
            self._attr_entity_category = category(SWITCH_TYPES[self.kind].get("entity_category", "config"))
        # 固定英文实体 ID：switch.ikuai_arp_filter / switch.ikuai_stream_control
        self._setup_forced_entity_id("switch", internal_key_slug(self.kind))

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.kind}_{self.coordinator.host}"

    @property
    def icon(self):
        return self._attr_icon

    @property
    def is_on(self):
        for item in self.coordinator.data.get("switch", []) or []:
            if item.get("name") == self._name:
                return item.get("onoff") == "on"
        return False

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_control_device(self._turn_on_body)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_control_device(self._turn_off_body)
        await self.coordinator.async_request_refresh()


class IKuaiExtServiceSwitch(IKUAIBaseSwitch):
    """系统服务开关（SNMP/Samba/FTP/UPnP/Web认证/IGMP/IPTV/看门狗/NAT/DPI/NTP）。"""

    def __init__(self, hass, kind, coordinator):
        super().__init__(hass, coordinator)
        self.kind = kind
        spec = EXT_SWITCH_TYPES[kind]
        self._field = spec["field"]
        self._section = spec.get("section")
        self._block_key, _ = EXT_SOURCE[kind]
        self._name = spec["name"]
        self._attr_icon = spec.get("icon", "mdi:toggle-switch")
        self._attr_entity_category = category("config")
        if spec.get("enabled_default") is False:
            self._attr_entity_registry_enabled_default = False
        # 固定英文实体 ID：switch.ikuai_sw_snmp / switch.ikuai_sw_ntp ...
        self._setup_forced_entity_id("switch", internal_key_slug(kind))

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.kind}_{self.coordinator.host}"

    @property
    def available(self):
        if not super().available:
            return False
        ext = (self.coordinator.data or {}).get("ext") or {}
        return ext.get(self._block_key) is not None

    def _row(self):
        ext = (self.coordinator.data or {}).get("ext") or {}
        block = ext.get(self._block_key)
        if not isinstance(block, dict):
            return {}
        if self._section:
            section = block.get(self._section)
            return section if isinstance(section, dict) else {}
        return block

    @property
    def is_on(self):
        return self._row().get(self._field) in _TRUE

    @property
    def extra_state_attributes(self):
        row = self._row()
        return {k: v for k, v in row.items() if k not in ("passwd", "secret") and v != ""} or None

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_set_service_switch(self.kind, True)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_set_service_switch(self.kind, False)


class IKUAISwitchmac(IKUAIBaseSwitch):
    """iKuai MAC 访问控制（禁止联网）条目开关：一条目一个实体。

    state = 该条目**当前是否在禁止联网** —— 取 ``acl_mac`` 返回行里的 ``enabled``
    （``yes`` → on）。状态每次都从协调器数据**现读**，所以 up/down 操作之后立刻
    反映真实状态，不会停留在启动那一刻的快照。

    显示名统一加「禁止联网_」前缀：``禁止联网_华为平板电脑``；条目没写备注时用
    MAC（``禁止联网_72:ff:d2:44:ac:91``）。**实体 ID 保持**
    ``switch.ikuai_mac_<MAC后6位>`` 不变。

    属性给出该条目的完整明细（与「拉黑/放行按钮」上的黑名单清单同一份映射）。
    """

    _attr_icon = "mdi:network-pos"

    def __init__(self, hass, coordinator, macid):
        super().__init__(hass, coordinator)
        self._macid = macid
        row = self._row()
        self._mac_address = row.get("mac")
        # unique_id 用（只在此处定一次，保持稳定）
        self._mac = str(self._mac_address or "").replace(":", "")
        # 固定英文实体 ID：switch.ikuai_mac_<MAC后6位>
        self._setup_forced_entity_id("switch", f"mac_{self._mac[-6:] or macid}")

    def _row(self) -> dict:
        """该条目在 ``acl_mac`` 最新返回里的那一行（取不到时返回 {}）。"""
        rows = self.coordinator.data.get("mac_control")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict) and row.get("id") == self._macid:
                    return row
        return {}

    @property
    def name(self):
        """禁止联网_<备注>；条目没备注就用 MAC。"""
        comment = self._row().get("comment")
        return f"禁止联网_{comment or self._mac_address or self._macid}"

    @property
    def unique_id(self):
        return f"{DOMAIN}_switch_{self.coordinator.host}_{self._mac}"

    @property
    def is_on(self):
        return self._row().get("enabled") == "yes"

    @property
    def extra_state_attributes(self):
        """该条黑名单记录的完整明细（空值不输出）。"""
        return mac_acl_entry(self._row()) or None

    async def async_turn_on(self, **kwargs):
        await self.coordinator.async_control_device(
            {"func_name": "acl_mac", "action": "up", "param": {"id": str(self._macid)}})
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        await self.coordinator.async_control_device(
            {"func_name": "acl_mac", "action": "down", "param": {"id": str(self._macid)}})
        await self.coordinator.async_request_refresh()


class IKuaiContainerSwitch(IKUAIBaseSwitch):
    """Docker 容器 / 虚拟机 启停开关。

    设备归属：Docker 容器 → **所有容器共用一台 Docker 设备**（容器自己的映射端口
    与 Web 访问地址写在本开关的状态属性里：``host_ports`` / ``web_url``）；
    虚拟机 → 各自独立的虚拟机设备（「访问」= 集成签发的 noVNC 远程控制台地址）。
    """

    def __init__(self, hass, coordinator, item_type, suffix, item):
        super().__init__(hass, coordinator)
        self._item_type = item_type  # docker / qemu
        self._suffix = suffix
        self._item = dict(item) if isinstance(item, dict) else {}
        self._item_name = self._item.get("name", item_type)
        self._attr_icon = "mdi:docker" if item_type == "docker" else "mdi:monitor"
        self._attr_entity_category = category("config")
        self._state_key = "state" if item_type == "docker" else "status"
        self._state_on = "running" if item_type == "docker" else 1
        # 固定英文实体 ID：Docker = switch.ikuai_docker_<容器名>，
        # 虚拟机 = switch.ikuai_vm_<虚拟机名>_power（名字取不到 ascii 片段时退回用 ID）
        name_slug = entity_id_slug(self._item_name) or str(suffix)
        if item_type == "docker":
            self._setup_forced_entity_id("switch", f"docker_{name_slug}")
        else:
            self._setup_forced_entity_id("switch", f"vm_{name_slug}_power")

    @property
    def device_info(self):
        """实时计算：虚拟机控制台链接有有效期、容器端口也可能改，都不能写死。"""
        row = self._current()
        if self._item_type == "docker":
            # 所有容器共用一台 Docker 设备；该容器的端口/访问地址在状态属性里
            return docker_device_info(self.coordinator)
        return vm_device_info(
            self.coordinator, row, console_url_for(self.coordinator, row))

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        if self._item_type != "docker":
            # 「远程控制」开关切换时立即刷新（console_url 会随之出现/消失）
            remove = add_manager_listener(self.coordinator, self.async_write_ha_state)
            if remove is not None:
                self.async_on_remove(remove)

    @property
    def name(self):
        """实体短名（完整名称 = 设备名 + 短名）。

        * Docker 容器：**用容器名**（如 ``qb``）—— 所有容器共用一台 Docker 设备，
          实体全部叫「电源」就分不清谁是谁了；
        * 虚拟机：统一叫「电源」—— 每台虚拟机一台设备，设备名已经是「虚拟机 win7」，
          实体再叫一遍就重复了（与 esxi_stats 的虚拟机电源开关一致）。
        """
        if self._item_type == "docker":
            return self._item_name or f"容器 {self._suffix}"
        return "电源"

    @property
    def unique_id(self):
        p = "docker" if self._item_type == "docker" else "vm"
        return f"{DOMAIN}_{p}switch_{self._suffix}_{self.coordinator.host}"

    def _current(self):
        key = "docker" if self._item_type == "docker" else "qemu"
        for row in (self.coordinator.data.get("ext") or {}).get(key) or []:
            if str(row.get("id")) == self._suffix:
                return row
        return dict(self._item)

    @property
    def is_on(self):
        return self._current().get(self._state_key) == self._state_on

    @property
    def extra_state_attributes(self):
        row = self._current()
        if self._item_type == "docker":
            # 该容器的完整明细：原「Docker容器数」实体 entries 里的全部字段，字段与
            # 取值都照搬（`env`/`cmd` 这类空串也保留，方便按固定键名取用）
            keys = ("name", "image", "state", "status", "id", "created",
                    "ipaddr", "ip6addr", "gateway", "ip6gateway", "mac", "interface",
                    "memused", "memory", "cpu_used", "auto_start",
                    "mounts", "env", "cmd", "comment")
            attrs = {k: row[k] for k in keys if k in row}
            # 端口映射与「访问」地址（设备页访问按钮取的就是首选 web_url）
            web_url, ports = container_web_url(self.coordinator, row)
            if ports:
                attrs["host_ports"] = ", ".join(str(p) for p in ports)
            if web_url:
                attrs["web_url"] = web_url
            return attrs or None

        keys = ("name", "system", "status", "cpu_cores", "mem_size", "cpu_usage",
                "auto_start", "enabled", "partname", "brname", "vdisk", "vnc_port",
                "vnc_acl", "elapse_time")
        # 注意：vnc_pwd 故意不进属性（会被写进状态机与 recorder）。
        # 控制台页面用的密码由 console.py 在渲染时单独下发。
        attrs = {k: row[k] for k in keys if k in row and row[k] != ""}
        # 原「远程控制台」传感器（已并入本开关）里**不重复**的字段：
        #   vm_id       —— 虚拟机 ID（vm_name 与上面的 name 同值，不再重复给）
        #   console_state —— 控制台可用性（可用 / 未开机 / 未开启 VNC / 已关闭）
        #   vnc_auth    —— 只表示"有没有密码"，不含密码本身
        if row.get("id") is not None:
            attrs["vm_id"] = row.get("id")
        attrs["console_state"] = _console_state(self.coordinator, row)
        if row.get("vnc_pwd"):
            attrs["vnc_auth"] = "password"
        # vnc_host / vnc_port / vnc_acl / console_url / remote_enabled 上面已有，不再重复
        attrs["remote_enabled"] = remote_enabled_for(self.coordinator)
        attrs["vnc_host"] = vnc_host(self.coordinator)
        url = console_url_for(self.coordinator, row)
        if url:
            attrs["console_url"] = url
        return attrs or None

    def update_item(self, item):
        self._item = dict(item) if isinstance(item, dict) else self._item
        self._item_name = item.get("name", self._item_name)

    async def async_turn_on(self, **kwargs):
        func = "docker_container" if self._item_type == "docker" else "qemu"
        await self.coordinator.async_control_device(
            {"func_name": func, "action": "up", "param": {"id": self._suffix}})
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        if self._item_type == "docker":
            body = {"func_name": "docker_container", "action": "down",
                    "param": {"id": self._suffix}}
        else:
            body = {"func_name": "qemu", "action": "down",
                    "param": {"id": self._suffix, "force": 0}}
        await self.coordinator.async_control_device(body)
        await self.coordinator.async_request_refresh()


class IKUAIRemoteControlSwitch(IKUAIBaseSwitch, RestoreEntity):
    """全局「远程控制」开关（挂在爱快主机设备上，控制所有虚拟机的页面访问权限）。

    语义：
        on（默认）→ 允许从 HA 打开任意虚拟机的页面（设备页「访问」可用）；
        off       → 清空所有 console_url 与虚拟机设备的「访问」链接，并拒绝新的
                    跳转（控制台视图 403）。**已经打开的页面不会被掐断** ——
                    集成只做同源代理、不代管通道（``session_count`` 恒为 0），
                    要断开请直接在爱快里操作或关闭 VNC；重新打开后会重新签发链接。

    这个开关不调用 iKuai 接口，只改集成内部状态；状态本身用 RestoreEntity 持久化。
    """

    _attr_should_poll = False
    _attr_icon = "mdi:remote-desktop"

    def __init__(self, hass, coordinator):
        super().__init__(hass, coordinator)
        self._attr_name = REMOTE_CONTROL_NAME
        # 固定英文实体 ID：switch.ikuai_remote_control
        self._setup_forced_entity_id("switch", "remote_control")

    @property
    def unique_id(self):
        return f"{DOMAIN}_remote_control_{self.coordinator.host}"

    @property
    def is_on(self):
        return remote_enabled_for(self.coordinator)

    @property
    def extra_state_attributes(self):
        """顺带暴露当前有多少台虚拟机可被远程控制、有几个活动会话。"""
        manager = manager_for(self.coordinator)
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        vms = [vm for vm in ((data.get("ext") or {}).get("qemu") or [])
               if _has_vnc(vm)]
        attrs = {
            "vnc_vm_count": len(vms),
        }
        if manager is not None:
            attrs["active_sessions"] = manager.session_count
            attrs["console_vms"] = [vm.get("name") for vm in vms]
        return attrs

    async def async_added_to_hass(self):
        """恢复上次状态并写进控制台管理器。"""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        enabled = (REMOTE_CONTROL_DEFAULT if last_state is None
                   else last_state.state == "on")
        manager = manager_for(self.coordinator)
        if manager is not None:
            # 启动时只是"记住"状态，不需要切断（此时还没有任何会话）
            await manager.async_apply_restored(enabled)

    async def async_turn_on(self, **kwargs):
        await self._async_set(True)

    async def async_turn_off(self, **kwargs):
        await self._async_set(False)

    async def _async_set(self, enabled: bool):
        manager = manager_for(self.coordinator)
        if manager is not None:
            await manager.async_set_enabled(enabled)
        self.async_write_ha_state()


def _has_vnc(vm: dict) -> bool:
    """该虚拟机是否开了 VNC（能远程控制的前提）。"""
    try:
        return int(str((vm or {}).get("vnc_port") or "0")) > 0
    except (TypeError, ValueError):
        return False


class IKuaiApiSwitch(IKUAIBaseSwitch, RestoreEntity):
    """透传 API 的总开关（默认关闭）。

    语义：
        on  → ``/ikuai/api/call`` 可用（GET 地址栏 / POST 单次或批量）；
        off → 该端点直接 403，**不会向爱快发任何请求**（视图本身零开销）。

    这个开关不调用 iKuai 接口，只改集成内部状态；开关自身的属性就是这套 API 的
    **说明书**：请求地址、常用请求体（GET 与 POST 两种写法）、密钥用法与调用统计。

    状态持久化用集成自己的 Store（``.storage/ikuai.api_switch``），与 recorder
    无关：重启 HA、清空历史库都不会丢；没有存档时才回到默认（关闭）。
    """

    _attr_should_poll = False
    _attr_entity_category = category("config")
    _attr_icon = "mdi:api"
    # 常用请求体、当前密钥与统计不写数据库（状态机/模板/前端照常可读）
    _unrecorded_attributes = frozenset({"presets", "stats", "current_key"})

    def __init__(self, hass, coordinator):
        super().__init__(hass, coordinator)
        self._attr_name = API_SWITCH_NAME
        self._attr_is_on = API_ENABLED_DEFAULT
        self._store = Store(hass, 1, f"{DOMAIN}.api_switch")
        # 固定英文实体 ID：switch.ikuai_api
        self._setup_forced_entity_id("switch", API_SWITCH_SUFFIX)

    @property
    def unique_id(self):
        return f"{DOMAIN}_api_switch_{self.coordinator.host}"

    @property
    def is_on(self):
        return bool(self._attr_is_on)

    async def async_added_to_hass(self):
        """恢复上次状态（默认关闭），并把状态登记给查询视图。"""
        await super().async_added_to_hass()
        saved = None
        try:
            data = await self._store.async_load()
            if isinstance(data, dict):
                saved = data.get("enabled")
        except Exception:  # pylint: disable=broad-except - 存档异常就当没有
            _LOGGER.debug("iKuai 查询 API 开关：读取存档失败", exc_info=True)
        if isinstance(saved, bool):
            self._attr_is_on = saved
        else:
            # 没有存档时（首次安装/存档被清）退回 recorder 里的上次状态
            last_state = await self.async_get_last_state()
            if last_state is not None and last_state.state in ("on", "off"):
                self._attr_is_on = last_state.state == "on"
            else:
                self._attr_is_on = API_ENABLED_DEFAULT
        set_api_enabled(self._hass, self.entry_id, self._attr_is_on)

    async def async_turn_on(self, **kwargs):
        await self._async_set(True)

    async def async_turn_off(self, **kwargs):
        await self._async_set(False)

    async def _async_set(self, enabled: bool):
        self._attr_is_on = bool(enabled)
        set_api_enabled(self._hass, self.entry_id, self._attr_is_on)
        try:
            await self._store.async_save({"enabled": self._attr_is_on})
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning("iKuai 查询 API 开关：状态保存失败", exc_info=True)
        self.async_write_ha_state()
        _LOGGER.info("iKuai 查询 API 已%s", "启用" if self._attr_is_on else "停用")

    @property
    def extra_state_attributes(self):
        """这套 API 的说明书：请求地址 + 常用请求体 + 密钥用法 + 统计。"""
        stats = api_stats(self._hass, self.entry_id)
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        current_key = None
        if self._attr_is_on and self.entry_id:
            current_key = ensure_key(self._hass, self.entry_id, data).get("key")
        try:
            multi = len(entry_ids(self._hass)) > 1
        except Exception:  # pylint: disable=broad-except
            multi = False
        auth_text = ("三种任选：?key=<密钥> / 请求头 X-IKuai-Key / HA 登录态"
                     "（前端 hass.fetchWithAuth）")
        if multi:
            auth_text += ("；装了多台爱快：用哪台的 key 就打哪台（key 自带归属），"
                          "或显式加 &entry=<条目 id>")
        return {
            "enabled": bool(self._attr_is_on),
            "entry": self.entry_id,
            "endpoint": API_CALL_PATH,
            "methods": 'GET（地址栏直接访问）/ POST（单个载荷，或 {"calls": [...]} 批量）',
            "body_format": '{"func_name": "...", "action": "show", "param": {...}}',
            "auth": auth_text,
            "key_sensor": "sensor.ikuai_api_key（每小时自动换新，属性里给 key）",
            "key_ttl_seconds": API_KEY_TTL_SECONDS,
            "calls_max": API_CALLS_MAX,
            "note": ("纯透传：请求体是爱快原生载荷、响应体是爱快原始响应（不脱敏、"
                     "写操作也放行）—— key 等价于爱快后台权限，只在内网用、别外发"),
            "pagination_hint": ("爱快 limit 单次最多 100 条；要拿全就把 0,100 / "
                                "100,100 / … 放进同一个 calls 批量里一次取回"),
            "current_key": current_key,
            # 常用请求体：每条给「完整 GET 地址（已嵌入密钥）」与 POST body 两种写法
            "presets": preset_entries(self._hass, self.entry_id, current_key),
            "get_note": ("GET 示例是完整地址、密钥与 entry 都已嵌进去"
                         "（开关关闭时为 <密钥> 占位）；参数值里含 & = 空格或中文时"
                         "请做 URL 编码，或直接改用 POST"),
            "get_shortcut": ('GET 也认"整段 JSON"写法（省略参数名）：'
                             '…/ikuai/api/call?key=<密钥>&{"func_name":"url_black",'
                             '"action":"show","param":{"TYPE":"url_white_refer"}}'),
            "stats": dict(stats),
            "querytime": data.get("querytime"),
        }


class IKuaiLogPollSwitch(IKUAIBaseSwitch, RestoreEntity):
    """「采集日志」开关：关掉就停止日志定时采集（默认开启）。

    语义：
        on（默认）→ 每 60 秒抓 12 类日志（条数 + 最近一条），供
                    ``sensor.ikuai_log_stats`` / ``sensor.ikuai_log_url_log`` 使用，
                    新日志同时投递 ``ikuai_log`` 事件与 HA 日志簿；
        off       → **一个日志请求都不发**（本来 12 类日志每分钟各抓一次），并且把上面
                    两个实体在注册表里**禁用**（``disabled_by=integration``）后重载本
                    条目 —— 它们会从实体列表里消失；重新打开开关会自动恢复启用；
        on/off 都不影响显式调用的服务（``ikuai.log_get`` / ``ikuai.log_clear``）。

    这个开关不调用 iKuai 接口，只改集成内部状态；状态用集成自己的 Store
    （``.storage/ikuai.log_switch``）持久化：重启 HA、清空历史库都不会丢 ——
    也正因为持久化在 Store 里，禁用/启用时触发的重载不会把开关自己弄丢。
    """

    _attr_should_poll = False
    _attr_entity_category = category("config")
    _attr_icon = "mdi:text-search"

    def __init__(self, hass, coordinator):
        super().__init__(hass, coordinator)
        self._attr_name = LOG_SWITCH_NAME
        self._attr_is_on = LOG_POLL_DEFAULT
        self._store = Store(hass, 1, f"{DOMAIN}.log_switch")
        # 固定英文实体 ID：switch.ikuai_log_poll
        self._setup_forced_entity_id("switch", LOG_SWITCH_SUFFIX)

    @property
    def unique_id(self):
        return f"{DOMAIN}_log_switch_{self.coordinator.host}"

    @property
    def is_on(self):
        return bool(self._attr_is_on)

    async def async_added_to_hass(self):
        """恢复上次状态（默认开启），并把状态登记给日志采集调度。"""
        await super().async_added_to_hass()
        saved = None
        try:
            data = await self._store.async_load()
            if isinstance(data, dict):
                saved = data.get("enabled")
        except Exception:  # pylint: disable=broad-except - 存档异常就当没有
            _LOGGER.debug("iKuai 采集日志开关：读取存档失败", exc_info=True)
        if isinstance(saved, bool):
            self._attr_is_on = saved
        else:
            # 没有存档时（首次安装/存档被清）退回 recorder 里的上次状态
            last_state = await self.async_get_last_state()
            if last_state is not None and last_state.state in ("on", "off"):
                self._attr_is_on = last_state.state == "on"
            else:
                self._attr_is_on = LOG_POLL_DEFAULT
        set_log_poll_enabled(self._hass, self.entry_id, self._attr_is_on)
        if not self._attr_is_on:
            # 开关是关的（例如上次用法是关）→ 把两个日志实体标成注册表禁用
            # （下一次重载即消失）；这里**不触发重载**，避免在加载过程中重载自己
            self._set_entities_disabled(True)

    # ------------------------------------------------- 注册表禁用 / 启用
    def _log_entity_ids(self) -> list:
        """两个日志实体的注册表 entity_id（按 unique_id 查，兼容 ID 被挤成 xxx_2）。"""
        ids: list = []
        try:
            registry = er.async_get(self._hass)
        except Exception:  # pylint: disable=broad-except
            return ids
        host = self.coordinator.host
        for unique_id in (f"{DOMAIN}_log_stats_{host}",
                          f"{DOMAIN}_log_url_log_{host}"):
            try:
                entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            except Exception:  # pylint: disable=broad-except
                entity_id = None
            if entity_id:
                ids.append(entity_id)
        return ids

    @staticmethod
    def _disabler():
        """注册表"由集成禁用"标记（老版本 HA 没有该枚举时退回字符串）。"""
        try:
            from homeassistant.helpers.entity_registry import RegistryEntryDisabler
            return RegistryEntryDisabler.INTEGRATION
        except Exception:  # pylint: disable=broad-except
            return "integration"

    def _set_entities_disabled(self, disabled: bool) -> int:
        """把两个日志实体在注册表里禁用 / 恢复启用；返回改动条数。"""
        try:
            registry = er.async_get(self._hass)
        except Exception:  # pylint: disable=broad-except
            return 0
        want = self._disabler() if disabled else None
        changed = 0
        for entity_id in self._log_entity_ids():
            entry = registry.async_get(entity_id)
            if entry is None or entry.disabled_by == want:
                continue
            try:
                registry.async_update_entity(entity_id, disabled_by=want)
                changed += 1
            except Exception:  # pylint: disable=broad-except
                _LOGGER.debug("iKuai 采集日志开关：%s 的禁用状态设置失败",
                              entity_id, exc_info=True)
        return changed

    def _async_reload_entry(self) -> None:
        """重载本条目，让注册表里的"禁用/启用"立刻生效。

        HA 对平台实体没有热插拔：禁用后要重载才会从实体列表消失，启用后也要重载
        才会重新创建。开关状态存在 Store 里，所以重载不会把它弄丢。
        """
        try:
            reload_entry = getattr(
                getattr(self._hass, "config_entries", None), "async_reload", None)
            if callable(reload_entry) and self.entry_id:
                self._hass.async_create_task(reload_entry(self.entry_id))
        except Exception:  # pylint: disable=broad-except
            _LOGGER.debug("iKuai 采集日志开关：触发集成重载失败", exc_info=True)

    # ---------------------------------------------------------------- 开关
    async def async_turn_on(self, **kwargs):
        await self._async_set(True)

    async def async_turn_off(self, **kwargs):
        await self._async_set(False)

    async def _async_set(self, enabled: bool):
        self._attr_is_on = bool(enabled)
        # 先落盘：紧接着可能要重载条目，重载后新实体要读到正确的状态
        try:
            await self._store.async_save({"enabled": self._attr_is_on})
        except Exception:  # pylint: disable=broad-except
            _LOGGER.warning("iKuai 采集日志开关：状态保存失败", exc_info=True)
        set_log_poll_enabled(self._hass, self.entry_id, self._attr_is_on)
        notify = getattr(self.coordinator, "async_update_listeners", None)
        if callable(notify):
            notify()        # 立刻刷新两个日志实体的可用性（不用等下一轮轮询）
        changed = self._set_entities_disabled(not self._attr_is_on)
        self.async_write_ha_state()
        _LOGGER.info("iKuai 日志采集已%s（%s）",
                     "启用" if self._attr_is_on else "停用",
                     "两个日志实体已恢复启用" if self._attr_is_on
                     else "两个日志实体已在注册表禁用")
        if changed:
            self._async_reload_entry()

    @property
    def extra_state_attributes(self):
        hub = getattr(self.coordinator, "loghub", None)
        entities = ["sensor.ikuai_log_stats", "sensor.ikuai_log_url_log"]
        return {
            "enabled": bool(self._attr_is_on),
            "interval_seconds": getattr(hub, "interval", None),
            "categories": len(LOG_CATEGORIES),
            "entities": entities,
            "entities_disabled": not self._attr_is_on,
            "note": ("关闭后不再定时抓日志（12 类 × 每分钟），并把 entities 里这两个"
                     "实体在注册表禁用（重载后从实体列表消失）；重新打开会自动恢复"
                     "启用。服务 ikuai.log_get / log_clear 是显式调用，不受影响"),
        }
