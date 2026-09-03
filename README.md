<div align="center">

<img src="img/Git_Logo.png" alt="PeekESP" width="560">

# PeekESP

**A physical system-metrics dashboard for a remote Linux box.**

![board](https://img.shields.io/badge/board-TTGO_T--Display-00E5FF?style=flat-square)
![build](https://img.shields.io/badge/build-Arduino_IDE_%7C_PlatformIO-8CC63F?style=flat-square)
![ui](https://img.shields.io/badge/UI-LVGL_8.3-FF2E7E?style=flat-square)
![license](https://img.shields.io/badge/license-MIT-5C6B82?style=flat-square)

</div>

An ESP32 (LilyGO TTGO T-Display) monitors a remote Linux box, polling every few
seconds and sweeping the readings into place with 500 ms eased animations rather
than snapping. Two pinned FreeRTOS tasks keep the network off the render thread,
so a slow link never costs a frame.

It reaches the host one of two ways, switchable from the device's own setup
screen. Through a **Cloudflare Worker** the host pushes to — both ends only dial
out, so it needs no port forward anywhere and works behind CGNAT. Or
**directly** over a WireGuard tunnel, which is more private but requires the
host to accept an inbound connection.

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
| [dietpi-wireguard-setup.sh](dietpi-wireguard-setup.sh) | **Direct transport only.** Creates the tunnel on the host, prints the keys. |
| [dietpi/peek-agent.py](dietpi/peek-agent.py) | Run on the Linux host: serves the JSON, and/or pushes it to the relay. |
| [windows/](windows/) | The same agent for a Windows PC, plus a one-file `.exe` build. |
| [cloudflare/](cloudflare/) | Worker relay for when the host has no reachable port. `npm test` covers it. |
| [.github/workflows/](.github/workflows/) | CI: tests the Worker, deploys it on merge, pings it weekly. |
| [tools/png_to_lvgl.py](tools/png_to_lvgl.py) | Turns a PNG into the compiled-in boot logo. |

## Architecture

Two pinned FreeRTOS tasks that share nothing but a mutex-guarded struct:

- **Core 0** — WiFi → NTP → (WireGuard handshake, Direct only) → a blocking
  `HTTP GET` → JSON parse. Everything here is allowed to stall for seconds.
- **Core 1** — `lv_timer_handler()` and nothing else. It never opens a socket, so
  a slow tunnel cannot drop a frame. It picks up new data by watching a sequence
  counter and takes the mutex with a zero timeout, so even a contended lock just
  defers the update to the next 120 ms tick rather than blocking the UI.

Values animate through `lv_anim_t` with `lv_anim_path_ease_out` over 500 ms. The
gauges run on a 0–1000 range rather than 0–100 so a 3 % change still resolves
into ~30 distinct steps and reads as a sweep, not a staircase.

## Choosing a transport

The hard part of this project isn't the display — it's that **something has to
accept an inbound connection**, and the ESP32 can only dial out. Two ways to
solve that, both supported and switchable from the setup portal:

| | **Direct** | **Cloudflare relay** |
|---|---|---|
| How | ESP32 → WireGuard tunnel (or LAN) → host | Host pushes → Worker → ESP32 polls |
| Needs an inbound port | **Yes**, on the host | **No** — both ends dial out |
| Works behind CGNAT | No | **Yes** |
| Works on a network you don't administer | No | **Yes** |
| Data path | End-to-end encrypted, never leaves your kit | Sits on Cloudflare, TLS + bearer tokens |
| Setup | Port forward + WireGuard keys | `wrangler deploy`, two secrets |

If you can port-forward the host, **Direct** is the better answer — nothing
leaves your infrastructure. If the host is behind CGNAT or on someone else's
network, that option simply doesn't exist, and the relay is how you get around
it. See [cloudflare/](cloudflare/) and the deploy steps below.

The relay itself comes in two flavours, and both can be live on one deployment:

| | **Private** | **Shared** |
|---|---|---|
| For | Just your own host and device | You and other people |
| URLs | `/ingest`, `/telemetry` | `/ingest/<stream>`, `/telemetry/<stream>` |
| Secrets | `PUSH_TOKEN` + `READ_TOKEN` | one `MASTER_SECRET` |
| Adding someone | n/a | `npm run mint -- alice`, entirely offline |
| Isolation | n/a | separate Durable Object per stream |

## Tailscale and the Direct transport

> **Only relevant to the Direct transport (2b).** With the Cloudflare relay
> there is no VPN at all — the host and the device both just make outbound HTTPS
> requests, and nothing below applies.

**An ESP32 cannot join a tailnet directly** — this is not a preference, and the
gap is not small:

| What Tailscale needs | Why the ESP32 can't |
|---|---|
| A control-plane client | `tailscaled` is Go. No implementation in C/C++, no Go runtime for Xtensa. |
| Node registration + key rotation | Keys are issued and **rotated** by the coordination server over an authenticated Noise channel. A static config pulled out of a tailnet goes stale on its own. |
| DERP relay fallback | When direct UDP fails, traffic falls back to HTTPS relays — a second full transport. |
| Disco / NAT traversal | Continuous peer discovery and endpoint negotiation. |

Tailscale *is* WireGuard for the data plane; everything above is the control
plane, and that's the part with no embedded client. Headscale doesn't help —
it reimplements the *server*, so you'd still need a client speaking the protocol.

What this project does instead: the DietPi runs a **plain WireGuard listener
(`wg0`) alongside its existing `tailscale0` interface**, and the ESP32 dials that.
Because the address the ESP32 asks for — the DietPi's own `100.x.x.x` — lives on
that same machine, the kernel answers it regardless of which interface the packet
arrived on. No forwarding rules, no NAT, no route advertisement needed.

`dietpi-wireguard-setup.sh` sets all of this up and prints the keys to paste into
the device's setup portal.

### How the ESP32 actually reaches the tailnet

```
ESP32 ──WiFi──▶ your router ──Internet──▶ DietPi  :51820/udp
                                             │
                                        wg0  10.10.44.1
                                             │
                                      ── DietPi kernel ──
                                             │
                                 tailscale0  100.x.x.x
                                             │
                                        the tailnet
```

The ESP32's WireGuard client makes the tunnel its **default route**, so every
packet it sends — including the telemetry request — goes down `wg0`. From there:

**Reaching the DietPi itself** (the default, and all this dashboard needs):
nothing extra. `100.x.x.x` is a local address on that machine, so the kernel
answers it no matter which interface the packet arrived on.

**Reaching other tailnet peers**: the DietPi has to forward. The `PostUp` rules
in `wg0.conf` do this by masquerading `wg0` traffic out of `tailscale0`, so other
peers see it as coming from the DietPi. Nothing needs approving in the admin
console. The trade is that it's one-way — other peers can't open connections
*to* the ESP32. If you want that, drop the `MASQUERADE` line and advertise the
subnet instead:

```bash
sudo tailscale up --advertise-routes=10.10.44.0/24
```

then approve the route at `login.tailscale.com/admin/machines`.

> **MagicDNS names will not resolve on the ESP32** — it has no route to
> Tailscale's resolver. Always give the setup portal a literal `100.x.x.x`
> address.

Two ordering constraints worth knowing, both already handled in the sketch:
NTP has to complete *before* the tunnel comes up (WireGuard's handshake carries
a replay-protection timestamp, and the ESP32 boots at epoch 0), and because the
tunnel becomes the default route, anything that must not go through it has to
happen first.

## Setup

Step 1 is common to both transports. Then do **either** 2a or 2b, not both.

### 1. On the host — the telemetry agent

**Linux:**

```bash
sudo install -m 755 dietpi/peek-agent.py /usr/local/bin/peek-agent
```

**Windows** — same JSON, same flags, standard library only. See
[windows/](windows/):

```bash
python windows\peek-agent-win.py --once
```

Check it reads your machine correctly before wiring anything up — run it in the
foreground and, from another shell:

```bash
curl http://localhost:8080/telemetry
```

You should get real numbers. The systemd unit comes later, once you know which
transport you're using, because the command line differs.

### 2a. Cloudflare relay — no port forward, no VPN

```bash
cd cloudflare
npm install -g wrangler && wrangler login
```

Generate two independent tokens and set them as secrets — never put them in
`wrangler.toml`, it's committed:

```bash
openssl rand -hex 24
```

```bash
npx wrangler secret put PUSH_TOKEN
npx wrangler secret put READ_TOKEN
npx wrangler deploy
```

Two tokens rather than one because the ESP32 sits on a desk with its token in
flash. If it's ever pulled apart, only the *reader* leaks — that token can't
push fabricated telemetry.

Point the agent at it instead of (or as well as) serving:

```bash
python3 peek-agent.py --push https://peek-relay.<you>.workers.dev/ingest --token <PUSH_TOKEN>
```

Check it end to end:

```bash
curl -H "Authorization: Bearer <READ_TOKEN>" https://peek-relay.<you>.workers.dev/telemetry
```

Then in the setup portal choose **Cloudflare relay**, paste the `/telemetry`
URL and the **READ** token. Run `npm test` in `cloudflare/` to exercise the
auth and routing logic locally — 12 checks, no account needed.

Deploys can also run themselves: add a `CLOUDFLARE_API_TOKEN` secret and the
[workflow](.github/workflows/cloudflare-worker.yml) tests every change to
`cloudflare/**`, deploys on merge to `main`, and health-checks weekly. See
[cloudflare/README.md](cloudflare/README.md#ci) for the two secrets and one
variable it wants.

> Free-tier note: this uses a **Durable Object**, not Workers KV. KV's free tier
> allows 1,000 writes/day and a 5-second push interval is 17,280 — it would fail
> partway through day one. Worker requests themselves are ~35k/day against a
> 100k/day free allowance.

Make it permanent:

```bash
sudo tee /etc/systemd/system/peek-agent.service >/dev/null <<'EOF'
[Unit]
Description=PeekESP telemetry agent
After=network-online.target
Wants=network-online.target

[Service]
Environment=PEEK_PUSH_TOKEN=YOUR_PUSH_TOKEN
ExecStart=/usr/bin/python3 /usr/local/bin/peek-agent --push https://peek-relay.YOU.workers.dev/ingest
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now peek-agent
```

**Tailscale plays no part in this path.** The host reaches Cloudflare over the
ordinary internet and the ESP32 does the same. WireGuard stays down — bringing
it up would make it the device's default route and swallow the HTTPS request.

### 2b. Direct — WireGuard tunnel *(alternative to 2a)*

Only workable if the host can accept an inbound connection: you control its
router and have a real public IP, not CGNAT. If that's not true, use 2a.

```bash
sudo bash dietpi-wireguard-setup.sh
```

It generates both keypairs, writes `/etc/wireguard/wg0.conf`, starts the tunnel,
and prints the values you paste into the setup portal. Then forward **UDP 51820**
to the host on your router.

Run the agent in serve mode — no `--push`, no tokens:

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
sudo systemctl daemon-reload && sudo systemctl enable --now peek-agent
```

This is the path where Tailscale matters — see
[Tailscale and the Direct transport](#tailscale-and-the-direct-transport).

### 3. On the ESP32 — Arduino IDE

1. **Boards Manager** → *esp32* by Espressif → install **2.0.17**.
   Core 3.x sits on ESP-IDF 5, which removed the `tcpip_adapter` API that
   WireGuard-ESP32 still calls; it will not compile there.
   Board: *LilyGo T-Display* (*ESP32 Dev Module* also works — both verified).
2. **Library Manager** → `lvgl` and `ArduinoJson`.
   Library Manager offers **lvgl 9.x** first — pick **8.3.9**. This sketch uses
   the v8 API and will not build against v9.
3. **TFT_eSPI — use LilyGO's copy.** They ship one already configured for this
   exact board: copy the `TFT_eSPI` folder out of
   [Xinyuan-LilyGO/TTGO-T-Display](https://github.com/Xinyuan-LilyGO/TTGO-T-Display)
   into `<Arduino>/libraries/`. Its `User_Setup_Select.h` already points at
   `Setup25_TTGO_T_Display.h`, so there's nothing to edit and no way to end up
   with the image offset by 40 px.
   *Alternative:* Library Manager's TFT_eSPI **2.5.43** also works — both are
   verified — but then you must edit `User_Setup_Select.h` yourself: comment out
   `#include <User_Setup.h>`, uncomment the `Setup25_TTGO_T_Display.h` line.
4. Only if you're using the **Direct** transport: **Sketch → Include Library →
   Add .ZIP Library** →
   [WireGuard-ESP32-Arduino](https://github.com/ciniml/WireGuard-ESP32-Arduino)
   (Code → Download ZIP).
5. Copy this repo's `lv_conf.h` to `<Arduino>/libraries/lv_conf.h` — *next to*
   the `lvgl` folder, not inside it. `LV_USE_SPINNER` and `LV_USE_QRCODE` both
   default to **0** upstream, so a stock config link-errors on two widgets this
   sketch uses.
6. Open `PeekESP/PeekESP.ino`, set *Tools → Partition Scheme → Huge APP*,
   upload. **No credentials needed at compile time** — you configure the device
   from its own screen. `secrets.h` is optional and only seeds factory defaults.

**Verified build** — ESP32 core 2.0.17, lvgl 8.3.9, ArduinoJson 7.4.3,
WireGuard-ESP32 0.1.5, against **both** TFT_eSPI builds:

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

### 3b. On the ESP32 — PlatformIO

Steps 3 and 5 are unnecessary; `platformio.ini` supplies the TFT_eSPI pinout and
the LVGL config path as build flags, which is why PlatformIO never had the
`User_Setup_Select.h` problem in the first place. Then:

```bash
pio run -t upload -t monitor
```

## Bringing it up on the LAN first

Worth doing once, whichever transport you end up on: it separates display and UI
faults from network faults.

In the setup portal pick **Direct**, untick *"Route telemetry through the
tunnel"*, and point the telemetry host at the host's plain `192.168.x.x`
address with the agent running in serve mode. If the gauges move, the display
and firmware are fine and anything that breaks afterwards is transport.

## Configuration

There is no compile-time setup. A freshly flashed device has no WiFi
credentials, so it boots straight into **setup mode**: it raises its own access
point and the screen shows a QR code.

1. Scan the QR with a phone camera — it encodes a `WIFI:` join string, so the
   phone joins the AP directly. No typing the generated password off a 1.14"
   panel.
2. The captive portal opens the form (or browse to `192.168.4.1`).
3. Fill in WiFi, the WireGuard keys, and the telemetry host. The SSID field is
   a dropdown populated by a live scan.
4. **Save & Reboot** — settings go to NVS and survive reflashing the sketch.

To change something later, hold the **left button for 1.5 s**; the device
reboots into setup mode. Holding it during power-on does the same, which is the
way back in if you typo the WiFi password. "Erase all settings" at the bottom of
the form returns the device to factory defaults.

> The portal serves plain HTTP over its own WPA2 link — your WireGuard private
> key crosses that link unencrypted. TLS would need a certificate no phone would
> trust, and the alternative is entering a 44-character base64 key with two
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
| Display works, `NO LINK` forever | Handshake rejected. `sudo wg show` on the DietPi — a `latest handshake` of *never* means the ESP32's packets are not arriving; check the UDP port forward. |
| Image offset ~40 px, or garbled colours | Wrong TFT_eSPI setup selected. Step 4 above. |
| Compile error on `tcpip_adapter.h` | ESP32 core 3.x. Downgrade to 2.0.17. |
| Boots, then reboots every ~60 s | Twelve failed polls in a row triggers the deliberate `esp_restart()`. The underlying failure is on the network side — watch the serial log at 115200. |
| Backlight on, screen black | LVGL found no `lv_conf.h`. Step 5 — it belongs *beside* the `lvgl` folder. |
| Always boots to the QR setup screen | No SSID saved yet, or the left button is reading LOW at power-on (stuck button / something pulling GPIO 0 down). |
| Typo'd the WiFi password | Hold the left button while powering on to force setup mode back up. |
| `undefined reference to lv_qrcode_create` | Stock `lv_conf.h`. `LV_USE_QRCODE` and `LV_USE_SPINNER` default to 0 — use this repo's copy. |
| `region 'dram0_0_seg' overflowed` | LVGL's heap is a static array counted against a ~160 KB segment. Lower `LV_MEM_SIZE` in `lv_conf.h` (48 KB here) or shrink the draw buffer in the sketch. |
| `'Gauge' was not declared in this scope` | You added a function above the type it uses. The Arduino IDE injects generated prototypes before the *first* function definition, so every type used in a signature must be declared above that point. See the note on `struct Gauge`. |

## License

MIT — see [LICENSE](LICENSE).
