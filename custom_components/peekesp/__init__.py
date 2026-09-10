"""The PeekESP integration.

Reads the same Cloudflare Worker relay the ESP32 display reads, using the same
pairing code. Nothing here talks to a monitored machine directly, and nothing
opens a port: the agents push out, this polls out, and neither end is
reachable from the internet.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_PAIR_CODE,
    CONF_RELAY,
    CONF_SCAN_INTERVAL,
    DEFAULT_RELAY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import PeekCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PeekESP from a config entry."""
    coordinator = PeekCoordinator(
        hass,
        entry,
        relay=entry.data.get(CONF_RELAY, DEFAULT_RELAY),
        pair_code=entry.data[CONF_PAIR_CODE],
        scan_interval=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )

    # Fails the setup if the very first poll fails, so a wrong relay URL shows
    # as "retrying" with the reason attached rather than as an integration that
    # loaded fine and has no entities.
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Changing the poll interval rebuilds the coordinator rather than mutating
    # a live one, which is the only way the new interval actually takes effect.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
