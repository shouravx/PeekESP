# Configuration Reference

Every value this project needs, where it lives, and what it should be set to.

There are **four separate places** configuration lives. They are not connected,
and a value set in one never reaches another. Most setup problems are a value
put in the wrong one.

| Store | Who reads it | When |
|---|---|---|
| GitHub → Actions secrets | The CI workflow | Only while deploying |
| Cloudflare → Worker secrets | The Worker itself | Every request, at runtime |
| ESP32 setup portal | The firmware | Every poll |
| DietPi command line | The agent | Every push |

> **The single most common mistake:** setting `PUSH_TOKEN` / `READ_TOKEN` as
> *GitHub* secrets. They are **Cloudflare Worker** secrets. A GitHub secret
> never reaches the running Worker. If every endpoint returns
> `{"error":"PUSH_TOKEN and READ_TOKEN secrets are not set"}`, this is why.

---

## 1. GitHub — Settings → Secrets and variables → Actions

### Secrets tab

| Name | Required | Value |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | **Yes** | Create at [dash.cloudflare.com/profile/api-tokens](https://dash.cloudflare.com/profile/api-tokens) → **Create Token** → use the **Edit Cloudflare Workers** template. Copy the token once; it is never shown again. |
| `CLOUDFLARE_ACCOUNT_ID` | Only if your token covers several accounts | Cloudflare dashboard → Workers & Pages → the **Account ID** in the right-hand sidebar. A 32-character hex string. |

### Variables tab

> Variables, **not** secrets. This one is not sensitive and needs to be readable
> in logs.

| Name | Required | Value |
|---|---|---|
| `WORKER_URL` | No, but the weekly health check silently does nothing without it | `https://peek-relay.peekesp.workers.dev` |

**Base URL only** — no path, no trailing slash needed. The workflow appends
`/health` itself. Putting `/telemetry` here makes the health check request
`/telemetry/health`, which 404s.

---

## 2. Cloudflare — Workers & Pages → `peek-relay` → Settings → Variables and Secrets

Add each with **Type: Secret** (not Text). Plain text variables would work
functionally but would be readable in the dashboard forever.

| Name | Value |
|---|---|
| `PUSH_TOKEN` | A fresh random string — `openssl rand -hex 24` |
| `READ_TOKEN` | A **different** random string — run the command again |

They must differ. The Worker rejects each token on the other's endpoint, which
is the entire point: the ESP32 sits on a desk carrying `READ_TOKEN` in flash, so
if the device is taken apart, that token still cannot push fabricated telemetry.

Secrets take effect immediately — no redeploy. `wrangler deploy` deliberately
never touches them, which is why you set them once and they survive every
future deploy.

Or from a terminal:

```bash
cd cloudflare
npx wrangler secret put PUSH_TOKEN
npx wrangler secret put READ_TOKEN
```

---

## 3. ESP32 — the on-device setup portal

Hold the left button 1.5 s (or power on holding it), scan the QR code, then
browse to `192.168.4.1`.

| Field | Value |
|---|---|
| Network (SSID) | Your WiFi — pick from the dropdown |
| Password | Your WiFi passphrase |
| Transport | **Cloudflare relay** |
| Worker URL | `https://peek-relay.peekesp.workers.dev/telemetry` |
| Read token | The **`READ_TOKEN`** value — never `PUSH_TOKEN` |
| Verify TLS certificate | **Checked** |
| Poll seconds | `5` |

The WireGuard fields are ignored entirely when Transport is set to relay.

---

## 4. DietPi — the agent command

```bash
python3 peek-agent.py --push https://peek-relay.peekesp.workers.dev/ingest --token PUSH_TOKEN_HERE
```

Or keep the token out of your shell history:

```bash
export PEEK_PUSH_TOKEN=...
python3 peek-agent.py --push https://peek-relay.peekesp.workers.dev/ingest
```

Add `--no-serve` if you do not also want the local `:8080` endpoint.

---

## The three URLs are not the same

This trips people up, because all three look like "the Worker URL":

| Used by | URL |
|---|---|
| GitHub `WORKER_URL` variable | `https://peek-relay.peekesp.workers.dev` |
| ESP32 setup portal | `https://peek-relay.peekesp.workers.dev`**`/telemetry`** |
| DietPi agent `--push` | `https://peek-relay.peekesp.workers.dev`**`/ingest`** |

---

## Direct (WireGuard) transport instead

If you are not using the relay, the portal fields are these instead. Every value
is printed by `dietpi-wireguard-setup.sh`.

| Field | Value | From |
|---|---|---|
| Transport | **Direct** | |
| This device's tunnel IP | `10.10.44.2` | fixed by the setup script |
| Private key (this device) | 44-char base64 | `/etc/wireguard/ttgo-dashboard.key` |
| Peer public key (DietPi) | 44-char base64 | `/etc/wireguard/dietpi_wg.pub` |
| Endpoint host | your public IP or DDNS name | your router / DDNS provider |
| Endpoint port | `51820` | fixed by the setup script |
| Host | the DietPi's `100.x.x.x` | `tailscale ip -4` |
| Port | `8080` | |
| Path | `/telemetry` | |

> Use a literal `100.x.x.x` address. **MagicDNS names will not resolve** on the
> ESP32 — it has no route to Tailscale's resolver.
