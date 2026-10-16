"""iKuai 二元传感器：WAN/线路/告警/消息 + 物理WAN口在线 + 认证账号在线情况。"""
import logging
import time

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
)

from .const import (
    BINARY_INTO_SYSTEM_INFO,
    BINARY_SENSOR_TYPES,
    COORDINATOR,
    DOMAIN,
)
from .entity import (
    DynamicEntityManager,
    IKuaiEntity,
    auth_device_info,
    auth_entity_slug,
    category,
    dhms,
    dhms_from_ts,
    human_bytes,
    internal_key_slug,
    wan_internet_text,
)
from .sensor import _cap_attrs

_LOGGER = logging.getLogger(__name__)

_DEVICE_CLASS_MAP = {
    "connectivity": BinarySensorDeviceClass.CONNECTIVITY,
    "problem": BinarySensorDeviceClass.PROBLEM,
}


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]

    # 「爱快云已绑定」「固件有新版本」已并入「系统信息」聚合实体（不再单独成实体）
    entities = [IKuaiBinarySensor(kind, coordinator)
                for kind in BINARY_SENSOR_TYPES
                if kind not in BINARY_INTO_SYSTEM_INFO]
    async_add_entities(entities, False)

    def band_items(data):
        bands = (data.get("ext") or {}).get("wan_bands") or []
        return {str(b.get("id")): b for b in bands if b.get("id") is not None}

    DynamicEntityManager(coordinator, async_add_entities, IKuaiWanBandBinary, band_items)

    # 认证账号（PPPoE / PPTP / L2TP / OVPN）在线情况：一个账号一个实体，
    # 全部账号共用一台「认证账号」设备；同账号的配置详情见 sensor 平台的「账号信息」实体
    def auth_items(data):
        rows = (data.get("ext") or {}).get("auth_accounts") or []
        return {str(r.get("key")): r for r in rows if r.get("key")}

    DynamicEntityManager(coordinator, async_add_entities, IKuaiAuthAccountBinary, auth_items)

    # 每口「网口 ethN 链路」实体已删除：物理口的 link/速率/双工/型号/驱动/MAC 与
    # 绑定关系全部并入 sensor.ikuai_interfaces（接口总览），旧注册表条目由
    # sensor._removed_unique_prefixes 里的 ikuai_ethlink_ 前缀清理。


class IKuaiBinarySensor(IKuaiEntity, BinarySensorEntity):
    """静态二元传感器。"""

    def __init__(self, kind, coordinator):
        super().__init__(coordinator)
        self.kind = kind
        spec = BINARY_SENSOR_TYPES[kind]
        self._spec = spec
        self._attr_device_class = _DEVICE_CLASS_MAP.get(spec.get("device_class"))
        self._attr_entity_category = category(spec.get("entity_category"))
        # 固定英文实体 ID：binary_sensor.ikuai_alarm / ikuai_wan_fault ...
        self._setup_forced_entity_id("binary_sensor", internal_key_slug(kind))

    @property
    def name(self):
        return self._spec["name"]

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.kind}_{self.coordinator.host}"

    @property
    def is_on(self):
        data = self.coordinator.data or {}
        ext = data.get("ext") or {}
        try:
            return bool(self._spec["value"](data, ext))
        except Exception:
            return False

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        ext = data.get("ext") or {}
        attrs = {"querytime": data.get("querytime")}
        attr_key = self._spec.get("attrs_key")
        if attr_key:
            value = ext.get(attr_key)
            if isinstance(value, list):
                attrs["entries"] = _cap_attrs(value)
                attrs["count"] = len(value)
            elif value is not None:
                attrs[attr_key] = _cap_attrs(value)
        return attrs


