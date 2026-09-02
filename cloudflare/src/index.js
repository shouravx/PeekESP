/**
 * PeekESP relay - a Cloudflare Worker that sits between the monitored host
 * and the ESP32 so neither side needs an inbound connection.
 *
 *   DietPi  --POST /ingest    (Bearer PUSH_TOKEN)--> Worker --> Durable Object
 *   ESP32   --GET  /telemetry (Bearer READ_TOKEN)--> Worker --> Durable Object
 *
 * Both ends only ever dial out over HTTPS, which is what makes this work
 * behind CGNAT, on a network you do not control, or anywhere a port forward
 * is impossible.
 *
 * Two tokens rather than one on purpose: the ESP32 carries its token in
 * flash and sits on a desk, so if it is ever pulled apart the reader token
 * is all that leaks - it cannot be used to push fabricated telemetry.
 *
 * State lives in a Durable Object rather than Workers KV because KV's free
 * tier allows 1,000 writes/day and a 5-second push interval is 17,280.
 */

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

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

    if (!env.PUSH_TOKEN || !env.READ_TOKEN) {
      return json({ error: "PUSH_TOKEN and READ_TOKEN secrets are not set" }, 500);
    }

    const id = env.TELEMETRY.idFromName("singleton");
    const stub = env.TELEMETRY.get(id);
    const token = bearer(request);

    if (url.pathname === "/ingest" && request.method === "POST") {
      if (!tokenMatches(token, env.PUSH_TOKEN)) return json({ error: "unauthorized" }, 401);
      return stub.fetch("https://do/", { method: "POST", body: await request.text() });
    }

    if (url.pathname === "/telemetry" && request.method === "GET") {
      if (!tokenMatches(token, env.READ_TOKEN)) return json({ error: "unauthorized" }, 401);
      return stub.fetch("https://do/");
    }

    // Deliberately unauthenticated, and deliberately says nothing about
    // whether any telemetry exists - it is only here so you can confirm the
    // Worker deployed without handing out a token.
    if (url.pathname === "/health") return json({ ok: true });

    return json({ error: "not found" }, 404);
  },
};
