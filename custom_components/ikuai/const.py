"""Constants for the iKuai integration."""

DOMAIN = "ikuai"

######### CONF KEY
CONF_USERNAME = "username"
CONF_PASSWD = "passwd"
CONF_PASS = "pas"
CONF_HOST = "host"
CONF_TOKEN_EXPIRE_TIME = "token_expire_time"
COORDINATOR = "coordinator"
LOGHUB = "loghub"
CONF_UPDATE_INTERVAL = "update_interval_seconds"
CONF_LOG_INTERVAL = "log_interval_seconds"
CONF_CUSTOM_SWITCHES = "custom_switches"
CONF_ACT_BUFFER = "act_buffer"
CONF_TRACKER_CONFIG = "tracker_config"
CONF_SOURCE_MODE = "source_mode"
MODE_UI = "mode_ui"
MODE_CONST = "mode_const"

UNDO_UPDATE_LISTENER = "undo_update_listener"

##### iKuai URL
LOGIN_URL = "/Action/login"
ACTION_URL = "/Action/call"

##### Web 悬浮窗反向代理
PROXY_PAGE_PREFIX = "/ikuai/web"
PROXY_STATIC_PREFIX = "/ikuai/static"
# 浏览器地址栏 origin 心跳端点（标准 HA 鉴权 GET；后端读请求 Host 头学习外部地址）
PROXY_ORIGIN_PREFIX = "/ikuai/api/origin"
PROXY_COOKIE_NAME = "ikuai_web_sess"
PROXY_SIGN_EXPIRY_HOURS = 24
PROXY_COOKIE_MAX_AGE = 24 * 3600

# 注入 HA 前端的悬浮窗脚本（www_ikuai/frontend/embed.js）版本号，拼在 URL 后面。
# 改 embed.js 后必须同步改这里与文件里的 window.__ikuaiWebEmbed，否则浏览器会继续用缓存里的旧脚本。
EMBED_ASSET_VERSION = "2026.10.12.3"

##### 虚拟机远程控制台（直接复用爱快固件自带的 noVNC 页面与 /Action/vnc 代理）
# 固件原生控制台静态页：/static/vnc/index.html?<vnc_port>，内部用标准 noVNC 连
# 主机同源的 ws://<路由器>/Action/vnc?port=<vnc_port>（nginx 校验后台登录 cookie
# 后桥到 QEMU VNC）。集成通过 Web 悬浮窗反向代理该页面与该 WebSocket，并在页面里
# 注入一小段脚本把 WS 指向 HA 同源代理路径 + 自动填 VNC 密码，得到与官方后台完全
# 一致、且运行在 HA 悬浮窗内的控制台（替代早期 HA 自建 RFB 字节桥方案）。
# 历史路径前缀保留（侧边栏面板仍挂在它下面，设备旧链接识别也用它做标记）。
CONSOLE_PAGE_PREFIX = "/ikuai/console"
# 前端静态资源（面板自定义元素 panel.js；无秘密，可公开）
CONSOLE_ASSET_PREFIX = f"{CONSOLE_PAGE_PREFIX}/assets"
# 面板等前端资源目录（相对集成根目录）
CONSOLE_FRONTEND_DIR = "frontend/vnc"
# 前端资源版本：拼在 assets URL 后面（?v=...）。
# 静态资源带 max-age=3600，改前端后必须改这个版本号，否则浏览器会拿旧脚本配新页面。
CONSOLE_ASSET_VERSION = "2026.10.12.5"
# 设备链接签名有效期 / 全量重签间隔（小时）
CONSOLE_SIGN_EXPIRY_HOURS = 24
# 爱快原生 VNC：静态页路径（代理前缀下）与 WS 路径
VNC_NATIVE_PAGE_TAIL = "static/vnc/index.html"
VNC_NATIVE_WS_TAIL = "Action/vnc"

# 全局「远程控制」开关默认值（无历史状态时）与实体名
REMOTE_CONTROL_DEFAULT = True
REMOTE_CONTROL_NAME = "远程控制"

# 虚拟机「屏幕画面」image 实体：抓帧超时（秒）与同一实体的抓帧节流（秒）
VNC_SNAPSHOT_TIMEOUT = 10
VNC_SNAPSHOT_CACHE_SECONDS = 5

# 侧边栏「iKuai 控制台」面板已整体移除：控制台只在实体 / 卡片的悬浮窗里打开，
# 不在 HA 侧边栏留入口（旧版本注册过它，重载时会「出现一下又消失」）。
# 保留这个 url_path：加载与卸载时用它清理残留在 HA 面板注册表里的旧条目。
CONSOLE_PANEL_URL_PATH = "ikuai-console"

# 扩展数据轮询分频：主轮询 N 次才完整抓取一次扩展模块（核心状态每次都抓）
EXT_FETCH_EVERY = 3
# 列表接口的 limit 是"偏移,条数"，而且**单次只返回前若干条**（给 0,10000 也拿不全，
# 实测要 0,100 / 100,100 / 200,100 … 逐页取），所以列表类抓取一律翻页：
#   LIST_PAGE_SIZE 每次请求要多少条（爱快单次上限就是 100）
#   LIST_MAX_PAGES 最多翻几页（防呆，终端/容器特别多时不要无限翻）
# 见 data_fetcher._show 与 config_flow._fetch_lan_info
LIST_PAGE_SIZE = 100
LIST_MAX_PAGES = 10
# 列表类查询的默认上限：一律一万条兜底，防止分页截断丢数据
LIST_LIMIT = "0,10000"
# 全量列表抓取（语义别名，与 LIST_LIMIT 相同）
LIST_FETCH_LIMIT = "0,10000"
COUNT_LIMIT = "0,1"
# 规则明细表（用于计数传感器属性）的抓取上限；属性只展示前30条，但抓取不截断
RULE_ROW_LIMIT = "0,10000"

