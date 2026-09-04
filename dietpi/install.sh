#!/bin/sh
#
# PeekESP agent installer for DietPi, Raspberry Pi OS, Debian and Ubuntu.
#
#   curl -fsSL https://raw.githubusercontent.com/shouravx/PeekESP/main/dietpi/install.sh | sudo sh
#
# It asks for the pairing code the device is showing, and everything else -
# relay URL, stream, push token - is derived from it locally. Nothing else is
# typed and nothing is configured on the relay.
#
# To skip the prompt (or to run this from a script):
#
#   curl -fsSL .../install.sh | sudo sh -s -- K7M2-P4QX-9R
#
# Piping a script from the internet into a root shell means trusting whatever
# is at that URL. If you would rather read it first - and you should - download
# it, look at it, then run it:
#
#   curl -fsSLO https://raw.githubusercontent.com/shouravx/PeekESP/main/dietpi/install.sh
#   less install.sh && sudo sh install.sh
#
# Removing it again:
#
#   sudo sh install.sh --uninstall
#
set -eu

RAW_BASE="https://raw.githubusercontent.com/shouravx/PeekESP/main"
PREFIX="/opt/peekesp"
CONF_DIR="/etc/peekesp"
CONF="$CONF_DIR/agent.conf"
UNIT="/etc/systemd/system/peek-agent.service"
SVC_USER="peekesp"
AGENT="$PREFIX/peek-agent.py"
CLI="/usr/local/bin/peekesp"

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
die()  { printf '\nerror: %s\n' "$*" >&2; exit 1; }


# ---------------------------------------------------------------------------
#  Preconditions, each with the fix rather than just the complaint
# ---------------------------------------------------------------------------
[ "$(id -u)" = "0" ] || die "run this as root:  sudo sh $0"

command -v systemctl >/dev/null 2>&1 \
    || die "this installs a systemd service and this machine has no systemctl.
       Run the agent by hand instead:
         python3 peek-agent.py --pair-code YOUR-CODE --no-serve"

# Before the python3 and curl checks: removing this should still work on a
# machine where one of them has since been uninstalled.
if [ "${1:-}" = "--uninstall" ]; then
    step "Removing the PeekESP agent"
    systemctl disable --now peek-agent.service 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    rm -rf "$PREFIX" "$CONF_DIR"
    rm -f "$CLI"
    userdel "$SVC_USER" 2>/dev/null || true
    say "Removed. The relay keeps this machine's last reading for 24 hours,"
    say "then drops it from the device on its own."
    exit 0
fi

PY=$(command -v python3 || true)
[ -n "$PY" ] || die "python3 is not installed.  sudo apt install -y python3"

# The agent is standard library only, so this is the interpreter version and
# nothing else. f-strings and secrets are not used; 3.6 is genuinely enough.
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 6) else 1)' \
    || die "python3 is older than 3.6"

if command -v curl >/dev/null 2>&1; then
    fetch() { curl -fsSL --proto '=https' --tlsv1.2 "$1" -o "$2"; }
elif command -v wget >/dev/null 2>&1; then
    fetch() { wget -q --https-only -O "$2" "$1"; }
else
    die "neither curl nor wget is installed.  sudo apt install -y curl"
fi


# ---------------------------------------------------------------------------
#  The one thing that has to be typed
# ---------------------------------------------------------------------------
CODE="${1:-}"
if [ -z "$CODE" ]; then
    # Piped into a shell, stdin IS the script - so the prompt has to come from
    # the terminal directly or this reads the rest of its own source.
    if [ -r /dev/tty ]; then
        printf '\nThe device shows a code like K7M2-P4QX-9R.\nPairing code: '
        read -r CODE < /dev/tty
    else
        die "no terminal to ask on. Pass the code as an argument:
         curl -fsSL .../install.sh | sudo sh -s -- K7M2-P4QX-9R"
    fi
fi
[ -n "$CODE" ] || die "no pairing code given"


# ---------------------------------------------------------------------------
#  Install
# ---------------------------------------------------------------------------
step "Downloading the agent"
mkdir -p "$PREFIX"
TMP="$PREFIX/.peek-agent.py.new"
fetch "$RAW_BASE/dietpi/peek-agent.py" "$TMP" \
    || die "could not download the agent from $RAW_BASE"
# Replaced only once the download succeeded, so a failed upgrade leaves the
# working copy in place instead of a truncated file.
mv "$TMP" "$AGENT"
chmod 0755 "$AGENT"

step "Installing the peekesp command"
TMP="$PREFIX/.peekesp.new"
fetch "$RAW_BASE/dietpi/peekesp" "$TMP" || die "could not download the peekesp command"
sh -n "$TMP" || { rm -f "$TMP"; die "the downloaded peekesp command does not parse"; }
mv "$TMP" "$CLI"
chmod 0755 "$CLI"

