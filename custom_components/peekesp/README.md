# PeekESP for Home Assistant

Puts the machines behind your pairing code into Home Assistant — CPU, memory,
storage, temperature, network throughput, battery — and gives you buttons that
drive the ESP32 display.

It reads the same Cloudflare Worker relay the display reads, with the same
pairing code. Nothing here connects to a monitored machine directly and nothing
opens a port: the agents push out, Home Assistant polls out, and neither end is
reachable from the internet. A machine behind CGNAT works exactly like one on
your LAN.

---

## Install

**HACS** — Integrations → ⋮ → Custom repositories → add
`https://github.com/shouravx/PeekESP` as an **Integration**, then install
PeekESP and restart Home Assistant.

**By hand** — copy `custom_components/peekesp/` into your
`config/custom_components/` and restart.

Then: Settings → Devices & Services → **Add Integration** → PeekESP, and type
the pairing code your device shows on screen. Dashes and capitals are ignored.

---

## What you get

One Home Assistant device per monitored machine, named after its hostname:

| Entity | Notes |
|---|---|
| CPU, Memory, Storage used | percent |
| Storage free, Storage total | across every fixed disk, not just the system drive |
| CPU temperature | absent on a machine with no sensor, rather than shown as −1 |
| Network in / out | kbit/s |
| Battery, Battery runtime, Charging, AC power | only created for a machine that has a battery |
| Last boot | a timestamp, not a counter — see below |
| Last reading | when the relay last heard from this machine |
| **Reporting** | the one to build automations on |

Plus a **PeekESP display** device carrying the buttons: Identify, Refresh now,
Wake, Standby, Reboot display.

### Two decisions worth knowing about

**"Reporting" stays available when a machine goes offline.** Every other entity
for that machine becomes unavailable once its reading goes stale, because
showing an hour-old CPU figure as current is the one failure a monitor must not
have. But an entity that goes *unavailable* can never turn *off*, and an
automation waiting for "turns off" would never fire. So `Reporting` follows the
relay instead, and reads unknown only when Home Assistant cannot reach the
relay at all — which is honest, because then nothing is known either way.

Stale means five missed polls, derived from your poll interval and never less
than 60 seconds.

**"Last boot" is a timestamp, not an uptime counter.** A seconds-since-boot
sensor changes on every poll, which writes a recorder row every 30 seconds
forever and draws a sawtooth. The boot time is the same fact, holds still while
the machine is up, and changes exactly when the thing worth noticing happens.

---

## The buttons

Commands are left at the relay and collected on the display's next poll, so
there is still nothing listening anywhere. Latency is one poll interval.

A command is delivered at most once and dropped after five minutes — a
"standby" queued while the display was unplugged for a week does not fire when
it comes back.

The vocabulary is closed and the relay rejects anything outside it. Nothing in
it changes configuration, and everything in it is undone by pressing a button
on the device, so there is no confirmation step: the worst outcome of a misclick
is a screen that returns a few seconds later.

---

## Poll interval

Default 30 seconds; change it in the integration's options.

Everything that touches the relay shares one budget. Cloudflare's free tier is
100,000 requests a day, and at their 5-second defaults each agent costs 17,280
and each display costs 17,280. This integration at 30 seconds costs 2,880 — and
at 5 seconds it would cost 17,280, a sixth of the budget, to watch numbers no
dashboard redraws that fast.

---

## The pairing code

The code is the credential: the stream id and both access tokens derive from it.
Anything that holds it can push telemetry to your display.

- It never leaves Home Assistant. The derivation happens locally and is one-way
  — neither the stream id nor a token can be turned back into the code.
- It is **not** used as the config entry title, so it does not appear in the UI
  or in screenshots pasted into issues. Name the entry whatever you like.
- It is redacted from diagnostics downloads.

If you press "new code" on the device, or re-flash with `--erase`, Home
Assistant will raise a reauth prompt for the new one.

---

## Troubleshooting

**"No machines are pushing to this code yet."** Either no agent is running, or
the code was mistyped. The integration genuinely cannot tell these apart — the
relay claims a stream's tokens on first use, so a typo derives a valid but empty
stream rather than an error. Check the code against the device's screen.

**"Another reader already holds this stream."** Someone else has this pairing
code, or the device generated a new one and this is the old one.

**Everything is unavailable.** Check `Last reading` on any machine — if it is
old, the agents stopped; if the integration itself is failing, the log will name
the relay and the reason.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

Standard library only — no Home Assistant needed. Covers the payload parsing and
the pairing derivation, including a cross-check that this implementation agrees
with the Windows agent, the Linux agent and the Worker's JavaScript. A
one-character drift between any two would mean reading a stream nothing pushes
to, with every request still looking perfectly valid from both ends.

What it does not cover is Home Assistant's own surface — entity registration,
the config flow's dialogs, the coordinator's HTTP. Those need a running instance.
