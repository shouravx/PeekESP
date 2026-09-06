#!/usr/bin/env python3
"""
ci_compile.py - compile the sketch, fail on our own warnings, report the size.

    python tools/ci_compile.py

Written in Python rather than as shell in the workflow because the same three
steps then behave identically on a laptop and on a runner. The shell version
compiled fine on Linux and then failed extracting the byte count, with the log
only visible to someone who could authenticate to Actions - which is the worst
place for a build to become undebuggable.

It also resolves arduino-cli through setup_arduino.cli_path() instead of a
hardcoded path, so "arduino-cli" and "arduino-cli.exe" stop being a difference
the workflow has to know about.
"""

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

sys.path.insert(0, str(REPO / "tools"))
import setup_arduino as setup           # noqa: E402


def main():
    cli = setup.cli_path()
    if not cli:
        sys.exit("arduino-cli not found - run: python tools/setup_arduino.py")

    print(f"arduino-cli  {cli}")
    r = subprocess.run([str(cli), "compile", "--fqbn", FQBN,
                        "--warnings", "all", str(REPO / "PeekESP")],
                       capture_output=True, text=True)
    out = (r.stdout or "") + (r.returncode and (r.stderr or "") or (r.stderr or ""))

    if r.returncode != 0:
        print(out)
        sys.exit("the sketch did not compile")

    # Only our own files. The libraries produce warnings we do not control -
    # TFT_eSPI has a permanent one about TOUCH_CS - and failing on those would
    # make this job impossible to keep green for reasons nobody here can fix.
    ours = [ln for ln in out.splitlines()
            if re.search(r"(PeekESP\.ino|clock_faces\.h):\d+:\d+: warning", ln)]
    if ours:
        for ln in ours:
            print("::error::" + ln.strip())
        sys.exit(f"{len(ours)} warning(s) in the sketch")

    m = re.search(r"Sketch uses (\d+) bytes", out)
    g = re.search(r"Global variables use (\d+) bytes", out)
    if not m:
        # Say what was actually there rather than failing on an empty variable.
        print(out[-2000:])
        sys.exit("could not find the size in arduino-cli's output (shown above)")

    flash = int(m.group(1))
    ram = int(g.group(1)) if g else 0
    print(f"flash   {flash:,} bytes  ({100.0 * flash / PARTITION:.1f}% of 3 MB)")
    if ram:
        print(f"ram     {ram:,} bytes")
    print("warnings none")

    if flash > CEILING:
        sys.exit(f"the sketch is within 8% of the {PARTITION:,} byte app partition")


if __name__ == "__main__":
    main()
