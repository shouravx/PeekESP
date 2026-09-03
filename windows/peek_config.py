"""
peek_config.py - settings shared by the headless agent and the tray app.

Config lives at %APPDATA%\\PeekESP\\config.json so it survives replacing the
exe, and so the tray app and a service instance read the same file. It is
plain JSON on purpose: editing it in Notepad is a supported way to configure
this, not a fallback.

    {
      "mode": "push",        // push | serve | both
      "relay_url": "https://peek-relay.you.workers.dev/ingest/alice",
      "token": "…",
      "interval": 5.0,
      "serve_port": 8080,
      "autostart": false
    }
"""

import json
import os
from pathlib import Path

APP_NAME = "PeekESP"
MODES = ("push", "serve", "both")

# Where a device looks unless told otherwise. Must match RELAY_BASE_URL in the
# firmware, or a paired device and this app would derive the same stream but
# look for it on two different relays.
DEFAULT_RELAY_BASE = "https://peek-relay.peekesp.workers.dev"

DEFAULTS = {
    "mode": "push",
    "pair_code": "",
    "relay_base": DEFAULT_RELAY_BASE,
    "relay_url": "",
    "token": "",
    # The device's read token. This agent never sends it - it is kept only so
    # the settings window can show you what to type into the ESP32 after
    # flashing it, which is otherwise a value you have to keep on a sticky note.
    "device_token": "",
    "interval": 5.0,
    "serve_port": 8080,
    "autostart": False,
}


def device_url(relay_url: str) -> str:
    """
    The URL the ESP32 should poll, derived from the one this agent pushes to.

    They differ only in the verb, and getting them out of step - pushing to
    /ingest/alice while the device reads /telemetry/bob - produces a device
    that sits at NO LINK against a perfectly healthy relay. Deriving it
    removes that whole class of mistake.
    """
    u = (relay_url or "").strip()
    if not u:
        return ""
    i = u.rfind("/ingest")
    if i == -1:
        return u
    return u[:i] + "/telemetry" + u[i + len("/ingest"):]


def new_token() -> str:
    """48 hex chars: 192 bits, and inside the firmware's 65-byte buffer."""
    import secrets
    return secrets.token_hex(24)


def format_pair(code: str) -> str:
    """Display form of a stored pairing code, or "" if there is none."""
    import peek_pair
    c = peek_pair.normalise(code)
    return peek_pair.format_code(c) if c else ""


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    return Path(base) / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


def load() -> dict:
    """Never raises: a corrupt or missing file falls back to defaults, because
    a background agent that refuses to start over a stray comma is worse than
    one that starts with sane settings and says so in the tray."""
    cfg = dict(DEFAULTS)
    p = config_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            for k in DEFAULTS:
                if k in stored:
                    cfg[k] = stored[k]
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        cfg["_error"] = f"could not read {p}, using defaults"
    return validate(cfg)


def validate(cfg: dict) -> dict:
    if cfg.get("mode") not in MODES:
        cfg["mode"] = DEFAULTS["mode"]
    try:
        cfg["interval"] = max(1.0, min(600.0, float(cfg.get("interval", 5.0))))
    except (TypeError, ValueError):
        cfg["interval"] = DEFAULTS["interval"]
    try:
        port = int(cfg.get("serve_port", 8080))
        cfg["serve_port"] = port if 1 <= port <= 65535 else DEFAULTS["serve_port"]
    except (TypeError, ValueError):
        cfg["serve_port"] = DEFAULTS["serve_port"]
    cfg["relay_base"] = (str(cfg.get("relay_base", "") or "").strip().rstrip("/")
                         or DEFAULT_RELAY_BASE)
    cfg["pair_code"] = str(cfg.get("pair_code", "") or "").strip().upper()
    cfg["relay_url"] = str(cfg.get("relay_url", "") or "").strip()
    cfg["token"] = str(cfg.get("token", "") or "").strip()
    cfg["device_token"] = str(cfg.get("device_token", "") or "").strip()
    cfg["autostart"] = bool(cfg.get("autostart", False))
    return cfg


def save(cfg: dict) -> Path:
    cfg = validate(dict(cfg))
    cfg.pop("_error", None)
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = config_path()
    tmp = p.with_suffix(".json.tmp")
    # Write-then-replace: a crash mid-write would otherwise leave a truncated
    # file, and this config holds the token.
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({k: cfg[k] for k in DEFAULTS}, fh, indent=2)
    os.replace(tmp, p)
    return p


def problems(cfg: dict):
    """Human-readable reasons the current settings will not work."""
    out = []
    if cfg["mode"] in ("push", "both"):
        if not cfg["relay_url"]:
            out.append("Relay URL is empty")
        elif not cfg["relay_url"].lower().startswith(("http://", "https://")):
            out.append("Relay URL should start with https://")
        elif "/ingest" not in cfg["relay_url"]:
            out.append("Relay URL should end in /ingest or /ingest/<stream>")
        if not cfg["token"]:
            out.append("Push token is empty")
        if cfg["device_token"] and cfg["device_token"] == cfg["token"]:
            out.append("Push and device tokens must differ")
    return out
