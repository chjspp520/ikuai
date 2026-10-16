"""iKuai 按钮：重启/关机/对时/清流量/告警消息/WAN重连/网络唤醒/MAC拉黑放行。"""
import asyncio
import logging
import re

from homeassistant.components.button import ButtonEntity, ButtonDeviceClass
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .const import COORDINATOR, DIAL_INLINE_MAX, DOMAIN, BUTTON_TYPES
from .entity import (
    DynamicEntityManager,
    IKuaiEntity,
    category,
    internal_key_slug,
    mac_acl_entry,
)
from .sensor import ATTR_ROW_MAX, _cap_attrs

_LOGGER = logging.getLogger(__name__)

MAC_INPUT_ENTITY = "text.ikuai_mac_input"


async def _async_cleanup_wanline_buttons(hass, entry, coordinator):
    """聚合模式下清理注册表残留的单线操作按钮。

    线路超过 DIAL_INLINE_MAX 条时不再为每条线路注册按钮（操作统一走悬浮窗
    宽带设置页）；升级前注册的按钮需按 unique_id 显式清除。
    """
    for _ in range(60):
        data = coordinator.data
        if data and (data.get("ext") or {}):
            break
        await asyncio.sleep(2)
    data = coordinator.data
    if not data:
        return
    lines = (data.get("ext") or {}).get("wan_lines") or []
    if len(lines) <= DIAL_INLINE_MAX:
        return

    registry = er.async_get(hass)
    host = coordinator.host
    removed = 0
    for reg_entry in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        if reg_entry.domain != "button":
            continue
        uid = reg_entry.unique_id
        for act in ("reconnect", "up", "down", "del"):
            prefix = f"{DOMAIN}_wanline_{act}_"
            tail = f"_{host}"
            if uid.startswith(prefix) and uid.endswith(tail) \
                    and uid[len(prefix):-len(tail)]:
                registry.async_remove(reg_entry.entity_id)
                removed += 1
                break
    if removed:
        _LOGGER.info("拨号线路聚合：清理 %d 个残留单线操作按钮", removed)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up iKuai button entities from a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]

    buttons = [IKUAIButton(kind, coordinator) for kind in BUTTON_TYPES]
    buttons.append(IKuaiMacBlockButton(coordinator))
    buttons.append(IKuaiMacUnblockButton(coordinator))
    async_add_entities(buttons, False)

    def band_items(data):
        bands = (data.get("ext") or {}).get("wan_bands") or []
        return {str(b.get("id")): b for b in bands if b.get("id") is not None}

    def wake_items(data):
        rows = (data.get("ext") or {}).get("wakeups") or []
        return {str(r.get("id")): r for r in rows if r.get("id") is not None}

    DynamicEntityManager(coordinator, async_add_entities, IKuaiWanReconnectButton, band_items)
    DynamicEntityManager(coordinator, async_add_entities, IKuaiWakeButton, wake_items)

    def wan_line_items(data):
        lines = (data.get("ext") or {}).get("wan_lines") or []
        if len(lines) > DIAL_INLINE_MAX:
            # 聚合模式：单线操作并入悬浮窗「宽带设置」页，不再注册按钮
            return {}
        items = {}
        for line in lines:
            lid = line.get("id")
            if lid is None:
                continue
            for act in ("reconnect", "up", "down", "del"):
                items[f"{lid}:{act}"] = line
        return items

    DynamicEntityManager(
        coordinator, async_add_entities, IKuaiWanLineButton, wan_line_items)

    # 聚合模式下清除升级前残留的单线按钮注册表条目
    hass.async_create_task(
        _async_cleanup_wanline_buttons(hass, config_entry, coordinator))


class IKUAIButton(IKuaiEntity, ButtonEntity):
    """Define a static iKuai button entity."""

    def __init__(self, kind, coordinator):
        super().__init__(coordinator)
        self.kind = kind
        self._name = BUTTON_TYPES[self.kind]["name"]
        self._attr_icon = BUTTON_TYPES[self.kind].get("icon")
        dc = BUTTON_TYPES[self.kind].get("device_class")
        if dc == "restart":
            self._attr_device_class = ButtonDeviceClass.RESTART
        if dc in ("update",):
            self._attr_device_class = ButtonDeviceClass.UPDATE
        # 固定英文实体 ID：button.ikuai_restart / button.ikuai_shutdown ...
        self._setup_forced_entity_id("button", internal_key_slug(self.kind))

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.kind}_{self.coordinator.host}"

    async def async_press(self):
        await self.coordinator.async_control_device(BUTTON_TYPES[self.kind]["action_body"])
        await self.coordinator.async_request_refresh()