# 全局 HTTP 并发上限。软路由的 Web 接口不能一次打太满（突发并发时它偶发挂起
# 连接），但 3 又太保守：启动/扩展轮有 40+ 个请求，会排成十几批，肉眼可见地慢。
# 6 是折中值（首轮约 4 批跑完）。
HTTP_CONCURRENCY = 6

# 认证账号：整表（pppuser）没带回某个在线账号时，最多再按账号单独查几次详情。
# 限量是为了账号很多（或被限流）时不要把爱快的 Web 接口打满。
ACCOUNT_DETAIL_FALLBACK_MAX = 5

# 在线终端的「设备详情」：一台设备两个请求，都是按 IP 逐个查
#   monitor_lanip + TYPE=terminal_flow + ip        → 应用流量明细
#   monitor_lanip + TYPE=conn,conn_num + ip        → 连接明细
# 所以每轮扩展抓取要限量 + 轮换覆盖（优先没抓过的、其次最久没更新的），终端很多
# 时不会把爱快接口打满（全局并发只有 3）；终端少时一轮就能全抓完。
TERMINAL_DETAIL_MAX = 10
# 每台设备的详情里保留几个应用：按上下行总量降序，全为 0 的应用直接丢弃
# （爱快固定返回十来个应用分类，其中大多数该设备根本没走流量）。
TERMINAL_APP_MAX = 8
# 每台设备保留几条连接：按上下行总量降序取前 N（连接数由接口 conn_num 单独给出，
# 真实连接可能有几百条，全塞进属性会让属性膨胀到几百 KB）。
TERMINAL_CONN_KEEP = 10

# ---------------------------------------------------------------------------
# 透传 API（可选功能，默认关闭）
#
# 纯通道：请求体就是爱快原生载荷（func_name/action/param），响应体就是爱快的
# 原始响应，所以爱快原生所有接口都能用。开关（switch.ikuai_api）打开后可用，
# 关闭时视图直接 403、一个上游请求都不发。
#
# 鉴权三选一：HA 登录态 / ?key=（见 sensor.ikuai_api_key，1 小时自动轮换）/
#             X-IKuai-Key 请求头。
# ---------------------------------------------------------------------------
API_CALL_PATH = "/ikuai/api/call"
API_VIEW_NAME = "ikuai:call"
# 开关实体（switch.ikuai_api）的名称与唯一 ID 后缀
API_SWITCH_NAME = "查询 API"
API_SWITCH_SUFFIX = "api"
# 默认关闭：新功能，且它等价于爱快后台的操作权限（写操作也放行），要用户主动开
API_ENABLED_DEFAULT = False
# 一次请求最多带几个调用（分页也靠它：10 个 limit 段 = 最多 1000 条）
API_CALLS_MAX = 10
# API key：有效期（秒，整点轮换）与长度；旧 key 再宽限一个周期
API_KEY_TTL_SECONDS = 3600
API_KEY_LEN = 16
# 密钥传感器实体（sensor.ikuai_api_key）
API_KEY_SENSOR_NAME = "API 密钥"
API_KEY_SENSOR_SUFFIX = "api_key"

# ---------------------------------------------------------------------------
# 「采集日志」开关（switch.ikuai_log_poll）
#
# 关掉后**停止日志定时采集**（12 类日志本来每分钟抓一次）：sensor.ikuai_log_stats /
# sensor.ikuai_log_url_log 变为「不可用」，ikuai_log 事件与日志簿也不再产生；
# 显式调用的服务（log_get / log_clear）不受影响。
# ---------------------------------------------------------------------------
LOG_SWITCH_NAME = "采集日志"
LOG_SWITCH_SUFFIX = "log_poll"
LOG_POLL_DEFAULT = True

# 同类拨号/××× 线路超过该条数时，单线传感器/按钮整合为汇总实体，操作走悬浮窗
DIAL_INLINE_MAX = 3


