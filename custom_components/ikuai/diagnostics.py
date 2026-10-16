"""诊断信息导出（密码脱敏）。"""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import COORDINATOR, LOGHUB, DOMAIN

REDACT_KEYS = {"passwd", "password", "pass", "secret", "sshd_passwd", "ca",
               "key", "ipsec_secret", "tls_auth", "cert"}


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("**REDACTED**" if k in REDACT_KEYS else _redact(v))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    bundle = hass.data[DOMAIN][entry.entry_id]
    coordinator = bundle[COORDINATOR]
    hub = bundle.get(LOGHUB)
    data = coordinator.data or {}

    return {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": _redact(dict(entry.data)),
            "options": _redact(dict(entry.options)),
        },
        "last_update_success": coordinator.last_update_success,
        "host": coordinator.host,
        "flat_data": _redact({
            k: v for k, v in data.items()
            if k not in ("ext", "logs", "tracker", "switch")
        }),
        "extended": _redact(data.get("ext")),
        "logs_summary": {
            key: {"name": block.get("name"), "total": block.get("total"),
                  "updated": block.get("updated")}
            for key, block in (data.get("logs") or {}).items()
        },
        "log_hub": {
            "interval": getattr(hub, "interval", None) if hub else None,
            "primed": sorted(getattr(hub, "_primed", set())) if hub else [],
        },
        "tracker_count": len(data.get("tracker") or []),
        "switch_states": data.get("switch"),
    }