class IKuaiWanReconnectButton(IKuaiEntity, ButtonEntity):
    """物理 WAN 口重连。"""

    _attr_icon = "mdi:restart-alert"
    _attr_entity_category = category("config")

    def __init__(self, coordinator, suffix, item):
        super().__init__(coordinator)
        self._suffix = suffix
        self._band_name = item.get("name") or item.get("interface") or f"wan{suffix}"
        self._is_pppoe = item.get("internet") in (3, 4, "PPPOE")
        # 固定英文实体 ID：button.ikuai_wan_1_reconnect
        self._setup_forced_entity_id("button", f"wan_{suffix}_reconnect")

    @property
    def name(self):
        return f"WAN {self._band_name} 重连"

    @property
    def unique_id(self):
        return f"{DOMAIN}_wanreconnect_{self._suffix}_{self.coordinator.host}"

    def update_item(self, item):
        self._band_name = item.get("name") or item.get("interface") or self._band_name
        self._is_pppoe = item.get("internet") in (3, 4, "PPPOE")

    async def async_press(self):
        action = "link_pppoe_reconnect" if self._is_pppoe else "link_dhcp_reconnect"
        await self.coordinator.async_control_device(
            {"func_name": "wan", "action": action, "param": {"id": int(self._suffix)}})


class IKuaiWanLineButton(IKuaiEntity, ButtonEntity):
    """多拨线路（VLAN 拨号）单线路操作：重拨/启用/停用/删除。"""

    _attr_entity_category = category("config")

    _META = {
        "reconnect": ("重拨", "mdi:restart-alert"),
        "up": ("启用", "mdi:play-circle-outline"),
        "down": ("停用", "mdi:pause-circle-outline"),
        "del": ("删除", "mdi:trash-can-outline"),
    }

    def __init__(self, coordinator, suffix, item):
        super().__init__(coordinator)
        self._line_id, self._act = suffix.split(":", 1)
        self._line_id = int(self._line_id)
        self._item = item
        self._attr_icon = self._META[self._act][1]
        # 固定英文实体 ID：button.ikuai_wanline_3_reconnect / _up / _down / _del
        self._setup_forced_entity_id("button", f"wanline_{self._line_id}_{self._act}")

    @property
    def name(self):
        label = self._line_name()
        return f"拨号 {label} {self._META[self._act][0]}"

    @property
    def unique_id(self):
        return f"{DOMAIN}_wanline_{self._act}_{self._line_id}_{self.coordinator.host}"

    def _line_name(self):
        return self._item.get("vlan_name") or self._item.get("interface") \
            or f"line{self._line_id}"

    def _latest(self):
        for line in (self.coordinator.data.get("ext") or {}).get("wan_lines") or []:
            if line.get("id") == self._line_id:
                return line
        return self._item

    def update_item(self, item):
        self._item = item

    @property
    def available(self):
        if not super().available:
            return False
        line = self._latest()
        if self._act == "up":
            return line.get("enabled") != "yes"
        if self._act == "down":
            return line.get("enabled") == "yes"
        if self._act == "reconnect":
            # 静态 IP 线路（vlan_internet=0）没有重拨动作
            return line.get("vlan_internet", 2) != 0
        return True

    async def async_press(self):
        if self._act == "reconnect":
            line = self._latest()
            action = ("link_dhcp_reconnect" if line.get("vlan_internet") == 1
                      else "link_pppoe_reconnect")
            body = {"func_name": "wan", "action": action,
                    "param": {"id": self._line_id}}
        elif self._act == "del":
            body = {"func_name": "wan", "action": "vlan_del",
                    "param": {"id": self._line_id}}
        else:
            body = {"func_name": "wan",
                    "action": "vlan_up" if self._act == "up" else "vlan_down",
                    "param": {"id": self._line_id}}
        await self.coordinator.async_control_device(body)
        await self.coordinator.async_request_refresh()


class IKuaiWakeButton(IKuaiEntity, ButtonEntity):
    """网络唤醒列表中的单设备唤醒。"""

    _attr_icon = "mdi:power-cycle"
    _attr_entity_category = category("config")

    def __init__(self, coordinator, suffix, item):
        super().__init__(coordinator)
        self._suffix = suffix
        self._item = item
        # 固定英文实体 ID：button.ikuai_wake_1
        self._setup_forced_entity_id("button", f"wake_{suffix}")

    @property
    def name(self):
        row = self._row() or self._item
        label = row.get("comment") or row.get("mac") or self._suffix
        return f"网络唤醒 {label}"

    @property
    def unique_id(self):
        return f"{DOMAIN}_wake_{self._suffix}_{self.coordinator.host}"

    def _row(self):
        for r in (self.coordinator.data.get("ext") or {}).get("wakeups") or []:
            if str(r.get("id")) == self._suffix:
                return r
        return None

    async def async_press(self):
        await self.coordinator.async_control_device(
            {"func_name": "wakeup", "action": "wake_id", "param": {"id": self._suffix}})


