"""iKuai 实体共享基类、设备划分、固定英文实体 ID 与动态实体管理。

设备划分（一台爱快拆成三档，实体 unique_id 全部保持不变，仅所属设备变化）：

    1. 爱快主机一个设备：``(DOMAIN, host)``            —— 主机传感器/按钮/开关/追踪
    2. 每台虚拟机一个独立设备：``(DOMAIN, f"{host}::vm::{vm_id}")``
       —— 虚拟机启停开关（含控制台状态属性）、屏幕画面事件；``configuration_url``
       指向集成签发的虚拟机页面地址（爱快固件原生页面经同源代理），设备页点
       「访问」直接就进该虚拟机的页面
    3. 所有 Docker 容器共用一个设备：``(DOMAIN, f"{host}::docker")``
       —— 该设备下是**每个容器的启停开关** + 容器总数/运行数等汇总传感器；
       每个容器的映射端口与 Web 访问地址写在该容器开关的状态属性里
       （``host_ports`` / ``web_url``），一台设备里就能看到全部容器

实体 ID 一律固定为**短英文**：``<domain>.ikuai_<suffix>``（例如 ``sensor.ikuai_cpu``、
``switch.ikuai_vm_4_power``），显示名称保持中文不变。历史上由中文名自动生成的拼音 ID
（``sensor.ikuai_cpuzhan_yong``）会在实体加入后迁移一次，见 ``ForcedEntityIdMixin``。
"""
from __future__ import annotations

import datetime
import functools
import logging
import re
from urllib.parse import unquote, urlparse

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN

# 所有固定实体 ID 都带这个前缀，便于与同域其它集成的实体区分
ENTITY_ID_PREFIX = "ikuai_"

_LOGGER = logging.getLogger(__name__)

_CATEGORY_MAP = {
    "config": EntityCategory.CONFIG,
    "diagnostic": EntityCategory.DIAGNOSTIC,
}


def category(value):
    return _CATEGORY_MAP.get(value)


# ---------------------------------------------------------------------------
# 文本字段的 URL 编码还原
# ---------------------------------------------------------------------------
# 爱快接口对部分文本字段做了 URL 编码（终端备注、用户名、SSID、网址等），
# 例如 "Apple%20TV" 实际是 "Apple TV"。所有实体的属性在出口处统一还原一次。
def decode_url_text(value):
    """还原单个字符串里的 URL 编码；不含 % 或已是明文时原样返回。"""
    if not isinstance(value, str) or "%" not in value:
        return value
    try:
        return unquote(value)
    except Exception:  # pylint: disable=broad-except
        return value


# 属性里的时间戳字段名（含 const 里给日志行用的中文映射名），命中且值像 unix 秒
# 就转成 "YYYY-MM-DD HH:MM:SS"；不像时间戳的值（例如「上线: 2」）保持原样。
_TS_FIELD_NAMES = frozenset({
    "timestamp", "updatetime", "pppoe_updatetime", "update_time", "created",
    "login_time", "logout_time", "online_time", "offline_time",
    "start_time", "end_time", "create_time", "connect_time",
    "disconnect_time", "last_time", "last_seen", "expire_time", "expires",
    "时间", "更新时间", "上线时间", "下线时间", "登录时间", "创建时间",
    "到期时间", "最后时间",
})


