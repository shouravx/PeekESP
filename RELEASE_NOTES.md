# PeekESP v1.2.0

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
| `PeekESP-1.2.0-win-x64.zip` | Both of the above plus the licence. This is what winget installs |
| `peekesp_1.2.0_all.deb` | Debian, Ubuntu, Raspberry Pi OS, DietPi |
| `PeekESP-merged.bin` | Firmware for the board — `python tools/flash.py` |
| `winget-manifests-1.2.0.zip` | Only needed to submit this version to winget |

Linux, one line:

```bash
curl -fsSL https://raw.githubusercontent.com/shouravx/PeekESP/main/dietpi/install.sh | sudo sh
```

The Windows executables are **unsigned**, so SmartScreen warns the first time:
*More info → Run anyway*. The `.sha256` beside the zip lets you check the file
is the one that was built. [Why it is unsigned, and what fixing it
costs.](windows/SIGNING.md)

---

## New in 1.2.0

### Sleeping no longer means unplugging it

1.1.0 slept with `esp_deep_sleep_start()` and woke on a GPIO. Two things were
wrong with that, and both were found on hardware.

The wake pin was **GPIO 0, a strapping pin**. Deep-sleep wake is a reset, and
the ESP32 reads GPIO 0 at reset to decide whether to run your sketch or the
serial bootloader — so holding down the wake button did the one thing that
guarantees it will not come back. Pressing harder made it worse.

The second is that deep sleep *is* a reset even when it works. Waking meant
rejoining WiFi, re-syncing NTP and re-polling: several seconds of a blank
screen before anything appeared, and the page you were on was gone.

There is no deep sleep now. Standby turns off the panel and the backlight,
drops the poll to once a minute, and leaves RAM and both tasks alive. Pressing
the button brings back the same page with the same values, immediately. The
cost is honest: standby draws more than deep sleep would. For a board on a desk
with a cable, resuming where you left off is worth more than the microamps.

### A clock, with the date and the right time zone

The last release synced NTP and never showed the time. There is a clock page
now: a large 12-hour time with an AM/PM marker, the day and date under it, and
a seconds ring around the edge.

The time zone is a setting, in minutes, so the places that are not on a whole
hour work — Dhaka at UTC+6, Kathmandu at +5:45, Adelaide at +9:30. The label
beside it is yours to type.

There were briefly two clock faces, one of which showed only the time. A screen
in a swipe carousel that shows less than the one before it is a screen you
swipe past, so it is gone.

### It says when a machine has stopped reporting

A dashboard that shows the last known numbers forever is worse than a blank
one: a dead machine looks like a healthy one whose figures happen not to be
moving. That is the failure this project exists to catch, so it was the wrong
one to have.

Each machine now carries the age of its reading, and after five polls bring
nothing newer it is drawn as **OFFLINE** with how long it has been silent.

The age is computed from the relay's own timestamp *plus* the time since the
display last managed a successful fetch. Without that second term a display
that has lost WiFi shows every machine as permanently fresh — which is exactly
backwards, because at that moment it knows nothing at all.

The freshness redraw runs on its own one-second timer rather than inside the
"new data arrived" path. In 1.1.0 the ages only advanced when new data came in,
and *no new data* was the thing being reported.

### The battery reading stopped lying

It reported 100 % on the charger and 20 % a second after unplugging. The
charging threshold was 4.32 V, but a real charger holds the cell at about
4.2 V, so the condition never fired and the charger's own voltage was being
shown as state of charge.

The threshold is 4.15 V with hysteresis, readings are smoothed across samples,
and while something external is holding the cell up the percentage is
suppressed rather than invented — because on this board it cannot be known.

Honest limit, unchanged: the T-Display exposes no charge-status pin. A cell
being topped up at 3.9 V reads exactly like one discharging at 3.9 V.

### Settings worth opening

The configuration page was one long form. It is now a proper page — grouped
sections, live validation, a WiFi scan that fills the field, and a dark theme
that matches the device.

New in it:

- **Five WiFi networks**, tried in order. A board that travels between a desk
  and a bench no longer needs reflashing.
- **A new pairing code on demand**, which is how you revoke one that has been
  shared or shown in a photograph.
- **Time zone offset and label.**
- **A switch to turn the web UI off entirely.** It is a listening socket with a
  password on it; if you do not use it, it should not be there.

It also *works*. In 1.1.0 the page answered 404 to everything, because every
`server.on()` was registered inside a task that had already returned.

### Update checks, and commands from the desktop

