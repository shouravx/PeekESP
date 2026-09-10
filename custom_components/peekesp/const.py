"""Constants for the PeekESP integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "peekesp"

CONF_PAIR_CODE = "pair_code"
CONF_RELAY = "relay"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_NAME = "name"

DEFAULT_RELAY = "https://peek-relay.peekesp.workers.dev"

# The entry is named, rather than titled with the pairing code. The code is the
# credential - anything that can read it can push telemetry to the display - and
# a config entry title is shown in the UI, quoted in support threads and
# screenshotted into issues. The stream id would leak nothing but means nothing
# to anyone either, so the user names it and the default is just the product.
DEFAULT_NAME = "PeekESP"

# Five seconds is what the agents push at and what the display polls at, but
# Home Assistant is a third reader of the same budget. Cloudflare's free tier
# is 100,000 requests a day across everything: an agent at 5s already costs
# 17,280 and so does a display, so an integration polling at 5s would add a
# third of the budget to watch numbers that a dashboard redraws once a minute
# anyway. Thirty seconds costs 2,880 a day and is still faster than anyone
# reads a gauge.
DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 5
MAX_SCAN_INTERVAL = 3600

UPDATE_INTERVAL = timedelta(seconds=DEFAULT_SCAN_INTERVAL)

# A machine is shown as unavailable once its reading is older than this. The
# relay keeps a slot for 24 hours, so without a threshold a host that died
# yesterday would still report yesterday's CPU as though it were current -
# which is the exact failure this project exists to catch.
#
# Derived from the poll interval rather than fixed: at 30s this is 150s, which
# is five missed pushes from an agent on its 5s default. Bounded below so a
# fast poll cannot mark a machine dead between two of its own pushes.
OFFLINE_AFTER_POLLS = 5
MIN_OFFLINE_AFTER_S = 60

# The relay caps a pairing code at six machines.
MAX_DEVICES = 6

# The closed vocabulary the relay accepts. Kept in step with COMMANDS in
# cloudflare/src/index.js - anything not in that set is rejected at the edge,
# so a button for it would be a button that silently does nothing.
COMMAND_REBOOT = "reboot"
COMMAND_STANDBY = "standby"
COMMAND_WAKE = "wake"
COMMAND_REFRESH = "refresh"
COMMAND_IDENTIFY = "identify"

# Manufacturer/model strings for the device registry. The machines are not
# PeekESP hardware - they are whatever is running the agent - so they are
# registered as what they are, with the integration named as the source.
MANUFACTURER = "PeekESP"
MODEL_MACHINE = "Monitored machine"
MODEL_DISPLAY = "ESP32 display"