def format_timestamp(value):
    """把 unix 秒转成本地时间字符串；不像时间戳的值原样返回。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str) and value.strip().isdigit():
        num = float(value.strip())
    else:
        return value
    if not 1_000_000_000 <= num <= 4_000_000_000:  # 2001-09 ~ 2096-11
        return value
    try:
        return datetime.datetime.fromtimestamp(num).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return value


def normalize_attributes(value, _depth=0):
    """属性出口的统一加工：URL 编码还原 + 时间戳字段转可读时间（键名不动）。"""
    if _depth > 8:
        return value
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in _TS_FIELD_NAMES:
                out[key] = format_timestamp(item)
            else:
                out[key] = normalize_attributes(item, _depth + 1)
        return out
    if isinstance(value, list):
        return [normalize_attributes(v, _depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(normalize_attributes(v, _depth + 1) for v in value)
    return decode_url_text(value)


# ---------------------------------------------------------------------------
# 设备标识与设备信息
# ---------------------------------------------------------------------------
def _data(coordinator) -> dict:
    return coordinator.data if isinstance(coordinator.data, dict) else {}


def _host_name(coordinator) -> str:
    return _data(coordinator).get("device_name") or "iKuai"


def host_identifier(coordinator) -> tuple[str, str]:
    """爱快主机设备标识。"""
    return (DOMAIN, coordinator.host)


def device_by_identifier(registry, identifier, entry_id):
    """按标识符取设备（设备注册表的兼容封装）。

    * 新版 HA（2026.8+）：``async_get_device_by_identifier(identifier, entry_id)``
      —— 标识符只在配置条目内唯一，必须带上 ``entry_id``，这也正是
      ``async_get_device`` 被弃用的原因（2027.8 失效）；
    * 老版本 HA：退回 ``async_get_device(identifiers={identifier})``。新版上这条
      路径不会走到，因此不会再触发弃用告警。
    """
    if entry_id:
        lookup = getattr(registry, "async_get_device_by_identifier", None)
        if callable(lookup):
            return lookup(identifier, entry_id)
    gotten = getattr(registry, "async_get_device", None)
    if callable(gotten):
        return gotten(identifiers={identifier})
    return None


def _device_alive(registry, device_id) -> bool:
    """注册表里这台设备还在吗（查不了就当作"还在"）。"""
    getter = getattr(registry, "async_get", None)
    if not callable(getter):
        return True
    try:
        return getter(device_id) is not None
    except Exception:  # pylint: disable=broad-except
        return True


def host_device_id(coordinator) -> str | None:
    """爱快主机设备在**设备注册表里的 id**（子设备挂父设备要用它）。

    HA 2026.8 起 ``via_device``（用标识符挂父设备）弃用，改为 ``via_device_id``
    （注册表 id），旧写法 2027.8 失效。id 由 :func:`ensure_host_device` 在平台加载
    之前缓存到 coordinator 上；缓存缺失时（例如平台加载之前的清理流程）现场查一次。

    返回值保证是**当下确实存在**的设备 id：``via_device_id`` 指向不存在的设备时，
    HA 会直接拒收这台子设备（``DeviceInfoError``，实体不会生成），宁可这次不挂父设备。
    """
    hass = getattr(coordinator, "hass", None)
    entry_id = getattr(coordinator, "entry_id", None)
    cached = getattr(coordinator, "host_device_id", None)
    if hass is None or not entry_id:
        return cached or None      # 没有 hass（离线/早期调用）：只能信缓存
    try:
        from homeassistant.helpers import device_registry as dr

        registry = dr.async_get(hass)
    except Exception:  # pylint: disable=broad-except
        return cached or None
    if cached and _device_alive(registry, cached):
        return cached
    try:
        device = device_by_identifier(registry, host_identifier(coordinator),
                                      entry_id)
    except Exception:  # pylint: disable=broad-except - 查不到就不挂父设备
        return None
    device_id = getattr(device, "id", None)
    if not device_id:
        return None
    try:
        coordinator.host_device_id = device_id
    except Exception:  # pylint: disable=broad-except - 个别只读协调器
        pass
    return device_id


def _via_device_id(coordinator) -> dict:
    """``via_device_id`` 参数（注册表 id）；查不到主机设备就不挂父设备。"""
    host_id = host_device_id(coordinator)
    return {"via_device_id": host_id} if host_id else {}


def docker_identifier(coordinator) -> tuple[str, str]:
    """Docker 设备标识：**所有容器共用这一台**。"""
    return (DOMAIN, f"{coordinator.host}::docker")


def container_identifier(coordinator, container_id) -> tuple[str, str]:
    """早期「每个容器一台独立设备」的设备标识 —— 现在只用来清理这批遗留设备。"""
    return (DOMAIN, f"{coordinator.host}::docker::{container_id}")


def vm_identifier(coordinator, vm_id) -> tuple[str, str]:
    """单台虚拟机设备标识。"""
    return (DOMAIN, f"{coordinator.host}::vm::{vm_id}")


def vnc_host(coordinator) -> str:
    """从 coordinator.host 取出纯主机名（去掉 scheme / 端口 / 路径）。"""
    host = coordinator.host or ""
    parsed = urlparse(host if "//" in host else f"//{host}")
    return parsed.hostname or host


def _host_configuration_url(coordinator) -> str:
    """主机设备「访问」地址：Web 悬浮窗代理的签名链接，未就绪时回退直连地址。"""
    entry_id = getattr(coordinator, "entry_id", None)
    hass = getattr(coordinator, "hass", None)
    if entry_id and hass is not None:
        try:
            from .web_proxy import get_signed_url

            url = get_signed_url(hass, entry_id)
            if url:
                return url
        except Exception:  # pylint: disable=broad-except - 链接只是锦上添花
            pass
    return coordinator.host


def _sw_version(coordinator) -> str:
    return _data(coordinator).get("sw_version") or "Unknown"


def _host_model(coordinator) -> str:
    ext = _data(coordinator).get("ext") or {}
    verinfo = ext.get("verinfo") or {}
    arch = verinfo.get("arch") or verinfo.get("sysbit") or ""
    return f"iKuai {arch}".strip() if arch else "iKuai Router"


def host_device_info(coordinator) -> dict:
    """爱快主机设备（所有主机级实体共用）。"""
    return {
        "identifiers": {host_identifier(coordinator)},
        "name": _data(coordinator).get("device_name", "iKuai Router"),
        "manufacturer": "iKuai 爱快",
        "model": _host_model(coordinator),
        "sw_version": _sw_version(coordinator),
        "configuration_url": _host_configuration_url(coordinator),
    }


# 常见 Web 管理端口的优先顺序：一个容器映射了多个宿主机端口时，「访问」按钮
# 优先挑这些（挑中 443/8443 用 https），都没有就取最小端口。
_WEB_PORT_PREFERENCE = (443, 8443, 80, 8080, 8081, 8000, 8008, 8888, 9000,
                        3000, 5000, 9090, 10000)
_HTTPS_PORTS = (443, 8443)


def _add_port(ports: list, value) -> None:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return
    if 1 <= port <= 65535 and port not in ports:
        ports.append(port)


def _parse_port_spec(ports: list, spec) -> None:
    """从各种形态的"端口映射"数据里取出**宿主机端口**（字段随固件版本而变）。

    兼容：整数；"8080"；"8080:80"；"0.0.0.0:8080->80/tcp"；
    ["8080:80", ...]；[{"host":8080,"container":80}, ...]；
    {"80/tcp":[{"HostPort":"8080"}]}（docker inspect 风格）。
    关键是只取**宿主机一侧**（冒号/箭头左边），不能把容器内端口也当成访问端口。
    """
    if spec is None or isinstance(spec, bool):
        return
    if isinstance(spec, int):
        _add_port(ports, spec)
        return
    if isinstance(spec, str):
        for chunk in spec.split(","):
            chunk = chunk.strip().split("/")[0].strip()
            if not chunk:
                continue
            if "->" in chunk:                       # 0.0.0.0:8080->80
                host_side = chunk.split("->", 1)[0]
                match = re.search(r"(\d{1,5})\s*$", host_side)
                if match:
                    _add_port(ports, match.group(1))
            elif ":" in chunk:                     # 8080:80 / :::8080
                host_side = chunk.rsplit(":", 1)[0]
                match = re.search(r"(\d{1,5})\s*$", host_side)
                if match:
                    _add_port(ports, match.group(1))
            elif chunk.isdigit():
                _add_port(ports, chunk)
        return
    if isinstance(spec, dict):
        for value in spec.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _add_port(ports, item.get("HostPort")
                                  or item.get("host_port") or item.get("host")
                                  or item.get("public") or item.get("src"))
                    elif isinstance(item, (int, str)):
                        _add_port(ports, item)
            elif isinstance(value, dict):
                _add_port(ports, value.get("HostPort") or value.get("host_port")
                          or value.get("host") or value.get("public"))
            elif isinstance(value, (int, str)) and str(value).strip().isdigit():
                _add_port(ports, value)
        return
    if isinstance(spec, (list, tuple)):
        for item in spec:
            _parse_port_spec(ports, item)


def container_host_ports(container: dict) -> list:
    """解析容器映射到**爱快宿主机**上的端口列表（去重、升序）。"""
    row = container or {}
    ports: list = []
    # 1) 标量字段
    for key in ("web_port", "host_port", "hostport", "http_port"):
        value = row.get(key)
        if value is not None and str(value).strip().isdigit():
            _add_port(ports, value)
    # 2) 各种集合字段名（不同固件/插件版本叫法不一）
    for key in ("ports", "host_ports", "hostports", "publish_ports",
                "published_ports", "port_bindings", "portmaps", "port_maps",
                "bindings", "port_forwards"):
        _parse_port_spec(ports, row.get(key))
    return sorted(ports)


def container_web_url(coordinator, container: dict):
    """返回 (首选访问URL, 全部宿主机端口)；没有任何映射端口时返回 (None, [])。"""
    ports = container_host_ports(container)
    if not ports:
        return None, ports
    chosen = next((p for p in _WEB_PORT_PREFERENCE if p in ports), ports[0])
    scheme = "https" if chosen in _HTTPS_PORTS else "http"
    return f"{scheme}://{vnc_host(coordinator)}:{chosen}/", ports


def docker_device_info(coordinator) -> dict:
    """Docker 设备：**所有容器共用一台**（容器开关 + 容器汇总传感器都挂这里）。

    单个容器的映射端口 / Web 访问地址不再写进设备，而是写在该容器开关的
    状态属性里（一个设备只能有一个「访问」链接，多个容器会互相覆盖）。
    """
    info = {
        "identifiers": {docker_identifier(coordinator)},
        "name": f"{_host_name(coordinator)} Docker",
        "manufacturer": "iKuai 爱快",
        "model": "iKuai Docker",
        "sw_version": _sw_version(coordinator),
    }
    info.update(_via_device_id(coordinator))
    return info


def vm_device_info(coordinator, vm: dict, console_url: str | None = None) -> dict:
    """单台虚拟机设备；有控制台链接时挂到设备页「访问」上。

    设备名带「虚拟机」前缀（与 esxi_stats 的虚拟机设备命名一致），
    这样设备列表里一眼能区分"主机"与"虚拟机"。
    """
    vm = vm or {}
    info = {
        "identifiers": {vm_identifier(coordinator, vm.get("id"))},
        "name": f"虚拟机 {vm.get('name') or vm.get('id')}",
        "manufacturer": "iKuai 爱快",
        "model": vm.get("system") or "iKuai 虚拟机",
        "sw_version": _sw_version(coordinator),
    }
    info.update(_via_device_id(coordinator))
    if console_url:
        info["configuration_url"] = console_url
    return info


def auth_identifier(coordinator) -> tuple[str, str]:
    """认证账号汇总设备标识（所有拨号账号共用一台设备）。"""
    return (DOMAIN, f"{coordinator.host}::auth")


def auth_device_info(coordinator) -> dict:
    """认证账号设备：PPPoE / PPTP / L2TP / OVPN 账号都挂在这一台下面。

    每个账号在该设备下有两个实体：``<账号> 在线``（在线情况）与
    ``<账号> 账号信息``（账号配置详情）—— 账号增删只增减实体，不会新增设备。
    """
    info = {
        "identifiers": {auth_identifier(coordinator)},
        "name": f"{_host_name(coordinator)} 认证账号",
        "manufacturer": "iKuai 爱快",
        "model": "iKuai 认证账号",
        "sw_version": _sw_version(coordinator),
    }
    info.update(_via_device_id(coordinator))
    return info


def auth_entity_slug(account: dict, fallback: str = "") -> str:
    """认证账号的实体 ID 片段：优先用户名（自动化里好认好写）。

    中文用户名转不出 slug，退回账号 id（保证实体 ID 里不出现拼音，也不会撞车）。
    """
    account = account or {}
    return (entity_id_slug(account.get("username"))
            or entity_id_slug(account.get("id"))
            or entity_id_slug(fallback))


def dhms(seconds) -> str | None:
    """秒 → 「3天02:05:11」这种时长文本。"""
    try:
        total = max(0, int(seconds))
    except (TypeError, ValueError):
        return None
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    text = f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{days}天{text}" if days else text


def stamp_ts(value) -> str | None:
    """时间戳 → 本地时间文本（0 / 空 / 非法值都不显示）。"""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return None


def human_bytes(value) -> str | None:
    """字节数 → 人类可读文本（原始数值同时留在属性里）。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024 or unit == "TB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.2f} {unit}"
        num /= 1024
    return None


