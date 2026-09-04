# The Linux agent

Reads this machine and pushes it to the relay every five seconds. Standard
library only — no `pip`, no `psutil`, nothing to install on a DietPi.

Works on DietPi, Raspberry Pi OS, Debian, Ubuntu, and anything else with
systemd and Python 3.6 or later.

## Install

Flash the device first, so it has a pairing code on screen. Then:

```bash
curl -fsSL https://raw.githubusercontent.com/shouravx/PeekESP/main/dietpi/install.sh | sudo sh
```

It asks for the code and derives everything else locally. Nothing else is typed,
and nothing is configured on the relay.

Non-interactively — the code as an argument:

```bash
curl -fsSL https://raw.githubusercontent.com/shouravx/PeekESP/main/dietpi/install.sh | sudo sh -s -- K7M2-P4QX-9R
```

Piping a script from the internet into a root shell means trusting whatever is
at that URL. If you would rather read it first — and that is genuinely the
better habit, not a disclaimer:

```bash
curl -fsSLO https://raw.githubusercontent.com/shouravx/PeekESP/main/dietpi/install.sh
less install.sh && sudo sh install.sh
```

## The `peekesp` command

The installer puts a `peekesp` command on the path. It is the whole interface —
you should not need `systemctl` or `journalctl` for anything routine.

```
peekesp status              is it running, and is it actually pushing
peekesp start|stop|restart  service control
peekesp enable|disable      start at boot, or not
peekesp logs [-f] [-n N]    journal for the agent
peekesp test                one reading, as this machine would report it

sudo peekesp pair CODE      re-pair to a different device
peekesp config              current settings
sudo peekesp set KEY VALUE  interval SECONDS | relay URL | serve on|off
sudo peekesp update         fetch the latest agent and restart
sudo peekesp uninstall      remove the service, the files and the account
peekesp version
```

`status` answers the question that actually matters, which is not whether a
service is running. A rejected push leaves the unit perfectly `active` while
nothing reaches the display, so `status` reads the journal and says so:

```
$ peekesp status
service   running
at boot   enabled
machine   dietpi
interval  5s
relay     https://peek-relay.peekesp.workers.dev
stream    e67e3b0d5b07d6ba

pushes are being REJECTED - usually the wrong pairing code.
re-pair with:  sudo peekesp pair NEW-CODE
```

`test` prints one reading and pushes nothing, which separates *is the agent
reading this machine correctly* from *is it reaching the relay* — two problems
that look identical from the device.

`set` writes to the config file and restarts the service if it was running.
The systemd unit carries no flags at all: everything comes from
`/etc/peekesp/agent.conf`, so changing the poll interval rewrites one line
rather than regenerating and reloading a unit.

```bash
sudo peekesp set interval 15      # a third of the requests
sudo peekesp set serve on         # also answer on :8080, for a LAN device
sudo peekesp set relay https://my-worker.workers.dev
```

`update` refuses to install a download that is not valid Python, because a
truncated fetch would otherwise take the service down until the next release.

## Several machines, one code

Run the same command with the **same pairing code** on every machine. They all
appear on the one display, and the left button swipes between them. Six per
code; a machine silent for 24 hours drops off the list on its own.

Nothing extra to configure — each agent already reports its own hostname, and
the relay keeps a slot per host rather than letting them overwrite each other.

The cost is per *agent*, not per screen: the device polls once and gets all of
them, so it makes the same number of requests whether it shows one machine or
six. At the default five-second interval each agent is 17,280 requests/day
against Cloudflare's 100,000/day free allowance, so one display and two machines
fits comfortably and four is getting close. `--interval 15` divides it by three.

## What it reports

| Field | Source |
|---|---|
| `cpu_percent` | `/proc/stat`, idle vs total delta |
| `ram_percent` | `/proc/meminfo`, using `MemAvailable` |
| `storage_percent`, `storage_total_gb`, `storage_free_gb` | every real filesystem in `/proc/mounts` |
| `cpu_temp_c` | `/sys/class/thermal`, preferring a zone whose type names a CPU |
| `battery_*` | `/sys/class/power_supply` |
| `uptime_seconds` | `/proc/uptime` |
| `net_rx_kbps`, `net_tx_kbps` | `/proc/net/dev` |

