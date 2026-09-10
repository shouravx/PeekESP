"""The relay's reply, turned into something typed.

Deliberately free of any Home Assistant import. Parsing what an agent sent is
the part most likely to be wrong - the payload comes from four different agent
versions on three operating systems - and it is the part worth testing without
standing up a Home Assistant instance to do it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# The relay caps a pairing code at six machines.
MAX_DEVICES = 6


def as_float(raw: Any, default: float | None = None) -> float | None:
    """A number, or the default. The relay hands back whatever an agent sent."""
    if raw is None or isinstance(raw, bool):
        # bool is an int in Python, and True would otherwise become 1.0 - a
        # plausible-looking CPU percentage from a field that was never a number.
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    # NaN and infinity survive float() and then poison every average and graph
    # downstream. json.loads produces them from bare NaN/Infinity tokens.
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return value


def as_int(raw: Any, default: int | None = None) -> int | None:
    value = as_float(raw)
    return default if value is None else int(value)


def sensorless(value: float | None) -> float | None:
    """Turn the agents' negative sentinel into "no reading".

    A machine with no thermal zone reports -1 rather than omitting the field,
    so that the display can tell "no sensor" from "an agent older than this
    field". Home Assistant has a real way to say the same thing, so the
    sentinel becomes None and the entity reads unknown instead of -1 °C.
    """
    return None if value is None or value < 0 else value


@dataclass(slots=True)
class Machine:
    """One machine's most recent reading, as the relay handed it over."""

    host: str
    age_s: int = 0
    cpu_percent: float | None = None
    ram_percent: float | None = None
    storage_percent: float | None = None
    storage_total_gb: float | None = None
    storage_free_gb: float | None = None
    cpu_temp_c: float | None = None
    battery_percent: int | None = None
    battery_minutes: int | None = None
    battery_charging: bool = False
    battery_ac: bool = False
    rx_kbps: float | None = None
    tx_kbps: float | None = None
    uptime_seconds: int | None = None

    @property
    def has_battery(self) -> bool:
        return self.battery_percent is not None or self.battery_minutes is not None

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "Machine":
        return cls(
            # The sketch stores 20 bytes and the agent truncates to 19, but the
            # relay does not enforce it - the host name is a storage key there,
            # not a validated field.
            host=str(raw.get("host") or "unknown")[:32],
            age_s=as_int(raw.get("age_s"), 0) or 0,
            cpu_percent=sensorless(as_float(raw.get("cpu_percent"))),
            ram_percent=sensorless(as_float(raw.get("ram_percent"))),
            storage_percent=sensorless(as_float(raw.get("storage_percent"))),
            storage_total_gb=sensorless(as_float(raw.get("storage_total_gb"))),
            storage_free_gb=sensorless(as_float(raw.get("storage_free_gb"))),
            cpu_temp_c=sensorless(as_float(raw.get("cpu_temp_c"))),
            battery_percent=as_int(sensorless(as_float(raw.get("battery_percent")))),
            battery_minutes=as_int(sensorless(as_float(raw.get("battery_minutes")))),
            battery_charging=bool(raw.get("battery_charging")),
            battery_ac=bool(raw.get("battery_ac")),
            rx_kbps=sensorless(as_float(raw.get("net_rx_kbps"))),
            tx_kbps=sensorless(as_float(raw.get("net_tx_kbps"))),
            uptime_seconds=as_int(sensorless(as_float(raw.get("uptime_seconds")))),
        )


@dataclass(slots=True)
class RelayData:
    """Everything one poll produced."""

    machines: dict[str, Machine] = field(default_factory=dict)
    latest_fw: str | None = None
    offline_after_s: int = 60

    def is_online(self, host: str) -> bool:
        machine = self.machines.get(host)
        return machine is not None and machine.age_s < self.offline_after_s


def parse_payload(payload: Any, offline_after_s: int) -> RelayData:
    """Turn one relay reply into machines.

    Handles both shapes the relay can produce. Current deployments return a
    ``devices`` array; the freshest host is also repeated at the top level so
    that a device flashed before the array existed keeps working. Reading the
    array when it is there and falling back to the flat form means this works
    against an older Worker rather than reporting nothing at all.
    """
    if not isinstance(payload, dict):
        raise ValueError("the relay did not return an object")

    rows = payload.get("devices")
    if not isinstance(rows, list) or not rows:
        rows = [payload] if payload.get("host") else []

    machines: dict[str, Machine] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        machine = Machine.parse(raw)
        # Last write wins on a duplicate host, which is also what the relay
        # does: its slots are keyed by host name.
        machines[machine.host] = machine

    latest_fw = payload.get("latest_fw")
    return RelayData(
        machines=machines,
        latest_fw=str(latest_fw) if latest_fw else None,
        offline_after_s=offline_after_s,
    )
