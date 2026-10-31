"""iKuai 传感器实体：基础状态 / 扩展诊断 / 日志中心 / 动态WAN线路。"""
import asyncio
import json
import logging
import time
from functools import partial
from urllib.parse import urlsplit

from homeassistant.helpers import device_registry as dr
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import callback
from homeassistant.helpers.event import async_call_later

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.helpers import entity_registry as er

from .api import api_stats, is_api_enabled, key_info
from .const import (
    API_CALL_PATH,
    API_KEY_LEN,
    API_KEY_SENSOR_NAME,
    API_KEY_SENSOR_SUFFIX,
    BINARY_INTO_SYSTEM_INFO,
    BINARY_SENSOR_TYPES,
    COORDINATOR,
    DIAL_INLINE_MAX,
    DOMAIN,
    SENSOR_TYPES,
    EXT_SENSOR_TYPES,
    LOG_CATEGORIES,
    RULE_COUNT_LABELS,
    TERMINAL_APP_MAX,
    TERMINAL_CONN_KEEP,
)
from .entity import (
    ENTITY_ID_PREFIX,
    DynamicEntityManager,
    IKuaiEntity,
    auth_device_info,
    auth_entity_slug,
    category,
    device_by_identifier,
    dhms,
    dhms_from_ts,
    docker_device_info,
    host_device_info,
    host_identifier,
    human_bytes,
    internal_key_slug,
    mask_cardid,
    stamp_ts,
    wan_internet_text,
    week_text,
)
from .logs import log_poll_enabled

_LOGGER = logging.getLogger(__name__)

_DEVICE_CLASS_MAP = {
    "temperature": SensorDeviceClass.TEMPERATURE,
    "data_rate": SensorDeviceClass.DATA_RATE,
    "duration": SensorDeviceClass.DURATION,
}
_STATE_CLASS_MAP = {
    "measurement": SensorStateClass.MEASUREMENT,
    "total_increasing": SensorStateClass.TOTAL_INCREASING,
}

# 实体属性中列表最多保留的行数（防止超过 recorder 16KB 属性上限）
ATTR_ROW_MAX = 500

# 日志统计 / 规则统计聚合实体的明细行数上限（与原「每类一个实体」时的行为一致）。
#
# 这两个实体的整包属性都声明了 ``_unrecorded_attributes``（不进数据库），所以
# 完全不受 recorder 单实体 16384 字节的属性上限约束；这里的行数上限只是让属性
# 规模与原实体保持一致，不再为体积做额外裁剪。
LOG_STATS_ROWS = ATTR_ROW_MAX
RULE_DETAIL_ROWS_MAX = ATTR_ROW_MAX

# 同类拨号/××× 线路组元数据：ext_key=ext 中行列表；uid=实体 unique_id 前缀（PPPoE 兼容旧版）
DIAL_GROUP_META = {
    "pppoe": {"ext_key": "wan_lines", "label": "多拨", "uid": "wanline"},
    "pptp": {"ext_key": "pptp_lines", "label": "PPTP", "uid": "pptp"},
    "l2tp": {"ext_key": "l2tp_lines", "label": "L2TP", "uid": "l2tp"},
}


def _cap_attrs(value, max_rows=ATTR_ROW_MAX):
    """递归截断属性中过长的列表，避免属性超过数据库大小限制。"""
    if isinstance(value, list):
        return value[:max_rows]
    if isinstance(value, dict):
        return {k: (_cap_attrs(v, max_rows) if isinstance(v, (list, dict)) else v)
                for k, v in value.items()}
    return value


# ============================================================================
# 聚合实体：把同类诊断项合并进一个实体（state = 主要指标，属性 = 其余同类项）
# ============================================================================
# 背景：基础 + 扩展传感器每个指标一个实体，光路由器设备下就有 180+ 个。这里按
# 「同类」收进聚合实体，取值仍复用 const 里各个传感器的 value 定义，所以新增一个
# 诊断项只需把它的 kind 加进对应聚合的成员列表。
#
# 刻意**不**参与聚合、仍独立成实体的项（有独立历史/统计价值，或被自动化引用）：
#   CPU 占用、CPU 温度、内存占用、系统上下行速率、每口 WAN 上下行速率、
#   上下行总量、在线/线路/告警类二元传感器、在线终端明细、应用流量三窗口、日志明细
#
# 已并入 system_info 的项（原本是独立实体）：
#   爱快云已绑定（原 binary_sensor.ikuai_cloud_bound）
#   固件有新版本（原 binary_sensor.ikuai_new_firmware，与 update.ikuai_firmware 重复）
#   远程访问地址（原 sensor.ikuai_web_url，取设备注册表 configuration_url）
INLINE_LOG_KEYS = frozenset({"url_log"})

# 已废弃：既不再独立创建，也没有并入聚合实体（与别的实体完全重复）
REMOVED_SENSOR_KINDS = ("ikuai_uptime", "ikuai_wan_ip", "ikuai_wan_uptime",
                        "ikuai_ap_online")
REMOVED_EXT_KINDS = ("ikuai_app_flow", "ikuai_docker_count")
# 已删除的聚合实体（key 即实体 ID 后缀）：wan_info 的 state/属性都被每口实体覆盖
REMOVED_AGGREGATE_KEYS = ("wan_info",)
# 已删除的二元传感器：
#   ikuai_wan_connected —— 只是每口「WAN x 状态」的 OR 汇总；
#   ikuai_iface_fault   —— 线路体检明细并入了 sensor.ikuai_interfaces
REMOVED_BINARY_KINDS = ("ikuai_wan_connected", "ikuai_iface_fault")


def _ext_value(kind, data, ext):
    """按 EXT_SENSOR_TYPES 里声明的 value 取单个扩展传感器的值。"""
    spec = EXT_SENSOR_TYPES.get(kind)
    if not spec:
        return None
    try:
        return spec["value"](data, ext)
    except Exception:  # pylint: disable=broad-except
        return None


def _binary_value(kind, data, ext):
    """取已并入聚合实体的二元量的值（复用 BINARY_SENSOR_TYPES 的定义）。"""
    spec = BINARY_SENSOR_TYPES.get(kind)
    if not spec or not spec.get("value"):
        return None
    try:
        return bool(spec["value"](data, ext))
    except Exception:  # pylint: disable=broad-except
        return None


def _agg_ext_values(kinds, data, ext):
    """批量取扩展传感器的值，属性名去掉 ikuai_ 前缀。"""
    return {kind[len("ikuai_"):]: _ext_value(kind, data, ext) for kind in kinds}


def _agg_sensor_values(kinds, data):
    """批量取基础传感器（data 顶层扁平键）的值，属性名去掉 ikuai_ 前缀。

    也用于 ``share_kinds``：这些指标既保留独立实体（要历史曲线），又把当前值
    复制一份进聚合实体的属性，方便一处看全。
    """
    return {kind[len("ikuai_"):]: data.get(kind) for kind in kinds}


def _agg_cpu_usage(data, ext):
    """系统信息主指标：总 CPU 占用（cpu_cores_usage[0]）。"""
    usage = ext.get("cpu_cores_usage") or []
    return _pct(usage[0]) if usage else None


def _agg_system_dynamic(data, ext):
    """系统信息动态部分：每核占用 + 每个磁盘分区使用率。"""
    usage = ext.get("cpu_cores_usage") or []
    cores = [_pct(v) for v in usage[1:]]
    partitions = {}
    for disk in ext.get("disk") or []:
        if not isinstance(disk, dict):
            continue
        for part in disk.get("partition") or []:
            if not isinstance(part, dict):
                continue
            mounted = part.get("mounted") or {}
            try:
                total = int(mounted.get("mt_total") or 0)
                used = int(mounted.get("mt_used") or 0)
            except (TypeError, ValueError):
                continue
            if not total:
                continue
            name = f"{disk.get('disk')}_{part.get('name')}"
            try:
                avail = int(mounted.get("mt_avail") or 0)
            except (TypeError, ValueError):
                avail = 0
            partitions[name] = {
                "usage": round(100 * used / total, 1),
                "mount_point": mounted.get("mt_name"),
                "fs_type": ((part.get("filesys") or {}).get("fs_type")),
                "model": disk.get("model"),
                "used": used,
                "avail": avail,
                "total": total,
                "total_text": human_bytes(total),
                "avail_text": human_bytes(avail),
            }
    # 注意 key 不能叫 cpu_cores：那是 ext_kinds 里「CPU核心数」的属性名
    return {"cpu_core_usage": cores,
            "cpu_core_freq": list(ext.get("cpu_freq_list") or []),
            "disk_partitions": partitions}


def _agg_lan_dynamic(data, ext):
    """LAN 动态部分：每个 LAN 口的 IPv4 地址与掩码。"""
    lans = {}
    for row in ext.get("snapshoot_lan") or []:
        if not isinstance(row, dict) or row.get("id") is None:
            continue
        lans[str(row.get("id"))] = {
            "interface": row.get("interface"),
            "ip": row.get("ip_addr") or None,
            "netmask": row.get("netmask"),
            "mac": row.get("mac"),
            "comment": row.get("comment"),
            "bandmode": row.get("bandmode"),
            "linkmode": row.get("linkmode"),
        }
    return {"lan_ips": lans}


def _agg_dial_state(data, ext):
    """拨号/VPN 主指标：PPPoE + PPTP + L2TP 在线条数之和。"""
    total = 0
    for kind in ("pppoe", "pptp", "l2tp"):
        try:
            total += int(data.get(f"ikuai_{kind}_count") or 0)
        except (TypeError, ValueError):
            pass
    return total


# 聚合实体规格：key 即实体 ID 后缀（sensor.ikuai_<key>）
AGGREGATE_SPECS: dict[str, dict] = {
    "system_info": {
        "name": "系统信息", "icon": "mdi:information-outline",
        "state_name": "CPU占用", "unit": "%", "state_class": "measurement",
        "state": _agg_cpu_usage,
        # 这几个指标保留独立实体（要历史曲线），同时把当前值复制进本实体属性
        "share_kinds": ("ikuai_cpu", "ikuai_cputemp", "ikuai_memory"),
        # 原实体的 attrs_key 明细块（行为审计记录、管理员列表、爱快云绑定明细、
        # 固件升级明细 —— 后两个原本是 binary_sensor 的属性）
        "attrs_blocks": ("audit", "webuser", "register", "upgrade"),
        # 云端绑定 / 固件有无新版本（原 binary_sensor，已并入本实体）
        "binary_kinds": BINARY_INTO_SYSTEM_INFO,
        # 远程访问地址（原 sensor.ikuai_web_url）
        "extras": ("device_url",),
        "ext_kinds": (
            "ikuai_uptime_seconds", "ikuai_cpu_cores", "ikuai_cpu_freq",
            "ikuai_memory_total", "ikuai_memory_available", "ikuai_disk_size",
            "ikuai_admin_count", "ikuai_backup_count", "ikuai_wuying_count",
            "ikuai_camera_count", "ikuai_qemu_count", "ikuai_cache_services",
            "ikuai_audit_storage_pct",
        ),
        "dynamic": _agg_system_dynamic,
    },
    # 原 "wan_info"（WAN 信息）已删除，因为它整个被别的实体覆盖：
    #   * state（在线口数）就是每口 binary_sensor.ikuai_wan_N_online 的计数；
    #   * 属性 upload/download/total_up/total_down 是 sensor.ikuai_upload 等独立
    #     实体的逐字复制（就是 share_kinds 那个"复制一份"）；
    #   * 属性 bands 与每口的「WAN x 状态」+ 两个速率实体完全重复。
    # 副作用：WAN IPv6 地址（ikuai_wan6_ip）不再被聚合，回到独立传感器。
    "lan_info": {
        "name": "LAN 信息", "icon": "mdi:lan",
        "state_name": "在线终端数", "unit": "个", "state_class": "measurement",
        "state": lambda d, e: d.get("ikuai_online_user"),
        "sensor_kinds": ("ikuai_online_user", "ikuai_connect_num", "ikuai_lan6_ip"),
        "ext_kinds": (
            "ikuai_route_v4", "ikuai_route_v6", "ikuai_arp_total", "ikuai_arp_bound",
            "ikuai_ipv6_neighbor", "ikuai_dhcp_leases", "ikuai_dhcp6_leases",
            "ikuai_dhcp_servers", "ikuai_dhcp_static", "ikuai_dhcp_acl_mac",
            "ikuai_dhcp6_acl_mac", "ikuai_online_user_v6", "ikuai_ap_total",
            "ikuai_ap_clients",
        ),
        # 原实体的 attrs_key 明细块（AP 列表、DHCP 服务器列表）
        "attrs_blocks": ("ac_list", "dhcp_servers"),
        "dynamic": _agg_lan_dynamic,
    },
    "dial_info": {
        "name": "拨号/VPN", "icon": "mdi:account-network",
        "state_name": "在线线路数", "unit": "个", "state_class": "measurement",
        "state": _agg_dial_state,
        "sensor_kinds": ("ikuai_pppoe_count", "ikuai_pppoe_fail_count",
                         "ikuai_pptp_count", "ikuai_pptp_fail_count",
                         "ikuai_l2tp_count", "ikuai_l2tp_fail_count"),
        "ext_kinds": ("ikuai_pppoe_total", "ikuai_pptp_total", "ikuai_l2tp_total",
                      "ikuai_ppp_online"),
        # 多拨线路明细（原「线路总数」传感器的 attrs 块）
        "attrs_blocks": ("wan_lines",),
    },
    "alarm_info": {
        "name": "告警与消息", "icon": "mdi:bell-alert",
        # 归入诊断区（与「规则统计」等诊断类放一起）
        "entity_category": "diagnostic",
        "state_name": "未读告警", "unit": "条", "state_class": "measurement",
        "state": lambda d, e: _ext_value("ikuai_alarms_unread", d, e),
        "ext_kinds": ("ikuai_alarms_unread", "ikuai_messages_unread",
                      "ikuai_iface_fail"),
        # 告警 / 消息明细（原「未读告警」传感器的 attrs 块）
        "attrs_blocks": ("alarms", "messages"),
    },
}

AGGREGATED_SENSOR_KINDS = frozenset(
    kind for spec in AGGREGATE_SPECS.values() for kind in spec.get("sensor_kinds", ()))
