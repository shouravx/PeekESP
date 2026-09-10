#!/bin/sh
#
# install.sh - put the PeekESP display on a Raspberry Pi.
#
#   curl -fsSL https://raw.githubusercontent.com/shouravx/PeekESP/main/pi/install.sh | sudo sh
#
# Asks which panel is wired up, installs only that panel's driver, and starts a
# systemd service. Nothing is installed for a panel you do not have: luma.lcd,
# luma.oled, RPLCD and the Waveshare SDK are four separate dependency trees and
# pulling all of them onto a Pi Zero to drive one OLED is rude.
#
set -eu

REPO_RAW="https://raw.githubusercontent.com/shouravx/PeekESP/main"
PREFIX="/opt/peekesp-display"
CONF_DIR="/etc/peekesp"
CONF="$CONF_DIR/display.conf"
UNIT="/etc/systemd/system/peek-display.service"
SVC="peek-display.service"
CLI="/usr/local/bin/peek-display"

say()  { printf '%s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = "0" ] || die "run this with sudo"

# ---------------------------------------------------------------------------
#  Which panel
# ---------------------------------------------------------------------------
# Read from /dev/tty, not stdin: this script is normally arriving through a
# pipe, and stdin is the pipe.
ask() {
    printf '%s' "$1" > /dev/tty
    read -r REPLY < /dev/tty || REPLY=""
    printf '%s' "$REPLY"
}

if [ -t 0 ] || [ -e /dev/tty ]; then
    say ""
    say "Which display is wired up?"
    say ""
    say "   1  SSD1306 / SH1106 OLED     0.96\" mono, I2C"
    say "   2  ILI9341 TFT               2.4\" colour, SPI"
    say "   3  ST7789 TFT                colour, SPI"
    say "   4  GC9A01 round TFT          1.28\" circular, SPI"
    say "   5  HD44780 character LCD     16x2, I2C backpack"
    say "   6  MAX7219 LED matrix        8x8 blocks, SPI"
    say "   7  Waveshare e-paper         SPI"
    say "   8  none - render to a PNG file"
    say ""
    choice=$(ask "Panel [1-8]: ")
else
    choice=8
fi

case "$choice" in
    1) PANEL=ssd1306; PKGS="luma.oled" ;;
    2) PANEL=ili9341; PKGS="luma.lcd" ;;
    3) PANEL=st7789;  PKGS="luma.lcd" ;;
    4) PANEL=gc9a01;  PKGS="luma.lcd" ;;
    5) PANEL=hd44780; PKGS="RPLCD smbus2" ;;
    6) PANEL=max7219; PKGS="luma.led_matrix" ;;
    7) PANEL=epaper;  PKGS="" ;;
    *) PANEL=png;     PKGS="" ;;
esac

say ""
say "panel     $PANEL"

# ---------------------------------------------------------------------------
#  Dependencies
# ---------------------------------------------------------------------------
say "installing dependencies ..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# python3-pil from apt rather than pip: Pillow from source on a Pi Zero is a
# twenty-minute build, and the packaged one is what the system libraries were
# compiled against anyway.
apt-get install -y -qq python3 python3-pil python3-pip >/dev/null

if [ -n "$PKGS" ]; then
    # --break-system-packages is required on Debian 12 and later, where the
    # system Python is marked externally managed (PEP 668). A venv would be
    # cleaner, but these drivers need the system's own RPi.GPIO and spidev,
    # and a venv that has to reach back into system site-packages for its
    # hardware access is not cleaner at all.
    pip3 install --break-system-packages $PKGS 2>/dev/null \
        || pip3 install $PKGS \
        || die "could not install: $PKGS"
fi

if [ "$PANEL" = "epaper" ]; then
    say ""
    say "Waveshare do not publish their e-paper library to PyPI. Install it:"
    say "    git clone --depth 1 https://github.com/waveshareteam/e-Paper"
    say "    sudo cp -r e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd \\"
    say "              /usr/lib/python3/dist-packages/"
    say ""
    say "then set the exact model in $CONF, for example:"
    say "    PEEK_PANEL_MODULE=waveshare_epd.epd2in13_V4"
    say ""
fi

# ---------------------------------------------------------------------------
#  Buses
# ---------------------------------------------------------------------------
# Enabling the bus the chosen panel needs, rather than both. raspi-config is
# absent on some images, so a failure here is reported and not fatal - the
# panel will say what is wrong when it cannot open the device.
enable_bus() {
    if command -v raspi-config >/dev/null 2>&1; then
        raspi-config nonint "$1" 0 >/dev/null 2>&1 \
            && say "enabled ${2}" \
            || say "could not enable ${2} - do it in raspi-config"
    else
        say "raspi-config not found - enable ${2} yourself if it is not already on"
    fi
}
case "$PANEL" in
    ssd1306|sh1106|hd44780) enable_bus do_i2c I2C ;;
    ili9341|st7789|gc9a01|max7219|epaper) enable_bus do_spi SPI ;;
