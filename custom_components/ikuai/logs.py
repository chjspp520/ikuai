"""
iKuai 日志中心
- 独立节流周期抓取 9 类 syslog 与 3 类行为审计记录
- 环形去重，增量投递到：
  1. HA 事件总线（ikuai_log，带 category）
  2. HA 日志簿（logbook）
  3. 分类诊断传感器（state=总条数，attrs=最近 N 条）
"""
from __future__ import annotations

import time
import asyncio
import datetime
import logging

from .const import LOG_CATEGORIES, LOG_ROWS_KEEP, EVENT_LOG, DOMAIN
from .entity import decode_url_text

_LOGGER = logging.getLogger(__name__)

# 「采集日志」开关的运行时状态（按配置条目存内存；实体自己用 Store 持久化）
_LOG_POLL_KEY = f"{DOMAIN}_log_poll"


def log_poll_store(hass) -> dict:
    """取（并按需初始化）各条目的日志采集开关状态。"""
    return hass.data.setdefault(DOMAIN, {}).setdefault(_LOG_POLL_KEY, {})


def set_log_poll_enabled(hass, entry_id, enabled: bool) -> None:
    log_poll_store(hass)[entry_id] = bool(enabled)


def log_poll_enabled(hass, entry_id=None, default: bool = True) -> bool:
    """该条目的日志采集是否开启（没有记录时按默认值：开启）。"""
    try:
        return bool(log_poll_store(hass).get(entry_id, default))
    except Exception:  # pylint: disable=broad-except
        return default


def _fmt_ts(value):
    """把整型时间戳格式化为本地时间字符串。"""
    try:
        iv = int(value)
        if iv > 1_000_000_000:
            return datetime.datetime.fromtimestamp(iv).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        pass
    return value


def render_row(category: dict, row: dict) -> str:
    """按分类字段映射把一行日志渲染成中文文本。"""
    if not isinstance(row, dict):
        return str(row)
    parts = []
    for field, label in category.get("fields", {}).items():
        val = row.get(field)
        if val in (None, "", "--"):
            continue
        if field in ("timestamp", "date_time"):
            val = _fmt_ts(val)
        else:
            # 爱快对备注/用户名等文本做了 URL 编码（Apple%20TV → Apple TV）
            val = decode_url_text(val)
        parts.append(f"{label}:{val}")
    if not parts:
        # 没有配置字段映射时退化为键值拼接
        parts = [f"{k}:{decode_url_text(v)}" for k, v in row.items()
                 if k not in ("id",) and v not in (None, "", "--")]
    text = " ".join(parts)
    return text[:240]


