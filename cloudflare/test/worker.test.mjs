import worker, { TelemetryStore, deriveToken, derivePairing } from "../src/index.js";
import { deriveTokenNode } from "../mint.mjs";

// --- stub the Durable Object runtime ---------------------------------------
class FakeStorage {
  constructor() { this.map = new Map(); }
  async put(a, b) {
    if (typeof a === "object") for (const [k, v] of Object.entries(a)) this.map.set(k, v);
    else this.map.set(a, b);
  }
  async get(k) { return this.map.get(k); }
}

// One store per Durable Object name, so cross-stream isolation is actually
// exercised rather than assumed.
const stores = new Map();
function storeFor(name) {
  if (!stores.has(name)) stores.set(name, new TelemetryStore({ storage: new FakeStorage() }));
  return stores.get(name);
}

const MASTER = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

const env = {
  PUSH_TOKEN: "push-secret-aaaaaaaaaaaaaaaa",
  READ_TOKEN: "read-secret-bbbbbbbbbbbbbbbb",
  MASTER_SECRET: MASTER,
  TELEMETRY: {
    idFromName: (n) => n,
    get: (n) => ({ fetch: (u, init) => storeFor(n).fetch(new Request(u, init)) }),
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
function assert(name, cond) {
  console.log(`${cond ? "PASS" : "FAIL"}  ${name}`);
  cond ? pass++ : fail++;
}

const SAMPLE = JSON.stringify({ host: "dietpi", cpu_percent: 12.5, ram_percent: 43.2 });

// ===========================================================================
//  health
// ===========================================================================
await check("health needs no token",
  await worker.fetch(req("/health"), env), 200,
  (j) => j && j.ok === true && j.configured === true && j.streams === true);

await check("health still answers 200 with every secret unset, and says so",
  await worker.fetch(req("/health"), { ...env, PUSH_TOKEN: "", READ_TOKEN: "", MASTER_SECRET: "" }), 200,
  (j) => j && j.ok === true && j.configured === false && j.streams === false
             && typeof j.hint === "string");

// ===========================================================================
//  legacy single-tenant
// ===========================================================================
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

await check("missing legacy secrets -> 500",
  await worker.fetch(req("/telemetry"), { ...env, READ_TOKEN: "" }), 500);

// ===========================================================================
//  derivation: the Worker and the mint script must agree exactly, or a minted
//  token would be rejected by the deployment it was minted for
// ===========================================================================
for (const [stream, role] of [["alice", "push"], ["alice", "read"], ["bob-2", "read"]]) {
  const a = await deriveToken(MASTER, stream, role);
  const b = deriveTokenNode(MASTER, stream, role);
  assert(`mint.mjs and worker derive the same ${stream}:${role} token`, a === b && a.length === 48);
}

const alicePush = await deriveToken(MASTER, "alice", "push");
const aliceRead = await deriveToken(MASTER, "alice", "read");
const bobPush = await deriveToken(MASTER, "bob", "push");
const bobRead = await deriveToken(MASTER, "bob", "read");

assert("read token fits the firmware's 65-byte buffer", aliceRead.length <= 64);
assert("push and read tokens differ", alicePush !== aliceRead);
assert("different streams get different tokens", alicePush !== bobPush && aliceRead !== bobRead);

// ===========================================================================
//  multi-tenant
// ===========================================================================
await check("stream ingest with derived push token -> 200",
  await worker.fetch(req("/ingest/alice", { method: "POST", token: alicePush, body: SAMPLE }), env), 200);

await check("stream telemetry with derived read token -> 200",
  await worker.fetch(req("/telemetry/alice", { token: aliceRead }), env), 200,
  (j) => j && j.host === "dietpi" && typeof j.age_s === "number");

await check("stream telemetry with NO token -> 401",
  await worker.fetch(req("/telemetry/alice"), env), 401);

await check("alice's read token is rejected on bob's stream",
  await worker.fetch(req("/telemetry/bob", { token: aliceRead }), env), 401);

await check("alice's push token cannot read alice's own stream",
  await worker.fetch(req("/telemetry/alice", { token: alicePush }), env), 401);

await check("legacy read token does not open a stream",
  await worker.fetch(req("/telemetry/alice", { token: env.READ_TOKEN }), env), 401);

await check("bob's stream is empty despite alice having pushed (isolated storage)",
  await worker.fetch(req("/telemetry/bob", { token: bobRead }), env), 503);

await check("bob pushes his own data",
  await worker.fetch(req("/ingest/bob", { method: "POST", token: bobPush,
    body: JSON.stringify({ host: "bob-box", cpu_percent: 99 }) }), env), 200);

await check("alice still sees her own data, not bob's",
  await worker.fetch(req("/telemetry/alice", { token: aliceRead }), env), 200,
  (j) => j && j.host === "dietpi");

await check("invalid stream name -> 400",
  await worker.fetch(req("/telemetry/Alice", { token: aliceRead }), env), 400);

await check("stream name with a slash is not treated as a stream -> 404",
  await worker.fetch(req("/telemetry/a/b", { token: aliceRead }), env), 404);

await check("streams disabled without MASTER_SECRET -> 500",
  await worker.fetch(req("/telemetry/alice", { token: aliceRead }), { ...env, MASTER_SECRET: "" }), 500);

await check("GET on /ingest -> 405",
  await worker.fetch(req("/ingest/alice", { token: alicePush }), env), 405);

await check("POST on /telemetry -> 405",
  await worker.fetch(req("/telemetry/alice", { method: "POST", token: aliceRead, body: SAMPLE }), env), 405);

// ===========================================================================
//  pairing derivation - cross-language vectors
//
//  These exact values were produced independently by openssl:
//    printf 'peek-stream:K7M2P4QX9R' | openssl dgst -sha256
//  and match windows/peek_pair.py. If this block ever fails, the PC and the
//  device derive different streams from the same code and simply never meet,
//  with every request still looking perfectly valid - so it is worth pinning.
// ===========================================================================
const VEC = {
  code: "K7M2P4QX9R",
  stream: "4b907ba136d0a7f2",
  push: "30e67e9d1b1b5981686fa242d0ff835eb8aee945805b0ffa",
  read: "ec3cb3699bd1284efb2fcfe056609e87edf4813b84e9ce84",
};
const d = await derivePairing(VEC.code);
assert("pairing stream matches the openssl/python vector", d.stream === VEC.stream);
assert("pairing push token matches the vector", d.push === VEC.push);
assert("pairing read token matches the vector", d.read === VEC.read);

const dashed = await derivePairing("k7m2-p4qx-9r");
assert("dashes and lower case normalise to the same code", dashed.stream === VEC.stream);
assert("pairing stream is 16 hex", /^[0-9a-f]{16}$/.test(d.stream));
assert("pairing tokens fit the firmware buffer", d.read.length === 48);

// ===========================================================================
//  pairing over the wire - trust on first use, and NO secrets configured
// ===========================================================================
const bare = { TELEMETRY: env.TELEMETRY };     // no PUSH/READ/MASTER at all

await check("paired read claims its token even with no worker secrets set",
  await worker.fetch(req(`/telemetry/${d.stream}`, { token: d.read }), bare), 503);

await check("paired push claims its token and stores data",
  await worker.fetch(req(`/ingest/${d.stream}`, { method: "POST", token: d.push, body: SAMPLE }), bare), 200);

await check("paired read now returns that data",
  await worker.fetch(req(`/telemetry/${d.stream}`, { token: d.read }), bare), 200,
  (j) => j && j.host === "dietpi" && typeof j.age_s === "number");

await check("a different read token is refused once the role is claimed",
  await worker.fetch(req(`/telemetry/${d.stream}`, { token: "f".repeat(48) }), bare), 401);

await check("the push token cannot read the paired stream either",
  await worker.fetch(req(`/telemetry/${d.stream}`, { token: d.push }), bare), 401);

await check("no token at all on a paired stream -> 401",
  await worker.fetch(req(`/telemetry/${d.stream}`), bare), 401);

// Trust on first use, stated plainly: an UNCLAIMED stream is claimed by
// whoever arrives first, so the guarantee is "once claimed, only that token" -
// not "only the right token was ever possible". Squatting still requires
// knowing the 16-hex stream id, which requires the code.
const squat = await derivePairing("AAAABBBBCC");
await check("an unclaimed stream accepts the first token it sees (this is TOFU)",
  await worker.fetch(req(`/telemetry/${squat.stream}`, { token: "1".repeat(48) }), bare), 503);

await check("...and having claimed it, the code's own token is now refused",
  await worker.fetch(req(`/telemetry/${squat.stream}`, { token: squat.read }), bare), 401);

const fresh = await derivePairing("CCCCDDDDEE");
await check("a second pairing code gets its own empty stream",
  await worker.fetch(req(`/telemetry/${fresh.stream}`, { token: fresh.read }), bare), 503);

await check("one code's token is refused on another code's CLAIMED stream",
  await worker.fetch(req(`/telemetry/${fresh.stream}`, { token: d.read }), bare), 401);

// A 16-hex name must take the pairing path even when MASTER_SECRET is set,
// or the two namespaces would overlap and a paired stream would be rejected.
await check("paired streams still work when MASTER_SECRET is configured",
  await worker.fetch(req(`/telemetry/${d.stream}`, { token: d.read }), env), 200);

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
