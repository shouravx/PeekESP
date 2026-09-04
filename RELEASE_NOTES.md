# PeekESP v1.1.0

A physical system-metrics dashboard. An ESP32 with a 1.14" display shows live
CPU, RAM, storage, temperature and network throughput for machines anywhere on
the internet — no port forward, no VPN, no account.

By [shouravx](https://github.com/shouravx) · MIT

---

## Downloads

```powershell
winget install shouravx.PeekESP
```

Or take what you need directly:

| File | What it is |
|---|---|
| `PeekESP.exe` | The tray app. Double-click it — this is the one most people want |
| `peek-agent.exe` | Headless, for a service or scheduled task |
| `PeekESP-1.1.0-win-x64.zip` | Both of the above plus the licence. This is what winget installs |
| `PeekESP-merged.bin` | Firmware for the board — `python tools/flash.py` |
| `winget-manifests-1.1.0.zip` | Only needed to submit this version to winget |

Linux, one line:

```bash
curl -fsSL https://raw.githubusercontent.com/shouravx/PeekESP/main/dietpi/install.sh | sudo sh
```

The Windows executables are **unsigned**, so SmartScreen warns the first time:
*More info → Run anyway*. The `.sha256` beside the zip lets you check the file
is the one that was built. [Why it is unsigned, and what fixing it
costs.](windows/SIGNING.md)

---

## New in 1.1.0

### One code, several machines

A pairing code identifies *you*, not a machine. Run the agent on a Windows box,
a Mac and a DietPi with the same code and all three appear on the display; the
left button swipes between them. Six per code.

Before this they overwrote a single slot in turn, which on the display looked
exactly like one flapping agent — the worst possible failure for a monitor,
because it is indistinguishable from a real problem with the machine you were
watching.

Swiping costs nothing. One poll already carried every machine, so a display
makes the same number of requests whether it shows one or six.

### Install on Linux in one line

```bash
curl -fsSL https://raw.githubusercontent.com/shouravx/PeekESP/main/dietpi/install.sh | sudo sh
```

Asks for the pairing code, derives the relay URL, stream and push token
locally, installs a hardened systemd service, and **waits to confirm the
machine is actually pushing** before saying it worked. A rejected push says so,
and says the usual cause is the wrong code.

Then everything is managed with a `peekesp` command — `status`, `logs -f`,
`test`, `pair`, `set interval 15`, `update`, `uninstall`. The unit passes no
flags at all; settings live in a config file, so changing the poll interval
rewrites one line rather than regenerating a unit.

`peekesp status` answers the question that actually matters, which is not
whether a service is running: a rejected push leaves the unit perfectly active
while nothing reaches the display.

### Storage is every disk

It reported the system drive only. A machine with a full 240 GB SSD and two
mostly empty 1 TB disks read 95 % when it was 57 % full — the honest number was
on the drives nobody asked about. Both agents now sum every real disk, and the
device shows `STORAGE  906G FREE` beside the bar.

### Temperature on Windows

This had never worked, and not for the reason previously stated. The WMI
fallback referenced `subprocess` in a module that never imported it, so every
attempt raised `NameError` into a bare `except Exception` and came back as
"this machine has no temperature sensor".

With that fixed there is a source that needs nothing installed and no
administrator: the thermal-zone performance counter. It reports an ACPI zone
rather than the CPU die, so it reads cooler than LibreHardwareMonitor would for
the same machine — which the docs now say plainly, because an unexplained 28 °C
on a busy laptop looks like a bug. LibreHardwareMonitor still comes first when
it is running.

### Battery, and a power screen

The last page of the swipe carousel shows the board's own cell — charge,
voltage, and whether something is holding it up — and underneath it, the
battery of the machine you were just looking at.

It does not appear on its own. An earlier build raised it whenever the charge
state changed, which on a board resting near the threshold meant every couple
of seconds, over the top of whatever you were reading. There is hysteresis now.

Honest limit: the T-Display exposes no charge-status pin, so state is inferred
from voltage. A cell being topped up at 3.9 V reads exactly like one
discharging at 3.9 V.

### The board can be woken again

Holding the right button sleeps it. **GPIO 0 cannot be the wake pin** — it is a
strapping pin, and deep-sleep wake is a reset, so held low across it the ESP32
comes up in the serial bootloader instead of running the sketch. The symptom is
a board that sleeps and will not come back, and pressing harder or longer makes
it worse because holding it low is the trigger. Wake is GPIO 35 now, so the
button that sleeps it is the one that wakes it.

### Windows packaging

`package.py` builds, zips both executables, writes the SHA-256 beside it and
generates winget manifests with the hash already in them, cross-checked against
the archive. The build passes `--noupx`, because UPX-packed binaries are one of
the strongest heuristics antivirus engines have.

**The executables are unsigned**, so SmartScreen warns the first time. *More
info → Run anyway.* [SIGNING.md](windows/SIGNING.md) explains what a
self-signed certificate does *not* fix — it is trusted by exactly one machine —
and what the real options cost.

---

## What's in the box

**Firmware** — LVGL dashboard on a LilyGO TTGO T-Display. Two arcs, a bar, a
temperature and throughput panel, and a power page. Values sweep to new
readings over 500 ms rather than snapping. Swiping between machines slides the
whole dashboard body as one object, with values swapped at the midpoint while
nothing is visible.

**Cloudflare Worker relay** — the hosts push, the device polls, both only ever
dial *out*. Three modes on one deployment: paired (no secrets at all), private
(two tokens), shared (named streams). 74 automated tests plus 17 live checks
against the real deployment.

**Agents** — Linux and Windows, both standard-library only. The Windows build
adds a tray app with a settings window and a headless `peek-agent.exe`.

**Prebuilt firmware** — `firmware/PeekESP-merged.bin`. Flashing needs no
Arduino IDE, no ESP32 core and no libraries; only esptool.

---

## Architecture

Two pinned FreeRTOS tasks sharing nothing but a mutex-guarded struct. Core 0
does WiFi, NTP and the blocking HTTPS request. Core 1 runs `lv_timer_handler()`
and never opens a socket, so a slow link cannot drop a frame.

The pairing derivation is the same three lines in four languages:

```
stream = SHA-256("peek-stream:" + CODE)  first 16 hex
push   = SHA-256("peek-push:"   + CODE)  first 48 hex
read   = SHA-256("peek-read:"   + CODE)  first 48 hex
```

C++ on the device, Python in the Windows app, Python again in the Linux agent,
JavaScript in the Worker — all pinned against vectors generated independently
by `openssl`. A drift between any two would mean a machine pushing to a stream
the device never reads, with every request still looking perfectly valid from
both ends.

---

## Verified

| | |
|---|---|
| Firmware | compiles clean under `--warnings all`, 39 % of a 3 MB partition |
| Worker | 74 unit tests, plus 17 live checks against the real deployment |
| Multi-device | three machines under one code, proven live end to end |
| Agents | Windows snapshot checked against PowerShell; Linux tested with stubbed `/proc` |
| Packaging | manifests pass `winget validate`; zip cross-checked, negative-tested |

## Not verified

**None of the device-side work in this release has run on hardware.** It
compiles; that is all that can be said. The swipe, the power page, deep sleep
and the wake button get their first run when you flash it.

The charging threshold is a documented guess — 4.32 V — because there is no
charge-status pin to check it against. The voltage is on screen next to the
percentage so it can be corrected against a real board.

`winget install --manifest` was not run: it needs administrator rights. The
winget-pkgs pipeline installs on a clean VM, which is the real test.

---

## Known limits

**The free tier is a request budget, not a device count.** At a 5-second
interval each agent costs 17,280 requests/day and each display 17,280, against
Cloudflare's 100,000. A display with two machines is ~52k and fits; four is
~86k and is close. `peekesp set interval 15` divides it by three.

**Temperature on Windows is an ACPI zone** unless LibreHardwareMonitor is
running. Real reading, real place, not the CPU die.

**2.4 GHz only, WPA2.** An ESP32 has no 5 GHz radio.

**Re-flashing keeps the pairing code.** It lives in NVS and survives a firmware
update. `python tools/flash.py --erase` gets a new one.

**An ESP32 cannot join a Tailscale network.** Tailscale is WireGuard plus a
control plane, and none of that has an embedded client. The relay exists
because of this.

---

## Thanks

[LVGL](https://lvgl.io), [TFT_eSPI](https://github.com/Bodmer/TFT_eSPI),
[ArduinoJson](https://arduinojson.org), and LilyGO for the board.
