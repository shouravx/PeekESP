#!/usr/bin/env python3
"""
peek-agent.py - the telemetry endpoint PeekESP polls.

Serves the JSON that PeekESP.ino expects on http://0.0.0.0:8080/telemetry.
Pure standard library, reads /proc directly - no psutil, no pip, nothing to
install on a DietPi.

    { "host": "dietpi", "cpu_percent": 12.5, "ram_percent": 43.2,
      "storage_percent": 61.0, "cpu_temp_c": 48.3, "uptime_seconds": 271830,
      "net_rx_kbps": 128.4, "net_tx_kbps": 12.9 }

Run it:      python3 peek-agent.py
Install it:  see the systemd unit in the repository README.
"""

import glob
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = "0.0.0.0"          # must NOT be 127.0.0.1, or the tunnel cannot reach it
PORT = 8080
PATH = "/telemetry"
DISK_MOUNT = "/"
SAMPLE_SECONDS = 2.0      # how often the background sampler takes a reading

# Cloudflare's edge bans the default "Python-urllib/3.x" user agent outright
# with error 1010, before the Worker ever runs - so a push would fail with an
# opaque 403 that no amount of checking tokens would explain. Identify
# ourselves properly instead.
USER_AGENT = "PeekESP-agent/1.0 (+https://github.com/shouravx/PeekESP)"


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


def _storage_percent():
    st = os.statvfs(DISK_MOUNT)
    used = st.f_blocks - st.f_bfree
    total = used + st.f_bavail          # matches what `df` reports
    return 100.0 * used / total if total else 0.0


def _cpu_temp_c():
    for path in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        try:
            with open(path, "r") as fh:
                milli = float(fh.read().strip())
            if milli > 0:
                return milli / 1000.0
        except (OSError, ValueError):
            continue
    return -1.0                          # the sketch renders "--" for this


def _uptime_seconds():
    with open("/proc/uptime", "r") as fh:
        return int(float(fh.readline().split()[0]))


def snapshot():
    with _lock:
        rolling = dict(_state)
    return {
        "host": socket.gethostname()[:19],       # the sketch stores 20 bytes
        "cpu_percent": round(rolling["cpu_percent"], 1),
        "ram_percent": round(_ram_percent(), 1),
        "storage_percent": round(_storage_percent(), 1),
        "cpu_temp_c": round(_cpu_temp_c(), 1),
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


def main():
    import argparse

    ap = argparse.ArgumentParser(description="PeekESP telemetry agent")
    ap.add_argument("--push", metavar="URL",
                    help="POST telemetry to this Cloudflare Worker /ingest URL")
    ap.add_argument("--token", default=os.environ.get("PEEK_PUSH_TOKEN", ""),
                    help="bearer token for --push (or set PEEK_PUSH_TOKEN)")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="seconds between pushes (default: 5)")
    ap.add_argument("--no-serve", action="store_true",
                    help="do not listen on :%d, push only" % PORT)
    args = ap.parse_args()

    if args.push and not args.token:
        ap.error("--push needs --token (or the PEEK_PUSH_TOKEN environment variable)")

    threading.Thread(target=_sampler, daemon=True).start()
    if not _primed.wait(SAMPLE_SECONDS * 3):   # wait for a real delta, not a guess
        print("warning: first sample did not arrive; rates may read 0", flush=True)

    if args.push:
        print("pushing to %s every %gs" % (args.push, args.interval), flush=True)
        if args.no_serve:
            push_loop(args.push, args.token, args.interval)
            return
        threading.Thread(target=push_loop,
                         args=(args.push, args.token, args.interval),
                         daemon=True).start()
    elif args.no_serve:
        ap.error("--no-serve with no --push would do nothing")

    print("peek-agent on http://%s:%d%s" % (BIND, PORT, PATH), flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
