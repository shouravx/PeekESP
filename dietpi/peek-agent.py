#!/usr/bin/env python3
"""
peek-agent.py - the telemetry endpoint PeekESP polls.

Serves the JSON that PeekESP.ino expects on http://0.0.0.0:8080/telemetry.
Pure standard library, reads /proc directly - no psutil, no pip, nothing to
install on a DietPi.

    { "host": "dietpi", "cpu_percent": 12.5, "ram_percent": 43.2,
      "storage_percent": 61.0, "storage_total_gb": 117.9,
      "storage_free_gb": 46.0, "cpu_temp_c": 48.3, "battery_percent": 78,
      "battery_charging": true, "battery_ac": true, "battery_minutes": 134,
      "uptime_seconds": 271830, "net_rx_kbps": 128.4, "net_tx_kbps": 12.9 }

Install it:  curl -fsSL https://raw.githubusercontent.com/shouravx/PeekESP/\\
             main/dietpi/install.sh | sudo sh
Pair it:     python3 peek-agent.py --pair-code K7M2-P4QX-9R --no-serve
Serve it:    python3 peek-agent.py          # JSON on :8080, for a LAN device

See dietpi/README.md for the rest.
"""

import glob
import hashlib
import json
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = "0.0.0.0"          # must NOT be 127.0.0.1, or the tunnel cannot reach it
PORT = 8080
PATH = "/telemetry"
SAMPLE_SECONDS = 2.0      # how often the background sampler takes a reading

# Mounted over the network. The bytes are real, but they are not this machine's
# storage, and an unreachable server makes statvfs() hang rather than fail.
NET_FSTYPES = frozenset((
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "sshfs", "ncpfs", "afs", "9p",
))

# A thermal zone is whatever the board vendor decided to expose: on a Pi it is
# the SoC, on a laptop it can be the battery bay or the wifi card. Prefer the
# zones that are actually a CPU before falling back to "the first one that
# reads above zero".
CPU_ZONE_HINTS = ("x86_pkg_temp", "coretemp", "cpu-thermal", "cpu_thermal",
                  "soc_thermal", "k10temp", "acpitz")

# Cloudflare's edge bans the default "Python-urllib/3.x" user agent outright
# with error 1010, before the Worker ever runs - so a push would fail with an
# opaque 403 that no amount of checking tokens would explain. Identify
# ourselves properly instead.
USER_AGENT = "PeekESP-agent/1.0 (+https://github.com/shouravx/PeekESP)"

# The same relay the firmware ships pointing at, so a pairing code alone is
# enough on both sides and there is no URL to copy anywhere.
RELAY_BASE = "https://peek-relay.peekesp.workers.dev"


# --------------------------------------------------------------------------
#  Pairing: the device shows a code, you type it here, and both ends derive
#  the same stream and tokens locally. Nothing but the code ever moves between
#  them, and the relay never sees it - which is why this needs no secrets
#  configured on the Worker at all.
#
#      stream = SHA-256("peek-stream:" + CODE)  first 16 hex
#      push   = SHA-256("peek-push:"   + CODE)  first 48 hex
#
#  Kept byte-identical to windows/peek_pair.py, cloudflare/src/index.js and the
#  firmware's C++. A drift in any one of them would have this machine pushing
#  to a stream the device never reads, with every request looking perfectly
#  valid from both ends.
# --------------------------------------------------------------------------
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIR_CODE_LEN = 10


def pair_normalise(code):
    """Dashes and case are decoration: "k7m2-p4qx-9r" is "K7M2P4QX9R"."""
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def pair_derive(code):
    c = pair_normalise(code)
    if len(c) != PAIR_CODE_LEN or any(ch not in PAIR_ALPHABET for ch in c):
        raise ValueError(
            "pairing code must be %d characters from %s "
            "(dashes and case are ignored)" % (PAIR_CODE_LEN, PAIR_ALPHABET))

    def h(prefix, n):
        return hashlib.sha256((prefix + c).encode("ascii")).hexdigest()[:n]

    return {"code": c, "stream": h("peek-stream:", 16), "push": h("peek-push:", 48)}


# --------------------------------------------------------------------------
#  Rate-based readings need two samples, so a background thread keeps a
#  rolling value rather than making every HTTP request block for a delta.
# --------------------------------------------------------------------------
_state = {"cpu_percent": 0.0, "net_rx_kbps": 0.0, "net_tx_kbps": 0.0}
_lock = threading.Lock()
# Set once the sampler has produced its first real delta. Without it, the
# first snapshot races the sampler and reports cpu/net as a flat 0.
_primed = threading.Event()


