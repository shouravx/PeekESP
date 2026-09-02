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

# --- gateway into the tailnet -------------------------------------------
# Reaching THIS box's own 100.x.x.x address needs none of the following: the
# kernel accepts packets addressed to a local IP off any interface. These
# rules are for reaching OTHER tailnet peers, by making the DietPi forward
# and masquerade for the ESP32.
#
# MASQUERADE rather than route advertisement on purpose: the other peers then
# see the traffic as coming from this machine's own tailscale address, so
# nothing needs approving in the admin console. The trade is that it is
# one-way -- other peers cannot open connections back to the ESP32. If you
# want that too, drop the MASQUERADE line and instead run
#   tailscale up --advertise-routes=${WG_SUBNET_BASE}.0/24
# then approve the route at https://login.tailscale.com/admin/machines
#
# iptables accepts rules naming an interface that does not exist yet, so these
# are harmless if Tailscale is not installed.
PostUp = iptables -A FORWARD -i %i -o tailscale0 -j ACCEPT
PostUp = iptables -A FORWARD -i tailscale0 -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostUp = iptables -t nat -A POSTROUTING -o tailscale0 -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -o tailscale0 -j ACCEPT
PostDown = iptables -D FORWARD -i tailscale0 -o %i -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
PostDown = iptables -t nat -D POSTROUTING -o tailscale0 -j MASQUERADE

[Peer]
# ${ESP32_NAME}
PublicKey = ${ESP32_PUB}
AllowedIPs = ${WG_SUBNET_BASE}.2/32
PersistentKeepalive = 25
EOF
chmod 600 ${WG_IF}.conf

echo "==> Enabling IP forwarding (needed to reach tailnet peers other than this box)..."
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
  3. Enter the values above in the ESP32's setup portal (scan the QR code on
     its screen, or browse to 192.168.4.1 while joined to its access point).
  4. Check handshake status any time with:  sudo wg show
     "latest handshake: (none)" means the ESP32's packets are not arriving —
     look at the port forward, not at the keys.

Reaching the tailnet from the ESP32
  This box's own ${TS_IP} works with no extra setup: the kernel answers a
  local address off any interface. Forwarding to OTHER tailnet peers is
  handled by the PostUp rules in ${WG_IF}.conf, which masquerade wg0 traffic
  out of tailscale0 — so those peers see it as coming from this machine.

  That is one-way. If other Tailscale devices should be able to open
  connections TO the ESP32, remove the MASQUERADE line from ${WG_IF}.conf and
  advertise the subnet instead:
    sudo tailscale up --advertise-routes=${WG_SUBNET_BASE}.0/24
    (then approve the route at https://login.tailscale.com/admin/machines)

  Note: MagicDNS names do not resolve on the ESP32 — it has no route to
  Tailscale's resolver. Always give it a literal 100.x.x.x address.
EOF