# 爱快 acl_mac 的生效星期字段：1=周一 … 7=周日
_WEEK_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# 不进属性的原始字段：comment:1 是爱快返回里的占位残留；
# mac 已经以 mac_address 输出，不重复给两份。
_MAC_ACL_SKIP = ("comment:1", "mac")


def week_text(week) -> str | None:
    """爱快的 week 字段（``"1234567"``）→ 中文文本（``"每天"`` / ``"周一、周二"``）。"""
    days = sorted({int(d) for d in str(week or "") if d in "1234567"})
    if not days:
        return None
    if len(days) == 7:
        return "每天"
    return "、".join(_WEEK_NAMES[d - 1] for d in days)


# 爱快 WAN 口的入网方式（internet 字段）
_WAN_INTERNET_TEXT = {0: "静态IP", 1: "DHCP", 2: "PPPoE/ADSL"}


def wan_internet_text(value) -> str | None:
    """WAN 口入网方式（``internet`` 字段）→ 中文文本（取不到返回 None）。"""
    try:
        mode = int(value)
    except (TypeError, ValueError):
        return None
    return _WAN_INTERNET_TEXT.get(mode, "未知")


def dhms_from_ts(value) -> tuple:
    """线路的 ``updatetime``（unix 秒）→ ``(上线秒数, 可读文本)``。

    取不到 / 非法时间戳时返回 ``(0, None)``；文本由 :func:`dhms` 生成。
    """
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return 0, None
    if ts <= 0:
        return 0, None
    seconds = max(0, int(datetime.datetime.now().timestamp()) - ts)
    return seconds, dhms(seconds)


