#!/usr/bin/env python3
"""
bump_version.py - set the version everywhere it is written down.

    python tools/bump_version.py 1.2.0
    python tools/bump_version.py --check

The version appears in four files, and three of them fail the build when they
disagree with the first:

    VERSION                     the source of truth
    PeekESP/PeekESP.ino         FW_VERSION - reported over the wire, shown on
                                the settings page, compared by the update check
    windows/peek_version.py     what the app reports
    packaging/aur/PKGBUILD      pkgver, which the AUR shows and builds from

Those guards catch drift, which is worth having - but catching drift at build
time still leaves someone editing four files by hand and finding out about the
fifth from a failed release. This edits all of them.

--check makes no changes and exits non-zero on disagreement, which is what CI
runs.
"""

import argparse
import io
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# (path, regex with one capturing group around the version, human name)
SITES = [
    (REPO / "PeekESP" / "PeekESP.ino",
     re.compile(r'(?m)^(#define\s+FW_VERSION\s+")([^"]+)(")'), "FW_VERSION"),
    (REPO / "windows" / "peek_version.py",
     re.compile(r'(?m)^(__version__\s*=\s*")([^"]+)(")'), "__version__"),
    (REPO / "packaging" / "aur" / "PKGBUILD",
     re.compile(r'(?m)^(pkgver=)([^\s]+)()'), "pkgver"),
]

VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")


def read(p):
    return io.open(p, encoding="utf-8").read()


def write(p, s):
    io.open(p, "w", encoding="utf-8", newline="\n").write(s)


def current():
    f = REPO / "VERSION"
    if not f.exists():
        sys.exit("no VERSION file at the repository root")
    return read(f).strip()


def main():
    ap = argparse.ArgumentParser(description="Set the version in every file that carries it")
    ap.add_argument("version", nargs="?", help="e.g. 1.2.0")
    ap.add_argument("--check", action="store_true",
                    help="report disagreement and exit non-zero, changing nothing")
    a = ap.parse_args()

    if a.check and a.version:
        ap.error("--check takes no version")
    if not a.check and not a.version:
        ap.error("give a version, or --check")

    want = a.version or current()
    if not VERSION_RE.match(want):
        sys.exit(f"'{want}' is not a version - digits and dots, like 1.2.0")

    problems = []
    changes = []

    for path, pattern, name in SITES:
        if not path.exists():
            problems.append(f"{path.relative_to(REPO)} is missing")
            continue
        text = read(path)
        m = pattern.search(text)
        if not m:
            problems.append(f"{path.relative_to(REPO)} has no {name}")
            continue
        found = m.group(2)
        if found == want:
            print(f"  ok       {path.relative_to(REPO)}  {name} = {found}")
            continue
        if a.check:
            problems.append(f"{path.relative_to(REPO)}: {name} is {found}, VERSION says {want}")
        else:
            write(path, pattern.sub(lambda mm: mm.group(1) + want + mm.group(3), text, count=1))
            changes.append(f"  updated  {path.relative_to(REPO)}  {name} {found} -> {want}")

    if not a.check and current() != want:
        write(REPO / "VERSION", want + "\n")
        changes.append(f"  updated  VERSION  {current()} -> {want}")

    for c in changes:
        print(c)

    if problems:
        print()
        for p in problems:
            print("  !! " + p)
        sys.exit(1)

    if a.check:
        print(f"\nall four agree on {want}")
    else:
        print(f"\nversion is now {want} everywhere")
        print("A release still needs: python tools/export_firmware.py, then a tag.")


if __name__ == "__main__":
    main()