**Storage is every disk, not just `/`.** A DietPi with a USB drive would
otherwise show a nearly full 4 GB card and never mention the 2 TB attached to
it. Only filesystems backed by a block device are counted, so tmpfs, overlay
and kernel views are excluded, and each device is counted once so a bind mount
is not billed twice. The numbers follow `df`: root's reserved 5 % is excluded
from both the total and the free figure, which keeps the percentage, the total
and the free space agreeing with each other and with the tool you would check
them against.

**Temperature picks a CPU zone.** It used to take whichever thermal zone came
first and read above zero, which on a laptop can be the battery bay or the wifi
card. Linux needs nothing installed for this; Windows is the awkward one.

**Battery is empty on a Pi**, which has neither a cell nor a mains supply that
announces itself, so it reports `-1` and the display leaves it out rather than
drawing a flat battery. On a laptop it is all there. Charging is the charging
state itself, not "plugged in": on mains at 100 % it reports on-AC and not
charging, because calling that *charging* is how a battery readout stops being
believed.

**Network counters skip the tunnels.** `lo`, `wg*`, `tailscale*`, `docker*` and
`veth*` are excluded, or the device's own polling traffic would appear in the
numbers it is displaying.

## Running it by hand

```bash
python3 peek-agent.py --pair-code K7M2-P4QX-9R
```

See what this machine reports, without pushing anything anywhere:

```bash
python3 peek-agent.py --once
```

Check a code without pushing anything:

```bash
python3 peek-agent.py --pair-code K7M2-P4QX-9R --verify
# e67e3b0d5b07d6ba
```

That is the stream id, and it is what to compare against the device's own
`[pair] code X -> stream Y` line on serial when the two will not meet.

Serve the JSON on `:8080` instead of pushing — for a device on the same LAN, or
just to look at it:

```bash
python3 peek-agent.py
curl http://localhost:8080/telemetry
```

**Pushing no longer opens a port as a side effect.** Once there is somewhere to
push, listening is opt-in via `--serve` (or `PEEK_SERVE=1`). An agent that
dials out has no reason to also accept connections from the whole LAN, and an
open port nobody asked for is the kind of default that is only noticed later.

Every setting has an environment variable, because the systemd unit passes no
flags: `PEEK_PAIR_CODE`, `PEEK_RELAY_BASE`, `PEEK_INTERVAL`, `PEEK_SERVE`.
`--push URL --token TOKEN` still drives the private and named-stream modes that
do not use pairing at all.

## What the installer does

1. Checks for root, systemd, Python 3.6+, and curl or wget — each failure names
   the fix rather than just the problem.
2. Downloads `peek-agent.py` to `/opt/peekesp/`, replacing the existing copy
   only once the download has succeeded, so a failed upgrade leaves the working
   one in place instead of a truncated file.
3. Validates the code by asking the agent, so the alphabet lives in exactly one
   place and cannot drift out of step with the firmware.
4. Installs the `peekesp` command to `/usr/local/bin`, after checking it
   parses. Creates a system account `peekesp` with no home and no shell.
5. Writes the code to `/etc/peekesp/agent.conf`, mode `0600`, root-owned.
   **The code is the credential** — the stream id and push token are derived
   from it, so anything that can read that file can push telemetry to your
   device. systemd reads it as root before dropping to the service account.
6. Installs the unit and starts it.
7. Waits, then reports whether the machine is *actually pushing* — not merely
   that a service was installed. A rejected push says so, and says that the
   usual cause is the wrong code.

The service gives up everything it can: no capabilities, no new privileges,
read-only filesystem, private `/tmp`. `ProtectHome` is deliberately **not** set,
which is worth knowing — it replaces `/home` with an empty mount, and since this
agent adds up every real filesystem, a machine with `/home` on its own partition
would silently stop counting it and report less disk than it has.
