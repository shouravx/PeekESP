/**
 * PeekESP relay - a Cloudflare Worker that sits between the monitored host
 * and the ESP32 so neither side needs an inbound connection.
 *
 *   host   --POST /ingest/<stream>    (Bearer push token)--> Worker --> DO
 *   ESP32  --GET  /telemetry/<stream> (Bearer read token)--> Worker --> DO
 *
 * Both ends only ever dial out over HTTPS, which is what makes this work
 * behind CGNAT, on a network you do not control, or anywhere a port forward
 * is impossible.
 *
 * ---------------------------------------------------------------------------
 * MULTI-TENANCY
 * ---------------------------------------------------------------------------
 * Each <stream> is an independent slot with its own Durable Object, so several
 * people can share one deployment without seeing each other's telemetry.
 *
 * Tokens are not stored anywhere. They are derived:
 *
 *   push token = HMAC-SHA256(MASTER_SECRET, "<stream>:push")  truncated to 48 hex
 *   read token = HMAC-SHA256(MASTER_SECRET, "<stream>:read")  truncated to 48 hex
 *
 * The Worker recomputes and compares, so onboarding someone is an offline
 * operation - run `npm run mint -- <stream>` and hand over the result. No
 * registry, no signup endpoint, no database, and nothing to keep in sync.
 *
 * The trade: tokens are a pure function of (master secret, stream), so a
 * single tenant cannot be revoked in isolation. To cut one off, move them to
 * a new stream name - `alice` -> `alice-v2` - which changes both their tokens
 * and leaves everyone else untouched. Rotating MASTER_SECRET invalidates
 * every stream at once.
 *
 * 48 hex characters, not the full 64, because the firmware stores the read
 * token in a 65-byte buffer. 192 bits of an HMAC is ample.
 *
 * PRIVATE mode: /ingest and /telemetry with no stream run against PUSH_TOKEN /
 * READ_TOKEN. Not a deprecated path - it is the right choice when the relay is
 * only ever yours. Both modes can be active on one deployment.
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

// Lowercase only: allowing both cases would make "Alice" and "alice" separate
// streams, which is a confusing way to lose your telemetry.
const STREAM_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
const TOKEN_HEX_LEN = 48;

function json(body, status = 200, extra = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...CORS, ...extra },
  });
}

/**
 * Constant-time string compare. A naive === leaks the length of the matching
 * prefix through timing, which is enough to walk a token out one byte at a
 * time given enough requests.
 */
function tokenMatches(given, expected) {
  if (typeof given !== "string" || typeof expected !== "string") return false;
  if (given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < given.length; i++) diff |= given.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

function bearer(request) {
  const header = request.headers.get("Authorization") || "";
  if (header.startsWith("Bearer ")) return header.slice(7).trim();
  // Query-string fallback: some constrained clients cannot set headers.
  // Discouraged - it ends up in logs - but the ESP32 does send the header.
  return new URL(request.url).searchParams.get("token") || "";
}

/**
 * Derive a stream's token. Must stay byte-identical to mint.mjs, which does
 * the same thing with node:crypto - there is a test asserting that.
 */
export async function deriveToken(masterSecret, stream, role) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(masterSecret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(`${stream}:${role}`));
  return [...new Uint8Array(sig)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, TOKEN_HEX_LEN);
}

export class TelemetryStore {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    if (request.method === "POST") {
      const text = await request.text();
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch {
        return json({ error: "body is not valid JSON" }, 400);
      }
      await this.state.storage.put({ latest: parsed, at: Date.now() });
      return json({ ok: true });
    }

    const [latest, at] = await Promise.all([
      this.state.storage.get("latest"),
      this.state.storage.get("at"),
    ]);

    if (!latest) {
      return json({ error: "no telemetry received yet" }, 503);
    }

    // age_s lets the display distinguish "the host stopped reporting" from
    // "the network is down" - without it a dead agent looks like fresh data
    // forever, which is the worst possible failure for a monitor.
    return json({ ...latest, age_s: Math.round((Date.now() - (at || 0)) / 1000) });
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }

    const privateReady = Boolean(env.PUSH_TOKEN && env.READ_TOKEN);   // just you
    const streamsReady  = Boolean(env.MASTER_SECRET);                 // shared

    // /health answers BEFORE any secret gate on purpose. It exists to confirm
    // a deploy landed, and "deployed but not yet configured" is a different
    // problem from "not deployed at all" - collapsing both into a 500 sends
    // you looking at the wrong thing. It says nothing about whether any
    // telemetry exists, and an unconfigured Worker rejects every real request
    // anyway, so reporting the flags gives nothing away.
    if (url.pathname === "/health") {
      return json({
        ok: true,
        configured: privateReady,
        streams: streamsReady,
        ...(privateReady || streamsReady ? {} : {
          hint: "set PUSH_TOKEN + READ_TOKEN (private) or MASTER_SECRET " +
                "(shared, multi-stream) with: wrangler secret put",
        }),
      });
    }

    const parts = url.pathname.split("/").filter(Boolean);
    const route = parts[0];
    const stream = parts[1];

    if (parts.length > 2 || (route !== "ingest" && route !== "telemetry")) {
      return json({ error: "not found" }, 404);
    }

    const wantsWrite = route === "ingest";
    if (wantsWrite !== (request.method === "POST")) {
      return json({
        error: wantsWrite ? "/ingest takes POST" : "/telemetry takes GET",
      }, 405);
    }

    const token = bearer(request);
    let doName;

    if (stream === undefined) {
      // ---- private: one host, one device ----
      if (!privateReady) {
        return json({
          error: "PUSH_TOKEN and READ_TOKEN secrets are not set",
          hint: "These are Cloudflare Worker secrets, not GitHub Actions secrets. " +
                "Set them with 'wrangler secret put PUSH_TOKEN' (and READ_TOKEN), or use " +
                "the shared form: set MASTER_SECRET and call /ingest/<stream>.",
        }, 500);
      }
      const expected = wantsWrite ? env.PUSH_TOKEN : env.READ_TOKEN;
      if (!tokenMatches(token, expected)) return json({ error: "unauthorized" }, 401);
      doName = "singleton";
    } else {
      // ---- shared: one stream per tenant ----
      if (!streamsReady) {
        return json({
          error: "MASTER_SECRET is not set, so per-stream URLs are disabled",
          hint: "wrangler secret put MASTER_SECRET",
        }, 500);
      }
      if (!STREAM_RE.test(stream)) {
        return json({
          error: "invalid stream name",
          hint: "lowercase letters, digits, - and _, starting alphanumeric, max 32 chars",
        }, 400);
      }
      const expected = await deriveToken(env.MASTER_SECRET, stream, wantsWrite ? "push" : "read");
      // Same 401 whether the stream exists or the token is simply wrong -
      // otherwise the response tells an attacker which stream names are real.
      if (!tokenMatches(token, expected)) return json({ error: "unauthorized" }, 401);
      doName = `stream:${stream}`;
    }

    const stub = env.TELEMETRY.get(env.TELEMETRY.idFromName(doName));
    return wantsWrite
      ? stub.fetch("https://do/", { method: "POST", body: await request.text() })
      : stub.fetch("https://do/");
  },
};
