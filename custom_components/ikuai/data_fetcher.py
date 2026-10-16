"""
iKuai API 数据抓取层
完全兼容：
1. 旧版: Result: 30000, 数据在 Data 字段
2. 新版: code: 0, 数据在 results 字段

数据组织：
- 扁平键（ikuai_xxx）：历史传感器，保持兼容
- data["ext"]：扩展模块原始/汇总数据，供新平台实体使用
- data["logs"]：由 IKuaiLogHub 注入的分类日志
"""

import logging
import json
import time
import datetime
import asyncio
from async_timeout import timeout
from aiohttp import ClientError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    ACCOUNT_DETAIL_FALLBACK_MAX,
    APP_FLOW_WINDOWS,
    APP_FLOW_WINDOW_LABELS,
    LOGIN_URL,
    ACTION_URL,
    SWITCH_TYPES,
    EXT_FETCH_EVERY,
    LIST_LIMIT,
    LIST_FETCH_LIMIT,
    LIST_MAX_PAGES,
    LIST_PAGE_SIZE,
    COUNT_LIMIT,
    DOMAIN,
    HTTP_CONCURRENCY,
    RULE_ROW_LIMIT,
    TERMINAL_DETAIL_MAX,
)

_LOGGER = logging.getLogger(__name__)

# 仅需要统计条数的表模块：(ext计数键, func_name, TYPE, total字段)
COUNT_SPECS = [
    ("acl", "acl", "total,data", "total"),
    ("conn_limit", "conn_limit", "total,data", "total"),
    ("acl_l7", "acl_l7", "total,data", "total"),
    ("acl_qq", "acl_qq", "total,data", "total"),
    ("dnat", "dnat", "total,data", "total"),
    ("nat_rule", "nat_rule", "total,data", "total"),
    ("static_rt", "static_rt", "total,data", "total"),
    ("vlan", "vlan", "total,data", "total"),
    ("stream_ipport", "stream_ipport", "total,data", "total"),
    ("stream_layer7", "stream_layer7", "total,data", "total"),
    ("stream_domain", "stream_domain", "total,data", "total"),
    ("stream_updown", "stream_updown", "total,data", "total"),
    ("simple_qos", "simple_qos", "total,data", "total"),
    ("mac_qos", "mac_qos", "total,data", "total"),
    ("alone_limit", "alone_limit", "total,data", "total"),
    ("high_prio_host", "high_prio_host", "total,data", "total"),
    ("dprotos", "dprotos", "total,data", "total"),
    ("dprotos_l7", "dprotos_l7", "total,data", "total"),
    ("port_mirror", "port_mirror", "total,data", "total"),
    ("udp_proxy", "udp_proxy", "total,data", "total"),
    ("lb_pcc", "lb_pcc", "total,data", "total"),
    ("custom_isp", "custom_isp", "total,data", "total"),
    ("upnpd_leases", "upnpd_leases", "total,data", "total"),
    ("dev_control", "dev_control", "total,data", "total"),
    ("cloud_switch", "cloud_switch", "total,data", "total"),
    ("wakeup_list", "wakeup", "total,data", "total"),
    ("coupon", "coupon", "total,data", "total"),
    ("pppuser", "pppuser", "total,data", "total"),
    ("ipgroup", "ipgroup", "total,data", "total"),
    ("macgroup", "macgroup", "total,data", "total"),
    ("dtgroup", "dtgroup", "total,data", "total"),
    ("ipv6group", "ipv6group", "total,data", "total"),
    ("domain_blacklist", "domain_blacklist", "total,data", "total"),
    ("dns", "dns", "dns_proxy,dns_proxy_total", "dns_proxy_total"),
    ("dns_replace", "dns_replace", "data,total", "total"),
    ("url_keywords", "url_keywords", "data,total", "total"),
    ("url_redirect", "url_redirect", "data,total", "total"),
    ("url_replace", "url_replace", "data,total", "total"),
]

# 列表接口返回行数组时可能使用的键名：分页合并（_show）时按第一个命中的键累积。
# data 最常用；client_data/client_list 见于 dhcp6；static_data 见 dhcp_static；
# dns_proxy 见 DNS；vlan_data 见 wan 接口。
_LIST_ROW_KEYS = ("data", "client_data", "client_list", "static_data",
                  "dns_proxy", "vlan_data", "terminal_flow")

# attrs 中需要脱敏的字段（ipsec_secret 见 L2TP/OpenVPN 服务端配置）
_SECRET_FIELDS = {"passwd", "password", "secret", "sshd_passwd", "api_token",
                  "token", "ipsec_secret"}


def _app_flow_row(block) -> dict:
    """从 app_flow 响应里取出那一行分布数据（兼容两种嵌套）。

    * ``homepage`` + ``{"TYPE":"app_flow","minute":"30"}`` → ``Data.app_flow.app_flow[0]``
    * ``monitor_system`` + ``{"TYPE":"app_flow","minute":30}`` → ``Data.app_flow[0]``
    """
    node = block.get("app_flow") if isinstance(block, dict) else None
    if isinstance(node, dict):
        node = node.get("app_flow")
    if isinstance(node, list) and node and isinstance(node[0], dict):
        return node[0]
    return {}


def _scrub(row):
    """删除敏感字段，避免出现在实体属性里。"""
    if isinstance(row, dict):
        for k in list(row.keys()):
            if k in _SECRET_FIELDS:
                row[k] = "***"
            elif isinstance(row[k], (dict, list)):
                _scrub(row[k])
    elif isinstance(row, list):
        for item in row:
            _scrub(item)
    return row


