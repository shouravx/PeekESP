#!/usr/bin/env python3
"""
peek_agent_win.py - the Windows counterpart of dietpi/peek-agent.py.

Serves and/or pushes exactly the same JSON, so a Windows PC can drive a
PeekESP display just as a Linux box does:

    { "host": "DESKTOP-1", "cpu_percent": 12.5, "ram_percent": 43.2,
      "storage_percent": 61.0, "cpu_temp_c": -1, "uptime_seconds": 271830,
      "net_rx_kbps": 128.4, "net_tx_kbps": 12.9 }

Standard library only - every reading comes from a Win32 call through
ctypes, so there is nothing to pip install and the compiled .exe stays
small. See build.py to produce a single-file executable.

    Push to a relay:  peek_agent_win.py --push https://.../ingest/me --token ...
    Serve on the LAN: peek_agent_win.py
"""

import ctypes
import ctypes.wintypes as w
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = "0.0.0.0"
PORT = 8080
PATH = "/telemetry"
DISK = os.environ.get("SystemDrive", "C:") + "\\"
SAMPLE_SECONDS = 2.0

# Cloudflare's edge bans the default "Python-urllib/3.x" user agent outright
# with error 1010, before the Worker ever runs - so a push would fail with an
# opaque 403 that no amount of checking tokens would explain. Identify
# ourselves properly instead.
USER_AGENT = "PeekESP-agent/1.0 (+https://github.com/shouravx/PeekESP)"

if os.name != "nt":
    sys.exit("This is the Windows agent. On Linux use dietpi/peek-agent.py.")

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)


# --------------------------------------------------------------------------
#  Win32 structures
# --------------------------------------------------------------------------
class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", w.DWORD), ("dwMemoryLoad", w.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


MAX_INTERFACE_NAME_LEN = 256
MAXLEN_PHYSADDR = 8
MAXLEN_IFDESCR = 256
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_OPER_STATUS_OPERATIONAL = 5


class MIB_IFROW(ctypes.Structure):
    _fields_ = [
        ("wszName", w.WCHAR * MAX_INTERFACE_NAME_LEN),
        ("dwIndex", w.DWORD), ("dwType", w.DWORD), ("dwMtu", w.DWORD),
        ("dwSpeed", w.DWORD), ("dwPhysAddrLen", w.DWORD),
        ("bPhysAddr", w.BYTE * MAXLEN_PHYSADDR),
        ("dwAdminStatus", w.DWORD), ("dwOperStatus", w.DWORD), ("dwLastChange", w.DWORD),
        ("dwInOctets", w.DWORD), ("dwInUcastPkts", w.DWORD), ("dwInNUcastPkts", w.DWORD),
        ("dwInDiscards", w.DWORD), ("dwInErrors", w.DWORD), ("dwInUnknownProtos", w.DWORD),
        ("dwOutOctets", w.DWORD), ("dwOutUcastPkts", w.DWORD), ("dwOutNUcastPkts", w.DWORD),
        ("dwOutDiscards", w.DWORD), ("dwOutErrors", w.DWORD), ("dwOutQLen", w.DWORD),
        ("dwDescrLen", w.DWORD), ("bDescr", ctypes.c_char * MAXLEN_IFDESCR),
    ]


def _filetime_to_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


# --------------------------------------------------------------------------
#  Rate-based readings need two samples, so a background thread keeps a
#  rolling value rather than making every request block for a delta.
# --------------------------------------------------------------------------
_state = {"cpu_percent": 0.0, "net_rx_kbps": 0.0, "net_tx_kbps": 0.0}
_lock = threading.Lock()
# Set once the sampler has produced its first real delta. Without it, the
# first snapshot races the sampler and reports cpu/net as a flat 0.
_primed = threading.Event()


def _cpu_totals():
    """(idle, total) in 100ns ticks. Kernel time already includes idle."""
    idle, kern, user = w.FILETIME(), w.FILETIME(), w.FILETIME()
    if not kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern), ctypes.byref(user)):
        raise ctypes.WinError(ctypes.get_last_error())
    i = _filetime_to_int(idle)
    return i, _filetime_to_int(kern) + _filetime_to_int(user)


