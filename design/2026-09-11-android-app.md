# PeekESP for Android — design

**Date:** 2026-09-11  **Status:** approved in conversation; this document is the
record of that decision, awaiting review before the build plan is written.

Kept in `design/` rather than `docs/`, because `docs/` is the GitHub Pages
source and anything in it is served on the public website.

---

## Goal

Monitor, from a phone, every machine and every display under one or more pairing
codes — live while the app is open, glanceable from the home screen.

## Scope

**In v1**

- Pair with one or more codes ("setups").
- A live dashboard of every machine and every display in a setup.
- A detail view per machine.
- A home-screen widget.
- A Liquid Glass visual language, as far as each surface allows.
- The backend, firmware and Pi changes that let displays report who they are.

**Not in v1** — offline alerts, display commands, iOS, a Play Store listing.

## Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Stack | Native Kotlin + Jetpack Compose | Real widgets and background work; no second layer to maintain |
| Glass | Kyant **Backdrop** (`io.github.kyant0:backdrop` 2.0.1, Apache-2.0) | Built for Liquid Glass and not experimental. Haze's glass is `@ExperimentalHazeApi`, still at 2.0.0-beta03 |
| Minimum Android | 8.0 (API 26) | Covers nearly every phone in use; the glass steps down by version instead of excluding older phones |
| Displays | Report status **and** identity | The relay knows nothing about displays today; they only read |
| Theme | Dark only | The product is a dark panel in a dark room; the website made the same call |
| Distribution | GitHub Releases, sideloaded | A Play listing needs the owner's developer account |

---

## Architecture

```
  agents ── POST /ingest/<stream> ──────────────▶ ┌──────────────────────┐
  (PCs, Pi, servers)   push token                 │  Worker + Durable    │
                                                  │  Object, per stream  │
  ESP32 / Pi displays ── GET /telemetry/<stream> ▶│                      │
                         read token               │  machines[]          │
                         + X-Peek-Display headers │  displays[]  (new)   │
                                                  │                      │
  phone app ─ GET /telemetry/<stream>?displays=1 ▶│                      │
                         read token               └──────────────────────┘
```

Everything dials out. Nothing listens.

---

## 1. Worker

**Displays identify themselves on every poll**, with three optional headers:

| Header | Content | Limit |
|---|---|---|
| `X-Peek-Display` | a stable id: first 12 hex of `SHA-256("peek-display:" + chip MAC)` | exactly 12 hex |
| `X-Peek-Fw` | firmware version, e.g. `1.3.0` | 16 chars |
| `X-Peek-Panel` | panel name, e.g. `GC9A01 240 round` | 32 chars |

A hash rather than the MAC, so a hardware identifier is not handed to everyone
who holds the read token.

**Malformed headers are ignored, never refused.** A display must never lose its
data because a header was wrong.

The front Worker forwards these to the Durable Object; today it forwards only
the role and the token.

**The Durable Object keeps `displays`**: id → `{fw, panel, last_seen}`.

- Updated in memory on every poll, **persisted at most once a minute** per
  display, so a display polling every five seconds does not become twelve
  storage writes a minute against the free tier.
- At most six per stream, the same cap as machines; the stalest is evicted.
- Dropped after 24 hours of silence, the same TTL as machines.

**The list is opt-in:** `GET /telemetry/<stream>?displays=1` adds

```json
"displays": [{ "id": "a3f09c21be47", "fw": "1.3.0", "panel": "GC9A01 240 round", "age_s": 4 }]
```

Without the parameter the response is **byte-identical to today's**. The firmware
parses that JSON, and a Worker deploy must not change what a display that has
not been reflashed yet receives.

Readers that are not displays — the app, Home Assistant — send no
`X-Peek-Display`, so they are never counted as displays.

**Online** means polled within 120 seconds.

**Tests** (added to `cloudflare/test/worker.test.mjs`): a header is recorded; a
missing header records nothing; malformed headers are ignored and the read
still succeeds; `?displays=1` includes the list; without it the body is
unchanged; the once-a-minute persistence throttle; the cap; the TTL.

## 2. Firmware

On every poll, send the three headers. The id is computed once at boot from
`ESP.getEfuseMac()` with the mbedtls SHA-256 the sketch already uses.
`FW_VERSION` and `PANEL_NAME` already exist.

No change to parsing. **This needs a reflash**, and a display on older firmware
simply does not appear in the app's list — it goes on working exactly as now.

Verified by compiling all seven panel builds and rebuilding their images.

## 3. Raspberry Pi display host

`relay.fetch()` sends the same headers. The id uses the same derivation as the
firmware with `/etc/machine-id` in place of the chip MAC — the first 12 hex of
`SHA-256("peek-display:" + machine-id)` — alongside the package version and the
configured panel's name. Pi displays then appear in the app beside the ESP32s.

---

## 4. Android app

### Data

- **Pairing derivation** — the sixth implementation of the same three lines.
  Pinned in a unit test against vectors from the existing five: a drift means
  reading a stream nothing pushes to, with every request looking valid.
- **Relay client** — `GET /telemetry/<stream>?displays=1` with the read token.
  Parsed with the rules the Home Assistant integration uses: `-1` means no
  sensor; NaN, infinity and booleans are not numbers; missing is unknown, never
  zero; the flat legacy shape is still read.
- **Freshness** — a machine is offline after `max(60 s, 5 × poll interval)`.
  The age counted is the relay's own timestamp **plus** the time since the app's
  last successful fetch. Without that second term a phone that lost its
  connection shows every machine as permanently fresh — the bug the firmware
  had and fixed.