step "Checking the pairing code"
# The agent validates it, so the alphabet lives in exactly one place. A copy
# here would be one more thing to drift out of step with the firmware.
STREAM=$("$PY" "$AGENT" --pair-code "$CODE" --verify) \
    || die "that does not look like a pairing code.
       Ten characters, dashes and case optional, as shown on the device."
say "stream $STREAM"
say "(compare that with the device's '[pair] code X -> stream Y' serial line"
say " if the two ever fail to meet)"

step "Creating the service account"
if ! id "$SVC_USER" >/dev/null 2>&1; then
    # Debian puts nologin in /usr/sbin and others in /sbin. Pointing an account
    # at a shell that does not exist makes some tools complain about it forever.
    NOLOGIN=/usr/sbin/nologin
    [ -x "$NOLOGIN" ] || NOLOGIN=/sbin/nologin
    [ -x "$NOLOGIN" ] || NOLOGIN=/bin/false
    useradd --system --no-create-home --shell "$NOLOGIN" "$SVC_USER" \
        || die "could not create the $SVC_USER user"
fi

step "Storing the pairing code"
# The code IS the credential - anything holding it can push to your stream, so
# it is root-only and never world-readable. systemd reads this as root before
# dropping to the service account.
mkdir -p "$CONF_DIR"
chmod 0750 "$CONF_DIR"
umask 077
cat > "$CONF" <<EOF
# PeekESP agent configuration.  Edit with:  sudo peekesp set KEY VALUE
#
# The pairing code is the only secret here: the stream id and the push token
# are derived from it, so anything that can read this file can push telemetry
# to your device. Re-pair the device to invalidate it.
PEEK_PAIR_CODE=$CODE

# Seconds between pushes. Each agent costs 86400/interval requests a day
# against Cloudflare's free 100,000, so this is the dial for fitting more
# machines onto one relay.
PEEK_INTERVAL=5

# A different Worker, if you deployed your own.
PEEK_RELAY_BASE=https://peek-relay.peekesp.workers.dev

# Also answer on :8080, for a device polling over the same LAN. Off by
# default: an open port is not something pushing telemetry should imply.
PEEK_SERVE=0
EOF
chmod 0600 "$CONF"
chown root:root "$CONF"

step "Installing the service"
cat > "$UNIT" <<EOF
[Unit]
Description=PeekESP telemetry agent
Documentation=https://github.com/shouravx/PeekESP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# Python buffers stdout when it is not a terminal, so without this the logs
# arrive in lumps - or not at all until the service stops.
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$CONF
# No flags: everything comes from the config file above, so changing the poll
# interval rewrites one line rather than regenerating and reloading a unit.
ExecStart=$PY $AGENT
Restart=always
RestartSec=10

User=$SVC_USER
Group=$SVC_USER

# The agent reads /proc and /sys and writes nothing, so it can give up
# essentially everything.
NoNewPrivileges=true
CapabilityBoundingSet=
ProtectSystem=strict
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictRealtime=true
LockPersonality=true
SystemCallArchitectures=native
PrivateTmp=true

# ProtectHome is deliberately NOT set. It replaces /home with an empty mount,
# and this agent adds up every real filesystem to report storage - so a machine
# with /home on its own partition would silently stop counting it and report
# less disk than it has.

[Install]
WantedBy=multi-user.target
EOF

chmod 0644 "$UNIT"        # the umask above would otherwise make it root-only
systemctl daemon-reload
systemctl enable --now peek-agent.service >/dev/null 2>&1 \
    || die "the service would not start; see: journalctl -u peek-agent -n 40"


# ---------------------------------------------------------------------------
#  Say whether it actually worked, rather than that it was installed
# ---------------------------------------------------------------------------
step "Checking it is pushing"
i=0
while [ "$i" -lt 15 ]; do
    if journalctl -u peek-agent -n 50 --no-pager 2>/dev/null \
        | grep -q 'pushing to'; then
        break
    fi
    i=$((i + 1))
    sleep 1
done

if ! systemctl is-active --quiet peek-agent.service; then
    say ""
    journalctl -u peek-agent -n 20 --no-pager 2>/dev/null || true
    die "the agent started and then stopped; the log above says why"
fi

if journalctl -u peek-agent -n 50 --no-pager 2>/dev/null \
    | grep -q 'push rejected'; then
    say ""
    journalctl -u peek-agent -n 20 --no-pager 2>/dev/null || true
    die "the relay refused this machine.
       That is usually the wrong pairing code - check what the device shows
       and run this again."
fi

cat <<EOF

Done. $(hostname) is now pushing to the relay every 5 seconds.

Everything is managed with the peekesp command:

  peekesp status              is it running, and is it actually pushing
  peekesp logs -f             follow the journal
  peekesp test                one reading, as this machine reports it
  sudo peekesp pair NEW-CODE  point it at a different device
  sudo peekesp set interval 15
  sudo peekesp uninstall
  peekesp help                the rest

The device should show this machine within a few seconds. Run the same
installer on other machines with the SAME code and they all appear on the one
display - tap the left button to swipe between them.
EOF