AGGREGATED_EXT_KINDS = frozenset(
    kind for spec in AGGREGATE_SPECS.values() for kind in spec.get("ext_kinds", ()))


def _agg_attr_names(spec):
    """某个聚合实体会暴露的全部属性名（键名规则与 extra_state_attributes 一致）。"""
    names = {"state_label", "querytime",
             "cpu_core_usage", "cpu_core_freq", "disk_partitions",
             "lan_ips"}
    for kind in list(spec.get("sensor_kinds", ())) + list(spec.get("share_kinds", ())):
        short = kind[len("ikuai_"):]
        names.add(short)
        names.add(f"{short}_attrs")
    names |= {kind[len("ikuai_"):] for kind in spec.get("ext_kinds", ())}
    names |= {kind[len("ikuai_"):] for kind in spec.get("binary_kinds", ())}
    for key in spec.get("attrs_blocks", ()):
        names.add(key)
        names.add(f"{key}_rows")
    if "device_url" in spec.get("extras", ()):
        names |= {"web_url", "web_url_full"}
    return names


# 聚合实体的整包属性都是「当前快照」（同类指标的集合），没有落库价值：
# 声明为不写数据库后，既不会撞 recorder 单实体 16384 字节的属性上限，也不会
# 让数据库随每次轮询膨胀；状态机、模板、前端仍照常可读。state 照常进历史。
AGG_UNRECORDED_ATTRS = frozenset(
    name for spec in AGGREGATE_SPECS.values() for name in _agg_attr_names(spec))


async def _async_cleanup_dial_orphans(hass, entry, coordinator):
    """聚合模式下清理注册表残留的旧单线传感器。

    DynamicEntityManager 只移除本进程创建过的实体；升级前注册的单线传感器
    在聚合模式（返回空集合）下会成为注册表孤儿，需要按 unique_id 显式清除。
    """
    for _ in range(60):
        data = coordinator.data
        if data and (data.get("ext") or {}):
            break
        await asyncio.sleep(2)
    data = coordinator.data
    if not data:
        return
    ext = data.get("ext") or {}
    host = coordinator.host
    aggregated = [meta["uid"] for kind, meta in DIAL_GROUP_META.items()
                  if len(ext.get(meta["ext_key"]) or []) > DIAL_INLINE_MAX]
    if not aggregated:
        return

    registry = er.async_get(hass)
    removed = 0
    for reg_entry in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        if reg_entry.domain != "sensor":
            continue
        uid = reg_entry.unique_id
        for kind_uid in aggregated:
            for field in ("online", "uptime"):
                prefix = f"{DOMAIN}_{kind_uid}_{field}_"
                tail = f"_{host}"
                if uid.startswith(prefix) and uid.endswith(tail) \
                        and uid[len(prefix):-len(tail)]:
                    registry.async_remove(reg_entry.entity_id)
                    removed += 1
                    break
    if removed:
        _LOGGER.info("拨号线路聚合：清理 %d 个残留单线传感器", removed)


def _removed_unique_ids(host):
    """合并后本集成不再创建的实体 unique_id（精确匹配，避免误伤保留项）。"""
    ids = {f"{DOMAIN}_{kind}_{host}"
           for kind in list(AGGREGATED_SENSOR_KINDS) + list(AGGREGATED_EXT_KINDS)
           + list(REMOVED_SENSOR_KINDS) + list(REMOVED_EXT_KINDS)}
    # 已整体删除的聚合实体与二元传感器（它们的注册表条目要清掉，否则会留下
    # 一批「已恢复 / 不可用」的幽灵实体）
    ids |= {f"{DOMAIN}_{key}_{host}" for key in REMOVED_AGGREGATE_KEYS}
    ids |= {f"{DOMAIN}_{kind}_{host}" for kind in REMOVED_BINARY_KINDS}
    ids |= {f"{DOMAIN}_log_{cat['key']}_{host}" for cat in LOG_CATEGORIES
            if cat["key"] not in INLINE_LOG_KEYS}
    ids |= {f"{DOMAIN}_rulecnt_{key}_{host}" for key in RULE_COUNT_LABELS}
    # 「爱快云已绑定」（binary）「固件有新版本」（binary）与「远程访问地址」（sensor）
    # 都已并入 system_info 聚合实体
    ids |= {f"{DOMAIN}_{kind}_{host}" for kind in BINARY_INTO_SYSTEM_INFO}
    ids.add(f"{DOMAIN}_weburl_{host}")
    return ids


def _removed_unique_prefixes():
    """数量不定的已合并 / 已删除实体（每核 / 每分区 / 每 LAN 口 / 每 WAN 口 / …）。"""
    return (f"{DOMAIN}_cpucore_", f"{DOMAIN}_diskpart_", f"{DOMAIN}_lanip_",
            f"{DOMAIN}_wanband_ip_", f"{DOMAIN}_wanband_uptime_",
            f"{DOMAIN}_vmconsole_",
            # 每口「网口 xxx 链路」已删除：信息全部并入 sensor.ikuai_interfaces
            f"{DOMAIN}_ethlink_")


# 从设备主页改到「诊断」区的实体：HA 用注册表里记下的类别显示已注册实体，代码里
# 新声明的 entity_category 不保证立刻覆盖，所以加载时显式同步一次（幂等）。
MOVED_TO_DIAGNOSTIC = (
    f"binary_sensor.{ENTITY_ID_PREFIX}alarm",      # 未读系统告警
    f"sensor.{ENTITY_ID_PREFIX}alarm_info",        # 告警与消息
    f"sensor.{ENTITY_ID_PREFIX}log_stats",         # 日志统计
    f"sensor.{ENTITY_ID_PREFIX}rule_stats",         # 规则统计（诊断类统计）
)


def _sync_diagnostic_category(registry) -> int:
    """把上述实体在注册表里的类别改成 diagnostic（已经是对的就跳过）。"""
    moved = 0
    for entity_id in MOVED_TO_DIAGNOSTIC:
        try:
            reg_entry = registry.async_get(entity_id)
        except Exception:  # pylint: disable=broad-except
            continue
        if reg_entry is None or reg_entry.entity_category == "diagnostic":
            continue
        try:
            registry.async_update_entity(entity_id, entity_category="diagnostic")
        except TypeError:
            # 老版本 HA 的 async_update_entity 不支持改 entity_category
            return moved
        except Exception:  # pylint: disable=broad-except
            continue
        moved += 1
    return moved


# 从「诊断」区移回普通区的实体：与 MOVED_TO_DIAGNOSTIC 同理 —— HA 页面用的是
# 注册表里记下的类别，而**默认禁用的实体根本不会被创建**（走不到实体注册时那次
# 同步），所以这里也显式清一次（幂等）。
MOVED_OUT_OF_DIAGNOSTIC = (
    f"sensor.{ENTITY_ID_PREFIX}interfaces",          # 接口
    f"sensor.{ENTITY_ID_PREFIX}hardware",            # 硬件信息
    f"sensor.{ENTITY_ID_PREFIX}auth_server",         # 本地认证服务
    f"sensor.{ENTITY_ID_PREFIX}api_key",             # API 密钥
    f"sensor.{ENTITY_ID_PREFIX}app_flow_windows",    # 流量分布
    f"sensor.{ENTITY_ID_PREFIX}wan6_ip",             # WAN IPv6地址
    f"sensor.{ENTITY_ID_PREFIX}iface_stream",        # 接口实时流量
    f"sensor.{ENTITY_ID_PREFIX}lan_hosts",           # 在线终端列表
)


def _sync_plain_category(registry) -> int:
    """把上述实体在注册表里的类别清回「无」（已不是诊断类的就跳过）。"""
    moved = 0
    for entity_id in MOVED_OUT_OF_DIAGNOSTIC:
        try:
            reg_entry = registry.async_get(entity_id)
        except Exception:  # pylint: disable=broad-except
            continue
        if reg_entry is None or not reg_entry.entity_category:
            continue
        try:
            registry.async_update_entity(entity_id, entity_category=None)
        except TypeError:
            # 老版本 HA 的 async_update_entity 不支持改 entity_category
            return moved
        except Exception:  # pylint: disable=broad-except
            continue
        moved += 1
    return moved


async def _async_cleanup_merged_entities(hass, entry, coordinator):
    """清理注册表：已并入聚合实体的条目，以及 ID 过时且仍禁用的旧条目。

    第一部分：合并后本集成不再创建这些实体，但注册表条目会保留（表现为一批
    「已恢复」的不可用实体），所以加载时显式清理一次。带 ``_2`` 兜底后缀的 ID
    同样按 unique_id 命中，不受实体 ID 形态影响。

    第二部分：早期版本把某些 kind 默认禁用，注册表按**当时的拼音 ID** 登记且
    ``disabled_by=integration``；后来代码把 ID 规范成英文（或改成默认启用），
    但禁用实体根本不会被创建，``async_migrate_entity_id`` 也就没机会跑，
    于是那条旧记录既占着注册表又挡住了新实体（例：规则统计曾以
    ``sensor.ikuai_gong_neng_gui_ze_tong_ji`` 禁用登记，导致
    ``sensor.ikuai_rule_stats`` 建不出来）。这类条目没有状态、没有历史，
    直接删掉让集成按固定 ID 重新注册。
    """
    host = getattr(coordinator, "host", "")
    exact = _removed_unique_ids(host)
    prefixes = _removed_unique_prefixes()
    try:
        registry = er.async_get(hass)
    except Exception:  # pylint: disable=broad-except
        return
    removed = 0
    stale_disabled = 0
    tail = f"_{host}"
    for reg_entry in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
        uid = reg_entry.unique_id or ""
        if uid in exact or uid.startswith(prefixes):
            registry.async_remove(reg_entry.entity_id)
            removed += 1
            continue
        if not reg_entry.disabled_by:
            continue
        # 只处理「静态 kind」（unique_id 形如 ikuai_ikuai_xxx_<host>）
        if not (uid.startswith(f"{DOMAIN}_{DOMAIN}_") and uid.endswith(tail)):
            continue
        kind = uid[len(DOMAIN) + 1:-len(tail)]
        domain = reg_entry.entity_id.split(".", 1)[0]
        expect = f"{domain}.{ENTITY_ID_PREFIX}{kind[len(DOMAIN) + 1:]}"
        if reg_entry.entity_id != expect:
            registry.async_remove(reg_entry.entity_id)
            stale_disabled += 1
    if removed:
        _LOGGER.info("iKuai 实体合并：清理 %d 个已聚合/废弃的实体条目", removed)
    if stale_disabled:
        _LOGGER.info(
            "iKuai 实体 ID 规范化：清理 %d 个 ID 过时且仍禁用的旧条目"
            "（它们从未创建过实体，启用时会以固定英文 ID 出现）", stale_disabled)
    moved = _sync_diagnostic_category(registry)
    if moved:
        _LOGGER.info("iKuai 实体分类：%d 个实体已归入「诊断」区", moved)
    plain = _sync_plain_category(registry)
    if plain:
        _LOGGER.info("iKuai 实体分类：%d 个实体已移出「诊断」区", plain)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up iKuai sensor entities from a config entry."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id][COORDINATOR]

    # 先清掉注册表里的历史残留（已并入聚合实体的条目、ID 过时且仍禁用的旧条目）。
    # 必须赶在下面建实体之前，这样本次就能直接以固定英文 ID 注册，不必再来一次
    # 重载（重载会让集成短时间内看起来「不可用」，也会让侧边栏面板闪一下）。
    await _async_cleanup_merged_entities(hass, config_entry, coordinator)

    # 独立传感器：跳过已并入聚合实体的 kind（成员见 AGGREGATE_SPECS）
    sensors = [IKUAISensor(kind, coordinator) for kind in SENSOR_TYPES
               if kind not in AGGREGATED_SENSOR_KINDS]
    sensors += [IKuaiRuleStatsSensor(kind, coordinator)
                if kind == "ikuai_rule_stats" else IKUAIExtSensor(kind, coordinator)
                for kind in EXT_SENSOR_TYPES if kind not in AGGREGATED_EXT_KINDS]
    # 日志：明细型的「网址浏览记录」单独保留（前端卡片读它的 records 属性按 MAC
    # 过滤展示），其余各类的条数与最近一条并入日志统计聚合实体
    sensors += [IKUAILogSensor(cat, coordinator) for cat in LOG_CATEGORIES
                if cat["key"] in INLINE_LOG_KEYS]
    # 按类别聚合的实体：系统信息 / WAN / LAN / 拨号 / 告警 / 日志统计
    sensors += [IKuaiAggregateSensor(coordinator, key) for key in AGGREGATE_SPECS]
    sensors.append(IKuaiLogStatsSensor(coordinator))
    sensors.append(IKuaiLanHostsSensor(coordinator))
    # 接口总览（物理口 ↔ 逻辑口 ↔ 地址/速率/线路检测；state = 网络是否正常）
    sensors.append(IKuaiInterfacesSensor(coordinator))
    # 硬件信息（CPU/主板/内存/硬盘/PCI 清单；数据来自已在抓的 hardwareinfo）
    sensors.append(IKuaiHardwareSensor(coordinator))
    # 本地认证 / VPN 服务端（PPPoE/PPTP/L2TP/OpenVPN）启停与参数
    sensors.append(IKuaiAuthServerSensor(coordinator))
    # DHCP 服务：服务端配置 + 静态分配 + 动态租约 + MAC 黑白名单
    sensors.append(IKuaiDhcpSensor(coordinator))
    # 透传 API 的密钥（整点自动换新；key 在属性里，不写数据库）
    sensors.append(IKuaiApiKeySensor(coordinator))
    # 全实体清单：按设备分类汇总本集成所有实体（id / 名称 / 状态值）
    sensors.append(IKuaiAllEntitiesSensor(hass, config_entry, coordinator))
    async_add_entities(sensors, False)

    # 物理 WAN 线路动态传感器：只保留每口的上下行速率（需要历史曲线，且不带属性）。
    # 每口的 IP / 网关 / DNS / MAC / 入网方式 / 上线时长只在该口的
    # binary_sensor.ikuai_wan_N_online（「WAN x 状态」）上出现一次。
    def band_items(data):
        bands = (data.get("ext") or {}).get("wan_bands") or []
        return {str(b.get("id")): b for b in bands if b.get("id") is not None}

    for field in ("upload", "download"):
        DynamicEntityManager(
            coordinator, async_add_entities,
            partial(IKuaiWanBandSensor, field=field), band_items)

    # 规则计数：37 张规则表已合并到 sensor.ikuai_rule_stats
    # （state=全部规则条数之和，属性 counts=每张表的条数），不再逐表建实体。

    # 拨号/××× 线路组：条数 <= DIAL_INLINE_MAX 时每条独立传感器（在线/上线时长），
    # 超过则整合为一个汇总实体（state=在线数量，每条明细全部进属性）
    for kind, meta in DIAL_GROUP_META.items():
        ext_key = meta["ext_key"]

        def per_line_items(data, _key=ext_key):
            rows = (data.get("ext") or {}).get(_key) or []
            if len(rows) > DIAL_INLINE_MAX:
                return {}
            return {f"{r.get('id')}:{field}": r for r in rows
                    if r.get("id") is not None
                    for field in ("online", "uptime")}

        def group_items(data, _key=ext_key, _kind=kind):
            rows = (data.get("ext") or {}).get(_key) or []
            return {_kind: rows} if len(rows) > DIAL_INLINE_MAX else {}

        DynamicEntityManager(
            coordinator, async_add_entities,
            partial(IKuaiDialLineSensor, kind=kind), per_line_items)
        DynamicEntityManager(
            coordinator, async_add_entities,
            partial(IKuaiDialGroupSensor, kind=kind), group_items)

    # 聚合模式下清除升级前残留的单线传感器注册表条目
    hass.async_create_task(
        _async_cleanup_dial_orphans(hass, config_entry, coordinator))

    # 每台虚拟机的「远程控制台」传感器已并入各自的电源开关
    # （switch.ikuai_vm_<名>_power，控制台独有字段见 switch._console_state）

    # 每个认证账号一个「账号信息」传感器（与同账号的「在线情况」二元传感器共用
    # 一台「认证账号」设备）：账号的详细信息就是它的状态属性
    def auth_items(data):
        rows = (data.get("ext") or {}).get("auth_accounts") or []
        return {str(r.get("key")): r for r in rows if r.get("key")}

    DynamicEntityManager(
        coordinator, async_add_entities, IKuaiAuthAccountInfo, auth_items)

    # 每核 CPU 占用 + 磁盘分区使用率：已合并到 sensor.ikuai_system_info 的
    # cpu_cores / disk_partitions 属性（不再每核、每分区各建一个实体）。
    # 每个 LAN 口 IPv4 地址：已合并到 sensor.ikuai_lan_info 的 lan_ips 属性。

    # 每台 AP 一个状态传感器（没有 AP 时不创建任何实体）
    def ap_items(data):
        rows = (data.get("ext") or {}).get("ac_list") or []
        return {str(r.get("id")): r for r in rows if r.get("id") is not None}

    DynamicEntityManager(
        coordinator, async_add_entities, IKuaiApSensor, ap_items)


