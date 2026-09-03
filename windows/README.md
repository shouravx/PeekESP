# PeekESP agent for Windows

Sends the same telemetry a DietPi does, so a Windows PC can drive a PeekESP
display. Same JSON contract, same flags as
[`dietpi/peek-agent.py`](../dietpi/peek-agent.py) — the only difference is
where the numbers come from.

**Standard library only.** Every reading is a Win32 call through `ctypes`, so
there is nothing to `pip install` and the compiled exe is CPython plus one
script.

## Run it

```bash
python peek-agent-win.py --once
```

That prints one reading and exits — the quickest way to confirm it can see your
machine.

Then either push to a relay:

```bash
python peek-agent-win.py --push https://peek-relay.YOU.workers.dev/ingest/STREAM --token TOKEN
```

or serve on the LAN for the Direct transport:

```bash
python peek-agent-win.py
```

`--no-serve` pushes without opening port 8080. `--interval` changes the push
rate from the default 5 s. `PEEK_PUSH_TOKEN` works instead of `--token` if you
would rather keep it out of your shell history.

## Build an exe

```bash
python build.py
```

PyInstaller goes into a local `.venv`, not your system Python — nothing outside
this folder changes. Output lands in `dist/peek-agent.exe`.

`--windowed` builds with no console window, which is what you want for
autostart. `--clean` removes the build artefacts.

## Run at login

No admin required:

```bash
schtasks /create /tn PeekAgent /sc onlogon /tr "\"C:\path\to\peek-agent.exe\" --push URL --token TOKEN"
```

Remove it with `schtasks /delete /tn PeekAgent /f`.

For a machine that should report even with nobody logged in, use `/sc onstart`
and `/ru SYSTEM` — that one does need an elevated prompt.

## What it reports

| Field | Source |
|---|---|
| `cpu_percent` | `GetSystemTimes`, idle vs total delta |
| `ram_percent` | `GlobalMemoryStatusEx` |
| `storage_percent` | `GetDiskFreeSpaceExW` on `%SystemDrive%` |
| `uptime_seconds` | `GetTickCount64` |
| `net_rx_kbps` / `net_tx_kbps` | `GetIfTable`, deduplicated |
| `cpu_temp_c` | always `-1` — see below |

**No temperature.** Windows exposes none without a vendor driver;
`MSAcpi_ThermalZoneTemperature` needs admin and most desktops don't implement
it. Reporting `-1` makes the display show `--` rather than a confident wrong
number. If you want real temperatures, LibreHardwareMonitor can expose them and
this agent could read from it — not wired up here.

**Network counters need deduplicating.** `GetIfTable` lists every NDIS *filter*
bound to an adapter as its own row: one Realtek NIC typically appears four times
(the adapter plus "WFP Native MAC Layer", "QoS Packet Scheduler" and "WFP 802.3
MAC Layer"), each reporting identical counters. Summing the table naively
multiplies real throughput by however many filters happen to be installed —
measured here as ~11 MB/s on a link actually doing ~3.4. The agent
deduplicates by MAC address, which collapses those clones, and skips adapters
that aren't operational so a disconnected NIC's stale totals don't count.

Counters from `GetIfTable` are 32-bit and wrap at 4 GB. A negative delta is
treated as a wrap and that sample is dropped, rather than reporting a spike.
