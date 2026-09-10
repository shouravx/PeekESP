"""Shared entity plumbing: one Home Assistant device per monitored machine."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityDescription
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL_MACHINE
from .coordinator import Machine, PeekCoordinator


class PeekMachineEntity(CoordinatorEntity[PeekCoordinator]):
    """An entity belonging to one monitored machine."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PeekCoordinator,
        host: str,
        description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.host = host
        self.entity_description = description

        # Scoped to the stream as well as the host, so two pairing codes that
        # both watch a machine called "raspberrypi" do not collide into one
        # device with two sets of entities fighting over it.
        self._attr_unique_id = f"{coordinator.stream}_{host}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.stream}_{host}")},
            name=host,
            manufacturer=MANUFACTURER,
            model=MODEL_MACHINE,
        )

    @property
    def machine(self) -> Machine | None:
        return self.coordinator.data.machines.get(self.host)

    @property
    def available(self) -> bool:
        """Unavailable when the reading is stale, not merely when it is old.

        A machine that stopped pushing an hour ago still has a reading in the
        relay, and reporting its last CPU figure as current is the one failure
        a monitor must not have - a dead host would look like a healthy one
        whose numbers happen not to be moving.
        """
        return (
            self.coordinator.last_update_success
            and self.coordinator.data.is_online(self.host)
        )


@callback
def add_entities_for_new_machines(
    entry: ConfigEntry,
    coordinator: PeekCoordinator,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[str], Iterable[Entity]],
) -> None:
    """Create entities for each machine, including ones that appear later.

    Machines arrive whenever someone installs an agent, which is usually after
    Home Assistant has already set the integration up. Without this, a second
    or third machine under the same pairing code would show up on the display
    and never in Home Assistant until a restart.
    """
    seen: set[str] = set()

    @callback
    def _sync() -> None:
        fresh = [h for h in coordinator.data.machines if h not in seen]
        if not fresh:
            return
        seen.update(fresh)
        entities: list[Entity] = []
        for host in fresh:
            entities.extend(build(host))
        if entities:
            async_add_entities(entities)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
