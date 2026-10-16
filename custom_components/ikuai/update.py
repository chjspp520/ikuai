"""iKuai 固件更新实体（HA 设备页原生「有更新」提示）。

只展示版本信息，不提供一键升级（固件升级是高风险操作，统一在爱快后台执行）。
数据来自 ext.upgrade.data（每次扩展轮询抓取，无新接口）。
"""
from homeassistant.components.update import UpdateEntity

from .const import COORDINATOR, DOMAIN
from .entity import IKuaiEntity, category


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]
    async_add_entities([IKuaiFirmwareUpdate(coordinator)], False)


class IKuaiFirmwareUpdate(IKuaiEntity, UpdateEntity):
    """固件版本：当前版本 / 最新版本 / 更新说明。"""

    _attr_icon = "mdi:package-up"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_entity_category = category("config")
        # 固定英文实体 ID：update.ikuai_firmware
        self._setup_forced_entity_id("update", "firmware")

    @property
    def name(self):
        return "固件更新"

    @property
    def unique_id(self):
        return f"{DOMAIN}_firmware_{self.coordinator.host}"

    def _data(self) -> dict:
        block = (self.coordinator.data or {}).get("ext", {}).get("upgrade") or {}
        return block.get("data") if isinstance(block, dict) else None

    @staticmethod
    def _fmt(ver, build):
        if ver and build:
            return f"{ver} Build{build}"
        return ver or None

    @property
    def installed_version(self):
        data = self._data() or {}
        return self._fmt(data.get("system_ver"), data.get("build_date"))

    @property
    def latest_version(self):
        data = self._data() or {}
        new_ver = data.get("new_system_ver")
        if not new_ver:
            # 没有新版本时返回当前版本，HA 即显示「最新」
            return self.installed_version
        return self._fmt(new_ver, data.get("new_build_date"))

    @property
    def release_summary(self):
        return (self._data() or {}).get("update_content") or None

    @property
    def extra_state_attributes(self):
        data = self._data() or {}
        attrs = {
            "libproto_ver": data.get("libproto_ver"),
            "libaudit_ver": data.get("libaudit_ver"),
            "libdomain_ver": data.get("libdomain_ver"),
            "firmware_channel": data.get("firmware_channel"),
            "querytime": (self.coordinator.data or {}).get("querytime"),
        }
        return {k: v for k, v in attrs.items() if v not in ("", None)}