def _net_totals():
    """
    (rx, tx) octets summed over real, operational interfaces.

    GetIfTable lists every NDIS *filter* bound to an adapter as its own row -
    a single Realtek NIC typically appears four times (the adapter itself plus
    "WFP Native MAC Layer", "QoS Packet Scheduler" and "WFP 802.3 MAC Layer"),
    every one reporting the same counters. Summing the table naively multiplies
    real throughput by however many filters happen to be installed, which is
    how this first reported ~11 MB/s on a link doing ~2.75.

    Deduplicating by MAC address collapses those clones, because the filter
    rows share the adapter's physical address.
    """
    size = w.ULONG(0)
    # First call sizes the buffer; ERROR_INSUFFICIENT_BUFFER (122) is expected.
    iphlpapi.GetIfTable(None, ctypes.byref(size), False)
    buf = ctypes.create_string_buffer(size.value)
    if iphlpapi.GetIfTable(buf, ctypes.byref(size), False) != 0:
        return 0, 0

    count = ctypes.cast(buf, ctypes.POINTER(w.DWORD))[0]
    rows = ctypes.cast(ctypes.byref(buf, ctypes.sizeof(w.DWORD)),
                       ctypes.POINTER(MIB_IFROW))

    seen = {}
    for i in range(count):
        row = rows[i]
        # Loopback would count our own traffic; tunnels would double-count the
        # very requests this agent is making.
        if row.dwType == IF_TYPE_SOFTWARE_LOOPBACK:
            continue
        # 5 = MIB_IF_OPER_STATUS_OPERATIONAL. Anything else is down, and a
        # disconnected adapter still reports the totals it had when it died.
        if row.dwOperStatus != IF_OPER_STATUS_OPERATIONAL:
            continue
        desc = bytes(row.bDescr[:row.dwDescrLen]).decode("latin-1", "replace").lower()
        if any(k in desc for k in ("loopback", "wireguard", "tailscale", "wintun")):
            continue

        mac = bytes(bytearray(row.bPhysAddr[:row.dwPhysAddrLen]))
        # Virtual interfaces may report no MAC; fall back to the description so
        # they are still counted once each rather than merged together.
        key = mac if mac else desc
        prev = seen.get(key)
        if prev is None or (row.dwInOctets + row.dwOutOctets) > (prev[0] + prev[1]):
            seen[key] = (row.dwInOctets, row.dwOutOctets)

    rx = sum(v[0] for v in seen.values())
    tx = sum(v[1] for v in seen.values())
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
        # GetIfTable's counters are 32-bit and wrap at 4 GB. A negative delta
        # means a wrap (or an adapter reset), so drop that one sample rather
        # than reporting a huge spike.
        d_rx = cur_net[0] - prev_net[0]
        d_tx = cur_net[1] - prev_net[1]
        rx_kbps = d_rx / 1024.0 / elapsed if d_rx >= 0 else 0.0
        tx_kbps = d_tx / 1024.0 / elapsed if d_tx >= 0 else 0.0

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
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not m.ullTotalPhys:
        return 0.0
    return 100.0 * (m.ullTotalPhys - m.ullAvailPhys) / m.ullTotalPhys


DRIVE_FIXED = 3


def _storage():
    """
    (used_percent, total_bytes, free_bytes) across every fixed drive.

    Reporting only %SystemDrive% was wrong on any machine with more than one
    disk: a full 240 GB C: read as "95% full" while 1.3 TB sat free next to it.
    Removable drives, network shares and optical drives are excluded - a USB
    stick appearing and disappearing should not move the gauge.
    """
    total = free = 0
    mask = kernel32.GetLogicalDrives()
    for i in range(26):
        if not (mask >> i) & 1:
            continue
        root = f"{chr(ord('A') + i)}:\\"
        if kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)) != DRIVE_FIXED:
            continue
        avail = ctypes.c_ulonglong(0)
        cap = ctypes.c_ulonglong(0)
        cap_free = ctypes.c_ulonglong(0)
        if kernel32.GetDiskFreeSpaceExW(ctypes.c_wchar_p(root),
                                        ctypes.byref(avail),
                                        ctypes.byref(cap),
                                        ctypes.byref(cap_free)):
            total += cap.value
            free += cap_free.value
    if not total:
        return 0.0, 0, 0
    return 100.0 * (total - free) / total, total, free


# Temperature is cached: the sources below are an HTTP request or a process
# launch, neither of which belongs in a 2-second sampler.
_temp = {"value": -1.0, "at": 0.0, "source": None, "misses": 0}
TEMP_TTL = 20.0

# A machine with no usable source at all backs off rather than launching
# PowerShell every 20 seconds until the end of time.
TEMP_TTL_MAX = 300.0
LHM_PORTS = (8085, 8086)