def mac_acl_entry(row: dict) -> dict:
    """一条 MAC 访问控制（黑名单，``acl_mac``）记录 → 展示用属性（空值不输出）。

    「每条目开关」与「拉黑/放行按钮上的黑名单清单」共用这一份字段映射，
    免得同一批字段名在两处各写一遍。``expires`` 为 0 表示永不过期（此时不给文本）。
    """
    row = row if isinstance(row, dict) else {}
    attrs = {
        "id": row.get("id"),
        "mac_address": row.get("mac"),
        "comment": row.get("comment"),
        "enabled": row.get("enabled"),      # 接口原值 yes/no
        "time": row.get("time"),            # 生效时段，如 00:00-23:59
        "week": row.get("week"),            # 生效星期，1=周一 … 7=周日
        "week_text": week_text(row.get("week")),
        "expires": row.get("expires"),      # 0 = 永不过期
        "expires_text": stamp_ts(row.get("expires")),
    }
    # 将来固件多返回别的字段就自动带出来（前向兼容）
    attrs.update({k: v for k, v in row.items()
                  if k not in attrs and k not in _MAC_ACL_SKIP
                  and v not in ("", None)})
    return {k: v for k, v in attrs.items() if v not in ("", None)}


def mask_cardid(value) -> str | None:
    """身份证号只留后 4 位：状态属性会写进 recorder，不做全量暴露。"""
    text = str(value or "")
    if not text:
        return None
    return f"{'*' * max(0, len(text) - 4)}{text[-4:]}"