class IKUAISensor(IKuaiEntity, SensorEntity):
    """历史扁平键传感器（保持 unique_id / 行为兼容）。"""

    def __init__(self, kind, coordinator):
        super().__init__(coordinator)
        self.kind = kind
        spec = SENSOR_TYPES[kind]
        self._attr_icon = spec.get("icon")
        if spec.get("device_class"):
            self._attr_device_class = _DEVICE_CLASS_MAP.get(spec["device_class"])
        if spec.get("state_class"):
            self._attr_state_class = _STATE_CLASS_MAP.get(spec["state_class"])
        self._attr_entity_category = category(spec.get("entity_category"))
        if spec.get("enabled_default") is False:
            self._attr_entity_registry_enabled_default = False
        # 固定英文实体 ID：sensor.ikuai_cpu / sensor.ikuai_online_user ...
        self._setup_forced_entity_id("sensor", internal_key_slug(kind))

    @property
    def name(self):
        return SENSOR_TYPES[self.kind]["name"]

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.kind}_{self.coordinator.host}"

    @property
    def native_unit_of_measurement(self):
        return SENSOR_TYPES[self.kind].get("unit_of_measurement")

    @property
    def native_value(self):
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.kind)

    # 兼容旧版自定义卡片读取 state / unit_of_measurement
    @property
    def state(self):
        return self.native_value

    @property
    def unit_of_measurement(self):
        return self.native_unit_of_measurement

    @property
    def extra_state_attributes(self):
        attrs = {}
        data = self.coordinator.data
        if data and data.get(self.kind + "_attrs"):
            attrs = data[self.kind + "_attrs"]
        if isinstance(attrs, dict):
            attrs = dict(attrs)
        else:
            attrs = {"data": attrs}
        attrs["querytime"] = (data or {}).get("querytime", "Unknown")
        return attrs


class IKUAIExtSensor(IKuaiEntity, SensorEntity):
    """扩展数据传感器，取值与附加属性均由 const 字典声明。"""

    def __init__(self, kind, coordinator):
        super().__init__(coordinator)
        self.kind = kind
        spec = EXT_SENSOR_TYPES[kind]
        self._spec = spec
        self._attr_icon = spec.get("icon")
        if spec.get("device_class"):
            self._attr_device_class = _DEVICE_CLASS_MAP.get(spec["device_class"])
        if spec.get("state_class"):
            self._attr_state_class = _STATE_CLASS_MAP.get(spec["state_class"])
        self._attr_entity_category = category(spec.get("entity_category"))
        if spec.get("enabled_default") is False:
            self._attr_entity_registry_enabled_default = False
        # 固定英文实体 ID：sensor.ikuai_arp_total / sensor.ikuai_docker_count ...
        self._setup_forced_entity_id("sensor", internal_key_slug(kind))

    @property
    def name(self):
        return self._spec["name"]

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.kind}_{self.coordinator.host}"

    @property
    def device_info(self):
        """Docker 相关汇总挂 Docker 设备，其余挂爱快主机设备。"""
        if self.kind.startswith("ikuai_docker"):
            return docker_device_info(self.coordinator)
        return host_device_info(self.coordinator)

    @property
    def native_unit_of_measurement(self):
        return self._spec.get("unit_of_measurement")

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        ext = data.get("ext") or {}
        try:
            value = self._spec["value"](data, ext)
        except Exception:
            return None
        if isinstance(value, float):
            return round(value, 2)
        return value

    @property
    def state(self):
        return self.native_value

    @property
    def unit_of_measurement(self):
        return self.native_unit_of_measurement

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        ext = data.get("ext") or {}
        attrs = {"querytime": data.get("querytime", "Unknown")}
        attr_key = self._spec.get("attrs_key")
        if attr_key:
            value = ext.get(attr_key)
            if isinstance(value, list):
                attrs["entries"] = _cap_attrs(value)
                attrs["count"] = len(value)
                if len(value) > ATTR_ROW_MAX:
                    attrs["truncated"] = True
            elif value is not None:
                attrs[attr_key] = _cap_attrs(value)
        return attrs


class IKuaiRuleStatsSensor(IKUAIExtSensor):
    """规则统计聚合实体（原 37 个 ``sensor.ikuai_rule_<表>`` 的合并体）。

    属性：

    * ``counts``  —— 每张规则表的条数（键与 ext.counts 一致）；
    * ``details`` —— 每张规则表的明细行（每表最多 ``RULE_DETAIL_ROWS_MAX`` 行，
      字段与原实体的 ``entries`` 一致）；没有记录的表是空列表，结构保持完整；
    * ``details_truncated`` —— 有表的明细行数被上限截断过。

    整包明细声明 ``_unrecorded_attributes``（不进数据库）：37 张表的明细远超
    recorder 单实体 16384 字节的属性上限，而这些明细属于「当前快照」，没有落库
    价值 —— 状态机、模板、前端仍照常可读。state（总条数）照常进历史。
    """

    _unrecorded_attributes = frozenset(
        {"counts", "details", "details_tables", "details_truncated"})

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        ext = data.get("ext") or {}
        attrs = {"querytime": data.get("querytime", "Unknown")}
        counts = ext.get("counts") or {}
        attrs["counts"] = counts
        rows_by_key = ext.get("rule_rows") or {}
        details = {}
        truncated = False
        # counts 与 rule_rows 的键取并集并保持顺序：有计数的表一定有明细键
        # （没有记录的表是空列表），有明细的表也不会漏
        for key in dict.fromkeys([*counts, *rows_by_key]):
            rows = rows_by_key.get(key) or []
            details[key] = _cap_attrs(rows, RULE_DETAIL_ROWS_MAX)
            if len(rows) > RULE_DETAIL_ROWS_MAX:
                truncated = True
        if details:
            attrs["details"] = details
            attrs["details_tables"] = len(details)
        if truncated:
            attrs["details_truncated"] = True
        return attrs


class IKuaiAggregateSensor(IKuaiEntity, SensorEntity):
    """同类诊断项的聚合实体：state = 该组主要指标，属性 = 其余同类指标。

    成员清单由 ``AGGREGATE_SPECS`` 声明，取值复用 const 中各个传感器的 value
    定义；「数量不定」的部分（每核占用、每个磁盘分区、每口 WAN、每个 LAN 口）
    由 dynamic 回调生成结构化属性。

    state 仍带 ``state_class``，所以主要指标（CPU 占用 / 在线终端数 / 规则总条数
    等）依旧有长期统计与历史曲线；被收入属性的指标则不再各自写历史。

    整包属性声明 ``_unrecorded_attributes``（不进数据库）：聚合后的属性是
    「当前快照」，没有落库价值，同时也不再受 recorder 单实体 16384 字节的
    属性上限约束。
    """

    _unrecorded_attributes = AGG_UNRECORDED_ATTRS

    def __init__(self, coordinator, agg_key):
        super().__init__(coordinator)
        self.agg_key = agg_key
        spec = AGGREGATE_SPECS[agg_key]
        self._spec = spec
        self._attr_icon = spec.get("icon")
        self._attr_entity_category = category(spec.get("entity_category"))
        # 固定英文实体 ID：sensor.ikuai_system_info / sensor.ikuai_lan_info ...
        # 目标 ID 若被别的集成占用（如旧的 ikuai_router），基类会兜底加 _2
        self._setup_forced_entity_id("sensor", agg_key)

    @property
    def name(self):
        return self._spec["name"]

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self.agg_key}_{self.coordinator.host}"

    @property
    def native_unit_of_measurement(self):
        return self._spec.get("unit")

    @property
    def state_class(self):
        return _STATE_CLASS_MAP.get(self._spec.get("state_class"))

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        try:
            return self._spec["state"](data, data.get("ext") or {})
        except Exception:  # pylint: disable=broad-except
            return None

    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        ext = data.get("ext") or {}
        attrs = {
            "state_label": self._spec.get("state_name"),
            "querytime": data.get("querytime"),
        }
        kinds = tuple(self._spec.get("sensor_kinds", ())) \
            + tuple(self._spec.get("share_kinds", ()))
        attrs.update(_agg_sensor_values(kinds, data))
        for kind in kinds:
            # 个别基础传感器自带明细块（如 ikuai_online_user_attrs / ikuai_memory_attrs）
            block = data.get(f"{kind}_attrs")
            if isinstance(block, dict) and block:
                attrs[f"{kind[len('ikuai_'):]}_attrs"] = _cap_attrs(block)
        attrs.update(_agg_ext_values(self._spec.get("ext_kinds", ()), data, ext))
        for key in self._spec.get("attrs_blocks", ()):
            # 原实体 attrs_key 指向的明细块（如审计记录 / 管理员列表 / AP 列表）；
            # 与上面的数值属性同名时加 _rows 后缀（例如 dhcp_servers 的条数 vs 明细）
            block = ext.get(key)
            if block in (None, "", [], {}):
                continue
            attrs[key if key not in attrs else f"{key}_rows"] = _cap_attrs(block)
        dynamic = self._spec.get("dynamic")
        if dynamic:
            attrs.update(dynamic(data, ext) or {})
        for kind in self._spec.get("binary_kinds", ()):
            attrs[kind[len("ikuai_"):]] = _binary_value(kind, data, ext)
        if "device_url" in self._spec.get("extras", ()):
            base, full = self._device_url()
            attrs["web_url"] = base
            attrs["web_url_full"] = full
        return attrs

    def _device_url(self):
        """远程访问地址：取设备注册表的 ``configuration_url``（与设备页「访问」一致）。

        原为独立的 ``sensor.ikuai_web_url``。完整地址约 400 字符（含 authSig），
        超过 HA 状态 255 字符上限，所以这里同样拆开给：``web_url`` 是源地址
        （``scheme://host:port``），``web_url_full`` 是完整可点击地址。
        """
        hass = getattr(self, "hass", None)
        if hass is None:
            return None, None
        try:
            registry = dr.async_get(hass)
        except Exception:  # pylint: disable=broad-except
            return None, None
        if hasattr(registry, "__await__"):
            # 老版本 HA 的 async_get 是协程，属性取值处无法 await
            return None, None
        try:
            device = device_by_identifier(
                registry, (DOMAIN, self.coordinator.host),
                getattr(self.coordinator, "entry_id", None))
        except Exception:  # pylint: disable=broad-except
            device = None
        url = getattr(device, "configuration_url", None) if device else None
        if not url:
            return None, None
        base = None
        try:
            parsed = urlsplit(url)
            if parsed.scheme and parsed.netloc:
                base = f"{parsed.scheme}://{parsed.netloc}"
        except ValueError:
            base = None
        return base, url


