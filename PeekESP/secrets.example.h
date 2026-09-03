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
 * gitignored, so nothing you put there lands in the repository.
 *
 * Nothing here is required. The device pairs from a code on its own screen.
 */
#pragma once

// --- The WiFi network the ESP32 physically sits on --------------------------
#define WIFI_SSID          ""   // leave empty -> device boots into setup mode
#define WIFI_PASSWORD      ""

// --- Where the telemetry lives ----------------------------------------------
// Normally you do not set these: pairing fills them in from the code on the
// device's screen. They are here for flashing a batch of devices that should
// come up already pointed at a named or private stream.
// #define RELAY_BASE_URL  "https://peek-relay.you.workers.dev"
