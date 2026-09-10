"""Config flow for PeekESP."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .const import (
    CONF_NAME,
    CONF_PAIR_CODE,
    CONF_RELAY,
    CONF_SCAN_INTERVAL,
    DEFAULT_NAME,
    DEFAULT_RELAY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .pairing import InvalidPairCode, derive

_LOGGER = logging.getLogger(__name__)

PROBE_TIMEOUT = 15


async def _probe(hass, relay: str, pair_code: str) -> tuple[str, int]:
    """Return (stream, machine_count). Raises for anything that blocks setup.

    The relay claims each role's token on first use, so a well-formed code that
    nobody has used is accepted rather than rejected - which means this cannot
    tell a correct code from a typo. Both derive a valid stream; the typo's is
    simply empty. So this reports the machine count and the flow tells the user
    what was found, instead of pretending to have verified something it cannot.
    """
    keys = derive(pair_code)                    # raises InvalidPairCode
    session = async_get_clientsession(hass)
    url = f"{relay.rstrip('/')}/telemetry/{keys['stream']}"

    async with asyncio.timeout(PROBE_TIMEOUT):
        response = await session.get(
            url, headers={"Authorization": f"Bearer {keys['read']}"}
        )
        if response.status == 401:
            # The stream exists and a different read token already claimed it.
            # Someone else has this pairing code, or the device was re-paired
            # and this is the old one.
            raise PermissionError
        if response.status in (404, 503):
            return keys["stream"], 0            # valid, nothing pushing yet
        response.raise_for_status()
        payload = await response.json(content_type=None)

    rows = payload.get("devices") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        return keys["stream"], len(rows)
    return keys["stream"], 1 if isinstance(payload, dict) and payload.get("host") else 0


class PeekESPConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair Home Assistant with a PeekESP relay stream."""

    VERSION = 1

    def __init__(self) -> None:
        self._reauth_entry: ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        description_placeholders: dict[str, str] = {}

        if user_input is not None:
            relay = user_input.get(CONF_RELAY, DEFAULT_RELAY).strip()
            code = user_input[CONF_PAIR_CODE]

            if not relay.startswith("https://"):
                errors[CONF_RELAY] = "insecure_relay"
            else:
                try:
                    stream, found = await _probe(self.hass, relay, code)
                except InvalidPairCode:
                    errors[CONF_PAIR_CODE] = "invalid_code"
                except PermissionError:
                    errors[CONF_PAIR_CODE] = "already_claimed"
                except asyncio.TimeoutError:
                    errors["base"] = "timeout"
                except aiohttp.ClientError:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001 - surfaced as unknown, and logged
                    _LOGGER.exception("unexpected error probing the relay")
                    errors["base"] = "unknown"
                else:
                    # The stream, not the code: two entries for the same code
                    # would poll the same data twice and double the request
                    # cost against a budget that is already the limiting
                    # factor. The stream is derived from the code, so this
                    # catches "K7M2P4QX9R" and "k7m2-p4qx-9r" as the same one.
                    await self.async_set_unique_id(stream)
                    self._abort_if_unique_id_configured()

                    data = {CONF_PAIR_CODE: code, CONF_RELAY: relay}
                    title = user_input.get(CONF_NAME, "").strip() or DEFAULT_NAME

                    if found == 0:
                        # Not an error. A code typed before any agent is
                        # installed is the normal first-run order, and so is a
                        # typo - the flow cannot tell them apart, so it says so
                        # rather than guessing.
                        description_placeholders["found"] = (
                            "No machines are pushing to this code yet. That is "
                            "expected if you have not installed an agent, but it "
                            "is also what a mistyped code looks like."
                        )
                    return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PAIR_CODE): str,
                    vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
                    vol.Optional(CONF_RELAY, default=DEFAULT_RELAY): str,
                }
            ),
            errors=errors,
            description_placeholders=description_placeholders or None,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """The pairing code changed - the device generated a new one."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        assert entry is not None

        if user_input is not None:
            relay = entry.data.get(CONF_RELAY, DEFAULT_RELAY)
            code = user_input[CONF_PAIR_CODE]
            try:
                stream, _ = await _probe(self.hass, relay, code)
            except InvalidPairCode:
                errors[CONF_PAIR_CODE] = "invalid_code"
            except PermissionError:
                errors[CONF_PAIR_CODE] = "already_claimed"
            except (asyncio.TimeoutError, aiohttp.ClientError):
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_PAIR_CODE: code},
                    unique_id=stream,
                )
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PAIR_CODE): str}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PeekESPOptionsFlow()


class PeekESPOptionsFlow(OptionsFlow):
    """How often to poll."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_SCAN_INTERVAL,
                            max=MAX_SCAN_INTERVAL,
                            step=5,
                            unit_of_measurement="seconds",
                            mode=NumberSelectorMode.BOX,
                        )
                    )
                }
            ),
        )
