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

// Paired streams are exactly 16 hex characters, derived from a pairing code.
// Keeping them to their own shape means a paired stream can never collide with
// a minted one like "alice", and the two auth paths never have to guess which
// kind of stream they are looking at.
const PAIR_RE = /^[0-9a-f]{16}$/;

const TOKEN_HEX_LEN = 48;

// ---------------------------------------------------------------------------
//  Several machines, one pairing code
// ---------------------------------------------------------------------------
// A pairing code identifies a person, not a machine. Someone with a desktop, a
// laptop and a Pi wants all three on the one display, so the store keeps a slot
// per reported host instead of a single "latest" that they would overwrite in
// turn - which is what happened before, and looked exactly like a flapping
// agent.
const DEVICE_PREFIX = "dev_";

// Enough for a plausible household. The cap exists so a misconfigured fleet
// cannot grow this object without bound, and so the response stays inside what
// an ESP32 can parse.
const MAX_DEVICES = 6;

// A laptop that is shut overnight is still something you monitor, so this is
// deliberately generous: it is here to stop a renamed or retired host lingering
// forever, not to hide one that is merely asleep. Anything still listed carries
// age_s, so the display can say how stale it is rather than pretending.
const DEVICE_TTL_MS = 24 * 60 * 60 * 1000;

// The host name arrives from the wire and becomes a storage key. Keys are only
// ever compared against keys we wrote ourselves, but a control character or a
// 4 KB name still has no business in storage, and a name that renders as
// nothing would give the display a blank tab to swipe to.
function deviceKey(name) {
  const clean = String(name ?? "")
    .replace(/[^\x20-\x7E]/g, "")
    .trim()
    .slice(0, 32);
  return DEVICE_PREFIX + (clean || "host");
}

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

/**
 * The pairing derivation, in one place.
 *
 * The device shows a code; the device and the PC each turn it into the same
 * stream id and token pair locally. The code itself never leaves either
 * machine and this Worker never sees it - which is why pairing needs no
 * secrets configured here at all.
 *
 * This Worker does not use these values (paired streams authenticate by
 * trust-on-first-use); it is exported so the test suite can prove the
 * JavaScript and the Python agent agree byte for byte. A drift here would
 * mean the PC and the device silently derive different streams and never
 * meet, with every request looking perfectly valid.
 */
