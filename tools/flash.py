#!/usr/bin/env python3
"""
flash.py - put the firmware on the board. No Arduino IDE, no board menus.

    python tools/flash.py

Finds the device, compiles, uploads, and then shows the serial output so you
can watch it boot and read the pairing code without opening anything else.

    --port COM5   skip detection
    --list        show detected serial ports and exit
    --no-monitor  upload and stop
    --erase       wipe saved settings first, so the device pairs from scratch

Runs tools/setup_arduino.py automatically if the core or libraries are missing.
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKETCH = REPO / "PeekESP"
FQBN = "esp32:esp32:lilygo_t_display:PartitionScheme=huge_app"
BAUD = "115200"

sys.path.insert(0, str(REPO / "tools"))
import setup_arduino as setup           # noqa: E402  (same folder, by design)


def cli():
    p = setup.cli_path()
    if not p:
        print("arduino-cli not found - running setup first\n")
        subprocess.check_call([sys.executable, str(REPO / "tools" / "setup_arduino.py"),
                               "--no-build"])
        p = setup.cli_path()
    return p


def ports(c):
    """(port, description) for everything that looks like a real serial device."""
    r = subprocess.run([str(c), "board", "list"], capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines()[1:]:
        if not line.strip() or line.startswith(("Port", "No boards")):
            continue
        parts = line.split()
        port = parts[0]
        if not re.match(r"^(COM\d+|/dev/tty)", port):
            continue
        out.append((port, " ".join(parts[1:])[:60]))
    return out


def pick_port(c, wanted):
    if wanted:
        return wanted
    found = ports(c)
    if not found:
        sys.exit(
            "No serial port found.\n"
            "  - Is the board plugged in with a DATA usb cable? Charge-only cables\n"
            "    power the board but expose no port at all, which looks identical.\n"
            "  - If Windows shows no COM port, install the CH9102 or CP210x driver.\n"
            "  - python tools/flash.py --list  shows what is detected.")
    if len(found) > 1:
        print("Several serial ports found:")
        for p, d in found:
            print(f"    {p}  {d}")
        sys.exit("Pick one with --port COM5")
    print(f"device: {found[0][0]}  {found[0][1]}")
    return found[0][0]


def monitor(c, port):
    """Stream serial until interrupted. The pairing code is printed here as
    well as shown on screen, which matters when the display is the thing you
    are debugging."""
    print(f"\n--- serial {port} @ {BAUD} - Ctrl+C to stop ---\n", flush=True)
    try:
        subprocess.call([str(c), "monitor", "-p", port, "-c", f"baudrate={BAUD}"])
    except KeyboardInterrupt:
        pass


def main():
    ap = argparse.ArgumentParser(description="compile and flash PeekESP")
    ap.add_argument("--port")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--no-monitor", action="store_true")
    ap.add_argument("--erase", action="store_true",
                    help="erase flash first: forgets WiFi and the pairing code")
    a = ap.parse_args()

    c = cli()

    if a.list:
        found = ports(c)
        print("\n".join(f"{p}  {d}" for p, d in found) or "no serial ports found")
        return

    port = pick_port(c, a.port)

    print("\ncompiling ...")
    r = subprocess.run([str(c), "compile", "--fqbn", FQBN, str(SKETCH)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-25:]
        print("\n".join(tail))
        sys.exit("\ncompile failed - if this mentions a missing library, run:\n"
                 "  python tools/setup_arduino.py")
    for line in r.stdout.splitlines():
        if "Sketch uses" in line or "Global variables" in line:
            print("  " + line.strip())

    if a.erase:
        # A device that has already paired keeps its code, so a re-flash alone
        # will not produce a new one. This is how you get back to a fresh code.
        print("\nerasing saved settings ...")
        subprocess.call([str(c), "burn-bootloader", "-p", port, "--fqbn", FQBN],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print(f"\nuploading to {port} ...")
    r = subprocess.run([str(c), "upload", "-p", port, "--fqbn", FQBN, str(SKETCH)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print((r.stderr or r.stdout).strip()[-800:])
        sys.exit("\nupload failed. If it says 'Failed to connect', hold the LEFT\n"
                 "button (BOOT) while it retries - some boards need that.")

    print("uploaded.")
    if a.no_monitor:
        return
    time.sleep(1.0)
    monitor(c, port)


if __name__ == "__main__":
    main()