def _cpu_totals():
    with open("/proc/stat", "r") as fh:
        parts = [float(x) for x in fh.readline().split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0.0)   # idle + iowait
    return idle, sum(parts)


def _net_totals():
    rx = tx = 0
    with open("/proc/net/dev", "r") as fh:
        for line in fh.readlines()[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            # Skip loopback and the tunnels themselves, otherwise the ESP32's
            # own polling traffic shows up in the numbers it is displaying.
            if name == "lo" or name.startswith(("wg", "tailscale", "docker", "veth")):
                continue
            fields = rest.split()
            rx += int(fields[0])
            tx += int(fields[8])
    return rx, tx


def _sampler():
    prev_cpu = _cpu_totals()
    prev_net = _net_totals()
    prev_t = time.monotonic()

    while True:
        time.sleep(SAMPLE_SECONDS)
        now = time.monotonic()
        elapsed = max(now - prev_t, 1e-6)

        cur_cpu = _cpu_totals()
        d_idle = cur_cpu[0] - prev_cpu[0]
        d_total = cur_cpu[1] - prev_cpu[1]
        cpu = 100.0 * (1.0 - d_idle / d_total) if d_total > 0 else 0.0

        cur_net = _net_totals()
        rx_kbps = (cur_net[0] - prev_net[0]) / 1024.0 / elapsed
        tx_kbps = (cur_net[1] - prev_net[1]) / 1024.0 / elapsed

        with _lock:
            _state["cpu_percent"] = max(0.0, min(100.0, cpu))
            _state["net_rx_kbps"] = max(0.0, rx_kbps)
            _state["net_tx_kbps"] = max(0.0, tx_kbps)

        _primed.set()
        prev_cpu, prev_net, prev_t = cur_cpu, cur_net, now


# --------------------------------------------------------------------------
#  Point-in-time readings
# --------------------------------------------------------------------------
def _ram_percent():
    info = {}
    with open("/proc/meminfo", "r") as fh:
        for line in fh:
            key, _, value = line.partition(":")
            info[key] = float(value.split()[0])
    total = info.get("MemTotal", 0.0)
    if total <= 0:
        return 0.0
    # MemAvailable is the honest number: it accounts for reclaimable cache,
    # which is why `free -m` and a naive MemFree calculation disagree so badly.
    available = info.get("MemAvailable", info.get("MemFree", 0.0))
    return 100.0 * (total - available) / total


def _storage():
    """(used_percent, total_bytes, free_bytes) across every real filesystem.

    Reporting only `/` understates a machine whose data lives on a second disk
    - a DietPi with a USB drive would show a nearly full 4 GB card and never
    mention the 2 TB attached to it.

    The numbers follow `df`: capacity is what a normal user can occupy, so the
    5 % reserved for root is excluded from both the total and the free figure.
    That keeps the percentage, the total and the free space agreeing with each
    other, and with the tool anyone would check them against.
    """
    total = free = 0
    seen = set()
    try:
        with open("/proc/mounts", "r") as fh:
            mounts = fh.readlines()
    except OSError:
        return 0.0, 0, 0

    for line in mounts:
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mount, fstype = fields[0], fields[1], fields[2]
        # A real filesystem is backed by a block device. Everything else is
        # RAM (tmpfs), a kernel view (proc, sysfs), or another filesystem seen
        # a second time (overlay) - counting those would invent capacity that
        # does not exist.
        if not device.startswith("/dev/") or fstype in NET_FSTYPES:
            continue
        # A bind mount and its source share a device. Counting both would
        # double the disk.
        if device in seen:
            continue
        seen.add(device)
        # /proc/mounts octal-escapes the characters that would otherwise break
        # its own field separation - a mount point named "My Drive" arrives as
        # "My\040Drive" and would not exist under that name. unicode_escape
        # decodes exactly the octal form the kernel writes.
        try:
            mount = mount.encode("latin-1", "backslashreplace").decode("unicode_escape")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
        try:
            st = os.statvfs(mount)
        except OSError:
            continue                     # unmounted underneath us, or no perms
        used = st.f_blocks - st.f_bfree
        avail = st.f_bavail
        total += (used + avail) * st.f_frsize
        free += avail * st.f_frsize

    if not total:
        return 0.0, 0, 0
    return 100.0 * (total - free) / total, total, free


def _read_zone(path):
    try:
        with open(path, "r") as fh:
            milli = float(fh.read().strip())
    except (OSError, ValueError):
        return None
    # Some zones sit at 0 until something enables them, and a few report a
    # sentinel far outside anything a chip survives.
    return milli / 1000.0 if 0 < milli < 150000 else None


def _cpu_temp_c():
    fallback = None

    for zone in sorted(glob.glob("/sys/class/thermal/thermal_zone*")):
        value = _read_zone(zone + "/temp")
        if value is None:
            continue
        if fallback is None:
            fallback = value
        try:
            with open(zone + "/type", "r") as fh:
                kind = fh.read().strip().lower()
        except OSError:
            continue
        if any(hint in kind for hint in CPU_ZONE_HINTS):
            return value

    if fallback is not None:
        return fallback
    return -1.0                          # the sketch renders "--" for this


def _sysfs(path):
    try:
        with open(path, "r") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _battery():
    """(percent, charging, on_ac, minutes_left) for this machine.

    A Pi has neither a battery nor a mains supply that announces itself, so
    this reports -1 and the display simply leaves it out. On a laptop it is all
    in /sys/class/power_supply.
    """
    on_ac = any(_sysfs(p) == "1"
                for pat in ("AC*/online", "ADP*/online", "ACAD/online")
                for p in glob.glob("/sys/class/power_supply/" + pat))

    bats = sorted(glob.glob("/sys/class/power_supply/BAT*"))
    if not bats:
        return -1, False, on_ac, -1

    b = bats[0]
    cap = _sysfs(b + "/capacity")
    if not cap.isdigit():
        return -1, False, on_ac, -1

    charging = _sysfs(b + "/status").lower() == "charging"

    # Energy pairs with power (µWh over µW) and charge pairs with current
    # (µAh over µA). Crossing them - energy over current - is dimensionally
    # nonsense and produces a confident, wrong number of hours.
    minutes = -1
    for amount, rate in (("energy_now", "power_now"), ("charge_now", "current_now")):
        a, r = _sysfs(b + "/" + amount), _sysfs(b + "/" + rate)
        if a.isdigit() and r.isdigit() and int(r) > 0:
            minutes = int(int(a) * 60 // int(r))
            break

    return min(100, int(cap)), charging, on_ac, minutes


def _uptime_seconds():
    with open("/proc/uptime", "r") as fh:
        return int(float(fh.readline().split()[0]))


def snapshot():
    with _lock:
        rolling = dict(_state)
    used_pct, total, free = _storage()
    bat_pct, bat_charging, bat_ac, bat_minutes = _battery()
    return {
        "host": socket.gethostname()[:19],       # the sketch stores 20 bytes
        "cpu_percent": round(rolling["cpu_percent"], 1),
        "ram_percent": round(_ram_percent(), 1),
        "storage_percent": round(used_pct, 1),
        "storage_total_gb": round(total / (1024 ** 3), 1),
        "storage_free_gb": round(free / (1024 ** 3), 1),
        "cpu_temp_c": round(_cpu_temp_c(), 1),
        "battery_percent": bat_pct,          # -1 on a machine with no battery
        "battery_charging": bat_charging,
        "battery_ac": bat_ac,
        "battery_minutes": bat_minutes,      # -1 when the rate is unknown
        "uptime_seconds": _uptime_seconds(),
        "net_rx_kbps": round(rolling["net_rx_kbps"], 1),
        "net_tx_kbps": round(rolling["net_tx_kbps"], 1),
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        if self.path.split("?")[0] not in (PATH, "/"):
            self.send_error(404)
            return
        body = json.dumps(snapshot()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass          # a poll every 5 seconds would otherwise flood the journal


# --------------------------------------------------------------------------
#  Push mode - for when the ESP32 cannot reach this machine at all.
#
#  Serving works only if something can open a connection TO this box: a port
#  forward, or a WireGuard tunnel terminating here. Behind CGNAT, or on a
#  network you do not administer, neither is possible. Pushing inverts that:
#  this side dials out to a Cloudflare Worker, the ESP32 dials out to the same
#  Worker, and nothing needs an inbound port.
# --------------------------------------------------------------------------
def push_loop(url, token, interval):
    import urllib.error
    import urllib.request

    fails = 0
    while True:
        body = json.dumps(snapshot()).encode()
        req = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            if fails:
                print("push recovered after %d failure(s)" % fails, flush=True)
            fails = 0
        except urllib.error.HTTPError as e:
            # 401 will never fix itself, so say plainly what is wrong rather
            # than burying it in a retry count.
            print("push rejected: HTTP %s %s" % (e.code, e.reason), flush=True)
            fails += 1
        except Exception as e:
            fails += 1
            if fails <= 3 or fails % 20 == 0:
                print("push failed (%d): %s" % (fails, e), flush=True)
        time.sleep(interval)


# Settings arrive from the environment so the systemd unit can stay fixed:
# changing the poll interval rewrites one line of a config file rather than
# regenerating and reloading a unit.
def _env_flag(name):
    return (os.environ.get(name, "") or "").strip().lower() in ("1", "true", "yes", "on")


def _env_float(name, fallback):
    try:
        return float(os.environ.get(name, "") or fallback)
    except ValueError:
        return fallback


def main():
    import argparse

    ap = argparse.ArgumentParser(description="PeekESP telemetry agent")
    ap.add_argument("--pair-code", metavar="CODE",
                    default=os.environ.get("PEEK_PAIR_CODE", ""),
                    help="the code shown on the device; derives everything else "
                         "(or set PEEK_PAIR_CODE)")
    ap.add_argument("--relay-base", metavar="URL",
                    default=os.environ.get("PEEK_RELAY_BASE") or RELAY_BASE,
                    # %(default)s, not the constant: this way --help shows what
                    # the service will actually use, environment included,
                    # rather than what it would use if nothing were configured.
                    help="relay to pair against (or set PEEK_RELAY_BASE; "
                         "default: %(default)s)")
    ap.add_argument("--push", metavar="URL",
                    help="POST telemetry to this Cloudflare Worker /ingest URL")
    ap.add_argument("--token", default=os.environ.get("PEEK_PUSH_TOKEN", ""),
                    help="bearer token for --push (or set PEEK_PUSH_TOKEN)")
    ap.add_argument("--interval", type=float, default=_env_float("PEEK_INTERVAL", 5.0),
                    help="seconds between pushes (or set PEEK_INTERVAL; "
                         "default: %(default)s)")
    ap.add_argument("--serve", action="store_true", default=_env_flag("PEEK_SERVE"),
                    help="also listen on :%d while pushing (or set PEEK_SERVE=1)" % PORT)
    ap.add_argument("--no-serve", action="store_true",
                    help="do not listen on :%d, push only" % PORT)
    ap.add_argument("--once", action="store_true",
                    help="print one reading as JSON and exit - what this "
                         "machine would report")
    ap.add_argument("--verify", action="store_true",
                    help="check the pairing code, print the stream it derives, "
                         "and exit without pushing anything")
    args = ap.parse_args()

    if args.verify:
        if not args.pair_code:
            ap.error("--verify needs --pair-code")
        try:
            print(pair_derive(args.pair_code)["stream"])
        except ValueError as e:
            ap.error(str(e))
        return

    if args.pair_code:
        try:
            d = pair_derive(args.pair_code)
        except ValueError as e:
            ap.error(str(e))
        args.push = "%s/ingest/%s" % (args.relay_base.rstrip("/"), d["stream"])
        args.token = d["push"]
        # The stream, not the code: the code is the secret and the journal is
        # not a secret. The stream id is what you compare against the device's
        # own "[pair] code X -> stream Y" line when they will not meet.
        print("paired to stream %s" % d["stream"], flush=True)

    if args.push and not args.token:
        ap.error("--push needs --token (or the PEEK_PUSH_TOKEN environment variable)")

    threading.Thread(target=_sampler, daemon=True).start()
    if not _primed.wait(SAMPLE_SECONDS * 3):   # wait for a real delta, not a guess
        print("warning: first sample did not arrive; rates may read 0", flush=True)

    # Before any of the serve/push argument checks: "show me what this machine
    # reports" is a question about this machine, not about where it sends it.
    if args.once:
        print(json.dumps(snapshot(), indent=2))
        return

    # Listening is opt-in as soon as something is being pushed. Opening
    # 0.0.0.0:8080 as a side effect of pushing is a port on the LAN that nobody
    # asked for; --serve turns it back on for a device that polls directly.
    serving = args.serve or not args.push
    if args.no_serve:
        serving = False
    if not args.push and not serving:
        ap.error("--no-serve with nothing to push to would do nothing")

    if args.push:
        print("pushing to %s every %gs" % (args.push, args.interval), flush=True)
        if not serving:
            push_loop(args.push, args.token, args.interval)
            return
        threading.Thread(target=push_loop,
                         args=(args.push, args.token, args.interval),
                         daemon=True).start()

    print("peek-agent on http://%s:%d%s" % (BIND, PORT, PATH), flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
