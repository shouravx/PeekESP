#!/usr/bin/env python3
"""
build_panels.py - one flashable firmware image per display, built for real.

    python tools/build_panels.py               every panel
    python tools/build_panels.py gc9a01-round  just this one
    python tools/build_panels.py --list

Writes firmware/<panel>/PeekESP-<panel>-merged.bin, each one a single image
that can be flashed at offset 0 with nothing else installed:

    python tools/flash.py --image firmware/gc9a01-round/PeekESP-gc9a01-round-merged.bin

Why PlatformIO and not arduino-cli
----------------------------------
tools/ci_compile.py builds every panel too, and its images are NOT
interchangeable with these. arduino-cli compiles against whatever TFT_eSPI
setup is installed - here the LilyGO one - so across all seven of its builds
only the *geometry* changes. The driver, the pins and the rotation stay the
T-Display's.

An image built that way and flashed onto a GC9A01 would drive an ST7789 on the
T-Display's pins and show nothing at all. Those builds are a compile check and
that is all they are.

The driver and pins live in platformio.ini's build_flags, which only PlatformIO
reads. So the images people actually flash have to come from here.
"""

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "firmware"

# The name is the PlatformIO env, the label is what a human is choosing
# between. Kept in step with platformio.ini and PeekESP/panel_profiles.h -
# a panel in one and not the others is what this list exists to prevent.
PANELS = [
    ("ttgo-t-display",  "LilyGO TTGO T-Display", "240x135", "ST7789"),
    ("ili9341-320x240", "ILI9341, landscape",    "320x240", "ILI9341"),
    ("ili9341-240x320", "ILI9341, upright",      "240x320", "ILI9341"),
    ("st7789-240x240",  "ST7789 1.3in square",   "240x240", "ST7789"),
    ("st7789-240x320",  "ST7789 2.0in",          "240x320", "ST7789"),
    ("gc9a01-round",    "GC9A01 1.28in round",   "240 dia", "GC9A01"),
    ("st7735-160x128",  "ST7735 1.8in",          "160x128", "ST7735"),
]

# esptool wants these four at these offsets. They are the same for every panel
# because they are the partition layout, not the display.
OFFSETS = ("0x1000", "bootloader.bin",
           "0x8000", "partitions.bin",
           "0xe000", "boot_app0.bin",
           "0x10000", "firmware.bin")


def die(msg):
    for line in str(msg).splitlines():
        print("::error::" + line)
    sys.exit(1)


def pio(*args, **kw):
    """Run PlatformIO through this interpreter, so it is the one that has it."""
    return subprocess.run([sys.executable, "-m", "platformio", *args], **kw)


def ensure_pio():
    r = pio("--version", capture_output=True, text=True)
    if r.returncode != 0:
        die("PlatformIO is not installed for this Python.\n"
            "    %s -m pip install platformio" % sys.executable)
    return (r.stdout or "").strip()


def find_esptool(build_dir):
    """esptool ships with the espressif32 platform PlatformIO just installed."""
    roots = [Path.home() / ".platformio" / "packages",
             build_dir.parent.parent]
    names = ("esptool.py", "esptool.exe", "esptool")
    for root in roots:
        if not root.exists():
            continue
        for name in names:
            found = sorted(root.rglob(name))
            if found:
                return found[0]
    return None


def merge(env, build_dir):
    """Combine the four parts into one image flashable at offset 0."""
    parts = []
    for i in range(0, len(OFFSETS), 2):
        offset, filename = OFFSETS[i], OFFSETS[i + 1]
        path = build_dir / filename
        if not path.exists():
            # boot_app0 lives in the framework package rather than the build
            # directory; every other missing part is a real failure.
            if filename == "boot_app0.bin":
                found = sorted((Path.home() / ".platformio" / "packages")
                               .rglob("boot_app0.bin"))
                if not found:
                    die("boot_app0.bin not found - cannot merge %s" % env)
                path = found[0]
            else:
                die("%s did not produce %s" % (env, filename))
        parts += [offset, str(path)]

    esptool = find_esptool(build_dir)
    if not esptool:
        die("esptool not found in the PlatformIO packages")

    dest_dir = OUT / env
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / ("PeekESP-%s-merged.bin" % env)

    cmd = ([sys.executable, str(esptool)] if esptool.suffix == ".py"
           else [str(esptool)])
    cmd += ["--chip", "esp32", "merge_bin", "-o", str(dest),
            "--flash_mode", "dio", "--flash_freq", "40m", "--flash_size", "4MB"]
    cmd += parts

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        die("merging %s failed:\n%s" % (env, (r.stdout or "") + (r.stderr or "")))
    return dest


