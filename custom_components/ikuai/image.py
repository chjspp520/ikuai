"""iKuai image 平台：每台虚拟机一个「屏幕画面」实体（与 esxi_stats 的 image 一致）。

* 实体名固定为「屏幕画面」，挂在各自的"虚拟机 xxx"设备下，实体 ID 形如
  ``image.ikuai_vm_4_screen``；
* 取图走 :mod:`vnc_snapshot`：按需连一次虚拟机 VNC、要一帧 Raw 画面、编成 PNG，
  不解码视频流、不占用控制台会话、「远程控制」总开关关闭时仍可用（只读）；
* 与 esxi_stats 一样：**没人看就不抓帧**（``should_poll = False``），
  并在返回时按需更新 ``image_last_updated`` 触发前端刷新。

取图的三条触发路径（**都很重要**，别只留第一条）：

1. ``async_image()``：前端真来取这张图（``/api/image_proxy/...``、设备页、屏幕墙）时抓一帧；
2. ``async_update()``：被 ``homeassistant.update_entity`` 点到时抓一帧
   —— 自定义卡片/自动化/脚本就是这么"叫醒"它的，**不依赖 image_proxy 的 token
   与签名路径**（早期只有第 1 条时，前端拿不到 token 就只能干等 → 一直显示"等待截图"）；
3. 开机瞬间（协调器看到 ``status`` 从"没开机"变成 1）：主动抓一帧把 ``image_last_updated``
   填上 —— 否则"有没有画面"的判据一直是空，谁也不知道该去取图。

前端用法（与 esxi 屏幕墙一致）：

    {{ state_attr('image.ikuai_vm_4_screen', 'entity_picture') }}   # /api/image_proxy/...
"""
from __future__ import annotations

import json
import logging
import time

from homeassistant.components.image import ImageEntity
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .const import (
    COORDINATOR,
    DOMAIN,
    VNC_SNAPSHOT_CACHE_SECONDS,
    VNC_SNAPSHOT_TIMEOUT,
)
from .entity import (
    DynamicEntityManager,
    ForcedEntityIdMixin,
    entity_id_slug,
    vm_device_info,
    vnc_host,
)
from .vnc_snapshot import VncSnapshotError, grab_png

_LOGGER = logging.getLogger(__name__)


