# Troubleshooting

Symptom first. Most entries have a one-command check.

---

## Relay

### Every endpoint returns `PUSH_TOKEN and READ_TOKEN secrets are not set`

The deploy worked. The Worker's own secrets are missing.

These are **Cloudflare Worker secrets, not GitHub Actions secrets** — separate
stores, and a GitHub secret never reaches the running Worker. See
[Configuration Reference](Configuration-Reference#2-cloudflare--workers--pages--peek-relay--settings--variables-and-secrets).

`/health` still answers `200` in this state and reports `configured: false`,
which is how you tell "deployed but unconfigured" from "not deployed".

### `401 unauthorized`

The token is wrong, or you used the wrong one of the two. `PUSH_TOKEN` works
only on `/ingest`; `READ_TOKEN` works only on `/telemetry`. That separation is
deliberate — swapping them fails by design, not by accident.

### `503 no telemetry received yet`

Not an error if you have not started the agent. Auth passed and the store is
empty. If the agent *is* running, check it is pushing to `/ingest` (not
`/telemetry`) and that its token is `PUSH_TOKEN`.

### Display shows `STALE`

The relay is reachable but the last push is older than 30 s — the agent stopped,
not the network.

```bash
sudo systemctl status peek-agent --no-pager
```

### `wrangler deploy` rejects Durable Objects

The free plan needs the SQLite-backed variety. `wrangler.toml` already uses
`new_sqlite_classes` for this. If it still fails, the account may predate free
Durable Object support.

### Weekly health check passes but checks nothing

The `WORKER_URL` repository **variable** is unset, so the job skips on purpose
rather than failing a fresh clone. Set it to the base URL — no path.

---

## Firmware build

### `'Gauge' was not declared in this scope`

You added a function above the type it uses. The Arduino IDE injects generated
prototypes immediately before the **first** function definition, so every type
named in a signature must be declared above that point. See the note next to
`struct Gauge`.

### `undefined reference to lv_qrcode_create` / `lv_spinner_create`

Stock `lv_conf.h`. `LV_USE_QRCODE` and `LV_USE_SPINNER` both default to **0**
upstream — use this repo's copy, placed *beside* the `lvgl` folder, not inside
it. This fails at link time, not compile time, which makes it slower to spot.

### `region 'dram0_0_seg' overflowed`

LVGL's heap is a static array competing with the draw buffer and the WiFi stack
for ~160 KB. Lower `LV_MEM_SIZE` in `lv_conf.h` (48 KB here) or shrink the draw
buffer.

### Sketch too big

Set *Tools → Partition Scheme → **Huge APP (3MB No OTA/1MB SPIFFS)***. The
relay build is ~1.23 MB, which is 94 % of the default partition — it fits, but
with nothing to spare.

---

## Display

### Backlight on, screen black

LVGL found no `lv_conf.h`. It belongs at `<Arduino>/libraries/lv_conf.h` —
*beside* the `lvgl` folder.

### Image offset by ~40 px, or colours wrong

Wrong TFT_eSPI setup. Easiest fix is LilyGO's own copy from
[Xinyuan-LilyGO/TTGO-T-Display](https://github.com/Xinyuan-LilyGO/TTGO-T-Display),
whose `User_Setup_Select.h` is already correct. With Library Manager's TFT_eSPI
you must edit that file yourself and uncomment
`Setup25_TTGO_T_Display.h` — the `CGRAM_OFFSET` in it is what the 135×240 panel
needs.

### Always boots to the QR setup screen

No SSID saved, or the left button reads LOW at power-on — a stuck button or
something pulling GPIO 0 down.

---

## Network

### Never leaves `NTP SYNC`

No route to the internet, or UDP 123 is blocked. This step cannot be skipped:
TLS validates certificate dates, so a device stuck at epoch 0 fails the
handshake without saying why.

### TLS handshake fails

Cloudflare rotated to a CA not pinned in `ca_certs.h`. Confirm with:

```bash
openssl s_client -connect peek-relay.peekesp.workers.dev:443 -showcerts </dev/null
```

Add the new root to `ca_certs.h`. The portal's **Verify TLS certificate** switch
gets a device running meanwhile, at the cost of making the connection
interceptable — treat it as temporary.
