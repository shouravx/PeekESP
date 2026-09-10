"""Poll the PeekESP relay and hand Home Assistant one reading per machine.

Only the I/O lives here. Turning a reply into machines is in model.py, which
imports nothing from Home Assistant and is tested on its own.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, MIN_OFFLINE_AFTER_S, OFFLINE_AFTER_POLLS
from .model import Machine, RelayData, parse_payload
from .pairing import derive

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 15

__all__ = ["Machine", "PeekCoordinator", "RelayData"]


class PeekCoordinator(DataUpdateCoordinator[RelayData]):
    """Reads the relay; never talks to a monitored machine directly."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        relay: str,
        pair_code: str,
        scan_interval: int,
    ) -> None:
        self.relay = relay.rstrip("/")
        self.scan_interval = scan_interval

        keys = derive(pair_code)
        self.stream: str = keys["stream"]
        self._read_token: str = keys["read"]
        self._push_token: str = keys["push"]

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=scan_interval),
        )

    @property
    def offline_after_s(self) -> int:
        """How stale a reading may be before its machine counts as gone.

        Derived from the poll interval rather than fixed: five missed polls is
        the same judgement the firmware makes. Bounded below so that a fast
        poll cannot declare a machine dead in the gap between two of its own
        pushes - an agent on its 5-second default would otherwise be marked
        offline by a 5-second HA poll that happened to land first.
        """
        return max(MIN_OFFLINE_AFTER_S, self.scan_interval * OFFLINE_AFTER_POLLS)

    async def _async_update_data(self) -> RelayData:
        session = async_get_clientsession(self.hass)
        url = f"{self.relay}/telemetry/{self.stream}"

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                response = await session.get(
                    url, headers={"Authorization": f"Bearer {self._read_token}"}
                )
                if response.status == 401:
                    # The pairing code changed - someone pressed "new code" on
                    # the device, or it was re-flashed with --erase. That is a
                    # credential problem a reconfigure fixes, not something to
                    # retry every 30 seconds until the end of time.
                    raise ConfigEntryAuthFailed("the relay rejected this pairing code")
                if response.status in (404, 503):
                    # 503 is the relay's "no telemetry received yet" - a valid
                    # code with no agent running against it. That is a correct
                    # answer, not a failure: reporting it as one would hide a
                    # real outage behind the message for a normal first run.
                    return RelayData(offline_after_s=self.offline_after_s)
                response.raise_for_status()
                payload = await response.json(content_type=None)
        except ConfigEntryAuthFailed:
            raise
        except asyncio.TimeoutError as err:
            raise UpdateFailed(
                f"{self.relay} did not answer in {REQUEST_TIMEOUT}s"
            ) from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"cannot reach {self.relay}: {err}") from err

        try:
            return parse_payload(payload, self.offline_after_s)
        except ValueError as err:
            raise UpdateFailed(str(err)) from err

    async def async_send_command(self, verb: str) -> None:
        """Leave a command for the display to collect on its next poll.

        Authorised with the push token, which is what the agent already holds:
        anyone who can forge this machine's telemetry can also ask its display
        to reboot, and neither is possible without the pairing code.
        """
        session = async_get_clientsession(self.hass)
        url = f"{self.relay}/command/{self.stream}"

        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                # The body is the bare verb, not JSON. The Worker hands
                # request.text() straight to parseCommand, so {"cmd":"reboot"}
                # would be read as that literal string and rejected as an
                # unknown command - a 400 for a request that looks right.
                response = await session.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._push_token}",
                        "Content-Type": "text/plain",
                    },
                    data=verb,
                )
                response.raise_for_status()
        except asyncio.TimeoutError as err:
            raise UpdateFailed(
                f"{self.relay} did not answer in {REQUEST_TIMEOUT}s"
            ) from err
        except aiohttp.ClientError as err:
            raise UpdateFailed(f"could not send '{verb}': {err}") from err

        _LOGGER.debug("sent '%s' to stream %s", verb, self.stream)
