"""Binary sensors: is the machine reporting, and how is it powered."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import Machine, PeekCoordinator
from .entity import PeekMachineEntity, add_entities_for_new_machines


@dataclass(frozen=True, kw_only=True)
class PeekBinaryDescription(BinarySensorEntityDescription):
    value: Callable[[Machine], bool | None]


BINARY_SENSORS: tuple[PeekBinaryDescription, ...] = (
    PeekBinaryDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value=lambda m: m.battery_charging,
    ),
    PeekBinaryDescription(
        key="ac_power",
        translation_key="ac_power",
        device_class=BinarySensorDeviceClass.PLUG,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda m: m.battery_ac,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PeekCoordinator = hass.data[DOMAIN][entry.entry_id]

    def build(host: str) -> Iterable[Entity]:
        # Always created, for every machine: this is the entity people build
        # notifications on, and one that only exists while the machine is
        # healthy is useless for the case it exists to cover.
        yield PeekOnlineSensor(coordinator, host)

        machine = coordinator.data.machines.get(host)
        for description in BINARY_SENSORS:
            if machine is not None and machine.battery_percent is None:
                continue        # a desktop has no charge state to report
            yield PeekBinarySensor(coordinator, host, description)

    add_entities_for_new_machines(entry, coordinator, async_add_entities, build)


class PeekOnlineSensor(PeekMachineEntity, BinarySensorEntity):
    """Whether this machine is still pushing readings."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: PeekCoordinator, host: str) -> None:
        super().__init__(
            coordinator,
            host,
            BinarySensorEntityDescription(
                key="online",
                translation_key="online",
                device_class=BinarySensorDeviceClass.CONNECTIVITY,
            ),
        )

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.is_online(self.host)

    @property
    def available(self) -> bool:
        # Deliberately NOT the inherited staleness check. This sensor's whole
        # job is to report a stale machine as off, and an entity that goes
        # unavailable at the same moment cannot: an automation waiting for
        # "turns off" would never fire, because unavailable is not off.
        #
        # It follows the coordinator instead, so it reads unknown only when
        # Home Assistant cannot reach the relay - which is honest, because
        # then nothing is known about the machine either way.
        return self.coordinator.last_update_success

    @property
    def extra_state_attributes(self) -> dict[str, int | str] | None:
        machine = self.machine
        if machine is None:
            return None
        return {
            "seconds_since_reading": machine.age_s,
            "offline_after_seconds": self.coordinator.data.offline_after_s,
        }


class PeekBinarySensor(PeekMachineEntity, BinarySensorEntity):
    """A power state read off the machine's own battery."""

    entity_description: PeekBinaryDescription

    @property
    def is_on(self) -> bool | None:
        machine = self.machine
        if machine is None:
            return None
        return self.entity_description.value(machine)