### 基础传感器（键名保持历史兼容，勿改名）
SENSOR_TYPES = {
    # 原 "ikuai_uptime"（启动时长文本）已删除：与 "ikuai_uptime_seconds"
    # （启动秒数，带 duration 设备类）完全同源，后者是 system_info 聚合实体的成员。
     "ikuai_cpu": {
        "icon": "mdi:cpu-64-bit",
        "label": "CPU占用",
        "name": "CPU占用",
        "unit_of_measurement": "%",
        "state_class": "measurement",
    },
     "ikuai_cputemp": {
        "icon": "mdi:thermometer",
        "label": "CPU温度",
        "name": "CPU温度",
        "unit_of_measurement": "°C",
        "device_class": "temperature",
        "state_class": "measurement",
    },
    "ikuai_memory": {
        "icon": "mdi:memory",
        "label": "内存占用",
        "name": "内存占用",
        "unit_of_measurement": "%",
        "state_class": "measurement",
    },
    "ikuai_online_user": {
        "icon": "mdi:account-multiple",
        "label": "在线终端数",
        "name": "在线终端数",
        "unit_of_measurement": "个",
        "state_class": "measurement",
    },
    # 原 "ikuai_ap_online"（AC 上报的 AP 数）已删除：与 ext 的 "ikuai_ap_total"
    # （按 ac_list 实测条数）语义重叠，后者更可靠，并入 lan_info 聚合实体。
    "ikuai_total_up": {
        "icon": "mdi:upload-network",
        "label": "上传总量",
        "name": "上传总量",
        "unit_of_measurement": "GB",
        "state_class": "total_increasing",
    },
    "ikuai_total_down": {
        "icon": "mdi:download-network",
        "label": "下载总量",
        "name": "下载总量",
        "unit_of_measurement": "GB",
        "state_class": "total_increasing",
    },
    "ikuai_upload": {
        "icon": "mdi:wifi-arrow-up",
        "label": "上传速度",
        "name": "上传速度",
        "unit_of_measurement": "MB/s",
        "device_class": "data_rate",
        "state_class": "measurement",
    },
    "ikuai_download": {
        "icon": "mdi:wifi-arrow-down",
        "label": "下载速度",
        "name": "下载速度",
        "unit_of_measurement": "MB/s",
        "device_class": "data_rate",
        "state_class": "measurement",
    },
    "ikuai_connect_num": {
        "icon": "mdi:lan-connect",
        "label": "连接数",
        "name": "连接数",
        "unit_of_measurement": "个",
        "state_class": "measurement",
    },
    # 原 "ikuai_wan_ip" / "ikuai_wan_uptime"（只反映 default_route 那口）已删除：
    # 每口的 IP 与上线时长现在挂在 binary_sensor.ikuai_wan_N_online（「WAN x 状态」）上。
    # 本项是 DHCPv6 客户端拿到的 **WAN 侧 IPv6 地址**：原先进 wan_info 聚合实体，
    # 该聚合已删除，于是它回到独立传感器。
    "ikuai_wan6_ip": {
        "icon": "mdi:ip-network",
        "label": "WAN IPv6地址",
        "name": "WAN IPv6地址",
    },
    "ikuai_lan6_ip": {
        "icon": "mdi:ip-network",
        "label": "LAN IPv6地址",
        "name": "LAN IPv6地址",
        "entity_category": "diagnostic",
    },
    "ikuai_pppoe_count": {
        "icon": "mdi:account-network",
        "label": "PPPoE在线数量",
        "name": "PPPoE在线数量",
        "unit_of_measurement": "个",
        "state_class": "measurement",
    },
    "ikuai_pppoe_fail_count": {
        "icon": "mdi:account-alert",
        "label": "PPPoE失败数量",
        "name": "PPPoE失败数量",
        "unit_of_measurement": "个",
        "state_class": "measurement",
    },
    "ikuai_pptp_count": {
        "icon": "mdi:account-network",
        "label": "PPTP在线数量",
        "name": "PPTP在线数量",
        "unit_of_measurement": "个",
        "entity_category": "diagnostic",
    },
    "ikuai_pptp_fail_count": {
        "icon": "mdi:account-alert",
        "label": "PPTP失败数量",
        "name": "PPTP失败数量",
        "unit_of_measurement": "个",
        "entity_category": "diagnostic",
    },
    "ikuai_l2tp_count": {
        "icon": "mdi:account-network",
        "label": "L2TP在线数量",
        "name": "L2TP在线数量",
        "unit_of_measurement": "个",
        "entity_category": "diagnostic",
    },
    "ikuai_l2tp_fail_count": {
        "icon": "mdi:account-alert",
        "label": "L2TP失败数量",
        "name": "L2TP失败数量",
        "unit_of_measurement": "个",
        "entity_category": "diagnostic",
    },
}


### 扩展传感器：value(data, ext) 取值；attrs_key 指向 ext 中的附加属性块
def _v(path, default=None):
    """读取 ext 嵌套路径，如 ('basic', 0, 'switch_ntp')。"""
    def _get(data, ext):
        cur = ext
        for p in path:
            if isinstance(cur, dict):
                cur = cur.get(p)
            elif isinstance(cur, list) and isinstance(p, int) and len(cur) > p:
                cur = cur[p]
            else:
                return default
        return default if cur is None else cur
    return _get


def _ap_clients(_data, ext):
    """AP 总带机量（字段名随固件版本不同，多候选键兼容）。"""
    total = 0
    for row in ext.get("ac_list") or []:
        if not isinstance(row, dict):
            continue
        for key in ("online_user", "user_num", "client_count", "wireless_user", "access_user"):
            try:
                total += int(row.get(key) or 0)
            except (TypeError, ValueError):
                pass
    return total


def _cloud_bound(_data, ext):
    """爱快云是否已绑定：register.data[0].code 非空视为已绑定。"""
    block = ext.get("register") or {}
    rows = block.get("data") if isinstance(block, dict) else None
    return bool(isinstance(rows, list) and rows and rows[0].get("code"))


def _cache_running(_data, ext):
    """缓存加速是否有任一服务处于 running。"""
    status = (ext.get("cache_status") or {}).get("status") or []
    return any(isinstance(s, dict) and s.get("cache_status") == "running"
               for s in status)


