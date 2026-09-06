#!/bin/sh
#
# build-deb.sh - produce peekesp_<version>_all.deb
#
#   sh packaging/build-deb.sh
#
# Needs only dpkg-deb, which is on every Debian-family machine. No debhelper,
# no build container, no sponsor - see packaging/README.md for why this is a
# .deb you host yourself rather than one in Debian.
#
set -eu

REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(tr -d ' \n\r' < "$REPO/VERSION")
OUT="$REPO/dist"
STAGE="$OUT/peekesp_${VERSION}_all"

command -v dpkg-deb >/dev/null 2>&1 || {
    echo "dpkg-deb not found. On a non-Debian machine:  sudo apt install dpkg" >&2
    exit 1
}

rm -rf "$STAGE"
mkdir -p "$OUT" \
         "$STAGE/DEBIAN" \
         "$STAGE/usr/lib/peekesp" \
         "$STAGE/usr/bin" \
         "$STAGE/lib/systemd/system" \
         "$STAGE/etc/peekesp" \
         "$STAGE/usr/share/doc/peekesp"

install -m755 "$REPO/dietpi/peek-agent.py" "$STAGE/usr/lib/peekesp/peek-agent.py"
install -m755 "$REPO/dietpi/peekesp"       "$STAGE/usr/bin/peekesp"
install -m644 "$REPO/dietpi/README.md"     "$STAGE/usr/share/doc/peekesp/README.md"
install -m644 "$REPO/LICENSE"              "$STAGE/usr/share/doc/peekesp/copyright"

cat > "$STAGE/DEBIAN/control" <<EOF
Package: peekesp
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: all
Depends: python3 (>= 3.6), systemd
Maintainer: shouravx <rhshourav02@gmail.com>
Homepage: https://github.com/shouravx/PeekESP
Description: Telemetry agent for a PeekESP display
 Pushes this machine's CPU, memory, storage, temperature and network
 throughput to a Cloudflare Worker, where an ESP32 with a small display
 collects it. Both ends dial out, so neither needs an inbound port and it
 works behind CGNAT.
 .
 Standard library Python throughout - no psutil, no wheels, nothing to
 resolve at install time.
EOF

# conffiles, so dpkg does not overwrite a pairing code on upgrade.
echo "/etc/peekesp/agent.conf" > "$STAGE/DEBIAN/conffiles"

cat > "$STAGE/etc/peekesp/agent.conf" <<'EOF'
# PeekESP agent configuration.  Edit with:  sudo peekesp set KEY VALUE
#
# The pairing code is the only secret here: the stream id and the push token
# are derived from it, so anything that can read this file can push telemetry
# to your device. Set it with:  sudo peekesp pair YOUR-CODE
PEEK_PAIR_CODE=
PEEK_INTERVAL=5
PEEK_RELAY_BASE=https://peek-relay.peekesp.workers.dev
PEEK_SERVE=0
EOF
chmod 600 "$STAGE/etc/peekesp/agent.conf"

cat > "$STAGE/lib/systemd/system/peek-agent.service" <<'EOF'
[Unit]
Description=PeekESP telemetry agent
Documentation=https://github.com/shouravx/PeekESP
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=/etc/peekesp/agent.conf
ExecStart=/usr/bin/python3 /usr/lib/peekesp/peek-agent.py
Restart=always
RestartSec=10

User=peekesp
Group=peekesp

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

# ProtectHome is deliberately NOT set: it replaces /home with an empty mount,
# and this agent adds up every real filesystem to report storage.

[Install]
WantedBy=multi-user.target
EOF

cat > "$STAGE/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    if ! id peekesp >/dev/null 2>&1; then
        adduser --system --group --no-create-home --shell /usr/sbin/nologin peekesp
    fi
    chown root:root /etc/peekesp/agent.conf
    chmod 600 /etc/peekesp/agent.conf
    systemctl daemon-reload >/dev/null 2>&1 || true
    cat <<'MSG'

PeekESP is installed but not paired. The device shows a code on its screen:

    sudo peekesp pair K7M2-P4QX-9R
    sudo peekesp enable

MSG
fi
EOF

cat > "$STAGE/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ]; then
    systemctl disable --now peek-agent.service >/dev/null 2>&1 || true
fi
EOF

cat > "$STAGE/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
# On purge only. On a plain remove the config stays, because it holds the
# pairing code and reinstalling should not mean re-pairing every machine.
if [ "$1" = "purge" ]; then
    deluser --system peekesp >/dev/null 2>&1 || true
    rm -rf /etc/peekesp
fi
systemctl daemon-reload >/dev/null 2>&1 || true
EOF

chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm" "$STAGE/DEBIAN/postrm"

dpkg-deb --root-owner-group --build "$STAGE" >/dev/null
DEB="$OUT/peekesp_${VERSION}_all.deb"
mv "$STAGE.deb" "$DEB"
rm -rf "$STAGE"

echo "$DEB"
echo
echo "check it:   dpkg-deb --info '$DEB' && dpkg-deb --contents '$DEB'"
echo "install:    sudo apt install '$DEB'"
echo "lint it:    lintian '$DEB'      # optional, and it will have opinions"
