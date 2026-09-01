/**
 * secrets.example.h
 *
 * Copy this file to `secrets.h` in the same folder as PeekESP.ino and fill it
 * in. `secrets.h` is gitignored, so your WireGuard private key never lands in
 * the repository. Anything you leave out here falls back to the placeholder
 * defaults in PeekESP.ino.
 *
 * Every value below is printed for you by dietpi-wireguard-setup.sh when you
 * run it on the DietPi.
 */
#pragma once

// --- The WiFi network the ESP32 physically sits on --------------------------
#define WIFI_SSID          "YOUR_WIFI_SSID"
#define WIFI_PASSWORD      "YOUR_WIFI_PASSWORD"

// --- WireGuard --------------------------------------------------------------
#define WG_LOCAL_IP        "10.10.44.2"                 // this ESP32 inside the tunnel
#define WG_PRIVATE_KEY     "ESP32_PRIVATE_KEY_HERE"     // /etc/wireguard/ttgo-dashboard.key
#define WG_PEER_PUBLIC_KEY "DIETPI_WG_PUBLIC_KEY_HERE"  // /etc/wireguard/dietpi_wg.pub
#define WG_ENDPOINT_HOST   "your-home.ddns.net"         // DietPi public IP or DDNS name
#define WG_ENDPOINT_PORT   51820

// --- Where the telemetry lives ----------------------------------------------
// The DietPi's Tailscale address: `tailscale ip -4` on the DietPi.
// For a first bring-up on your LAN you can point this at the DietPi's plain
// 192.168.x.x address and set USE_WIREGUARD to 0 in PeekESP.ino.
#define DIETPI_HOST        "100.64.12.3"
#define DIETPI_PORT        8080
#define DIETPI_PATH        "/telemetry"
