#!/usr/bin/env node
/**
 * Mint a stream's token pair.
 *
 *   MASTER_SECRET=... npm run mint -- alice
 *   MASTER_SECRET=... npm run mint -- alice --url https://peek-relay.you.workers.dev
 *
 * Runs entirely offline. Tokens are a pure function of (master secret,
 * stream, role), so the Worker recomputes the same values and there is
 * nothing to upload, register or keep in sync. Onboarding someone never
 * touches the deployment.
 *
 * This derivation must stay byte-identical to deriveToken() in src/index.js.
 * test/worker.test.mjs asserts that the two agree.
 */

import { createHmac, randomBytes } from "node:crypto";
import { pathToFileURL } from "node:url";

const TOKEN_HEX_LEN = 48;
const STREAM_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;

export function deriveTokenNode(masterSecret, stream, role) {
  return createHmac("sha256", masterSecret)
    .update(`${stream}:${role}`)
    .digest("hex")
    .slice(0, TOKEN_HEX_LEN);
}

function die(msg) {
  console.error("error: " + msg);
  process.exit(1);
}

// The test suite imports deriveTokenNode from this file to prove it agrees
// with the Worker's WebCrypto derivation, so the CLI must not run on import.
const isMain = process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href;
if (!isMain) {
  // exported for tests; nothing else to do
} else {
  main();
}

function main() {
const argv = process.argv.slice(2);

if (argv.includes("--new-master")) {
  // 32 bytes: this is an HMAC key, not something anyone types.
  console.log(randomBytes(32).toString("hex"));
  console.log("\nStore it with:  npx wrangler secret put MASTER_SECRET");
  console.log("Keep a copy somewhere safe - you need it to mint further streams,");
  console.log("and rotating it invalidates every stream at once.");
  process.exit(0);
}

const stream = argv.find((a) => !a.startsWith("--"));
const urlIdx = argv.indexOf("--url");
const base = urlIdx !== -1 ? (argv[urlIdx + 1] || "").replace(/\/+$/, "") : null;

if (!stream) {
  console.error(`Usage:
  MASTER_SECRET=... node mint.mjs <stream> [--url https://peek-relay.you.workers.dev]
  node mint.mjs --new-master

  <stream>  lowercase letters, digits, - and _, starting alphanumeric, max 32 chars`);
  process.exit(1);
}

if (!STREAM_RE.test(stream)) {
  die(`invalid stream name "${stream}" - lowercase letters, digits, - and _, ` +
      "starting alphanumeric, max 32 chars");
}

const master = process.env.MASTER_SECRET;
if (!master) {
  die("MASTER_SECRET is not set.\n" +
      "  Generate one:  node mint.mjs --new-master\n" +
      "  Then:          MASTER_SECRET=... node mint.mjs " + stream);
}

const push = deriveTokenNode(master, stream, "push");
const read = deriveTokenNode(master, stream, "read");
const shown = base || "https://peek-relay.YOU.workers.dev";

console.log(`
stream: ${stream}

  push token (host agent - keep private)
    ${push}

  read token (the display device)
    ${read}

Give the owner of this stream:

  On the host:
    python3 peek-agent.py --push ${shown}/ingest/${stream} --token ${push}

  In the device's setup portal:
    Transport    Cloudflare relay
    Worker URL   ${shown}/telemetry/${stream}
    Read token   ${read}

  Check it:
    curl -H "Authorization: Bearer ${read}" ${shown}/telemetry/${stream}
    (503 "no telemetry received yet" means auth works and nothing has pushed yet)
${base ? "" : `
Pass --url https://your-worker.workers.dev to print real URLs.`}
To revoke this stream later, move the owner to a new name (${stream}-v2):
tokens are derived, so they change with the name and no one else is affected.
`);
}