class IKuaiLogStatsSensor(IKuaiEntity, SensorEntity):
    """日志统计聚合实体：state = 各类日志条数之和，属性 = 每类的条数与最近明细。

    属性 ``categories`` = ``{类别中文名: {count, updated, latest, entries}}``：

    * ``latest`` 是最近一条的关键字段（按 const 里声明的中文映射取名）；
    * ``entries`` 保留该类的最近 ``LOG_STATS_ROWS`` 条（与原「日志-XX」实体
      的 entries 行数一致）。

    整包属性声明 ``_unrecorded_attributes``（不进数据库）：既绕开 recorder 单
    实体 16384 字节的属性上限，也不会让数据库随每次日志轮询膨胀；状态机、模板、
    前端仍照常可读。明细型的「网址浏览记录」仍由 ``IKUAILogSensor`` 单独承载
    （前端卡片读它的 ``records`` 属性按 MAC 过滤展示访问历史）。
    """

    _unrecorded_attributes = frozenset({"categories"})
    # 归入诊断区（与「规则统计」等诊断类放一起）
    _attr_entity_category = category("diagnostic")

    @property
    def available(self) -> bool:
        """「采集日志」开关关闭时不再有日志数据 → 实体置为不可用。"""
        return super().available and log_poll_enabled(self.hass, self.entry_id)

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_icon = "mdi:script-text-outline"
        # 固定英文实体 ID：sensor.ikuai_log_stats
        self._setup_forced_entity_id("sensor", "log_stats")

    @property
    def name(self):
        return "日志统计"

    @property
    def unique_id(self):
        return f"{DOMAIN}_log_stats_{self.coordinator.host}"

    @property
    def native_unit_of_measurement(self):
        return "条"

    @staticmethod
    def _block_total(block):
        """条数：模块不提供总数（total 恒为 -1）时退回本次取到的行数。"""
        total = block.get("total")
        if isinstance(total, int) and total >= 0:
            return total
        return len(block.get("rows") or [])

    def _blocks(self):
        """参与 state 求和的日志分类（不含单独成实体的明细型日志）。"""
        logs = (self.coordinator.data or {}).get("logs") or {}
        return [(cat, logs.get(cat["key"]) or {}) for cat in LOG_CATEGORIES
                if cat["key"] not in INLINE_LOG_KEYS]

    @staticmethod
    def _latest_row(cat, block):
        """最近一条日志的关键字段（按 const 里的中文映射取名）。"""
        rows = block.get("rows") or []
        latest = {}
        if rows and isinstance(rows[0], dict):
            for key, label in (cat.get("fields") or {}).items():
                value = rows[0].get(key)
                if value not in ("", None):
                    latest[label] = value
        return latest

    @property
    def native_value(self):
        return sum(self._block_total(block) for _cat, block in self._blocks())

    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        logs = (self.coordinator.data or {}).get("logs") or {}
        categories = {}
        for cat in LOG_CATEGORIES:
            block = logs.get(cat["key"]) or {}
            rows = block.get("rows") or []
            inline = cat["key"] in INLINE_LOG_KEYS
            item = {
                "count": self._block_total(block),
                "updated": block.get("updated"),
                "latest": self._latest_row(cat, block),
            }
            if inline:
                # 明细型日志（网址浏览记录）有自己独立的实体（records 属性），
                # 这里只给条数与最近一条，不重复整包明细；也不计入 state 求和
                item["detailed"] = True
                if block.get("total_supported") is False:
                    item["total_supported"] = False
            else:
                # 其余分类的明细只在这里（每类最近 LOG_STATS_ROWS 条）
                item["entries"] = _cap_attrs(rows, LOG_STATS_ROWS)
                if len(rows) > LOG_STATS_ROWS:
                    item["truncated"] = True
            categories[cat["name"]] = item
        return {"categories": categories,
                "querytime": (self.coordinator.data or {}).get("querytime")}


class IKUAILogSensor(IKuaiEntity, SensorEntity):
    """分类日志诊断传感器：状态=总条数，属性=最近 N 条。"""

    def __init__(self, cat, coordinator):
        super().__init__(coordinator)
        self._cat = cat
        self.key = cat["key"]
        self._attr_entity_category = category("diagnostic")
        self._attr_icon = cat.get("icon", "mdi:file-document-outline")
        # 固定英文实体 ID：sensor.ikuai_log_system / sensor.ikuai_log_url_log ...
        self._setup_forced_entity_id("sensor", f"log_{self.key}")

    # records 整包明细不写数据库（条数按分类的 rows_keep 定，可能远超 16384 字节）；
    # 状态机、模板、前端卡片（点击客户端看访问历史）仍照常可读。
    _unrecorded_attributes = frozenset({"records"})

    @property
    def name(self):
        return f"日志-{self._cat['name']}"

    @property
    def unique_id(self):
        return f"{DOMAIN}_log_{self.key}_{self.coordinator.host}"

    @property
    def available(self):
        if not super().available:
            return False
        # 「采集日志」开关关闭时不再抓日志 → 该分类实体置为不可用
        if not log_poll_enabled(self.hass, self.entry_id):
            return False
        return self.key in ((self.coordinator.data or {}).get("logs") or {})

    @property
    def native_value(self):
        logs = (self.coordinator.data or {}).get("logs") or {}
        block = logs.get(self.key)
        return block.get("total") if block else 0

    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        logs = (self.coordinator.data or {}).get("logs") or {}
        block = logs.get(self.key) or {}
        rows = block.get("rows") or []
        attrs = {
            "category": self.key,
            "func_name": self._cat["func"],
            "updated": block.get("updated"),
            # 明细用 records 命名：与旧版实体和前端卡片（点客户端看访问历史）一致
            "records": rows,
            "shown": len(rows),
        }
        if block.get("total_supported") is False:
            # 该模块不提供总数（如 audit_url_log 的 total 恒为 -1）：
            # 状态值与 shown 都只是本次抓到的条数
            attrs["total_supported"] = False
        got = block.get("total")
        if isinstance(got, int) and got > len(rows):
            attrs["truncated"] = True
        return attrs


FIELD_META = {
    "ip":       ("IP地址", "mdi:ip-network-outline", None, None, None),
    "uptime":   ("运行秒数", "mdi:timer-outline", "s", "duration", "measurement"),
    "upload":   ("上行速率", "mdi:arrow-up-bold-box-outline", "MB/s", "data_rate", "measurement"),
    "download": ("下行速率", "mdi:arrow-down-bold-box-outline", "MB/s", "data_rate", "measurement"),
}


class IKuaiWanBandSensor(IKuaiEntity, SensorEntity):
    """单个物理 WAN 口的实时速率。

    只出速率数值（+ 历史曲线），**不挂任何状态属性**：该口的接口名 / IP / 网关 /
    DNS / MAC / 入网方式 / 是否默认线路 / 上线时长统一放在同编号的
    ``binary_sensor.ikuai_wan_<id>_online``（显示名「WAN x 状态」）上 —— 否则同一份
    网关/DNS 会在速率实体和状态实体里各存一份。
    """

    _attr_entity_category = None

    def __init__(self, coordinator, suffix, item, field):
        super().__init__(coordinator)
        self._suffix = suffix
        self.field = field
        self._band_name = item.get("name") or item.get("interface") or f"wan{suffix}"
        meta = FIELD_META[field]
        self._meta = meta
        self._attr_icon = meta[1]
        if meta[3]:
            self._attr_device_class = _DEVICE_CLASS_MAP.get(meta[3])
        if meta[4]:
            self._attr_state_class = _STATE_CLASS_MAP.get(meta[4])
        # 固定英文实体 ID：sensor.ikuai_wan_1_ip / sensor.ikuai_wan_1_download ...
        self._setup_forced_entity_id("sensor", f"wan_{suffix}_{field}")

    @property
    def name(self):
        label = self._meta[0]
        return f"WAN {self._band_name} {label}"

    @property
    def unique_id(self):
        return f"{DOMAIN}_wanband_{self.field}_{self._suffix}_{self.coordinator.host}"

    def _band(self):
        for b in (self.coordinator.data.get("ext") or {}).get("wan_bands") or []:
            if str(b.get("id")) == self._suffix:
                return b
        return {}

    def _stream(self):
        band = self._band()
        iface = band.get("name") or band.get("interface")
        for row in (self.coordinator.data.get("ext") or {}).get("iface_stream") or []:
            if row.get("interface") == iface:
                return row
        return {}

    @property
    def native_unit_of_measurement(self):
        return self._meta[2]

    @property
    def native_value(self):
        band = self._band()
        if self.field == "ip":
            return band.get("ip_addr") or band.get("pppoe_ip_addr") or None
        if self.field == "uptime":
            ts = band.get("updatetime") or band.get("pppoe_updatetime") or 0
            try:
                return max(0, int(time.time() - int(ts))) if int(ts) > 0 else 0
            except (ValueError, TypeError):
                return 0
        stream = self._stream()
        if self.field == "upload":
            return round(int(stream.get("upload", 0)) / 1024 / 1024, 3)
        if self.field == "download":
            return round(int(stream.get("download", 0)) / 1024 / 1024, 3)
        return None

    @property
    def state(self):
        return self.native_value


class IKuaiDialLineSensor(IKuaiEntity, SensorEntity):
    """单条拨号/××× 线路传感器（仅在该组线路数 <= DIAL_INLINE_MAX 时存在）。

    suffix 形如 "12:online" / "12:uptime"。
    """

    _attr_entity_category = None

    def __init__(self, coordinator, suffix, item, kind):
        super().__init__(coordinator)
        self.kind = kind
        self._meta = DIAL_GROUP_META[kind]
        line_id, self.field = suffix.split(":", 1)
        self._line_id = line_id
        self._item = item or {}
        if self.field == "uptime":
            self._attr_icon = "mdi:timer-outline"
            self._attr_device_class = SensorDeviceClass.DURATION
        else:
            self._attr_icon = "mdi:phone-in-talk-outline"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        # 固定英文实体 ID：sensor.ikuai_wanline_3_online / sensor.ikuai_ptpp_1_uptime ...
        self._setup_forced_entity_id(
            "sensor", f"{self._meta['uid']}_{line_id}_{self.field}")

    @property
    def name(self):
        return f"{self._meta['label']} {self._line_name()} " \
               f"{'在线' if self.field == 'online' else '上线时长'}"

    @property
    def unique_id(self):
        return f"{DOMAIN}_{self._meta['uid']}_{self.field}_{self._line_id}_" \
               f"{self.coordinator.host}"

    def _rows(self):
        return (self.coordinator.data.get("ext") or {}).get(self._meta["ext_key"]) or []

    def _line(self):
        for x in self._rows():
            if str(x.get("id")) == self._line_id:
                return x
        return self._item

    def _line_name(self, line=None):
        line = line or self._item
        return line.get("vlan_name") or line.get("name") \
            or line.get("interface") or f"line{self._line_id}"

    def _is_online(self, line):
        if self.kind == "pppoe":
            # PPPoE 以拿到 IP 且有拨号成功时间为准
            if line.get("vlan_internet", 2) == 2:
                return bool(line.get("pppoe_ip_addr")) and \
                    int(line.get("pppoe_updatetime") or 0) > 0
            return line.get("enabled") == "yes"
        # PPTP / L2TP 客户端
        return bool(line.get("ip_addr")) and bool(line.get("updatetime"))

    def _uptime_ts(self, line):
        return int(line.get("pppoe_updatetime" if self.kind == "pppoe"
                           else "updatetime") or 0)

    def update_item(self, item):
        self._item = item or {}

    @property
    def native_unit_of_measurement(self):
        return "s" if self.field == "uptime" else None

    @property
    def native_value(self):
        line = self._line()
        if self.field == "online":
            return 1 if self._is_online(line) else 0
        ts = self._uptime_ts(line)
        return max(0, int(time.time()) - ts) if ts > 0 else 0

    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        line = self._line()
        attrs = {
            "kind": self.kind,
            "line_id": self._line_id,
            "line_name": self._line_name(line),
            "enabled": line.get("enabled"),
            "online": self._is_online(line),
            "querytime": (self.coordinator.data or {}).get("querytime"),
        }
        if self.kind == "pppoe":
            attrs.update({
                "interface": line.get("interface"),
                "vlan_id": line.get("vlan_id"),
                "vlan_internet": line.get("vlan_internet"),
                "ip_addr": line.get("pppoe_ip_addr"),
                "gateway": line.get("pppoe_gateway"),
                "dns1": line.get("pppoe_dns1"),
                "dns2": line.get("pppoe_dns2"),
                "default_route": line.get("default_route"),
                "mtu": line.get("mtu"),
                "username": line.get("username"),
            })
        else:
            attrs.update({
                "server": line.get("server") or line.get("server_addr"),
                "ip_addr": line.get("ip_addr"),
                "username": line.get("username"),
                "comment": line.get("comment"),
            })
        return attrs


class IKuaiDialGroupSensor(IKuaiEntity, SensorEntity):
    """同类线路超过 DIAL_INLINE_MAX 条时的汇总实体：state=在线数量，明细全入属性。"""

    _attr_icon = "mdi:router-network"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, suffix, item, kind):
        super().__init__(coordinator)
        self.kind = kind
        self._meta = DIAL_GROUP_META[kind]
        # 固定英文实体 ID：sensor.ikuai_wanline_group / sensor.ikuai_pptp_group ...
        self._setup_forced_entity_id("sensor", f"{self._meta['uid']}_group")

    @property
    def name(self):
        return f"{self._meta['label']}线路汇总"

    @property
    def unique_id(self):
        return f"{DOMAIN}_dialgroup_{self.kind}_{self.coordinator.host}"

    def _rows(self):
        return (self.coordinator.data.get("ext") or {}).get(self._meta["ext_key"]) or []

    @staticmethod
    def _online(kind, line):
        if kind == "pppoe":
            if line.get("vlan_internet", 2) == 2:
                return bool(line.get("pppoe_ip_addr")) and \
                    int(line.get("pppoe_updatetime") or 0) > 0
            return line.get("enabled") == "yes"
        return bool(line.get("ip_addr")) and bool(line.get("updatetime"))

    def _brief(self, line):
        """精简为属性中的一行（避免完整行过多超出属性大小上限）。"""
        online = self._online(self.kind, line)
        ts = int(line.get("pppoe_updatetime" if self.kind == "pppoe"
                          else "updatetime") or 0)
        row = {
            "id": line.get("id"),
            "name": line.get("vlan_name") or line.get("name")
                    or line.get("interface"),
            "enabled": line.get("enabled"),
            "online": online,
            "uptime": max(0, int(time.time()) - ts) if ts > 0 else 0,
        }
        if self.kind == "pppoe":
            row.update({
                "interface": line.get("interface"),
                "vlan_id": line.get("vlan_id"),
                "vlan_internet": line.get("vlan_internet"),
                "ip_addr": line.get("pppoe_ip_addr"),
                "gateway": line.get("pppoe_gateway"),
                "default_route": line.get("default_route"),
                "username": line.get("username"),
            })
        else:
            row.update({
                "server": line.get("server") or line.get("server_addr"),
                "ip_addr": line.get("ip_addr"),
                "username": line.get("username"),
            })
        return row

    @property
    def native_unit_of_measurement(self):
        return "条"

    @property
    def native_value(self):
        rows = self._rows()
        return sum(1 for x in rows if self._online(self.kind, x))

    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        rows = self._rows()
        lines = [self._brief(x) for x in rows]
        online = sum(1 for x in lines if x["online"])
        attrs = {
            "kind": self.kind,
            "total": len(rows),
            "online": online,
            "offline": max(0, len(rows) - online),
            "threshold": DIAL_INLINE_MAX,
            "lines": _cap_attrs(lines),
            "querytime": (self.coordinator.data or {}).get("querytime"),
        }
        if len(lines) > ATTR_ROW_MAX:
            attrs["truncated"] = True
        return attrs


