#!/usr/bin/env python3
"""
package.py - turn the built exes into a release zip and the winget manifests.

    python package.py                 build, zip, write the manifests
    python package.py --no-build      use whatever is already in dist/
    python package.py --version 1.2.0 override the VERSION file

Produces:

    dist/PeekESP-<version>-win-x64.zip
    dist/PeekESP-<version>-win-x64.zip.sha256
    packaging/winget/<version>/shouravx.PeekESP.yaml
                              /shouravx.PeekESP.installer.yaml
                              /shouravx.PeekESP.locale.en-US.yaml

winget wants one installer per package, and this ships two executables - the
tray app and the headless agent - so the installer is a zip of both, declared
as nested portables. Both end up on PATH.

Nothing here publishes anything. The manifests point at a release URL that
will not exist until you push the tag and publish the release; the SHA-256 is
of the zip this script just made, so it only stays correct if that exact zip
is the one uploaded.
"""

import argparse
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DIST = HERE / "dist"
WINGET = REPO / "packaging" / "winget"

PACKAGE_ID = "shouravx.PeekESP"
REPO_URL = "https://github.com/shouravx/PeekESP"
MANIFEST_VERSION = "1.6.0"

# The tray app first: it is the one a person runs.
PAYLOAD = [
    ("PeekESP.exe", "PeekESP"),
    ("peek-agent.exe", "peek-agent"),
]


def read_version(override):
    if override:
        return override
    f = REPO / "VERSION"
    if not f.exists():
        sys.exit("no VERSION file and no --version given")
    v = f.read_text(encoding="utf-8").strip()
    # winget rejects a version it cannot order, and the failure arrives as a
    # rejected pull request days later rather than here.
    if not v or not all(p.isdigit() for p in v.split(".")):
        sys.exit(f"VERSION is {v!r}; winget wants digits and dots, like 1.2.0")
    return v


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    # winget's schema wants upper case, and its validator is not lenient.
    return h.hexdigest().upper()


def build():
    print("=== building ===")
    r = subprocess.run([sys.executable, str(HERE / "build.py")])
    if r.returncode != 0:
        sys.exit("build failed - nothing packaged")


def make_zip(version):
    name = f"PeekESP-{version}-win-x64.zip"
    out = DIST / name

    missing = [n for n, _ in PAYLOAD if not (DIST / n).exists()]
    if missing:
        sys.exit("not built: " + ", ".join(missing) + "\n  python build.py")

    if out.exists():
        out.unlink()

    print(f"\n=== packing {name} ===")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for exe, _alias in PAYLOAD:
            z.write(DIST / exe, exe)
            print(f"   {exe}  {(DIST / exe).stat().st_size / 1048576:.1f} MB")
        z.write(REPO / "LICENSE", "LICENSE")
        z.writestr("README.txt", READ_ME.format(version=version, repo=REPO_URL))

    digest = sha256(out)
    # Written next to the zip so whoever uploads it can check the manifest
    # against the file rather than against this script's memory of it.
    (DIST / (name + ".sha256")).write_text(f"{digest}  {name}\n", encoding="utf-8")
    print(f"   -> {out.stat().st_size / 1048576:.1f} MB")
    print(f"   sha256 {digest}")
    return name, digest


READ_ME = """PeekESP {version}

PeekESP.exe     the tray app - double-click it, then right-click the tray
                icon for Settings and type the code your device is showing.
peek-agent.exe  headless, for running as a service or a scheduled task.
                  peek-agent.exe --once      one reading, then exit
                  peek-agent.exe --config    edit settings without the tray

These are not code-signed, so Windows SmartScreen will warn the first time.
More info -> Run anyway. Source and build instructions:

  {repo}
"""


# ---------------------------------------------------------------------------
#  winget manifests
# ---------------------------------------------------------------------------
VERSION_YAML = """# Created by windows/package.py
PackageIdentifier: {pid}
PackageVersion: {version}
DefaultLocale: en-US
ManifestType: version
ManifestVersion: {mv}
"""

INSTALLER_YAML = """# Created by windows/package.py
PackageIdentifier: {pid}
PackageVersion: {version}
MinimumOSVersion: 10.0.0.0
InstallerType: zip
NestedInstallerType: portable
NestedInstallerFiles:
{nested}
Installers:
  - Architecture: x64
    InstallerUrl: {url}
    InstallerSha256: {sha}
ReleaseDate: {date}
ManifestType: installer
ManifestVersion: {mv}
"""

