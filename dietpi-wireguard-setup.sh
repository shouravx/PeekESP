#!/usr/bin/env bash
# ==============================================================================
#  dietpi-wireguard-setup.sh
#
#  Turns the DietPi into a WireGuard endpoint for the ESP32 dashboard, fully
#  independent of its existing Tailscale install (two separate interfaces,
#  wg0 and tailscale0, coexisting on the same box).
#
#  Because the ESP32's only target IP (the DietPi's own 100.x.x.x Tailscale
#  address) lives on this same machine, no NAT/iptables forwarding is
#  required — the kernel delivers packets addressed to a local IP regardless
#  of which interface they arrived on. If you later want OTHER Tailscale
#  peers to be able to reach the ESP32 itself, see the --advertise-routes
#  note printed at the end.
#
#  Run this ON THE DIETPI as root:   sudo bash dietpi-wireguard-setup.sh
# ==============================================================================
set -euo pipefail

WG_SUBNET_BASE="10.10.44"   # private /24 just for this tunnel
WG_PORT=51820
WG_IF="wg0"
ESP32_NAME="ttgo-dashboard"

echo "==> Installing WireGuard tools..."
apt-get update -qq
apt-get install -y wireguard >/dev/null

cd /etc/wireguard
umask 077

if [ ! -f dietpi_wg.key ]; then
  echo "==> Generating DietPi keypair..."
  wg genkey | tee dietpi_wg.key | wg pubkey > dietpi_wg.pub
fi

if [ ! -f "${ESP32_NAME}.key" ]; then
  echo "==> Generating ${ESP32_NAME} (ESP32) keypair..."
  wg genkey | tee "${ESP32_NAME}.key" | wg pubkey > "${ESP32_NAME}.pub"
fi

DIETPI_PRIV=$(cat dietpi_wg.key)
DIETPI_PUB=$(cat dietpi_wg.pub)
ESP32_PRIV=$(cat "${ESP32_NAME}.key")
ESP32_PUB=$(cat "${ESP32_NAME}.pub")

cat > ${WG_IF}.conf << EOF
[Interface]
Address = ${WG_SUBNET_BASE}.1/24
ListenPort = ${WG_PORT}
PrivateKey = ${DIETPI_PRIV}

[Peer]
# ${ESP32_NAME}
PublicKey = ${ESP32_PUB}
AllowedIPs = ${WG_SUBNET_BASE}.2/32
PersistentKeepalive = 25
EOF
chmod 600 ${WG_IF}.conf

echo "==> Enabling IP forwarding (defensive default; not strictly required for this single-box setup)..."
sysctl -w net.ipv4.ip_forward=1 >/dev/null
grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf 2>/dev/null || echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf

echo "==> Starting ${WG_IF}..."
systemctl enable --now "wg-quick@${WG_IF}"

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "==> UFW is active: opening UDP ${WG_PORT}..."
  ufw allow ${WG_PORT}/udp
fi

TS_IP="$(tailscale ip -4 2>/dev/null || echo '<run: tailscale ip -4>')"

cat << EOF

================================================================================
 Done. Paste these into the USER CONFIG section of main.cpp on the ESP32:
--------------------------------------------------------------------------------
 WG_LOCAL_IP        = IPAddress(${WG_SUBNET_BASE//./, }, 2)
 WG_PRIVATE_KEY      = "${ESP32_PRIV}"
 WG_ENDPOINT_ADDR     = "<this DietPi's public IP or DDNS hostname>"
 WG_ENDPOINT_PUB      = "${DIETPI_PUB}"
 WG_ENDPOINT_PORT     = ${WG_PORT}

 DIETPI_TAILSCALE_IP  = "${TS_IP}"
================================================================================

Next steps:
  1. Forward UDP ${WG_PORT} to this machine on your router, unless it already
     has a directly reachable public IP.
  2. Make sure your telemetry HTTP server (returning the JSON the ESP32
     expects) is listening on 0.0.0.0:8080, not just 127.0.0.1.
  3. Check handshake status any time with:  sudo wg show

Optional — only needed if OTHER Tailscale devices (not just this ESP32
dashboard) should be able to reach the ESP32 by its own address:
  sudo tailscale up --advertise-routes=${WG_SUBNET_BASE}.0/24
  (then approve the route at https://login.tailscale.com/admin/machines)
EOF
