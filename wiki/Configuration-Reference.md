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
| Host command line | The agent (Linux or Windows) | Every push |

> **The single most common mistake:** setting `MASTER_SECRET` (or `PUSH_TOKEN` /
> `READ_TOKEN`) as a *GitHub* secret. Those are **Cloudflare Worker** secrets.
> A GitHub secret never reaches the running Worker. If requests come back saying
> a secret is not set, this is why.

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

Add with **Type: Secret** (not Text). Plain text variables would work but stay
readable in the dashboard forever.

### Shared — one secret, any number of users

| Name | Value |
|---|---|
| `MASTER_SECRET` | `node cloudflare/mint.mjs --new-master` |

That's all the Worker needs. Every tenant's tokens are **derived** from it:

```
push token = HMAC-SHA256(MASTER_SECRET, "<stream>:push")  first 48 hex chars
read token = HMAC-SHA256(MASTER_SECRET, "<stream>:read")  first 48 hex chars
```

Nothing is stored, so onboarding is offline — the deployment is never touched:

```bash
MASTER_SECRET=... npm run mint -- alice --url https://peek-relay.peekesp.workers.dev
```

It prints that stream's two tokens plus the exact agent command and portal
settings to hand over. Their URLs become
`…/ingest/alice` and `…/telemetry/alice`, and their data lives in its own
Durable Object — invisible to every other stream.

> **`MASTER_SECRET` is the crown jewel.** Anyone holding it can mint tokens for
> any stream, including read tokens for streams that aren't theirs. It belongs
> in `wrangler secret put` and on whichever machine you mint from — nowhere
> else, and never in GitHub.

**Revoking one tenant:** tokens are a pure function of (master secret, stream),
so you can't revoke one in place. Move them to a new stream name — `alice` →
`alice-v2` — which changes both their tokens and leaves everyone else alone.
Rotating `MASTER_SECRET` invalidates every stream at once.

### Private — the simpler option when it's only you

| Name | Value |
|---|---|
| `PUSH_TOKEN` | A fresh random string — `openssl rand -hex 24` |
| `READ_TOKEN` | A **different** random string — run the command again |

Use hex, not base64: the Worker accepts a `?token=` fallback and base64's
`+ / =` would need URL-escaping there. Keep them ≤ 64 characters — the firmware
stores the read token in a 65-byte buffer. `-hex 24` gives 48, with margin;
`-hex 32` gives exactly 64 and leaves none.

They must differ. The Worker rejects each token on the other's endpoint, which
is the entire point: the ESP32 sits on a desk carrying the read token in flash,
so if the device is taken apart, that token still cannot push fabricated
telemetry.

These use the un-suffixed URLs, `/ingest` and `/telemetry`. **Private is not a
deprecated path** — it is the right choice when the relay is only ever yours.
Both modes can be active on the same deployment at once.

Secrets take effect immediately — no redeploy. `wrangler deploy` deliberately
never touches them, which is why you set them once and they survive every
future deploy.

---

## 3. ESP32 — the on-device setup portal

Hold the left button 1.5 s (or power on holding it), scan the QR code, then
browse to `192.168.4.1`.

| Field | Value |
|---|---|
| Network (SSID) | Your WiFi — pick from the dropdown |
| Password | Your WiFi passphrase |
| Transport | **Cloudflare relay** |
| Worker URL | `…/telemetry` (private) or `…/telemetry/<stream>` (shared) |
| Read token | The **read** token — never the push one |
| Verify TLS certificate | **Checked** |
| Poll seconds | `5` |

The WireGuard fields are ignored entirely when Transport is set to relay.

---

## 4. The host — the agent command

**Windows** uses `windows/peek-agent-win.py` (or the compiled
`peek-agent.exe`); flags and JSON are identical to the Linux agent. It reports
`cpu_temp_c: -1`, which the display renders as `--`, because Windows exposes no
temperature without a vendor driver.

```bash
python3 peek-agent.py --push https://peek-relay.peekesp.workers.dev/ingest --token PUSH_TOKEN_HERE
```

Shared mode — note the stream name on the end:

```bash
python3 peek-agent.py --push https://peek-relay.peekesp.workers.dev/ingest/alice --token ALICE_PUSH_TOKEN
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
| ESP32 setup portal | `…`**`/telemetry`** or `…`**`/telemetry/<stream>`** |
| Host agent `--push` | `…`**`/ingest`** or `…`**`/ingest/<stream>`** |

A stream's two URLs must use the **same** stream name, or the device reads a
slot nothing is writing to and sits at `NO LINK`.

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