EXT_SENSOR_TYPES = {
    # ---------- 系统 ----------
    "ikuai_uptime_seconds": {
        "name": "启动秒数", "icon": "mdi:timer-outline", "unit_of_measurement": "s",
        "device_class": "duration", "state_class": "measurement", "entity_category": "diagnostic",
        "value": lambda d, e: d.get("uptime_seconds"),
    },
    "ikuai_cpu_cores": {
        "name": "CPU核心数", "icon": "mdi:cpu-64-bit", "unit_of_measurement": "核",
        "entity_category": "diagnostic",
        # hardwareinfo.data.hardwareinfo.cpucores 是固件报的核心数；
        # cpulist 首个 "auto" 是调度策略不是核心，兜底计数时要减掉
        "value": lambda d, e: (
            (e.get("hardware", {}).get("hardwareinfo", {}) or {}).get("cpucores")
            or max(0, len(e.get("hardware", {}).get("cpucore_list", [])) - 1)) or None,
    },
    "ikuai_cpu_freq": {
        "name": "CPU频率", "icon": "mdi:speedometer", "unit_of_measurement": "MHz",
        "entity_category": "diagnostic",
        "value": lambda d, e: "".join(
            c for c in str((e.get("hardware", {}).get("hardwareinfo", {}) or {}
                           ).get("cpumhz") or "") if c.isdigit()) or None,
    },
    "ikuai_memory_total": {
        "name": "内存总量", "icon": "mdi:memory", "unit_of_measurement": "MB",
        "entity_category": "diagnostic",
        "value": lambda d, e: round((e.get("memory_raw", {}).get("total") or 0) / 1024, 1) or None,
    },
    "ikuai_memory_available": {
        "name": "可用内存", "icon": "mdi:memory", "unit_of_measurement": "MB",
        "entity_category": "diagnostic",
        "value": lambda d, e: round((e.get("memory_raw", {}).get("available") or 0) / 1024, 1) or None,
    },
    "ikuai_disk_size": {
        "name": "磁盘容量", "icon": "mdi:harddisk", "unit_of_measurement": "GB",
        "entity_category": "diagnostic",
        # ext.disk 是磁盘列表（每块盘的原始字节容量在 size），这里汇总所有物理盘
        "value": lambda d, e: round(sum(
            float(x.get("size") or 0) for x in (e.get("disk") or [])
            if isinstance(x, dict)) / 1024 / 1024 / 1024, 1) or None,
        "attrs_key": "disk",
    },
    # ---------- 网络 / 多拨 ----------
    "ikuai_pppoe_total": {
        "name": "多拨线路总数", "icon": "mdi:router-network", "unit_of_measurement": "条",
        "state_class": "measurement",
        "value": lambda d, e: len(e.get("wan_lines", [])),
        "attrs_key": "wan_lines_summary",
    },
    "ikuai_route_v4": {
        "name": "IPv4路由条数", "icon": "mdi:routes", "unit_of_measurement": "条",
        "entity_category": "diagnostic",
        "value": lambda d, e: (e.get("route_v4_total") or {}).get("total"),
        "attrs_key": "route_v4_total",
    },
    "ikuai_route_v6": {
        "name": "IPv6路由条数", "icon": "mdi:routes", "unit_of_measurement": "条",
        "entity_category": "diagnostic",
        "value": lambda d, e: (e.get("route_v6_total") or {}).get("total"),
        "attrs_key": "route_v6_total",
    },
    "ikuai_arp_total": {
        "name": "ARP表条目", "icon": "mdi:table", "unit_of_measurement": "条",
        "entity_category": "diagnostic",
        "value": lambda d, e: e.get("arp_total"),
        "attrs_key": "arp_rows",
    },
    "ikuai_arp_bound": {
        "name": "ARP已绑定数", "icon": "mdi:link-variant", "unit_of_measurement": "条",
        "entity_category": "diagnostic",
        "value": lambda d, e: e.get("arp_bound"),
        "attrs_key": "arp_bound_rows",
    },
    "ikuai_ipv6_neighbor": {
        "name": "IPv6邻居数", "icon": "mdi:ip-network", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: (e.get("ipv6_neighbor_total") or {}).get("total"),
        "attrs_key": "ipv6_neighbor_total",
    },
    # ---------- DHCP ----------
    "ikuai_dhcp_leases": {
        "name": "DHCP租约数", "icon": "mdi:ip-outline", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: e.get("dhcp_leases_total"),
        "attrs_key": "dhcp_leases",
    },
    "ikuai_dhcp6_leases": {
        "name": "DHCPv6租约数", "icon": "mdi:ip-outline", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: e.get("dhcp6_total"),
        "attrs_key": "dhcp6_leases",
    },
    "ikuai_dhcp_servers": {
        "name": "DHCP服务器数", "icon": "mdi:dns-outline", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: len(e.get("dhcp_servers", [])),
        "attrs_key": "dhcp_servers",
    },
    "ikuai_dhcp_static": {
        "name": "DHCP静态分配数", "icon": "mdi:ip-network", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        # dhcp_static 接口（static_total）才是真正的静态分配条数；
        # 旧版误用 arp options 的 dhcpd_arp（那是「兼容ARP」开关，不是条数）
        "value": lambda d, e: (e.get("dhcp_static") or {}).get("total"),
        "attrs_key": "dhcp_static",
    },
    "ikuai_dhcp_acl_mac": {
        "name": "DHCP-MAC黑白名单(IPv4)", "icon": "mdi:clipboard-list-outline",
        "unit_of_measurement": "条", "entity_category": "diagnostic",
        # mode: 0=黑名单模式, 1=白名单模式；属性含模式与全部条目
        "value": lambda d, e: (e.get("dhcp_acl_mac") or {}).get("total"),
        "attrs_key": "dhcp_acl_mac",
    },
    "ikuai_dhcp6_acl_mac": {
        "name": "DHCPv6-MAC黑白名单", "icon": "mdi:clipboard-list-outline",
        "unit_of_measurement": "条", "entity_category": "diagnostic",
        "value": lambda d, e: (e.get("dhcp6_acl_mac") or {}).get("total"),
        "attrs_key": "dhcp6_acl_mac",
    },
    "ikuai_online_user_v6": {
        "name": "IPv6在线终端数", "icon": "mdi:devices", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: e.get("online_v6_total"),
        "attrs_key": "lan_hosts_v6",
    },
    "ikuai_iface_fail": {
        "name": "线路体检异常数", "icon": "mdi:heart-pulse", "unit_of_measurement": "条",
        "value": lambda d, e: e.get("iface_fail"),
        "attrs_key": "iface_fail_rows",
    },
    # ---------- 业务 / 设备 ----------
    "ikuai_ppp_online": {
        "name": "VPN/拨号在线用户", "icon": "mdi:account-key", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: len(e.get("ppp_online", [])),
        "attrs_key": "ppp_online",
    },
    "ikuai_camera_count": {
        "name": "摄像头数量", "icon": "mdi:cctv", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: len(e.get("cameras", [])),
        "attrs_key": "cameras",
    },
    # 原 "ikuai_docker_count"（Docker容器数）已删除：容器总数与每台容器的完整明细
    # 都挪到各容器自己的电源开关属性里（见 switch.IKuaiContainerSwitch）
    "ikuai_qemu_count": {
        "name": "虚拟机数量", "icon": "mdi:monitor", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: len(e.get("qemu", [])),
        "attrs_key": "qemu",
    },
    # ---------- 消息 / 告警 ----------
    "ikuai_alarms_unread": {
        "name": "未读告警", "icon": "mdi:bell-alert", "unit_of_measurement": "条",
        "value": lambda d, e: e.get("alarms", {}).get("unread", 0),
        "attrs_key": "alarms",
    },
    "ikuai_messages_unread": {
        "name": "未读消息", "icon": "mdi:bell-badge", "unit_of_measurement": "条",
        "entity_category": "diagnostic",
        "value": lambda d, e: e.get("messages", {}).get("unread", 0),
        "attrs_key": "messages",
    },
    # ---------- 行为审计存储 ----------
    "ikuai_audit_storage_pct": {
        "name": "行为记录存储占用", "icon": "mdi:database-search", "unit_of_measurement": "%",
        "entity_category": "diagnostic",
        "value": lambda d, e: round(
            100 * (e.get("audit", {}).get("use_size") or 0) /
            max(1, e.get("audit", {}).get("total_size") or 1), 1),
        "attrs_key": "audit",
    },
    # ---------- 应用流量 / 接口流量 / 规则统计（聚合属性型） ----------
    # 原 "ikuai_app_flow"（应用流量分布）已删除：它的 state 就是
    # app_flow_windows["30min"]["Total"]（见 data_fetcher 的 _get_app_flow_windows），
    # 属性也只是 30 分钟窗口的切片，与 "ikuai_app_flow_windows" 完全重复。
    "ikuai_app_flow_windows": {
        # 一个实体装三个时间窗口：属性 app_flow_windows = {30min: {...}, 1h: {...}, 1d: {...}}
        "name": "流量分布", "icon": "mdi:chart-donut-variant",
        # 状态值 = 近30分钟总流量（单位：字节）；各窗口的完整分布见属性
        "unit_of_measurement": "B",
        "value": lambda d, e: (e.get("app_flow_windows", {}).get("30min") or {}).get("Total"),
        "attrs_key": "app_flow_windows",
    },
    "ikuai_iface_stream": {
        "name": "接口实时流量", "icon": "mdi:swap-vertical", "unit_of_measurement": "个",
        "enabled_default": False,
        "value": lambda d, e: len(e.get("iface_stream", [])),
        "attrs_key": "iface_stream",
    },
    "ikuai_rule_stats": {
        # 规则统计「聚合实体」：state = 全部规则表条数之和，属性 counts = 每张表的条数。
        # 原先每张表一个 sensor.ikuai_rule_<key>（共 38 个动态实体），现已全部收进这里，
        # 规则明细行（ext.rule_rows）不再单独暴露（逐表明细会超出 recorder 属性上限）。
        "name": "规则统计", "icon": "mdi:format-list-numbered", "unit_of_measurement": "条",
        "state_class": "measurement", "entity_category": "diagnostic",
        "value": lambda d, e: sum(v for v in (e.get("counts") or {}).values() if isinstance(v, int)),
        "attrs_key": "counts",
    },
    # ---------- A 档补全：数据已在 ext 中，零新增接口 ----------
    "ikuai_admin_count": {
        "name": "管理员数量", "icon": "mdi:account-key-outline", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: len(e.get("webuser") or []),
        "attrs_key": "webuser",
    },
    "ikuai_ap_total": {
        "name": "AP总数", "icon": "mdi:access-point", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: len(e.get("ac_list") or []),
        "attrs_key": "ac_list",
    },
    "ikuai_ap_clients": {
        "name": "AP总带机", "icon": "mdi:account-multiple", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": _ap_clients,
        "attrs_key": "ac_list",
    },
    "ikuai_wuying_count": {
        "name": "无影实例数", "icon": "mdi:monitor-cellphone", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: len(e.get("wuying") or []),
        "attrs_key": "wuying",
    },
    "ikuai_cache_services": {
        "name": "缓存加速服务", "icon": "mdi:speedometer-slow", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: len([
            s for s in (((e.get("cache_status") or {}).get("data") or {}).get("service") or [])
            if str(s.get("enabled")).lower() in ("yes", "1", "on", "true")]),
        "attrs_key": "cache_status",
    },
    "ikuai_backup_count": {
        "name": "配置备份数", "icon": "mdi:cloud-upload-outline", "unit_of_measurement": "个",
        "entity_category": "diagnostic",
        "value": lambda d, e: len((e.get("backup") or {}).get("data") or []),
        "attrs_key": "backup",
    },
    "ikuai_pptp_total": {
        "name": "PPTP线路总数", "icon": "mdi:router-network", "unit_of_measurement": "条",
        "entity_category": "diagnostic",
        "value": lambda d, e: d.get("ikuai_pptp_total"),
        "attrs_key": "pptp_lines",
    },
    "ikuai_l2tp_total": {
        "name": "L2TP线路总数", "icon": "mdi:router-network", "unit_of_measurement": "条",
        "entity_category": "diagnostic",
        "value": lambda d, e: d.get("ikuai_l2tp_total"),
        "attrs_key": "l2tp_lines",
    },
}


