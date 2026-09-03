#!/usr/bin/env python3
"""
flash.py - put the firmware on the board. No Arduino IDE, no toolchain.

    python tools/flash.py

Flashes the prebuilt image committed at firmware/PeekESP-merged.bin, then shows
the serial output so you can watch it boot and read the pairing code. Nothing
is compiled, so this needs neither the ESP32 core nor any library - only
esptool, which is ~3 MB and installed into tools/.venv on first use.

    --build       compile from source instead (needs tools/setup_arduino.py)
    --port COM5   skip detection
    --list        show detected serial ports and exit
    --erase       wipe saved settings first, so the device pairs from scratch
    --no-monitor  flash and stop

Rebuild the committed image after changing the sketch:

    python tools/export_firmware.py
"""

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SKETCH = REPO / "PeekESP"
MERGED = REPO / "firmware" / "PeekESP-merged.bin"
VENV = REPO / "tools" / ".venv"
FQBN = "esp32:esp32:lilygo_t_display:PartitionScheme=huge_app"
BAUD = 115200

sys.path.insert(0, str(REPO / "tools"))
import setup_arduino as setup           # noqa: E402  (same folder, by design)


def venv_python():
    py = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not py.exists():
        print("creating tools/.venv ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    return py


def esptool_cmd():
    """
    Prefer the esptool that ships inside the ESP32 core, if the core happens to
    be installed - it is already there and costs nothing. Otherwise put one in
    a local venv rather than on the system Python.
    """
    core = Path.home() / "AppData/Local/Arduino15/packages/esp32" if sys.platform == "win32" \
        else Path.home() / ".arduino15/packages/esp32"
    if core.exists():
        for name in ("esptool.exe", "esptool", "esptool.py"):
            hit = next(core.rglob(name), None)
            if hit:
                return [str(hit)]

    py = venv_python()
    r = subprocess.run([str(py), "-c", "import esptool"], capture_output=True)
    if r.returncode != 0:
        print("installing esptool (~3 MB) into tools/.venv ...")
        subprocess.check_call([str(py), "-m", "pip", "install", "--quiet",
                               "--disable-pip-version-check", "esptool"])
    return [str(py), "-m", "esptool"]


def ports():
    """Serial ports, via pyserial in the local venv, so this needs no core."""
    py = venv_python()
    if subprocess.run([str(py), "-c", "import serial.tools.list_ports"],
                      capture_output=True).returncode != 0:
        print("installing pyserial into tools/.venv ...")
        subprocess.check_call([str(py), "-m", "pip", "install", "--quiet",
                               "--disable-pip-version-check", "pyserial"])
    r = subprocess.run(
        [str(py), "-c",
         "import serial.tools.list_ports as p;"
         "print('\\n'.join(f'{x.device}\\t{x.description}' for x in p.comports()))"],
        capture_output=True, text=True)
    out = []
    for line in r.stdout.splitlines():
        if "\t" in line:
            dev, desc = line.split("\t", 1)
            if re.match(r"^(COM\d+|/dev/tty)", dev):
                out.append((dev, desc[:60]))
    return out


def pick_port(wanted):
    if wanted:
        return wanted
    found = ports()
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


def monitor(port):
    py = venv_python()
    print(f"\n--- serial {port} @ {BAUD} - Ctrl+C to stop ---\n", flush=True)
    code = (
        "import serial,sys;"
        f"s=serial.Serial('{port}',{BAUD},timeout=1)\n"
        "while True:\n"
        "    d=s.readline()\n"
        "    if d: sys.stdout.write(d.decode('utf-8','replace')); sys.stdout.flush()\n"
    )
    try:
        subprocess.call([str(py), "-c", code])
    except KeyboardInterrupt:
        pass


def build_from_source():
    cli = setup.cli_path()
    if not cli:
        sys.exit("--build needs the toolchain:  python tools/setup_arduino.py")
    print("compiling ...")
    r = subprocess.run([str(cli), "compile", "--fqbn", FQBN, str(SKETCH)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("\n".join((r.stderr or r.stdout).strip().splitlines()[-25:]))
        sys.exit("\ncompile failed")
    for line in r.stdout.splitlines():
        if "Sketch uses" in line or "Global variables" in line:
            print("  " + line.strip())
    return cli


def main():
    ap = argparse.ArgumentParser(description="flash PeekESP")
    ap.add_argument("--port")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--build", action="store_true", help="compile instead of using firmware/")
    ap.add_argument("--erase", action="store_true",
                    help="erase flash first: forgets WiFi and the pairing code")
    ap.add_argument("--no-monitor", action="store_true")
    a = ap.parse_args()

    if a.list:
        found = ports()
        print("\n".join(f"{p}  {d}" for p, d in found) or "no serial ports found")
        return

    if a.build:
        cli = build_from_source()
        port = pick_port(a.port)
        print(f"\nuploading to {port} ...")
        r = subprocess.run([str(cli), "upload", "-p", port, "--fqbn", FQBN, str(SKETCH)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print((r.stderr or r.stdout).strip()[-800:])
            sys.exit("\nupload failed. Hold the LEFT button (BOOT) and try again.")
    else:
        if not MERGED.exists():
            sys.exit(f"{MERGED} is missing - run: python tools/export_firmware.py")
        esp = esptool_cmd()
        port = pick_port(a.port)
        size_mb = MERGED.stat().st_size / (1024 * 1024)
        print(f"\nflashing {MERGED.name} ({size_mb:.1f} MB) to {port} ...")

        if a.erase:
            print("erasing flash first ...")
            subprocess.call(esp + ["--chip", "esp32", "--port", port, "erase_flash"])

        r = subprocess.call(esp + ["--chip", "esp32", "--port", port, "--baud", "921600",
                                   "write_flash", "-z", "0x0", str(MERGED)])
        if r != 0:
            sys.exit("\nflash failed. If it says 'Failed to connect', hold the LEFT\n"
                     "button (BOOT) while it retries - some boards need that.")

    print("\ndone. The device should show a pairing code in a few seconds.")
    if a.no_monitor:
        return
    time.sleep(1.5)
    monitor(port)


if __name__ == "__main__":
    main()