def ensure_host_device(hass, entry, coordinator) -> None:
    """先建出爱快主机设备，并把它的注册表 id 缓存到 coordinator 上。

    虚拟机 / Docker / 认证账号设备都挂在主机设备下面（``via_device_id``），而各平台是
    并行加载的，因此这里在转发平台之前先把主机设备建好：既要让父设备先存在，也要拿到
    它的 **注册表 id** —— 新接口只认 ``via_device_id``（id），不再接受标识符形式的
    ``via_device``。
    """
    from homeassistant.helpers import device_registry as dr

    info = host_device_info(coordinator)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers=info["identifiers"],
        name=info["name"],
        manufacturer=info["manufacturer"],
        model=info["model"],
        sw_version=info["sw_version"],
    )
    coordinator.host_device_id = device.id


def _is_live(hass, entity_id) -> bool:
    """该实体当前还在状态机里吗（注册表清理要避开活实体）。"""
    states = getattr(hass, "states", None)
    getter = getattr(states, "get", None)
    if not callable(getter):
        return False
    try:
        return getter(entity_id) is not None
    except Exception:  # pylint: disable=broad-except
        return False


def cleanup_stale_auth_registry(hass, entry, coordinator) -> None:
    """清理早期"每个认证账号一台设备"遗留的实体注册项与设备。

    现在的形态：**全部账号共用一台「认证账号」设备**，每个账号两个实体
    （``<账号> 在线`` / ``<账号> 账号信息``）。老版本的实体 unique_id 是
    ``ikuai_auth_<账号>_<主机>``，与新版（``..._online_<主机>`` / ``..._info_<主机>``）
    不同，升级后会变成孤儿，还占着固定实体 ID —— 结果新版实体被挤成 ``xxx_2``，
    设备列表里也一直挂着"一账号一台"的旧设备（用户看到的"怎么还是两个设备"）。

    因此这里在平台加载**之前**把老注册项与老设备删掉；只删本条目名下、
    unique_id 不是新版形态的认证账号注册项，以及删完后没有实体挂着的认证账号设备。

    两条护栏（本函数在平台加载**之后**还会再跑一遍搬设备，那时实体已经建好了）：

    * ``keep_ids``：集成里另有一些 ``ikuai_auth_`` 开头但**不是账号**的固定实体
      （「本地认证服务」``sensor.ikuai_auth_server``），只按前缀+后缀判断会把它们
      误当"老账号条目"删掉，必须按 unique_id 显式放行；
    * **活着的实体一律不删**：实体还在状态机里就说明它是当前代码产出的（老孤儿条目
      因为禁用根本不会被创建），删掉注册项只会留下"有状态、没注册项"的幽灵实体
      —— 前端看不到、``sensor.ikuai_all_entities`` 也列不出来。
    """
    from homeassistant.helpers import device_registry as dr

    entity_reg = er.async_get(hass)
    device_reg = dr.async_get(hass)
    host = coordinator.host
    new_suffixes = (f"_online_{host}", f"_info_{host}")
    keep_ids = {f"{DOMAIN}_auth_server_{host}"}

    removed_entities = []
    for reg_entry in er.async_entries_for_config_entry(entity_reg, entry.entry_id):
        unique_id = reg_entry.unique_id or ""
        if not unique_id.startswith(f"{DOMAIN}_auth_"):
            continue
        if unique_id.endswith(new_suffixes) or unique_id in keep_ids:
            continue      # 新版账号实体 / 非账号的固定实体：留着
        if _is_live(hass, reg_entry.entity_id):
            continue      # 还活着 ⇒ 属于当前代码，删不得
        entity_reg.async_remove(reg_entry.entity_id)
        removed_entities.append(reg_entry.entity_id)

    removed_devices = []
    shared = auth_identifier(coordinator)
    moved = []
    for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
        identifiers = set(device.identifiers or ())
        stale = [ident for ident in identifiers
                 if ident[0] == DOMAIN and ident[1].startswith(f"{host}::auth::")]
        if not stale:
            continue      # 公用设备（<host>::auth）与其它设备不动
        # 还挂在这台老设备上的新版认证账号实体：先搬回公用设备，再判空
        # （只搬``..._online_<主机>`` / ``..._info_<主机>`` 这两种账号实体；
        #  其它 ``ikuai_auth_`` 开头的固定实体不属于「认证账号」设备）
        for reg_entry in er.async_entries_for_config_entry(entity_reg, entry.entry_id):
            if not (reg_entry.unique_id or "").endswith(new_suffixes):
                continue
            if reg_entry.device_id != device.id:
                continue
            target = next((d for d in dr.async_entries_for_config_entry(
                device_reg, entry.entry_id) if shared in set(d.identifiers or ())), None)
            if target is None:
                target = device_reg.async_get_or_create(
                    config_entry_id=entry.entry_id, identifiers={shared},
                    **_via_device_id(coordinator))
            entity_reg.async_update_entity(reg_entry.entity_id, device_id=target.id)
            moved.append(reg_entry.entity_id)
        if er.async_entries_for_device(entity_reg, device.id, include_disabled_entities=True):
            continue      # 还有实体挂着（例如别的集成的实体）：不动
        device_reg.async_remove_device(device.id)
        removed_devices.append(stale[0][1])
    if moved:
        _LOGGER.info("iKuai 认证账号实体重新挂到公用设备：%s", moved)

    if removed_entities or removed_devices:
        _LOGGER.info(
            "iKuai 已清理早期「一个认证账号一台设备」的残留：实体 %s / 设备 %s"
            "（现在所有账号都在同一台「认证账号」设备下）",
            removed_entities, removed_devices)