def verify(env, dest):
    """The image must name its own panel, and no other.

    Worth checking rather than assuming, because the failure it catches is
    silent: a stale .pio directory, a typo in a build flag, or an env whose
    -D never reached the compiler all produce a plausible 1.4 MB image built
    for the wrong display. The only symptom on hardware is a blank screen,
    which looks exactly like bad wiring.

    PANEL_NAME reaches the binary through the boot banner, so this is testing
    the same string the device prints about itself.
    """
    names = {p[0]: NAME_IN_IMAGE[p[0]] for p in PANELS}
    data = dest.read_bytes()
    want = names[env].encode("ascii")
    if want not in data:
        die("%s does not contain %r - it may have been built for another panel"
            % (dest.name, names[env]))
    strays = [n for e, n in names.items()
              if e != env and n.encode("ascii") in data]
    if strays:
        die("%s also contains %s - the build flags are not isolating panels"
            % (dest.name, ", ".join(repr(s) for s in strays)))


# The exact PANEL_NAME strings from PeekESP/panel_profiles.h. A mismatch here
# is itself a failure worth having: it means the two lists have drifted.
NAME_IN_IMAGE = {
    "ttgo-t-display":  "TTGO T-Display 240x135",
    "ili9341-320x240": "ILI9341 320x240",
    "ili9341-240x320": "ILI9341 240x320",
    "st7789-240x240":  "ST7789 240x240",
    "st7789-240x320":  "ST7789 240x320",
    "gc9a01-round":    "GC9A01 240 round",
    "st7735-160x128":  "ST7735 160x128",
}


def build(env):
    print("  compiling ...", end="", flush=True)
    started = time.time()
    r = pio("run", "-e", env, capture_output=True, text=True)
    took = time.time() - started
    if r.returncode != 0:
        print(" failed")
        out = (r.stdout or "") + (r.stderr or "")
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-30:]
        die("%s did not build:\n%s" % (env, "\n".join(tail)))
    print(" %.0fs" % took, end="", flush=True)

    build_dir = REPO / ".pio" / "build" / env
    dest = merge(env, build_dir)
    verify(env, dest)
    size = dest.stat().st_size
    app = (build_dir / "firmware.bin").stat().st_size
    print("   app %s b   image %.2f MB   identifies as %s"
          % ("{:,}".format(app), size / 1048576.0, NAME_IN_IMAGE[env]))
    return dest, app, size


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("panels", nargs="*", help="panel names (default: all)")
    ap.add_argument("--list", action="store_true", help="list panels and exit")
    args = ap.parse_args()

    if args.list:
        for env, label, size, driver in PANELS:
            print("%-18s %-24s %-8s %s" % (env, label, size, driver))
        return

    known = {p[0] for p in PANELS}
    targets = args.panels or [p[0] for p in PANELS]
    unknown = [t for t in targets if t not in known]
    if unknown:
        die("unknown panel(s): %s\nKnown: %s"
            % (", ".join(unknown), ", ".join(sorted(known))))

    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    print("PlatformIO %s" % ensure_pio())
    print("PeekESP %s" % version)
    print("")

    results = []
    for env in targets:
        label = next(p[1] for p in PANELS if p[0] == env)
        print("%-18s %s" % (env, label))
        results.append((env,) + build(env))
        print("")

    print("-" * 62)
    for env, dest, app, size in results:
        print("%-18s %s" % (env, dest.relative_to(REPO)))
    print("")
    print("%d image(s). Flash one with:" % len(results))
    print("    python tools/flash.py --image %s"
          % results[0][1].relative_to(REPO))
    print("")
    print("Only ttgo-t-display has been run on hardware. The rest are built")
    print("from the pins in platformio.ini and have never driven a panel.")


if __name__ == "__main__":
    main()