class IKuaiAllEntitiesSensor(IKuaiEntity, SensorEntity):
    """聚合实体：把本集成所有实体按设备分类汇总在一个实体里。

    * state = 实体总数；
    * 属性 ``groups`` = 分类列表，一类对应一个设备：
        - ``router`` 爱快路由（主机设备下的全部实体）
        - ``docker`` 全部 Docker 容器（共用一个 Docker 设备）
        - ``auth`` 全部认证账号（共用一个「认证账号」设备）
        - ``vm_<id>`` 每台虚拟机一个类（设备名形如 ``虚拟机 win7``）
        - 以后新增的设备类型（``<主机>::<kind>``）自动成类，无需改代码
      每类里有 ``key`` / ``name``（设备名）/ ``count`` / ``entities``，
      ``entities`` 每项是 ``{entity_id, name, state, icon}``。

    实体是按**设备标识**归类的：只要实体挂在带本集成标识（``<主机>::…``）的设备上，
    就会自动出现在这里（新增账号/虚拟机/Docker 后无需任何配置）。

    不写历史
    --------
    ``_unrecorded_attributes`` 把整包属性排除在 recorder 之外：实体状态机、模板、
    前端都照常能读到这些属性，但数据库里不会存这份大属性（也就完全绕开了
    recorder 16384 字节的单实体属性上限）。想连状态值本身也不进历史库，可在
    ``configuration.yaml`` 里加：

        recorder:
          exclude:
            entity_globs:
              - sensor.ikuai_all_entities
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:format-list-bulleted-square"
    # 整包属性不写数据库（状态机/模板仍然可读）
    _unrecorded_attributes = frozenset(
        {"total", "listed", "classes", "truncated", "omitted", "groups"})

    # 极端情况下的兜底上限（正常情况下属性不进数据库，不存在 recorder 限制）
    _ATTR_BUDGET = 60000
    # 状态变化后的合并刷新间隔（秒），避免每次状态变化都重写这个"大属性"实体
    _FLUSH_DELAY = 5.0

    def __init__(self, hass, entry, coordinator):
        super().__init__(coordinator)
        self._hass = hass
        self._entry_id = entry.entry_id
        self._attr_name = "所有实体"
        self._payload: dict | None = None
        self._ids: set[str] = set()
        self._pending = None
        # 固定英文实体 ID：sensor.ikuai_all_entities
        self._setup_forced_entity_id("sensor", "all_entities")

    @property
    def unique_id(self):
        return f"{DOMAIN}_all_entities_{self.coordinator.host}"

    # ------------------------------------------------------------ 分类与汇总
    def _classify(self, device):
        """按设备标识判断实体属于哪一类，**新增设备类型不用改这里**。

        本集成的设备标识统一是 ``<主机>::<类型>[::<子标识>]``：

            ``<主机>``                     → router（爱快主机，一台）
            ``<主机>::docker``             → docker（所有容器共用一台）
            ``<主机>::auth``               → auth（所有认证账号共用，一台）
            ``<主机>::vm::<id>``           → ``vm_<id>``（每台虚拟机一台）
            以后新增的 ``<主机>::<kind>[::<sub>]`` → ``<kind>`` / ``<kind>_<sub>``

        也就是说：实体一旦挂到本集成的某台设备上，就会被自动收进聚合实体，
        不需要为每种新设备写一条规则。
        """
        if device is None:
            return None
        identifiers = device.identifiers or set()
        if host_identifier(self.coordinator) in identifiers:
            return "router", (device.name_by_user or device.name or "iKuai")
        name = device.name_by_user or device.name or "iKuai"
        for domain, ident in identifiers:
            if domain != DOMAIN or "::" not in ident:
                continue
            rest = ident.split("::", 1)[1]
            if not rest:
                continue                      # 主机设备（已在上面处理）
            kind, _, sub = rest.partition("::")
            if kind == "vm":
                return (f"vm_{sub or '0'}",
                        device.name_by_user or device.name or f"虚拟机 {sub}")
            return (f"{kind}_{sub}" if sub else kind), name
        return None

    @staticmethod
    def _size(obj) -> int:
        return len(json.dumps(obj, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))

    def _build_payload(self) -> dict:
        try:
            registry = er.async_get(self._hass)
            dev_reg = dr.async_get(self._hass)
        except Exception:  # pylint: disable=broad-except
            return {"total": 0, "listed": 0, "classes": 0, "groups": []}

        groups: dict[str, dict] = {}
        ids: set[str] = set()
        for entry in er.async_entries_for_config_entry(registry, self._entry_id):
            if entry.disabled_by:
                # 禁用条目（enabled_default=False / 高风险默认关闭）在状态机里并不
                # 存在，列出来只会是一批 state=null 的噪音，还会虚增实体总数
                continue
            entity_id = entry.entity_id
            if entity_id == self.entity_id:
                continue  # 不把自己算进去（否则自己写状态会自激）
            device = dev_reg.async_get(entry.device_id) if entry.device_id else None
            classified = self._classify(device)
            if classified is None:
                continue
            key, device_name = classified
            state = self._hass.states.get(entity_id)
            attributes = state.attributes if state is not None else {}
            group = groups.setdefault(
                key, {"key": key, "name": device_name, "count": 0, "entities": []})
            group["entities"].append({
                "entity_id": entity_id,
                "name": (attributes.get("friendly_name")
                         or entry.name or entry.original_name or entity_id),
                "state": state.state if state is not None else None,
                # 图标优先级：当前状态里的 icon → 注册表里用户改过的 icon → 代码声明的原始 icon
                "icon": (attributes.get("icon")
                         or getattr(entry, "icon", None)
                         or getattr(entry, "original_icon", None)),
            })
            group["count"] += 1
            ids.add(entity_id)

        # 分类顺序：路由 → Docker → 认证账号 → 各虚拟机 → 以后新增的类型（按 key 排）
        rank = {"router": 0, "docker": 1, "auth": 2}
        ordered = sorted(
            groups.values(),
            key=lambda g: (rank.get(g["key"], 3), g["key"]))

        total = len(ids)
        # 先按"最终形态"建包（含 truncated/omitted/listed 三个标记），
        # 这样裁剪时算的就是最终体积
        payload = {"total": total, "listed": total, "classes": len(ordered),
                   "truncated": False, "omitted": 0, "groups": ordered}
        omitted = 0
        current = self._size(payload)
        while current > self._ATTR_BUDGET and any(g["entities"] for g in ordered):
            # 轮着从各类尾部裁，保证每类都还在，计数仍是真实的
            for group in ordered:
                if current <= self._ATTR_BUDGET:
                    break
                if not group["entities"]:
                    continue
                item = group["entities"].pop()
                current -= self._size(item) + 1
                group["count"] -= 1
                omitted += 1
        payload["listed"] = total - omitted
        if omitted:
            payload["truncated"] = True
            payload["omitted"] = omitted
            _LOGGER.debug(
                "iKuai 聚合实体属性超过 %d 字节，已裁剪 %d 个实体（total=%d）",
                self._ATTR_BUDGET, omitted, total)
        self._ids = ids
        return payload

    def _ensure_payload(self) -> dict:
        if self._payload is None:
            self._payload = self._build_payload()
        return self._payload

    @property
    def native_value(self):
        """状态值 = 本集成实体总数。"""
        return self._ensure_payload().get("total")


    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        return self._ensure_payload()

    # ------------------------------------------------------------ 刷新时机
    @callback
    def _handle_coordinator_update(self):
        self._payload = None
        # 聚合类实体属性体量大，没变化时不要重写（详见 IKuaiEntity 的说明）
        signature = self._state_signature()
        if signature is not None and signature == self._last_state_signature:
            return
        self._last_state_signature = signature
        self.async_write_ha_state()

    @callback
    def _handle_state_changed(self, event):
        """只关心本集成的实体；合并 5 秒刷新一次，避免大属性实体被反复写。"""
        entity_id = (event.data or {}).get("entity_id")
        if not entity_id or entity_id == self.entity_id or entity_id not in self._ids:
            return
        if self._pending is not None:
            return
        self._pending = async_call_later(
            self._hass, self._FLUSH_DELAY, self._flush_soon)

    @callback
    def _flush_soon(self, _now=None):
        self._pending = None
        self._payload = None
        self.async_write_ha_state()

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # 协调器刷新走基类注册的 _handle_coordinator_update（10 秒一次，顺带发现新增实体），
        # 这里再订阅实体状态变化，5 秒合并刷新一次
        self.async_on_remove(
            self._hass.bus.async_listen(EVENT_STATE_CHANGED, self._handle_state_changed))
        self._payload = self._build_payload()   # 首次先把 _ids 建出来
        # 注册表里的旧 ID（拼音）在这里迁移到固定英文 ID
        self.async_migrate_entity_id()


class IKuaiAuthAccountInfo(IKuaiEntity, SensorEntity):
    """认证账号信息（每账号一个实体，与「<账号> 在线」同属一台「认证账号」设备）。

    * state：启用 / 停用（账号 ``enabled`` 的中文视图，缺数据时为「未知」）；
    * attributes：**该账号的详细信息** —— 用户名、姓名、电话、备注、有效期（到期
      时间戳 + 可读文本 + 是否已过期）、共享数、IP 类型、绑定接口/VLAN、创建与
      最近上下线时间、限速值等配置项。

    安全：口令类字段在抓取层就被剔除（连 ``***`` 都不会下发），身份证号只留后 4 位。
    """

    _attr_icon = "mdi:account-details"

    # 账号配置字段白名单（顺序即展示顺序）
    _DETAIL_FIELDS = ("username", "name", "phone", "comment", "enabled", "ppptype",
                      "pppname", "ip_type", "share", "address", "bind_ifname",
                      "bind_vlanid", "auto_mac", "auto_vlanid", "proxy_username",
                      "pppoev6_wan", "duration", "create_time", "expires", "packages",
                      "upload", "download")
    # 需要额外给一份可读文本的时间戳字段
    _TIME_FIELDS = ("start_time", "create_time", "last_conntime", "last_offtime")

    def __init__(self, coordinator, suffix, item):
        super().__init__(coordinator)
        self._suffix = suffix
        self._item = dict(item) if isinstance(item, dict) else {}
        # 固定英文实体 ID：sensor.ikuai_auth_cs_info
        self._setup_forced_entity_id(
            "sensor", f"auth_{auth_entity_slug(self._account(), suffix)}_info")
        self._attr_name = f"{suffix} 账号信息"

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
        return f"{DOMAIN}_auth_{self._suffix}_info_{self.coordinator.host}"

    @property
    def device_info(self):
        return auth_device_info(self.coordinator)

    @property
    def native_value(self):
        enabled = str((self._account().get("detail") or {}).get("enabled") or "").lower()
        if enabled in ("yes", "1", "true", "on"):
            return "启用"
        if enabled in ("no", "0", "false", "off"):
            return "停用"
        return "未知"

    @property
    def extra_state_attributes(self):
        account = self._account()
        detail = account.get("detail") or {}
        attrs = {
            "username": account.get("username") or self._suffix,
            "account_id": account.get("id"),
            "online": bool(account.get("online")),
        }
        attrs.update({k: detail[k] for k in self._DETAIL_FIELDS
                      if k in detail and detail[k] not in ("", None)})

        for key in self._TIME_FIELDS:
            text = stamp_ts(detail.get(key))
            if text:
                attrs[f"{key}_text"] = text
        # 有效期：0 = 长期；也顺带给"是否已过期"，方便自动化直接判断
        expires = detail.get("expires")
        try:
            expires_ts = int(expires)
        except (TypeError, ValueError):
            expires_ts = 0
        if expires_ts > 0:
            attrs["expires_text"] = stamp_ts(expires_ts)
            attrs["expired"] = expires_ts < time.time()
        else:
            attrs["expires_text"] = "长期有效"
            attrs["expired"] = False
        if "enabled" in detail:
            attrs["enabled_on"] = str(detail.get("enabled")).lower() in ("yes", "1", "true", "on")
        cardid = detail.get("cardid")
        if cardid:
            attrs["cardid_masked"] = mask_cardid(cardid)
        attrs["querytime"] = (self.coordinator.data or {}).get("querytime")
        return attrs


def _pct(value):
    """"3.99%" / 3.99 → 3.99；非法值 None。"""
    try:
        return round(float(str(value).replace("%", "")), 2)
    except (TypeError, ValueError):
        return None


class IKuaiApSensor(IKuaiEntity, SensorEntity):
    """单台 AP 的在线状态（动态，每台 AP 一个；无 AP 时不建实体）。

    AP 行字段随 AC 固件版本不同，在线判据与带机数都做多候选键兼容。
    """

    _attr_icon = "mdi:access-point"

    def __init__(self, coordinator, suffix, item):
        super().__init__(coordinator)
        self._suffix = suffix
        self._item = dict(item) if isinstance(item, dict) else {}
        # 固定英文实体 ID：sensor.ikuai_ap_1
        self._setup_forced_entity_id("sensor", f"ap_{suffix}")

    @property
    def name(self):
        row = self._row()
        return f"AP {row.get('apname') or row.get('name') or self._suffix}"

    @property
    def unique_id(self):
        return f"{DOMAIN}_ap_{self._suffix}_{self.coordinator.host}"

    def _row(self):
        for row in (self.coordinator.data.get("ext") or {}).get("ac_list") or []:
            if str(row.get("id")) == self._suffix:
                return row
        return dict(self._item)

    @staticmethod
    def _online(row):
        if row.get("status") in (1, "1", "online", "yes", "on"):
            return True
        if row.get("online") in (1, "1", True):
            return True
        return row.get("state") == "online"

    @property
    def native_value(self):
        return "在线" if self._online(self._row()) else "离线"

    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        row = self._row()
        clients = None
        for key in ("online_user", "user_num", "client_count",
                    "wireless_user", "access_user"):
            if row.get(key) not in (None, ""):
                clients = row.get(key)
                break
        attrs = {
            "online": self._online(row),
            "mac": row.get("mac"),
            "ip_addr": row.get("ip_addr"),
            "model": row.get("model") or row.get("apmodel"),
            "version": row.get("ver") or row.get("softver") or row.get("version"),
            "clients": clients,
            "uptime": row.get("uptime") or row.get("elapse_time"),
            "querytime": (self.coordinator.data or {}).get("querytime"),
        }
        return {k: v for k, v in attrs.items() if v not in ("", None)}

    def update_item(self, item):
        self._item = dict(item) if isinstance(item, dict) else self._item


class IKuaiLanHostsSensor(IKuaiEntity, SensorEntity):
    """在线终端列表实体：state=IPv4终端数，属性含 IPv4/IPv6 两份明细
    （``entries`` / ``ipv6_entries``，各按 ``ATTR_ROW_MAX`` 截断）+ IPv4 实时流量
    Top5，对齐爱快「状态监控」两个终端监控页。

    节点结构
    --------
    ``entries`` 里每个在线终端是一个节点，节点内的 ``detail`` 是该设备的**子节点**
    （应用流量明细 + 连接明细，按 IP 从 ``monitor_lanip`` 的 ``TYPE=terminal_flow``
    与 ``TYPE=conn,conn_num`` 抓取），数值**直接带单位**、与爱快后台显示一致：:

        entries:
          - ip_addr: 192.168.5.14        # 设备节点
            mac: 02:42:c0:a8:05:0e
            hostname: ...
            total_up: 31.74 GB           # 整机累计（接口给字节，÷1024³ 后带单位）
            total_down: 23.12 GB
            upload: 280 B/s              # 实时速率（接口给字节/秒）
            download: 458 B/s
            simple_qos: true             # 是否被限速（每台设备都有这个标记）
            speed_limit:                 # 被限速时才有的子节点（simple_qos 规则）
              id: 1
              upload: 100                # 接口原值，与爱快「IP限速」页一致
              download: 100
              comment: 快速添加
              enabled: 'yes'
              time: 00:00-23:59
              week: '1234567'
              week_text: 每天
            detail:                      # 该设备的子节点
              updated: 1760...           # 这份详情的抓取时刻
              app_count: 5
              apps:
                - app_name: 常用协议
                  upload_rate: 16.38 KB/s
                  share_up: 99.59%
                  total_up: 31.6 GB
                  download_rate: 15.4 KB/s
                  share_down: 86.02%
                  total_down: 19.87 GB
              conn_num: 3
              conns_shown: 3
              conns:
                - protocol: tcp
                  status: 已连接
                  interface: wan2
                  app_name: Nps_and_Npc
                  dst_addr: 47.108.189.87
                  dst_port: 8024
                  src_port: 49470
                  forward_addr: 100.67.70.147
                  total_up: 271.74 MB
                  total_down: 55.46 MB

    数据来源
    --------
    IPv4 来自每次主轮询都要抓的 ``monitor_lanip``（不新增请求）；IPv6 来自
    ``monitor_lanipv6``；``detail`` 来自每轮扩展轮询里按 IP 限量抓取的
    ``terminal_flow`` + ``conn``（见 ``data_fetcher._ext_terminal_detail``）；
    ``simple_qos`` / ``speed_limit`` 来自扩展轮询里**已经在抓**的规则明细
    （``ext.rule_rows.simple_qos``，按 IP 匹配终端，同样不新增请求）。

    默认禁用，需要时在实体注册表启用。
    """

    _attr_icon = "mdi:devices"
    _attr_native_unit_of_measurement = "个"
    # 整包属性不写数据库：终端明细 + 每台设备的应用明细远超 recorder 单实体
    # 16384 字节的属性上限，而这些都属于「当前快照」，没有落库价值 ——
    # 状态机、模板、前端仍照常可读。state（终端数）照常进历史。
    _unrecorded_attributes = frozenset(
        {"entries", "ipv6_entries", "upload_top", "download_top"})

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self._attr_entity_registry_enabled_default = False
        # 固定英文实体 ID：sensor.ikuai_lan_hosts
        self._setup_forced_entity_id("sensor", "lan_hosts")

    @property
    def name(self):
        return "在线终端列表"

    @property
    def unique_id(self):
        return f"{DOMAIN}_lan_hosts_{self.coordinator.host}"

    @property
    def native_value(self):
        return len((self.coordinator.data.get("ext") or {}).get("lan_hosts") or [])

    @property
    def state(self):
        return self.native_value

    # IPv4 终端监控（monitor_lanip）爱快实际提供的全部有意义字段；
    # 注意接口里没有 interface/online_time，上线时间是 uptime(文本)+timestamp(unix)
    _V4_FIELDS = (
        "id", "ip_addr", "mac", "hostname", "comment",
        "client_device", "client_model", "client_type",
        "username", "ppptype", "auth_type",
        "ssid", "apname", "apmac", "bssid", "signal", "frequencies",
        "connect_num", "upload", "download", "uprate", "downrate",
        "total_up", "total_down", "uptime", "timestamp", "reject",
        "link_addr", "dtalk_name", "ac_gid",
    )

    # 连接明细保留的字段（顺序与爱快后台「连接详情」的列一致）
    _CONN_FIELDS = ("protocol", "status", "interface", "app_name",
                    "dst_addr", "dst_port", "src_port", "forward_addr")

    # 设备节点里需要换算成带单位文本的字段：
    #   _BYTES_FIELDS 接口给字节（÷1024 到 KB、÷1024² 到 MB、÷1024³ 到 GB）；
    #   _RATE_FIELDS  接口给字节/秒（换算后补 /s，如 "12.23 KB/s"）。
    # 口径与爱快 Web 接口其它字段一致（累计量都是字节），与后台显示同一量级。
    _BYTES_FIELDS = ("total_up", "total_down")
    _RATE_FIELDS = ("upload", "download")

    @classmethod
    def _brief(cls, row, detail=None, qos=None):
        """设备节点：白名单字段 + 限速标记 + （可选）detail 子节点。

        ``total_up`` / ``total_down`` 是爱快给的字节数、``upload`` / ``download``
        是字节/秒，这里都换算成带单位的文本（``34084923221`` → ``"31.74 GB"``、
        ``12523`` → ``"12.23 KB/s"``），和 detail 里的应用/连接保持同一种写法；
        其余字段原样透传。

        ``simple_qos`` 是每个设备都有的布尔标记（**这台设备是否被限速**）；
        被限速时再给 ``speed_limit`` 子节点（``simple_qos`` 规则的明细）。
        """
        item = {}
        for key in cls._V4_FIELDS:
            value = row.get(key)
            if value in ("", None):
                continue
            if key in cls._BYTES_FIELDS:
                try:
                    value = cls._fmt_bytes(int(value))
                except (TypeError, ValueError):
                    pass
            elif key in cls._RATE_FIELDS:
                try:
                    value = cls._fmt_rate(int(value))
                except (TypeError, ValueError):
                    pass
            item[key] = value
        item["simple_qos"] = bool(qos)
        if qos:
            item["speed_limit"] = cls._speed_limit(qos)
        if detail:
            # 设备子节点：该终端的应用流量 + 连接明细
            # （按 IP 抓，见 data_fetcher._ext_terminal_detail）
            item["detail"] = detail
        return item

    # 限速规则（simple_qos）里给到设备节点上的字段
    _QOS_FIELDS = ("id", "upload", "download", "comment", "enabled",
                   "time", "week", "interface")

    @classmethod
    def _speed_limit(cls, row):
        """一条 IP 限速规则 → 设备节点里的 ``speed_limit`` 子节点。

        ``upload`` / ``download`` 是接口**原值**（与爱快「IP限速」页一致，不做换算）；
        另外给一个中文生效星期 ``week_text``（``"1234567"`` → ``"每天"``）。
        """
        item = {key: row.get(key) for key in cls._QOS_FIELDS
                if row.get(key) not in ("", None)}
        text = week_text(row.get("week"))
        if text:
            item["week_text"] = text
        return item

    @staticmethod
    def _qos_map(ext):
        """IP → 限速规则（``simple_qos``）：爱快的 IP 限速是按 IP 匹配终端的。

        数据来自扩展轮询里**已经在抓**的规则明细（``rule_rows.simple_qos``，
        由 ``data_fetcher._ext_counts`` 用全量 limit 抓取），所以**零新增请求**。

        只做 IP 精确匹配 —— 规则里写的是 IP 分组 / 网段（如 ``192.168.1.1-100``）
        时匹配不到，那些终端会显示 ``simple_qos: false``。
        """
        rows = (ext.get("rule_rows") or {}).get("simple_qos") or []
        found = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            ip = str(row.get("ip_addr") or "").strip()
            if ip and ip not in found:
                found[ip] = row
        return found

    @staticmethod
    def _fmt_bytes(value):
        """字节数 → 带单位的文本（1024 进制，去掉多余的尾零）。

        ``33924792029`` → ``"31.6 GB"``，``429`` → ``"429 B"``。
        """
        text = human_bytes(value)
        if not text or text.endswith(" B"):
            return text
        num, _, unit = text.partition(" ")
        return f"{num.rstrip('0').rstrip('.')} {unit}"

    @classmethod
    def _fmt_rate(cls, value):
        """字节/秒 → 带单位文本（``16768`` → ``"16.38 KB/s"``）。"""
        text = cls._fmt_bytes(value)
        return f"{text}/s" if text else None

    @staticmethod
    def _fmt_share(part, whole):
        """占比文本（``33924792029 / 34063797034`` → ``"99.59%"``）。"""
        if not whole:
            return None
        return f"{100 * part / whole:.2f}%"

    @classmethod
    def _detail(cls, entry):
        """把某终端的详情整理成「设备详情」子节点。

        数值**直接带单位**（与爱快后台显示一致，不再给"数值 + 文本"两份）：

        * ``apps`` —— 对应后台「终端详情 → 应用」那张表，每行
          应用名 / 上行速率 / 累计上行及占比 / 下行速率 / 累计下行及占比。
          速率是抓取那一刻的瞬时值（B/s，按轮换抓取，看新鲜度用 ``updated``），
          累计是字节。占比因为文本已经无法再计算，所以一并给出。
        * ``conns`` —— 该终端当前的连接明细（协议 / 状态 / 接口 / 所属应用 /
          对端地址 / 对端端口 / 本地端口 / 转发地址 / 上下行累计），按流量降序
          取前 ``TERMINAL_CONN_KEEP`` 条；``conn_num`` 是接口给出的真实连接总数
          （可能有几百条，不全部塞进属性）。

        上下行累计都为 0 的应用会丢掉（爱快固定返回十来个分类，多数该设备没走流量）。
        """
        def _num(value):
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        buffered = []
        for app in (entry or {}).get("apps") or []:
            if not isinstance(app, dict):
                continue
            up = _num(app.get("total_up"))
            down = _num(app.get("total_down"))
            if up <= 0 and down <= 0:
                continue
            buffered.append((up + down, app, up, down))
        if not buffered:
            return None
        buffered.sort(key=lambda item: item[0], reverse=True)
        up_sum = sum(item[2] for item in buffered)
        down_sum = sum(item[3] for item in buffered)
        apps = [{
            "app_name": app.get("app_name"),
            "upload_rate": cls._fmt_rate(_num(app.get("upload"))),
            "share_up": cls._fmt_share(up, up_sum),
            "total_up": cls._fmt_bytes(up),
            "download_rate": cls._fmt_rate(_num(app.get("download"))),
            "share_down": cls._fmt_share(down, down_sum),
            "total_down": cls._fmt_bytes(down),
        } for _rank, app, up, down in buffered]

        conns = []
        for row in (entry or {}).get("conns") or []:
            if not isinstance(row, dict):
                continue
            item = {key: row.get(key) for key in cls._CONN_FIELDS
                    if row.get(key) not in ("", None)}
            up = _num(row.get("total_up"))
            down = _num(row.get("total_down"))
            item["total_up"] = cls._fmt_bytes(up)
            item["total_down"] = cls._fmt_bytes(down)
            item["_rank"] = up + down
            conns.append(item)
        conns.sort(key=lambda conn: conn["_rank"], reverse=True)
        for conn in conns:
            conn.pop("_rank", None)

        detail = {
            "updated": (entry or {}).get("updated"),
            "app_count": len(apps),
            "truncated": len(apps) > TERMINAL_APP_MAX,
            "apps": apps[:TERMINAL_APP_MAX],
        }
        if conns:
            detail["conn_num"] = (entry or {}).get("conn_num")
            detail["conns_shown"] = min(len(conns), TERMINAL_CONN_KEEP)
            detail["conns"] = conns[:TERMINAL_CONN_KEEP]
        return detail

    @staticmethod
    def _compact(row):
        """保留整行所有非空字段（IPv6 终端字段随固件版本不同，不做白名单裁剪）。"""
        skip = {"ip_addr_int"}
        return {k: v for k, v in row.items()
                if k not in skip and v not in ("", None)}

    @staticmethod
    def _top(rows, field, qos_map=None):
        """实时流量 Top5：``field`` 是速率字段（upload / download），值同样带单位。"""
        qos_map = qos_map or {}
        ranked = []
        for row in rows:
            try:
                val = int(row.get(field) or 0)
            except (TypeError, ValueError):
                continue
            if val > 0:
                item = IKuaiLanHostsSensor._brief(
                    row, qos=qos_map.get(str(row.get("ip_addr") or "")))
                item[field] = IKuaiLanHostsSensor._fmt_rate(val)
                ranked.append((val, item))
        ranked.sort(key=lambda x: x[0], reverse=True)
        return [item for _val, item in ranked[:5]]

    @property
    def extra_state_attributes(self):
        ext = self.coordinator.data.get("ext") or {}
        rows = ext.get("lan_hosts") or []
        rows_v6 = ext.get("lan_hosts_v6") or []
        detail_cache = ext.get("terminal_detail") or {}
        # 限速规则（simple_qos）按 IP 匹配终端：已经在抓的规则明细，零新增请求
        qos_map = self._qos_map(ext)
        # entries：每个在线终端一个节点，节点内的 detail 是该设备的子节点
        # （应用流量明细 + 连接明细，按 IP 从 monitor_lanip 的 terminal_flow / conn 抓取）
        entries = [self._brief(r, self._detail(detail_cache.get(r.get("ip_addr"))),
                               qos_map.get(str(r.get("ip_addr") or "")))
                   for r in rows if isinstance(r, dict)]
        attrs = {
            "count": len(rows),
            # 与爱快后台一致：IPv4终端监控 / IPv6终端监控 分开列示
            "ipv4_count": len(rows),
            "ipv6_count": len(rows_v6),
            "entries": _cap_attrs(entries),
            "ipv6_entries": _cap_attrs(
                [self._compact(r) for r in rows_v6 if isinstance(r, dict)]),
            "upload_top": self._top(rows, "upload", qos_map),
            "download_top": self._top(rows, "download", qos_map),
            # 有多少个终端已经抓到设备详情（终端多时按轮换补齐）
            "detail_count": len([1 for e in entries if e.get("detail")]),
            # 在线终端里有多少台处在限速中（规则数见 sensor.ikuai_rule_stats）
            "speed_limited": len([1 for e in entries if e.get("simple_qos")]),
            "querytime": (self.coordinator.data or {}).get("querytime"),
        }
        if len(rows) > ATTR_ROW_MAX:
            attrs["truncated"] = True
        if len(rows_v6) > ATTR_ROW_MAX:
            attrs["ipv6_truncated"] = True
        return attrs


class IKuaiHardwareSensor(IKuaiEntity, SensorEntity):
    """硬件信息：CPU 型号/位数/频率/缓存/核心/线程、主板、内存、硬盘、PCI 设备清单。

    state = **CPU 型号**（最能代表这台机器的一条硬件事实，如
    ``Intel(R) Xeon(R) CPU E3-1265L V2 @ 2.50GHz``）。

    属性与爱快 ``hardwareinfo`` 接口的字段一一对应：``cpu_model`` / ``cpu_bit`` /
    ``cpu_mhz`` / ``cpu_cache`` / ``cpu_cores`` / ``cpu_threads`` / ``mainboard`` /
    ``memory_mb``（+ 可读 ``memory_text``）/ ``hdd`` / ``pci``（按控制器类别分组的
    设备清单，属性不写库）/ ``pci_devices``（设备总数）。

    数据来自扩展轮询里**已经在抓**的 ``hardwareinfo``（``ext.hardware``），
    零新增请求。
    """

    _attr_icon = "mdi:chip"
    # PCI 清单是整块硬件快照（长文本），不写数据库
    _unrecorded_attributes = frozenset({"pci"})

    # 爱快 hardwareinfo 字段 → 属性名（一一对应）
    _FIELDS = (("cpu_model", "cpumodel"), ("cpu_bit", "cpubit"),
               ("cpu_mhz", "cpumhz"), ("cpu_cache", "cpucache"),
               ("cpu_cores", "cpucores"), ("cpu_threads", "cputhread"))

    def __init__(self, coordinator):
        super().__init__(coordinator)
        # 固定英文实体 ID：sensor.ikuai_hardware
        self._setup_forced_entity_id("sensor", "hardware")

    @property
    def name(self):
        return "硬件信息"

    @property
    def unique_id(self):
        return f"{DOMAIN}_hardware_{self.coordinator.host}"

    def _blocks(self) -> tuple:
        """``(hardwareinfo, pciinfo)`` 两个子块（取不到时给空 dict）。"""
        hw = (self.coordinator.data.get("ext") or {}).get("hardware") or {}
        if not isinstance(hw, dict):
            return {}, {}
        info = hw.get("hardwareinfo") or {}
        pci = hw.get("pciinfo") or {}
        return (info if isinstance(info, dict) else {},
                pci if isinstance(pci, dict) else {})

    @property
    def native_value(self):
        return self._blocks()[0].get("cpumodel") or None

    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        info, pci = self._blocks()
        attrs = {}
        for name, key in self._FIELDS:
            value = info.get(key)
            if value not in ("", None):
                attrs[name] = value
        # 主板字符串带多余的空格/逗号（" /DQ77KB, "），顺手修一下
        mainboard = str(info.get("mainboard") or "").strip().strip(",").strip()
        if mainboard:
            attrs["mainboard"] = mainboard
        try:
            memory = int(info.get("memory") or 0)
        except (TypeError, ValueError):
            memory = 0
        if memory:
            attrs["memory_mb"] = memory
            attrs["memory_text"] = human_bytes(memory * 1024 * 1024)
        if info.get("hdd"):
            attrs["hdd"] = info["hdd"]
        if pci:
            attrs["pci"] = {kind: _cap_attrs(rows) for kind, rows in pci.items()}
            attrs["pci_devices"] = sum(
                len(rows) for rows in pci.values() if isinstance(rows, list))
        attrs["querytime"] = (self.coordinator.data or {}).get("querytime")
        return attrs


class IKuaiAuthServerSensor(IKuaiEntity, SensorEntity):
    """本地认证 / VPN 服务端（PPPoE・PPTP・L2TP・OpenVPN）：启停状态 + 参数。

    state = **已启用的服务数**（几个服务端开着）。

    属性按服务分组（``pppoe`` / ``pptp`` / ``l2tp`` / ``openvpn``）列出该服务端的参数，
    字段名与爱快接口一致、空值不给；``enabled`` 归一成布尔（接口给的是 ``yes``/``no``），
    另给 ``enabled_services``（已启用的服务名列表，模板/卡片直接用）。

    安全：RADIUS 的 ``secret`` 与 IPsec 的 ``ipsec_secret`` 在**抓取时**就已打成
    ``***``（只表明"设了密码"）；OpenVPN 的 ``ca`` / ``cert`` / ``key`` / ``tls_auth``
    是整段 PEM，抓取时整段丢弃，只留 ``has_ca`` / ``has_cert`` / ``has_key`` /
    ``has_tls_auth`` 四个布尔。所以实体属性里不含任何密钥。

    数据来自扩展轮询（每 3 个主轮询一次）的 4 个服务端接口，整包属性不写数据库。
    """

    _attr_icon = "mdi:server-security"
    _attr_native_unit_of_measurement = "个"
    _unrecorded_attributes = frozenset(
        {"pppoe", "pptp", "l2tp", "openvpn", "enabled_services"})

    # (ext.auth_servers 里的键, 显示名)
    _SERVICES = (("pppoe", "PPPoE"), ("pptp", "PPTP"),
                 ("l2tp", "L2TP"), ("openvpn", "OpenVPN"))
    _TRUE = ("yes", "1", "true", "on")

    def __init__(self, coordinator):
        super().__init__(coordinator)
        # 固定英文实体 ID：sensor.ikuai_auth_server
        self._setup_forced_entity_id("sensor", "auth_server")

    @property
    def name(self):
        return "本地认证服务"

    @property
    def unique_id(self):
        return f"{DOMAIN}_auth_server_{self.coordinator.host}"

    def _servers(self) -> dict:
        servers = (self.coordinator.data.get("ext") or {}).get("auth_servers") or {}
        return servers if isinstance(servers, dict) else {}

    @classmethod
    def _is_enabled(cls, row) -> bool:
        return str((row or {}).get("enabled")).strip().lower() in cls._TRUE

    @classmethod
    def _service_block(cls, row: dict) -> dict:
        """一个服务端的参数块：``enabled`` 归一成布尔，空值不给。"""
        block = {key: value for key, value in row.items()
                 if value not in ("", None)}
        block["enabled"] = cls._is_enabled(row)
        return block

    @property
    def native_value(self):
        servers = self._servers()
        return len([1 for key, _label in self._SERVICES
                    if self._is_enabled(servers.get(key))])

    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        servers = self._servers()
        attrs = {}
        enabled = []
        for key, label in self._SERVICES:
            row = servers.get(key)
            if not isinstance(row, dict):
                continue
            attrs[key] = self._service_block(row)
            if attrs[key]["enabled"]:
                enabled.append(label)
        attrs["enabled_services"] = enabled
        attrs["querytime"] = (self.coordinator.data or {}).get("querytime")
        return attrs


class IKuaiDhcpSensor(IKuaiEntity, SensorEntity):
    """DHCP 服务：服务端配置 + 静态分配 + 动态租约 + MAC 黑白名单。

    state = **已启用的 DHCP 服务端数**（有几个接口开着 DHCP，通常 1）。

    属性（数据来自扩展轮询里**已经在抓**的 6 个 DHCP 接口，零新增请求）：

    * ``servers`` —— 每台 DHCP 服务端的配置，字段名与爱快一致、空值不给：
      ``enabled``（归一成布尔）/ ``interface`` / ``addr_pool`` / ``exclude_pool`` /
      ``netmask`` / ``gateway`` / ``dns1`` / ``dns2`` / ``domain`` / ``wins1`` /
      ``wins2`` / ``lease``（秒）+ ``lease_text``（如 ``1天03:46:40``）/ ``available``
      （可用地址数）/ ``status``；``interfaces`` 是已启用服务端的接口名列表；
    * ``static_total`` / ``statics`` —— 静态分配（MAC→IP 绑定）的条数与明细
      （``ip_addr`` / ``mac`` / ``hostname`` / ``comment`` / ``gateway`` / ``dns1`` /
      ``dns2`` / ``enabled``）；``compat_arp`` 是「兼容 ARP」开关；
    * ``lease_total`` / ``leases`` —— 动态租约的条数与明细（``start_time`` /
      ``end_time`` 自动转成可读时间，``timeout`` 秒另给 ``timeout_text``）；
    * ``blocked_total`` / ``blocked`` / ``acl_mode`` / ``acl_mode_text`` —— 服务端
      MAC 黑白名单（``acl_mode`` 0=黑名单 1=白名单，与爱快后台一致）的条数与明细；
    * ``dhcp6_lease_total`` / ``dhcp6_leases`` / ``dhcp6_blocked_total`` /
      ``dhcp6_blocked`` / ``dhcp6_acl_mode_text`` —— IPv6 侧的同样内容；
    * ``querytime`` —— 本次数据抓取时刻。

    明细列表按 ``ATTR_ROW_MAX`` 截断（超出时给 ``truncated``），整包属性不写数据库。
    """

    _attr_icon = "mdi:dns-outline"
    _attr_native_unit_of_measurement = "个"
    _unrecorded_attributes = frozenset({
        "servers", "interfaces", "statics", "leases", "blocked",
        "dhcp6_leases", "dhcp6_blocked"})

    _TRUE = ("yes", "1", "true", "on")

    def __init__(self, coordinator):
        super().__init__(coordinator)
        # 固定英文实体 ID：sensor.ikuai_dhcp
        self._setup_forced_entity_id("sensor", "dhcp")

    @property
    def name(self):
        return "DHCP服务"

    @property
    def unique_id(self):
        return f"{DOMAIN}_dhcp_{self.coordinator.host}"

    # ------------------------------------------------------------- 取数
    def _ext(self, key):
        return (self.coordinator.data.get("ext") or {}).get(key)

    def _block(self, key) -> dict:
        block = self._ext(key)
        return block if isinstance(block, dict) else {}

    def _servers(self) -> list:
        rows = self._ext("dhcp_servers")
        return [r for r in rows if isinstance(r, dict)] \
            if isinstance(rows, list) else []

    def _rows(self, key) -> list:
        """``{rows: [...], total: n}`` 形态的明细行。"""
        rows = self._block(key).get("rows")
        return [r for r in rows if isinstance(r, dict)] \
            if isinstance(rows, list) else []

    # ------------------------------------------------------------- 加工
    @classmethod
    def _is_enabled(cls, row) -> bool:
        return str((row or {}).get("enabled")).strip().lower() in cls._TRUE

    @classmethod
    def _row(cls, row: dict) -> dict:
        """单行：空值字段不给，``enabled`` 归一成布尔。"""
        out = {k: v for k, v in (row or {}).items() if v not in ("", None)}
        if "enabled" in out:
            out["enabled"] = cls._is_enabled(row)
        return out

    @classmethod
    def _with_text(cls, row: dict, field: str, out_field: str) -> dict:
        """加工一行，再把秒数字段补一个时长文本（``lease``/``timeout``）。"""
        block = cls._row(row)
        text = dhms(block.get(field))
        if text:
            block[out_field] = text
        return block

    # ------------------------------------------------------------- 实体
    @property
    def native_value(self):
        return len([1 for row in self._servers() if self._is_enabled(row)])

    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        servers = [self._with_text(r, "lease", "lease_text")
                   for r in self._servers()]
        statics = self._rows("dhcp_static")
        leases = self._rows("dhcp_leases")
        blocked = self._rows("dhcp_acl_mac")
        v6_leases = self._rows("dhcp6_leases")
        v6_blocked = self._rows("dhcp6_acl_mac")
        static_block = self._block("dhcp_static")
        acl_block = self._block("dhcp_acl_mac")
        v6_acl_block = self._block("dhcp6_acl_mac")
        attrs = {
            "servers": _cap_attrs(servers),
            "interfaces": [r.get("interface") for r in servers
                           if r.get("interface") and r.get("enabled")],
            "static_total": static_block.get("total", len(statics)),
            "statics": _cap_attrs([self._row(r) for r in statics]),
            "compat_arp": static_block.get("compat_arp"),
            "lease_total": self._block("dhcp_leases").get("total", len(leases)),
            "leases": _cap_attrs([self._with_text(r, "timeout", "timeout_text")
                                  for r in leases]),
            "blocked_total": acl_block.get("total", len(blocked)),
            "blocked": _cap_attrs([self._row(r) for r in blocked]),
            "acl_mode": acl_block.get("mode"),
            "acl_mode_text": acl_block.get("mode_text"),
            "dhcp6_lease_total": self._block("dhcp6_leases").get("total",
                                                                 len(v6_leases)),
            "dhcp6_leases": _cap_attrs([self._with_text(r, "timeout", "timeout_text")
                                        for r in v6_leases]),
            "dhcp6_blocked_total": v6_acl_block.get("total", len(v6_blocked)),
            "dhcp6_blocked": _cap_attrs([self._row(r) for r in v6_blocked]),
            "dhcp6_acl_mode_text": v6_acl_block.get("mode_text"),
            "querytime": (self.coordinator.data or {}).get("querytime"),
        }
        if len(statics) > ATTR_ROW_MAX or len(leases) > ATTR_ROW_MAX:
            attrs["truncated"] = True
        return attrs


class IKuaiInterfacesSensor(IKuaiEntity, SensorEntity):
    """接口总览：把「物理网口 ↔ 逻辑口 ↔ 地址/速率/线路检测」串到一个实体里。

    state = **网络是否正常**，判据是 WAN 口的线路检测结果（``snapshoot_wan.errmsg``）：

    * 参与判断的只有**有 MAC 地址**的 WAN 口 —— 没接线的口 ``mac`` / ``errmsg``
      都是空，不参与（否则会被误判成异常）；
    * 全部参与口都是「线路检测成功」→ ``正常``；
    * **只要有一口不是**（多拨场景：一条通、一条不通）→ ``异常``；
    * 一个可判断的口都没有 → ``未知``。

    属性：

    * ``ports`` —— 物理口（``ether_info``）：link / 协商速率 / 双工 / 型号 / 驱动 /
      MAC / 绑定到的逻辑口；绑 LAN 时带 ``lan`` 子节点、绑 WAN 时带 ``wan`` 子节点
      （IP、网关、DNS、上线时长、入网方式、线路检测结果），逻辑口有实时速率时再带
      ``rate``；
    * ``lan_ports`` / ``wan_ports`` —— 逻辑口明细（含 ``member`` 成员物理口，能看出
      聚合 / 桥是怎么组起来的）；
    * ``abnormal`` —— 线路检测不通过的 WAN 口（state=异常时它非空）；
    * ``iface_check_failed`` / ``iface_check_count`` —— 爱快「线路体检」
      （``monitor_iface`` → ``iface_check``）里不通过的行与总检查项数
      （原来那条 ``binary_sensor.ikuai_iface_fault`` 的内容，已并入本实体）；
    * ``summary`` —— 端口数 / up 数 / 可判断 WAN 数 / 是否正常 / 体检不通过数。

    数据全部来自**已经在抓**的请求（主轮询的 ``lan`` + ``TYPE=ether_info,snapshoot``、
    扩展轮询的 ``monitor_iface``），零新增请求；整包属性不写数据库。
    """

    _attr_icon = "mdi:ethernet"
    _unrecorded_attributes = frozenset(
        {"ports", "lan_ports", "wan_ports", "abnormal", "summary",
         "iface_check_failed"})

    # 线路检测通过时爱快返回的固定文本
    _CHECK_OK = "线路检测成功"
    STATE_OK = "正常"
    STATE_BAD = "异常"
    STATE_UNKNOWN = "未知"
    # 虚拟网桥 / 桥接口不算物理口（与爱快后台、原「网口链路」实体一致）
    _VIRTUAL_PREFIXES = ("vnet", "qtap", "vnic")
    _LAN_KEYS = ("id", "ip_addr", "netmask", "comment", "bandmode", "linkmode",
                 "member")
    _WAN_KEYS = ("id", "ip_addr", "netmask", "gateway", "dns1", "dns2", "mac",
                 "internet", "default_route", "comment", "member", "errmsg",
                 "check_res")

    def __init__(self, coordinator):
        super().__init__(coordinator)
        # 固定英文实体 ID：sensor.ikuai_interfaces
        self._setup_forced_entity_id("sensor", "interfaces")

    @property
    def name(self):
        return "接口"

    @property
    def unique_id(self):
        return f"{DOMAIN}_interfaces_{self.coordinator.host}"

    # ---------------------------------------------------------------- 取数
    @staticmethod
    def _wan_rows(ext) -> dict:
        """WAN 口明细：``wan_bands``（实时数据）与 ``snapshoot_wan``（线路检测结果、
        成员、计数）合并成一份，键为口 id（或接口名）。"""
        merged: dict = {}
        for source in (ext.get("wan_bands"), ext.get("snapshoot_wan")):
            for row in source or []:
                if not isinstance(row, dict):
                    continue
                key = str(row.get("id") or row.get("interface") or "")
                if not key:
                    continue
                item = merged.setdefault(key, {})
                for name, value in row.items():
                    if name.startswith("_") or value in ("", None):
                        continue
                    item.setdefault(name, value)
        return merged

    @classmethod
    def _checkable(cls, ext) -> list:
        """参与"网络是否正常"判断的口：带 MAC 地址的 WAN 口。"""
        return [row for row in cls._wan_rows(ext).values()
                if str(row.get("mac") or "").strip()]

    @classmethod
    def _network_state(cls, ext) -> tuple:
        """返回 ``(state, 不正常的口列表)``。"""
        checked = cls._checkable(ext)
        if not checked:
            return cls.STATE_UNKNOWN, []
        bad = [row for row in checked
               if str(row.get("errmsg") or "").strip() != cls._CHECK_OK]
        return (cls.STATE_BAD if bad else cls.STATE_OK), bad

    @property
    def native_value(self):
        return self._network_state(self.coordinator.data.get("ext") or {})[0]

    @property
    def state(self):
        return self.native_value

    # ------------------------------------------------------------ 组装属性
    @classmethod
    def _logic_port(cls, row, keys) -> dict:
        """一个逻辑口的展示字段（空值不给，附带入网方式文本与上线时长）。"""
        if not isinstance(row, dict):
            return {}
        item = {key: row.get(key) for key in keys if row.get(key) not in ("", None)}
        if "internet" in item:
            item["internet_text"] = wan_internet_text(item["internet"])
        seconds, text = dhms_from_ts(
            row.get("updatetime") or row.get("pppoe_updatetime"))
        if text:
            item["uptime_seconds"] = seconds
            item["uptime_text"] = text
        return item

    @classmethod
    def _logic_ports(cls, rows, keys) -> dict:
        out = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            iface = str(row.get("interface") or row.get("id") or "")
            if iface:
                out[iface] = cls._logic_port(row, keys)
        return out

    @staticmethod
    def _members_index(rows) -> dict:
        """逻辑口行按「成员物理口名」与「接口名」双索引（member = 聚合/桥的成员）。"""
        index: dict = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            for member in row.get("member") or []:
                if isinstance(member, str) and member:
                    index.setdefault(member, row)
            iface = row.get("interface")
            if iface:
                index.setdefault(str(iface), row)
        return index

    @staticmethod
    def _iface_check(ext) -> tuple:
        """线路体检（``iface_check``）：返回 ``(全部行, 不通过的行)``。

        与 ``snapshoot_wan.errmsg``（WAN 线路检测）是两个不同来源：这里用的是爱快
        「线路体检」的探测结果，``result`` 不是 ``success`` 就算不通过。
        """
        rows = [row for row in ext.get("iface_check") or []
                if isinstance(row, dict)]
        failed = [row for row in rows
                  if row.get("result") not in ("success", "", None)]
        return rows, failed

    @staticmethod
    def _rates(ext) -> dict:
        """逻辑口 → 实时速率（MB/s），来自扩展轮询的 ``iface_stream``。"""
        out: dict = {}
        for row in ext.get("iface_stream") or []:
            if not isinstance(row, dict) or not row.get("interface"):
                continue
            try:
                up = round(int(row.get("upload", 0)) / 1024 / 1024, 3)
                down = round(int(row.get("download", 0)) / 1024 / 1024, 3)
            except (TypeError, ValueError):
                continue
            out[str(row["interface"])] = {"upload": up, "download": down}
        return out

    @classmethod
    def _ports(cls, ext) -> dict:
        info = ext.get("ether_info") or {}
        lans = cls._members_index([r for r in ext.get("snapshoot_lan") or []
                                   if isinstance(r, dict)])
        wans = cls._members_index(list(cls._wan_rows(ext).values()))
        rates = cls._rates(ext)
        ports: dict = {}
        for name, row in info.items():
            if not isinstance(row, dict):
                continue
            mac = str(row.get("mac") or "")
            if str(name).startswith(cls._VIRTUAL_PREFIXES) \
                    or row.get("driver") == "kvm" or mac.startswith("00:00:00"):
                continue        # 虚拟网桥 / 桥接口：不是物理口
            bound = row.get("interface")
            if not isinstance(bound, str) or bound in ("", "Null"):
                bound = None
            item = {
                "link": row.get("link") == 1,
                "bound": bound,
                "speed_mbps": row.get("speed") or None,
                "duplex": {1: "full", 2: "half"}.get(row.get("duplex")),
                "type": row.get("type"),
                "model": row.get("model"),
                "driver": row.get("driver"),
                "mac": mac or None,
            }
            if bound:
                lan = lans.get(bound)
                if lan is not None:
                    item["lan"] = cls._logic_port(lan, cls._LAN_KEYS)
                wan = wans.get(bound)
                if wan is not None:
                    item["wan"] = cls._logic_port(wan, cls._WAN_KEYS)
                if rates.get(bound):
                    item["rate"] = rates[bound]
            ports[str(name)] = {k: v for k, v in item.items() if v not in ("", None)}
        return ports

    @property
    def extra_state_attributes(self):
        ext = self.coordinator.data.get("ext") or {}
        state, bad = self._network_state(ext)
        ports = self._ports(ext)
        ok = None if state == self.STATE_UNKNOWN else state == self.STATE_OK
        lan_rows = [r for r in ext.get("snapshoot_lan") or [] if isinstance(r, dict)]
        check_rows, check_failed = self._iface_check(ext)
        return {
            "network_ok": ok,
            "ports": ports,
            "lan_ports": self._logic_ports(lan_rows, self._LAN_KEYS),
            "wan_ports": self._logic_ports(
                list(self._wan_rows(ext).values()), self._WAN_KEYS),
            "abnormal": [
                {k: v for k, v in {
                    "interface": row.get("interface"),
                    "errmsg": row.get("errmsg"),
                    "mac": row.get("mac"),
                    "ip_addr": row.get("ip_addr"),
                }.items() if v not in ("", None)}
                for row in bad],
            # 线路体检（原 binary_sensor.ikuai_iface_fault 的内容）
            "iface_check_count": len(check_rows),
            "iface_check_failed": _cap_attrs(check_failed),
            "summary": {
                "ports": len(ports),
                "up": len([1 for p in ports.values() if p.get("link")]),
                "wan_checked": len(self._checkable(ext)),
                "wan_abnormal": len(bad),
                "check_failed": len(check_failed),
                "network_ok": ok,
            },
            "querytime": (self.coordinator.data or {}).get("querytime"),
        }


class IKuaiApiKeySensor(IKuaiEntity, SensorEntity):
    """透传 API 的密钥（整点自动换新）。

    * state = **剩余有效分钟数** —— 真实 key 不能放 state，因为 state 一定会进
      recorder 历史库；``key`` 放在属性里并声明不写数据库，状态机/模板/前端照常可读；
    * key 算法：``md5(集成盐 | 小时序号 | CPU 指纹)[:API_KEY_LEN]``。盐由集成随机
      生成、只存在内存里，所以别人即使在爱快后台看到你的 CPU，也算不出 key；
    * 整点换新，**旧 key 再宽限一小时**（正在用的请求不会突然失效）。

    用法（三种鉴权任选，见开关 ``switch.ikuai_api`` 属性里的说明）::

        http://<HA>/ikuai/api/call?key=<key>&func_name=homepage&action=show&TYPE=sysstat
        请求头 X-IKuai-Key: <key>
    """

    _attr_icon = "mdi:key-variant"
    _attr_native_unit_of_measurement = "min"
    # key 本身不写数据库（state 只给剩余分钟数）
    _unrecorded_attributes = frozenset({"key"})

    def __init__(self, coordinator):
        super().__init__(coordinator)
        # 固定英文实体 ID：sensor.ikuai_api_key
        self._setup_forced_entity_id("sensor", API_KEY_SENSOR_SUFFIX)

    @property
    def name(self):
        return API_KEY_SENSOR_NAME

    @property
    def unique_id(self):
        return f"{DOMAIN}_api_key_{self.coordinator.host}"

    def _entry_id(self):
        return getattr(self.coordinator, "entry_id", None)

    def _info(self):
        entry_id = self._entry_id()
        if not entry_id:
            return None
        data = self.coordinator.data if isinstance(self.coordinator.data, dict) else {}
        try:
            return key_info(self.hass, entry_id, data)
        except Exception:  # pylint: disable=broad-except
            return None

    @property
    def native_value(self):
        info = self._info()
        return info["remaining_minutes"] if info else None

    @property
    def state(self):
        return self.native_value

    @property
    def extra_state_attributes(self):
        info = self._info()
        if not info:
            return None
        entry_id = self._entry_id()
        try:
            stats = api_stats(self.hass, entry_id)
            enabled = is_api_enabled(self.hass, entry_id)
        except Exception:  # pylint: disable=broad-except
            stats, enabled = {}, None
        usage = (f"地址栏：{API_CALL_PATH}?key=<key>&func_name=…&action=show"
                 "；或请求头 X-IKuai-Key: <key>")
        if entry_id:
            usage += f"；装了多台爱快时加 &entry={entry_id}（本实体所属那台）"
        return {
            "key": info["key"],
            "entry": entry_id,
            "valid_until": info["valid_until"],
            "remaining_seconds": info["remaining_seconds"],
            "algorithm": (f"md5(集成盐 | 小时序号 | CPU指纹 | 条目id)[:{API_KEY_LEN}]，"
                          "整点换新、旧 key 宽限 1 小时；多台爱快各自的 key 互不相同"),
            "usage": usage,
            "api_enabled": enabled,
            "key_hits": stats.get("key_hits"),
            "cpu_fingerprint": info.get("cpu"),
        }
