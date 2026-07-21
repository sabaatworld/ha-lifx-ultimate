"""Diagnostics support for LIFX."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_HOST, CONF_IP_ADDRESS, CONF_MAC
from homeassistant.core import HomeAssistant

from .const import CONF_ENTRY_TYPE, CONF_LABEL, CONF_MEMBERS, ENTRY_TYPE_PARALLEL_GROUP
from .coordinator import LIFXConfigEntry

TO_REDACT = [CONF_LABEL, CONF_HOST, CONF_IP_ADDRESS, CONF_MAC, CONF_MEMBERS]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LIFXConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a LIFX config entry."""
    if entry.data.get(CONF_ENTRY_TYPE) == ENTRY_TYPE_PARALLEL_GROUP:
        runtime = entry.runtime_data
        return {
            "entry": {
                "title": entry.title,
                "data": async_redact_data(dict(entry.data), TO_REDACT),
            },
            "parallel_group": {
                "member_count": len(entry.data[CONF_MEMBERS]),
                "worker_health": list(runtime.parallel.worker_health),
                "available": runtime.available,
            },
        }

    coordinator = entry.runtime_data
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
        },
        "data": async_redact_data(await coordinator.diagnostics(), TO_REDACT),
    }