LOCALE_YAML = """# Created by windows/package.py
PackageIdentifier: {pid}
PackageVersion: {version}
PackageLocale: en-US
Publisher: shouravx
PublisherUrl: https://github.com/shouravx
PublisherSupportUrl: {repo}/issues
Author: shouravx
PackageName: PeekESP
PackageUrl: {repo}
License: MIT
LicenseUrl: {repo}/blob/main/LICENSE
Copyright: Copyright (c) 2026 MD Shourav Hossain
ShortDescription: Live CPU, RAM, storage and network for your PC, on an ESP32 desk display.
Description: |-
  PeekESP turns a LilyGO TTGO T-Display into a physical dashboard for a machine
  anywhere on the internet - no port forward, no VPN and no account. The device
  shows a pairing code on first boot; typing it into this app is the entire
  setup, because both ends derive the relay stream and tokens from that code
  locally.

  This package contains the tray app and a headless agent for running as a
  service. Several machines can share one pairing code and appear on the same
  display.
Moniker: peekesp
Tags:
  - dashboard
  - esp32
  - hardware
  - monitor
  - monitoring
  - system-monitor
  - telemetry
ReleaseNotesUrl: {repo}/releases/tag/v{version}
ManifestType: defaultLocale
ManifestVersion: {mv}
"""


def write_manifests(version, zip_name, digest):
    import datetime

    out = WINGET / version
    out.mkdir(parents=True, exist_ok=True)
    url = f"{REPO_URL}/releases/download/v{version}/{zip_name}"
    nested = "\n".join(
        f"  - RelativeFilePath: {exe}\n    PortableCommandAlias: {alias}"
        for exe, alias in PAYLOAD)

    files = {
        f"{PACKAGE_ID}.yaml": VERSION_YAML.format(
            pid=PACKAGE_ID, version=version, mv=MANIFEST_VERSION),
        f"{PACKAGE_ID}.installer.yaml": INSTALLER_YAML.format(
            pid=PACKAGE_ID, version=version, mv=MANIFEST_VERSION,
            nested=nested, url=url, sha=digest,
            date=datetime.date.today().isoformat()),
        f"{PACKAGE_ID}.locale.en-US.yaml": LOCALE_YAML.format(
            pid=PACKAGE_ID, version=version, mv=MANIFEST_VERSION, repo=REPO_URL),
    }

    print(f"\n=== manifests -> {out.relative_to(REPO)} ===")
    for name, body in files.items():
        (out / name).write_text(body, encoding="utf-8", newline="\n")
        print("   " + name)
    return out, url


def main():
    ap = argparse.ArgumentParser(description="Package PeekESP for release and winget")
    ap.add_argument("--version", help="override the VERSION file")
    ap.add_argument("--no-build", action="store_true",
                    help="package whatever is already in dist/")
    a = ap.parse_args()

    if os.name != "nt" and not a.no_build:
        sys.exit("building needs Windows; use --no-build to package existing files")

    version = read_version(a.version)
    print(f"version {version}")

    if not a.no_build:
        build()

    zip_name, digest = make_zip(version)
    out, url = write_manifests(version, zip_name, digest)

    print(f"""
{"=" * 68}
Next, in order - none of it has happened yet:

1. Publish the release, so the URL in the manifest exists.

     git tag v{version} && git push origin v{version}

   The release workflow builds and attaches the zip as a DRAFT release.
   Check it, then publish it from the Releases page.

2. Confirm the URL really serves the file:

     curl -sIL {url} | findstr /i "HTTP content-length"

3. Submit to winget. Fork microsoft/winget-pkgs, copy the manifests to
   manifests/s/shouravx/PeekESP/{version}/ and open a pull request.
   Validate them first - this catches the schema mistakes that otherwise
   come back as a review comment days later:

     winget validate --manifest {out.relative_to(REPO)}
     winget install --manifest {out.relative_to(REPO)}

   The second one actually installs from the manifest, which is the only way
   to find out that a nested path is wrong.

winget does not require code signing. It does run the installer through
SmartScreen and a malware scan, and an unsigned PyInstaller exe is the kind of
thing that occasionally trips a heuristic - see windows/SIGNING.md.
{"=" * 68}""")


if __name__ == "__main__":
    main()