def cleanup_stale_container_devices(hass, entry, coordinator) -> None:
    """把早期「每个 Docker 容器一台独立设备」遗留的设备清干净。

    现在所有容器共用一台 Docker 设备（``<host>::docker``）：容器开关的实体 ID 与
    unique_id 都没变，只是重新归到那台设备上。这个函数在平台加载**之后**做两件事：

    1. 万一某个容器开关的注册项还挂在老的 ``<host>::docker::<容器id>`` 设备上
       （平台加载时没走 device_info 刷新的情况），把它重新挂到公用 Docker 设备；
    2. 删掉已经没有任何实体挂着的 ``<host>::docker::…`` 设备（也就是设备页里
       那一堆空壳「容器 xxx」）。

    公用 Docker 设备（``<host>::docker``）与虚拟机设备一律不动。
    """
    from homeassistant.helpers import device_registry as dr

    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    shared = docker_identifier(coordinator)
    prefix = f"{coordinator.host}::docker::"

    def shared_device():
        for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
            if shared in set(device.identifiers or ()):
                return device
        return None

    # 1) 容器开关重新归到公用 Docker 设备（公用设备不存在就按需建出来）
    target = shared_device()
    if target is None:
        target = device_reg.async_get_or_create(
            config_entry_id=entry.entry_id, identifiers={shared},
            **_via_device_id(coordinator))
    moved = []
    for reg_entry in er.async_entries_for_config_entry(entity_reg, entry.entry_id):
        if not (reg_entry.unique_id or "").startswith(f"{DOMAIN}_dockerswitch_"):
            continue
        device = device_reg.async_get(reg_entry.device_id) if reg_entry.device_id else None
        if device is None or shared in set(device.identifiers or ()):
            continue                    # 没设备 / 已经在公用设备上
        if not any(ident[0] == DOMAIN and ident[1].startswith(prefix)
                   for ident in set(device.identifiers or ())):
            continue                    # 挂的不是老容器设备：不动
        entity_reg.async_update_entity(reg_entry.entity_id, device_id=target.id)
        moved.append(reg_entry.entity_id)

    # 2) 删掉空壳的老容器设备
    removed = []
    for device in dr.async_entries_for_config_entry(device_reg, entry.entry_id):
        stale = [ident for ident in set(device.identifiers or ())
                 if ident[0] == DOMAIN and ident[1].startswith(prefix)]
        if not stale:
            continue                    # 公用 Docker 设备（<host>::docker）不碰
        if er.async_entries_for_device(entity_reg, device.id, include_disabled_entities=True):
            continue                    # 还有实体挂着：说明是在用的设备
        device_reg.async_remove_device(device.id)
        removed.append(stale[0][1])
    if moved or removed:
        _LOGGER.info(
            "iKuai Docker 设备归并完成：%d 个容器实体重新挂到公用 Docker 设备，"
            "清理遗留设备 %s", len(moved), removed)


