"""End-to-end pairing against the DEPLOYED Worker.

Plays both roles: the PC pushing to /ingest/<stream> and the device polling
/telemetry/<stream>, using only values derived from a freshly generated
pairing code. Nothing is configured on the Worker beforehand.
"""
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, r"D:\GITHUB\PeekESP\windows")
import peek_pair as pair

BASE = "https://peek-relay.peekesp.workers.dev"
UA = "PeekESP-agent/1.0 (+https://github.com/shouravx/PeekESP)"

res = []


def call(method, url, token=None, body=None, timeout=20):
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={k: v for k, v in {
            "User-Agent": UA,
            "Content-Type": "application/json" if body is not None else None,
            "Authorization": ("Bearer " + token) if token else None,
        }.items() if v})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return 0, {"error": str(e)}


def check(name, got, want, extra=""):
    ok = got == want
    res.append((f"{name}  -> {got}" + (f"  {extra}" if extra else ""), ok))


code = pair.new_code()
d = pair.derive(code)
u = pair.urls(BASE, d["stream"])
print(f"pairing code {pair.format_code(code)}   stream {d['stream']}\n")

s, b = call("GET", f"{BASE}/health")
check("worker healthy", s, 200, str(b))

# The device polls first and claims the read role.
s, b = call("GET", u["telemetry"], d["read"])
check("device claims read, nothing pushed yet", s, 503)

# The PC pushes and claims the push role.
s, b = call("POST", u["ingest"], d["push"],
            {"host": "live-test", "cpu_percent": 42.5, "ram_percent": 17.0,
             "storage_percent": 61.0, "cpu_temp_c": -1, "uptime_seconds": 99,
             "net_rx_kbps": 1.0, "net_tx_kbps": 2.0})
check("PC pushes with the derived push token", s, 200)

s, b = call("GET", u["telemetry"], d["read"])
ok = s == 200 and b.get("host") == "live-test" and "age_s" in b
res.append((f"device reads its own data back  -> {s}  {json.dumps(b)[:70]}", ok))

s, _ = call("GET", u["telemetry"], "f" * 48)
check("a wrong read token is refused once claimed", s, 401)

s, _ = call("GET", u["telemetry"], d["push"])
check("the push token cannot read", s, 401)

s, _ = call("POST", u["ingest"], d["read"], {"x": 1})
check("the read token cannot push", s, 401)

# Trust on first use: an unclaimed stream is claimed by whoever arrives
# first, so the right assertion is that a CLAIMED stream rejects outsiders.
other = pair.derive(pair.new_code())
ou = pair.urls(BASE, other["stream"])["telemetry"]
s, _ = call("GET", ou, other["read"])
check("a fresh code's stream starts empty and claims its own token", s, 503)
s, _ = call("GET", ou, d["read"])
check("another code's token is refused on that now-claimed stream", s, 401)

print()
fails = 0
for name, ok in res:
    print(("PASS  " if ok else "FAIL  ") + name)
    fails += 0 if ok else 1
print(f"\n{len(res) - fails} passed, {fails} failed")
sys.exit(1 if fails else 0)
