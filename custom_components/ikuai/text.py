"""iKuai 文本输入实体：配合拉黑/放行按钮使用。"""
import logging

from homeassistant.components.text import TextEntity
from homeassistant.helpers.restore_state import RestoreEntity

from .const import COORDINATOR, DOMAIN
from .entity import IKuaiEntity, category

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]
    async_add_entities([IKUAIMACText(coordinator)], False)


class IKUAIMACText(IKuaiEntity, TextEntity, RestoreEntity):
    """MAC 地址输入框（支持多个，逗号/换行分隔）。"""

    _attr_entity_category = category("config")
    _attr_icon = "mdi:form-textbox"
    _attr_mode = "text"

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._value = ""
        # 固定英文实体 ID：text.ikuai_mac_input
        # （拉黑/放行按钮是按 unique_id 查注册表拿到这个实体 ID 的，不依赖硬编码）
        self._setup_forced_entity_id("text", "mac_input")

    @property
    def name(self):
        return "MAC拉黑输入"

    @property
    def unique_id(self):
        return f"{DOMAIN}_mac_input_{self.coordinator.host}"

    @property
    def native_value(self):
        return self._value

    async def async_set_value(self, value: str) -> None:
        self._value = value.strip() if isinstance(value, str) else ""
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and isinstance(last.state, str):
            self._value = last.state