def entity_id_slug(value) -> str:
    """把一段文本规范成 entity_id 片段：只保留 [a-z0-9_]，不做拼音转换。

    故意不用 ``homeassistant.util.slugify``（它会把中文转成拼音，正是要避免的）。
    """
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")


def internal_key_slug(key: str) -> str:
    """内部 key（``ikuai_cpu``）→ 实体 ID 片段（``cpu``）。"""
    text = str(key or "")
    return text[len(ENTITY_ID_PREFIX):] if text.startswith(ENTITY_ID_PREFIX) else text


class ForcedEntityIdMixin:
    """固定实体 ID 为短英文（``<domain>.ikuai_<suffix>``），显示名称不变。

    Home Assistant 的实体注册表优先于代码里设置的 entity_id：首次创建时直接生效；
    对已经注册过的旧 ID（历史上的拼音 ID，如 sensor.ikuai_cpuzhan_yong），需要在
    实体加入后调用 ``async_migrate_entity_id()`` 迁移一次（注册表改名，历史与统计
    随之保留，实体不变）。
    """

    _forced_entity_id: str = ""

    def __init_subclass__(cls, **kwargs):
        """把所有实体的 ``extra_state_attributes`` 输出统一做一次 URL 编码还原。

        爱快对备注 / 用户名 / SSID / 网址这类文本做了 URL 编码（``Apple%20TV``），
        逐个实体改容易漏，所以在子类定义时自动包一层：新写的实体只要照旧实现
        ``extra_state_attributes``，输出就自动还原（明文值不受影响）。
        """
        super().__init_subclass__(**kwargs)
        attr = cls.__dict__.get("extra_state_attributes")
        if attr is None:
            return
        if isinstance(attr, property):
            getter = attr.fget
            if getter is None or getattr(getter, "_url_decoded", False):
                return

            @functools.wraps(getter)
            def wrapped(self):
                return normalize_attributes(getter(self))

            cls.extra_state_attributes = property(wrapped)
        elif callable(attr):
            if getattr(attr, "_url_decoded", False):
                return

            @functools.wraps(attr)
            def wrapped(self, *args, **kwargs):
                return normalize_attributes(attr(self, *args, **kwargs))

            cls.extra_state_attributes = wrapped
        else:
            return
        wrapped._url_decoded = True

    def _setup_forced_entity_id(self, platform_domain: str, suffix: str) -> None:
        """设置固定实体 ID（形如 <domain>.ikuai_<英文后缀>）。"""
        suffix = entity_id_slug(suffix)
        if not suffix:
            return
        self._forced_entity_id = f"{platform_domain}.{ENTITY_ID_PREFIX}{suffix}"
        self.entity_id = self._forced_entity_id

    @property
    def _display_name(self) -> str:
        return (getattr(self, "name", None) or getattr(self, "_attr_name", None)
                or self._forced_entity_id)

    @callback
    def async_migrate_entity_id(self) -> None:
        """把已注册的旧实体 ID 迁移到固定 ID（注册表 ID 优先于代码中设置的 ID）。

        目标 ID 被**别的集成**占用时（典型情况：同时装着旧的 ikuai_router，它占着
        ``sensor.ikuai_cpu``、``switch.ikuai_vm_win7`` 这类同样的英文 ID），退一步用
        ``<目标>_2``、``_3`` … 兜底 —— 保证本集成的实体 ID 里不出现拼音；等那个
        集成被删掉后，下次重载会自动回到干净的固定 ID。
        """
        target = getattr(self, "_forced_entity_id", "")
        current = self.entity_id
        if not target or not current or current == target:
            return
        hass = getattr(self, "hass", None)
        if hass is None:
            return
        try:
            registry = er.async_get(hass)
            occupant = registry.async_get(target)
            if occupant is None:
                new_id = target
            elif current.startswith(f"{target}_"):
                # 已经是 <目标>_N 的兜底形态：不要来回改名
                return
            else:
                new_id = ""
                for index in range(2, 20):
                    candidate = f"{target}_{index}"
                    if registry.async_get(candidate) is None:
                        new_id = candidate
                        break
                if not new_id:
                    _LOGGER.warning(
                        "iKuai 实体「%s」的固定实体 ID %s 已被占用且找不到可用兜底，"
                        "保留当前 %s", self._display_name, target, current)
                    return
                _LOGGER.info(
                    "iKuai 实体「%s」的固定实体 ID %s 被 %s 集成占用，暂用 %s"
                    "（删掉该集成条目后重载即可回到 %s）",
                    self._display_name, target, getattr(occupant, "platform", "?"),
                    new_id, target)
            registry.async_update_entity(current, new_entity_id=new_id)
            # 显式回写：同一生命周期内后续代码（自动化引用/属性）应拿到新 ID
            self.entity_id = new_id
            if new_id == target:
                _LOGGER.info("iKuai 实体「%s」的实体 ID 已由 %s 迁移为 %s",
                             self._display_name, current, new_id)
        except Exception as err:  # pylint: disable=broad-except
            _LOGGER.warning("iKuai 实体 ID 迁移 %s -> %s 失败: %s", current, target, err)


