# PeekESP relay

A Cloudflare Worker that lets the dashboard work when **nothing can accept an
inbound connection** — the host behind CGNAT, on a network you don't administer,
or anywhere a port forward isn't an option.

```
host   ──POST /ingest/<stream>    (Bearer push token)──▶  Worker ──▶ Durable Object
ESP32  ──GET  /telemetry/<stream> (Bearer read token)──▶  Worker ──▶ Durable Object
```

Both ends only ever dial **out** over HTTPS. Neither needs to be reachable.

One deployment serves any number of independent `<stream>`s, so other people can
use your relay without seeing your telemetry or you seeing theirs.

## Deploy

```bash
npm install -g wrangler && wrangler login
```

```bash
node mint.mjs --new-master
npx wrangler secret put MASTER_SECRET
npx wrangler deploy
```

That is the whole deployment. Per-tenant tokens are derived from
`MASTER_SECRET`, so there is nothing else to set — see **Two modes** below.

`MASTER_SECRET` is a **secret, not a var** — `wrangler.toml` is committed, so
nothing sensitive belongs in it.

> **These are Cloudflare Worker secrets, not GitHub Actions secrets.** They are
> separate stores and a GitHub secret never reaches the running Worker. If
> requests come back saying a secret is not set, the deploy succeeded and this
> is the step that's missing. `wrangler deploy` uploads code
> only and deliberately leaves Worker secrets alone, which is why you set them
> once and they survive every later deploy. You can also set them in the
> dashboard: **Workers & Pages → your worker → Settings → Variables and Secrets
> → Add → type Secret**, which takes effect without a redeploy.

## Two modes

### Multi-tenant (recommended)

One deployment, any number of independent streams. Each gets its own Durable
Object, so nobody sees anyone else's telemetry.

```bash
node mint.mjs --new-master
npx wrangler secret put MASTER_SECRET
```

Then onboard someone — **entirely offline**, the deployment is never touched:

```bash
MASTER_SECRET=... npm run mint -- alice --url https://peek-relay.you.workers.dev
```

It prints their push token, read token, and the exact agent command and portal
settings to hand over.

Tokens are not stored anywhere. They are derived:

```
push token = HMAC-SHA256(MASTER_SECRET, "<stream>:push")  truncated to 48 hex
read token = HMAC-SHA256(MASTER_SECRET, "<stream>:read")  truncated to 48 hex
```

The Worker recomputes and compares. No registry, no signup endpoint, no
database, nothing to keep in sync.

48 hex characters rather than the full 64 because the firmware stores the read
token in a 65-byte buffer. 192 bits of an HMAC is ample.

**Revoking one tenant:** tokens are a pure function of (master secret, stream),
so a single tenant cannot be revoked in place. Move them to a new stream name —
`alice` → `alice-v2` — which changes both their tokens and leaves every other
stream untouched. Rotating `MASTER_SECRET` invalidates everything at once.

**The master secret is the crown jewel.** Anyone holding it can mint tokens for
any stream, including read tokens for streams that are not theirs. It belongs in
`wrangler secret put` and on whichever machine you mint from — nowhere else, and
never in GitHub.

### Single-tenant (legacy)

`/ingest` and `/telemetry` with no stream still work against `PUSH_TOKEN` and
`READ_TOKEN`, so an existing deployment keeps running unchanged. Both modes can
be active at once.

## Endpoints

| Method | Path | Token | Purpose |
|---|---|---|---|
| `POST` | `/ingest/<stream>` | derived push | A tenant's host publishes |
| `GET` | `/telemetry/<stream>` | derived read | A tenant's device reads |
| `POST` | `/ingest` | `PUSH_TOKEN` | Single-tenant publish |
| `GET` | `/telemetry` | `READ_TOKEN` | Single-tenant read |
| `GET` | `/health` | none | Confirm deploy — answers with no secrets set, reporting `configured` and `streams` |

Stream names are lowercase `a-z 0-9 - _`, starting alphanumeric, max 32 chars.
Lowercase only so `Alice` and `alice` cannot become two different streams.

`/telemetry` returns the stored JSON with an added **`age_s`** — seconds since
that stream last received a push. The firmware shows `STALE` above 30 s, which
separates "the agent died" from "the network is down". Without it a dead agent
looks like fresh data forever, the worst failure mode for a monitor.

## Why two tokens

The ESP32 sits on a desk with its token in flash. If someone walks off with it,
only the **reader** leaks — that token cannot push fabricated telemetry into
your dashboard. One shared token would give away both.

Tokens are compared in constant time; `===` leaks the matching-prefix length
through timing, which is enough to walk a token out a byte at a time.

## Why a Durable Object, not KV

Workers KV's free tier allows **1,000 writes/day**. A 5-second push interval is
**17,280** — it would fail partway through the first day. Durable Objects have
no such per-day write cap, and the free plan covers the SQLite-backed ones this
uses.

Request volume is ~35k/day (both ends, every 5 s) against a 100k/day free
allowance, so the whole thing fits comfortably in the free tier.

If `wrangler deploy` says Durable Objects aren't available on your account,
that's the thing to check — the `new_sqlite_classes` migration in
`wrangler.toml` is what makes it free-plan eligible.

## Test

```bash
npm test
```

33 checks over routing, auth and isolation: that each token is rejected on the
other's endpoint, that one stream's token cannot open another's, that streams
have genuinely separate storage, that a malformed body is refused, that `age_s`
is attached, and that missing secrets fail closed.

It also asserts that `mint.mjs` (node:crypto) and the Worker (WebCrypto) derive
**byte-identical** tokens — if those two ever drift, every minted token would be
rejected by the deployment it was minted for.

Runs against a stubbed Durable Object, so it needs no Cloudflare account and no
`npm install`.

## CI

[`.github/workflows/cloudflare-worker.yml`](../.github/workflows/cloudflare-worker.yml)
tests every change to `cloudflare/**`, deploys on merge to `main`, and pings
`/health` weekly. Path-filtered, so firmware commits don't redeploy the Worker.

One-time repo configuration under
**Settings → Secrets and variables → Actions**:

| Name | Kind | Required | What |
|---|---|---|---|
| `CLOUDFLARE_API_TOKEN` | secret | **yes** | [Create one](https://dash.cloudflare.com/profile/api-tokens) with the *Edit Cloudflare Workers* template |
| `CLOUDFLARE_ACCOUNT_ID` | secret | if your token spans several accounts | From the Workers dashboard sidebar |
| `WORKER_URL` | variable | no | e.g. `https://peek-relay.you.workers.dev` — enables the health check; skipped silently when unset |

`MASTER_SECRET` (and `PUSH_TOKEN` / `READ_TOKEN`) deliberately **do not** go
into GitHub. They're Worker secrets held by Cloudflare, `wrangler deploy` leaves
them untouched, and keeping them out of CI means they never sit in a second
system's secret store.

The deploy job targets a `production` environment, so you can attach required
reviewers to it in repo settings if you'd rather approve deploys by hand.
