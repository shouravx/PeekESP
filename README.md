<div align="center">

<img src="img/Git_Logo.png" alt="PeekESP" width="560">

# PeekESP

**A physical system-metrics dashboard for a remote Linux box.**

![board](https://img.shields.io/badge/board-TTGO_T--Display-00E5FF?style=flat-square)
![build](https://img.shields.io/badge/build-Arduino_IDE_%7C_PlatformIO-8CC63F?style=flat-square)
![ui](https://img.shields.io/badge/UI-LVGL_8.3-FF2E7E?style=flat-square)
![license](https://img.shields.io/badge/license-MIT-5C6B82?style=flat-square)

</div>

An ESP32 (LilyGO TTGO T-Display) monitors a remote Linux or Windows machine,
polling every few seconds and sweeping the readings into place with 500 ms eased
animations rather than snapping.

## Quick start

```bash
python quickstart.py
```

Builds the Windows app and flashes the board with the firmware committed in
[firmware/](firmware/). **No Arduino IDE, no ESP32 core, no libraries, no
toolchain** — the only download is esptool, about 3 MB. Then:

1. The device shows a pairing code, like **`K7M2-P4QX-9R`**
2. Run `windows/dist/PeekESP.exe` → tray icon → **Settings**
3. Type the code into **Pair a device** → **Pair** → **Save**

That is the whole setup. **No account, no token, no port forward, no VPN** — the
device and the app derive everything else from the code, and the relay never
sees it.

Only planning to *change* the firmware? `python quickstart.py --dev` installs the
Arduino toolchain and builds from source instead.

```
┌──────────────────────────────────────────────┐
│ PEEK  // dietpi                42 ms      ◍  │
│──────────────────────────────────────────────│
│    ╭────╮      ╭────╮      ┌──────────────┐  │
│    │ 12 │      │ 43 │      │ TEMP         │  │
│    │CPU%│      │RAM%│      │ 48°          │  │
│    ╰────╯      ╰────╯      │ ↓128K  ↑13K  │  │
│                            └──────────────┘  │
│ STORAGE                                 61%  │
│ ████████████████████░░░░░░░░░░░░░░░░░░░░░░░  │
│ up 3d 4h                            LINK OK  │
└──────────────────────────────────────────────┘
```

## What's in here

| Path | What it is |
|---|---|
| [PeekESP/PeekESP.ino](PeekESP/PeekESP.ino) | The firmware. Open this one in the Arduino IDE. |
| [PeekESP/secrets.example.h](PeekESP/secrets.example.h) | Optional factory defaults. Real config happens on-device. |
| [lv_conf.h](lv_conf.h) | LVGL config for this board. |
| [platformio.ini](platformio.ini) + [main.cpp](main.cpp) | PlatformIO build of the exact same sketch. |
| [dietpi/peek-agent.py](dietpi/peek-agent.py) | Run on the Linux host: serves the JSON, and/or pushes it to the relay. |
| [windows/](windows/) | The same agent for a Windows PC, plus a one-file `.exe` build. |
| [cloudflare/](cloudflare/) | Worker relay for when the host has no reachable port. `npm test` covers it. |
| [.github/workflows/](.github/workflows/) | CI: tests the Worker, deploys it on merge, pings it weekly. |
| [tools/png_to_lvgl.py](tools/png_to_lvgl.py) | Turns a PNG into the compiled-in boot logo. |
| [tools/make_icon.py](tools/make_icon.py) | Builds the multi-resolution Windows `.ico` from the logo. |
| [quickstart.py](quickstart.py) | Install, build and flash in one command. |
| [tools/setup_arduino.py](tools/setup_arduino.py) | Installs the core and every library, patched and pinned. |
| [firmware/](firmware/) | The compiled firmware, ready to flash. No toolchain needed. |
| [tools/flash.py](tools/flash.py) | Flashes it and opens the serial monitor. No IDE. |
| [tools/export_firmware.py](tools/export_firmware.py) | Rebuilds that image after a firmware change. |

## Architecture

Two pinned FreeRTOS tasks that share nothing but a mutex-guarded struct:

- **Core 0** — WiFi → NTP → a blocking `HTTPS GET` → JSON parse. Everything
  here is allowed to stall for seconds at a time.
- **Core 1** — `lv_timer_handler()` and nothing else. It never opens a socket, so
  a slow link cannot drop a frame. It picks up new data by watching a sequence
  counter and takes the mutex with a zero timeout, so even a contended lock just
  defers the update to the next 120 ms tick rather than blocking the UI.

Values animate through `lv_anim_t` with `lv_anim_path_ease_out` over 500 ms. The
gauges run on a 0–1000 range rather than 0–100 so a 3 % change still resolves
into ~30 distinct steps and reads as a sweep, not a staircase.

## Why a relay

The hard part of this project isn't the display — it's that **something has to
accept an inbound connection**, and the ESP32 can only dial out.

A plain WireGuard tunnel solves that, and this project used to do it, but the
tunnel has to terminate on the monitored host. Behind CGNAT, or on a network you
don't administer, that option doesn't exist. Pushing through a Cloudflare Worker
instead means **both ends only ever dial out**, which works everywhere the
tunnel did and everywhere it didn't.

An ESP32 also can't join a Tailscale network, which is the other thing people
reach for: Tailscale is WireGuard plus a control plane — node registration,
rotating keys, NAT traversal, DERP relays — and none of that has an embedded
client.

The relay has three modes, all on one deployment:

| | **Paired** | **Private** | **Shared** |
|---|---|---|---|
| Setup | type the device's code | two Worker secrets | one `MASTER_SECRET` |
| Worker config | **none** | `PUSH_TOKEN` + `READ_TOKEN` | mint per stream |
| For | just working | only you | you and other people |

## Get it

| | |
|---|---|
| **This repository** | [Download ZIP](https://github.com/shouravx/PeekESP/archive/refs/heads/main.zip) &middot; `git clone https://github.com/shouravx/PeekESP.git` |
| **Windows app** | build it: `cd windows && python build.py` &rarr; `dist/PeekESP.exe` |
| **Arduino IDE** | [arduino.cc/en/software](https://www.arduino.cc/en/software) |
| **Python 3** | [python.org/downloads](https://www.python.org/downloads/) — needed only for the setup scripts and the agent |

**Nothing else to download.** The compiled firmware is committed at
`firmware/PeekESP-merged.bin` (1.25 MB), so flashing needs no core, no libraries
and no toolchain — `tools/flash.py` fetches only esptool, ~3 MB.

```bash
python tools/flash.py              # flash the committed image, then watch serial
python tools/flash.py --erase      # also wipe settings, for a fresh pairing code
```

To modify the firmware you do need the toolchain, and one command installs all
of it at the pinned versions, patched:

```bash
python tools/setup_arduino.py      # ~250 MB, once
python tools/export_firmware.py    # rebuild the committed image after a change
```

`setup_arduino.py --check` reports what is installed without changing anything.
Everything lands in the normal Arduino folders, so the Arduino IDE sees it too
if you prefer to work there.

<details>
<summary>If you would rather install them by hand</summary>

| | Version | Where |
|---|---|---|
| ESP32 core | **2.0.17** | Boards Manager, or [package_esp32_index.json](https://espressif.github.io/arduino-esp32/package_esp32_index.json) |
| lvgl | **8.3.9** — not 9.x | [github.com/lvgl/lvgl](https://github.com/lvgl/lvgl/releases/tag/v8.3.9) |
| TFT_eSPI | 2.5.43 | [github.com/Bodmer/TFT_eSPI](https://github.com/Bodmer/TFT_eSPI) |
| ArduinoJson | 7.4.3 | [arduinojson.org](https://arduinojson.org/) |

Then two edits the script would have done for you: in
`<Arduino>/libraries/TFT_eSPI/User_Setup_Select.h` comment out
`#include <User_Setup.h>` and uncomment the `Setup25_TTGO_T_Display.h` line, and
copy this repo's `lv_conf.h` to `<Arduino>/libraries/lv_conf.h` — *beside* the
`lvgl` folder, not inside it.

The libraries are not committed to this repository because LVGL alone is 97 MB
across 1160 files and TFT_eSPI another 34 MB; and LVGL finds `lv_conf.h` by
looking one directory *above* its own folder, which cannot work from inside a
sketch without build flags the Arduino IDE has no way to set.

</details>

## Setup

### 1. On the host — the telemetry agent

**Linux:**

```bash
sudo install -m 755 dietpi/peek-agent.py /usr/local/bin/peek-agent
```

**Windows** — same JSON, same flags, standard library only. See
[windows/](windows/):

```bash
python windows\peek_agent_win.py --once
```

Check it reads your machine correctly before wiring anything up — run it in the
foreground and, from another shell:

```bash
curl http://localhost:8080/telemetry
```

You should get real numbers. The systemd unit comes after pairing, because the
push URL contains the stream the code produced.

### 2. Deploy the relay

```bash
cd cloudflare
npm install -g wrangler && wrangler login
npx wrangler deploy
```

That is the whole deployment. **Pairing needs no secrets on the Worker at all** —
the device and the app derive their stream and tokens from the pairing code
locally, and the Worker never sees it. `/health` reporting
`"configured": false` is the expected state.

`PUSH_TOKEN` / `READ_TOKEN` (private) and `MASTER_SECRET` (named streams) are
only for the other two modes — see [cloudflare/](cloudflare/).

Deploys can run themselves: add a `CLOUDFLARE_API_TOKEN` secret and the
[workflow](.github/workflows/cloudflare-worker.yml) tests every change to
`cloudflare/**`, deploys on merge to `main`, and health-checks weekly.

> Free-tier note: this uses a **Durable Object**, not Workers KV. KV's free tier
> allows 1,000 writes/day and a 5-second push interval is 17,280 — it would fail
> partway through day one. A paired set costs ~35k requests/day against a
> 100k/day allowance, so **one deployment covers two devices**.

### 3. Pair the device

Flash the sketch, and the ESP32 shows a code like **`K7M2-P4QX-9R`**. Open the
Windows app, right-click the tray icon → **Settings**, type the code into
**Pair a device**, press **Pair** then **Save**.

That is the entire configuration. The relay URL, the stream and both tokens are
derived from the code on both sides; dashes and case are ignored. The device
stops showing the code as soon as the first reading arrives.

Make the agent permanent afterwards:

```bash
sudo tee /etc/systemd/system/peek-agent.service >/dev/null <<'EOF'
[Unit]
Description=PeekESP telemetry agent
After=network-online.target
Wants=network-online.target

[Service]
Environment=PEEK_PUSH_TOKEN=YOUR_PUSH_TOKEN
ExecStart=/usr/bin/python3 /usr/local/bin/peek-agent --push https://peek-relay.YOU.workers.dev/ingest/STREAM
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now peek-agent
```

On Windows the tray app's **Start automatically when I sign in** does the same
thing with no service to install.

### 4. Flashing the ESP32

```bash
python tools/flash.py
```

Finds the board, writes `firmware/PeekESP-merged.bin`, and opens the serial
monitor so you can read the pairing code as it boots. `--erase` wipes saved
settings, which is how you get a *new* pairing code — re-flashing alone keeps
the old one, because it lives in NVS and survives a firmware update.

`--build` compiles from source instead, if you have the toolchain.

Prefer the Arduino IDE? Everything is already installed and patched: open
`PeekESP/PeekESP.ino`, set **Board: LilyGo T-Display** and **Tools → Partition
Scheme → Huge APP**, upload. *ESP32 Dev Module* also works; both are verified.

**Verified build** — ESP32 core 2.0.17, lvgl 8.3.9, ArduinoJson 7.4.3,
against **both** TFT_eSPI builds:

| TFT_eSPI | Flash | Result |
|---|---|---|
| LilyGO 2.2.20 (preconfigured) | 1,234,601 B | clean, no warnings |
| Library Manager 2.5.43 | 1,238,797 B | clean, no warnings |

The sketch only uses `init` / `setRotation` / `fillScreen` / `startWrite` /
`setAddrWindow` / `pushColors` / `endWrite`, all stable across that version gap.

```
Sketch uses 1234601 bytes (39%) of program storage space.
Global variables use 119748 bytes (36%) of dynamic memory.
```

That 39 % is with **Huge APP**. On the default 1.2 MB partition it's **94 %** —
it still fits, but there's nothing left, so set *Tools → Partition Scheme →
Huge APP (3MB No OTA/1MB SPIFFS)*.

### 4b. Flashing the ESP32 — PlatformIO

Steps 3 and 5 are unnecessary; `platformio.ini` supplies the TFT_eSPI pinout and
the LVGL config path as build flags, which is why PlatformIO never had the
`User_Setup_Select.h` problem in the first place. Then:

```bash
pio run -t upload -t monitor
```

## Configuration

There is no compile-time setup. A freshly flashed device has no WiFi
credentials, so it boots straight into **setup mode**: it raises its own access
point and the screen shows a QR code.

1. Scan the QR with a phone camera — it encodes a `WIFI:` join string, so the
   phone joins the AP directly. No typing the generated password off a 1.14"
   panel.
2. The captive portal opens the form (or browse to `192.168.4.1`).
3. Fill in your WiFi. The SSID field is a dropdown populated by a live scan.
   Everything else is already set if you paired from the app.
4. **Save & Reboot** — settings go to NVS and survive reflashing the sketch.

To change something later, hold the **left button for 1.5 s**; the device
reboots into setup mode. Holding it during power-on does the same, which is the
way back in if you typo the WiFi password. "Erase all settings" at the bottom of
the form returns the device to factory defaults.

> The portal serves plain HTTP over its own WPA2 link — your WiFi passphrase
> crosses that link unencrypted. TLS would need a certificate no phone would
> trust, and the alternative is entering a 48-character token with two
> buttons. The AP is only up while you are configuring it, and its password is
> derived per-device from the MAC.

## Changing the boot logo

The device shows the logo for about a second at boot, then fades it out while
the network is already connecting underneath — the splash costs no boot time,
it just covers the moment the dashboard would otherwise show a screen of zeroes.

`PeekESP/logo_splash.h` is generated, not hand-written. To use different
artwork:

```bash
python tools/png_to_lvgl.py img/logo.png PeekESP/logo_splash.h --name logo_splash --size 96 --bg 05070E
```

It composites transparency onto the theme background (`--bg`), scales to fit
`--size`, and emits RGB565 in the byte order the firmware's `LV_COLOR_16_SWAP`
setting expects. At 96×96 that is 18 KB of **flash** — the image is `const`, so
it costs no RAM at all.

Alpha is flattened rather than kept: `LV_IMG_CF_TRUE_COLOR_ALPHA` would be 3
bytes per pixel and blend every frame, and the splash only ever sits on one
colour. Pass `--swap` if you ever set `LV_COLOR_16_SWAP` to 1 — the symptom of
getting it wrong is swapped red and blue, not an error.

## Buttons

| Button | Action |
|---|---|
| Left (GPIO 0) — tap | Refresh now instead of waiting out the poll interval |
| Left — hold 1.5 s | Reboot into setup mode |
| Left — held at power-on | Boot straight into setup mode |
| Right (GPIO 35) | Cycle backlight brightness — 100 / 59 / 27 / 8 %, remembered across reboots |

## Telemetry contract

`GET http://<host>:8080/telemetry` → `200 application/json`

```json
{
  "host": "dietpi",
  "cpu_percent": 12.5,
  "ram_percent": 43.2,
  "storage_percent": 61.0,
  "cpu_temp_c": 48.3,
  "uptime_seconds": 271830,
  "net_rx_kbps": 128.4,
  "net_tx_kbps": 12.9
}
```

Every field is optional on the wire — a missing key falls back to a default
rather than failing the parse. `cpu_temp_c` below zero renders as `--`, which is
what hosts with no thermal zone report.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Display works, `NO LINK` forever | The agent isn't pushing, or the device is polling a different stream. Check `[pair] code X -> stream Y` on serial matches what the app shows. |
| Image offset ~40 px, or garbled colours | Wrong TFT_eSPI setup selected. Step 4 above. |
| Boots, then reboots every ~60 s | Twelve failed polls in a row triggers the deliberate `esp_restart()`. The underlying failure is on the network side — watch the serial log at 115200. |
| Backlight on, screen black | LVGL found no `lv_conf.h`. Step 5 — it belongs *beside* the `lvgl` folder. |
| Always boots to the QR setup screen | No SSID saved yet, or the left button is reading LOW at power-on (stuck button / something pulling GPIO 0 down). |
| Typo'd the WiFi password | Hold the left button while powering on to force setup mode back up. |
| `undefined reference to lv_qrcode_create` | Stock `lv_conf.h`. `LV_USE_QRCODE` and `LV_USE_SPINNER` default to 0 — use this repo's copy. |
| `region 'dram0_0_seg' overflowed` | LVGL's heap is a static array counted against a ~160 KB segment. Lower `LV_MEM_SIZE` in `lv_conf.h` (48 KB here) or shrink the draw buffer in the sketch. |
| `'Gauge' was not declared in this scope` | You added a function above the type it uses. The Arduino IDE injects generated prototypes before the *first* function definition, so every type used in a signature must be declared above that point. See the note on `struct Gauge`. |

## License

MIT — see [LICENSE](LICENSE).
