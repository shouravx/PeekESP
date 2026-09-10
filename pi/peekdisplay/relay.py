"""Read telemetry from the relay, and turn it into something typed.

Standard library only. A Raspberry Pi Zero takes a noticeable amount of time to
install a wheel, and this half of the program needs nothing that is not already
in Python - the drawing is where the dependencies live.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .pairing import derive

DEFAULT_RELAY = "https://peek-relay.peekesp.workers.dev"
USER_AGENT = "PeekESP-pi/1.0"
TIMEOUT = 15


class RelayError(RuntimeError):
    """The relay could not be read. Transient unless it says otherwise."""


class AuthError(RelayError):
    """The stream rejected this token - the pairing code changed."""


def _f(raw, default=None):
    """A number, or the default. The relay returns whatever an agent sent."""
    if raw is None or isinstance(raw, bool):
        # bool is an int in Python; True would otherwise become a plausible 1.0.
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value or value in (float("inf"), float("-inf")):
        return default            # NaN and Infinity survive json.loads
    return value


def _i(raw, default=None):
    value = _f(raw)
    return default if value is None else int(value)


def _sensorless(value):
    """-1 is the agents' way of saying "this machine has no such sensor"."""
    return None if value is None or value < 0 else value


@dataclass
class Machine:
    """One machine's most recent reading."""

    host: str = "unknown"
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
    def has_battery(self):
        return self.battery_percent is not None

    @classmethod
    def parse(cls, raw):
        return cls(
            host=str(raw.get("host") or "unknown")[:32],
            age_s=_i(raw.get("age_s"), 0) or 0,
            cpu_percent=_sensorless(_f(raw.get("cpu_percent"))),
            ram_percent=_sensorless(_f(raw.get("ram_percent"))),
            storage_percent=_sensorless(_f(raw.get("storage_percent"))),
            storage_total_gb=_sensorless(_f(raw.get("storage_total_gb"))),
            storage_free_gb=_sensorless(_f(raw.get("storage_free_gb"))),
            cpu_temp_c=_sensorless(_f(raw.get("cpu_temp_c"))),
            battery_percent=_i(_sensorless(_f(raw.get("battery_percent")))),
            battery_minutes=_i(_sensorless(_f(raw.get("battery_minutes")))),
            battery_charging=bool(raw.get("battery_charging")),
            battery_ac=bool(raw.get("battery_ac")),
            rx_kbps=_sensorless(_f(raw.get("net_rx_kbps"))),
            tx_kbps=_sensorless(_f(raw.get("net_tx_kbps"))),
            uptime_seconds=_i(_sensorless(_f(raw.get("uptime_seconds")))),
        )


@dataclass
class Snapshot:
    """Everything one poll produced."""

    machines: list = field(default_factory=list)
    latest_fw: str | None = None
    fetched_at: float = field(default_factory=time.monotonic)
    # Seconds since this process last got a successful reply. Added to each
    # machine's own age, because a Pi that lost its network shows every machine
    # as permanently fresh otherwise - which is exactly backwards, since at
    # that moment it knows nothing at all.
    stale_s: int = 0

    def age_of(self, machine):
        return machine.age_s + self.stale_s

    def is_online(self, machine, offline_after_s):
        return self.age_of(machine) < offline_after_s


def parse_payload(payload):
    """Turn one relay reply into machines, newest Worker or older."""
    if not isinstance(payload, dict):
        raise RelayError("the relay did not return an object")

    rows = payload.get("devices")
    if not isinstance(rows, list) or not rows:
        # The freshest host is repeated at the top level for devices flashed
        # before the array existed. Falling back to it keeps this working
        # against an older deployment rather than showing nothing.
        rows = [payload] if payload.get("host") else []

    machines = []
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        machine = Machine.parse(raw)
        if machine.host in seen:
            continue
        seen.add(machine.host)
        machines.append(machine)

    # Sorted by name, not by recency. The display rotates through this list,
    # and an order that reshuffles whenever a push lands would move a machine
    # out from under whoever is reading it.
    machines.sort(key=lambda m: m.host)

    latest = payload.get("latest_fw")
    return Snapshot(machines=machines, latest_fw=str(latest) if latest else None)


class Relay:
    """Polls one stream. Holds the last good reading so a blip is not a blank."""

    def __init__(self, pair_code, relay_base=DEFAULT_RELAY):
        keys = derive(pair_code)
        self.stream = keys["stream"]
        self._read = keys["read"]
        self._push = keys["push"]
        self.base = relay_base.rstrip("/")
        self.last = Snapshot()
        self._last_ok = None

    # -- HTTP ---------------------------------------------------------------

    def _request(self, url, data=None, token=None, content_type=None):
        headers = {"User-Agent": USER_AGENT, "Authorization": "Bearer " + token}
        if content_type:
            headers["Content-Type"] = content_type
        body = data.encode("utf-8") if isinstance(data, str) else data
        req = urllib.request.Request(url, data=body, headers=headers,
                                     method="POST" if body is not None else "GET")
        # Default verification, explicitly. The relay is a public HTTPS
        # endpoint with a real certificate; a Pi with a wrong clock should fail
        # loudly here rather than be quietly taught to skip the check.
        ctx = ssl.create_default_context()
        return urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx)

    def fetch(self):
        """Return a Snapshot. Raises RelayError; the caller decides what to show."""
        url = "%s/telemetry/%s" % (self.base, self.stream)
        try:
            with self._request(url, token=self._read) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 401:
                raise AuthError(
                    "the relay rejected this pairing code - it may have been "
                    "regenerated on the device"
                ) from err
            if err.code in (404, 503):
                # A valid code that nothing has pushed to yet. Correct, not a
                # failure: an empty snapshot is the honest answer.
                snap = Snapshot()
                self.last = snap
                self._last_ok = time.monotonic()
                return snap
            raise RelayError("relay returned HTTP %d" % err.code) from err
        except urllib.error.URLError as err:
            raise RelayError("cannot reach %s: %s" % (self.base, err.reason)) from err
        except (ValueError, OSError) as err:
            raise RelayError(str(err)) from err

        snap = parse_payload(payload)
        self.last = snap
        self._last_ok = time.monotonic()
        return snap

    def poll(self):
        """fetch(), but a failure ages the previous reading instead of raising.

        A monitor that blanks on one dropped packet is worse than one that says
        "this is 40 seconds old", so a failed poll keeps the last good data and
        starts counting. The renderer draws the age, and past the threshold the
        machine reads OFFLINE - which is the truth in both cases, whether the
        agent stopped or this Pi lost its network.
        """
        try:
            snap = self.fetch()
            snap.stale_s = 0
            return snap, None
        except RelayError as err:
            if self._last_ok is not None:
                self.last.stale_s = int(time.monotonic() - self._last_ok)
            return self.last, err

    def send_command(self, verb):
        """Leave a command for an ESP32 display sharing this pairing code.

        The body is the bare verb, not JSON: the Worker hands request.text()
        straight to its parser, so {"cmd": "reboot"} would be read as that
        literal string and refused as an unknown command.
        """
        url = "%s/command/%s" % (self.base, self.stream)
        try:
            with self._request(url, data=verb, token=self._push,
                               content_type="text/plain") as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            raise RelayError("relay returned HTTP %d for '%s'" % (err.code, verb)) from err
        except urllib.error.URLError as err:
            raise RelayError("cannot reach %s: %s" % (self.base, err.reason)) from err
