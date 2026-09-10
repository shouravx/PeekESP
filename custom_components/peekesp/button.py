"""Buttons that leave a command for the ESP32 display to collect."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    COMMAND_IDENTIFY,
    COMMAND_REBOOT,
    COMMAND_REFRESH,
    COMMAND_STANDBY,
    COMMAND_WAKE,
    DOMAIN,
    MANUFACTURER,
    MODEL_DISPLAY,
)
from .coordinator import PeekCoordinator


@dataclass(frozen=True, kw_only=True)
class PeekButtonDescription(ButtonEntityDescription):
    command: str


# The relay accepts a closed vocabulary and rejects anything else at the edge,
# so a button for a verb outside this set would be a button that silently does
# nothing. Kept in step with COMMANDS in cloudflare/src/index.js.
#
# Everything here is undone by pressing a button on the device itself, and none
# of it changes configuration - that is what the device's own settings page is
# for, behind its own password. So there is no confirmation step: the worst
# outcome of a misclick is a screen that comes back a few seconds later.
BUTTONS: tuple[PeekButtonDescription, ...] = (
    PeekButtonDescription(
        key="identify",
        translation_key="identify",
        command=COMMAND_IDENTIFY,
        icon="mdi:television-ambient-light",
    ),
    PeekButtonDescription(
        key="refresh",
        translation_key="refresh",
        command=COMMAND_REFRESH,
        icon="mdi:refresh",
    ),
    PeekButtonDescription(
        key="wake",
        translation_key="wake",
        command=COMMAND_WAKE,
        icon="mdi:monitor",
    ),
    PeekButtonDescription(
        key="standby",
        translation_key="standby",
        command=COMMAND_STANDBY,
        icon="mdi:monitor-off",
    ),
    PeekButtonDescription(
        key="reboot",
        translation_key="reboot",
        command=COMMAND_REBOOT,
        entity_category=EntityCategory.CONFIG,
        icon="mdi:restart",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PeekCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(PeekButton(coordinator, d) for d in BUTTONS)


class PeekButton(CoordinatorEntity[PeekCoordinator], ButtonEntity):
    """One command, left at the relay for the display's next poll."""

    _attr_has_entity_name = True
    entity_description: PeekButtonDescription

    def __init__(
        self, coordinator: PeekCoordinator, description: PeekButtonDescription
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.stream}_display_{description.key}"

        # Its own device, separate from the monitored machines. The display is
        # a different thing from the computers it shows, and hanging "reboot"
        # off a machine's device would read as rebooting that machine - which
        # is emphatically not what it does.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.stream}_display")},
            name="PeekESP display",
            manufacturer=MANUFACTURER,
            model=MODEL_DISPLAY,
        )

    @property
    def available(self) -> bool:
        # Follows the relay, not the display. There is no way to know whether a
        # display is powered on - it polls out and never announces itself - so
        # "available" here can only honestly mean "the relay will accept a
        # command", which is exactly what pressing this does.
        return self.coordinator.last_update_success

    async def async_press(self) -> None:
        await self.coordinator.async_send_command(self.entity_description.command)
