"""Configuration, in the same shape as the agent's.

KEY=VALUE in a file the service reads, because that is what dietpi/peekesp
already does and a second format for a second program on the same machine is a
second thing to remember.

Anything after PEEK_PANEL_ is passed to the panel driver as a keyword, so a
display with an unusual address or an extra pin needs no code change:

    PEEK_PANEL=ssd1306
    PEEK_PANEL_ADDRESS=0x3D
    PEEK_PANEL_ROTATE=2
"""

from __future__ import annotations

import os
import shlex

DEFAULT_PATH = "/etc/peekesp/display.conf"

PANEL_PREFIX = "PEEK_PANEL_"

DEFAULTS = {
    "PEEK_PAIR_CODE": "",
    "PEEK_RELAY_BASE": "https://peek-relay.peekesp.workers.dev",
    "PEEK_PANEL": "png",
    # The display polls; it does not receive. Five seconds matches the ESP32
    # and the agents, and costs 17,280 requests a day against Cloudflare's free
    # 100,000 - see the README before lowering it.
    "PEEK_INTERVAL": "5",
    # How long one machine stays on screen before the next. Only meaningful
    # with more than one machine under the pairing code.
    "PEEK_ROTATE_SECONDS": "8",
    # Five missed polls, the same judgement the firmware makes.
    "PEEK_OFFLINE_AFTER_POLLS": "5",
    "PEEK_BRIGHTNESS": "",
}


def _coerce(value):
    """0x3C stays hex, "true" becomes True, "4" becomes int, else the string."""
    text = str(value).strip()
    low = text.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(text, 16) if low.startswith("0x") else int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def read_file(path):
    """Parse KEY=VALUE. Missing file is not an error - defaults cover it."""
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                # shlex so a quoted value keeps its spaces and a trailing
                # comment does not become part of a URL.
                parts = shlex.split(value, comments=True)
                values[key.strip()] = parts[0] if parts else ""
    except FileNotFoundError:
        pass
    except OSError as err:
        raise SystemExit("cannot read %s: %s" % (path, err))
    return values


class Config:
    def __init__(self, values):
        self._v = values

    def __getitem__(self, key):
        return self._v.get(key, DEFAULTS.get(key, ""))

    def get(self, key, default=None):
        value = self._v.get(key, DEFAULTS.get(key))
        return default if value in (None, "") else value

    def int(self, key, default=0):
        try:
            return int(float(self.get(key, default)))
        except (TypeError, ValueError):
            return default

    @property
    def panel_options(self):
        """Everything under PEEK_PANEL_, lower-cased, for the driver."""
        return {
            key[len(PANEL_PREFIX):].lower(): _coerce(value)
            for key, value in self._v.items()
            if key.startswith(PANEL_PREFIX) and value != ""
        }

    @property
    def offline_after_s(self):
        """Never less than a minute, so a fast poll cannot declare a machine
        dead in the gap between two of its own pushes."""
        return max(60, self.int("PEEK_INTERVAL", 5)
                   * self.int("PEEK_OFFLINE_AFTER_POLLS", 5))


def load(path=None, overrides=None):
    """File, then environment, then command line - each beating the last."""
    values = dict(DEFAULTS)
    values.update(read_file(path or os.environ.get("PEEK_CONFIG", DEFAULT_PATH)))
    # The environment wins over the file so a systemd drop-in or a one-off
    # `PEEK_PANEL=png peek-display` can override without editing anything.
    for key in list(values) + [k for k in os.environ if k.startswith("PEEK_")]:
        if key in os.environ:
            values[key] = os.environ[key]
    for key, value in (overrides or {}).items():
        if value is not None:
            values[key] = str(value)
    return Config(values)
