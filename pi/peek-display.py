#!/usr/bin/env python3
"""
peek-display.py - show PeekESP telemetry on a screen attached to a Raspberry Pi.

    peek-display.py                      run with /etc/peekesp/display.conf
    peek-display.py --list-panels        what this build can drive
    peek-display.py --panel ssd1306      override the configured panel
    peek-display.py --self-test          a pattern that proves the wiring
    peek-display.py --once --panel png --out frame.png
    peek-display.py --demo --panel png --out frame.png

The same pairing code as the ESP32 display and the agents. A Pi can watch the
machines *and* be one of them: run dietpi/peek-agent.py alongside this and it
appears in its own carousel.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from peekdisplay import app, config, panels          # noqa: E402
from peekdisplay.pairing import InvalidPairCode      # noqa: E402
from peekdisplay.relay import Relay, RelayError, Machine, Snapshot   # noqa: E402


def build_parser():
    p = argparse.ArgumentParser(
        description="PeekESP telemetry on a Raspberry Pi display",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Panels: " + ", ".join(panels.names()),
    )
    p.add_argument("--config", metavar="PATH",
                   help="config file (default %s)" % config.DEFAULT_PATH)
    p.add_argument("--pair-code", metavar="CODE",
                   help="override the configured pairing code")
    p.add_argument("--relay", metavar="URL", help="override the relay URL")
    p.add_argument("--panel", metavar="NAME", help="override the panel")
    p.add_argument("--interval", type=float, metavar="S",
                   help="seconds between polls")
    p.add_argument("--rotate-seconds", type=float, metavar="S",
                   help="seconds each machine stays on screen")
    p.add_argument("--out", metavar="PATH", default="peek.png",
                   help="where the png panel writes (default peek.png)")
    p.add_argument("--width", type=int, help="panel width, for png and overrides")
    p.add_argument("--height", type=int, help="panel height")
    p.add_argument("--scale", type=int, default=1,
                   help="magnify the png output, for looking at small panels")

    p.add_argument("--once", action="store_true",
                   help="one poll, one frame, then exit")
    p.add_argument("--demo", action="store_true",
                   help="one frame from invented data - no relay, no code")
    p.add_argument("--self-test", action="store_true",
                   help="draw a pattern that proves the wiring, then exit")
    p.add_argument("--list-panels", action="store_true",
                   help="list panel names and exit")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


DEMO = [
    Machine(host="kushtia-server", age_s=3, cpu_percent=42.5, ram_percent=78.1,
            storage_percent=61.0, storage_total_gb=1117.9,
            storage_free_gb=436.0, cpu_temp_c=48.3, rx_kbps=1284.0,
            tx_kbps=112.9, uptime_seconds=271830),
    Machine(host="dhaka-laptop", age_s=7, cpu_percent=91.2, ram_percent=55.0,
            storage_percent=93.0, storage_total_gb=476.0, storage_free_gb=33.0,
            cpu_temp_c=71.0, battery_percent=64, battery_charging=True,
            rx_kbps=88.0, tx_kbps=12.0, uptime_seconds=9300),
]


def make_panel(cfg, args):
    name = args.panel or cfg.get("PEEK_PANEL", "png")
    options = dict(cfg.panel_options)
    if args.width:
        options["width"] = args.width
    if args.height:
        options["height"] = args.height
    if name in ("png", "file", "none"):
        options.setdefault("width", 240)
        options.setdefault("height", 135)
        options["path"] = args.out
        options["scale"] = args.scale
    return panels.create(name, **options)


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.list_panels:
        for name in panels.names():
            print(name)
        for alias, target in sorted(panels.ALIASES.items()):
            print("%-12s -> %s" % (alias, target))
        return 0

    cfg = config.load(args.config, overrides={
        "PEEK_PAIR_CODE": args.pair_code,
        "PEEK_RELAY_BASE": args.relay,
        "PEEK_PANEL": args.panel,
        "PEEK_INTERVAL": args.interval,
        "PEEK_ROTATE_SECONDS": args.rotate_seconds,
    })

    try:
        panel = make_panel(cfg, args)
    except panels.PanelError as err:
        print(err, file=sys.stderr)
        return 2

    with panel:
        if args.self_test:
            print("panel     %s  %sx%s" % (panel.name, *panel.size))
            print(app.self_test(panel))
            if isinstance(panel, panels.PngPanel):
                print("wrote     %s" % panel.path)
            return 0

        if args.demo:
            # No relay and no pairing code: this is for looking at a layout on
            # a panel that is not paired to anything yet, which is exactly when
            # you want to see whether it is wired up correctly.
            display = app.Display(panel, relay=None,
                                  offline_after_s=cfg.offline_after_s)
            display.draw(Snapshot(machines=DEMO))
            if isinstance(panel, panels.PngPanel):
                print("wrote %s" % panel.path)
            return 0

        code = cfg.get("PEEK_PAIR_CODE", "")
        if not code:
            print("No pairing code. Put one in %s:\n"
                  "    PEEK_PAIR_CODE=K7M2-P4QX-9R\n"
                  "or pass --pair-code. The device shows it on first boot.\n\n"
                  "To check the wiring without one:  --self-test  or  --demo"
                  % (args.config or config.DEFAULT_PATH), file=sys.stderr)
            return 2

        try:
            relay = Relay(code, cfg.get("PEEK_RELAY_BASE"))
        except InvalidPairCode as err:
            print("error: %s" % err, file=sys.stderr)
            return 2

        display = app.Display(
            panel, relay,
            interval=cfg.int("PEEK_INTERVAL", 5),
            rotate_seconds=cfg.int("PEEK_ROTATE_SECONDS", 8),
            offline_after_s=cfg.offline_after_s,
        )

        if args.once:
            snapshot = display.tick()
            for machine in snapshot.machines:
                print("%-20s cpu %5s  ram %5s  age %ds" % (
                    machine.host,
                    "--" if machine.cpu_percent is None else "%.1f" % machine.cpu_percent,
                    "--" if machine.ram_percent is None else "%.1f" % machine.ram_percent,
                    snapshot.age_of(machine)))
            if not snapshot.machines:
                print("no machines are reporting to this pairing code")
            if isinstance(panel, panels.PngPanel):
                print("wrote %s" % panel.path)
            return 0

        print("panel     %s  %sx%s" % (panel.name, *panel.size))
        print("stream    %s" % relay.stream)
        print("polling   every %ss" % cfg.int("PEEK_INTERVAL", 5))
        display.run()
        return 0


if __name__ == "__main__":
    sys.exit(main())
