import worker, { TelemetryStore, deriveToken, derivePairing } from "../src/index.js";
import { deriveTokenNode } from "../mint.mjs";

// --- stub the Durable Object runtime ---------------------------------------
// Deliberately shaped like the real DurableObjectStorage rather than like the
// three calls the Worker happened to make first: list() returns a Map in sorted
// key order, and delete() takes either one key or an array. A stub that only
// implements what is currently called turns the next feature into a crash
// during the test run instead of a failing assertion.
class FakeStorage {
  constructor() { this.map = new Map(); }
  async put(a, b) {
    if (typeof a === "object") for (const [k, v] of Object.entries(a)) this.map.set(k, v);
    else this.map.set(a, b);
  }
  async get(k) { return this.map.get(k); }
  async delete(k) {
    const keys = Array.isArray(k) ? k : [k];
    let n = 0;
    for (const key of keys) if (this.map.delete(key)) n++;
    return Array.isArray(k) ? n : n > 0;
  }
  async list({ prefix = "" } = {}) {
    return new Map([...this.map]
      .filter(([key]) => key.startsWith(prefix))
      .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : 0)));
  }
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

// ===========================================================================
//  Several machines behind one pairing code
// ===========================================================================
const multi = await derivePairing("MULTI2DEV34");

// Paired streams live in a Durable Object named "pair:<stream>", so reaching
// the right fake storage means using the same name the Worker does.
const multiStore = storeFor("pair:" + multi.stream).state.storage;

const pushHost = (host, extra = {}) =>
  worker.fetch(req("/ingest/" + multi.stream, {
    method: "POST",
    token: multi.push,
    body: JSON.stringify({ host, cpu_percent: 1, ...extra }),
  }), env);

const readMulti = () =>
  worker.fetch(req("/telemetry/" + multi.stream, { token: multi.read }), env);

// Timestamps are written by Date.now(), so several pushes in one tick are
// indistinguishable. Ageing a slot by hand is what makes "the freshest host"
// and "expired" testable at all rather than dependent on how fast this runs.
function ageHost(name, ms) {
  const key = "dev_" + name;
  const row = multiStore.map.get(key);
  if (!row) return false;
  multiStore.map.set(key, { ...row, at: row.at - ms });
  return true;
}

await check("first host pushes", await pushHost("windows-pc"), 200);
await check("second host pushes to the same stream", await pushHost("dietpi"), 200);
await check("third host, a Mac", await pushHost("macbook"), 200);

await check("all three come back in one response", await readMulti(), 200,
  (j) => j.device_count === 3 && j.devices.length === 3);

await check("the array is sorted by name, not by arrival",
  await readMulti(), 200,
  (j) => j.devices.map((d) => d.host).join(",") === "dietpi,macbook,windows-pc");

await check("every device carries its own age",
  await readMulti(), 200,
  (j) => j.devices.every((d) => typeof d.age_s === "number"));

// The whole point of the top-level copy: a device flashed before any of this
// existed reads these fields and never looks at the array. Alphabetically
// "dietpi" sorts first, so ageing the other two proves the top level follows
// recency rather than array order.
assert("two hosts can be aged for the freshness test",
  ageHost("dietpi", 60_000) && ageHost("macbook", 30_000));
await check("the freshest host is mirrored at the top level for old firmware",
  await readMulti(), 200,
  (j) => j.host === "windows-pc" && j.age_s === 0);
await check("...and the stale ones report their real age in the array",
  await readMulti(), 200,
  (j) => j.devices.find((d) => d.host === "dietpi").age_s === 60);

await check("re-pushing a host updates its slot rather than adding one",
  await pushHost("dietpi", { cpu_percent: 99 }), 200);
await check("...so the count is unchanged and the value is the new one",
  await readMulti(), 200,
  (j) => j.device_count === 3 &&
         j.devices.find((d) => d.host === "dietpi").cpu_percent === 99);

// A host string is attacker-influenced in the sense that it arrives on the wire
// and is used as a storage key.
await check("a control character in the host name is stripped",
  await pushHost("bad" + String.fromCharCode(7) + "name  "), 200);
await check("...and it lands under a clean, trimmed key",
  await readMulti(), 200,
  (j) => j.devices.some((d) => d.host === "badname"));

await check("an agent that reports no host at all still gets a slot",
  await worker.fetch(req("/ingest/" + multi.stream, {
    method: "POST", token: multi.push, body: JSON.stringify({ cpu_percent: 5 }),
  }), env), 200);
await check("...named so the display has something to show",
  await readMulti(), 200, (j) => j.devices.some((d) => d.host === "host"));

// --- the cap ---------------------------------------------------------------
// Five slots exist by now; three more takes it past the cap of six.
for (const name of ["h1", "h2", "h3"]) await pushHost(name);
await check("the store caps how many machines one code can hold",
  await readMulti(), 200, (j) => j.device_count === 6);
await check("...and it is the stalest that gets dropped, not the newest",
  await readMulti(), 200,
  (j) => j.devices.some((d) => d.host === "h3") &&
         !j.devices.some((d) => d.host === "windows-pc"));

// --- the TTL ---------------------------------------------------------------
assert("a host can be aged past the 24 h TTL", ageHost("h1", 25 * 60 * 60 * 1000));
await check("a host silent for over a day is not listed",
  await readMulti(), 200, (j) => !j.devices.some((d) => d.host === "h1"));
await check("...and the next push actually deletes it rather than just hiding it",
  await pushHost("h2"), 200);
assert("the expired slot is gone from storage", !multiStore.map.has("dev_h1"));

// --- upgrading a store written by the previous Worker ----------------------
const legacyStore = new TelemetryStore({ storage: new FakeStorage() });
await legacyStore.state.storage.put({
  latest: { host: "olddata", cpu_percent: 7 }, at: Date.now() - 3000,
});
await check("a store holding only the old single 'latest' still serves it",
  await legacyStore.fetch(new Request("https://relay.example/")), 200,
  (j) => j.host === "olddata" && j.device_count === 1 && j.devices.length === 1);

await legacyStore.fetch(new Request("https://relay.example/", {
  method: "POST", body: JSON.stringify({ host: "newdata", cpu_percent: 8 }),
}));
await check("...and the first new push replaces it rather than sitting beside it",
  await legacyStore.fetch(new Request("https://relay.example/")), 200,
  (j) => j.device_count === 1 && j.devices[0].host === "newdata");
assert("the legacy keys are gone once a per-host slot exists",
  !legacyStore.state.storage.map.has("latest"));

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
