# PeekESP agent for Windows

Sends the same telemetry a Linux host does, so a Windows PC can drive a PeekESP
display. Same JSON contract as [`dietpi/peek-agent.py`](../dietpi/peek-agent.py)
— only the plumbing underneath differs.

Two executables, because the two jobs want opposite things:

| | **PeekESP.exe** | **peek-agent.exe** |
|---|---|---|
| For | Your desktop | A server, service or scheduled task |
| UI | Tray icon + settings window | None, console only |
| Config | The settings window, or the JSON file | CLI flags, or `--config` |
| Size | ~17.6 MB | ~8.5 MB |
| Dependencies | pystray + Pillow, bundled | **standard library only** |

## Pairing (the whole setup)

1. Flash the ESP32 and power it on. It shows a code like **`K7M2-P4QX-9R`**.
2. Open `PeekESP.exe`, right-click the tray icon, **Settings**.
3. Type the code into **Pair a device** and press **Pair**, then **Save**.

That is it. The relay URL, both tokens and the stream are derived from the code
on both sides; nothing else is typed and no account or secret is involved.
Dashes and case are ignored, so `k7m2-p4qx-9r` works.

The device shows its code until the first reading arrives, then switches to the
dashboard by itself.

Everything below the pairing box is filled in automatically and only worth
touching if you are running your own relay.

## The tray app

Double-click `PeekESP.exe`. It runs in the background with a tray icon;
right-click it for **Settings**, **Reload config file**, **Start / stop** and
**Quit**. The tray tooltip and the top of the menu show live status — `pushing`,
`serving`, `error - HTTP 401 token rejected`, and so on.

The settings window uses the Windows 11 **acrylic backdrop** where the OS
provides it (DWM `SYSTEMBACKDROP_TYPE`, Windows 11 22H2 and later) and falls
back to a flat dark theme where it does not, so it looks deliberate on Windows
10 rather than half-broken. It tells you which you got, in the corner.

**Test connection** does a real push and reports what came back, translated:
`401` becomes "token rejected", `404` "wrong URL or stream", `500` "worker
secrets not set". That is the fastest way to tell a wrong token from a wrong URL.

**Start automatically when I sign in** registers a Task Scheduler entry — no
admin prompt, no service install, and unticking it removes the task.

### "On the device, after flashing"

The panel at the bottom shows exactly what to type into the ESP32's setup
portal once it is flashed — because the device and this agent use *different*
halves of the pair:

| | This agent | The ESP32 |
|---|---|---|
| URL | `…/ingest/alice` | `…/telemetry/alice` |
| Token | push | **read** |

The device URL is **derived from the ingest URL as you type it**, so the two
cannot drift apart. Pushing to `/ingest/alice` while the device polls
`/telemetry/bob` gives you a device stuck on `NO LINK` against a perfectly
healthy relay, and that is a genuinely annoying afternoon.

**New** mints a fresh 48-hex read token — 192 bits, and inside the firmware's
65-byte buffer. Use it in **private** mode: the value it generates is what you
set as `READ_TOKEN` in Cloudflare *and* what you type into the device. In
**shared** mode you do not generate anything here — paste the read token that
`npm run mint` printed for your stream.

**Copy** puts the URL and token on the clipboard together, so you can paste
them into the portal from another machine.

The read token is stored in `config.json` but **never sent** by this agent. It
is kept only so the value is in one place instead of on a sticky note.

## Or just edit the file

Everything the window writes lives in
`%APPDATA%\PeekESP\config.json`, and editing it by hand is a supported path,
not a workaround. **Reload config file** in the tray menu picks up changes
without restarting.

```json
{
  "mode": "push",
  "relay_url": "https://peek-relay.you.workers.dev/ingest/alice",
  "token": "…",
  "device_token": "…",
  "interval": 5.0,
  "serve_port": 8080,
  "autostart": false
}
```

`mode` is `push`, `serve` or `both`. A corrupt or missing file falls back to
defaults rather than refusing to start — a background agent that dies over a
stray comma is worse than one that starts and says so in the tray.

## Headless, for a service

```bash
peek-agent.exe --once
```

prints one reading and exits — the quickest check that it can see your machine.

```bash
peek-agent.exe --config
```

reads the same `config.json`, so **no token appears on the command line**, where
any process listing would expose it. That is the recommended form for a service.

Flags still work for one-off runs: `--push URL --token T`, `--interval N`,
`--no-serve`, or `PEEK_PUSH_TOKEN` in the environment.

Run at startup for all users, even with nobody logged in — this one does need an
elevated prompt:

```bash
schtasks /create /tn PeekESP /sc onstart /ru SYSTEM /tr "\"C:\path\peek-agent.exe\" --config"
```

## Build

```bash
python build.py
```

PyInstaller, pystray and Pillow go into a local `.venv`, not your system Python
— nothing outside this folder changes. Output is `dist/`. `--clean` removes the
artefacts; `--console` builds the tray app with a console window for debugging.

## What it reports

| Field | Source |
|---|---|
| `cpu_percent` | `GetSystemTimes`, idle vs total delta |
| `ram_percent` | `GlobalMemoryStatusEx` |
| `storage_percent` | `GetDiskFreeSpaceExW` on `%SystemDrive%` |
| `uptime_seconds` | `GetTickCount64` |
| `net_rx_kbps` / `net_tx_kbps` | `GetIfTable`, deduplicated |
| `cpu_temp_c` | always `-1` — see below |

**No temperature.** Windows exposes none without a vendor driver;
`MSAcpi_ThermalZoneTemperature` needs admin and most desktops don't implement
it. Reporting `-1` makes the display show `--` rather than a confident wrong
number.

**Network counters need deduplicating.** `GetIfTable` lists every NDIS *filter*
bound to an adapter as its own row: one Realtek NIC typically appears four times
(the adapter plus "WFP Native MAC Layer", "QoS Packet Scheduler" and "WFP 802.3
MAC Layer"), each reporting identical counters. Summing the table naively
multiplies real throughput by however many filters are installed — measured here
as 11041 KB/s on a link actually doing 3442. The agent deduplicates by MAC
address and skips adapters that aren't operational, so a disconnected NIC's
stale totals don't count. Counters are 32-bit and wrap at 4 GB; a negative delta
is treated as a wrap and that sample dropped.

**The user agent is set deliberately.** Cloudflare's edge bans the default
`Python-urllib/3.x` user agent outright with error 1010, before the Worker ever
runs — which surfaces as an opaque `403` that looks nothing like an auth
problem. All three agents identify themselves properly instead.