class IKuaiEntity(ForcedEntityIdMixin, CoordinatorEntity):
    """所有 iKuai 实体的公共基类（默认挂爱快主机设备）。"""

    _attr_has_entity_name = True

    def __init__(self, coordinator):
        super().__init__(coordinator)
        self.host = coordinator.host
        self.entry_id = getattr(coordinator, "entry_id", None)

    @property
    def device_info(self):
        return host_device_info(self.coordinator)

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success and self.coordinator.data)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # 注册表里的旧 ID（历史拼音 ID）在这里迁移到固定英文 ID
        self.async_migrate_entity_id()


class DynamicEntityManager:
    """
    根据 coordinator 数据动态增删实体。
    items_getter() -> dict[unique_suffix, item_dict]
    entity_cls(coordinator, suffix, item)
    """

    def __init__(self, coordinator, async_add_entities, entity_cls, items_getter):
        self.coordinator = coordinator
        self._add = async_add_entities
        self._cls = entity_cls
        self._getter = items_getter
        self._entities: dict = {}
        self.coordinator.async_add_listener(self._sync)
        self._sync()

    def _sync(self):
        if not self.coordinator.data:
            return
        try:
            items = self._getter(self.coordinator.data) or {}
        except Exception as e:
            _LOGGER.debug("动态实体枚举失败: %s", e)
            return
        current = set(self._entities.keys())
        wanted = set(items.keys())

        added = []
        for suffix in wanted - current:
            entity = self._cls(self.coordinator, suffix, items[suffix])
            self._entities[suffix] = entity
            added.append(entity)
        if added:
            self._add(added, False)

        for suffix in current - wanted:
            entity = self._entities.pop(suffix, None)
            if entity is not None:
                try:
                    self.coordinator.hass.async_create_task(entity.async_remove())
                except Exception:
                    pass
        # 更新保留实体的行数据
        for suffix in wanted & current:
            item = items[suffix]
            fn = getattr(self._entities[suffix], "update_item", None)
            if callable(fn):
                fn(item)