The relay is talking to the device anyway, so it now carries the latest release
tag with the reply and the device raises a banner when it is behind. The
alternative — every device polling `api.github.com` — is a second host, a
second certificate authority in an image with no room, and a second request per
device per interval against the budget that is the actual limit here.

The tray app checks for its own updates on a six-hour floor, and can send the
device a short, closed list of commands: reboot, standby, wake, refresh,
identify (flashes the screen, so you can tell two boards apart), jump to a
page, and set the backlight.

A command is left at the relay and collected on the next poll, so there is
still nothing listening anywhere. It is delivered at most once and dropped
after five minutes. Nothing in that vocabulary changes configuration and
everything in it is undone by pressing a button on the device — configuration
is what the settings page is for, behind its own password.

### Packaging and CI

- **`peekesp_1.2.0_all.deb`** — `sudo apt install ./peekesp_1.2.0_all.deb`.
  Built with `dpkg-deb` alone, no container and no sponsor.
- **`packaging/aur/PKGBUILD`** for Arch.
- The release is **built by CI from the tag**: the Linux package is installed
  and its files checked, the Windows agent is run and its JSON parsed, the tag
  is checked against the `VERSION` file, and every asset is named rather than
  counted. It stops at a **draft** — publishing is a person clicking publish.
- **The firmware compiles on every push**, under `--warnings all`, with a size
  ceiling and a check that the committed image is not older than the sketch.

### A landing page

[shouravx.github.io/PeekESP](https://shouravx.github.io/PeekESP/) — photographs
of the real device, what it does, and how to flash it.

---

## What's in the box

**Firmware** — LVGL dashboard on a LilyGO TTGO T-Display. Two arcs, a bar, a
temperature and throughput panel, a clock and a power page. Values sweep to new
readings over 500 ms rather than snapping. Swiping between machines slides the
whole dashboard body as one object, with values swapped at the midpoint while
nothing is visible.

**Cloudflare Worker relay** — the hosts push, the device polls, both only ever
dial *out*. Three modes on one deployment: paired (no secrets at all), private
(two tokens), shared (named streams). 86 automated tests.

**Agents** — Linux and Windows, both standard-library only. The Windows build
adds a tray app with a settings window and a headless `peek-agent.exe`.

**Prebuilt firmware** — `firmware/PeekESP-merged.bin`. Flashing needs no
Arduino IDE, no ESP32 core and no libraries; only esptool.

---

## Architecture

Two pinned FreeRTOS tasks sharing nothing but a mutex-guarded struct. Core 0
does WiFi, NTP, the blocking HTTPS request and the battery ADC. Core 1 runs
`lv_timer_handler()` and never opens a socket, so a slow link cannot drop a
frame. The ADC moved off the render path in this release: sampling it inside
the LVGL loop meant a battery reading and an animation frame competing for the
same core.

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
| Firmware | compiles clean under `--warnings all`, 43 % of a 3 MB partition |
| Worker | 86 unit tests, plus live checks against the real deployment |
| Multi-device | three machines under one code, proven live end to end |
| Agents | Windows snapshot checked against PowerShell; Linux run in CI |
| Packaging | the `.deb` is installed and inspected in CI; manifests pass `winget validate` |

## Not verified

**The device-side work in this release has not run on hardware.** Standby, the
clock, the corrected battery reading, the redesigned settings page, the
multi-network join and the commands all compile; that is what can be said.

The charging threshold is still inferred — 4.15 V — because there is no
charge-status pin to check it against. The voltage is on screen beside the
percentage so it can be corrected against a real board.

---

## Known limits

**The free tier is a request budget, not a device count.** At a 5-second
interval each agent costs 17,280 requests/day and each display 17,280, against
Cloudflare's 100,000. A display with two machines is ~52k and fits; four is
~86k and is close. `peekesp set interval 15` divides it by three.

**There is no over-the-air update.** The `huge_app` scheme has one application
slot, and the image no longer fits a two-slot layout. Updating the firmware
means a cable.

**Temperature on Windows is an ACPI zone** unless LibreHardwareMonitor is
running. Real reading, real place, not the CPU die.

**2.4 GHz only, WPA2.** An ESP32 has no 5 GHz radio.

**Re-flashing keeps the pairing code.** It lives in NVS and survives a firmware
update. `python tools/flash.py --erase` gets a new one, and so does the button
on the settings page.

**An ESP32 cannot join a Tailscale network.** Tailscale is WireGuard plus a
control plane, and none of that has an embedded client. The relay exists
because of this.

---

## Thanks

[LVGL](https://lvgl.io), [TFT_eSPI](https://github.com/Bodmer/TFT_eSPI),
[ArduinoJson](https://arduinojson.org), and LilyGO for the board.
