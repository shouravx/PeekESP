/**
 * secrets.example.h  -  OPTIONAL
 *
 * You do not need this file. The device configures itself: on first boot it
 * raises an access point, shows a QR code, and serves a settings form. What you
 * save there lives in NVS and survives reflashing the sketch.
 *
 * This file only seeds the FACTORY DEFAULTS - the values a device falls back to
 * when NVS is empty or has been erased. It is worth filling in if you flash
 * several devices and would rather they came up already configured.
 *
 * Copy it to `secrets.h` in the same folder as PeekESP.ino. `secrets.h` is
 * gitignored, so your WireGuard private key never lands in the repository.
 *
 * Every value below is printed for you by dietpi-wireguard-setup.sh when you
 * run it on the DietPi.
 */
#pragma once

// --- The WiFi network the ESP32 physically sits on --------------------------
#define WIFI_SSID          ""   // leave empty -> device boots into setup mode
#define WIFI_PASSWORD      ""

// --- WireGuard --------------------------------------------------------------
#define WG_LOCAL_IP        "10.10.44.2"                 // this ESP32 inside the tunnel
#define WG_PRIVATE_KEY     ""   // /etc/wireguard/ttgo-dashboard.key
#define WG_PEER_PUBLIC_KEY ""   // /etc/wireguard/dietpi_wg.pub
#define WG_ENDPOINT_HOST   ""   // DietPi public IP or DDNS name
#define WG_ENDPOINT_PORT   51820

// --- Where the telemetry lives ----------------------------------------------
// The DietPi's Tailscale address: `tailscale ip -4` on the DietPi.
// For a first bring-up on your LAN, point this at the DietPi's plain
// 192.168.x.x address and untick the tunnel checkbox in the setup portal.
#define DIETPI_HOST        "100.64.12.3"
#define DIETPI_PORT        8080
#define DIETPI_PATH        "/telemetry"
