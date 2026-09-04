# PeekESP v1.0.0

A physical system-metrics dashboard. An ESP32 with a 1.14" display shows live
CPU, RAM, storage, temperature and network throughput for a machine anywhere on
the internet — no port forward, no VPN, no account.

By [shouravx](https://github.com/shouravx) · MIT

---

## Setup is one code

```bash
python quickstart.py
```

Builds the Windows app and flashes the board. Then the device shows a code:

```
K7M2-P4QX-9R
```

Type it into the app. Done — the relay URL, the stream and both tokens are
derived from that code on both sides, and the relay never sees it.

Nothing else is typed. No account, no token to copy, no port to forward.

---

## What's in the box

**Firmware** — LVGL dashboard on a LilyGO TTGO T-Display. Two arcs, a bar, a
temperature and throughput panel. Values sweep to new readings over 500 ms
rather than snapping; the gauges run on a 0–1000 range so a 3 % change still
resolves into a visible sweep instead of a staircase. Boot logo, on-device WiFi
setup portal with a QR code to join it, and a status line that names what it is
doing.

**Cloudflare Worker relay** — the host pushes, the device polls, both only ever
dial *out*. Three modes on one deployment: paired (no secrets at all), private
(two tokens), shared (named streams for several people). 50 automated tests.

**Agents** — Linux (`dietpi/peek-agent.py`) and Windows, both standard-library
only. The Windows build adds a tray app with a settings window, and a headless
`peek-agent.exe` for running as a service.

**Prebuilt firmware** — `firmware/PeekESP-merged.bin`, 1.25 MB. Flashing needs
no Arduino IDE, no ESP32 core and no libraries; only esptool, about 3 MB.

---

## Architecture

Two pinned FreeRTOS tasks sharing nothing but a mutex-guarded struct. Core 0
does WiFi, NTP and the blocking HTTPS request. Core 1 runs `lv_timer_handler()`
and never opens a socket, so a slow link cannot drop a frame.

The pairing derivation is the same three lines in three languages:

```
stream = SHA-256("peek-stream:" + CODE)  first 16 hex
push   = SHA-256("peek-push:"   + CODE)  first 48 hex
read   = SHA-256("peek-read:"   + CODE)  first 48 hex
```

C++ on the device, Python in the app, JavaScript in the tests — pinned against
vectors generated independently by `openssl`, because a drift between any two
would mean the PC and the device derive different streams and never meet, with
every request still looking perfectly valid.

Paired streams authenticate by trust-on-first-use. The guarantee is precisely
*"once a role is claimed, only that token works"* — not *"only the right token
was ever possible"*. Squatting an unclaimed stream requires the 16-hex id,
which requires the code.

---

## Verified

| | |
|---|---|
| Firmware | compiles clean, no warnings, 39 % of a 3 MB partition |
| Worker | 50 unit tests, plus 9 live checks against the real deployment |
| Pairing | full round-trip proven end to end on the deployed Worker |
| Windows app | both executables run; settings window driven by smoke tests |
| CI | deploys the Worker on merge, proven with a real change |

## Not verified

The display was brought up on real hardware late, and several things there have
been exercised only briefly or not at all: the boot splash, the QR setup portal,
the on-device SHA-256 derivation, and the TLS handshake under poor signal. The
device-side code compiles and the parts that have run, worked — but this is a
1.0.0, not a soak-tested release.

---

## Known limits

**The free tier is a request budget, not a device count.** Cloudflare allows
100,000 requests/day. At a 5-second interval each agent costs 17,280 and each
display costs 17,280, so a display with two machines is ~52k and fits, while a
display with four is ~86k and is close to the edge. A display costs the same
whether it shows one machine or six — one poll returns all of them. Raising the
interval to 15 s divides everything by three.

**Temperature on Windows is an ACPI zone unless you install something.** A CPU
die temperature sits behind a kernel driver that reads the chip's MSRs, so the
only way to get one is LibreHardwareMonitor with its web server on. Without it
the agent falls back to the thermal-zone performance counter, which needs no
driver and no administrator but reports wherever the board vendor put a sensor —
usually well below the CPU. It is a real reading of a real place; it just isn't
the die. Linux needs none of this; `/sys/class/thermal` is enough.

**2.4 GHz only, WPA2.** An ESP32 has no 5 GHz radio. A network that does not
appear in the scan list is usually 5 GHz-only or WPA3-only rather than a fault.

**Re-flashing keeps the pairing code.** It lives in NVS and survives a firmware
update, which is correct and surprising the first time you want a new one. Use
`python tools/flash.py --erase`.

**An ESP32 cannot join a Tailscale network.** Tailscale is WireGuard plus a
control plane — node registration, rotating keys, DERP relays — and none of that
has an embedded client. The relay exists because of this.

---

## Notable fixes during development

Several of these were only findable by running the thing, and are worth
recording because each looked like something else:

- **Watchdog reboot loop.** Core 0 is the core allowed to block, so the idle-task
  watchdog on it was always contradictory. `WebServer::handleClient()` waits up
  to 5 s for a client that never finishes its request — exactly what captive
  portal probes do — and that starved IDLE0 at precisely the watchdog timeout.
- **SSIDs were injected into the setup page unescaped.** An SSID is chosen by
  whoever owns the access point; a neighbour naming theirs `"><script>…` had
  script execution on the page where the WiFi password is typed.
- **Cloudflare bans the default `Python-urllib` user agent** with error 1010,
  before the Worker runs. Every push would have failed with an opaque 403 that
  looks exactly like an auth problem.
- **A build that succeeded and produced a broken binary.** Excluding `email`
  from PyInstaller built cleanly and died at launch, because `http.server`
  imports it.
- **A build that failed and left the previous binary in place**, so the test
  afterwards passed against stale code.
- **`LV_USE_SPINNER` and `LV_USE_QRCODE` default to 0** in LVGL and fail at
  *link* time, not compile time.

---

## Thanks

[LVGL](https://lvgl.io), [TFT_eSPI](https://github.com/Bodmer/TFT_eSPI),
[ArduinoJson](https://arduinojson.org), and LilyGO for the board.