# 应用流量分布的三个时间窗口：节点名 -> homepage 接口的 minute 参数（字符串）
APP_FLOW_WINDOWS = (("30min", "30"), ("1h", "60"), ("1d", "1440"))
APP_FLOW_WINDOW_LABELS = {"30min": "近30分钟", "1h": "近1小时", "1d": "近1日"}


### 二元传感器
BINARY_SENSOR_TYPES = {
    # 原 "ikuai_wan_connected"（WAN在线）已删除：它只是每口
    # binary_sensor.ikuai_wan_N_online（「WAN x 状态」）的 OR 汇总，信息完全重叠。
    "ikuai_wan_fault": {
        "name": "多拨线路异常", "device_class": "problem",
        "value": lambda d, e: (d.get("ikuai_pppoe_fail_count") or 0) > 0,
        "attrs_key": "wan_lines_summary",
    },
    # 原 "ikuai_iface_fault"（线路体检告警）已删除：线路体检（monitor_iface →
    # iface_check）的明细与不通过条数并入 sensor.ikuai_interfaces，不再单独成实体。
    "ikuai_alarm": {
        "name": "未读系统告警", "device_class": "problem", "entity_category": "diagnostic",
        "value": lambda d, e: (e.get("alarms", {}).get("unread") or 0) > 0,
        "attrs_key": "alarms",
    },
    "ikuai_message": {
        "name": "未读消息通知", "device_class": "problem", "entity_category": "diagnostic",
        "value": lambda d, e: (e.get("messages", {}).get("unread") or 0) > 0,
        "attrs_key": "messages",
    },
    # ---------- A 档补全 ----------
    "ikuai_new_firmware": {
        "name": "固件有新版本",
        "value": lambda d, e: bool(
            ((e.get("upgrade") or {}).get("data") or {}).get("new_system_ver")),
        "attrs_key": "upgrade",
    },
    "ikuai_cloud_bound": {
        "name": "爱快云已绑定", "device_class": "connectivity",
        "value": _cloud_bound,
        "attrs_key": "register",
    },
    "ikuai_cache_running": {
        "name": "缓存加速运行中",
        "value": _cache_running,
        "attrs_key": "cache_status",
    },
}

