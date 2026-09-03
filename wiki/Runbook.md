# Runbook

Bring-up in the order that makes failures easy to localise. Each step has a
check that must pass before the next one is worth attempting.

Values referenced here are defined in
**[Configuration Reference](Configuration-Reference)**.

---

### 1. Worker deployed

```bash
curl https://peek-relay.peekesp.workers.dev/health
```

| Response | Meaning |
|---|---|
| `{"ok":true,"configured":true}` | Ready — go to step 2 |
| `{"ok":true,"configured":false,...}` | Deployed, secrets not set — do step 1b |
| Connection error / 1101 | Not deployed. Check the Actions tab. |

### 1b. Set the Worker secrets

Cloudflare → Workers & Pages → `peek-relay` → Settings → Variables and Secrets →
Add, type **Secret**, for `PUSH_TOKEN` and `READ_TOKEN`. Re-run the check above
until `configured` is `true`.

### 2. Auth works

```bash
curl -H "Authorization: Bearer READ_TOKEN_HERE" https://peek-relay.peekesp.workers.dev/telemetry
```

| Response | Meaning |
|---|---|
| `503 no telemetry received yet` | **Correct at this stage.** Auth passed, store is empty. |
| `401 unauthorized` | Wrong token, or you used `PUSH_TOKEN` |
| `200` with data | Something is already pushing |

A 503 here is success. It is the cleanest possible confirmation that the token
is right and the Worker is healthy.

### 3. Host is pushing

On the DietPi:

```bash
python3 peek-agent.py --push https://peek-relay.peekesp.workers.dev/ingest --token PUSH_TOKEN_HERE
```

Re-run the step 2 curl. You should now get real JSON with `age_s` under 10.
If `age_s` climbs past 30 the agent has stopped — the ESP32 will show `STALE`.

Make it permanent:

```bash
sudo tee /etc/systemd/system/peek-agent.service >/dev/null <<'EOF'
[Unit]
Description=PeekESP telemetry agent
After=network-online.target
Wants=network-online.target

[Service]
Environment=PEEK_PUSH_TOKEN=PUSH_TOKEN_HERE
ExecStart=/usr/bin/python3 /usr/local/bin/peek-agent --push https://peek-relay.peekesp.workers.dev/ingest
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload && sudo systemctl enable --now peek-agent
```

```bash
sudo systemctl status peek-agent --no-pager
```

### 4. Device configured

Setup portal → Transport **Cloudflare relay**, Worker URL ending in
`/telemetry`, and the **read** token. Save & Reboot.

Expect `LINK OK` within about 15 seconds of boot: WiFi, then NTP, then the first
HTTPS request.

---

## Reading the device

The bottom-right corner is the whole state machine.

| Shown | Meaning |
|---|---|
| `BOOT` | Just powered on |
| `WIFI...` | Associating |
| `NTP SYNC` | Getting the clock. **Required** — TLS validates certificate dates, so a device at epoch 0 cannot connect |
| `LINK OK` | Fresh data |
| `STALE` | Relay reachable, but the host stopped pushing more than 30 s ago |
| `NO LINK` | Request failing |

`STALE` versus `NO LINK` is the useful distinction: `STALE` means the network is
fine and the *agent* died; `NO LINK` means the device cannot reach the relay.

Serial at **115200** gives the reason behind `NO LINK`.

---

## Buttons

| Action | Effect |
|---|---|
| Left, tap | Refresh now |
| Left, hold 1.5 s | Reboot into setup mode |
| Left, held at power-on | Boot straight into setup mode — the way back in after a typo'd WiFi password |
| Right | Cycle brightness, remembered across reboots |

---

## Rotating a token

1. Set the new value in Cloudflare (takes effect immediately).
2. Update the consumer — the agent's `--token`, or the portal's read token.

Rotating `READ_TOKEN` only affects the ESP32; rotating `PUSH_TOKEN` only affects
the agent. There is no deploy involved either way, and the two are independent.