- **Setups** — `{name, relay, code}`. The code is the credential: stored
  encrypted with an AES-GCM key held in the Android Keystore, never logged,
  never sent (only the derived stream and token travel), not shown again after
  entry except on an explicit reveal.

### Screens

1. **Pair.** Code entry grouped as the device shows it (`K7M2-P4QX-9R`),
   checked against the alphabet locally, then a probe of the relay. The result
   is reported honestly: *found 3 machines*, or *nothing is pushing to this code
   yet — expected if no agent is installed, and also exactly what a typo looks
   like*. The relay claims tokens on first use, so it cannot tell the two apart.
2. **Dashboard.** Machines as glass cards — name, CPU and RAM arcs, storage,
   temperature, throughput, age. Displays as glass rows — online or not, polled
   *n* s ago, firmware, panel. A setup switcher when there is more than one.
   Pull to refresh. Polls every 5 s while in the foreground and stops in the
   background.
3. **Machine.** The device's own gauges — 270° arcs, the same severity colours —
   plus storage free and total, temperature, throughput, battery if there is
   one, boot time and last reading.
4. **Settings.** Setups (add, rename, remove), the poll interval (5–60 s) with
   the request-budget note, and an about screen with the version and an update
   check against GitHub releases.

### Visual language

- **Glass needs something behind it.** Liquid Glass over a flat dark ground
  refracts nothing and reads as a grey rectangle. The dashboard sits on a slow,
  living field built from the device's palette — cyan `#00E5FF` and magenta
  `#FF2E7E` on `#05070E` — that warms toward amber and red when a machine is in
  trouble, so the glass itself carries state.
- **By Android version**, because that is what the platform allows:
  - 13 and later: lens refraction, highlights, vibrancy.
  - 12: backdrop blur and tint.
  - 11 and earlier: tinted translucency. No faked blur.
- **Type:** Montserrat for figures and headings — the face on the device — and a
  plainer sans for body text.
- **Motion:** one authored moment. Arcs sweep to their values over 500 ms with an
  ease-out, as `lv_anim_path_ease_out` does on the device.
- **Accessibility is where glass usually fails**, so it is designed in rather
  than checked after:
  - text sits on a tint strong enough to hold 4.5:1 against the *brightest*
    point of the field behind it;
  - every value has a content description;
  - with animations disabled system-wide, nothing sweeps and the field is still;
  - layouts hold at 200% font scale.
- The visual system is worked through with the design skill before any screen
  is built.

### Widget

- **Jetpack Glance**, in two sizes: 2×2 (online and offline counts, and the
  worst machine) and 4×2 (each machine with CPU, RAM and status).
- **Glass-like, not glass.** Widgets render through RemoteViews, which cannot
  sample or blur the wallpaper. Translucent tinted panels with a specular edge
  are as far as Android lets a widget go.
- **Updates** every 15 minutes (the platform's floor for background work), on
  tap, and whenever the app has fresh data in the foreground.
- **Shows its age** — *as of 12 min ago* — because a widget is stale by design,
  and a stale number presented as current is the one failure a monitor must not
  have.

### Security and privacy

- HTTPS only; cleartext traffic disabled.
- The `INTERNET` permission and nothing else.
- No analytics and no SDKs beyond the libraries named here.

### Request budget

The app polls only while open: 5 s is 17,280 requests a day if left open around
the clock — the same as a display. The widget adds 96 a day. Both are stated in
settings next to the interval.

---

## 5. Build, verification, release

- **Local toolchain** in `%USERPROFILE%\.peekesp-toolchain`, outside the
  repository: JDK 21 and the Android SDK. Not under `AppData`: this machine's
  Python is the Microsoft Store build, which silently redirects anything it
  writes to `AppData\Local` into a private sandbox that Gradle and the JDK
  cannot see. The first download landed there and looked like it had worked.
- **Gradle wrapper** committed, versions in a version catalog.
- **Tests on the JVM:**
  - pairing vectors against the other five implementations;
  - parsing, including sentinels, NaN and booleans;
  - freshness, including the stale-connection term;
  - the relay client against a mock server;
  - **Roborazzi screenshots** of every screen and both widget sizes, at font
    scale 1.0 and 2.0, compared against committed golden images.
- **CI** — a new `android.yml`: unit and screenshot tests, then an APK. Tagged
  releases attach the APK; the rolling dev pre-release includes it too.
- **Signing** — a release keystore that the owner creates (one `keytool`
  command), and four GitHub secrets: `ANDROID_KEYSTORE_BASE64`,
  `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`.
  Without them CI builds a debug-signed APK and labels it as one.

### What will and will not have been verified

- **There is no Android device here and no emulator planned.** Verification is
  unit tests and JVM-rendered screenshots.
- **The screenshots will not show real refraction.** They are rendered by
  Robolectric on the JVM, which draws the fallback path, not AGSL shaders. Layout,
  type, colour and contrast are checked; the refraction itself is first seen on
  a phone running Android 13 or later.
- **Displays appear only after a reflash**, and nothing here has driven one.

---

## Order of work

Two phases. The first stands on its own and changes nothing for existing
displays; the second depends on it only for the displays section.

**Phase A — displays report themselves**

1. Worker: display tracking, tests, deploy. Backward compatible.
2. Firmware: the headers, then all seven panel images rebuilt.
3. Pi display host: the headers.

**Phase B — the app**

4. Toolchain, project skeleton, data layer and its tests.
5. The visual system, through the design skill, with screenshots.
6. Screens, then the widget.
7. CI, release wiring, and the signing instructions.
