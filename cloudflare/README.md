# PeekESP relay

A Cloudflare Worker that lets the dashboard work when **nothing can accept an
inbound connection** — the host behind CGNAT, on a network you don't administer,
or anywhere a port forward isn't an option.

```
DietPi  ──POST /ingest    (Bearer PUSH_TOKEN)──▶  Worker ──▶ Durable Object
ESP32   ──GET  /telemetry (Bearer READ_TOKEN)──▶  Worker ──▶ Durable Object
```

Both ends only ever dial **out** over HTTPS. Neither needs to be reachable.

## Deploy

```bash
npm install -g wrangler && wrangler login
```

Generate two independent tokens:

```bash
openssl rand -hex 24
```

```bash
npx wrangler secret put PUSH_TOKEN
npx wrangler secret put READ_TOKEN
npx wrangler deploy
```

They're **secrets, not vars** — `wrangler.toml` is committed, so nothing
sensitive belongs in it.

## Endpoints

| Method | Path | Token | Purpose |
|---|---|---|---|
| `POST` | `/ingest` | `PUSH_TOKEN` | The host publishes a telemetry snapshot |
| `GET` | `/telemetry` | `READ_TOKEN` | The ESP32 reads the latest one |
| `GET` | `/health` | none | Confirm the Worker deployed |

`/telemetry` returns the stored JSON with an added **`age_s`** — seconds since
the host last pushed. The firmware shows `STALE` above 30 s, which is what
separates "the agent died" from "the network is down". Without it a dead agent
looks like fresh data forever, the worst possible failure mode for a monitor.

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

12 checks over routing and auth — that each token is rejected on the other's
endpoint, that a malformed body is refused, that `age_s` is attached, that
missing secrets fail closed. Runs against a stubbed Durable Object, so it needs
no Cloudflare account and no `npm install`.

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

`PUSH_TOKEN` and `READ_TOKEN` deliberately **do not** go into GitHub. They're
Worker secrets held by Cloudflare, `wrangler deploy` leaves them untouched, and
keeping them out of CI means they never sit in a second system's secret store.

The deploy job targets a `production` environment, so you can attach required
reviewers to it in repo settings if you'd rather approve deploys by hand.