# 已整合进「系统信息」聚合实体、不再单独成实体的二元量（binary_sensor 平台会跳过
# 这些 kind，取值仍复用上面的定义，由聚合实体以属性暴露）：
#   * ikuai_cloud_bound   —— 爱快云绑定属于纯系统信息；
#   * ikuai_new_firmware  —— 与 update.ikuai_firmware（HA 原生固件更新实体，带版本
#     对比与更新说明）完全重复，保留 update 实体即可。
BINARY_INTO_SYSTEM_INFO = ("ikuai_cloud_bound", "ikuai_new_firmware")


### 按钮
BUTTON_TYPES = {
    "ikuai_restart": {
        "label": "Ikuai重启",
        "name": "重启",
        "device_class": "restart",
        "action_body": {"func_name": "reboots", "action": "reboots"}
    },
    "ikuai_restart_reconnect_wan": {
        "label": "重连wan网络",
        "name": "重连WAN网络",
        "device_class": "restart",
        "action_body": {"func_name": "wan", "action": "link_pppoe_reconnect", "param": {"id": 1}}
    },
    "ikuai_shutdown": {
        "label": "关机",
        "name": "关机",
        "icon": "mdi:power",
        "action_body": {"func_name": "reboots", "action": "shutdown"}
    },
    "ikuai_sync_time": {
        "label": "同步系统时间",
        "name": "同步时间",
        "icon": "mdi:clock-check-outline",
        "action_body": {"func_name": "basic", "action": "sync_time"}
    },
    "ikuai_clean_traffic": {
        "label": "清空流量统计",
        "name": "清空流量统计",
        "icon": "mdi:broom",
        "action_body": {"func_name": "cflow", "action": "clean"}
    },
    "ikuai_alarms_read_all": {
        "label": "全部告警标记已读",
        "name": "告警全部已读",
        "icon": "mdi:bell-check",
        "action_body": {"func_name": "rt_collect", "action": "reading_all"}
    },
    "ikuai_messages_clean": {
        "label": "清空消息通知",
        "name": "清空消息",
        "icon": "mdi:bell-off",
        "action_body": {"func_name": "ikmessages", "action": "clean"}
    },
}


### 内置开关（键名保持历史兼容）
SWITCH_TYPES = {
    "ikuai_arp_filter": {
        "icon": "mdi:account-lock",
        "label": "iKuai非绑定MAC不允许上网",
        "name": "非绑定MAC不允许上网",
        "entity_category": "config",
        "turn_on_body": {"func_name": "arp", "action": "seting", "param": {"arp_filter": 1}},
        "turn_off_body": {"func_name": "arp", "action": "seting", "param": {"arp_filter": 0}},
        "show_body": {"func_name": "arp", "action": "show", "param": {"TYPE": "options"}},
        "show_on": {"arp_filter": 1},
        "show_off": {"arp_filter": 0},
    },
    "ikuai_stream_control": {
        "icon": "mdi:network-outline",
        "label": "iKuai流控模式（自动/关闭）",
        "name": "流控模式",
        "entity_category": "config",
        "turn_on_body": {"func_name": "stream_control", "action": "seting", "param": {"stream_ctl_mode": 1}},
        "turn_off_body": {"func_name": "stream_control", "action": "seting", "param": {"stream_ctl_mode": 0}},
        "show_body": {"func_name": "stream_control", "action": "show", "param": {"TYPE": "stream_ctl_mode"}},
        "show_on": {"stream_ctl_mode": 1},
        "show_off": {"stream_ctl_mode": 0},
    }
}

