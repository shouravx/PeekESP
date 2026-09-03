#!/usr/bin/env python3
"""
setup_arduino.py - install everything the sketch needs, in one command.

    python tools/setup_arduino.py

Installs the ESP32 core and all three libraries at the exact versions this
project is verified against, patches TFT_eSPI for the T-Display pinout, drops
lv_conf.h where LVGL looks for it, and then compiles the sketch to prove the
result works. Nothing to download by hand and nothing to edit.

Everything lands in the normal Arduino folders, so the Arduino IDE sees it all
afterwards - this is a setup step, not a parallel toolchain.

    --check     report what is installed and what is missing, change nothing
    --no-build  skip the verification compile

Why the libraries are not simply committed to this repository: LVGL alone is
97 MB across 1160 files and TFT_eSPI another 34 MB. Beyond the repository size,
LVGL finds lv_conf.h by looking one directory above its own folder, which does
not work from inside a sketch's src/ without build flags the Arduino IDE cannot
set. Pinning versions here gets the same reproducibility at none of the cost.
"""

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TOOLS = REPO / "tools"
CLI_DIR = TOOLS / ".arduino-cli"

CORE = "esp32:esp32@2.0.17"
CORE_INDEX = "https://espressif.github.io/arduino-esp32/package_esp32_index.json"
LIBS = ["lvgl@8.3.9", "ArduinoJson@7.4.3", "TFT_eSPI@2.5.43"]

CLI_URLS = {
    ("Windows", "64bit"): "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Windows_64bit.zip",
    ("Linux", "64bit"):   "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Linux_64bit.tar.gz",
    ("Darwin", "64bit"):  "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_macOS_64bit.tar.gz",
}


def say(msg):
    print(msg, flush=True)


def cli_path():
    exe = "arduino-cli.exe" if os.name == "nt" else "arduino-cli"
    local = CLI_DIR / exe
    if local.exists():
        return local
    found = shutil.which(exe)
    return Path(found) if found else None


def install_cli():
    """Fetch arduino-cli into tools/.arduino-cli rather than onto PATH, so this
    never fights with an arduino-cli the machine already had."""
    key = (platform.system(), "64bit")
    url = CLI_URLS.get(key)
    if not url:
        sys.exit(f"no arduino-cli download known for {key}; install it yourself and re-run")

    CLI_DIR.mkdir(parents=True, exist_ok=True)
    archive = CLI_DIR / url.rsplit("/", 1)[-1]
    say(f"downloading arduino-cli ...")
    urllib.request.urlretrieve(url, archive)

    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as z:
            z.extractall(CLI_DIR)
    else:
        import tarfile
        with tarfile.open(archive) as t:
            t.extractall(CLI_DIR)
    archive.unlink()

    p = cli_path()
    if not p:
        sys.exit("extracted arduino-cli but cannot find the binary")
    if os.name != "nt":
        p.chmod(0o755)
    return p


def run(cli, *args, quiet=False):
    cmd = [str(cli), *args]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 and not quiet:
        say("  ! " + " ".join(args[:3]))
        say((r.stderr or r.stdout).strip()[-600:])
    return r


def user_dir(cli):
    r = run(cli, "config", "get", "directories.user", quiet=True)
    return Path(r.stdout.strip() or (Path.home() / "Documents" / "Arduino"))


def patch_tft_setup(libs):
    """
    TFT_eSPI picks its board by an #include in User_Setup_Select.h, and the
    default is a generic setup that leaves the 135x240 panel offset by 40px.
    Doing this here removes the one manual edit the setup used to need.
    """
    f = libs / "TFT_eSPI" / "User_Setup_Select.h"
    if not f.exists():
        return "TFT_eSPI not installed"
    text = f.read_text(encoding="utf-8", errors="replace")
    want = "#include <User_Setups/Setup25_TTGO_T_Display.h>"
    if f"\n{want}" in text and "\n//#include <User_Setup.h>" in text:
        return "already correct"

    backup = f.with_suffix(".h.orig")
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")

    out = []
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("#include <User_Setup.h>"):
            line = "//" + line
        elif st.startswith("//") and want in st:
            line = want + "    // selected by tools/setup_arduino.py"
        out.append(line)
    f.write_text("\n".join(out) + "\n", encoding="utf-8")

    check = f.read_text(encoding="utf-8")
    active = [l for l in check.splitlines() if l.strip().startswith("#include <User_Setup")]
    if len(active) != 1 or want not in active[0]:
        return f"PATCH FAILED - {len(active)} active includes"
    return "patched (original kept as User_Setup_Select.h.orig)"


def status(cli, libs):
    say("\ninstalled:")
    r = run(cli, "core", "list", quiet=True)
    say("  %-11s: " % "core" + "" + ("esp32 2.0.17" if "2.0.17" in r.stdout else "MISSING"))
    r = run(cli, "lib", "list", quiet=True)
    for want in ("lvgl", "ArduinoJson", "TFT_eSPI"):
        line = next((l for l in r.stdout.splitlines() if l.startswith(want)), None)
        say("  %-11s: %s" % (want, " ".join(line.split()[:2]) if line else "MISSING"))
    say("  %-11s: %s" % ("lv_conf.h", "present" if (libs / "lv_conf.h").exists() else "MISSING"))


def main():
    ap = argparse.ArgumentParser(description="one-command Arduino setup for PeekESP")
    ap.add_argument("--check", action="store_true", help="report only, change nothing")
    ap.add_argument("--no-build", action="store_true", help="skip the verification compile")
    a = ap.parse_args()

    cli = cli_path()
    if not cli:
        if a.check:
            sys.exit("arduino-cli not installed - run without --check to fetch it")
        cli = install_cli()
    say(f"arduino-cli: {cli}")

    libs = user_dir(cli) / "libraries"

    if a.check:
        status(cli, libs)
        return

    say(f"\ninstalling into {libs.parent}")
    say("(this pulls ~250 MB of core and toolchain the first time)")

    say(f"\ncore: {CORE}")
    run(cli, "core", "update-index", "--additional-urls", CORE_INDEX, quiet=True)
    r = run(cli, "core", "install", CORE, "--additional-urls", CORE_INDEX)
    if r.returncode != 0:
        sys.exit("core install failed")

    run(cli, "lib", "update-index", quiet=True)
    for lib in LIBS:
        say(f"library: {lib}")
        if run(cli, "lib", "install", lib).returncode != 0:
            sys.exit(f"{lib} failed to install")

    say("\nTFT_eSPI board setup: " + patch_tft_setup(libs))

    libs.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(REPO / "lv_conf.h", libs / "lv_conf.h")
    say(f"lv_conf.h -> {libs / 'lv_conf.h'}")

    status(cli, libs)

    if a.no_build:
        say("\nskipping the verification compile (--no-build)")
        return

    say("\nverifying by compiling the sketch ...")
    r = run(cli, "compile", "--fqbn",
            "esp32:esp32:lilygo_t_display:PartitionScheme=huge_app", str(REPO / "PeekESP"))
    if r.returncode != 0:
        sys.exit("\nthe sketch did not compile - the output above says why")
    for line in r.stdout.splitlines():
        if "Sketch uses" in line or "Global variables" in line:
            say("  " + line.strip())

    say("""
Done. The Arduino IDE will now find all of this:

  Open PeekESP/PeekESP.ino
  Board:            LilyGo T-Display
  Partition Scheme: Huge APP (3MB No OTA/1MB SPIFFS)
  Upload.""")


if __name__ == "__main__":
    main()
