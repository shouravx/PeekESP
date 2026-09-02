import worker, { TelemetryStore } from "../src/index.js";

// --- stub the Durable Object runtime ---------------------------------------
class FakeStorage {
  constructor() { this.map = new Map(); }
  async put(a, b) {
    if (typeof a === "object") for (const [k, v] of Object.entries(a)) this.map.set(k, v);
    else this.map.set(a, b);
  }
  async get(k) { return this.map.get(k); }
}

const store = new TelemetryStore({ storage: new FakeStorage() });
const env = {
  PUSH_TOKEN: "push-secret-aaaaaaaaaaaaaaaa",
  READ_TOKEN: "read-secret-bbbbbbbbbbbbbbbb",
  TELEMETRY: {
    idFromName: () => "singleton",
    get: () => ({ fetch: (u, init) => store.fetch(new Request(u, init)) }),
  },
};

const req = (path, { method = "GET", token, body } = {}) =>
  new Request("https://relay.example" + path, {
    method,
    headers: token ? { Authorization: "Bearer " + token } : {},
    body,
  });

let pass = 0, fail = 0;
async function check(name, res, expectStatus, bodyTest) {
  const status = res.status;
  let json = null;
  try { json = await res.clone().json(); } catch {}
  const ok = status === expectStatus && (!bodyTest || bodyTest(json));
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}  -> ${status}${ok ? "" : "  got " + JSON.stringify(json)}`);
  ok ? pass++ : fail++;
}

const SAMPLE = JSON.stringify({ host: "dietpi", cpu_percent: 12.5, ram_percent: 43.2 });

await check("health needs no token",
  await worker.fetch(req("/health"), env), 200,
  (j) => j && j.ok === true && j.configured === true);

// /health must survive a missing-secrets deploy: it is the only way to tell
// "deployed but unconfigured" from "not deployed", and CI pings it.
await check("health still answers 200 with secrets unset, and says so",
  await worker.fetch(req("/health"), { ...env, PUSH_TOKEN: "", READ_TOKEN: "" }), 200,
  (j) => j && j.ok === true && j.configured === false && typeof j.hint === "string");

await check("telemetry before any push -> 503",
  await worker.fetch(req("/telemetry", { token: env.READ_TOKEN }), env), 503);

await check("telemetry with NO token -> 401",
  await worker.fetch(req("/telemetry"), env), 401);

await check("telemetry with WRONG token -> 401",
  await worker.fetch(req("/telemetry", { token: "read-secret-XXXXXXXXXXXXXXXX" }), env), 401);

await check("telemetry with the PUSH token -> 401 (tokens are not interchangeable)",
  await worker.fetch(req("/telemetry", { token: env.PUSH_TOKEN }), env), 401);

await check("ingest with NO token -> 401",
  await worker.fetch(req("/ingest", { method: "POST", body: SAMPLE }), env), 401);

await check("ingest with the READ token -> 401 (reader cannot forge data)",
  await worker.fetch(req("/ingest", { method: "POST", token: env.READ_TOKEN, body: SAMPLE }), env), 401);

await check("ingest with the PUSH token -> 200",
  await worker.fetch(req("/ingest", { method: "POST", token: env.PUSH_TOKEN, body: SAMPLE }), env), 200);

await check("ingest of non-JSON -> 400",
  await worker.fetch(req("/ingest", { method: "POST", token: env.PUSH_TOKEN, body: "not json" }), env), 400);

await check("telemetry now returns the data plus age_s",
  await worker.fetch(req("/telemetry", { token: env.READ_TOKEN }), env), 200,
  (j) => j && j.host === "dietpi" && j.cpu_percent === 12.5 && typeof j.age_s === "number");

await check("unknown path -> 404",
  await worker.fetch(req("/nope", { token: env.READ_TOKEN }), env), 404);

await check("missing secrets -> 500",
  await worker.fetch(req("/telemetry"), { ...env, READ_TOKEN: "" }), 500);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