### 服务开关：state_from(ext)->bool；body 由 DataFetcher.async_set_service_switch 组装
EXT_SWITCH_TYPES = {
    "ikuai_sw_snmp": {
        "name": "SNMP服务", "icon": "mdi:clipboard-search-outline", "field": "enabled",
    },
    "ikuai_sw_samba": {
        "name": "Samba服务", "icon": "mdi:folder-network", "field": "enabled",
    },
    "ikuai_sw_ftp": {
        "name": "FTP服务", "icon": "mdi:file-upload-outline", "field": "open_ftp",
    },
    "ikuai_sw_upnp": {
        "name": "UPnP服务", "icon": "mdi:lan-check", "field": "enabled",
    },
    "ikuai_sw_webauth": {
        "name": "Web认证", "icon": "mdi:account-check-outline", "field": "enabled",
    },
    "ikuai_sw_igmp": {
        "name": "IGMP代理", "icon": "mdi:multicast", "field": "enabled",
    },
    "ikuai_sw_iptv": {
        "name": "IPTV功能", "icon": "mdi:television", "field": "enabled",
    },
    "ikuai_sw_watchdog": {
        "name": "硬件看门狗", "icon": "mdi:dog-service", "field": "watchdog",
    },
    # 以下为高风险开关，默认不在注册表启用，需要时手动启用
    "ikuai_sw_nat": {
        "name": "NAT转发", "icon": "mdi:nat", "field": "switch_nat",
        "enabled_default": False, "risky": True,
    },
    "ikuai_sw_dpi": {
        "name": "DPI深度识别", "icon": "mdi:magnify-scan", "field": "switch_dpi",
        "enabled_default": False, "risky": True,
    },
    "ikuai_sw_ntp": {
        "name": "NTP对时服务", "icon": "mdi:clock-outline", "field": "switch_ntp",
        "enabled_default": False,
    },
    # ---- ALG 应用网关（alg.data[0]：support_ftp/tftp/sip/h323）----
    "ikuai_sw_alg_ftp": {
        "name": "ALG FTP", "icon": "mdi:file-transfer-outline", "field": "support_ftp",
    },
    "ikuai_sw_alg_tftp": {
        "name": "ALG TFTP", "icon": "mdi:file-upload-outline", "field": "support_tftp",
    },
    "ikuai_sw_alg_sip": {
        "name": "ALG SIP", "icon": "mdi:phone-outline", "field": "support_sip",
    },
    "ikuai_sw_alg_h323": {
        "name": "ALG H323", "icon": "mdi:phone-plus-outline", "field": "support_h323",
    },
    # ---- 高风险：外网访问 / 自动升级，默认禁用，需要时在注册表手动启用 ----
    "ikuai_sw_wanweb": {
        "name": "外网Web管理", "icon": "mdi:web", "field": "open_wanweb",
        "enabled_default": False, "risky": True,
    },
    "ikuai_sw_sshd": {
        "name": "SSH远程登录", "icon": "mdi:ssh", "field": "open_sshd",
        "enabled_default": False, "risky": True,
    },
    "ikuai_sw_telnetd": {
        "name": "Telnet远程登录", "icon": "mdi:console-line", "field": "open_telnetd",
        "enabled_default": False, "risky": True,
    },
    "ikuai_sw_auto_upgrade": {
        "name": "自动升级", "icon": "mdi:tray-arrow-up", "field": "auto_upgrade_sec",
        # 设置项嵌在 upgrade 块的 data 子表里（switch 类按 section 下钻读取）
        "section": "data",
        "enabled_default": False, "risky": True,
    },
}