export async function derivePairing(code) {
  const c = String(code || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
  return {
    code: c,
    stream: (await sha256hex("peek-stream:" + c)).slice(0, 16),
    push: (await sha256hex("peek-push:" + c)).slice(0, TOKEN_HEX_LEN),
    read: (await sha256hex("peek-read:" + c)).slice(0, TOKEN_HEX_LEN),
  };
}

export async function sha256hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export class TelemetryStore {
  constructor(state) {
    this.state = state;
  }

  /**
   * Trust-on-first-use for paired streams.
   *
   * A pairing code never reaches this Worker - the device and the PC each
   * derive the stream id and both tokens from it locally. So there is nothing
   * here to check a token against until someone presents one, and the first
   * presenter for each role claims it. Everyone afterwards must match.
   *
   * What stops a stranger claiming your stream is that they would have to know
   * the stream id, and that is 16 hex characters derived from a code with ~50
   * bits of entropy that is only ever shown on your device's screen. Only the
   * hash is stored, so a dump of this object does not yield working tokens.
   */
  async claimOrVerify(role, token) {
    const key = "auth_" + role;
    const presented = await sha256hex(token);
    const stored = await this.state.storage.get(key);
    if (stored === undefined) {
      await this.state.storage.put(key, presented);
      return true;
    }
    // Both are our own SHA-256 hex, so length is fixed and a plain compare
    // leaks nothing an attacker could not compute themselves.
    return stored === presented;
  }

  async fetch(request) {
    const role = request.headers.get("X-Peek-Role");
    if (role) {
      const token = request.headers.get("X-Peek-Token") || "";
      if (!(await this.claimOrVerify(role, token))) {
        return json({ error: "unauthorized" }, 401);
      }
    }

    if (request.method === "POST") {
      const text = await request.text();
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch {
        return json({ error: "body is not valid JSON" }, 400);
      }
      const now = Date.now();
      await this.state.storage.put(deviceKey(parsed.host), { payload: parsed, at: now });
      // Written by a Worker older than per-host slots. Once any host has its
      // own slot the single "latest" is stale by definition, and leaving it
      // would resurrect one machine's numbers if every slot later expired.
      await this.state.storage.delete(["latest", "at"]);
      await this.prune(now);
      return json({ ok: true });
    }

    const now = Date.now();
    const { list: devices, newest } = await this.devices(now);

    if (!devices.length) {
      // An object last written by the previous Worker still holds "latest" and
      // nothing under dev_. Serving it means an upgrade does not blank the
      // display for the few seconds until the next push arrives.
      const [latest, at] = await Promise.all([
        this.state.storage.get("latest"),
        this.state.storage.get("at"),
      ]);
      if (!latest) {
        return json({ error: "no telemetry received yet" }, 503);
      }
      const legacy = { ...latest, age_s: Math.round((now - (at || 0)) / 1000) };
      return json({ ...legacy, devices: [legacy], device_count: 1 });
    }

    // The freshest host is repeated at the top level so that a device flashed
    // before any of this existed keeps working: it reads the fields it knows
    // and never looks at the array.
    const freshest = devices.find((d) => d.host === newest) || devices[0];

    // age_s lets the display distinguish "the host stopped reporting" from
    // "the network is down" - without it a dead agent looks like fresh data
    // forever, which is the worst possible failure for a monitor.
    return json({ ...freshest, devices, device_count: devices.length });
  }

  /** { list, newest } - live slots in a stable order, plus which one is current. */
  async devices(now) {
    const rows = [...(await this.state.storage.list({ prefix: DEVICE_PREFIX }))]
      .filter(([, v]) => v && now - (v.at || 0) <= DEVICE_TTL_MS)
      .sort((a, b) => (b[1].at || 0) - (a[1].at || 0))
      .slice(0, MAX_DEVICES);

    // Recency is decided on the stored millisecond rather than on age_s, which
    // is rounded to a whole second: three agents pushing on the same tick would
    // tie, and "the freshest host" would quietly become "whichever sorted
    // first alphabetically".
    const newest = rows.length ? rows[0][0].slice(DEVICE_PREFIX.length) : null;

    const list = rows
      .map(([key, v]) => ({
        ...v.payload,
        host: key.slice(DEVICE_PREFIX.length),
        age_s: Math.round((now - (v.at || 0)) / 1000),
      }))
      // Sorted by name for the response, not by recency: the display swipes
      // through this array, and an order that reshuffles whenever a push lands
      // would move a machine out from under someone's thumb.
      .sort((a, b) => (a.host < b.host ? -1 : a.host > b.host ? 1 : 0));

    return { list, newest };
  }

  /** Drop expired slots, then the stalest of whatever is over the cap. */
  async prune(now) {
    const rows = [...(await this.state.storage.list({ prefix: DEVICE_PREFIX }))];
    const expired = rows.filter(([, v]) => !v || now - (v.at || 0) > DEVICE_TTL_MS);
    const live = rows
      .filter(([, v]) => v && now - (v.at || 0) <= DEVICE_TTL_MS)
      .sort((a, b) => (b[1].at || 0) - (a[1].at || 0));

    const doomed = [...expired, ...live.slice(MAX_DEVICES)].map(([k]) => k);
    if (doomed.length) {
      await this.state.storage.delete(doomed);
    }
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
    } else if (PAIR_RE.test(stream)) {
      // ---- paired: the device and the PC derived this from a pairing code ----
      // No master secret involved: the Durable Object claims each role's token
      // on first use. Checked before STREAM_RE because a 16-hex name would
      // also satisfy it, and these two namespaces must not overlap.
      if (!token) return json({ error: "unauthorized" }, 401);
      const stub = env.TELEMETRY.get(env.TELEMETRY.idFromName(`pair:${stream}`));
      const init = {
        headers: { "X-Peek-Role": wantsWrite ? "push" : "read", "X-Peek-Token": token },
      };
      return wantsWrite
        ? stub.fetch("https://do/", { ...init, method: "POST", body: await request.text() })
        : stub.fetch("https://do/", init);
    } else {
      // ---- shared: one stream per tenant, tokens minted from MASTER_SECRET ----
      if (!streamsReady) {
        return json({
          error: "MASTER_SECRET is not set, so named streams are disabled",
          hint: "Either 'wrangler secret put MASTER_SECRET', or pair a device - "
              + "pairing needs no secrets at all.",
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