class DataFetcher:
    """Class to fetch data from iKuai router."""

    def __init__(self, hass, host, username, passwd, pas, tracker_config, custom_switches_config=None):
        """Initialize the data fetcher."""
        self._host = host
        self._username = username
        self._passwd = passwd
        self._pass = pas
        self._hass = hass
        self._session_client = async_get_clientsession(hass, verify_ssl=False)
        self._datatracker = {}
        self._datarefreshtimes = {}
        self._tracker_config = tracker_config if tracker_config else {}
        self._custom_switches_config = custom_switches_config or {}
        # 全局并发上限（见 const.HTTP_CONCURRENCY）：请求一多，3 会让启动/扩展轮
        # 排成十几批，明显拖慢；6 是"不打满软路由"与"别排太久"的折中。
        self._semaphore = asyncio.Semaphore(HTTP_CONCURRENCY)

        # 扩展数据
        self._ext_tick = 0
        self._ext_cache = {}
        self.last_ext = {}
        self._unsupported = set()
        self.log_cache = {}

    def is_json(self, jsonstr):
        """Check if a string is valid JSON."""
        try:
            json.loads(jsonstr)
        except (ValueError, TypeError):
            return False
        return True

    def _header(self, sess_key):
        return {
            "Cookie": f"username={self._username}; login=1; sess_key={sess_key}",
            "Content-Type": "application/json;charset=UTF-8",
        }

    async def _post_once(self, url, headerstr, json_body):
        """单次 POST，返回解析后的 JSON 或文本；失败返回异常标记。"""
        async with timeout(15):
            async with self._session_client.post(url, headers=headerstr, json=json_body) as response:
                if response.status != 200:
                    return None
                text = await response.text()
                if self.is_json(text):
                    return json.loads(text)
                return text

    async def requestpost_json(self, url, headerstr, json_body):
        """Send an asynchronous POST request and return JSON data.

        软路由在突发并发时偶发挂起连接，超时/断连自动重试一次。
        """
        for attempt in (1, 2):
            async with self._semaphore:
                try:
                    return await self._post_once(url, headerstr, json_body)
                except (ClientError, asyncio.TimeoutError) as e:
                    if attempt == 1:
                        _LOGGER.debug("iKuai 请求失败，重试一次: %r", e)
                        await asyncio.sleep(0.5)
                        continue
                    _LOGGER.warning("Network error visiting iKuai: %r", e)
                    return None
                except Exception as e:
                    _LOGGER.error("Unexpected error in requestpost_json: %r", e)
                    return None
        return None

    async def requestpost_cookies(self, url, headerstr, json_body):
        """Send a POST request and extract the sess_key from cookies."""
        async with self._semaphore:
            try:
                async with timeout(10):
                    async with self._session_client.post(url, headers=headerstr, json=json_body) as response:
                        if response.status != 200:
                            return None
                        for cookie in response.cookies:
                            if cookie == "sess_key":
                                return response.cookies["sess_key"].value
                        return None
            except Exception as e:
                _LOGGER.error("Error in requestpost_cookies: %s", e)
                return None

    async def _login_ikuai(self):
        """Perform login to iKuai and return the session key."""
        header = {"Content-Type": "application/json;charset=UTF-8"}
        json_body = {"username": self._username, "passwd": self._passwd, "pass": self._pass}
        url = self._host + LOGIN_URL
        try:
            return await self.requestpost_cookies(url, header, json_body)
        except Exception:
            return None

    def seconds_to_dhms(self, seconds):
        """Convert seconds to a readable Day-Hour-Minute-Second format."""
        try:
            seconds = int(seconds)
            days = seconds // (3600 * 24)
            hours = (seconds // 3600) % 24
            minutes = (seconds // 60) % 60
            seconds = seconds % 60
            if days > 0: return f"{days}天{hours}小时{minutes}分钟"
            if hours > 0: return f"{hours}小时{minutes}分钟"
            if minutes > 0: return f"{minutes}分钟{seconds}秒"
            return f"{seconds}秒"
        except Exception:
            return "Unknown"

    def _get_data_block(self, resdata):
        """Helper to extract data block compatible with both API versions."""
        if not isinstance(resdata, dict): return None
        # 兼容性判断：成功状态码可能是 code=0 (新) 或 Result=30000 (旧)
        if resdata.get("code") == 0 or resdata.get("Result") == 30000:
            # 数据块可能在 results (新) 或 Data (旧)
            return resdata.get("results") or resdata.get("Data")
        return None

    @staticmethod
    def _limit_count(param):
        """从 param 的 limit（"偏移,条数"）里取出条数；取不到返回 None。"""
        try:
            return int(str((param or {}).get("limit", "")).split(",")[1])
        except (IndexError, TypeError, ValueError):
            return None

    async def _show_once(self, sess_key, func, param=None, action="show"):
        """单次 show 调用。返回 data block；401 返回 401；不支持/失败返回 None。"""
        if func in self._unsupported:
            return None
        json_body = {"func_name": func, "action": action, "param": param or {}}
        resdata = await self.requestpost_json(self._host + ACTION_URL, self._header(sess_key), json_body)
        if not isinstance(resdata, dict):
            return None
        if resdata.get("Result") == 10014 or resdata.get("code") == 10014:
            return 401
        result_code = resdata.get("Result")
        if result_code == 30002 and "Not found funcname" in str(resdata.get("ErrMsg", "")):
            _LOGGER.debug("iKuai 固件不支持模块 %s，后续轮询跳过", func)
            self._unsupported.add(func)
            return None
        return self._get_data_block(resdata)

    async def _show(self, sess_key, func, param=None, action="show"):
        """通用 show 调用：**列表类查询自动翻页取全**。

        爱快列表接口的 limit 是"偏移,条数"，而且单次最多返回 100 条 —— 传
        "0,10000" 也只会拿到第一页，数据就不全了。所以当 limit 里的条数 ≥
        单页上限时，这里按 0,100 / 100,100 / 200,100 … 逐页取，把行数组合并后
        返回同一个 block（其余字段如 total 取自第一页）。

        某页没取满就说明到底了，所以列表不长时不会多出请求；明确只要少量行的
        查询（如消息 "0,10"、应用流量 "0,15"）保持单次请求，不受影响。
        """
        if (self._limit_count(param) or 0) < LIST_PAGE_SIZE:
            return await self._show_once(sess_key, func, param, action)

        merged = None
        rows: list = []
        row_key = None
        for page in range(LIST_MAX_PAGES):
            page_param = dict(param or {},
                              limit=f"{page * LIST_PAGE_SIZE},{LIST_PAGE_SIZE}")
            block = await self._show_once(sess_key, func, page_param, action)
            if block == 401:
                return 401 if merged is None else merged
            if not isinstance(block, dict):
                break
            if merged is None:
                merged = dict(block)
            if row_key is None:
                for key in _LIST_ROW_KEYS:
                    if isinstance(block.get(key), list):
                        row_key = key
                        break
            if row_key is None:
                # 该接口这次没返回行数组（可能只有 total）→ 保持单次结果
                break
            page_rows = block.get(row_key) or []
            rows.extend(page_rows)
            if len(page_rows) < LIST_PAGE_SIZE:
                break
        if merged is None:
            return None
        if row_key is not None:
            merged[row_key] = rows
        return merged

    async def _get_ikuai_status(self, sess_key, data_dict, ext):
        """Fetch general system status and AC information."""
        json_body = {"func_name": "homepage", "action": "show", "param": {"TYPE": "sysstat,ac_status"}}
        resdata = await self.requestpost_json(
            self._host + ACTION_URL, self._header(sess_key), json_body)

        if isinstance(resdata, dict) and resdata.get("Result") == 10014: return 401

        data_block = self._get_data_block(resdata)
        if not data_block: return

        sysstat = data_block.get("sysstat", {})
        ac_status = data_block.get("ac_status", {})

        if sysstat:
            verinfo = sysstat.get("verinfo", {}) or {}
            data_dict["sw_version"] = verinfo.get("verstring", "Unknown")
            data_dict["device_name"] = sysstat.get("hostname", "iKuai")
            uptime = int(sysstat.get("uptime", 0) or 0)
            data_dict["uptime_seconds"] = uptime
            data_dict["ikuai_uptime"] = self.seconds_to_dhms(uptime)

            cputemp_list = sysstat.get("cputemp", [])
            data_dict["ikuai_cputemp"] = cputemp_list[0] if (isinstance(cputemp_list, list) and cputemp_list) else None

            cpu_list = sysstat.get("cpu", [])
            data_dict["ikuai_cpu"] = str(cpu_list[0]).replace("%", "") if (isinstance(cpu_list, list) and cpu_list) else "0"
            # 完整 CPU 占用列表（第 0 项是总占用，其后按核给出）与每核频率，
            # 供「每核 CPU 占用」动态实体使用
            ext["cpu_cores_usage"] = [str(x) for x in cpu_list] if isinstance(cpu_list, list) else []
            freq_list = sysstat.get("freq", [])
            ext["cpu_freq_list"] = [str(x) for x in freq_list] if isinstance(freq_list, list) else []

            memory = sysstat.get("memory", {}) or {}
            data_dict["ikuai_memory"] = str(memory.get("used", "")).replace("%", "")
            data_dict["ikuai_memory_attrs"] = memory
            ext["memory_raw"] = memory

            online_user = sysstat.get("online_user", {}) or {}
            data_dict["ikuai_online_user"] = online_user.get("count", 0)
            data_dict["ikuai_online_user_attrs"] = online_user

            stream = sysstat.get("stream", {}) or {}
            data_dict["ikuai_connect_num"] = int(stream.get("connect_num", 0))
            data_dict["ikuai_upload"] = round(stream.get("upload", 0) / 1024 / 1024, 3)
            data_dict["ikuai_download"] = round(stream.get("download", 0) / 1024 / 1024, 3)
            data_dict["ikuai_total_up"] = round(stream.get("total_up", 0) / 1024 / 1024 / 1024, 2)
            data_dict["ikuai_total_down"] = round(stream.get("total_down", 0) / 1024 / 1024 / 1024, 2)

            ext["verinfo"] = _scrub(dict(verinfo))
            ext["link_status"] = sysstat.get("link_status")
            data_dict["ikuai_ap_online"] = ac_status.get("ap_online", 0)
            data_dict["ikuai_ap_online_attrs"] = ac_status
            data_dict["querytime"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return

    async def _get_ikuai_waninfo(self, sess_key, data_dict, ext):
        json_body = {"func_name": "lan", "action": "show", "param": {"TYPE": "ether_info,snapshoot"}}
        resdata = await self.requestpost_json(
            self._host + ACTION_URL, self._header(sess_key), json_body)

        data_block = self._get_data_block(resdata)
        if not data_block: return

        ext["ether_info"] = data_block.get("ether_info")
        ext["snapshoot_lan"] = _scrub(data_block.get("snapshoot_lan"))
        snapshoot_wan = data_block.get("snapshoot_wan")
        ext["snapshoot_wan"] = _scrub(snapshoot_wan)
        wan_online = False
        if isinstance(snapshoot_wan, list):
            for item in snapshoot_wan:
                if item.get("default_route") == 1:
                    data_dict["ikuai_wan_ip"] = item.get("ip_addr", "")
                    data_dict["ikuai_wan_ip_attrs"] = item
                    up_time = item.get("updatetime", 0)
                    data_dict["ikuai_wan_uptime"] = self.seconds_to_dhms(int(time.time() - up_time)) if up_time > 0 else ""
                    if item.get("ip_addr"):
                        wan_online = True
                elif item.get("internet") in [3, 4]:
                    await self._get_ikuai_showvlan(sess_key, item.get("interface"), data_dict)
        data_dict["wan_online"] = wan_online

    async def _get_ikuai_showvlan(self, sess_key, interface, data_dict):
        # 列表接口（limit 是"偏移,条数"）统一交给 _show 自动翻页
        data_block = await self._show(
            sess_key, "wan", {"TYPE": "vlan_data,vlan_total", "interface": interface,
                              "limit": LIST_FETCH_LIMIT})
        if data_block and isinstance(data_block, dict) \
                and isinstance(data_block.get("vlan_data"), list):
            for vlan in data_block["vlan_data"]:
                if vlan.get("default_route") == 1:
                    data_dict["ikuai_wan_ip"] = vlan.get("pppoe_ip_addr", "")
                    up_time = vlan.get("pppoe_updatetime", 0)
                    data_dict["ikuai_wan_uptime"] = self.seconds_to_dhms(int(time.time() - up_time)) if up_time > 0 else ""

    async def _get_ikuai_lan6info(self, sess_key, data_dict):
        json_body = {"func_name": "ipv6", "action": "show", "param": {"TYPE": "lan_data,lan_total"}}
        resdata = await self.requestpost_json(
            self._host + ACTION_URL, self._header(sess_key), json_body)
        data_block = self._get_data_block(resdata)
        if data_block and isinstance(data_block.get("lan_data"), list) and data_block["lan_data"]:
            data_dict["ikuai_lan6_ip"] = data_block["lan_data"][0].get("ipv6_addr", "")

    async def _get_ikuai_wan6info(self, sess_key, data_dict):
        json_body = {"func_name": "ipv6", "action": "show", "param": {"TYPE": "data,total"}}
        resdata = await self.requestpost_json(
            self._host + ACTION_URL, self._header(sess_key), json_body)
        data_block = self._get_data_block(resdata)
        if data_block and isinstance(data_block.get("data"), list) and data_block["data"]:
            data_dict["ikuai_wan6_ip"] = data_block["data"][0].get("dhcp6_ip_addr", "")

    async def _get_ikuai_mac_control(self, sess_key, data_dict):
        # MAC 黑名单是列表接口：交给 _show 翻页（黑名单超过 100 条时也能取全）
        data_block = await self._show(
            sess_key, "acl_mac", {"TYPE": "total,data", "limit": LIST_LIMIT})
        data_dict["mac_control"] = _scrub(data_block.get("data", "")) \
            if isinstance(data_block, dict) else ""

    async def _get_ikuai_pppoe_info(self, sess_key, data_dict, ext):
        data_block = await self._show(
            sess_key, "wan", {"TYPE": "vlan_data,vlan_total", "limit": LIST_FETCH_LIMIT})
        _LOGGER.debug("PPPoE API响应: %s", "ok" if data_block else data_block)
        if not isinstance(data_block, dict):
            return
        if data_block:
            vlan_data = data_block.get("vlan_data", []) or []
            vlan_data = _scrub(vlan_data)
            ext["wan_lines"] = vlan_data
            self._ext_cache["wan_lines"] = vlan_data
            # 计算总数量和在线数量
            online_rows = [x for x in vlan_data
                           if x.get("pppoe_ip_addr") and x.get("pppoe_updatetime", 0) > 0]
            data_dict["ikuai_pppoe_total"] = len(vlan_data)
            data_dict["ikuai_pppoe_count"] = len(online_rows)
            # 确保失败数量不为负数
            data_dict["ikuai_pppoe_fail_count"] = max(0, len(vlan_data) - len(online_rows))
            if online_rows and not data_dict.get("ikuai_wan_ip"):
                default = next((x for x in online_rows if x.get("default_route") == 1), online_rows[0])
                data_dict.setdefault("ikuai_wan_ip", default.get("pppoe_ip_addr", ""))
                data_dict["wan_online"] = True

    async def _get_ikuai_pptp_info(self, sess_key, data_dict, ext):
        data_block = await self._show(
            sess_key, "pptp_client", {"TYPE": "total,data", "limit": LIST_FETCH_LIMIT,
                                      "ORDER_BY": "", "ORDER": ""})
        if isinstance(data_block, dict) and data_block:
            pptp_data = _scrub(data_block.get("data", [])) or []
            ext["pptp_lines"] = pptp_data
            data_dict["ikuai_pptp_total"] = len(pptp_data)
            data_dict["ikuai_pptp_count"] = len([
                x for x in pptp_data if x.get("updatetime") and x.get("ip_addr")])
            data_dict["ikuai_pptp_fail_count"] = max(
                0, data_dict["ikuai_pptp_total"] - data_dict["ikuai_pptp_count"])

    async def _get_ikuai_l2tp_info(self, sess_key, data_dict, ext):
        data_block = await self._show(
            sess_key, "l2tp_client", {"TYPE": "total,data", "limit": LIST_FETCH_LIMIT,
                                      "ORDER_BY": "", "ORDER": ""})
        if isinstance(data_block, dict) and data_block:
            l2tp_data = _scrub(data_block.get("data", [])) or []
            ext["l2tp_lines"] = l2tp_data
            data_dict["ikuai_l2tp_total"] = len(l2tp_data)
            data_dict["ikuai_l2tp_count"] = len([
                x for x in l2tp_data if x.get("updatetime") and x.get("ip_addr")])
            data_dict["ikuai_l2tp_fail_count"] = max(
                0, data_dict["ikuai_l2tp_total"] - data_dict["ikuai_l2tp_count"])

    async def _get_all_lan_hosts(self, sess_key):
        # 列表接口：交给 _show 自动翻页（终端超过 100 台时也能取全）
        block = await self._show(sess_key, "monitor_lanip",
                                 {"TYPE": "data,total", "limit": LIST_FETCH_LIMIT})
        rows = block.get("data") if isinstance(block, dict) else None

        online_devices = {"ip": {}, "mac": {}}
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            if item.get("ip_addr"): online_devices["ip"][item["ip_addr"]] = item
            if item.get("mac"): online_devices["mac"][item["mac"].lower()] = item
        return online_devices

    async def _get_lan_hosts_v6(self, sess_key):
        """IPv6 终端监控明细（状态监控 → IPv6终端监控），返回整行列表。"""
        block = await self._show(sess_key, "monitor_lanipv6",
                                 {"TYPE": "data,total", "limit": LIST_FETCH_LIMIT})
        rows = block.get("data") if isinstance(block, dict) else None
        return _scrub(rows or [])

    async def _get_ikuai_switch(self, sess_key, name, show_body, show_on, show_off, data_dict):
        resdata = await self.requestpost_json(
            self._host + ACTION_URL, self._header(sess_key), show_body)
        data_block = self._get_data_block(resdata)
        if not data_block: return

        check_data = data_block.get("data")[0] if (isinstance(data_block.get("data"), list) and data_block["data"]) else data_block

        is_on = all(check_data.get(k) == v for k, v in show_on.items())
        data_dict["switch"].append({"name": name, "onoff": "on" if is_on else "off"})

    # ------------------------------------------------------------------
    # 扩展模块抓取
    # ------------------------------------------------------------------

    @staticmethod
    def _first_row(key="data"):
        def _t(block):
            rows = block.get(key)
            return rows[0] if isinstance(rows, list) and rows else None
        return _t

    async def _ext_put(self, sess_key, errors, key, func, param=None, transform=None, action="show"):
        try:
            block = await self._show(sess_key, func, param, action)
            if block == 401:
                errors.append(401)
                return
            if block is not None:
                value = transform(block) if transform else block
                if value is not None:
                    self._ext_cache[key] = value
        except Exception as e:  # 单个模块失败不影响其他模块
            _LOGGER.debug("扩展模块 %s 抓取失败: %s", func, e)

    async def _ext_wan_bands(self, sess_key, errors):
        block = await self._show(sess_key, "wan", {"TYPE": "data,total",
                                                   "limit": LIST_FETCH_LIMIT})
        if block == 401:
            errors.append(401)
            return
        if not block:
            return
        bands = _scrub(block.get("data", [])) or []
        snap = {i.get("id"): i for i in (self._ext_cache.get("snapshoot_wan") or []) if isinstance(i, dict)}
        for band in bands:
            bid = band.get("id")
            if bid in snap:
                for k, v in snap[bid].items():
                    band.setdefault(k, v)
            upd = band.get("pppoe_updatetime") or band.get("updatetime") or 0
            ip = band.get("pppoe_ip_addr") or band.get("ip_addr") or ""
            band["_online"] = bool(ip) and int(upd or 0) > 0
        self._ext_cache["wan_bands"] = bands

    async def _ext_iface(self, sess_key, errors):
        block = await self._show(sess_key, "monitor_iface",
                                 {"TYPE": "iface_check,iface_stream"})
        if block == 401:
            errors.append(401)
            return
        if not block:
            return
        check_rows = block.get("iface_check") or []
        stream_rows = block.get("iface_stream") or []
        self._ext_cache["iface_check"] = check_rows
        self._ext_cache["iface_stream"] = stream_rows
        fail_rows = [r for r in check_rows
                     if r.get("result") not in ("success", "", None)]
        self._ext_cache["iface_fail_rows"] = fail_rows
        self._ext_cache["iface_fail"] = len(fail_rows)

    async def _ext_arp(self, sess_key, errors):
        block = await self._show(sess_key, "arp", {
            "TYPE": "total,data", "ORDER_BY": "ip_addr_int",
            "orderType": "IP", "ORDER": "asc", "limit": LIST_FETCH_LIMIT})
        if block == 401:
            errors.append(401)
            return
        if not block:
            return
        rows = _scrub(block.get("data", [])) or []
        bound_rows = [r for r in rows if str(r.get("bind_state")) not in ("0", "")]
        self._ext_cache["arp_rows"] = rows
        self._ext_cache["arp_total"] = block.get("total", len(rows))
        self._ext_cache["arp_bound"] = len(bound_rows)
        self._ext_cache["arp_bound_rows"] = bound_rows

    async def _ext_dhcp(self, sess_key, errors):
        def _acl(block, mode_key):
            """MAC 黑白名单：mode 0=黑名单 1=白名单。"""
            mode = block.get(mode_key, 0)
            return {
                "rows": _scrub(block.get("data", [])),
                "total": block.get("total", 0),
                "mode": mode,
                "mode_text": "白名单" if str(mode) == "1" else "黑名单",
            }

        specs = [
            # DHCP 服务端配置（接口/地址池/网关/DNS/租约/启用）
            ("dhcp_servers", "dhcp_server",
             {"TYPE": "total,data", "limit": LIST_FETCH_LIMIT},
             lambda b: _scrub(b.get("data", []))),
            # 动态租约（IPv4）：按剩余租期倒序（与爱快后台「终端列表」默认一致）
            ("dhcp_leases", "dhcp_lease",
             {"TYPE": "data,total", "ORDER_BY": "timeout", "ORDER": "desc",
              "limit": LIST_FETCH_LIMIT},
             lambda b: {"rows": _scrub(b.get("data", [])), "total": b.get("total", 0)}),
            # 动态租约（IPv6）
            ("dhcp6_leases", "dhcp6_lease",
             {"TYPE": "client_list,client_total", "ORDER_BY": "expires", "ORDER": "desc",
              "limit": LIST_FETCH_LIMIT},
             lambda b: {"rows": _scrub(b.get("client_data", b.get("client_list", []))),
                        "total": b.get("client_total", 0)}),
            # 静态分配（别名 static_data/static_total；dhcpd_arp=兼容ARP开关）
            ("dhcp_static", "dhcp_static",
             {"TYPE": "static_total,static_data,dhcpd_arp", "limit": LIST_FETCH_LIMIT},
             lambda b: {"rows": _scrub(b.get("static_data", [])),
                        "total": b.get("static_total", 0),
                        "compat_arp": b.get("dhcpd_arp", 0)}),
            # MAC 黑白名单（IPv4 / IPv6）
            ("dhcp_acl_mac", "dhcp_acl_mac",
             {"TYPE": "total,data,dhcp_acl_mac", "limit": LIST_FETCH_LIMIT},
             lambda b: _acl(b, "dhcp_acl_mac")),
            ("dhcp6_acl_mac", "dhcp6_acl_mac",
             {"TYPE": "total,data,dhcp6_acl_mac", "limit": LIST_FETCH_LIMIT},
             lambda b: _acl(b, "dhcp6_acl_mac")),
        ]
        for key, func, param, tf in specs:
            await self._ext_put(sess_key, errors, key, func, param, tf)
        leases = self._ext_cache.get("dhcp_leases") or {}
        if leases:
            self._ext_cache["dhcp_leases_total"] = leases.get("total", 0)
        v6 = self._ext_cache.get("dhcp6_leases") or {}
        if v6:
            self._ext_cache["dhcp6_total"] = v6.get("total", 0)
        # ARP 全局选项（arp_filter 等）
        block = await self._show(sess_key, "arp", {"TYPE": "options"})
        if block == 401:
            errors.append(401)
        elif block:
            self._ext_cache["arp_options"] = block

    async def _ext_messages(self, sess_key, errors):
        block = await self._show(sess_key, "ikmessages",
                                 {"TYPE": "total,data", "ORDER_BY": "id", "ORDER": "desc", "limit": "0,10"})
        if block == 401:
            errors.append(401)
            return
        unread_block = await self._show(sess_key, "ikmessages", {"TYPE": "total", "FILTER1": "read,==,0"})
        if unread_block == 401:
            errors.append(401)
        elif block:
            self._ext_cache["messages"] = {
                "total": block.get("total", 0),
                "unread": (unread_block or {}).get("total", 0) if isinstance(unread_block, dict) else 0,
                "rows": _scrub(block.get("data", [])),
            }

    async def _ext_alarms(self, sess_key, errors):
        # 未读数是按行数数出来的，所以这里要取全（limit 走列表分页），
        # 否则告警超过一页时未读告警实体会偏小
        block = await self._show(sess_key, "rt_collect",
                                 {"TYPE": "warning_notify,data,total",
                                  "ORDER_BY": "id", "ORDER": "desc",
                                  "limit": LIST_FETCH_LIMIT})
        if block == 401:
            errors.append(401)
            return
        if block:
            rows = _scrub(block.get("data", [])) or []
            self._ext_cache["alarms"] = {
                "warning_notify": block.get("warning_notify"),
                "total": block.get("total", 0),
                "unread": len([r for r in rows if str(r.get("readflag")) == "0"]),
                "rows": rows,
            }

    async def _ext_app_flow(self, sess_key, errors):
        """应用流量分布：一次抓三个时间窗口（近30分钟 / 近1小时 / 近1日）。"""
        results = await asyncio.gather(
            *[self._show(sess_key, "homepage", {"TYPE": "app_flow", "minute": minute})
              for _key, minute in APP_FLOW_WINDOWS],
            return_exceptions=True)

        windows = {}
        for (key, minute), block in zip(APP_FLOW_WINDOWS, results):
            if block == 401:
                errors.append(401)
                continue
            if not isinstance(block, dict):
                continue
            row = _app_flow_row(block)
            if not row:
                continue
            windows[key] = {"label": APP_FLOW_WINDOW_LABELS[key],
                            "minute": int(minute), **row}

        if windows:
            self._ext_cache["app_flow_windows"] = windows
            # 兼容原有「应用流量分布」传感器：state = 近30分钟总量
            first = windows.get("30min")
            if first:
                self._ext_cache["app_flow_total"] = first.get("Total")
                self._ext_cache["app_flow"] = {k: v for k, v in first.items()
                                               if k not in ("label", "minute")}

        block2 = await self._show(sess_key, "monitor_app_flow", {"TYPE": "flow", "limit": "0,15"})
        if block2 == 401:
            errors.append(401)
        elif block2:
            self._ext_cache["app_rows"] = _scrub(block2.get("data", []))

    async def _ext_auth_servers(self, sess_key, errors):
        """本地认证 / VPN 服务端（PPPoE・PPTP・L2TP・OpenVPN）的启停与参数。

        服务端配置很少改，跟着扩展轮询（每 3 个主轮询一次）抓一次就够。

        **证书 / 私钥 / 密钥不落进 ext**：RADIUS 的 ``secret`` 与 IPsec 的
        ``ipsec_secret`` 由 ``_scrub`` 打成 ``***``（只表明"设了密码"），OpenVPN 的
        ``ca`` / ``cert`` / ``key`` / ``tls_auth`` 是整段 PEM，直接丢弃、只留
        ``has_ca`` / ``has_cert`` / ``has_key`` / ``has_tls_auth`` 四个布尔。
        """
        specs = (
            ("pppoe", "pppoe_server", {"TYPE": "data"}),
            ("pptp", "pptp_server", {}),
            ("l2tp", "l2tp_server", {}),
            ("openvpn", "openvpn-server", {"TYPE": "data,status"}),
        )
        out = {}
        for key, func, param in specs:
            block = await self._show(sess_key, func, param)
            if block == 401:
                errors.append(401)
                return
            if not isinstance(block, dict):
                continue
            rows = block.get("data")
            if not isinstance(rows, list) or not rows \
                    or not isinstance(rows[0], dict):
                continue
            row = _scrub(dict(rows[0]))
            if key == "openvpn":
                for field, flag in (("ca", "has_ca"), ("cert", "has_cert"),
                                    ("key", "has_key"),
                                    ("tls_auth", "has_tls_auth")):
                    row[flag] = bool(row.pop(field, ""))
                if block.get("status") is not None:
                    row["status"] = block["status"]
            out[key] = row
        if out:
            self._ext_cache["auth_servers"] = out

    async def _ext_counts(self, sess_key, errors):
        counts = {}
        rows_map = {}
        for name, func, typespec, total_key in COUNT_SPECS:
            block = await self._show(
                sess_key, func, {"TYPE": typespec, "limit": RULE_ROW_LIMIT})
            if block == 401:
                errors.append(401)
                break
            if isinstance(block, dict):
                val = block.get(total_key)
                if isinstance(val, int):
                    counts[name] = val
                # 明细行全量缓存（属性渲染时再截断展示）；各模块行键默认为 data，DNS 为 dns_proxy
                row_key = "dns_proxy" if name == "dns" else "data"
                rows = block.get(row_key)
                if isinstance(rows, list):
                    rows_map[name] = _scrub(rows)
        if counts:
            self._ext_cache["counts"] = counts
        if rows_map:
            self._ext_cache["rule_rows"] = rows_map

    def _terminal_detail_wanted(self):
        """「在线终端列表」实体启用时才去抓设备详情。

        该实体**默认禁用**；没启用还按 IP 逐个去抓，纯粹白费请求、
        还会给爱快的 Web 接口（全局并发只有 3、超时 15s 且失败重试一次）加压。
        查不到注册表（环境异常）时按"要抓"处理，功能优先。
        """
        try:
            from homeassistant.helpers import entity_registry as er
            registry = er.async_get(self._hass)
            entity_id = registry.async_get_entity_id(
                "sensor", DOMAIN, f"{DOMAIN}_lan_hosts_{self._host}")
            if not isinstance(entity_id, str) or not entity_id:
                return False       # 注册表里没有这个实体（从未创建）
            entry = registry.async_get(entity_id)
            if entry is None:
                return False
            return not getattr(entry, "disabled_by", None)
        except Exception:  # pylint: disable=broad-except
            return True

    async def _ext_terminal_detail(self, sess_key, errors, ext):
        """在线终端的「设备详情」：应用流量明细 + 连接明细。

        两个接口都是"一个终端一次请求"：

        * ``monitor_lanip`` + ``TYPE=terminal_flow`` + ``ip`` → 各应用上下行累计；
        * ``monitor_lanip`` + ``TYPE=conn,conn_num`` + ``ip`` → 连接列表（对端
          地址/端口、所属应用、所属接口、该连接的上下行累计）。

        所以限量 + 轮换：优先抓还没有详情的终端，其次抓最久没更新的，每轮最多
        ``TERMINAL_DETAIL_MAX`` 台。连接明细固定只取第一页（``limit=0,100``，走
        ``_show_once`` 不触发自动翻页），真实条数由接口的 ``conn_num`` 给出 ——
        连接多的终端（NAT 场景几百条）不会把请求量放大。

        另外两道闸：实体没启用就一个请求都不发；连续两台都失败立刻收手（单请求
        最坏要 15s 超时 + 重试，连续失败时不能把整轮轮询拖成几分钟）。
        """
        hosts = ext.get("lan_hosts") or []
        if not hosts or not self._terminal_detail_wanted():
            self._ext_cache.pop("terminal_detail", None)
            return
        cache = self._ext_cache.setdefault("terminal_detail", {})
        ips = []
        for row in hosts:
            if not isinstance(row, dict):
                continue
            ip = row.get("ip_addr")
            if ip and ip not in ips:
                ips.append(ip)
        if not ips:
            cache.clear()      # 一个终端都不在线：明细整体作废
            return
        # 已下线的终端：明细一起清掉，属性里不留幽灵设备
        for ip in [i for i in cache if i not in ips]:
            cache.pop(ip, None)
        fresh = [ip for ip in ips if ip not in cache]
        stale = sorted((ip for ip in ips if ip in cache),
                       key=lambda ip: (cache.get(ip) or {}).get("updated") or 0)
        fails = 0
        for ip in (fresh + stale)[:max(1, int(TERMINAL_DETAIL_MAX))]:
            flow_block = await self._show(
                sess_key, "monitor_lanip",
                {"TYPE": "terminal_flow", "ip": ip, "limit": "0,100",
                 "ORDER_BY": "", "ORDER": ""})
            if flow_block == 401:
                errors.append(401)
                return
            apps = (flow_block.get("terminal_flow")
                    if isinstance(flow_block, dict) else None)

            conn_block = await self._show_once(
                sess_key, "monitor_lanip",
                {"TYPE": "conn,conn_num", "ip": ip, "interface": "all",
                 "proto": "all", "maxnum": 500, "limit": "0,100",
                 "ORDER_BY": "", "ORDER": ""})
            if conn_block == 401:
                errors.append(401)
                return
            conns = conn_block.get("conn") if isinstance(conn_block, dict) else None
            conn_num = (conn_block.get("conn_num")
                        if isinstance(conn_block, dict) else None)

            if not isinstance(apps, list) and not isinstance(conns, list):
                fails += 1
                if fails >= 3:
                    _LOGGER.debug("iKuai 终端详情连续失败，本轮剩余终端跳过")
                    break
                continue
            fails = 0
            cache[ip] = {
                "updated": time.time(),
                "apps": _scrub(apps) if isinstance(apps, list) else [],
                "conns": _scrub(conns) if isinstance(conns, list) else [],
                "conn_num": conn_num if isinstance(conn_num, int)
                else len(conns or []),
            }

    async def _fetch_extended(self, sess_key, ext):
        """抓取一次完整扩展数据，写入 self._ext_cache。"""
        errors: list = []
        p = self._ext_put
        first = self._first_row
        tasks = [
            # ---- 系统设置 ----
            p(sess_key, errors, "basic", "basic",
              {"TYPE": "timestamp,data,interface,support_fast"}, first()),
            p(sess_key, errors, "advanced", "advanced", {}, first()),
            p(sess_key, errors, "sysctl", "ik_sysctl", {"TYPE": "data"}, first()),
            p(sess_key, errors, "watchdog", "watchdog", {}, first()),
            p(sess_key, errors, "hardware", "hardwareinfo", {}, lambda b: _scrub(b)),
            p(sess_key, errors, "irq", "irqbalance", {"TYPE": "cpulist,freqlist"}),
            # 磁盘：抓全量磁盘（每块盘的分区/挂载容量供「磁盘使用率」动态实体使用）
            p(sess_key, errors, "disk", "disk_mgmt", {"TYPE": "data"},
              lambda b: _scrub(b.get("data", []))),
            # 远程访问：含 sshd_passwd，必须脱敏后再进 ext（会被写进状态属性）
            p(sess_key, errors, "remote", "remote_control", {},
              lambda b: _scrub(b["data"][0])
              if isinstance(b.get("data"), list) and b["data"] else None),
            p(sess_key, errors, "webuser", "webuser",
              {"TYPE": "data", "limit": LIST_FETCH_LIMIT},
              lambda b: _scrub(b.get("data", []))),
            # 固件升级：整块（data=版本/新版本/库版本，auto_upgrade=自动升级开关状态，
            # fileinfo=可升级文件）
            p(sess_key, errors, "upgrade", "upgrade",
              {"TYPE": "auto_upgrade,data,fileinfo"}, lambda b: _scrub(b)),
            p(sess_key, errors, "register", "register", {"TYPE": "data,gwid"}),
            p(sess_key, errors, "backup", "backup", {"TYPE": "data"}),
            # ---- 服务 ----
            p(sess_key, errors, "netsnmp", "netsnmp", {}, first()),
            p(sess_key, errors, "smbd", "smbd", {"TYPE": "data"}, first()),
            p(sess_key, errors, "ftp", "ftp_server", {"TYPE": "ftp_status"}),
            p(sess_key, errors, "upnpd", "upnpd", {"TYPE": "interface_wan,data"}, first()),
            p(sess_key, errors, "webauth", "webauth",
              {"TYPE": "data,sync_interface,template"}, first()),
            p(sess_key, errors, "alg", "alg", {"TYPE": "data"}, first()),
            p(sess_key, errors, "igmp", "igmp_proxy",
              {"TYPE": "data,lan_interface,wan_interface"}, first()),
            p(sess_key, errors, "iptv", "iptv", {"TYPE": "data,ether_info"}, first()),
            p(sess_key, errors, "audit", "audit", {"TYPE": "data"}, first()),
            p(sess_key, errors, "cflow", "cflow", {}),
            # 无影云桌面：实例配置行列表（api_token 在 _scrub 中脱敏）
            p(sess_key, errors, "wuying", "wuying",
              {"TYPE": "data,interface", "limit": LIST_FETCH_LIMIT},
              lambda b: _scrub(b.get("data", []))),
            # 缓存加速：整块（data.service 各缓存开关 + status 运行状态/报错）
            p(sess_key, errors, "cache_status", "ik_web_cache",
              {"TYPE": "data,status", "object": "service", "service": "data,video"},
              lambda b: _scrub(b)),
            # ---- 网络 ----
            self._ext_wan_bands(sess_key, errors),
            self._ext_iface(sess_key, errors),
            self._ext_arp(sess_key, errors),
            self._ext_dhcp(sess_key, errors),
            p(sess_key, errors, "route_v4_total", "static_rt_table",
              {"TYPE": "data,total", "limit": LIST_FETCH_LIMIT},
              lambda b: {"total": b.get("total", 0),
                         "rows": _scrub(b.get("data", []))}),
            p(sess_key, errors, "route_v6_total", "static_rt_table",
              {"TYPE": "data6,total6", "limit": LIST_FETCH_LIMIT},
              lambda b: {"total": b.get("total6", b.get("total", 0)),
                         "rows": _scrub(b.get("data6", b.get("data", [])))}),
            p(sess_key, errors, "ipv6_neighbor_total", "ipv6_neighbor",
              {"TYPE": "data,total", "ORDER_BY": "id", "ORDER": "desc",
               "limit": LIST_FETCH_LIMIT},
              lambda b: {"total": b.get("total", 0),
                         "rows": _scrub(b.get("data", []))}),
            p(sess_key, errors, "online_v6_total", "monitor_lanipv6",
              {"TYPE": "data,total", "limit": COUNT_LIMIT}, lambda b: b.get("total")),
            # ---- 业务/设备 ----
            p(sess_key, errors, "docker", "docker_container",
              {"TYPE": "data,network,image,cpuused,memavailable,sysbit"},
              lambda b: _scrub(b.get("data", []))),
            p(sess_key, errors, "qemu", "qemu", {"TYPE": "total,data", "limit": LIST_FETCH_LIMIT},
              lambda b: _scrub(b.get("data", []))),
            p(sess_key, errors, "ac_list", "ac_server",
              {"TYPE": "total,data", "limit": LIST_FETCH_LIMIT},
              lambda b: _scrub(b.get("data", []))),
            p(sess_key, errors, "cameras", "camera",
              {"TYPE": "total,data", "limit": LIST_FETCH_LIMIT},
              lambda b: _scrub(b.get("data", []))),
            p(sess_key, errors, "wakeups", "wakeup",
              {"TYPE": "total,data", "limit": LIST_FETCH_LIMIT},
              lambda b: _scrub(b.get("data", []))),
            p(sess_key, errors, "ppp_online", "ppp_online",
              {"TYPE": "data,total", "limit": LIST_FETCH_LIMIT},
              lambda b: _scrub(b.get("data", []))),
            # 认证账号配置详情（pppuser）：与在线会话合并成「认证账号」实体节点
            p(sess_key, errors, "auth_users", "pppuser",
              {"TYPE": "total,data", "limit": LIST_FETCH_LIMIT},
              lambda b: _scrub(b.get("data", []))),
            # 本地认证 / VPN 服务端（PPPoE・PPTP・L2TP・OpenVPN）的启停与参数
            self._ext_auth_servers(sess_key, errors),
            self._ext_messages(sess_key, errors),
            self._ext_alarms(sess_key, errors),
            self._ext_app_flow(sess_key, errors),
            self._ext_counts(sess_key, errors),
            # 在线终端的「设备详情」：按 IP 逐个抓应用流量 + 连接明细（限量 + 轮换）
            self._ext_terminal_detail(sess_key, errors, ext),
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        if 401 in errors:
            return 401

        # 硬件信息整理
        hw = self._ext_cache.get("hardware") or {}
        if isinstance(hw, dict):
            freq_list = (self._ext_cache.get("irq") or {}).get("cpulist") or []
            hw["cpucore_list"] = freq_list
            self._ext_cache["hardware"] = hw

        # 多拨线路汇总（属性用；缓存层不截断，属性渲染层负责截断展示）
        lines = self._ext_cache.get("wan_lines") or []
        fail_lines = [x.get("vlan_name") or x.get("interface")
                      for x in lines
                      if not (x.get("pppoe_ip_addr") and x.get("pppoe_updatetime", 0) > 0)]
        self._ext_cache["wan_lines_summary"] = {
            "total": len(lines),
            "online": len(lines) - len(fail_lines),
            "fail": len(fail_lines),
            "fail_lines": fail_lines,
            "lines": [
                {
                    "interface": x.get("vlan_name") or x.get("interface"),
                    "vlan_id": x.get("vlan_id"),
                    "ip_addr": x.get("pppoe_ip_addr"),
                    "gateway": x.get("pppoe_gateway"),
                    "default_route": x.get("default_route"),
                    "enabled": x.get("enabled"),
                    "uptime": self.seconds_to_dhms(int(time.time() - x.get("pppoe_updatetime", 0)))
                    if x.get("pppoe_updatetime") else "",
                    "username": x.get("username"),
                }
                for x in lines
            ],
        }

        # 认证账号：在线会话（ppp_online）与账号配置（pppuser）合并成「账号节点」
        await self._ext_auth_accounts(sess_key, errors)
        if 401 in errors:
            return 401

        self._ext_cache["unsupported"] = sorted(self._unsupported)
        self.last_ext = self._ext_cache
        ext.update(self._ext_cache)

    async def _ext_auth_accounts(self, sess_key, errors):
        """把「在线会话」与「账号配置」按用户名合并，供认证账号实体使用。

        * 在线会话 ``ppp_online``：谁在线、IP/MAC、会话标识、上下行字节数、上线时间；
        * 账号配置 ``pppuser``：是否启用、到期时间、限速/共享数、绑定线路、备注…

        两个表都可能缺对方，因此以账号配置表列出**全部账号**（离线账号也要有节点），
        在线表里多出来的用户名（例如账号已删但会话还在）也照样建节点。

        ``passwd`` 在抓取时已被 ``_scrub`` 打成 ``***``，这里再显式丢掉一次，
        确保明文口令不会进状态机 / recorder。
        """
        online_rows = self._ext_cache.get("ppp_online") or []
        detail_rows = self._ext_cache.get("auth_users") or []

        details: dict = {}
        for row in detail_rows:
            user = str(row.get("username") or "").strip()
            if user:
                details[user] = row

        online: dict = {}
        for row in online_rows:
            user = str(row.get("username") or "").strip()
            if user:
                online[user] = row

        # 兜底：整表没带回某个在线账号的配置时，按 FILTER1 单独查一次（限量）
        missing = [user for user in online if user not in details]
        for user in missing[:ACCOUNT_DETAIL_FALLBACK_MAX]:
            block = await self._show(sess_key, "pppuser", {"FILTER1": f"username,==,{user}"})
            if block == 401:
                errors.append(401)
                break
            rows = (block or {}).get("data") or []
            if rows:
                details[user] = _scrub(rows[0])

        accounts = []
        for user in list(details) + [u for u in online if u not in details]:
            detail_row = details.get(user) or {}
            online_row = online.get(user) or {}
            accounts.append({
                "key": user,
                "username": user,
                "id": detail_row.get("id") or online_row.get("id"),
                "uid": online_row.get("uid"),
                "online": bool(online_row),
                "ppptype": online_row.get("ppptype") or detail_row.get("ppptype"),
                "detail": {k: v for k, v in detail_row.items() if k != "passwd"},
                "session": {k: v for k, v in online_row.items() if k != "passwd"},
            })

        self._ext_cache["auth_accounts"] = accounts
        self._ext_cache["auth_accounts_total"] = len(accounts)
        self._ext_cache["auth_online_count"] = len(online)

    async def async_set_service_switch(self, sess_key, key, turn_on):
        """组装并发送服务类开关指令。返回响应。"""
        ext = self.last_ext or {}
        v = 1 if turn_on else 0

        def row_of(name):
            row = dict(ext.get(name) or {})
            return row

        if key == "ikuai_sw_snmp":
            row = row_of("netsnmp"); row["enabled"] = v
            body = {"func_name": "netsnmp", "action": "save", "param": row}
        elif key == "ikuai_sw_samba":
            row = row_of("smbd")
            body = {"func_name": "smbd", "action": "save", "param": {
                "enabled": v, "workgroup": row.get("workgroup", "WORKGROUP"),
                "wsdd2": row.get("wsdd2", 1), "access": row.get("access", 1)}}
        elif key == "ikuai_sw_ftp":
            ftp = ext.get("ftp") or {}
            body = {"func_name": "ftp_server", "action": "save", "param": {
                "ftp_port": ftp.get("ftp_port", 21), "open_ftp": v,
                "ftp_access": ftp.get("ftp_access", 1)}}
        elif key == "ikuai_sw_upnp":
            row = row_of("upnpd"); row["enabled"] = str(v)
            body = {"func_name": "upnpd", "action": "save", "param": row}
        elif key == "ikuai_sw_webauth":
            body = {"func_name": "webauth", "action": "switch", "param": {"enabled": v}}
        elif key == "ikuai_sw_igmp":
            row = row_of("igmp"); row["enabled"] = v
            body = {"func_name": "igmp_proxy", "action": "save", "param": row}
        elif key == "ikuai_sw_iptv":
            row = row_of("iptv"); row["enabled"] = v
            body = {"func_name": "iptv", "action": "save", "param": row}
        elif key == "ikuai_sw_watchdog":
            row = row_of("watchdog")
            body = {"func_name": "watchdog", "action": "save", "param": {
                "watchdog": v, "watchdog_thresh": row.get("watchdog_thresh", 3)}}
        elif key in ("ikuai_sw_nat", "ikuai_sw_dpi", "ikuai_sw_ntp"):
            row = row_of("basic")
            field = {"ikuai_sw_nat": "switch_nat",
                     "ikuai_sw_dpi": "switch_dpi",
                     "ikuai_sw_ntp": "switch_ntp"}[key]
            row[field] = v
            body = {"func_name": "basic", "action": "save", "param": row}
        elif key in ("ikuai_sw_alg_ftp", "ikuai_sw_alg_tftp",
                     "ikuai_sw_alg_sip", "ikuai_sw_alg_h323"):
            row = row_of("alg")
            field = {"ikuai_sw_alg_ftp": "support_ftp",
                     "ikuai_sw_alg_tftp": "support_tftp",
                     "ikuai_sw_alg_sip": "support_sip",
                     "ikuai_sw_alg_h323": "support_h323"}[key]
            row[field] = v
            body = {"func_name": "alg", "action": "save", "param": row}
        elif key in ("ikuai_sw_wanweb", "ikuai_sw_sshd", "ikuai_sw_telnetd"):
            # 高风险：ext 里的 remote 行为了展示已把 sshd_passwd 脱敏成 ***，
            # 不能直接回传（会把 SSH 密码真的改成 ***）。写前重新拉一次原始行。
            fresh = await self._show(sess_key, "remote_control", {})
            rows = fresh.get("data") if isinstance(fresh, dict) else None
            row = dict(rows[0]) if isinstance(rows, list) and rows else row_of("remote")
            row.pop("sshd_passwd", None)  # 改访问开关不需要也不应该改密码
            field = {"ikuai_sw_wanweb": "open_wanweb",
                     "ikuai_sw_sshd": "open_sshd",
                     "ikuai_sw_telnetd": "open_telnetd"}[key]
            row[field] = v
            body = {"func_name": "remote_control", "action": "save", "param": row}
        elif key == "ikuai_sw_auto_upgrade":
            block = ext.get("upgrade") or {}
            row = dict(block.get("data") or {})
            row["auto_upgrade_sec"] = v
            body = {"func_name": "upgrade", "action": "save", "param": row}
        else:
            raise ValueError(f"unknown service switch {key}")

        return await self.async_execute_action(sess_key, body)

    async def async_execute_action(self, sess_key, action_body):
        """执行任意 /Action/call 指令。"""
        return await self.requestpost_json(
            self._host + ACTION_URL, self._header(sess_key), action_body)

    @staticmethod
    def _dial_brief(kind, line):
        """拨号/VPN 线路精简为属性行（在线/失败明细共用）。"""
        if kind == "pppoe":
            online = (bool(line.get("pppoe_ip_addr"))
                      and int(line.get("pppoe_updatetime") or 0) > 0) \
                if line.get("vlan_internet", 2) == 2 \
                else line.get("enabled") == "yes"
            ts = int(line.get("pppoe_updatetime") or 0)
            row = {
                "id": line.get("id"),
                "name": line.get("vlan_name") or line.get("interface"),
                "enabled": line.get("enabled"),
                "online": online,
                "interface": line.get("interface"),
                "vlan_id": line.get("vlan_id"),
                "ip_addr": line.get("pppoe_ip_addr"),
                "gateway": line.get("pppoe_gateway"),
                "default_route": line.get("default_route"),
                "username": line.get("username"),
            }
        else:
            online = bool(line.get("ip_addr")) and bool(line.get("updatetime"))
            ts = int(line.get("updatetime") or 0)
            row = {
                "id": line.get("id"),
                "name": line.get("name") or line.get("server")
                        or line.get("server_addr") or line.get("interface"),
                "enabled": line.get("enabled"),
                "online": online,
                "server": line.get("server") or line.get("server_addr"),
                "ip_addr": line.get("ip_addr"),
                "username": line.get("username"),
            }
        row["uptime"] = max(0, int(time.time()) - ts) if ts > 0 else 0
        return row

    def _fill_dial_attrs(self, data_dict):
        """为 PPPoE/PPTP/L2TP 的「在线数/失败数/总数」扁平传感器填充明细属性。

        扁平传感器 IKUAISensor 的属性取自 ``data[<kind>_attrs]``。
        """
        groups = [
            ("ikuai_pppoe", "wan_lines", "pppoe"),
            ("ikuai_pptp", "pptp_lines", "pptp"),
            ("ikuai_l2tp", "l2tp_lines", "pptp"),
        ]
        ext = data_dict.get("ext") or {}
        for prefix, ext_key, kind in groups:
            rows = ext.get(ext_key) or []
            briefs = [self._dial_brief(kind, x) for x in rows
                      if isinstance(x, dict)]
            online_rows = [x for x in briefs if x["online"]]
            fail_rows = [x for x in briefs if not x["online"]]
            base = {"total": len(briefs), "online": len(online_rows),
                    "fail": len(fail_rows)}
            data_dict[f"{prefix}_count_attrs"] = {
                **base, "entries": online_rows}
            data_dict[f"{prefix}_fail_count_attrs"] = {
                **base, "entries": fail_rows}
            data_dict[f"{prefix}_total_attrs"] = {
                **base, "entries": briefs}

    async def get_data(self, sess_key):
        """Orchestrate data fetching for all components."""
        new_data = {"switch": [], "tracker": [], "ikuai_wan_ip": "未检测到",
                    "wan_online": False, "ext": {}}

        # 并发抓取主要状态
        status_res = await self._get_ikuai_status(sess_key, new_data, new_data["ext"])
        if status_res == 401: return 401

        tasks = [
            self._get_ikuai_waninfo(sess_key, new_data, new_data["ext"]),
            self._get_ikuai_wan6info(sess_key, new_data),
            self._get_ikuai_lan6info(sess_key, new_data),
            self._get_ikuai_mac_control(sess_key, new_data),
            self._get_ikuai_pppoe_info(sess_key, new_data, new_data["ext"]),
            self._get_ikuai_pptp_info(sess_key, new_data, new_data["ext"]),
            self._get_ikuai_l2tp_info(sess_key, new_data, new_data["ext"]),
            self._get_all_lan_hosts(sess_key),
            self._get_lan_hosts_v6(sess_key),
        ]

        # 抓取内置开关
        for switch in SWITCH_TYPES:
            tasks.append(self._get_ikuai_switch(
                sess_key, SWITCH_TYPES[switch]["name"],
                SWITCH_TYPES[switch]["show_body"], SWITCH_TYPES[switch]["show_on"],
                SWITCH_TYPES[switch]["show_off"], new_data))

        # 抓取自定义开关
        for _, switch_config in self._custom_switches_config.items():
            tasks.append(self._get_ikuai_switch(
                sess_key, switch_config["name"],
                switch_config.get("show_body", {}), switch_config.get("show_on", {}),
                switch_config.get("show_off", {}), new_data))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        all_lan_devices = None
        v6_hosts = None
        for r in results:
            if isinstance(r, dict) and "ip" in r and "mac" in r:
                all_lan_devices = r
            elif isinstance(r, list):
                v6_hosts = r

        # IPv4 在线终端明细（monitor_lanip 本来就抓了，按 MAC 去重后入 ext；
        # 供「在线终端列表」诊断实体使用，不新增接口请求）
        if all_lan_devices:
            new_data["ext"]["lan_hosts"] = _scrub(
                list(all_lan_devices.get("mac", {}).values()))

        # IPv6 在线终端明细（状态监控 → IPv6终端监控，每终端全字段）
        if v6_hosts is not None:
            new_data["ext"]["lan_hosts_v6"] = v6_hosts

        # 扩展模块（首次主轮询跳过，保证 HA 启动/重载快速完成；
        # 之后每 EXT_FETCH_EVERY 个主轮询完整抓一次，其余沿用缓存）
        self._ext_tick += 1
        if self._ext_tick >= EXT_FETCH_EVERY:
            self._ext_tick = 0
            ext_res = await self._fetch_extended(sess_key, new_data["ext"])
            if ext_res == 401:
                return 401
        elif self._ext_cache:
            new_data["ext"].update(self._ext_cache)

        # 多拨在线同样意味着 WAN 可用
        if (new_data.get("ikuai_pppoe_count") or 0) > 0:
            new_data["wan_online"] = True
        new_data["ext"]["unsupported"] = sorted(self._unsupported)

        # 拨号/VPN 线路明细挂到「在线数/失败数」扁平传感器属性
        self._fill_dial_attrs(new_data)

        # 处理 Tracker 逻辑
        if self._tracker_config:
            for target_id, config in self._tracker_config.items():
                buffer_times = config.get("buffer", 2)
                found_item = None

                if all_lan_devices:
                    if config.get("type", "mac" if ":" in target_id else "ip") == "ip":
                        found_item = all_lan_devices["ip"].get(target_id)
                    else:
                        found_item = all_lan_devices["mac"].get(target_id.lower())

                if found_item:
                    self._datatracker[target_id] = found_item
                    self._datarefreshtimes[target_id] = 0
                    new_data["tracker"].append(found_item)
                elif self._datatracker.get(target_id):
                    curr = self._datarefreshtimes.get(target_id, 0)
                    if curr < buffer_times:
                        new_data["tracker"].append(self._datatracker[target_id])
                        self._datarefreshtimes[target_id] = curr + 1

        # 分类日志缓存（LogHub 更新），保证实体每次都能拿到
        new_data["logs"] = self.log_cache

        return new_data
