# PeekESP

A physical system-metrics dashboard: an ESP32 with a 1.14" display showing live
CPU, RAM, storage, temperature and throughput for a remote Linux box.

This wiki is the **operational** reference — what to set, in what order, and
what to do when something is wrong. For what the project *is* and how it is
built, see the [README](https://github.com/shouravx/PeekESP).

## Start here

| | |
|---|---|
| **[Configuration Reference](Configuration-Reference)** | Every value, where it lives, what it should be |
| **[Runbook](Runbook)** | Bring-up in order, each step with a check |
| **[Troubleshooting](Troubleshooting)** | Symptom → cause |

## The three pieces

```
┌─────────┐   pushes    ┌────────────┐   polls    ┌────────┐
│ DietPi  │ ──────────▶ │ Cloudflare │ ◀───────── │ ESP32  │
│  agent  │   /ingest   │   Worker   │ /telemetry │ display│
└─────────┘             └────────────┘            └────────┘
```

Both ends dial **out**. Neither needs an inbound port, which is what makes this
work behind CGNAT or on a network you do not administer.

There is a second, more private option — **Direct**, where the ESP32 reaches the
host over a WireGuard tunnel with nothing in between. It needs the host to
accept an inbound connection. Both are supported and switchable from the
device's own setup screen without reflashing.

## Two things worth knowing up front

**An ESP32 cannot join a Tailscale network.** Tailscale is WireGuard plus a
control plane — node registration, rotating keys, DERP relays — and none of that
has an embedded client. The direct transport works by dialling a plain WireGuard
listener running *alongside* Tailscale on the host, which then bridges into the
tailnet.

**Configuration lives in four unconnected places** — GitHub Actions secrets,
Cloudflare Worker secrets, the device's setup portal, and the agent's command
line. A value set in one never reaches another. Nearly every setup problem is a
value in the wrong one, so
[Configuration Reference](Configuration-Reference) is the page to read first.