def _powershell(command, timeout=8):
    """Run one PowerShell command and hand back stdout, or "" if it failed.

    Catches what running a process can actually throw, and nothing else. A
    bare `except Exception` here hid a missing `import subprocess` for the
    whole life of the WMI fallback: every call raised NameError, was swallowed,
    and came back as "this machine has no temperature sensor".
    """
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _temp_from_hardware_monitor():
    """
    LibreHardwareMonitor / OpenHardwareMonitor, if one is running with its web
    server enabled. They ship a kernel driver, which is the only way anything
    on Windows reads a CPU temperature reliably - there is no OS API for it.
    """
    import json as _json
    import urllib.request as _u

    def walk(node, out):
        text = str(node.get("Text", ""))
        value = str(node.get("Value", ""))
        if "°C" in value or value.endswith("C"):
            try:
                out.append((text, float(value.split()[0].replace(",", "."))))
            except (ValueError, IndexError):
                pass
        for kid in node.get("Children", []):
            walk(kid, out)

    for port in LHM_PORTS:
        try:
            with _u.urlopen(f"http://127.0.0.1:{port}/data.json", timeout=1.5) as r:
                data = _json.loads(r.read())
        except Exception:
            continue
        found = []
        walk(data, found)
        if not found:
            continue
        # Prefer a package/average sensor over an individual core.
        for want in ("package", "cpu average", "core average", "tctl", "cpu"):
            for text, val in found:
                if want in text.lower() and 0 < val < 125:
                    return val
        for _text, val in found:
            if 0 < val < 125:
                return val
    return None


def _temp_from_perf_zone():
    """
    The performance counter over the ACPI thermal zones. This is the one that
    works on an ordinary machine: no driver to install, no administrator, and
    present on Windows 8 and later.

    What it is NOT is a CPU die temperature. An ACPI zone is wherever the board
    vendor put a sensor - often the chassis or near the chipset - so it reads
    well below what LibreHardwareMonitor would report for the same machine at
    the same moment. That is why it sits second: a real CPU reading is better,
    but a real chassis reading beats "--".
    """
    out = _powershell(
        "$z = Get-CimInstance -ClassName "
        "Win32_PerfFormattedData_Counters_ThermalZoneInformation "
        "-ErrorAction Stop; "
        "($z | Measure-Object -Property HighPrecisionTemperature -Maximum).Maximum")
    try:
        # Tenths of a Kelvin. The hottest zone, because a board with several
        # is telling you about several different places and the warmest one is
        # the one worth knowing about.
        c = float(out) / 10.0 - 273.15
    except ValueError:
        return None
    return c if 0 < c < 125 else None


def _temp_from_wmi():
    """ACPI thermal zone read directly. Needs administrator - unelevated it
    returns Access denied - and plenty of boards do not implement the class."""
    out = _powershell(
        "(Get-CimInstance -Namespace root/wmi -ClassName "
        "MSAcpi_ThermalZoneTemperature -ErrorAction Stop | "
        "Select-Object -First 1).CurrentTemperature")
    if not out.isdigit():
        return None
    c = int(out) / 10.0 - 273.15
    return c if 0 < c < 125 else None


# Best first. The two PowerShell ones cost a process launch each, which is why
# the working source is remembered rather than rediscovered every time.
_TEMP_SOURCES = (
    ("hwmonitor", _temp_from_hardware_monitor),
    ("perfzone", _temp_from_perf_zone),
    ("acpi", _temp_from_wmi),
)


def _cpu_temp_c():
    now = time.monotonic()

    # A machine that has never produced a reading is asked less and less often,
    # up to once every five minutes. One that has a working source is asked at
    # the normal interval, because that answer is cheap and worth having fresh.
    ttl = TEMP_TTL
    if _temp["source"] is None and _temp["misses"]:
        ttl = min(TEMP_TTL_MAX, TEMP_TTL * (1 + _temp["misses"]))
    if now - _temp["at"] < ttl:
        return _temp["value"]
    _temp["at"] = now

    known = _temp["source"]
    order = [s for s in _TEMP_SOURCES if s[0] == known] if known else list(_TEMP_SOURCES)

    for name, fn in order:
        val = fn()
        if val is not None:
            _temp.update(value=val, source=name, misses=0)
            return val

    # The remembered source stopped answering - the monitor was closed, say.
    # Forgetting it means the next tick tries everything again instead of
    # reporting -1 forever because one thing went away.
    _temp.update(value=-1.0, source=None, misses=_temp["misses"] + 1)
    return -1.0


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong)]


BATTERY_FLAG_CHARGING = 8
BATTERY_FLAG_NO_BATTERY = 128
UNKNOWN_BYTE = 255
UNKNOWN_DWORD = 0xFFFFFFFF