class _IKuaiMacActionButton(IKuaiEntity, ButtonEntity):
    """读取 MAC 输入文本框中的 MAC 批量执行拉黑/放行。

    按钮在 HA 里没有 state（恒为 ``unknown``，这是按钮域的规范），但**属性可以带
    信息**，所以这里把两件事挂到属性上：

    * ``blacklist`` —— 当前黑名单（``acl_mac``）的**全部条目明细**（含备注、
      生效时段/星期、过期时间、启用状态），字段与每条目的开关实体一致；
    * ``input_macs`` —— 按下这个按钮**会操作哪些 MAC**（来自 ``text.ikuai_mac_input``，
      与真正执行时同一套解析），为空就说明按了也不会发生任何事。
    """

    _attr_entity_category = category("config")
    # 固定英文实体 ID 的后缀（button.ikuai_<suffix>）
    _ID_SUFFIX = ""

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._setup_forced_entity_id("button", self._ID_SUFFIX)

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # 输入框一改就刷新属性（否则要等下一轮主轮询，最多 10 秒才看得到）
        self.async_on_remove(async_track_state_change_event(
            self.hass, [self.input_entity_id()], self._handle_input_changed))

    @callback
    def _handle_input_changed(self, _event):
        self.async_write_ha_state()

    def input_entity_id(self) -> str:
        """MAC 输入文本框的实际实体 ID（按 unique_id 查注册表，查不到用固定 ID）。"""
        try:
            registry = er.async_get(self.hass)
            found = registry.async_get_entity_id(
                "text", DOMAIN, f"{DOMAIN}_mac_input_{self.coordinator.host}")
        except Exception:  # pylint: disable=broad-except
            found = None
        return found or MAC_INPUT_ENTITY

    @staticmethod
    def _macs_from_text(text):
        """从输入框文本解析 MAC（逗号/分号/换行分隔 —— 与按下时的规则完全一致）。"""
        out = []
        for chunk in str(text or "").replace(",", "\n").replace(";", "\n").splitlines():
            chunk = chunk.strip()
            match = re.match(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}", chunk)
            if match:
                out.append(match.group(0).replace("-", ":").lower())
        return out

    def _input_macs(self):
        """输入框里当前解析出的 MAC 列表。"""
        state = self.hass.states.get(self.input_entity_id())
        return self._macs_from_text(getattr(state, "state", None))

    async def _parse_macs(self):
        return self._input_macs()

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        rows = data.get("mac_control")
        entries = [mac_acl_entry(row) for row in rows
                   if isinstance(row, dict)] if isinstance(rows, list) else []
        macs = self._input_macs()
        attrs = {
            # 当前黑名单全部条目（字段与每条目的 switch.ikuai_mac_* 一致）
            "blacklist_total": len(entries),
            "blacklist": _cap_attrs(entries),
            # 按下本按钮会操作哪些 MAC
            "input_entity": self.input_entity_id(),
            "input_macs": macs,
            "input_count": len(macs),
            "querytime": data.get("querytime"),
        }
        if len(entries) > ATTR_ROW_MAX:
            attrs["blacklist_truncated"] = True
        return attrs


class IKuaiMacBlockButton(_IKuaiMacActionButton):
    _attr_icon = "mdi:account-cancel"
    _ID_SUFFIX = "mac_block"

    @property
    def name(self):
        return "拉黑输入的MAC"

    @property
    def unique_id(self):
        return f"{DOMAIN}_mac_block_input_{self.coordinator.host}"

    async def async_press(self):
        macs = await self._parse_macs()
        if not macs:
            _LOGGER.warning("MAC输入框为空或格式不正确")
            return
        for mac in macs:
            await self.coordinator.async_control_device(
                {"func_name": "mac_comment", "action": "add",
                 "param": {"mac": mac, "comment": "HA拉黑"}})
            await self.coordinator.async_control_device(
                {"func_name": "acl_mac", "action": "add",
                 "param": {"mac": mac, "enabled": "yes",
                           "time": "00:00-23:59", "week": "1234567"}})
        await self.coordinator.async_request_refresh()


class IKuaiMacUnblockButton(_IKuaiMacActionButton):
    _attr_icon = "mdi:account-check"
    _ID_SUFFIX = "mac_unblock"

    @property
    def name(self):
        return "放行输入的MAC"

    @property
    def unique_id(self):
        return f"{DOMAIN}_mac_unblock_input_{self.coordinator.host}"

    async def async_press(self):
        macs = await self._parse_macs()
        if not macs:
            _LOGGER.warning("MAC输入框为空或格式不正确")
            return
        for mac in macs:
            await self.coordinator.async_control_device(
                {"func_name": "acl_mac", "action": "del", "param": {"mac": mac}})
        await self.coordinator.async_request_refresh()
