#!/usr/bin/env python3
"""
build.py - compile peek-agent-win.py into a single peek-agent.exe.

    python build.py              console window, good for a first run
    python build.py --windowed   no console, for autostart
    python build.py --clean      remove build artefacts and exit

PyInstaller is the only requirement and the script installs it into a local
.venv rather than your system Python, so nothing is changed outside this
folder. The agent itself is standard library only, so the resulting exe is
just CPython plus one script - no psutil, no wheels, no runtime to install on
the target machine.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "peek-agent-win.py"
VENV = HERE / ".venv"
DIST = HERE / "dist"
WORK = HERE / "build"
SPEC = HERE / "peek-agent.spec"

if os.name != "nt":
    sys.exit("build.py produces a Windows .exe and must run on Windows.")


def clean():
    for p in (DIST, WORK, SPEC):
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            print("removed", p.name + "/")
        elif p.exists():
            p.unlink()
            print("removed", p.name)


def venv_python():
    """Create the local venv on first run and return its interpreter."""
    py = VENV / "Scripts" / "python.exe"
    if not py.exists():
        print("creating .venv ...")
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV)])
    return py


def ensure_pyinstaller(py):
    r = subprocess.run([str(py), "-c", "import PyInstaller"], capture_output=True)
    if r.returncode == 0:
        return
    print("installing PyInstaller into .venv ...")
    subprocess.check_call([str(py), "-m", "pip", "install", "--quiet",
                           "--disable-pip-version-check", "pyinstaller"])


def main():
    args = sys.argv[1:]

    if "--clean" in args:
        clean()
        return

    if not SCRIPT.exists():
        sys.exit(f"missing {SCRIPT.name}")

    py = venv_python()
    ensure_pyinstaller(py)

    cmd = [
        str(py), "-m", "PyInstaller",
        "--onefile",
        "--name", "peek-agent",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(HERE),
        "--noconfirm",
        # tkinter only - it is genuinely unused and is the one big win.
        # Do NOT also exclude email/xml/unittest/pydoc: http.server imports
        # email, so dropping it builds cleanly and then fails at runtime with
        # "No module named 'email'". A build that succeeds is not evidence the
        # binary works.
        "--exclude-module", "tkinter",
    ]
    cmd.append("--windowed" if "--windowed" in args else "--console")
    cmd.append(str(SCRIPT))

    print("building ...")
    subprocess.check_call(cmd)

    exe = DIST / "peek-agent.exe"
    if not exe.exists():
        sys.exit("build reported success but produced no exe")

    size_mb = exe.stat().st_size / (1024 * 1024)
    print(f"""
built {exe}  ({size_mb:.1f} MB)

Check it:
  {exe.name} --once

Push to a relay:
  {exe.name} --push https://peek-relay.YOU.workers.dev/ingest/STREAM --token TOKEN

Run at login (no admin needed):
  schtasks /create /tn PeekAgent /sc onlogon /tr "\\"{exe}\\" --push URL --token TOKEN"
""")


if __name__ == "__main__":
    main()
