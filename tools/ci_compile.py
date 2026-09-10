#!/usr/bin/env python3
"""
ci_compile.py - compile the sketch, fail on our own warnings, report the size.

    python tools/ci_compile.py                 the default panel
    python tools/ci_compile.py --all           every panel profile
    python tools/ci_compile.py --panel gc9a01-round

Written in Python rather than as shell in the workflow because the same steps
then behave identically on a laptop and on a runner. The shell version compiled
fine on Linux and then failed extracting the byte count, with the log only
visible to someone who could authenticate to Actions - which is the worst place
for a build to become undebuggable.

What --all does and does not prove
----------------------------------
It compiles the sketch at each panel's geometry, which is the part most likely
to break: the layout arithmetic, the LVGL buffer sizing, and the static_asserts
in layout_metrics.h that pin the T-Display to the pixel.

It does NOT prove the TFT_eSPI driver configuration. arduino-cli builds against
whatever User_Setup the installed library has - the LilyGO one - so every panel
here is compiled with the T-Display's driver and pins and only the *geometry*
varies. The driver, the pins and the rotation come from platformio.ini, and the
only thing that proves those is a panel on a desk.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FQBN = "esp32:esp32:lilygo_t_display:PartitionScheme=huge_app"

# The app partition in huge_app.csv is 0x300000. Anything approaching it should
# be noticed here rather than discovered at flash time.
PARTITION = 3 * 1024 * 1024
CEILING = int(PARTITION * 0.92)

# Kept in step with PeekESP/panel_profiles.h and platformio.ini. A panel in one
# and not the others is the failure this list exists to prevent.
PANELS = [
    ("ttgo-t-display",   "PEEK_PANEL_TTGO_T_DISPLAY",   240, 135),
    ("ili9341-320x240",  "PEEK_PANEL_ILI9341_320X240",  320, 240),
    ("ili9341-240x320",  "PEEK_PANEL_ILI9341_240X320",  240, 320),
    ("st7789-240x240",   "PEEK_PANEL_ST7789_240X240",   240, 240),
    ("st7789-240x320",   "PEEK_PANEL_ST7789_240X320",   240, 320),
    ("gc9a01-round",     "PEEK_PANEL_GC9A01_ROUND",     240, 240),
    ("st7735-160x128",   "PEEK_PANEL_ST7735_160X128",   160, 128),
]

sys.path.insert(0, str(REPO / "tools"))
import setup_arduino as setup           # noqa: E402


# Everything that ends this script says why through ::error::, not through
# stderr. A GitHub Actions log needs a signed-in account to read; annotations
# are on the public API. The first two failures of this job reported nothing
# beyond "exit code 1" to anyone outside Actions, which is how a build becomes
# undebuggable by the people who can still read the code.
def die(msg):
    for line in str(msg).splitlines():
        print("::error::" + line)
    sys.exit(1)


def compile_one(cli, define=None, label="default"):
    """Compile once. Returns (flash, ram). Ends the script on any failure."""
    cmd = [str(cli), "compile", "--fqbn", FQBN, "--warnings", "all"]
    if define:
        cmd += ["--build-property", "compiler.cpp.extra_flags=-D%s=1" % define]
    cmd.append(str(REPO / "PeekESP"))

    r = subprocess.run(cmd, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")

    if r.returncode != 0:
        print(out)
        # The last lines name the file and line, so they are the ones worth
        # carrying into an annotation.
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-40:]
        die("%s did not compile:\n%s" % (label, "\n".join(tail)))

    # Only our own files. The libraries produce warnings we do not control -
    # TFT_eSPI has a permanent one about TOUCH_CS - and failing on those would
    # make this job impossible to keep green for reasons nobody here can fix.
    ours = [ln for ln in out.splitlines()
            if re.search(r"(PeekESP\.ino|clock_faces\.h|panel_profiles\.h|"
                         r"layout_metrics\.h):\d+:\d+: warning", ln)]
    if ours:
        for ln in ours:
            print("::error::" + ln.strip())
        die("%d warning(s) in %s" % (len(ours), label))

    m = re.search(r"Sketch uses (\d+) bytes", out)
    g = re.search(r"Global variables use (\d+) bytes", out)
    if not m:
        # Say what was actually there rather than failing on an empty variable.
        print(out)
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-40:]
        die("could not find the size in arduino-cli's output for %s. It said:\n%s"
            % (label, "\n".join(tail)))

    flash = int(m.group(1))
    ram = int(g.group(1)) if g else 0
    if flash > CEILING:
        die("%s is within 8%% of the %d byte app partition" % (label, PARTITION))
    return flash, ram


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--all", action="store_true",
                    help="compile every panel profile")
    ap.add_argument("--panel", metavar="NAME",
                    help="compile one panel profile by name")
    args = ap.parse_args()

    cli = setup.cli_path()
    if not cli:
        die("arduino-cli not found. Looked in %s and on PATH. "
            "Run: python tools/setup_arduino.py" % setup.CLI_DIR)
    print("arduino-cli  %s" % cli)

    if args.panel:
        match = [p for p in PANELS if p[0] == args.panel]
        if not match:
            die("unknown panel '%s'. Known: %s"
                % (args.panel, ", ".join(p[0] for p in PANELS)))
        targets = match
    elif args.all:
        targets = PANELS
    else:
        flash, ram = compile_one(cli)
        print("flash   %s bytes  (%.1f%% of 3 MB)"
              % ("{:,}".format(flash), 100.0 * flash / PARTITION))
        print("ram     %s bytes" % "{:,}".format(ram))
        print("warnings none")
        return

    print("")
    print("%-18s %6s %14s %12s" % ("panel", "size", "flash", "ram"))
    print("-" * 54)
    for name, define, w, h in targets:
        flash, ram = compile_one(cli, define, name)
        print("%-18s %6s %10s b  %10s b  %.1f%%"
              % (name, "%dx%d" % (w, h), "{:,}".format(flash),
                 "{:,}".format(ram), 100.0 * flash / PARTITION))
    print("")
    print("warnings none")
    print("%d panel(s) compiled" % len(targets))
    print("")
    print("NOTE: geometry only. Every build above used the installed TFT_eSPI")
    print("      setup, so the driver, pins and rotation are NOT verified here")
    print("      - those come from platformio.ini and need the actual panel.")


if __name__ == "__main__":
    main()
