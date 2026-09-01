<div align="center">

<img src="img/Git_Logo.png" alt="PeekESP" width="560">

# PeekESP

**A physical system-metrics dashboard for a remote Linux box.**

![board](https://img.shields.io/badge/board-TTGO_T--Display-00E5FF?style=flat-square)
![build](https://img.shields.io/badge/build-Arduino_IDE_%7C_PlatformIO-8CC63F?style=flat-square)
![ui](https://img.shields.io/badge/UI-LVGL_8.3-FF2E7E?style=flat-square)
![license](https://img.shields.io/badge/license-MIT-5C6B82?style=flat-square)

</div>

An ESP32 (LilyGO TTGO T-Display) reaches a remote DietPi box over an encrypted
WireGuard tunnel, polls it every 5 seconds, and sweeps the readings into place
with 500 ms eased animations instead of snapping. Two pinned FreeRTOS tasks keep
the network off the render thread, so a slow tunnel never costs a frame.

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
| [PeekESP/secrets.example.h](PeekESP/secrets.example.h) | Copy to `secrets.h`, fill in, stays out of git. |
| [lv_conf.h](lv_conf.h) | LVGL config for this board. |
| [platformio.ini](platformio.ini) + [main.cpp](main.cpp) | PlatformIO build of the exact same sketch. |
| [dietpi-wireguard-setup.sh](dietpi-wireguard-setup.sh) | Run on the DietPi: creates the tunnel, prints the keys. |
| [dietpi/peek-agent.py](dietpi/peek-agent.py) | Run on the DietPi: serves the JSON the ESP32 reads. |

## Architecture

Two pinned FreeRTOS tasks that share nothing but a mutex-guarded struct:

- **Core 0** — WiFi → NTP → WireGuard handshake → blocking `HTTP GET` → JSON parse.
  Everything here is allowed to stall for seconds at a time.
- **Core 1** — `lv_timer_handler()` and nothing else. It never opens a socket, so
  a slow tunnel cannot drop a frame. It picks up new data by watching a sequence
  counter and takes the mutex with a zero timeout, so even a contended lock just
  defers the update to the next 120 ms tick rather than blocking the UI.

Values animate through `lv_anim_t` with `lv_anim_path_ease_out` over 500 ms. The
gauges run on a 0–1000 range rather than 0–100 so a 3 % change still resolves
into ~30 distinct steps and reads as a sweep, not a staircase.

## The Tailscale part, honestly

**An ESP32 cannot join a tailnet directly.** Tailscale is WireGuard plus a control
plane — node registration, rotating keys, NAT traversal, DERP relays — and there
is no embedded client for any of that.

What this project does instead: the DietPi runs a **plain WireGuard listener
(`wg0`) alongside its existing `tailscale0` interface**, and the ESP32 dials that.
Because the address the ESP32 asks for — the DietPi's own `100.x.x.x` — lives on
that same machine, the kernel answers it regardless of which interface the packet
arrived on. No forwarding rules, no NAT, no route advertisement needed.

`dietpi-wireguard-setup.sh` sets all of this up and prints the keys to paste into
`secrets.h`.

## Setup

### 1. On the DietPi

```bash
sudo bash dietpi-wireguard-setup.sh
```

It generates both keypairs, writes `/etc/wireguard/wg0.conf`, starts the tunnel,
and prints the block of values you need for `secrets.h`.

Then start the telemetry endpoint:

```bash
sudo install -m 755 dietpi/peek-agent.py /usr/local/bin/peek-agent
```

```bash
sudo tee /etc/systemd/system/peek-agent.service >/dev/null <<'EOF'
[Unit]
Description=PeekESP telemetry endpoint
After=network.target

[Service]
ExecStart=/usr/bin/python3 /usr/local/bin/peek-agent
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now peek-agent
```

Check it: `curl http://localhost:8080/telemetry`

Finally, forward UDP 51820 to the DietPi on your router, unless the box already
has a directly reachable public IP.

### 2. On the ESP32 — Arduino IDE

1. **Boards Manager** → *esp32* by Espressif → install **2.0.17**.
   Core 3.x sits on ESP-IDF 5, which removed the `tcpip_adapter` API that
   WireGuard-ESP32 still calls; it will not compile there.
   Board: *ESP32 Dev Module*, Flash 4MB, default partition scheme.
2. **Library Manager** → `TFT_eSPI`, `lvgl` (**8.3.x**, not 9.x), `ArduinoJson`.
3. **Sketch → Include Library → Add .ZIP Library** →
   [WireGuard-ESP32-Arduino](https://github.com/ciniml/WireGuard-ESP32-Arduino)
   (Code → Download ZIP).
4. Edit `<Arduino>/libraries/TFT_eSPI/User_Setup_Select.h`: comment out
   `#include <User_Setup.h>` and uncomment
   `#include <User_Setups/Setup25_TTGO_T_Display.h>`. That header carries the
   MOSI=19 SCLK=18 CS=5 DC=16 RST=23 BL=4 pinout **and** the CGRAM offset the
   135×240 panel needs — without it the image sits 40 px off.
5. Copy this repo's `lv_conf.h` to `<Arduino>/libraries/lv_conf.h` — *next to*
   the `lvgl` folder, not inside it.
6. Copy `PeekESP/secrets.example.h` to `PeekESP/secrets.h` and paste in the
   values the setup script printed.
7. Open `PeekESP/PeekESP.ino`, upload.

### 2b. On the ESP32 — PlatformIO

Steps 4 and 5 are unnecessary; `platformio.ini` supplies the TFT_eSPI pinout and
the LVGL config path as build flags. Still do step 6, then:

```bash
pio run -t upload -t monitor
```

## Bringing it up without the tunnel

Set `USE_WIREGUARD 0` in `PeekESP.ino` and point `DIETPI_HOST` at the DietPi's
plain LAN address. That isolates display and UI problems from tunnel problems,
which is worth doing once before you debug a handshake.

## Buttons

| Button | Action |
|---|---|
| Left (GPIO 0) | Refresh now instead of waiting out the 5 s interval |
| Right (GPIO 35) | Cycle backlight brightness — 100 / 59 / 27 / 8 % |

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
| Display works, `NO LINK` forever | Handshake rejected. `sudo wg show` on the DietPi — a `latest handshake` of *never* means the ESP32's packets are not arriving; check the UDP port forward. |
| Image offset ~40 px, or garbled colours | Wrong TFT_eSPI setup selected. Step 4 above. |
| Compile error on `tcpip_adapter.h` | ESP32 core 3.x. Downgrade to 2.0.17. |
| Boots, then reboots every ~60 s | Twelve failed polls in a row triggers the deliberate `esp_restart()`. The underlying failure is on the network side — watch the serial log at 115200. |
| Backlight on, screen black | LVGL found no `lv_conf.h`. Step 5 — it belongs *beside* the `lvgl` folder. |

## License

MIT — see [LICENSE](LICENSE).