class IKuaiWanBandBinary(IKuaiEntity, BinarySensorEntity):
    """物理 WAN 口状态（每口一个，显示名「WAN x 状态」）。

    state = 已连接（on）/ 未连接（off）：有 IP 且带更新时间即算已连接。
    带 ``device_class: connectivity``，前端按设备类显示成"已连接/未连接"。

    该口的全部"有用的信息"都挂在这里（接口名 / IP / 网关 / DNS / MAC / 入网方式 /
    是否默认线路 / 上线时长）；同编号的 ``sensor.ikuai_wan_<id>_upload`` /
    ``_download`` 只出速率、不带属性，同一份信息不会在两处各存一份。
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, suffix, item):
        super().__init__(coordinator)
        self._suffix = suffix
        self._band_name = item.get("name") or item.get("interface") or f"wan{suffix}"
        # 固定英文实体 ID：binary_sensor.ikuai_wan_1_online
        # （ID 保持不变，只改显示名 —— 免得既有自动化/卡片失联）
        self._setup_forced_entity_id("binary_sensor", f"wan_{suffix}_online")

    @property
    def name(self):
        return f"WAN {self._band_name} 状态"

    @property
    def unique_id(self):
        return f"{DOMAIN}_wanband_online_{self._suffix}_{self.coordinator.host}"

    def _band(self):
        for b in (self.coordinator.data.get("ext") or {}).get("wan_bands") or []:
            if str(b.get("id")) == self._suffix:
                return b
        return {}

    @property
    def is_on(self):
        band = self._band()
        ip = band.get("ip_addr") or band.get("pppoe_ip_addr")
        ts = band.get("updatetime") or band.get("pppoe_updatetime") or 0
        try:
            ts_ok = int(ts) > 0
        except (ValueError, TypeError):
            ts_ok = False
        return bool(ip and ts_ok)

    @property
    def extra_state_attributes(self):
        """该口的配置型信息都挂在这里（速率实体上不再重复）。"""
        band = self._band()
        seconds, uptime_text = dhms_from_ts(
            band.get("updatetime") or band.get("pppoe_updatetime"))
        attrs = {
            "interface": band.get("name") or band.get("interface"),
            "ip": band.get("ip_addr") or band.get("pppoe_ip_addr") or None,
            "uptime_seconds": seconds,
            "uptime_text": uptime_text,
            "gateway": band.get("gateway") or band.get("pppoe_gateway") or None,
            "dns1": band.get("dns1") or band.get("pppoe_dns1") or None,
            "dns2": band.get("dns2") or band.get("pppoe_dns2") or None,
            "mac": band.get("mac") or band.get("bandif") or None,
            "internet": band.get("internet"),
            "internet_text": wan_internet_text(band.get("internet")),
            "default_route": band.get("default_route"),
            "querytime": (self.coordinator.data or {}).get("querytime"),
        }
        return {k: v for k, v in attrs.items() if v not in ("", None)}

    def update_item(self, item):
        self._band_name = item.get("name") or item.get("interface") or self._band_name


class IKuaiAuthAccountBinary(IKuaiEntity, BinarySensorEntity):
    """认证账号在线情况（每账号一个实体，账号都挂在同一台「认证账号」设备下）。

    * state：在线（on）/ 离线（off）；
    * attributes：**在线情况** —— 会话 IP/MAC、会话标识、接入方式与接口、
      上线时间与在线时长、本次会话上下行流量。

    账号自身的配置详情（是否启用、有效期、限速/共享数、绑定线路、备注、最近上下线
    时间…）放在同账号的「账号信息」传感器上（sensor.IKuaiAuthAccountInfo），
    两个实体同属一台设备，互不重复。
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    # 在线会话字段白名单（顺序即展示顺序）
    _SESSION_FIELDS = ("pppdev", "interface", "ppptype", "ip_addr", "mac", "session",
                       "uid", "expires", "packages", "download", "upload")

    def __init__(self, coordinator, suffix, item):
        super().__init__(coordinator)
        self._suffix = suffix
        self._item = dict(item) if isinstance(item, dict) else {}
        # 固定英文实体 ID：binary_sensor.ikuai_auth_cs_online
        self._setup_forced_entity_id(
            "binary_sensor", f"auth_{auth_entity_slug(self._account(), suffix)}_online")
        self._attr_name = f"{suffix} 在线"

    def _account(self) -> dict:
        """取本账号最新数据（协调器里找不到时退回初始化快照）。"""
        for row in (self.coordinator.data.get("ext") or {}).get("auth_accounts") or []:
            if str(row.get("key")) == self._suffix:
                return row
        return dict(self._item)

    def update_item(self, item):
        self._item = dict(item) if isinstance(item, dict) else self._item

    @property
    def unique_id(self):
        return f"{DOMAIN}_auth_{self._suffix}_online_{self.coordinator.host}"

    @property
    def device_info(self):
        return auth_device_info(self.coordinator)

    @property
    def is_on(self):
        return bool(self._account().get("online"))

    @property
    def extra_state_attributes(self):
        account = self._account()
        session = account.get("session") or {}
        attrs = {
            "username": account.get("username") or self._suffix,
            "account_id": account.get("id"),
            "online": bool(account.get("online")),
            "ppptype": account.get("ppptype"),
        }
        attrs.update({k: session[k] for k in self._SESSION_FIELDS
                      if k in session and session[k] not in ("", None)})

        # 上线时间与在线时长（原始秒数 + 可读时长）
        try:
            seconds = int(time.time()) - int(session.get("auth_time"))
        except (TypeError, ValueError):
            seconds = None
        if seconds is not None and seconds >= 0:
            attrs["auth_time"] = session.get("auth_time")
            attrs["online_seconds"] = seconds
            attrs["online_time"] = dhms(seconds)
        # 本次会话上下行流量（原始字节 + 可读文本）
        for key in ("download", "upload"):
            if key in session:
                attrs[f"{key}_text"] = human_bytes(session.get(key))
        attrs["querytime"] = (self.coordinator.data or {}).get("querytime")
        return attrs


# class IKuaiEthLinkBinary（每口「网口 ethN 链路」）已删除：
# 物理口的 link / 协商速率 / 双工 / 型号 / 驱动 / MAC 与绑定关系，全部并入
# sensor.ikuai_interfaces（接口总览）；旧注册表条目由
# sensor._removed_unique_prefixes 里的 ikuai_ethlink_ 前缀清理。