def _battery():
    """(percent, charging, on_ac, minutes_left) for the monitored machine.

    GetSystemPowerStatus rather than WMI's Win32_Battery: it is a plain kernel
    call, needs no administrator, and does not cost a process launch - so it
    can run on every snapshot instead of behind a cache. A desktop reports no
    battery and the percent comes back as -1, which the display renders as
    nothing at all rather than as a flat cell.
    """
    st = SYSTEM_POWER_STATUS()
    if not kernel32.GetSystemPowerStatus(ctypes.byref(st)):
        return -1, False, False, -1

    on_ac = st.ACLineStatus == 1
    if st.BatteryFlag & BATTERY_FLAG_NO_BATTERY or st.BatteryLifePercent == UNKNOWN_BYTE:
        return -1, False, on_ac, -1

    # Charging is its own flag, not "plugged in". A laptop sitting on AC at
    # 100 % is plugged in and not charging, and calling that "charging" is how
    # a battery readout stops being believed.
    charging = bool(st.BatteryFlag & BATTERY_FLAG_CHARGING)
    minutes = (-1 if st.BatteryLifeTime == UNKNOWN_DWORD
               else int(st.BatteryLifeTime // 60))
    return min(100, st.BatteryLifePercent), charging, on_ac, minutes


def _uptime_seconds():
    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    return int(kernel32.GetTickCount64() / 1000)


def snapshot():
    with _lock:
        rolling = dict(_state)
    pct, total, free = _storage()
    bat_pct, bat_charging, bat_ac, bat_minutes = _battery()
    return {
        "host": socket.gethostname()[:19],       # the sketch stores 20 bytes
        "cpu_percent": round(rolling["cpu_percent"], 1),
        "ram_percent": round(_ram_percent(), 1),
        "storage_percent": round(pct, 1),
        "storage_total_gb": round(total / (1024 ** 3), 1),
        "storage_free_gb": round(free / (1024 ** 3), 1),
        "cpu_temp_c": round(_cpu_temp_c(), 1),
        "battery_percent": bat_pct,          # -1 on a machine with no battery
        "battery_charging": bat_charging,
        "battery_ac": bat_ac,
        "battery_minutes": bat_minutes,      # -1 when Windows will not estimate
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
        pass          # a poll every 5 seconds would otherwise flood the console


def push_loop(url, token, interval):
    import urllib.error
    import urllib.request

    fails = 0
    while True:
        body = json.dumps(snapshot()).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + token,
                     "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            if fails:
                print("push recovered after %d failure(s)" % fails, flush=True)
            fails = 0
        except urllib.error.HTTPError as e:
            print("push rejected: HTTP %s %s" % (e.code, e.reason), flush=True)
            fails += 1
        except Exception as e:
            fails += 1
            if fails <= 3 or fails % 20 == 0:
                print("push failed (%d): %s" % (fails, e), flush=True)
        time.sleep(interval)


def main():
    global PORT
    import argparse

    ap = argparse.ArgumentParser(description="PeekESP telemetry agent for Windows")
    ap.add_argument("--push", metavar="URL",
                    help="POST telemetry to this relay /ingest URL")
    ap.add_argument("--token", default=os.environ.get("PEEK_PUSH_TOKEN", ""),
                    help="bearer token for --push (or set PEEK_PUSH_TOKEN)")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="seconds between pushes (default: 5)")
    ap.add_argument("--no-serve", action="store_true",
                    help="do not listen on :%d, push only" % PORT)
    ap.add_argument("--once", action="store_true",
                    help="print one reading and exit (for checking it works)")
    ap.add_argument("--config", action="store_true",
                    help="take settings from %%APPDATA%%\\PeekESP\\config.json")
    args = ap.parse_args()

    # --config lets a service or scheduled task run with no secrets on its
    # command line, where they would otherwise be visible to every process
    # listing on the machine. Explicit flags still win over the file.
    if args.config:
        import peek_config
        cfg = peek_config.load()
        if cfg.get("_error"):
            print("config: " + cfg["_error"], flush=True)
        if not args.push and cfg["mode"] in ("push", "both"):
            args.push = cfg["relay_url"]
        if not args.token:
            args.token = cfg["token"]
        if args.interval == 5.0:
            args.interval = cfg["interval"]
        if cfg["mode"] == "push":
            args.no_serve = True
        PORT = cfg["serve_port"]

    threading.Thread(target=_sampler, daemon=True).start()
    if not _primed.wait(SAMPLE_SECONDS * 3):   # wait for a real delta, not a guess
        print("warning: first sample did not arrive; rates may read 0", flush=True)

    if args.once:
        print(json.dumps(snapshot(), indent=2))
        return

    if args.push and not args.token:
        ap.error("--push needs --token (or the PEEK_PUSH_TOKEN environment variable)")

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
