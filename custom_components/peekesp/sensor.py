"""Sensors: one set per monitored machine."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfInformation,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import Machine, PeekCoordinator
from .entity import PeekMachineEntity, add_entities_for_new_machines


@dataclass(frozen=True, kw_only=True)
class PeekSensorDescription(SensorEntityDescription):
    """A sensor plus how to pull it out of a reading."""

    value: Callable[[Machine], float | int | datetime | None]


def _uptime_since(machine: Machine) -> datetime | None:
    """A timestamp, not a running count.

    A seconds-since-boot sensor changes every single poll, which writes a row
    to the recorder database every 30 seconds forever and produces a sawtooth
    graph nobody wants. The boot time is the same fact and is constant while
    the machine stays up, so it compresses to one state until it reboots -
    which is the event actually worth seeing.
    """
    if machine.uptime_seconds is None:
        return None
    # Rounded to the minute so that jitter in the agent's own clock does not
    # rewrite the timestamp on every poll and undo the point of the above.
    boot = dt_util.utcnow() - timedelta(seconds=machine.uptime_seconds)
    return boot.replace(second=0, microsecond=0)


def _last_seen(machine: Machine) -> datetime:
    return dt_util.utcnow() - timedelta(seconds=machine.age_s)


SENSORS: tuple[PeekSensorDescription, ...] = (
    PeekSensorDescription(
        key="cpu",
        translation_key="cpu",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:cpu-64-bit",
        value=lambda m: m.cpu_percent,
    ),
    PeekSensorDescription(
        key="ram",
        translation_key="ram",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:memory",
        value=lambda m: m.ram_percent,
    ),
    PeekSensorDescription(
        key="storage",
        translation_key="storage",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:harddisk",
        value=lambda m: m.storage_percent,
    ),
    PeekSensorDescription(
        key="storage_free",
        translation_key="storage_free",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:harddisk",
        value=lambda m: m.storage_free_gb,
    ),
    PeekSensorDescription(
        key="storage_total",
        translation_key="storage_total",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.GIBIBYTES,
        suggested_display_precision=1,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:harddisk",
        value=lambda m: m.storage_total_gb,
    ),
    PeekSensorDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value=lambda m: m.cpu_temp_c,
    ),
    PeekSensorDescription(
        key="network_rx",
        translation_key="network_rx",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement="kbit/s",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:download-network",
        value=lambda m: m.rx_kbps,
    ),
    PeekSensorDescription(
        key="network_tx",
        translation_key="network_tx",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement="kbit/s",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        icon="mdi:upload-network",
        value=lambda m: m.tx_kbps,
    ),
    PeekSensorDescription(
        key="battery",
        translation_key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda m: m.battery_percent,
    ),
    PeekSensorDescription(
        key="battery_runtime",
        translation_key="battery_runtime",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.MINUTES,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:battery-clock",
        value=lambda m: m.battery_minutes,
    ),
    PeekSensorDescription(
        key="boot_time",
        translation_key="boot_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-start",
        value=_uptime_since,
    ),
    PeekSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:clock-check",
        value=_last_seen,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PeekCoordinator = hass.data[DOMAIN][entry.entry_id]

    def build(host: str) -> Iterable[Entity]:
        machine = coordinator.data.machines.get(host)
        for description in SENSORS:
            # A desktop has no battery, and the agents say so with -1 rather
            # than by omitting the field. Creating the entity anyway would give
            # every machine a permanently unknown battery sensor, so the two
            # battery sensors are only created for a machine that reports one.
            if description.key.startswith("battery") and machine is not None:
                if not machine.has_battery:
                    continue
            yield PeekSensor(coordinator, host, description)

    add_entities_for_new_machines(entry, coordinator, async_add_entities, build)


class PeekSensor(PeekMachineEntity, SensorEntity):
    """One reading from one machine."""

    entity_description: PeekSensorDescription

    @property
    def native_value(self) -> float | int | datetime | None:
        machine = self.machine
        if machine is None:
            return None
        return self.entity_description.value(machine)

    @property
    def available(self) -> bool:
        # "Last seen" is the one sensor that must survive the machine going
        # offline: it is how you find out when it went. Reporting it as
        # unavailable would erase the answer at the moment it becomes the
        # question.
        if self.entity_description.key == "last_seen":
            return self.coordinator.last_update_success and self.machine is not None
        return super().available
