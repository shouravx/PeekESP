#!/usr/bin/env python3
"""
sign.py - Authenticode-sign the built executables.

    python sign.py --thumbprint 8A3F...      a certificate in your cert store
    python sign.py --pfx code.pfx            a .pfx file (password from env)
    python sign.py --azure-metadata meta.json --azure-dlib <path to dlib>
    python sign.py --verify                  check what is already signed

Every mode timestamps. A signature without a timestamp stops verifying the day
the certificate expires, which for a code-signing certificate is one to three
years - so an unsigned-looking binary appears on machines that were happily
running it the week before, with nothing having changed.

--------------------------------------------------------------------------
WHAT SIGNING ACTUALLY BUYS, AND WHAT IT DOES NOT
--------------------------------------------------------------------------
A self-signed certificate - MakeCert, Cert2SPC, New-SelfSignedCertificate -
does NOT stop SmartScreen on anyone else's computer. It is a test certificate:
useful for proving this script works and for machines where you have installed
your own root, and worth nothing on a stranger's PC. The Microsoft article
listing MakeCert and Cert2SPC says "for testing purposes only" against both,
and both are deprecated besides.

To actually change what another PC does you need a certificate from a public
CA, and since June 2023 the CA/Browser Forum requires the private key to live
on FIPS 140-2 Level 2 hardware - so the cheap file-based certificates that
used to exist are gone. See SIGNING.md for what the real options cost.

winget does NOT require any of this. An unsigned build is publishable.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"
TARGETS = ["PeekESP.exe", "peek-agent.exe"]

# RFC 3161. Sectigo is the fallback because DigiCert's timestamper has had
# multi-hour outages, and a failed timestamp fails the whole signature.
TIMESTAMP_URLS = [
    "http://timestamp.digicert.com",
    "http://timestamp.sectigo.com",
]

if os.name != "nt":
    sys.exit("sign.py drives signtool.exe and must run on Windows.")


def find_signtool():
    """
    signtool lives in a versioned Windows SDK directory, and there is usually
    more than one. Newest wins; x64 preferred over x86 only because it is the
    one that exists on every modern install.
    """
    if os.environ.get("SIGNTOOL"):
        p = Path(os.environ["SIGNTOOL"])
        if p.exists():
            return p
        sys.exit(f"SIGNTOOL is set to {p}, which does not exist")

    found = []
    for root in (Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
                 Path(os.environ.get("ProgramFiles", r"C:\Program Files"))):
        kits = root / "Windows Kits" / "10" / "bin"
        if not kits.is_dir():
            continue
        for ver in kits.iterdir():
            for arch in ("x64", "x86"):
                exe = ver / arch / "signtool.exe"
                if exe.exists():
                    found.append((ver.name, arch, exe))
    if not found:
        sys.exit(
            "signtool.exe not found.\n"
            "It ships with the Windows SDK:\n"
            "  winget install Microsoft.WindowsSDK\n"
            "or point at one you already have:  set SIGNTOOL=C:\\path\\signtool.exe")

    found.sort(key=lambda t: ([int(x) for x in t[0].split(".") if x.isdigit()],
                              t[1] == "x64"))
    return found[-1][2]


def run(cmd, quiet=False):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if not quiet or r.returncode != 0:
        out = (r.stdout or "") + (r.stderr or "")
        for line in out.splitlines():
            if line.strip():
                print("   " + line.rstrip())
    return r.returncode


def sign_one(signtool, exe, base_args):
    """Sign one file, trying each timestamp server before giving up.

    Timestamping is the step that fails for reasons that have nothing to do
    with you, so it is the only part worth retrying - and worth retrying
    against a different server rather than the same one.
    """
    for i, ts in enumerate(TIMESTAMP_URLS):
        cmd = [str(signtool), "sign", "/fd", "SHA256",
               "/tr", ts, "/td", "SHA256"] + base_args + [str(exe)]
        print(f"\n=== signing {exe.name} (timestamp: {ts}) ===")
        if run(cmd) == 0:
            return True
        if i + 1 < len(TIMESTAMP_URLS):
            print("   timestamp server failed; trying the next one")
    return False


def verify(signtool, exe):
    """
    /pa uses the Authenticode policy - the one Windows itself applies when
    deciding whether to run something - rather than the default driver policy,
    which would reject a perfectly good application signature.
    """
    print(f"\n=== verifying {exe.name} ===")
    return run([str(signtool), "verify", "/pa", "/v", str(exe)]) == 0


def main():
    ap = argparse.ArgumentParser(
        description="Authenticode-sign the built PeekESP executables",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="A self-signed certificate will not silence SmartScreen on "
               "anyone else's machine. See SIGNING.md.")
    ap.add_argument("--thumbprint", help="SHA-1 thumbprint of a certificate in "
                                         "the Windows certificate store")
    ap.add_argument("--pfx", help="path to a .pfx / .p12 certificate file")
    ap.add_argument("--password-env", default="PEEK_SIGN_PASSWORD",
                    help="environment variable holding the .pfx password "
                         "(default: %(default)s). Never pass it on the command "
                         "line: it lands in your shell history and in the "
                         "process list, where any local user can read it.")
    ap.add_argument("--azure-dlib", help="Azure.CodeSigning.Dlib.dll, for "
                                         "Azure Trusted Signing")
    ap.add_argument("--azure-metadata", help="Trusted Signing metadata json")
    ap.add_argument("--verify", action="store_true",
                    help="only report on signatures already present")
    ap.add_argument("--files", nargs="*", help="override which files to sign")
    a = ap.parse_args()

    signtool = find_signtool()
    print(f"signtool  {signtool}")

    names = a.files or TARGETS
    exes = [DIST / n if not Path(n).is_absolute() else Path(n) for n in names]
    missing = [e for e in exes if not e.exists()]
    if missing:
        sys.exit("not built yet: " + ", ".join(e.name for e in missing)
                 + "\n  python build.py")

    if a.verify:
        ok = all(verify(signtool, e) for e in exes)
        if not ok:
            print("\nUnsigned, or signed by a certificate this machine does not "
                  "trust.\nA self-signed certificate reports exactly this on "
                  "every machine but the one that made it.")
        return 0 if ok else 1

    # --- how to sign -------------------------------------------------------
    if a.azure_dlib or a.azure_metadata:
        if not (a.azure_dlib and a.azure_metadata):
            ap.error("--azure-dlib and --azure-metadata go together")
        base = ["/v", "/dlib", a.azure_dlib, "/dmdf", a.azure_metadata]
    elif a.thumbprint:
        base = ["/sha1", a.thumbprint.replace(" ", "")]
    elif a.pfx:
        pfx = Path(a.pfx)
        if not pfx.exists():
            sys.exit(f"no such certificate file: {pfx}")
        base = ["/f", str(pfx)]
        pw = os.environ.get(a.password_env, "")
        if pw:
            base += ["/p", pw]
        else:
            print(f"note: {a.password_env} is not set; signtool will prompt if "
                  "the certificate needs a password")
    else:
        ap.error("choose one: --thumbprint, --pfx, or the two --azure- options"
                 "\n  (nothing to sign with is not the same as nothing to do)")

    failed = [e.name for e in exes if not sign_one(signtool, e, base)]
    if failed:
        sys.exit("\nsigning failed for: " + ", ".join(failed))

    print("\n" + "=" * 52)
    ok = all(verify(signtool, e) for e in exes)
    print("=" * 52)
    if not ok:
        print("""
Signed, but this machine does not trust the result. That is expected and
harmless for a self-signed certificate, and a real problem for a purchased
one - in which case the intermediate certificate is usually missing. Ask the
CA for their chain bundle.""")
        return 1
    print("\nSigned and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
