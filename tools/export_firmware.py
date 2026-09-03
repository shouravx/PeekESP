#!/usr/bin/env python3
"""
export_firmware.py - rebuild the flashable image committed in firmware/.

    python tools/export_firmware.py

Compiles the sketch and merges the four pieces the ESP32 expects into one
image that can be written at offset 0:

    0x01000  bootloader
    0x08000  partition table
    0x0e000  boot_app0        (which OTA slot to start - fixed, from the core)
    0x10000  the sketch

Committing the merged result is what lets tools/flash.py work with no core, no
libraries and no 250 MB toolchain - only esptool. Re-run this after changing
the sketch, or the committed image will quietly be the previous firmware.

Needs the toolchain: python tools/setup_arduino.py
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "firmware"
FQBN = "esp32:esp32:lilygo_t_display:PartitionScheme=huge_app"

sys.path.insert(0, str(REPO / "tools"))
import setup_arduino as setup           # noqa: E402


def find_core():
    base = (Path.home() / "AppData/Local/Arduino15/packages/esp32") if sys.platform == "win32" \
        else (Path.home() / ".arduino15/packages/esp32")
    if not base.exists():
        sys.exit("ESP32 core not installed - run: python tools/setup_arduino.py")
    return base


def main():
    cli = setup.cli_path()
    if not cli:
        sys.exit("arduino-cli not found - run: python tools/setup_arduino.py")

    OUT.mkdir(exist_ok=True)
    print("compiling ...")
    r = subprocess.run([str(cli), "compile", "--fqbn", FQBN,
                        "--output-dir", str(OUT), str(REPO / "PeekESP")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("\n".join((r.stderr or r.stdout).strip().splitlines()[-25:]))
        sys.exit("compile failed")
    for line in r.stdout.splitlines():
        if "Sketch uses" in line or "Global variables" in line:
            print("  " + line.strip())

    # The .elf and .map are debug output - 35 MB of it - and have no business
    # in a repository whose point is that the firmware is small enough to ship.
    for junk in OUT.glob("*.elf"):
        junk.unlink()
    for junk in OUT.glob("*.map"):
        junk.unlink()

    core = find_core()
    boot_app0 = next(core.rglob("boot_app0.bin"), None)
    if not boot_app0:
        sys.exit("boot_app0.bin not found in the core")
    shutil.copyfile(boot_app0, OUT / "boot_app0.bin")

    esptool = next((p for n in ("esptool.exe", "esptool", "esptool.py")
                    for p in core.rglob(n)), None)
    if not esptool:
        sys.exit("esptool not found in the core")

    merged = OUT / "PeekESP-merged.bin"
    print("merging ...")
    r = subprocess.run([
        str(esptool), "--chip", "esp32", "merge_bin", "-o", str(merged),
        "--flash_mode", "dio", "--flash_freq", "keep", "--flash_size", "4MB",
        "0x1000", str(OUT / "PeekESP.ino.bootloader.bin"),
        "0x8000", str(OUT / "PeekESP.ino.partitions.bin"),
        "0xe000", str(OUT / "boot_app0.bin"),
        "0x10000", str(OUT / "PeekESP.ino.bin"),
    ], capture_output=True, text=True)
    if r.returncode != 0 or not merged.exists():
        print(r.stderr or r.stdout)
        sys.exit("merge failed")

    # Keep only the merged image: the parts are inside it, and committing both
    # doubles what every clone pays for no benefit.
    for part in ("PeekESP.ino.bin", "PeekESP.ino.bootloader.bin",
                 "PeekESP.ino.partitions.bin", "boot_app0.bin"):
        p = OUT / part
        if p.exists():
            p.unlink()

    print(f"\n{merged}  ({merged.stat().st_size / (1024 * 1024):.2f} MB)")
    print("flash it with:  python tools/flash.py")


if __name__ == "__main__":
    main()
