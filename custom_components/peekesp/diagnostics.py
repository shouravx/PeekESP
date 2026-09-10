"""Diagnostics, with the credential taken out."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_PAIR_CODE, DOMAIN
from .coordinator import PeekCoordinator

# The pairing code is the credential: the stream id and both tokens derive from
# it, so anything holding it can push telemetry to someone's display. A
# diagnostics download is the file people paste into a public issue, which is
# the last place it should appear.
#
# The stream id is kept. It is one-way from the code, it is what identifies the
# entry in a bug report, and it reveals nothing that would let anyone read or
# write the stream without the tokens.
REDACT = {CONF_PAIR_CODE}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    coordinator: PeekCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), REDACT),
            "options": dict(entry.options),
        },
        "relay": {
            "url": coordinator.relay,
            "stream": coordinator.stream,
            "scan_interval_s": coordinator.scan_interval,
            "offline_after_s": data.offline_after_s,
            "last_update_success": coordinator.last_update_success,
            "latest_firmware": data.latest_fw,
        },
        "machines": {
            host: {
                **asdict(machine),
                "online": data.is_online(host),
            }
            for host, machine in data.machines.items()
        },
    }