def _vnc_port(vm: dict) -> int | None:
    try:
        port = int(str((vm or {}).get("vnc_port") or "0"))
    except (TypeError, ValueError):
        return None
    return port or None


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up iKuai image entities（每台虚拟机一个，随协调器动态增删）。"""
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]

    def vm_items(data):
        rows = (data.get("ext") or {}).get("qemu") or []
        return {str(v.get("id")): v for v in rows if v.get("id") is not None}

    DynamicEntityManager(
        coordinator, async_add_entities, IKuaiVmScreenImage, vm_items)


class IKuaiVmScreenImage(ForcedEntityIdMixin, ImageEntity):
    """虚拟机屏幕画面（按需从 VNC 抓一帧，PNG）。"""

    _attr_content_type = "image/png"
    _attr_icon = "mdi:monitor-screenshot"
    _attr_has_entity_name = True
    # 与 esxi_stats 同样的取舍：没人看就不抓帧（要抓帧时会主动刷新状态）
    _attr_should_poll = False

    def __init__(self, coordinator, suffix, item):
        # ImageEntity 需要在构造时拿到 hass（它会建 access_tokens / httpx client）；
        # 老版本签名不带 hass，这里做兼容
        try:
            super().__init__(coordinator.hass)
        except TypeError:  # pragma: no cover - 兼容旧签名
            super().__init__()
        self.coordinator = coordinator
        self._suffix = suffix
        self._fallback = dict(item) if isinstance(item, dict) else {}
        self._attr_name = "屏幕画面"
        self._png: bytes | None = None
        self._stamp = 0.0
        self._attr_image_last_updated = None
        self._attr_available = False
        # 上一轮"有没有开机"：用来抓"开机后的第一帧"（见 _handle_coordinator_update）
        self._was_running: bool | None = None
        # 上一次写状态时的 (state, 属性) 签名，用于跳过无变化的写入
        self._last_state_signature = None
        # 固定英文实体 ID：image.ikuai_vm_<虚拟机名>_screen（名字取不到 ascii 时退回 ID）
        name_slug = entity_id_slug(self._fallback.get("name")) or str(suffix)
        self._setup_forced_entity_id("image", f"vm_{name_slug}_screen")

    # ------------------------------------------------------------------ 数据
    def _current(self) -> dict:
        """取本机最新数据（协调器里没有时退回初始化快照）。"""
        for row in (self.coordinator.data or {}).get("ext", {}).get("qemu") or []:
            if str(row.get("id")) == self._suffix:
                return row
        return dict(self._fallback)

    def update_item(self, item):
        self._fallback = dict(item) if isinstance(item, dict) else self._fallback

    @property
    def unique_id(self):
        return f"{DOMAIN}_vmscreen_{self._suffix}_{self.coordinator.host}"

    @property
    def device_info(self):
        """挂在各自的虚拟机设备下（与启停开关、远程控制台同一个设备）。"""
        vm = self._current()
        return vm_device_info(self.coordinator, vm, None)

    def _is_running(self) -> bool:
        """这台虚拟机现在能不能抓画面：协调器正常 + 已开机 + 有 VNC 端口。"""
        vm = self._current()
        return bool(self.coordinator.last_update_success and vm.get("status") == 1
                    and _vnc_port(vm))

    @property
    def available(self) -> bool:
        """虚拟机没开机时不可用（不显示关机前的旧画面）。"""
        return self._is_running()

    @property
    def extra_state_attributes(self):
        vm = self._current()
        attrs = {
            "vm_name": vm.get("name"),
            "vm_id": vm.get("id"),
            "vnc_host": vnc_host(self.coordinator),
            "vnc_port": _vnc_port(vm),
            "source": "vnc",
        }
        if self._stamp:
            attrs["last_capture"] = dt_util.as_local(
                dt_util.utc_from_timestamp(self._stamp)).isoformat(timespec="seconds")
        return attrs

    # ------------------------------------------------------------------ 取图
    def _capture(self, host: str, port: int, password: str):
        """阻塞抓帧（在 executor 里跑）。"""
        return grab_png(host, port, password, timeout=VNC_SNAPSHOT_TIMEOUT)

    async def async_image(self) -> bytes | None:
        """返回虚拟机当前画面的 PNG 字节（按需抓帧 + 秒级节流）。"""
        return await self._async_capture()

    async def async_update(self) -> None:
        """被 ``homeassistant.update_entity`` 点到时抓一帧。

        前端（自定义卡片 / 屏幕墙 / 自动化）就是这么"叫醒"它的：
        这条路径**不碰 image_proxy 的 token 与签名**，所以最稳；
        ``should_poll = False`` 不变 —— 没人点就一帧都不抓（开机那一帧除外）。
        """
        await self._async_capture()

    async def _async_capture(self) -> bytes | None:
        """真正抓帧的地方（``async_image`` 与 ``async_update`` 共用）。

        抓到新画面才更新 ``image_last_updated``（前端据此重新取图），
        并顺手写一次状态 —— 否则"有没有画面"的判据（state）一直是空的。
        """
        vm = self._current()
        port = _vnc_port(vm)
        if not self.coordinator.last_update_success or vm.get("status") != 1 or not port:
            return None

        now = time.monotonic()
        if self._png is not None and now - self._stamp < VNC_SNAPSHOT_CACHE_SECONDS:
            return self._png

        host = vnc_host(self.coordinator)
        try:
            _width, _height, png = await self.hass.async_add_executor_job(
                self._capture, host, port, vm.get("vnc_pwd") or "")
        except VncSnapshotError as err:
            _LOGGER.warning("iKuai 虚拟机 %s 抓取屏幕画面失败：%s", vm.get("name"), err)
            return self._png
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("iKuai 虚拟机 %s 抓取屏幕画面异常：%s", vm.get("name"), err)
            return self._png

        self._stamp = now
        if png != self._png:
            # 画面真的变了才动时间戳：前端据此重新拉图，同时避免"刷一次变一次"的请求循环
            self._png = png
            self._attr_image_last_updated = dt_util.utcnow()
            self._invalidate_image_cache()
            self.async_write_ha_state()
        return png

    def _invalidate_image_cache(self) -> None:
        """让 image 组件的缓存属性重新计算（否则代理通路一直返回旧帧）。"""
        for key in ("image_last_updated", "content_type", "image_url"):
            self.__dict__.pop(key, None)
        self._cached_image = None

    # ------------------------------------------------------------------ 生命周期
    def _state_signature(self):
        """(state, 属性快照, 可用性) 签名；没变化就不重写状态。

        本类没有继承 :class:`IKuaiEntity`（它要的是 ``ImageEntity`` 而非
        ``CoordinatorEntity``），所以签名方法得自己带一份 —— 协调器每轮轮询都会
        调到下面这个监听器，虚拟机的屏幕画面绝大多数时候是静止的，没必要每轮写状态。
        """
        try:
            attrs = self.extra_state_attributes or {}
            payload = json.dumps(attrs, sort_keys=True, default=str)
            return (self.state, payload, self.available)
        except Exception:  # pylint: disable=broad-except
            return None

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update))
        # 注册表里的旧 ID（拼音）在这里迁移到固定英文 ID
        self.async_migrate_entity_id()

    @callback
    def _handle_coordinator_update(self) -> None:
        """协调器推数据：写状态；**刚开机**时主动抓一帧。

        为什么非抓不可：``should_poll = False`` 的实体自己不会去取图，
        而"有没有画面"的判据就是 state（``image_last_updated``）——
        一直是空的话，前端只知道"没有画面"，只能显示"等待截图"，
        而它又必须先知道"有画面"才肯去取图：**死锁**（必须人工点开设备页才破）。
        这里在**开机那一刻**抓一帧把判据填上（每台虚拟机每次开机一帧，开销可忽略）；
        之后谁来看谁走 ``async_image()`` / ``async_update()``。
        """
        running = self._is_running()
        if running and self._was_running is not True and self._png is None:
            self.hass.async_create_task(self._async_capture())
        self._was_running = running
        signature = self._state_signature()
        if signature is not None and signature == self._last_state_signature:
            return
        self._last_state_signature = signature
        self.async_write_ha_state()
