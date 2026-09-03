#!/usr/bin/env python3
"""
quickstart.py - set up PeekESP from a fresh clone, in one command.

    python quickstart.py

Runs the steps in order and stops at the first real failure:

    1. build the Windows app  (Windows only)
    2. flash the board        (if one is plugged in)

The firmware is committed already built, so flashing needs no ESP32 core, no
libraries and no 250 MB toolchain - only esptool, which is ~3 MB. Pass --dev if
you intend to CHANGE the firmware; that installs the full Arduino toolchain
first and compiles from source instead.

Each step is a script you can run on its own afterwards - this only saves you
knowing the order. Skip any with --no-app / --no-flash.

Nothing here needs a Cloudflare account, a token, or a port forward. The device
shows a pairing code when it boots and the app asks for that code; everything
else is derived from it.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
PY = sys.executable


def rule(n, total, title):
    print(f"\n{'=' * 60}\n  step {n}/{total}  {title}\n{'=' * 60}", flush=True)


def step(script, *args, cwd=None):
    return subprocess.call([PY, str(script), *args], cwd=str(cwd) if cwd else None)


def main():
    ap = argparse.ArgumentParser(description="one-command PeekESP setup")
    ap.add_argument("--dev", action="store_true",
                    help="install the Arduino toolchain and build from source")
    ap.add_argument("--no-app", action="store_true", help="skip building the Windows app")
    ap.add_argument("--no-flash", action="store_true", help="skip flashing the board")
    ap.add_argument("--port", help="serial port, if detection picks the wrong one")
    a = ap.parse_args()

    if sys.version_info < (3, 8):
        sys.exit("needs Python 3.8 or newer")

    windows = os.name == "nt"
    todo = []
    if a.dev:
        todo.append("libs")
    if not a.no_app and windows:
        todo.append("app")
    if not a.no_flash:
        todo.append("flash")
    total = len(todo)
    n = 0

    if "libs" in todo:
        n += 1
        rule(n, total, "installing the ESP32 core and libraries")
        print("First run downloads ~250 MB of toolchain. Later runs are quick.\n")
        if step(REPO / "tools" / "setup_arduino.py") != 0:
            sys.exit("\nSetup failed above. Nothing else was changed.")

    if "app" in todo:
        n += 1
        rule(n, total, "building the Windows app")
        if step(REPO / "windows" / "build.py", cwd=REPO / "windows") != 0:
            # Not fatal: the firmware half is still usable, and the most common
            # cause is simply that the app is already running.
            print("\n! The app did not build - see above. Continuing anyway.")
        else:
            print(f"\n  {REPO / 'windows' / 'dist' / 'PeekESP.exe'}")

    if "flash" in todo:
        n += 1
        rule(n, total, "flashing the board")
        args = ["--no-monitor"] + (["--port", a.port] if a.port else [])
        if a.dev:
            args.append("--build")
        if step(REPO / "tools" / "flash.py", *args) != 0:
            print("\n! Not flashed - see above. Plug the board in and run:")
            print("    python tools/flash.py")

    print(f"""
{'=' * 60}
  next
{'=' * 60}

  1. The device shows a pairing code, like  K7M2-P4QX-9R
  2. Run windows/dist/PeekESP.exe  ->  tray icon  ->  Settings
  3. Type the code into "Pair a device", press Pair, then Save

  Watch it boot:   python tools/flash.py
  Fresh code:      python tools/flash.py --erase
  Change firmware: python quickstart.py --dev
""")


if __name__ == "__main__":
    main()
