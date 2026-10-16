# iKuai 爱快路由器集成（Home Assistant）

把爱快（iKuai）软路由接进 Home Assistant：系统状态、WAN/LAN、在线终端、拨号/VPN、
Docker 与虚拟机、认证账号、日志与告警……另外还提供两个"进阶"能力：

* **透传查询 API** —— 把爱快原生 Web 接口（`/Action/call`）原样开放成一个 HTTP 端点，
  任何爱快接口都能用，开关可随时关闭；
* **Web 管理悬浮窗** —— 在 HA 里直接开爱快的原版管理界面（同源反向代理，登录态托管）。

---

## 目录

- [特性一览](#特性一览)
- [安装](#安装)
- [配置](#配置)
- [数据采集机制](#数据采集机制)
- [实体总览](#实体总览)
- [接口与网络状态](#接口与网络状态)
- [服务](#服务)
- [透传查询 API](#透传查询-api)
- [虚拟机页面](#虚拟机页面)
- [Web 管理悬浮窗](#web-管理悬浮窗)
- [实体 ID 规则与迁移](#实体-id-规则与迁移)
- [性能与降噪建议](#性能与降噪建议)
- [故障排查](#故障排查)
- [已知限制](#已知限制)

---

## 特性一览

| 能力 | 说明 |
|---|---|
| 系统状态 | CPU 占用/温度、内存、磁盘分区、运行时长、各类计数；**硬件信息**（CPU 型号/位数/频率/缓存/核心/线程、主板、内存、硬盘、PCI 设备清单） |
| 网络状态 | WAN/LAN 口地址、上下行速率、连接数、在线终端、DHCP/ARP/路由/IPv6 邻居；**接口总览**（物理口 ↔ 逻辑口 ↔ 线路检测，`state` = 网络是否正常）；**DHCP 服务**（服务端配置、静态分配、动态租约、MAC 黑白名单） |
| 线路 | 每口 WAN 的 IP、运行秒数、实时速率；PPPoE/PPTP/L2TP 在线与失败数；多拨线路操作 |
| 终端 | 在线终端列表（IPv4/IPv6），每台可展开展示**应用流量明细 + 连接明细 + 限速状态** |
| 无线 | AP 总数/带机、每台 AP 状态、无线终端日志 |
| 业务 | Docker 容器（开关/资源）、虚拟机（电源、屏幕画面、虚拟机页面）、认证账号（信息+在线）、**本地认证服务端**（PPPoE/PPTP/L2TP/OpenVPN 启停与参数） |
| 流量与规则 | 应用流量分布（30 分钟/1 小时/1 日）、38 张规则表的条数统计 |
| 日志与告警 | 12 类日志（条数 + 最近一条明细）、未读告警/消息、一键已读/清空 |
| 通知与动作 | 网络唤醒（WOL）、踢终端下线、MAC 拉黑/放行、重启/关机/同步时间/清空流量 |
| 设备追踪 | 按 IP/MAC 生成 `device_tracker`，可用来自动化"谁在家" |
| 透传查询 API | 可选开关；GET/POST 直接调爱快任意接口，密钥每小时自动轮换；装了多台爱快时按台区分（key 自带归属，也可用 `entry` 指名） |
| Web 悬浮窗 | 设备页「访问」打开爱快原版后台；虚拟机「访问」直达爱快虚拟机页面 |

---

## 安装

### HACS（推荐）

1. HACS → 集成 → 右上角 ⋮ → 自定义仓库；
2. 地址填本仓库，类型选「集成」，添加；
3. 在 HACS 里搜索 `iKuai` 并下载，然后**重启 Home Assistant**。

### 手动

把 `custom_components/ikuai` 整个目录复制到你的 HA 配置目录下：

```
config/
└── custom_components/
    └── ikuai/
```

重启 Home Assistant。

---

## 配置

### 初次添加

设置 → 设备与服务 → 添加集成 → 搜索 `iKuai`。

| 字段 | 默认值 | 说明 |
|---|---|---|
| 路由器地址 | `http://192.168.1.1` | 爱快后台地址，可带端口 |
| 用户名 | `admin` | 爱快后台登录账号 |
| 密码 | — | 爱快后台登录密码 |
| 刷新间隔（秒） | `10` | 主轮询周期，最小 `5` |
| 离线判定缓冲次数 | `2` | 设备追踪：连续多少次抓不到才判定离线 |
| 追踪清单来源 | `UI 配置` | `UI 配置` 在图形界面里挑设备；`const 配置` 使用 `const.py` 里预置的 `DEVICE_TRACKERS` |

保存时会先真正登录一次爱快校验凭据，失败会在表单里直接提示。

> 同一个集成里可以添加**多台爱快**：每台是一个独立条目，各有自己的刷新间隔、实体、设备
> 与开关（密钥、统计也各算各的）。服务用 `entry_id` 区分目标（**留空 = 所有台都执行**），
> 透传 API 用该台的 key 或 `entry` 参数（见 [透传查询 API](#透传查询-api)）。

### 设备追踪

配置流程里可以：

- **扫描添加**：读取爱快当前在线终端，按 IP / MAC 的正则或包含过滤，勾选要追踪的设备；
- **手动添加**：直接填 IP、子网段、MAC（每行一个）；
- **管理设备**：逐个改名、逐台设置离线判定缓冲（留空则用全局默认值）；
- **删除设备**。

被追踪的设备会生成 `device_tracker.ikuai_tracker_<MAC 或 IP>`，并挂到对应的设备页下。

### 选项

集成条目 ⋮ → 配置，可随时修改：刷新间隔、离线判定缓冲、追踪清单来源，
以及进入设备追踪的增删改子流程。修改后会重载集成。

---

## 数据采集机制

了解这些有助于判断"卡不卡""为什么某个实体更新慢"。

| 机制 | 默认值 | 说明 |
|---|---|---|
| 主轮询 | `10` 秒 | 抓核心状态：系统、WAN/LAN、在线终端、线路、内置开关 |
| 扩展轮询 | 每 **3** 个主轮询 | 抓"重"数据：DHCP/ARP、Docker/虚拟机、日志、告警、规则明细、认证/VPN 服务端配置等 |
| 数据库日志轮询 | `60` 秒 | 12 类日志的条数与最近一条（**只取第一页**，不翻页）；与主轮询分开调度，默认固定、不在配置项里（`const` 里可调，下限 20 秒）；可用 `switch.ikuai_log_poll`（「采集日志」）整体关掉 |
| 全局并发 | `6` | 所有上游请求共用一个信号量（`const.HTTP_CONCURRENCY`，可调） |
| 列表分页 | 每页 `100` 条 | 爱快 `limit` 单次最多返回 100 条，列表类抓取自动翻页取全（最多 10 页）；明确只要少量行的请求（如消息 `0,10`、应用流量 `0,15`）不翻页 |
| 终端详情 | 每轮 `10` 台 | 应用流量 + 连接明细，按 IP 逐个抓（每台 2 个请求），**轮换覆盖**；仅当在线终端实体启用时才抓 |

几处刻意的设计：

- **实体的大属性不写数据库**：聚合实体（系统/LAN/拨号/…）、规则统计、日志统计、在线终端列表、
  查询 API 开关的明细属性都声明了 `_unrecorded_attributes` —— 状态机、模板、前端照常可读，
  但 recorder 不会把这几百 KB 的属性写进历史库（也绕开了单实体 16KB 属性上限）；
- **带历史价值的指标单独成实体**：CPU 占用、CPU 温度、内存占用、上下行速率/总量、
  每口 WAN 速率 —— 这些保留 `state_class`，历史和长期统计不断；
- **诊断类实体默认禁用**：7 个高风险/低频服务开关（NAT 转发、DPI 深度识别、NTP 对时、
  外网 Web 管理、SSH、Telnet、自动升级）与「在线终端列表」「接口实时流量」默认关闭，
  需要时在实体注册表里启用。

---

## 实体总览

### 设备页结构

| 设备 | 挂载内容 |
|---|---|
| `iKuai（主机）` | 所有系统/网络/线路/告警实体、服务开关、按钮、文本输入、固件更新 |
| `iKuai Docker` | 所有容器共用一台设备，每个容器一个启停开关（该容器的映射端口与 `web_url` 在**开关属性**里；容器设备本身没有「访问」链接） |
| `虚拟机 <名称>` | 每台虚拟机一台设备：电源开关、屏幕画面、指向爱快虚拟机页面的「访问」 |
| `认证账号` | 所有 PPPoE/PPTP/L2TP/OVPN 账号共用一台设备 |
| `iKuai（主机）` 的「访问」 | 经同源反向代理打开爱快原版后台，见 [Web 管理悬浮窗](#web-管理悬浮窗) |

### 1. 聚合实体（6 个）

把同类诊断项收进一个实体：`state` 是主要指标，其余同类项进属性。

| 实体 | state | 属性里收纳 |
|---|---|---|
| `sensor.ikuai_system_info` 系统信息 | CPU 占用 % | 温度、内存总量/可用、核心数/频率、启动时长、磁盘容量、每核占用、每个分区使用率、管理员/备份/无影/摄像头/虚拟机数、缓存加速、行为记录占用、爱快云绑定与固件状态、远程访问地址（`web_url` / `web_url_full`） |
| `sensor.ikuai_lan_info` LAN 信息 | 在线终端数 | 各 LAN 口 IP/掩码/MAC、IPv6 地址、DHCP 租约/静态/服务器、MAC 黑白名单、ARP 条目与绑定、IPv6 邻居、路由条数、连接数、AP 总数/带机（物理网口链路/速率见 `sensor.ikuai_interfaces` 的 `ports`）；这里的 DHCP 项是**计数**，明细见 `sensor.ikuai_dhcp` |
| `sensor.ikuai_dial_info` 拨号/VPN | 在线线路数 | PPPoE/PPTP/L2TP 的在线/失败/总数、VPN 在线用户、多拨线路明细 |
| `sensor.ikuai_alarm_info` 告警与消息 | 未读告警数 | 未读消息、线路体检异常数（`iface_fail`，明细在「接口」实体里）、告警明细、消息明细、固件更新/云绑定/缓存加速状态 |
| `sensor.ikuai_rule_stats` 规则统计 | 规则总条数 | 38 张规则表各自的条数与明细行数（每张表最多展示 500 行） |
| `sensor.ikuai_log_stats` 日志统计 | 各类日志总条数 | 12 类日志的条数、更新时间、最近一条关键字段 |

> 聚合实体的**整包属性不写数据库**，可以放心展开查看。
>
> 说明：`sensor.ikuai_system_info` 里的 `cpu_cores` / `cpu_freq` / `memory_total` /
> `memory_available` / `disk_size` 与 `sensor.ikuai_hardware` **同源**（都来自
> `hardwareinfo`）—— 前者是"运行状态视图"，后者是"完整规格视图"（含型号/主板/PCI）。

### 2. 独立传感器（保留历史曲线）

| 实体 | 说明 |
|---|---|
| `sensor.ikuai_cpu` / `sensor.ikuai_cputemp` / `sensor.ikuai_memory` | CPU 占用、CPU 温度、内存占用 |
| `sensor.ikuai_upload` / `sensor.ikuai_download` | 实时上行/下行速率（MB/s） |
| `sensor.ikuai_total_up` / `sensor.ikuai_total_down` | 累计上行/下行总量（GB，`total_increasing`，可做长期流量统计） |
| `sensor.ikuai_wan_<id>_upload` / `_download` | 每个 WAN 口的实时速率（**只出速率值，不带状态属性**；该口的 IP/网关/DNS/MAC/入网方式/上线时长在同编号的状态实体上） |
| `sensor.ikuai_interfaces` 接口 | **网络是否正常**（`正常` / `异常` / `未知`，见下）；属性含 `ports`（每个物理口：link/协商速率/双工/型号/驱动/MAC/绑定关系 + 关联的逻辑口与实时速率）、`lan_ports` / `wan_ports`（含 `member` 成员）、`abnormal`（检测不通过的线路）、`network_ok`（布尔，与 state 同义）、`summary` |
| `sensor.ikuai_iface_stream` 接口实时流量 | **默认禁用**；`state` = 有数据的网口数，属性 `iface_stream` 是每个网口的实时上/下行速率明细（来自扩展轮询的 `monitor_iface`，零新增请求） |
| `sensor.ikuai_hardware` 硬件信息 | `state`=**CPU 型号**；属性：`cpu_bit` / `cpu_mhz` / `cpu_cache` / `cpu_cores` / `cpu_threads`、`mainboard`、`memory_mb`（+ 可读 `memory_text`）、`hdd`、`pci`（按控制器类别分组的设备清单，不写库）/ `pci_devices`。数据来自已在抓的 `hardwareinfo`，零新增请求 |
| `sensor.ikuai_auth_server` 本地认证服务 | `state`=**已启用的服务端个数**；属性按服务分组：`pppoe` / `pptp` / `l2tp` / `openvpn`（该服务端的参数，`enabled` 已归一成布尔、空值不给）、`enabled_services`（已启用的服务名）。**密钥不进属性**：RADIUS `secret`、IPsec `ipsec_secret` 显示为 `***`，OpenVPN 的证书/私钥整段丢弃、只给 `has_ca` / `has_cert` / `has_key` / `has_tls_auth` |
| `sensor.ikuai_dhcp` DHCP服务 | `state`=**已启用的服务端个数**；属性：`servers`（每台服务端的配置：`interface` / `addr_pool` / `netmask` / `gateway` / `dns1` / `dns2` / `domain` / `lease`+`lease_text` / `available` / `status`，`enabled` 归一成布尔、空值不给）、`interfaces`（已启用的接口名）、`static_total` / `statics`（静态分配 MAC→IP 绑定，含 `hostname` / `comment`）、`compat_arp`（兼容 ARP 开关）、`lease_total` / `leases`（动态租约，`start_time` / `end_time` 自动转可读时间、`timeout` 另给 `timeout_text`）、`blocked_total` / `blocked` / `acl_mode` / `acl_mode_text`（服务端 MAC 黑白名单，0=黑名单 1=白名单）、`dhcp6_lease_total` / `dhcp6_leases` / `dhcp6_blocked*`（IPv6 侧）。数据来自已在抓的 6 个 DHCP 接口，零新增请求；每个明细列表最多 500 行（超出给 `truncated`） |
| `sensor.ikuai_lan_hosts` 在线终端列表 | **默认禁用**；`state`=终端数，属性含 IPv4/IPv6 明细、流量 Top5、`speed_limited`（在限速中的终端数），以及每台设备的 `simple_qos` 限速标记 + `detail` 子节点（应用流量 + 连接明细，见下） |
| `sensor.ikuai_wan6_ip` WAN IPv6 地址 | DHCPv6 客户端拿到的 WAN 侧 IPv6 地址（原在「WAN 信息」聚合实体里，聚合删除后回到独立传感器） |
| `sensor.ikuai_app_flow_windows` 流量分布 | 近 30 分钟 / 1 小时 / 1 日的应用流量分布 |
| `sensor.ikuai_log_url_log` 日志-网址浏览记录 | 最近一页（100 条）浏览记录，供卡片按 MAC 过滤展示 |
| `sensor.ikuai_api_key` API 密钥 | 透传 API 的密钥（每小时轮换，见 [透传查询 API](#透传查询-api)） |
| `sensor.ikuai_all_entities` 所有实体 | 本集成全部实体清单（按设备分组），方便排查 |
| （无）在线终端数 / 连接数 / LAN IPv6 | 这三项**不单独成实体**，就在 `sensor.ikuai_lan_info` 的属性里：`online_user` / `connect_num` / `lan6_ip` |

**在线终端列表的设备详情子节点**（`entries[].detail`）示例：

```yaml
entries:
  - ip_addr: 192.168.5.14
    mac: 02:42:c0:a8:05:0e
    comment: Dock_NPS
    total_up: 31.74 GB          # 整机累计（接口给字节，已换算并带单位）
    total_down: 23.12 GB
    upload: 280 B/s             # 整机实时速率
    download: 458 B/s
    simple_qos: true            # ← 是否被限速（每台设备都有这个标记）
    speed_limit:                # ← 被限速时才有的限速明细（simple_qos 规则）
      id: 1
      upload: 100               # 接口原值，与爱快「IP限速」页一致
      download: 100
      comment: 快速添加
      enabled: 'yes'
      time: 00:00-23:59
      week: '1234567'
      week_text: 每天
    detail:                     # ← 该设备的子节点
      updated: 1760...          # 这份详情的抓取时刻
      app_count: 5
      apps:                     # 应用流量（对应爱快后台「终端详情 → 应用」）
        - app_name: 常用协议
          upload_rate: 16.38 KB/s
          share_up: 99.59%
          total_up: 31.6 GB
          download_rate: 15.4 KB/s
          share_down: 86.02%
          total_down: 19.87 GB
      conn_num: 3               # 该终端当前连接总数
      conns_shown: 3
      conns:                    # 连接明细（按流量降序，默认保留前 10 条）
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
```

> 详情是按轮换抓取的（每轮最多 10 台，每台 2 个请求），`updated` 是该份数据的时刻；
> 想看整机的"当前"速率用设备节点自身的 `upload`/`download`（每轮都刷新）。
>
> `simple_qos` / `speed_limit` 来自扩展轮询里**已经在抓**的 IP 限速规则（`simple_qos`，
> **零新增请求**），按 **IP 精确匹配**：规则里写的是 IP 分组 / 网段时匹配不到，
> 那些设备会显示 `simple_qos: false`。`speed_limit.upload` / `download` 是接口原值
> （与爱快「IP限速」页同一个数），未做换算。

### 3. 二元传感器

| 实体 | 说明 |
|---|---|
| `binary_sensor.ikuai_wan_fault` | 多拨线路是否有异常 |
| ~~`binary_sensor.ikuai_iface_fault`~~ | **已删除**：线路体检（`monitor_iface` → `iface_check`）明细并入了 `sensor.ikuai_interfaces`（`iface_check_failed` / `iface_check_count`），不再单独成实体 |
| `binary_sensor.ikuai_alarm` | 是否有未读系统告警 |
| `binary_sensor.ikuai_message` | 是否有未读消息 |
| `binary_sensor.ikuai_cache_running` | 缓存加速是否运行中 |
| `binary_sensor.ikuai_wan_<id>_online` | 每个 WAN 口的「**WAN x 状态**」（动态）：已连接 / 未连接（`device_class: connectivity`）；该口的 IP、上线时长（`uptime_seconds` / `uptime_text`）、网关、DNS、MAC、入网方式（`internet` / `internet_text`）、是否默认线路都在它的属性里 |
| `binary_sensor.ikuai_auth_<账号>_online` | 每个认证账号在线状态（动态） |
| ~~`binary_sensor.ikuai_eth<口>_link`~~ | **已删除**：物理口链路/速率/型号/绑定关系全在 `sensor.ikuai_interfaces` 的 `ports` 里 |

> 「爱快云是否已绑定」「固件是否有新版本」不再单独成实体，而是作为
> `sensor.ikuai_system_info` 的属性暴露（固件版本另有 `update.ikuai_firmware`）。
>
> 全局「WAN 是否在线」也已删除：它只是每口「WAN x 状态」的汇总（有任一口在线即为真）。

### 4. 开关

| 实体 | 说明 |
|---|---|
| `switch.ikuai_arp_filter` | 非绑定 MAC 不允许上网 |
| `switch.ikuai_stream_control` | 流控模式 |
| `switch.ikuai_sw_snmp` / `_samba` / `_ftp` / `_upnp` / `_webauth` / `_igmp` / `_iptv` / `_watchdog` / `_alg_ftp` / `_alg_tftp` / `_alg_sip` / `_alg_h323` | 各类系统服务开关（默认启用） |
| `switch.ikuai_sw_nat` / `_dpi` / `_ntp` / `_wanweb` / `_sshd` / `_telnetd` / `_auto_upgrade` | NAT 转发 / DPI 深度识别 / NTP 对时 / 外网 Web 管理 / SSH / Telnet / 自动升级 —— 这 7 个**默认禁用**，需要时在实体注册表里启用 |
| `switch.ikuai_mac_<MAC后6位>` | 每条 MAC 访问控制（禁止联网）条目一个开关（**实体 ID 固定不变**）；**显示名统一「禁止联网_<备注>」**，条目没写备注时用 MAC（`禁止联网_72:ff:d2:44:ac:91`）。`state` = 该条目当前是否在禁止联网（取 `acl_mac` 返回行的 `enabled`，**每次现读**，up/down 后立刻反映）；属性给出该条记录明细：`mac_address`、`id`、`comment`、`time`（生效时段）、`week` / `week_text`（生效星期，如"每天"）、`expires` / `expires_text`（0 = 永不过期）、`enabled` |
| `switch.ikuai_docker_<容器名>` | Docker 容器启停（动态） |
| `switch.ikuai_vm_<虚拟机名>_power` | 虚拟机电源（动态）；属性含该虚拟机配置（CPU/内存/磁盘/自启/分区）、VNC 信息（`vnc_port` / `vnc_host` / `vnc_acl` / `vnc_auth`）与控制台状态（`console_state` / `console_url` / `remote_enabled`） |
| `switch.ikuai_remote_control` | 全局「远程控制」总开关：关闭会清空虚拟机的「访问」链接并拒绝新跳转 |
| `switch.ikuai_log_poll` | 「采集日志」总开关（**默认开启**）：关掉就停止日志定时采集（12 类 × 每分钟，**一个日志请求都不发**），并把 `sensor.ikuai_log_stats` / `sensor.ikuai_log_url_log` 在注册表里**禁用**（重载后从实体列表消失）、`ikuai_log` 事件与日志簿停止；开关重新打开会**自动恢复启用**。服务 `ikuai.log_get` / `log_clear` 是显式调用，不受影响 |
| `switch.ikuai_api` | 「查询 API」总开关（默认关闭，见下） |
| `switch.ikuai_<自定义名>` | `configuration.yaml` 里配置的自定义开关 |

### 5. 按钮

| 实体 | 说明 |
|---|---|
| `button.ikuai_restart` / `_shutdown` | 重启 / 关机 |
| `button.ikuai_restart_reconnect_wan` | 重连 WAN 网络 |
| `button.ikuai_sync_time` | 同步时间 |
| `button.ikuai_clean_traffic` | 清空流量统计 |
| `button.ikuai_alarms_read_all` / `_messages_clean` | 告警全部已读 / 清空消息 |
| `button.ikuai_mac_block` / `_mac_unblock` | 拉黑 / 放行文本输入框里的 MAC；**属性带当前黑名单清单**（`blacklist` / `blacklist_total`，字段与每条目的 `switch.ikuai_mac_*` 一致）与**本次会操作的 MAC**（`input_entity` / `input_macs` / `input_count`，输入框一改就刷新） |
| `button.ikuai_wan_<id>_reconnect` | 每个 WAN 口重连（动态） |
| `button.ikuai_wake_<id>` | 网络唤醒已配置的每台设备（动态） |
| `button.ikuai_wanline_<id>_<动作>` | 多拨线路的重连/启用/停用/删除（聚合时动态生成） |

> 按钮实体在 HA 里**没有 state**（恒为 `unknown`，这是按钮域的规范，点按只写日志），
> 但**可以有属性** —— 上面两个 MAC 按钮就是靠属性把"当前黑名单 + 待操作 MAC"带出来的。

### 6. 文本 / 图片 / 更新

| 实体 | 说明 |
|---|---|
| `text.ikuai_mac_input` | MAC 输入框（配合拉黑/放行按钮，支持多个 MAC，逗号或换行分隔） |
| `image.ikuai_vm_<虚拟机名>_screen` | 虚拟机屏幕画面（动态，抓帧节流） |
| `update.ikuai_firmware` | 固件版本与"有更新"提示（不提供一键升级，升级请在爱快后台做） |

### 7. 动态实体

下面这些**随路由器实际配置自动增删**，不需要手工维护：

- 每个 WAN 口：`binary_sensor.ikuai_wan_<id>_online`（「WAN x 状态」，属性带该口的
  IP/网关/DNS/MAC/入网方式/上线时长）、`sensor.ikuai_wan_<id>_upload` / `_download`
  （只出速率）、`button.ikuai_wan_<id>_reconnect`；
- 每条网络唤醒条目：`button.ikuai_wake_<id>`；
- 每条多拨线路：超过 3 条时自动聚合为汇总实体 + 悬浮窗操作；
- 每个 Docker 容器 / 每台虚拟机 / 每个认证账号 / 每台 AP；
- 每个 MAC 访问控制（禁止联网）条目：`switch.ikuai_mac_<MAC后6位>`（集成加载时按列表创建）。

### 8. 设备追踪

`device_tracker.ikuai_tracker_<MAC 或 IP>`：由配置流程里选中的 IP/MAC 生成（实体 ID
由目标的 MAC/IP 推导，与中文备注名无关，改名不会牵动 ID），属性就是该终端在爱快里的整行
信息（IP、MAC、备注、连接数、上下行速率与累计、上线时间等）。可用于"在家/离家"自动化。

---

## 接口与网络状态

`sensor.ikuai_interfaces`（显示名「接口」）把"物理网口 ↔ 逻辑口 ↔ 地址/速率/线路检测"串到一个实体里。

**state = 网络是否正常**，判据是 WAN 口的**线路检测结果**（`snapshoot_wan.errmsg`）：

- 只有**带 MAC 地址**的 WAN 口参与判断 —— 没接线的口 `mac` / `errmsg` 都是空，不参与（否则会被误判成异常）；
- 全部参与口都是「线路检测成功」→ `正常`；
- **只要有一条不是**（多拨场景：一条通、一条不通）→ `异常`，`abnormal` 里列出是哪几条、`errmsg` 是什么；
- 一个可判断的口都没有 → `未知`（`network_ok: null`）；
- 另有布尔属性 **`network_ok`**（`true` / `false` / `null`），写自动化比比对文本方便。

其余属性：

| 属性 | 内容 |
|---|---|
| `ports` | 每个物理口（`ether_info`）：`link`、`speed_mbps`、`duplex`、`type`、`model`、`driver`、`mac`、`bound`（绑到哪条逻辑口），以及关联到的 `lan` / `wan` 子节点与 `rate`（该逻辑口的实时速率，MB/s） |
| `lan_ports` / `wan_ports` | 逻辑口明细；`member` 是成员物理口，能看出聚合/桥的构成（如 `lan1` = `[eth0, qtap3_lan1_1, vnet]`） |
| `abnormal` | 线路检测不通过的 WAN 口（正常时为 `[]`） |
| `iface_check_count` / `iface_check_failed` | 爱快「**线路体检**」（`monitor_iface` → `iface_check`）的检查项总数与**不通过的行**（`result` 不是 `success`）—— 原 `binary_sensor.ikuai_iface_fault` 的内容，已并入本实体。它与 state 用的 `errmsg`（WAN 线路检测）是**两个不同来源**，所以体检失败**不会**把 state 变成 `异常` |
| `summary` | `ports` / `up` / `wan_checked` / `wan_abnormal` / `check_failed` / `network_ok` |

虚拟网桥（`vnet` / `qtap*` / `vnic*`、`driver=kvm`、MAC 全 0）不算物理口，不出现在 `ports` 里（但会作为成员出现在逻辑口的 `member` 中）。

> 数据全部来自**已经在抓**的请求（主轮询的 `lan` + `TYPE=ether_info,snapshoot`、扩展轮询的 `monitor_iface`）—— **零新增请求**；整包属性不写数据库。
>
> 原先"每个物理口一个 `binary_sensor.ikuai_ethN_link`"的实体**已删除**（信息全在这里，旧注册表条目会自动清理）。要让"网线断了"触发自动化，可以监听本实体 state 变成 `异常`，或用模板触发器读 `ports.eth0.link`。
>
> 同理 `binary_sensor.ikuai_iface_fault`（线路体检告警）也已删除，内容在本实体的 `iface_check_count` / `iface_check_failed` 与 `summary.check_failed` 里（需要"体检异常"触发器的话，可以用模板二元传感器读 `summary.check_failed > 0`）。

---

## 服务

全部服务都支持可选的 `entry_id`：**留空时会对所有已加载的爱快都执行一遍**（想只打一台
就填它的条目 id —— 见各台 `switch.ikuai_api` 属性 `entry`）。`ikuai.api_call` 的返回里每条
都带 `entry_id` / `host`，便于分辨结果来自哪台。

| 服务 | 说明 |
|---|---|
| `ikuai.api_call` | **通用 API 调用**（逃生舱）：直接调 `/Action/call`，参数错误可能影响路由运行 |
| `ikuai.wake_on_lan` | 网络唤醒（发送魔术包） |
| `ikuai.wan_reconnect` | 重连指定 WAN 线路（PPPoE / DHCP） |
| `ikuai.terminal_disconnect` | 踢指定 IP 的终端下线 |
| `ikuai.mac_block` / `ikuai.mac_unblock` | MAC 拉黑（禁止上网）/ 放行 |
| `ikuai.log_get` | 即时查询某类日志并**返回数据**（可在自动化里用响应） |
| `ikuai.log_clear` | 清空指定分类日志（`all` 为全部，谨慎） |
| `ikuai.alarms_read_all` | 告警全部标记已读 |
| `ikuai.wan_line_add` / `_edit` / `_del` | 多拨（VLAN 拨号）线路的增/改/删 |
| `ikuai.stream_updown_add` / `_del` | 上下行分离（流控分流）规则的增/改/删 |

字段细节见集成目录下的 `services.yaml`（HA 的"开发者工具 → 动作"里也有中文表单）。

示例：

```yaml
# 早上把客厅电视的 MAC 放行
service: ikuai.mac_unblock
data:
  mac: "aa:bb:cc:dd:ee:ff"

# 查询最近 20 条认证日志
service: ikuai.log_get
data:
  category: pppauth
  limit: 20
response_variable: logs
```

---

## 透传查询 API

**可选功能，默认关闭。** 打开 `switch.ikuai_api` 后，集成会暴露一个只做"通道"的
HTTP 端点：请求体就是爱快原生载荷（`func_name` / `action` / `param`），响应体就是
爱快的原始响应 —— 因此爱快原生**所有**接口都能用，固件升级也不会过期。

> ⚠️ 这个通道**不限制写操作、不做脱敏**（`advanced` 里的 `sshd_passwd`、`pppuser` 的
> 密码等都会原样返回），也就是说 key 等价于**爱快后台的操作权限**。只在本机/内网使用，
> 不要外发；用完随时关掉开关（关掉后所有旧链接立即失效）。

### 请求地址

| 方式 | 说明 |
|---|---|
| `GET /ikuai/api/call` | 浏览器地址栏直接用 |
| `POST /ikuai/api/call` | 单个载荷，或 `{"calls": [...]}` 批量（一次最多 **10** 个） |

| 查询参数 | 说明 |
|---|---|
| `key` | 密钥（见下）。`key` / `entry` / `func_name` / `action` 是**保留参数**，其余一律平铺进爱快的 `param` |
| `entry` | **装了多台爱快时**指定打哪台（值为该条目 id，见 `switch.ikuai_api` 属性 `entry`）；只装一台时省略即可 |

### 鉴权（三种任选）

1. **查询串密钥**：`?key=<密钥>`；
2. **请求头密钥**：`X-IKuai-Key: <密钥>`（`switch.ikuai_api` 的属性 `auth` 里也这么写）；
3. **HA 登录态**：前端用 `hass.fetchWithAuth(...)`（不要裸 `fetch`，HA 会把未鉴权请求
   记为失败登录）。

### 多台爱快（一个集成里加多台路由器）

每台路由器都是一个独立条目，各有一个 `switch.ikuai_api` 开关和一个
`sensor.ikuai_api_key` 密钥传感器，**每台的 key 都不一样**（key 里带了条目 id）。
请求按下面顺序自动定位：

1. URL 里显式写了 `&entry=<条目 id>` → 打那一台；
2. 没写 `entry`，但给了 `key` → **用 key 自动认出是哪一台**；
3. 只有一个条目 → 就用它（单台用户照旧不用管 `entry`）。

两者都定位不到时会返回明确的 `entry_required`（**不再是会被误认成密钥错误的
`unauthorized`**，HTTP 400）：

```json
{"error": "entry_required",
 "hint": "本集成装了 2 台爱快，请求没指明是哪一台：在 URL 里加 &entry=<条目 id>…"}
```

`switch.ikuai_api` 属性 `presets` 里给的完整地址**已经带上 `entry=`**，直接复制就不会
踩这个坑，也能保证以后再加一台路由器时，今天复制出去的地址依然有效。调用统计
（`stats`）也是**按台分开**的，不会两台混在一起。

### 密钥（`sensor.ikuai_api_key`）

```yaml
sensor.ikuai_api_key:
  state: 42                     # 剩余有效分钟数（单位 min）
  attributes:
    key: a9867e51f0608d8f       # ← 复制这个用（属性不写数据库）
    entry: 01M4PYYG7GHTTVEF3Y699FQ42B   # 本实体所属那台的条目 id（多台时配 &entry= 用）
    valid_until: '2026-10-13 22:00:00'
    remaining_seconds: 2520
    algorithm: md5(集成盐 | 小时序号 | CPU指纹 | 条目id)[:16]，整点换新、旧 key 宽限 1 小时
    usage: 地址栏：/ikuai/api/call?key=<key>&func_name=…&action=show；或请求头 X-IKuai-Key: <key>
    api_enabled: true
    key_hits: 12
    cpu_fingerprint: …
```

- 每小时整点自动换新，**旧 key 再宽限 1 小时**（正在跑的脚本不会突然 401）；
- **多台爱快时每台的 key 不同**（key 里含条目 id），所以只给 key 也能定位到那台；
- 算法里的"盐"由集成随机生成、**只存在内存**、从不外露 —— 别人即使在爱快后台看到你的
  CPU 值，也算不出 key；副作用是 **重启一次 HA，之前签发的所有 key 立即失效**（重载集成
  不受影响），重新从本传感器的 `key` 属性取一次即可。

### 三种写法

```bash
# ① GET 平铺（推荐）
http://<你的HA>:8123/ikuai/api/call?key=<密钥>&func_name=homepage&action=show&TYPE=sysstat

# ①' 装了多台爱快：补上 entry（key 也能自动认出是哪台，写 entry 更明确）
http://<你的HA>:8123/ikuai/api/call?key=<密钥>&entry=<条目id>&func_name=homepage&action=show&TYPE=sysstat

# ② GET 直接塞整段 JSON（省略参数名）
http://<你的HA>:8123/ikuai/api/call?key=<密钥>&{"func_name":"url_black","action":"show","param":{"TYPE":"url_white_refer"}}

# ③ POST（复杂载荷/批量推荐）
curl -X POST -H "X-IKuai-Key: <密钥>" -H "Content-Type: application/json" \
  -d '{"func_name":"monitor_lanip","action":"show","param":{"TYPE":"data,total","limit":"0,100"}}' \
  http://<你的HA>:8123/ikuai/api/call
```

规则：`key` / `entry` / `func_name` / `action` 是保留参数，`param` 里的**每一项平铺成
同名查询参数**。GET 写法里，值含 `&` `#` `+` 空格或中文时必须 URL 编码（或改用 POST）。

### 批量与分页

爱快 `limit` 单次最多返回 100 条，**想拿全就用 `calls` 批量一次取回多页**：

```json
{"calls": [
  {"func_name": "arp", "action": "show", "param": {"TYPE": "total,data", "limit": "0,100"}},
  {"func_name": "arp", "action": "show", "param": {"TYPE": "total,data", "limit": "100,100"}},
  {"func_name": "arp", "action": "show", "param": {"TYPE": "total,data", "limit": "200,100"}}
]}
```

响应结构：

- **单次**（GET，或 POST 传单个载荷）：**原样返回**爱快的响应体；
- **批量**：`{"results": [...]}`，每项是 `{"ok": true, "call": …, "body": …}`，失败则
  `{"ok": false, "error": …, "hint": …}`；外层还有
  `"meta": {"took_ms": …, "count": …, "calls_max": 10, "truncated": false}`
  （超过 10 个会被截断并置 `truncated`）。

### 常用请求体

`switch.ikuai_api` 的属性本身就是一份说明书（`entry` / `endpoint` / `methods` / `auth` /
`key_ttl_seconds` / `calls_max` / `pagination_hint` / `note` 等），其中 `presets`
直接列好了 **12 条**常用请求体，每条同时给**完整地址栏 URL**（密钥与 entry 都已嵌进去，
复制即用）和 **POST body** 两种写法：

```yaml
presets:
  - name: 系统状态
    get: http://192.168.1.132:8123/ikuai/api/call?key=…&entry=…&func_name=homepage&action=show&TYPE=sysstat,ac_status
    post: |-
      { "func_name": "homepage", "action": "show", "param": { "TYPE": "sysstat,ac_status" } }
  - name: 在线终端列表
  - name: 单台终端·应用流量
  - name: 单台终端·连接
  - name: WAN 线路
  - name: LAN 口信息
  - name: ARP 表
  - name: DHCP 租约
  - name: 磁盘信息
  - name: 规则条数（示例：ACL）
  - name: 网址黑白名单（多参数示例）
  - name: ⚠ 写操作示例：重启
entry: 01M4PYYG7GHTTVEF3Y699FQ42B   # 本开关所属那台的条目 id（POST 也可以直接带它）
current_key: a9867e51f0608d8f     # 开关自己就带当前密钥
stats: {calls: 0, results: 0, errors: 0, key_hits: 0, last_call: null}   # 按台分开统计
```

### 怎么"抄"一个接口

爱快后台按 F12 → Network → 找到那条 `/Action/call` 请求，看它的 payload：

```json
{"func_name":"xxx","action":"yyy","param":{"A":1,"B":"c"}}
```

- **POST**：整段复制当请求体；
- **GET**：保留 `func_name` / `action`，把 `param` 里的键值用 `&` 平铺到查询串后面，
  最后补 `key=`。

---

## 虚拟机页面

每台虚拟机是一个独立设备，设备页的「访问」**直接打开该虚拟机在爱快里的页面**：

- 链接由集成签发（`/ikuai/console/<令牌>?authSig=…`，**24 小时**有效），打开时 302 到
  经同源反向代理（`/ikuai/web/`）加载的**爱快固件自带页面** —— 画面、键盘手势、剪贴板全部
  由爱快页面自己实现，集成不自建客户端、不做字节桥，随固件升级自动跟随；
- 代理对该页面的两处加工：把页面内的 WebSocket 引到 HA 同源路径
  （`/ikuai/web/Action/vnc?port=<端口>`，登录态由后端托管，路由器密码不出现在前端），
  以及在该虚拟机设有 VNC 密码时自动填入；
- 该虚拟机在爱快里**未开启 VNC** 时不会生成「访问」链接（点开会提示"该虚拟机未开启 VNC"）；
- `switch.ikuai_remote_control` 关闭时：清空所有虚拟机设备的「访问」链接，并拒绝新的跳转
  （控制台视图 403）。集成只做同源转发、不跟踪会话，所以**已经打开的页面不会被掐断**；
  重新打开后会重新签发链接；
- `image.ikuai_vm_<名>_screen` 提供虚拟机屏幕截图（抓帧快照，不是实时流）。

## Web 管理悬浮窗

- 设备页「访问」（或 `sensor.ikuai_system_info` 属性里的 `web_url_full`）会打开
  爱快**原版后台**：集成把整个 Web 界面反向代理到 `/ikuai/web/` 下（同源，登录态由
  后端托管，路由器密码不会出现在前端）；
- 前端脚本 `www_ikuai/frontend/embed.js` 负责把代理页以悬浮窗形式嵌进 HA。

---

## 实体 ID 规则与迁移

- 本集成的实体 ID 一律使用**固定英文短 ID**，例如 `sensor.ikuai_system_info`、
  `switch.ikuai_sw_snmp`（而不是拼音 ID）；
- 如果目标 ID 被**别的集成**占用（典型：同时装有旧的 `ikuai_router`），会临时退让成
  `_2`、`_3`… 并在日志里说明；删掉那个集成后重载，ID 会自动回到干净的固定值；
- 历史上的拼音 ID 会在加载时**自动迁移**（注册表改名，历史与统计随之保留）；
- 加载时还会自动清理：已并入聚合实体的旧条目、ID 过时且仍禁用的旧条目、早期
  "一个容器/一个账号一台设备"的重复设备 —— 也就是**残留问题重载一次即可自愈**。

---

## 性能与降噪建议

| 目标 | 做法 |
|---|---|
| 启动更快 | 刷新间隔别设太小；日志只取第一页（已默认）；必要时调大 `const.HTTP_CONCURRENCY` |
| 少写数据库 | 聚合实体/诊断实体的大属性已声明不写库；想连状态值也不进历史，可在 `configuration.yaml` 里加 `recorder: exclude: entity_globs: sensor.ikuai_*` |
| 界面清爽 | 诊断类实体默认归入「诊断」区；高风险开关与「在线终端列表」默认禁用 |
| 降低路由器压力 | 加大刷新间隔（如 30 秒）；不启用「在线终端列表」（不启用时终端详情**一个请求都不发**）；不需要日志就关掉「采集日志」开关（省下 12 个请求/分钟） |

---

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| 集成卡片一直显示"正在启动" | 这句其实代表**首轮抓取失败**（`ConfigEntryNotReady`），HA 会按 5 秒 → … 最长 15 分钟退避重试，**不是"还在加载"**。首轮抓取整体有 **150 秒**上限，超时同样判失败。多为爱快 Web 限流/不响应，或账号密码错误：先看日志里 `Network error visiting iKuai` / `Failed to get access token`，再把刷新间隔调大 |
| 实体显示"不可用"、属性为空 | 上游请求失败或集成正在重载；看日志中的 `Unexpected error in requestpost_json` |
| 虚拟机设备没有「访问」链接 | 该虚拟机在爱快里**没开启 VNC**（集成只给开了 VNC 的虚拟机签发链接），或 `switch.ikuai_remote_control` 被关掉了 |
| 实体 ID 变成 `xxx_2` | 目标 ID 被别的集成占用。日志里会写明是哪个集成；删掉它再重载 |
| 日志/规则类计数偏小 | 爱快 `limit` 单次最多 100 条：列表类会自动翻页，但**日志只取第一页**（有意为之，避免拖慢轮询）。要全量请用透传 API 批量翻页 |
| 在线终端列表里没有某台设备详情 | 详情按轮换抓取（每轮最多 10 台），等 1~3 个扩展轮（约 30~90 秒）会出现 |
| `sensor.ikuai_log_stats` / `_log_url_log` 消失或「不可用」 | 「采集日志」开关（`switch.ikuai_log_poll`）被关掉了 —— 关掉后不再采集日志，这两个实体会被**注册表禁用**（消失）；打开开关即自动恢复 |
| 「接口」实体 state 是 `异常` | 看它 `abnormal` 属性：列出的 WAN 口 `errmsg` 不是「线路检测成功」（多拨时一条不通也算异常）。若某条线本就没插网线（`mac` 为空）不会参与判断 |
| 「接口」实体 state 是 `未知` | 一个"带 MAC 的 WAN 口"都没有 → 没有可判断的线路（这台路由器当前没有可用 WAN） |
| 透传 API 返回 401 | 没带密钥、密钥过期（整点换新），或**重启过 HA**（加密盐在内存里，重启后旧 key 全部失效）：从 `sensor.ikuai_api_key` 属性 `key` 重新复制 |
| 透传 API 返回 `entry_required` | **装了多台爱快**而请求没指明是哪一台：用**那一台**的 `sensor.ikuai_api_key` 的 key（key 自带归属，会自动定位），或显式加 `&entry=<条目 id>`（见对应 `switch.ikuai_api` 属性 `entry`）；直接抄 `presets` 里的完整地址最省事 |
| 透传 API 返回 `entry_unknown` / `entry_not_loaded` | `entry` 值写错了 / 该条目还没加载完（重载后等几秒再试） |
| 透传 API 返回 403 | `switch.ikuai_api` 没打开，或刚被关掉（旧链接同时失效） |
| 地址栏打开透传 API 返回 401 | HA 视图要求带登录态：用 `?key=` 或 `fetchWithAuth`（浏览器 Console） |

## 已知限制

- 爱快大部分列表接口**单次最多返回 100 条**，分页由集成处理；日志类仅取第一页；
- "虚拟机屏幕画面"是抓帧快照，不是实时视频流（实时操作请点设备页「访问」打开爱快虚拟机页面）；
- 固件升级不提供一键执行（高风险操作请在爱快后台做）；
- 透传 API 的写操作**不做二次确认**，调用前请确认参数（可以先用 `show` 试探）；
- 透传 API 的 key **每小时轮换、且重启 HA 后全部失效**（加密盐只存在内存；重载集成不受
  影响）—— 固定写死 URL 的外部脚本请从 `sensor.ikuai_api_key` 取当前 key，或改用 POST +
  HA 登录态；
- 装了多台爱快时，`/ikuai/api/call` 需要能定位到具体一台：用**该台**的 key，或加
  `&entry=<条目 id>`（`switch.ikuai_api` 属性 `presets` 里的地址已经带好两者）；
- 爱快部分接口（如 `audit_url_log`）**不支持 `TYPE=total`**，集成按实际情况取数（日志类
  只取第一页 100 条）。

---

## 鸣谢

感谢所有使用与反馈问题的朋友。有问题欢迎在仓库的 Issues 里提出（附上 HA 日志中
与 `ikuai` 相关的片段会更容易定位）。