class IKuaiLogHub:
    """周期性抓取分类日志并增量分发。"""

    def __init__(self, hass, fetcher, interval_seconds: int = 60, entry_id=None):
        self._hass = hass
        self._fetcher = fetcher
        self._entry_id = entry_id
        self._interval = max(20, int(interval_seconds))
        self._last_update = 0.0
        self._lock = asyncio.Lock()
        # category key -> {"total":n, "rows":[...], "updated": str}
        self.data: dict = {}
        # category key -> 已见过的最大 id（首次只填充不广播）
        self._max_id: dict = {}
        self._primed: set = set()
        self._logbook_available = True

    @property
    def interval(self):
        return self._interval

    def snapshot(self):
        return self.data

    async def async_maybe_update(self, sess_key) -> dict:
        """节流更新；返回最新日志数据。

        「采集日志」开关关闭时直接返回已有快照 —— **一个日志请求都不发**
        （12 类日志本来每分钟一次，关掉能省下这些请求）。
        """
        if not log_poll_enabled(self._hass, self._entry_id):
            return self.data
        now = time.time()
        if now - self._last_update < self._interval and self.data:
            return self.data
        async with self._lock:
            now = time.time()
            if now - self._last_update < self._interval and self.data:
                return self.data
            await self._async_update(sess_key)
            self._last_update = time.time()
            return self.data

    async def async_force_update(self, sess_key) -> dict:
        """强制刷新（服务调用/清空日志后）。"""
        async with self._lock:
            await self._async_update(sess_key)
            self._last_update = time.time()
            return self.data

    async def _async_update(self, sess_key):
        tasks = [self._fetch_category(sess_key, cat) for cat in LOG_CATEGORIES]
        await asyncio.gather(*tasks, return_exceptions=True)
        self._fetcher.log_cache = self.data

    async def _fetch_category(self, sess_key, category: dict):
        key = category["key"]
        # 分类可以自带 param（个别模块不认 TYPE=total，见 const.py 的 url_log）
        param = category.get("param")
        if param is None:
            order_by = category.get("order_by", "id")
            limit = category.get("limit", f"0,{LOG_ROWS_KEEP}")
            param = {"TYPE": "total,data", "ORDER_BY": order_by,
                     "ORDER": "desc", "limit": limit}
        else:
            param = dict(param)

        # 声明了 page_size 的模块要翻页取全：limit 是"偏移,条数"，且单次最多 100 条
        # （一次要 10000 条会漏数据，见 const.py 的 url_log）
        page_size = int(category.get("page_size") or 0)
        max_pages = max(1, int(category.get("max_pages") or 1)) if page_size else 1
        rows: list = []
        total = None
        pages = 0
        while pages < max_pages:
            page_param = dict(param)
            if page_size:
                page_param["limit"] = f"{pages * page_size},{page_size}"
            block = await self._fetcher._show(sess_key, category["func"], page_param)
            pages += 1
            if block == 401 or not isinstance(block, dict):
                break
            page_rows = block.get("data")
            if page_rows is None:
                # 个别模块字段名不同（如 dhcp6 风格）
                page_rows = block.get("client_data") or []
            page_rows = page_rows if isinstance(page_rows, list) else []
            rows.extend(page_rows)
            if total is None:
                total = block.get("total")
            if not page_size or len(page_rows) < page_size:
                # 不分页，或这一页没取满 → 已经到底
                break
        if not rows and total is None:
            return
        # total 恒为 -1（如 audit_url_log）表示"该模块不提供总数"：退化为本次取到的行数
        total_supported = isinstance(total, int) and total >= 0
        if not total_supported:
            total = len(rows)

        # 计算新日志（按 id；无 id 时用 timestamp+内容指纹）
        def row_id(r):
            if "id" in r:
                try:
                    return int(r["id"])
                except (ValueError, TypeError):
                    pass
            return f"{r.get('timestamp', '')}-{str(r.get('content', r.get('event', '')))[:40]}"

        incoming = []
        for r in rows:
            rid = row_id(r)
            if isinstance(rid, int):
                incoming.append((rid, r))
        new_rows_numeric = sorted(
            [(rid, r) for rid, r in incoming if rid > self._max_id.get(key, -1)],
            key=lambda x: x[0])

        first_run = key not in self._primed
        if incoming:
            self._max_id[key] = max(rid for rid, _ in incoming)
        self._primed.add(key)

        updated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        keep = int(category.get("rows_keep") or LOG_ROWS_KEEP)
        self.data[key] = {
            "name": category["name"],
            "icon": category.get("icon"),
            "func": category["func"],
            "total": total,
            "total_supported": total_supported,
            "rows": rows[:keep],
            "pages": pages,
            "updated": updated,
        }

        # 首次拉取只建立基线，避免重启刷出大量历史日志
        if first_run or not new_rows_numeric:
            return

        rendered = [{
            "id": rid,
            "time": _fmt_ts(r.get("timestamp") or r.get("date_time")),
            "message": render_row(category, r),
            "raw": r,
        } for rid, r in new_rows_numeric]

        # 1) 事件总线
        self._hass.bus.async_fire(EVENT_LOG, {
            "category": key,
            "name": category["name"],
            "count": len(rendered),
            "entries": rendered,
        })

        # 2) 日志簿
        if self._logbook_available:
            try:
                from homeassistant.components.logbook import async_log_entry
                for item in rendered:
                    await async_log_entry(
                        self._hass,
                        name=f"iKuai {category['name']}",
                        message=item["message"],
                        domain=DOMAIN,
                    )
            except ImportError:
                self._logbook_available = False
                _LOGGER.debug("logbook 组件不可用，日志仅投递事件总线与传感器")
            except Exception as e:  # 日志失败不能影响主循环
                _LOGGER.debug("写入日志簿失败: %s", e)
