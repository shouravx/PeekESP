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

DEFAULTS = {
    "mode": "push",
    "relay_url": "",
    "token": "",
    "interval": 5.0,
    "serve_port": 8080,
    "autostart": False,
}


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
    cfg["relay_url"] = str(cfg.get("relay_url", "") or "").strip()
    cfg["token"] = str(cfg.get("token", "") or "").strip()
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
    return out