esac

# ---------------------------------------------------------------------------
#  Files
# ---------------------------------------------------------------------------
say "installing to $PREFIX ..."
mkdir -p "$PREFIX/peekdisplay" "$CONF_DIR"

fetch() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --proto '=https' --tlsv1.2 "$1" -o "$2"
    else
        wget -q --https-only -O "$2" "$1"
    fi
}

for f in peek-display.py; do
    fetch "$REPO_RAW/pi/$f" "$PREFIX/$f"
done
for f in __init__.py app.py character.py config.py layout.py panels.py pairing.py relay.py; do
    fetch "$REPO_RAW/pi/peekdisplay/$f" "$PREFIX/peekdisplay/$f"
done

# Checked before anything is started: a truncated download parses as nothing,
# and a service that crash-loops on a syntax error is harder to diagnose than
# a refusal here.
python3 -c "
import ast, sys, pathlib
for p in pathlib.Path(sys.argv[1]).rglob('*.py'):
    ast.parse(p.read_text())
print('sources parse')
" "$PREFIX" || die "the download is incomplete - nothing was started"

chmod 0755 "$PREFIX/peek-display.py"
ln -sf "$PREFIX/peek-display.py" "$CLI"

# ---------------------------------------------------------------------------
#  Pairing code
# ---------------------------------------------------------------------------
CODE=""
if [ -f "$CONF" ]; then
    CODE=$(grep '^PEEK_PAIR_CODE=' "$CONF" 2>/dev/null | cut -d= -f2- || true)
fi
if [ -z "$CODE" ] && [ -e /dev/tty ]; then
    say ""
    say "The pairing code is shown on the device's screen, or in the tray app."
    say "Leave it blank to configure later."
    CODE=$(ask "Pairing code: ")
fi

umask 077
if [ ! -f "$CONF" ]; then
    cat > "$CONF" <<EOF
# PeekESP display configuration.
#
# The pairing code is the credential: the stream and both tokens derive from
# it, so anything that can read this file can push telemetry to your display.
PEEK_PAIR_CODE=$CODE
PEEK_RELAY_BASE=https://peek-relay.peekesp.workers.dev

PEEK_PANEL=$PANEL
PEEK_INTERVAL=5
PEEK_ROTATE_SECONDS=8

# Anything after PEEK_PANEL_ is passed to the driver, so an unusual address or
# an extra pin needs no code change:
#   PEEK_PANEL_ADDRESS=0x3D
#   PEEK_PANEL_ROTATE=2
#   PEEK_PANEL_WIDTH=128
#   PEEK_PANEL_HEIGHT=64
EOF
else
    # Keep the existing settings; only correct the panel if it changed.
    sed -i "s|^PEEK_PANEL=.*|PEEK_PANEL=$PANEL|" "$CONF"
    say "kept the existing $CONF"
fi
chmod 0600 "$CONF"

# ---------------------------------------------------------------------------
#  Service
# ---------------------------------------------------------------------------
# Runs as root, unlike the telemetry agent. That agent reads /proc and needs
# nothing, so it drops to its own account; this one needs /dev/spidev, /dev/i2c
# and GPIO, and the usual answer - add a user to the spi, i2c and gpio groups -
# does not cover the sysfs GPIO paths some of these libraries reach for. Saying
# so plainly is better than a hardened-looking unit that silently needs root.
cat > "$UNIT" <<EOF
[Unit]
Description=PeekESP display
Documentation=https://github.com/shouravx/PeekESP/blob/main/pi/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PYTHONUNBUFFERED=1
EnvironmentFile=$CONF
ExecStart=/usr/bin/python3 $PREFIX/peek-display.py
Restart=always
RestartSec=10

NoNewPrivileges=true
ProtectHome=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SVC" >/dev/null 2>&1 || true

say ""
say "checking the panel ..."
if python3 "$PREFIX/peek-display.py" --self-test --panel "$PANEL" 2>/dev/null; then
    say "the panel answered"
else
    say "the panel did not respond. Check the wiring, then:"
    say "    peek-display --self-test"
fi

if [ -n "$CODE" ]; then
    systemctl restart "$SVC"
    say ""
    say "started. It is now polling every 5 seconds."
else
    say ""
    say "No pairing code yet. Add one and start it:"
    say "    sudo sed -i 's/^PEEK_PAIR_CODE=.*/PEEK_PAIR_CODE=YOUR-CODE/' $CONF"
    say "    sudo systemctl restart $SVC"
fi

say ""
say "  peek-display --self-test      prove the wiring"
say "  peek-display --demo           a frame from invented data"
say "  journalctl -u $SVC -f"
say "  sudo nano $CONF"
