#!/usr/bin/env python3
"""
build.py - compile peek_agent_win.py into a single peek-agent.exe.

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
SCRIPT = HERE / "peek_agent_win.py"
TRAY = HERE / "peek_tray.py"
ICON = HERE.parent / "img" / "logo.ico"
VENV = HERE / ".venv"
DIST = HERE / "dist"
WORK = HERE / "build"
SPECS = [HERE / "peek-agent.spec", HERE / "PeekESP.spec"]

if os.name != "nt":
    sys.exit("build.py produces a Windows .exe and must run on Windows.")


def clean():
    for p in [DIST, WORK] + SPECS:
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


def ensure_gui_deps(py):
    """pystray and Pillow are needed only by the tray app; the headless agent
    stays standard-library-only so its exe has nothing extra in it."""
    r = subprocess.run([str(py), "-c", "import pystray, PIL"], capture_output=True)
    if r.returncode == 0:
        return
    print("installing pystray + pillow into .venv ...")
    subprocess.check_call([str(py), "-m", "pip", "install", "--quiet",
                           "--disable-pip-version-check", "pystray", "pillow"])


def ensure_pyinstaller(py):
    r = subprocess.run([str(py), "-c", "import PyInstaller"], capture_output=True)
    if r.returncode == 0:
        return
    print("installing PyInstaller into .venv ...")
    subprocess.check_call([str(py), "-m", "pip", "install", "--quiet",
                           "--disable-pip-version-check", "pyinstaller"])


def check_not_running():
    """
    PyInstaller cannot overwrite a running exe: Windows locks it and the build
    dies with a bare PermissionError several screens into the log, long after
    the part anyone reads. Worse, the previous binary survives, so the next
    test appears to pass while actually exercising stale code. Say so up front.
    """
    stuck = []
    for exe in (DIST / "peek-agent.exe", DIST / "PeekESP.exe"):
        if not exe.exists():
            continue
        try:
            with open(exe, "ab"):
                pass
        except PermissionError:
            stuck.append(exe.name)
    if stuck:
        names = " and ".join(stuck)
        sys.exit(
            f"{names} is running, so it cannot be replaced.\n"
            f"  Quit it from the tray icon, or:  taskkill /F /IM {stuck[0]}\n"
            "  (Closing the settings window is not enough - the tray keeps it alive.)")


def main():
    args = sys.argv[1:]

    if "--clean" in args:
        clean()
        return

    if not SCRIPT.exists():
        sys.exit(f"missing {SCRIPT.name}")

    check_not_running()

    py = venv_python()
    ensure_pyinstaller(py)

    built = []

    # ---- headless agent: stdlib only, small, for services and servers ----
    cmd = [
        str(py), "-m", "PyInstaller", "--onefile",
        "--name", "peek-agent",
        "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(HERE),
        "--noconfirm", "--console",
        # tkinter only - it is genuinely unused HERE and is the one big win.
        # Do NOT also exclude email/xml/unittest/pydoc: http.server imports
        # email, so dropping it builds cleanly and then fails at runtime with
        # "No module named 'email'". A build that succeeds is not evidence the
        # binary works, which is why both exes are smoke-tested after building.
        "--exclude-module", "tkinter",
        "--exclude-module", "PIL",
        # peek_config is imported inside main() for --config, which PyInstaller's
        # static scan can miss entirely.
        "--hidden-import", "peek_config",
        str(SCRIPT),
    ]
    print("building peek-agent.exe (headless) ...")
    subprocess.check_call(cmd)
    built.append(DIST / "peek-agent.exe")

    # ---- tray app: adds the icon, the settings window and pystray ----
    if TRAY.exists():
        ensure_gui_deps(py)
        cmd = [
            str(py), "-m", "PyInstaller", "--onefile",
            "--name", "PeekESP",
            "--distpath", str(DIST), "--workpath", str(WORK), "--specpath", str(HERE),
            "--noconfirm",
            # No console window: this one lives in the tray.
            "--windowed" if "--console" not in args else "--console",
            "--icon", str(ICON),
            # The icon is loaded at runtime for the tray image too, so it has
            # to travel inside the exe, not just be baked into the resource.
            "--add-data", f"{ICON}{os.pathsep}.",
            str(TRAY),
        ]
        print("building PeekESP.exe (tray + settings) ...")
        subprocess.check_call(cmd)
        built.append(DIST / "PeekESP.exe")

    missing = [p for p in built if not p.exists()]
    if missing:
        sys.exit("build reported success but produced no exe: " + str(missing))

    print()
    for p in built:
        print(f"built {p}  ({p.stat().st_size / (1024 * 1024):.1f} MB)")

    print(f"""
Tray app - double-click PeekESP.exe. Right-click the tray icon for Settings,
and tick "Start automatically when I sign in" there.

Headless, for a service or a scheduled task:
  peek-agent.exe --once
  peek-agent.exe --config
  peek-agent.exe --push https://peek-relay.YOU.workers.dev/ingest/STREAM --token TOKEN

--config reads {Path(os.environ.get('APPDATA', '')) / 'PeekESP' / 'config.json'}
so no token appears on the command line, where any process listing would show it.
""")


if __name__ == "__main__":
    main()