### 日志中心分类（顺序即展示顺序）
# (key, func_name, 中文名, 行渲染字段中文映射)
LOG_CATEGORIES = [
    {
        "key": "system", "func": "syslog-sysevent", "name": "系统日志", "icon": "mdi:script-text-outline",
        "fields": {"content": "内容"},
    },
    {
        "key": "webadmin", "func": "syslog-webadmin", "name": "操作日志", "icon": "mdi:account-cog-outline",
        "fields": {"username": "用户", "ip_addr": "IP", "function": "模块", "event": "事件"},
    },
    {
        "key": "pppauth", "func": "syslog-pppauth", "name": "认证日志", "icon": "mdi:account-key-outline",
        "fields": {"username": "用户", "ip_addr": "IP", "ppptype": "类型", "result": "结果",
                   "interface": "接口", "macip": "MAC/IP", "event": "事件"},
    },
    {
        "key": "arp", "func": "syslog-arp", "name": "ARP日志", "icon": "mdi:lan",
        "fields": {"content": "内容"},
    },
    {
        "key": "apaction", "func": "syslog-apaction", "name": "无线终端日志", "icon": "mdi:wifi",
        "fields": {"mac": "MAC", "mac_comment": "备注", "ssid": "SSID", "action": "动作",
                   "signal": "信号", "errmsg": "信息"},
    },
    {
        "key": "dhcpd", "func": "syslog-dhcpd", "name": "DHCP日志", "icon": "mdi:dns-outline",
        "fields": {"msgtype": "类型", "interface": "接口", "mac": "MAC", "ip_addr": "IP", "event": "事件"},
    },
    {
        "key": "ddns", "func": "syslog-ddns", "name": "动态域名日志", "icon": "mdi:domain",
        "fields": {"domain": "域名", "ip_addr": "IP", "interface": "接口", "result": "结果", "event": "事件"},
    },
    {
        "key": "wanpppoe", "func": "syslog-wanpppoe", "name": "外网拨号日志", "icon": "mdi:phone-in-talk-outline",
        "fields": {"interface": "接口", "content": "内容"},
    },
    {
        "key": "notice", "func": "syslog-notice", "name": "推送通知日志", "icon": "mdi:bell-outline",
        "fields": {"ip_addr": "IP", "type": "类型", "event": "事件"},
    },
    # 行为审计记录（非 syslog-* 系列，参数一致）
    {
        "key": "terminal_log", "func": "audit_terminal_log", "name": "终端上下线记录", "icon": "mdi:devices",
        # 单次上限就是 100 条（爱快 limit 一次最多返回 100，写 10000 也一样）：
        # 明确写 100 免得多发无用请求，也免得让人以为"取到了全量"
        "order_by": "timestamp", "limit": "0,100",
        "fields": {"ip_addr": "IP", "mac": "MAC", "comment": "备注", "client_model": "型号",
                   "online_time": "上线", "logout_time": "下线", "date_time": "时间"},
    },
    {
        "key": "url_log", "func": "audit_url_log", "name": "网址浏览记录", "icon": "mdi:web",
        "optional": True,
        # 该模块**不支持 TYPE=total**：带 total 会被拒（Result 30006 unknown TYPE (total)），
        # 所以这里单独指定参数，只要 data；条数用本次取到的行数（它的 total 恒为 -1）。
        #
        # limit 的格式是 "偏移,条数"，而且**单次最多返回 100 条**：一次要 10000 条会漏数据
        # （实测正确做法是 0,100 / 100,100 / 200,100 … 逐页取）。
        # 因此声明 page_size，由 logs.py 自动翻页，最多 max_pages 页。
        "param": {"TYPE": "data", "limit": "0,100"},
        # **只看第一页**（100 条）：日志是给人看的诊断数据，以前 max_pages=20
        # 意味着单这一项就要发 20 个请求，把启动与每轮轮询都拖慢。
        "page_size": 100,
        "max_pages": 1,
        # 内存与实体属性里保留的条数（只抓第一页，所以与 page_size 对齐）
        "rows_keep": 100,
        # 字段按实际返回：host(网址) / uri(路径) / appname(应用) / ip_addr / mac /
        # comment / timestamp，没有 url 字段
        "fields": {"host": "网址", "uri": "路径", "appname": "应用", "ip_addr": "IP",
                   "mac": "MAC", "comment": "备注", "timestamp": "时间"},
    },
    {
        "key": "im_log", "func": "audit_im_log", "name": "IM聊天记录", "icon": "mdi:chat-outline",
        "optional": True,
        "fields": {},
    },
]

LOG_ROWS_KEEP = 50

### 事件总线
EVENT_LOG = "ikuai_log"
EVENT_ALARM = "ikuai_alarm"

### 服务名
SERVICE_API_CALL = "api_call"
SERVICE_WAKE_ON_LAN = "wake_on_lan"
SERVICE_WAN_RECONNECT = "wan_reconnect"
SERVICE_TERMINAL_DISCONNECT = "terminal_disconnect"
SERVICE_MAC_BLOCK = "mac_block"
SERVICE_MAC_UNBLOCK = "mac_unblock"
SERVICE_LOG_GET = "log_get"
SERVICE_LOG_CLEAR = "log_clear"
SERVICE_ALARMS_READ_ALL = "alarms_read_all"
# 多拨线路 CRUD（func_name=wan，action=vlan_*）
SERVICE_WAN_LINE_ADD = "wan_line_add"
SERVICE_WAN_LINE_EDIT = "wan_line_edit"
SERVICE_WAN_LINE_DEL = "wan_line_del"
# 上下行分离规则（func_name=stream_updown）
SERVICE_STREAM_UPDOWN_ADD = "stream_updown_add"
SERVICE_STREAM_UPDOWN_DEL = "stream_updown_del"

### 规则计数动态传感器中文名（键 = COUNT_SPECS 的 ext 计数键/func_name）
RULE_COUNT_LABELS = {
    "acl": "ACL规则数",
    "conn_limit": "连接数限制规则数",
    "acl_l7": "应用协议控制规则数",
    "acl_qq": "QQ黑白名单规则数",
    "dnat": "端口映射规则数",
    "nat_rule": "NAT规则数",
    "static_rt": "静态路由条数",
    "vlan": "VLAN接口数",
    "stream_ipport": "端口分流规则数",
    "stream_layer7": "协议分流规则数",
    "stream_domain": "域名分流规则数",
    "stream_updown": "上下行分离规则数",
    "simple_qos": "IP限速规则数",
    "mac_qos": "MAC限速规则数",
    "alone_limit": "独立限速规则数",
    "high_prio_host": "优先主机规则数",
    "dprotos": "自定义协议数",
    "dprotos_l7": "高级自定义协议数",
    "port_mirror": "端口镜像规则数",
    "udp_proxy": "UDP代理规则数",
    "lb_pcc": "多线负载规则数",
    "custom_isp": "自定义运营商数",
    "upnpd_leases": "UPnP租约数",
    "dev_control": "设备管控规则数",
    "cloud_switch": "云开关规则数",
    "wakeup_list": "网络唤醒条目数",
    "coupon": "计费账号数",
    "pppuser": "认证账号数",
    "ipgroup": "IP分组数",
    "macgroup": "MAC分组数",
    "dtgroup": "用户分组数",
    "ipv6group": "IPv6分组数",
    "domain_blacklist": "网址黑白名单数",
    "dns": "DNS代理规则数",
    "dns_replace": "DNS替换规则数",
    "url_keywords": "关键字替换规则数",
    "url_redirect": "网址跳转规则数",
    "url_replace": "参数替换规则数",
}
