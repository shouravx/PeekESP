/**
 * ============================================================================
 *  PeekESP - a physical dashboard for a remote DietPi box
 *  LilyGO TTGO T-Display (ESP32) - ST7789V 135x240 - LVGL 8.x - WireGuard
 * ============================================================================
 *
 *  Core 0 : WiFi -> NTP -> WireGuard tunnel -> blocking HTTP GET -> JSON.
 *  Core 1 : LVGL and nothing else. It reads a mutex-protected snapshot of the
 *           telemetry and drives 500 ms eased animations. Network latency on
 *           core 0 can never stall a frame on core 1.
 *
 *  Two run modes, chosen at boot:
 *    DASHBOARD - the normal display, using settings stored in NVS.
 *    SETUP     - no usable config yet, or the left button was held at boot.
 *                The device raises its own access point and serves a config
 *                page; the screen shows a QR code that joins you to it.
 *
 *  ----------------------------------------------------------------------
 *  ARDUINO IDE SETUP (do these once, or nothing will compile / display)
 *  ----------------------------------------------------------------------
 *  1. Boards Manager -> "esp32" by Espressif, install 2.0.17.
 *       Core 3.x is built on ESP-IDF 5, which removed the tcpip_adapter API
 *       that WireGuard-ESP32 still uses. Stay on 2.0.x.
 *     Board: "LilyGo T-Display" (or "ESP32 Dev Module" - both verified)
 *     Partition Scheme: "Huge APP (3MB No OTA/1MB SPIFFS)"
 *
 *  2. Library Manager: "lvgl" (8.3.x - NOT 9.x) and "ArduinoJson".
 *     Library Manager offers lvgl 9.x first; pick 8.3.9. This sketch uses the
 *     v8 API and will not build against v9.
 *
 *  3. TFT_eSPI from LilyGO, who ship a copy already configured for this exact
 *     board - copy the TFT_eSPI folder out of
 *       https://github.com/Xinyuan-LilyGO/TTGO-T-Display
 *     into <Arduino>/libraries/. Its User_Setup_Select.h already points at
 *     Setup25_TTGO_T_Display.h, so there is nothing to edit and no way to end
 *     up with the image offset by 40 px.
 *     (Library Manager's TFT_eSPI 2.5.43 also works - both are verified - but
 *     then you must edit User_Setup_Select.h by hand: comment out
 *     "#include <User_Setup.h>" and uncomment the Setup25 line.)
 *
 *  4. WireGuard-ESP32, only if you use the DIRECT transport: Sketch ->
 *     Include Library -> Add .ZIP Library, using
 *       https://github.com/ciniml/WireGuard-ESP32-Arduino  (Code -> Download ZIP)
 *
 *  5. LVGL config. Copy this repo's lv_conf.h to
 *       <Arduino>/libraries/lv_conf.h     (next to the lvgl folder, NOT inside it)
 *     LV_USE_SPINNER and LV_USE_QRCODE both default to 0 upstream, so the
 *     stock config link-errors on two widgets this sketch uses.
 *
 *  6. secrets.h is now OPTIONAL - it only seeds the factory defaults. Real
 *     configuration happens on-device through the setup portal. Copy
 *     secrets.example.h to secrets.h if you would rather bake yours in.
 * ============================================================================
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <time.h>
#include <math.h>
#include <esp_system.h>
#include <mbedtls/sha256.h>

#include "ca_certs.h"

#include <TFT_eSPI.h>
#include <lvgl.h>
#include <ArduinoJson.h>
#include <WireGuard-ESP32.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

// ============================================================================
//  FACTORY DEFAULTS
//  These are only the values a freshly-flashed device starts from. Whatever
//  you save through the setup portal lives in NVS and wins over all of them.
// ============================================================================
#if __has_include("secrets.h")
  #include "secrets.h"
#endif

#ifndef WIFI_SSID
  #define WIFI_SSID              ""
#endif
#ifndef WIFI_PASSWORD
  #define WIFI_PASSWORD          ""
#endif
#ifndef WG_LOCAL_IP
  #define WG_LOCAL_IP            "10.10.44.2"
#endif
#ifndef WG_PRIVATE_KEY
  #define WG_PRIVATE_KEY         ""
#endif
#ifndef WG_PEER_PUBLIC_KEY
  #define WG_PEER_PUBLIC_KEY     ""
#endif
#ifndef WG_ENDPOINT_HOST
  #define WG_ENDPOINT_HOST       ""
#endif
#ifndef WG_ENDPOINT_PORT
  #define WG_ENDPOINT_PORT       51820
#endif
#ifndef DIETPI_HOST
  #define DIETPI_HOST            "100.64.12.3"
#endif
#ifndef DIETPI_PORT
  #define DIETPI_PORT            8080
#endif
#ifndef DIETPI_PATH
  #define DIETPI_PATH            "/telemetry"
#endif

#define ANIM_MS                  500   // the sweep duration the arcs/bar use
#define HTTP_TIMEOUT_MS          8000  // TLS handshakes need more than plain HTTP
#define MAX_CONSECUTIVE_FAILURES 12    // ~60 s of nothing -> reboot the stack
#define SETUP_HOLD_MS            1500  // left-button hold that forces setup mode
#define STALE_AFTER_S            30    // relay data older than this reads as stale

// How the device reaches its telemetry.
//   DIRECT - straight HTTP to the host, over a WireGuard tunnel or the LAN.
//            Needs the host to accept an inbound connection: a port forward,
//            or a tunnel terminating on it.
//   RELAY  - HTTPS to a Cloudflare Worker that the host pushes to. Both ends
//            only dial OUT, so it works behind CGNAT and on networks you do
//            not administer. See cloudflare/ in this repo.
#define TRANSPORT_DIRECT 0
#define TRANSPORT_RELAY  1
//   PAIRED - the default. The device invents a one-time code, shows it, and
//            derives its stream and read token from it. Typing that code into
//            the PeekESP app makes the app derive the same values, and the two
//            meet on the relay. No URLs, no tokens, nothing else to enter.
#define TRANSPORT_PAIRED 2

// Where a freshly flashed device looks unless told otherwise. Change this if
// you deploy your own Worker - see cloudflare/ in the repository.
#ifndef RELAY_BASE_URL
  #define RELAY_BASE_URL "https://peek-relay.peekesp.workers.dev"
#endif

// 32 characters with I, O, 0 and 1 removed - the pairs people mistype reading
// a code off a 1.14" screen. 10 characters is 2^50.
#define PAIR_ALPHABET "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
#define PAIR_CODE_LEN 10

// Expected JSON (see dietpi/peek-agent.py):
//   { "host":"dietpi", "cpu_percent":12.5, "ram_percent":43.2,
//     "storage_percent":61.0, "cpu_temp_c":48.3, "uptime_seconds":271830,
//     "net_rx_kbps":128.4, "net_tx_kbps":12.9 }

// ============================================================================
//  Palette + geometry
// ============================================================================
#define COL_BG        lv_color_hex(0x05070E)
#define COL_PANEL     lv_color_hex(0x0B1220)
#define COL_TRACK     lv_color_hex(0x1A2334)
#define COL_CYAN      lv_color_hex(0x00E5FF)
#define COL_MAGENTA   lv_color_hex(0xFF2E7E)
#define COL_AMBER     lv_color_hex(0xFFC145)
#define COL_GREEN     lv_color_hex(0x35F2A0)
#define COL_RED       lv_color_hex(0xFF4D6D)
#define COL_TEXT      lv_color_hex(0xE6EDF7)
#define COL_TEXT_DIM  lv_color_hex(0x5C6B82)

static const uint16_t SCREEN_W = 240;   // rotation 1 = landscape
static const uint16_t SCREEN_H = 135;

#if LV_FONT_MONTSERRAT_12
  #define F_SM  &lv_font_montserrat_12
#else
  #define F_SM  LV_FONT_DEFAULT
#endif
#if LV_FONT_MONTSERRAT_20
  #define F_BIG &lv_font_montserrat_20
#else
  #define F_BIG LV_FONT_DEFAULT
#endif

// ============================================================================
//  TYPES
//  Every type used in a function signature must be declared here, above the
//  first function definition. The Arduino IDE generates prototypes for the
//  whole sketch and injects them immediately before that first definition, so
//  a type declared lower down produces "'X' was not declared in this scope"
//  pointing at a line that looks perfectly valid.
// ============================================================================
struct Telemetry {
  float    cpu_percent     = 0;
  float    ram_percent     = 0;
  float    storage_percent = 0;
  float    cpu_temp_c      = -1;      // <0 = host did not report one
  float    rx_kbps         = 0;
  float    tx_kbps         = 0;
  uint32_t uptime_seconds  = 0;
  uint32_t age_s           = 0;       // relay only: seconds since the host pushed
  char     host[20]        = "dietpi";
};

enum NetState : uint8_t {
  NET_BOOT, NET_WIFI, NET_TIME, NET_TUNNEL, NET_ONLINE, NET_STALE, NET_ERROR
};

struct Gauge {
  lv_obj_t  *arc   = nullptr;
  lv_obj_t  *value = nullptr;
  lv_color_t base  = COL_CYAN;
  int32_t    shown = 0;   // current on-screen value, 0..1000 (tenths of a %)
};

// Everything the setup portal can change. Sizes are the protocol maxima:
// an SSID is 32 bytes, a WPA2 passphrase 63, a WireGuard key 44 base64 chars.
struct Config {
  char     wifi_ssid[33]   = WIFI_SSID;
  char     wifi_pass[64]   = WIFI_PASSWORD;

  uint8_t  transport       = TRANSPORT_PAIRED;
  char     relay_url[128]  = "";      // https://<name>.workers.dev/telemetry
  char     relay_token[65] = "";      // READ_TOKEN from the Worker
  char     relay_base[96]  = RELAY_BASE_URL;   // pairing builds its URL from this
  char     pair_code[12]   = "";      // one-time code shown on screen
  bool     tls_verify      = true;    // pin the roots in ca_certs.h

  bool     wg_enabled      = true;
  char     wg_local_ip[16] = WG_LOCAL_IP;
  char     wg_priv[48]     = WG_PRIVATE_KEY;
  char     wg_peer_pub[48] = WG_PEER_PUBLIC_KEY;
  char     wg_host[64]     = WG_ENDPOINT_HOST;
  uint16_t wg_port         = WG_ENDPOINT_PORT;
  char     host[64]        = DIETPI_HOST;
  uint16_t port            = DIETPI_PORT;
  char     path[48]        = DIETPI_PATH;
  uint16_t poll_s          = 5;
  uint8_t  bl_idx          = 0;
};

// ============================================================================
//  Shared state between core 0 (network) and core 1 (UI)
// ============================================================================
static Config            cfg;
static Preferences       prefs;

static Telemetry         g_telemetry;              // guarded by g_lock
static SemaphoreHandle_t g_lock = nullptr;

static volatile NetState g_state    = NET_BOOT;
static volatile bool     g_busy     = false;       // an HTTP GET is in flight
static volatile uint32_t g_seq      = 0;           // bumped on every good parse
static volatile uint32_t g_latency  = 0;           // ms for the last GET
static volatile bool     g_force    = false;       // button-triggered refresh
static volatile uint32_t g_reboot_at = 0;          // 0 = not scheduled

static bool     g_setup_mode = false;
static char     g_ap_ssid[24];
static char     g_ap_pass[16];

static WireGuard wg;
static WebServer server(80);
static DNSServer dns;

// ============================================================================
//  Display + LVGL plumbing
// ============================================================================
static TFT_eSPI tft = TFT_eSPI();

static lv_disp_draw_buf_t draw_buf;
// One 40-line partial buffer, ~19 KB. A second buffer would only pay for
// itself with a DMA flush; pushColors() below is synchronous, so LVGL would
// wait on it either way and the extra 19 KB would buy nothing.
static lv_color_t lv_buf[SCREEN_W * 40];

static const int PIN_BL      = 4;
static const int PIN_BTN_L   = 0;    // "BOOT" - refresh, or hold for setup mode
static const int PIN_BTN_R   = 35;   // cycle backlight brightness
static const int BL_CHANNEL  = 0;

static const uint8_t BL_LEVELS[] = { 255, 150, 70, 20 };
#define BL_LEVEL_COUNT (sizeof(BL_LEVELS) / sizeof(BL_LEVELS[0]))

static void backlight_init() {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcAttach(PIN_BL, 5000, 8);
#else
  ledcSetup(BL_CHANNEL, 5000, 8);
  ledcAttachPin(PIN_BL, BL_CHANNEL);
#endif
}

static void backlight_set(uint8_t duty) {
#if defined(ESP_ARDUINO_VERSION_MAJOR) && ESP_ARDUINO_VERSION_MAJOR >= 3
  ledcWrite(PIN_BL, duty);
#else
  ledcWrite(BL_CHANNEL, duty);
#endif
}

static void disp_flush(lv_disp_drv_t *disp, const lv_area_t *area, lv_color_t *color_p) {
  const uint32_t w = area->x2 - area->x1 + 1;
  const uint32_t h = area->y2 - area->y1 + 1;

  tft.startWrite();
  tft.setAddrWindow(area->x1, area->y1, w, h);
  // lv_conf.h keeps LV_COLOR_16_SWAP at 0, so TFT_eSPI does the byte swap.
  // Written this way the sketch stays correct if you flip that setting.
  tft.pushColors((uint16_t *)&color_p->full, w * h, !LV_COLOR_16_SWAP);
  tft.endWrite();

  lv_disp_flush_ready(disp);
}

// ============================================================================
//  Configuration in NVS
//  Preferences keys are capped at 15 characters, hence the terse names.
// ============================================================================
static void config_load() {
  prefs.begin("peek", true);            // read-only
  prefs.getString("ssid",   cfg.wifi_ssid,   sizeof cfg.wifi_ssid);
  prefs.getString("pass",   cfg.wifi_pass,   sizeof cfg.wifi_pass);
  prefs.getString("wgip",   cfg.wg_local_ip, sizeof cfg.wg_local_ip);
  prefs.getString("wgpriv", cfg.wg_priv,     sizeof cfg.wg_priv);
  prefs.getString("wgpub",  cfg.wg_peer_pub, sizeof cfg.wg_peer_pub);
  prefs.getString("wghost", cfg.wg_host,     sizeof cfg.wg_host);
  prefs.getString("host",   cfg.host,        sizeof cfg.host);
  prefs.getString("path",   cfg.path,        sizeof cfg.path);
  prefs.getString("rurl",   cfg.relay_url,   sizeof cfg.relay_url);
  prefs.getString("rtok",   cfg.relay_token, sizeof cfg.relay_token);
  prefs.getString("rbase",  cfg.relay_base,  sizeof cfg.relay_base);
  prefs.getString("pcode",  cfg.pair_code,   sizeof cfg.pair_code);
  cfg.transport  = prefs.getUChar("tport", cfg.transport);
  cfg.tls_verify = prefs.getBool("tlsv",   cfg.tls_verify);
  cfg.wg_enabled = prefs.getBool("wgen",   cfg.wg_enabled);
  cfg.wg_port    = prefs.getUShort("wgport", cfg.wg_port);
  cfg.port       = prefs.getUShort("port",   cfg.port);
  cfg.poll_s     = prefs.getUShort("poll",   cfg.poll_s);
  cfg.bl_idx     = prefs.getUChar("bl",      cfg.bl_idx);
  prefs.end();

  if (cfg.bl_idx >= BL_LEVEL_COUNT) cfg.bl_idx = 0;
  if (cfg.poll_s < 1)   cfg.poll_s = 1;      // a 0 here would busy-loop core 0
  if (cfg.poll_s > 600) cfg.poll_s = 600;
}

static void config_save() {
  prefs.begin("peek", false);
  prefs.putString("ssid",   cfg.wifi_ssid);
  prefs.putString("pass",   cfg.wifi_pass);
  prefs.putString("wgip",   cfg.wg_local_ip);
  prefs.putString("wgpriv", cfg.wg_priv);
  prefs.putString("wgpub",  cfg.wg_peer_pub);
  prefs.putString("wghost", cfg.wg_host);
  prefs.putString("host",   cfg.host);
  prefs.putString("path",   cfg.path);
  prefs.putString("rurl",   cfg.relay_url);
  prefs.putString("rtok",   cfg.relay_token);
  prefs.putString("rbase",  cfg.relay_base);
  prefs.putString("pcode",  cfg.pair_code);
  prefs.putUChar ("tport",  cfg.transport);
  prefs.putBool  ("tlsv",   cfg.tls_verify);
  prefs.putBool  ("wgen",   cfg.wg_enabled);
  prefs.putUShort("wgport", cfg.wg_port);
  prefs.putUShort("port",   cfg.port);
  prefs.putUShort("poll",   cfg.poll_s);
  prefs.putUChar ("bl",     cfg.bl_idx);
  prefs.end();
}

static void config_save_backlight() {      // called from the button handler
  prefs.begin("peek", false);
  prefs.putUChar("bl", cfg.bl_idx);
  prefs.end();
}

static void config_erase() {
  prefs.begin("peek", false);
  prefs.clear();
  prefs.end();
}

// ============================================================================
//  Pairing
//
//  The device invents a code, shows it, and derives everything else from it:
//
//    stream = SHA-256("peek-stream:" + CODE)  first 16 hex
//    read   = SHA-256("peek-read:"   + CODE)  first 48 hex
//
//  The app you type the code into runs the identical derivation, so both ends
//  arrive at the same stream and token without ever sending the code anywhere.
//  The relay never learns it either - a paired stream authenticates by
//  claiming its token on first use.
//
//  Keep this byte-identical to windows/peek_pair.py and derivePairing() in
//  cloudflare/src/index.js. Vector, checked against openssl and pinned in the
//  Worker's test suite:
//
//    K7M2P4QX9R -> stream 4b907ba136d0a7f2
//                  read   ec3cb3699bd1284efb2fcfe056609e87edf4813b84e9ce84
// ============================================================================
static void sha256_prefixed_hex(const char *prefix, const char *code,
                                char *out, size_t out_size) {
  uint8_t digest[32];
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts_ret(&ctx, 0);          // 0 = SHA-256, not SHA-224
  mbedtls_sha256_update_ret(&ctx, (const unsigned char *)prefix, strlen(prefix));
  mbedtls_sha256_update_ret(&ctx, (const unsigned char *)code, strlen(code));
  mbedtls_sha256_finish_ret(&ctx, digest);
  mbedtls_sha256_free(&ctx);

  // Not named HEX: Arduino's Print.h defines that as the numeric constant 16,
  // so a local array of that name expands to "static const char 16[]".
  static const char HEXD[] = "0123456789abcdef";
  size_t bytes = (out_size - 1) / 2;
  if (bytes > sizeof digest) bytes = sizeof digest;
  for (size_t i = 0; i < bytes; i++) {
    out[i * 2]     = HEXD[digest[i] >> 4];
    out[i * 2 + 1] = HEXD[digest[i] & 0x0F];
  }
  out[bytes * 2] = '\0';
}

static void pair_new_code(char *out, size_t out_size) {
  static const char AB[] = PAIR_ALPHABET;
  const size_t n = sizeof(AB) - 1;
  size_t len = out_size - 1;
  if (len > PAIR_CODE_LEN) len = PAIR_CODE_LEN;
  for (size_t i = 0; i < len; i++) {
    // esp_random() is the hardware RNG and is properly seeded once WiFi or
    // Bluetooth is up; at this point in boot it is still good enough for a
    // code that only has to be unguessable, not cryptographic.
    out[i] = AB[esp_random() % n];
  }
  out[len] = '\0';
}

// "K7M2P4QX9R" -> "K7M2-P4QX-9R", which is far easier to read off the screen
// and retype without transposing a pair.
static void pair_format(const char *code, char *out, size_t out_size) {
  size_t o = 0;
  for (size_t i = 0; code[i] && o + 2 < out_size; i++) {
    if (i && i % 4 == 0) out[o++] = '-';
    out[o++] = code[i];
  }
  out[o] = '\0';
}

// Fills relay_url and relay_token from the code. Called after the code is
// known, so the rest of the firmware just uses the relay path unchanged.
static void pair_apply(Config &c) {
  if (c.pair_code[0] == '\0') return;
  char stream[17];
  sha256_prefixed_hex("peek-stream:", c.pair_code, stream, sizeof stream);
  sha256_prefixed_hex("peek-read:", c.pair_code, c.relay_token, 49);
  snprintf(c.relay_url, sizeof c.relay_url, "%s/telemetry/%s", c.relay_base, stream);
  Serial.printf("[pair] code %s -> stream %s\n", c.pair_code, stream);
}

// A device with no SSID has nothing to connect to, so it goes to setup.
// The placeholder is treated as unset too: an older secrets.h carrying
// "YOUR_WIFI_SSID" is non-empty, and without this check the device would
// consider itself configured and never offer the setup portal at all.
static bool config_usable() {
  return cfg.wifi_ssid[0] != '\0' && strcmp(cfg.wifi_ssid, "YOUR_WIFI_SSID") != 0;
}

static void request_setup_mode() {
  prefs.begin("peek", false);
  prefs.putBool("forcecfg", true);
  prefs.end();
  g_reboot_at = millis() + 300;
}

static bool consume_setup_request() {
  prefs.begin("peek", false);
  const bool forced = prefs.getBool("forcecfg", false);
  if (forced) prefs.putBool("forcecfg", false);   // one-shot
  prefs.end();
  return forced;
}

// ============================================================================
//  Widgets   (struct Gauge is declared up in the TYPES section)
// ============================================================================
static Gauge     g_cpu, g_ram;
static lv_obj_t *g_bar       = nullptr;
static lv_obj_t *g_bar_value = nullptr;
static int32_t   g_bar_shown = 0;
static lv_obj_t *g_dot       = nullptr;
static lv_obj_t *g_spinner   = nullptr;
static lv_obj_t *g_lbl_temp  = nullptr;
static lv_obj_t *g_lbl_net   = nullptr;
static lv_obj_t *g_lbl_ping  = nullptr;
static lv_obj_t *g_lbl_host  = nullptr;
static lv_obj_t *g_lbl_foot  = nullptr;
static lv_obj_t *g_lbl_state = nullptr;
static lv_obj_t *g_pair_panel   = nullptr;
static lv_obj_t *g_lbl_code     = nullptr;
static lv_obj_t *g_lbl_pair_hint = nullptr;

// ---------------------------------------------------------------------------
//  Animation: every value sweeps to its new reading instead of snapping.
//  Gauges run at 0..1000 rather than 0..100 so a 3 % change still produces
//  ~30 distinct steps over the 500 ms - no visible staircase.
// ---------------------------------------------------------------------------
static void arc_anim_cb(void *var, int32_t v) {
  Gauge *g = (Gauge *)var;
  g->shown = v;
  lv_arc_set_value(g->arc, v);
  lv_label_set_text_fmt(g->value, "%d", (int)((v + 5) / 10));
}

static void bar_anim_cb(void *var, int32_t v) {
  (void)var;
  g_bar_shown = v;
  lv_bar_set_value(g_bar, v, LV_ANIM_OFF);   // we drive the easing ourselves
  lv_label_set_text_fmt(g_bar_value, "%d%%", (int)((v + 5) / 10));
}

static void sweep(void *var, lv_anim_exec_xcb_t cb, int32_t from, int32_t to) {
  if (from == to) return;
  lv_anim_t a;
  lv_anim_init(&a);
  lv_anim_set_var(&a, var);
  lv_anim_set_exec_cb(&a, cb);
  lv_anim_set_values(&a, from, to);
  lv_anim_set_time(&a, ANIM_MS);
  lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
  lv_anim_start(&a);   // replaces any in-flight anim on the same (var, cb)
}

// Load colour: the accent stays on-brand until the reading gets uncomfortable.
static lv_color_t load_color(lv_color_t base, int32_t tenths) {
  if (tenths >= 900) return COL_RED;
  if (tenths >= 750) return COL_AMBER;
  return base;
}

static int32_t to_tenths(float pct) {
  if (isnan(pct)) return 0;
  long v = lroundf(pct * 10.0f);
  if (v < 0)    v = 0;
  if (v > 1000) v = 1000;
  return (int32_t)v;
}

static void gauge_set(Gauge *g, float pct) {
  const int32_t target = to_tenths(pct);
  lv_obj_set_style_arc_color(g->arc, load_color(g->base, target), LV_PART_INDICATOR);
  sweep(g, arc_anim_cb, g->shown, target);
}

static void bar_set(float pct) {
  const int32_t target = to_tenths(pct);
  lv_obj_set_style_bg_color(g_bar, load_color(COL_CYAN, target), LV_PART_INDICATOR);
  sweep(g_bar, bar_anim_cb, g_bar_shown, target);
}

// ---------------------------------------------------------------------------
//  Status dot: a "radar ping" - the dot brightens while a ring expands out of
//  it and fades. Outline instead of a shadow, because outlines need no blur
//  pass and so cost almost nothing per frame.
// ---------------------------------------------------------------------------
static void dot_anim_cb(void *var, int32_t v) {   // v: 0..255
  lv_obj_t *o = (lv_obj_t *)var;
  lv_obj_set_style_bg_opa(o, (lv_opa_t)(90 + (v * 165) / 255), 0);
  lv_obj_set_style_outline_width(o, 1 + (v * 6) / 255, 0);
  lv_obj_set_style_outline_opa(o, (lv_opa_t)(255 - v), 0);
}

static void pulse(lv_color_t color, uint16_t period_ms) {
  lv_anim_del(g_dot, dot_anim_cb);
  lv_obj_set_style_bg_color(g_dot, color, 0);
  lv_obj_set_style_outline_color(g_dot, color, 0);

  lv_anim_t a;
  lv_anim_init(&a);
  lv_anim_set_var(&a, g_dot);
  lv_anim_set_exec_cb(&a, dot_anim_cb);
  lv_anim_set_values(&a, 0, 255);
  lv_anim_set_time(&a, period_ms);
  lv_anim_set_playback_time(&a, period_ms);
  lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
  lv_anim_set_path_cb(&a, lv_anim_path_ease_in_out);
  lv_anim_start(&a);
}

static void apply_state(NetState st) {
  switch (st) {
    case NET_BOOT:   lv_label_set_text(g_lbl_state, "BOOT");      pulse(COL_TEXT_DIM, 900); break;
    case NET_WIFI:   lv_label_set_text(g_lbl_state, "WIFI...");   pulse(COL_AMBER,    450); break;
    case NET_TIME:   lv_label_set_text(g_lbl_state, "NTP SYNC");  pulse(COL_AMBER,    450); break;
    case NET_TUNNEL: lv_label_set_text(g_lbl_state, "TUNNEL..."); pulse(COL_AMBER,    450); break;
    case NET_ONLINE: lv_label_set_text(g_lbl_state, "LINK OK");   pulse(COL_GREEN,   1400); break;
    case NET_STALE:  lv_label_set_text(g_lbl_state, "STALE");     pulse(COL_AMBER,    700); break;
    case NET_ERROR:  lv_label_set_text(g_lbl_state, "NO LINK");   pulse(COL_RED,      300); break;
  }
}

// ---------------------------------------------------------------------------
//  Small formatters. LVGL's built-in printf has no float support, so anything
//  with a decimal point goes through newlib's snprintf first.
// ---------------------------------------------------------------------------
static void fmt_rate(char *out, size_t n, float kbps) {
  if (kbps >= 1000.0f) snprintf(out, n, "%.1fM", kbps / 1024.0f);
  else                 snprintf(out, n, "%.0fK", kbps);
}

static void fmt_uptime(char *out, size_t n, uint32_t s) {
  const uint32_t d = s / 86400u, h = (s % 86400u) / 3600u, m = (s % 3600u) / 60u;
  if      (d) snprintf(out, n, "up %ud %uh", (unsigned)d, (unsigned)h);
  else if (h) snprintf(out, n, "up %uh %um", (unsigned)h, (unsigned)m);
  else        snprintf(out, n, "up %um",     (unsigned)m);
}

// ---------------------------------------------------------------------------
//  UI construction - runs once on core 1, before either task starts.
// ---------------------------------------------------------------------------
static lv_obj_t *make_label(lv_obj_t *parent, const lv_font_t *font, lv_color_t color,
                            int16_t x, int16_t y, const char *text) {
  lv_obj_t *l = lv_label_create(parent);
  lv_label_set_long_mode(l, LV_LABEL_LONG_CLIP);
  lv_label_set_text(l, text);
  lv_obj_set_style_text_font(l, font, 0);
  lv_obj_set_style_text_color(l, color, 0);
  lv_obj_set_pos(l, x, y);
  return l;
}

static void screen_base(lv_obj_t *scr) {
  lv_obj_set_style_bg_color(scr, COL_BG, 0);
  lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
  lv_obj_set_style_pad_all(scr, 0, 0);
  lv_obj_set_style_border_width(scr, 0, 0);
  lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_scrollbar_mode(scr, LV_SCROLLBAR_MODE_OFF);
}

static void make_gauge(Gauge *g, lv_obj_t *parent, int16_t x, int16_t y,
                       lv_color_t accent, const char *caption) {
  g->base  = accent;
  g->shown = 0;

  g->arc = lv_arc_create(parent);
  lv_obj_set_size(g->arc, 66, 66);
  lv_obj_set_pos(g->arc, x, y);
  lv_arc_set_rotation(g->arc, 135);
  lv_arc_set_bg_angles(g->arc, 0, 270);
  lv_arc_set_range(g->arc, 0, 1000);
  lv_arc_set_value(g->arc, 0);
  lv_obj_remove_style(g->arc, NULL, LV_PART_KNOB);      // no drag handle
  lv_obj_clear_flag(g->arc, LV_OBJ_FLAG_CLICKABLE);
  lv_obj_set_style_bg_opa(g->arc, LV_OPA_TRANSP, LV_PART_MAIN);
  lv_obj_set_style_border_width(g->arc, 0, LV_PART_MAIN);
  lv_obj_set_style_pad_all(g->arc, 0, LV_PART_MAIN);
  lv_obj_set_style_arc_color(g->arc, COL_TRACK, LV_PART_MAIN);
  lv_obj_set_style_arc_width(g->arc, 6, LV_PART_MAIN);
  lv_obj_set_style_arc_rounded(g->arc, true, LV_PART_MAIN);
  lv_obj_set_style_arc_color(g->arc, accent, LV_PART_INDICATOR);
  lv_obj_set_style_arc_width(g->arc, 6, LV_PART_INDICATOR);
  lv_obj_set_style_arc_rounded(g->arc, true, LV_PART_INDICATOR);

  g->value = lv_label_create(g->arc);
  lv_label_set_text(g->value, "0");
  lv_obj_set_style_text_font(g->value, F_BIG, 0);
  lv_obj_set_style_text_color(g->value, COL_TEXT, 0);
  lv_obj_align(g->value, LV_ALIGN_CENTER, 0, -5);

  lv_obj_t *cap = lv_label_create(g->arc);
  lv_label_set_text(cap, caption);
  lv_obj_set_style_text_font(cap, F_SM, 0);
  lv_obj_set_style_text_color(cap, COL_TEXT_DIM, 0);
  lv_obj_align(cap, LV_ALIGN_CENTER, 0, 14);
}

static void build_dashboard_ui() {
  lv_obj_t *scr = lv_scr_act();
  screen_base(scr);

  // ---- header ----
  make_label(scr, F_SM, COL_CYAN, 8, 3, "PEEK");
  g_lbl_host = make_label(scr, F_SM, COL_TEXT_DIM, 44, 3, "// dietpi");

  g_lbl_ping = make_label(scr, F_SM, COL_TEXT_DIM, 140, 3, "-- ms");
  lv_obj_set_width(g_lbl_ping, 66);
  lv_obj_set_style_text_align(g_lbl_ping, LV_TEXT_ALIGN_RIGHT, 0);

  lv_obj_t *rule = lv_obj_create(scr);
  lv_obj_remove_style_all(rule);
  lv_obj_set_size(rule, 224, 1);
  lv_obj_set_pos(rule, 8, 19);
  lv_obj_set_style_bg_color(rule, COL_CYAN, 0);
  lv_obj_set_style_bg_opa(rule, LV_OPA_30, 0);

  // Spinner ring, drawn around the dot, visible only while a GET is in flight.
  g_spinner = lv_spinner_create(scr, 900, 70);
  lv_obj_set_size(g_spinner, 20, 20);
  lv_obj_set_pos(g_spinner, 217, 0);
  lv_obj_remove_style(g_spinner, NULL, LV_PART_KNOB);
  lv_obj_set_style_arc_width(g_spinner, 2, LV_PART_MAIN);
  lv_obj_set_style_arc_opa(g_spinner, LV_OPA_TRANSP, LV_PART_MAIN);
  lv_obj_set_style_arc_width(g_spinner, 2, LV_PART_INDICATOR);
  lv_obj_set_style_arc_color(g_spinner, COL_CYAN, LV_PART_INDICATOR);
  lv_obj_add_flag(g_spinner, LV_OBJ_FLAG_HIDDEN);

  g_dot = lv_obj_create(scr);
  lv_obj_remove_style_all(g_dot);
  lv_obj_set_size(g_dot, 10, 10);
  lv_obj_set_pos(g_dot, 222, 5);
  lv_obj_set_style_radius(g_dot, LV_RADIUS_CIRCLE, 0);
  lv_obj_set_style_bg_opa(g_dot, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(g_dot, COL_AMBER, 0);
  lv_obj_set_style_outline_pad(g_dot, 1, 0);
  lv_obj_set_style_outline_width(g_dot, 0, 0);

  // ---- gauges ----
  make_gauge(&g_cpu, scr,  6, 23, COL_CYAN,    "CPU %");
  make_gauge(&g_ram, scr, 78, 23, COL_MAGENTA, "RAM %");

  // ---- temperature / throughput panel ----
  lv_obj_t *panel = lv_obj_create(scr);
  lv_obj_remove_style_all(panel);
  lv_obj_set_size(panel, 84, 66);
  lv_obj_set_pos(panel, 150, 23);
  lv_obj_set_style_bg_color(panel, COL_PANEL, 0);
  lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
  lv_obj_set_style_radius(panel, 6, 0);
  lv_obj_set_style_border_width(panel, 1, 0);
  lv_obj_set_style_border_color(panel, COL_TRACK, 0);
  lv_obj_set_style_pad_all(panel, 4, 0);
  lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);

  make_label(panel, F_SM, COL_TEXT_DIM, 0, 0, "TEMP");
  g_lbl_temp = make_label(panel, F_BIG, COL_TEXT, 0, 13, "--");
  g_lbl_net  = make_label(panel, F_SM,  COL_TEXT_DIM, 0, 40,
                          LV_SYMBOL_DOWN " --  " LV_SYMBOL_UP " --");

  // ---- storage ----
  make_label(scr, F_SM, COL_TEXT_DIM, 8, 92, "STORAGE");
  g_bar_value = make_label(scr, F_SM, COL_TEXT, 162, 92, "0%");
  lv_obj_set_width(g_bar_value, 70);
  lv_obj_set_style_text_align(g_bar_value, LV_TEXT_ALIGN_RIGHT, 0);

  g_bar = lv_bar_create(scr);
  lv_obj_set_size(g_bar, 226, 8);
  lv_obj_set_pos(g_bar, 7, 108);
  lv_bar_set_range(g_bar, 0, 1000);
  lv_bar_set_value(g_bar, 0, LV_ANIM_OFF);
  lv_obj_set_style_bg_color(g_bar, COL_TRACK, LV_PART_MAIN);
  lv_obj_set_style_radius(g_bar, 4, LV_PART_MAIN);
  lv_obj_set_style_bg_color(g_bar, COL_CYAN, LV_PART_INDICATOR);
  lv_obj_set_style_bg_grad_color(g_bar, COL_MAGENTA, LV_PART_INDICATOR);
  lv_obj_set_style_bg_grad_dir(g_bar, LV_GRAD_DIR_HOR, LV_PART_INDICATOR);
  lv_obj_set_style_radius(g_bar, 4, LV_PART_INDICATOR);

  // ---- footer ----
  g_lbl_foot  = make_label(scr, F_SM, COL_TEXT_DIM,   8, 118, "waiting for host");
  g_lbl_state = make_label(scr, F_SM, COL_TEXT_DIM, 124, 118, "BOOT");
  lv_obj_set_width(g_lbl_state, 108);
  lv_obj_set_style_text_align(g_lbl_state, LV_TEXT_ALIGN_RIGHT, 0);

  apply_state(NET_BOOT);
}

// ---------------------------------------------------------------------------
//  Pairing overlay - covers the dashboard until the first reading arrives.
//
//  An overlay rather than a second screen so there is one object tree and one
//  place data lands; pairing finishing is then just hiding a panel, with no
//  screen swap to get wrong while an animation is mid-flight.
// ---------------------------------------------------------------------------
static void build_pair_overlay(lv_obj_t *scr) {
  g_pair_panel = lv_obj_create(scr);
  lv_obj_remove_style_all(g_pair_panel);
  lv_obj_set_size(g_pair_panel, SCREEN_W, SCREEN_H);
  lv_obj_set_pos(g_pair_panel, 0, 0);
  lv_obj_set_style_bg_color(g_pair_panel, COL_BG, 0);
  lv_obj_set_style_bg_opa(g_pair_panel, LV_OPA_COVER, 0);
  lv_obj_clear_flag(g_pair_panel, LV_OBJ_FLAG_SCROLLABLE);

  make_label(g_pair_panel, F_SM, COL_CYAN, 8, 6, "PEEK");
  make_label(g_pair_panel, F_SM, COL_TEXT_DIM, 44, 6, "// PAIRING");

  lv_obj_t *rule = lv_obj_create(g_pair_panel);
  lv_obj_remove_style_all(rule);
  lv_obj_set_size(rule, 224, 1);
  lv_obj_set_pos(rule, 8, 22);
  lv_obj_set_style_bg_color(rule, COL_CYAN, 0);
  lv_obj_set_style_bg_opa(rule, LV_OPA_30, 0);

  make_label(g_pair_panel, F_SM, COL_TEXT_DIM, 8, 32, "ENTER THIS CODE IN THE PEEKESP APP");

  char shown[16];
  pair_format(cfg.pair_code, shown, sizeof shown);
  g_lbl_code = lv_label_create(g_pair_panel);
  lv_label_set_text(g_lbl_code, shown);
  lv_obj_set_style_text_font(g_lbl_code, F_BIG, 0);
  lv_obj_set_style_text_color(g_lbl_code, COL_TEXT, 0);
  lv_obj_set_style_text_letter_space(g_lbl_code, 2, 0);
  lv_obj_align(g_lbl_code, LV_ALIGN_TOP_MID, 0, 54);

  g_lbl_pair_hint = make_label(g_pair_panel, F_SM, COL_TEXT_DIM, 8, 92,
                               "waiting for the app...");
  lv_obj_set_width(g_lbl_pair_hint, 224);
  lv_obj_set_style_text_align(g_lbl_pair_hint, LV_TEXT_ALIGN_CENTER, 0);

  make_label(g_pair_panel, F_SM, COL_TEXT_DIM, 8, 116,
             "hold left button to change settings");
}

static void pairing_done() {
  if (g_pair_panel && !lv_obj_has_flag(g_pair_panel, LV_OBJ_FLAG_HIDDEN)) {
    lv_obj_add_flag(g_pair_panel, LV_OBJ_FLAG_HIDDEN);
  }
}

// ---------------------------------------------------------------------------
//  Setup screen. The QR encodes a WIFI: join string, so scanning it with a
//  phone camera joins the access point directly - nobody has to read a
//  generated password off a 1.14" panel and retype it.
// ---------------------------------------------------------------------------
static void build_setup_ui() {
  lv_obj_t *scr = lv_scr_act();
  screen_base(scr);

  make_label(scr, F_SM, COL_CYAN, 8, 3, "PEEK");
  make_label(scr, F_SM, COL_AMBER, 44, 3, "// SETUP MODE");

  lv_obj_t *rule = lv_obj_create(scr);
  lv_obj_remove_style_all(rule);
  lv_obj_set_size(rule, 224, 1);
  lv_obj_set_pos(rule, 8, 19);
  lv_obj_set_style_bg_color(rule, COL_AMBER, 0);
  lv_obj_set_style_bg_opa(rule, LV_OPA_30, 0);

  // The QR needs a light quiet zone to scan reliably, so it keeps a white
  // background even on this dark theme - that is a scanning requirement,
  // not an aesthetic slip.
  lv_obj_t *qr = lv_qrcode_create(scr, 100, lv_color_black(), lv_color_white());
  lv_obj_set_pos(qr, 10, 26);
  lv_obj_set_style_border_color(qr, lv_color_white(), 0);
  lv_obj_set_style_border_width(qr, 3, 0);

  char join[96];
  const int n = snprintf(join, sizeof join, "WIFI:T:WPA;S:%s;P:%s;;", g_ap_ssid, g_ap_pass);
  lv_qrcode_update(qr, join, n);

  make_label(scr, F_SM, COL_TEXT_DIM, 122, 26, "SCAN TO JOIN");
  make_label(scr, F_SM, COL_TEXT,     122, 41, g_ap_ssid);
  make_label(scr, F_SM, COL_TEXT_DIM, 122, 58, "PASSWORD");
  make_label(scr, F_SM, COL_TEXT,     122, 73, g_ap_pass);
  make_label(scr, F_SM, COL_TEXT_DIM, 122, 92, "THEN OPEN");
  make_label(scr, F_SM, COL_CYAN,     122, 107, "192.168.4.1");
}

// ---------------------------------------------------------------------------
//  Core 1 -> pulls a snapshot of core 0's state. Runs inside lv_timer_handler,
//  so it is always on the UI thread and free to touch LVGL.
// ---------------------------------------------------------------------------
static void ui_sync_cb(lv_timer_t *t) {
  (void)t;
  static uint32_t last_seq   = 0;
  static NetState last_state = (NetState)0xFF;
  static uint32_t busy_until = 0;

  const NetState st = g_state;
  if (st != last_state) {
    apply_state(st);
    last_state = st;
  }

  // Keep the spinner up for a beat after a fast reply, otherwise a 30 ms
  // round trip would flash it for a single frame and read as a glitch.
  const uint32_t now = millis();
  if (g_busy) busy_until = now + 250;
  const bool show   = (int32_t)(now - busy_until) < 0;
  const bool hidden = lv_obj_has_flag(g_spinner, LV_OBJ_FLAG_HIDDEN);
  if (show == hidden) {
    if (show) lv_obj_clear_flag(g_spinner, LV_OBJ_FLAG_HIDDEN);
    else      lv_obj_add_flag(g_spinner, LV_OBJ_FLAG_HIDDEN);
  }

  const uint32_t seq = g_seq;
  if (seq == last_seq) return;

  Telemetry snap;
  if (xSemaphoreTake(g_lock, 0) != pdTRUE) return;   // never block the UI
  snap = g_telemetry;
  xSemaphoreGive(g_lock);
  last_seq = seq;

  pairing_done();          // first reading means the app found us
  gauge_set(&g_cpu, snap.cpu_percent);
  gauge_set(&g_ram, snap.ram_percent);
  bar_set(snap.storage_percent);

  char buf[48], a[12], b[12];

  if (snap.cpu_temp_c >= 0) snprintf(buf, sizeof buf, "%.0f\xC2\xB0", snap.cpu_temp_c);
  else                      snprintf(buf, sizeof buf, "--");
  lv_label_set_text(g_lbl_temp, buf);

  fmt_rate(a, sizeof a, snap.rx_kbps);
  fmt_rate(b, sizeof b, snap.tx_kbps);
  snprintf(buf, sizeof buf, LV_SYMBOL_DOWN " %s  " LV_SYMBOL_UP " %s", a, b);
  lv_label_set_text(g_lbl_net, buf);

  fmt_uptime(buf, sizeof buf, snap.uptime_seconds);
  lv_label_set_text(g_lbl_foot, buf);

  snprintf(buf, sizeof buf, "// %s", snap.host);
  lv_label_set_text(g_lbl_host, buf);

  lv_label_set_text_fmt(g_lbl_ping, "%d ms", (int)g_latency);
}

// ---------------------------------------------------------------------------
//  Core 1 - LVGL, and only LVGL.
// ---------------------------------------------------------------------------
static void uiTask(void *arg) {
  (void)arg;
  uint32_t last_tick = millis();
  for (;;) {
    const uint32_t now = millis();
#if LV_TICK_CUSTOM == 0
    lv_tick_inc(now - last_tick);      // lv_conf.h keeps LV_TICK_CUSTOM at 0
#endif
    last_tick = now;
    lv_timer_handler();
    vTaskDelay(pdMS_TO_TICKS(5));      // yield to IDLE1 / the watchdog
  }
}

// ============================================================================
//  Core 0 - WiFi, NTP, WireGuard, HTTP. Never touches an LVGL object.
// ============================================================================
static bool wifi_connect(uint32_t timeout_ms) {
  if (WiFi.status() == WL_CONNECTED) return true;

  g_state = NET_WIFI;
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);              // modem sleep adds ~100 ms of jitter
  WiFi.setAutoReconnect(true);
  WiFi.begin(cfg.wifi_ssid, cfg.wifi_pass);

  const uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > timeout_ms) return false;
    vTaskDelay(pdMS_TO_TICKS(250));
  }
  Serial.printf("[net] wifi ok, %s rssi %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  return true;
}

static bool time_sync(uint32_t timeout_ms) {
  // WireGuard stamps each handshake with a monotonic timestamp for replay
  // protection. The ESP32 boots at epoch 0 with no battery-backed RTC, so
  // without a real clock first, the peer discards the handshake in silence.
  g_state = NET_TIME;
  configTime(0, 0, "pool.ntp.org", "time.google.com");

  const uint32_t start = millis();
  while (time(nullptr) < 1700000000) {          // ~Nov 2023, i.e. "plausible"
    if (millis() - start > timeout_ms) return false;
    vTaskDelay(pdMS_TO_TICKS(250));
  }
  return true;
}

static bool tunnel_up() {
  if (!cfg.wg_enabled) return true;
  if (cfg.wg_priv[0] == '\0' || cfg.wg_peer_pub[0] == '\0' || cfg.wg_host[0] == '\0') {
    Serial.println("[wg] tunnel enabled but keys/endpoint are blank - skipping");
    return false;
  }

  g_state = NET_TUNNEL;
  IPAddress local;
  if (!local.fromString(cfg.wg_local_ip)) {
    Serial.println("[wg] local IP is not a valid address");
    return false;
  }
  const bool ok = wg.begin(local, cfg.wg_priv, cfg.wg_host,
                           cfg.wg_peer_pub, cfg.wg_port);
  // begin() only builds the interface; the handshake happens asynchronously
  // and the library retries it on its own, so the first GET or two below can
  // legitimately time out before traffic starts flowing.
  Serial.printf("[wg] begin -> %s\n", ok ? "interface up" : "FAILED");
  return ok;
}

static bool parse_payload(const String &payload, Telemetry &out) {
  // ArduinoJson 7 sizes itself; 6 needs the capacity up front.
#if ARDUINOJSON_VERSION_MAJOR >= 7
  JsonDocument doc;
#else
  StaticJsonDocument<640> doc;
#endif
  const DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    Serial.printf("[json] %s\n", err.c_str());
    return false;
  }

  out.cpu_percent     = doc["cpu_percent"]     | 0.0f;
  out.ram_percent     = doc["ram_percent"]     | 0.0f;
  out.storage_percent = doc["storage_percent"] | 0.0f;
  out.cpu_temp_c      = doc["cpu_temp_c"]      | -1.0f;
  out.rx_kbps         = doc["net_rx_kbps"]     | 0.0f;
  out.tx_kbps         = doc["net_tx_kbps"]     | 0.0f;
  out.uptime_seconds  = doc["uptime_seconds"]  | 0u;
  out.age_s           = doc["age_s"]           | 0u;   // relay only
  strlcpy(out.host, doc["host"] | "dietpi", sizeof out.host);
  return true;
}

// --- DIRECT: plain HTTP straight at the host, over the tunnel or the LAN ----
static bool fetch_direct(Telemetry &out, uint32_t &latency_ms) {
  char url[160];
  snprintf(url, sizeof url, "http://%s:%u%s",
           cfg.host, (unsigned)cfg.port, cfg.path);

  WiFiClient client;
  HTTPClient http;
  http.setReuse(false);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);

  const uint32_t t0 = millis();
  if (!http.begin(client, url)) return false;

  const int code = http.GET();
  if (code != HTTP_CODE_OK) {
    Serial.printf("[http] GET %s -> %d\n", url, code);
    http.end();
    return false;
  }

  const String payload = http.getString();
  latency_ms = millis() - t0;
  http.end();
  return parse_payload(payload, out);
}

// --- RELAY: HTTPS to the Cloudflare Worker the host pushes into -------------
static bool fetch_relay(Telemetry &out, uint32_t &latency_ms) {
  if (cfg.relay_url[0] == '\0') {
    Serial.println("[relay] no URL configured");
    return false;
  }

  // Stack-allocating the TLS client means its ~40 KB of session state is
  // released the moment this function returns, rather than being held for the
  // 5 seconds until the next poll.
  WiFiClientSecure tls;
  if (cfg.tls_verify) tls.setCACert(RELAY_ROOT_CAS);
  else                tls.setInsecure();          // portal escape hatch
  tls.setTimeout(HTTP_TIMEOUT_MS / 1000);         // WiFiClient's unit is seconds
  tls.setHandshakeTimeout(HTTP_TIMEOUT_MS / 1000);

  HTTPClient http;
  http.setReuse(false);
  http.setConnectTimeout(HTTP_TIMEOUT_MS);
  http.setTimeout(HTTP_TIMEOUT_MS);

  const uint32_t t0 = millis();
  if (!http.begin(tls, cfg.relay_url)) {
    Serial.println("[relay] bad URL");
    return false;
  }
  http.addHeader("Authorization", String("Bearer ") + cfg.relay_token);
  // Cloudflare's edge rejects some generic client user agents outright with
  // error 1010, before the Worker ever runs - which surfaces as an opaque 403
  // that looks nothing like an auth problem. Identify ourselves properly.
  http.setUserAgent("PeekESP-device/1.0");

  const int code = http.GET();
  if (code != HTTP_CODE_OK) {
    // 401 means the token is wrong and will never fix itself; 503 means the
    // Worker is up but the host has not pushed anything yet. Both are worth
    // distinguishing from a generic timeout in the log.
    Serial.printf("[relay] GET -> %d%s\n", code,
                  code == 401 ? " (token rejected)" :
                  code == 503 ? " (worker has no data yet)" : "");
    http.end();
    return false;
  }

  const String payload = http.getString();
  latency_ms = millis() - t0;
  http.end();
  return parse_payload(payload, out);
}

static bool fetch(Telemetry &out, uint32_t &latency_ms) {
  return cfg.transport == TRANSPORT_DIRECT ? fetch_direct(out, latency_ms)
                                           : fetch_relay(out, latency_ms);
}

static void netTask(void *arg) {
  (void)arg;

  while (!wifi_connect(30000)) {
    g_state = NET_ERROR;
    vTaskDelay(pdMS_TO_TICKS(2000));
  }
  // Both transports want a real clock: WireGuard for its handshake timestamp,
  // TLS for certificate validity dates. A device stuck at epoch 0 fails either
  // way, and in both cases it fails silently.
  if (!time_sync(20000)) Serial.println("[net] NTP timed out - handshake/TLS may fail");

  // The relay talks to the public internet, so the tunnel stays down: bringing
  // it up would make it the default route and send the HTTPS request into it.
  if (cfg.transport == TRANSPORT_DIRECT) tunnel_up();

  uint8_t failures = 0;

  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      g_state = NET_ERROR;
      WiFi.reconnect();
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }

    Telemetry fresh;
    uint32_t  latency = 0;

    g_busy = true;
    const bool ok = fetch(fresh, latency);
    g_busy = false;

    if (ok) {
      if (xSemaphoreTake(g_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        g_telemetry = fresh;
        xSemaphoreGive(g_lock);
        g_latency = latency;
        g_seq++;                 // publish last: the UI polls on this
      }
      // A reachable relay holding old data is a different fault from an
      // unreachable one: the network is fine, the host stopped reporting.
      // Showing LINK OK over frozen numbers would be the worst outcome.
      g_state  = (cfg.transport != TRANSPORT_DIRECT && fresh.age_s > STALE_AFTER_S)
                   ? NET_STALE : NET_ONLINE;
      failures = 0;
    } else {
      g_state = NET_ERROR;
      if (failures < 255) failures++;
    }

    // If the whole stack has been wedged for a minute, a clean reboot beats
    // sitting there showing stale numbers. Rebooting also re-runs NTP, which
    // is what a rejected-handshake tunnel usually needs anyway.
    if (failures >= MAX_CONSECUTIVE_FAILURES) {
      Serial.println("[net] too many consecutive failures - restarting");
      vTaskDelay(pdMS_TO_TICKS(200));
      esp_restart();
    }

    // Sleep in slices so a button press can cut the wait short.
    const uint32_t wake = millis() + (uint32_t)cfg.poll_s * 1000u;
    while ((int32_t)(millis() - wake) < 0 && !g_force) {
      if (g_reboot_at) break;
      vTaskDelay(pdMS_TO_TICKS(50));
    }
    g_force = false;
  }
}

// ============================================================================
//  Setup portal - a captive access point serving the configuration form.
//
//  Everything typed here (WiFi passphrase, WireGuard private key) crosses a
//  local WPA2 link in plain HTTP. That is a deliberate trade: TLS would need
//  a certificate no phone would trust, and the alternative - typing a 44
//  character base64 key with two buttons - is not a real alternative. The AP
//  is up only while you are configuring, and its password is per-device.
// ============================================================================
static const char PAGE_CSS[] PROGMEM =
  "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
  "<title>PeekESP setup</title><style>"
  "*{box-sizing:border-box}"
  "body{margin:0;padding:20px;background:#05070E;color:#E6EDF7;"
  "font:15px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}"
  "main{max-width:520px;margin:0 auto}"
  "h1{font-size:20px;margin:0 0 2px;color:#00E5FF;letter-spacing:.06em}"
  "p.sub{margin:0 0 22px;color:#5C6B82;font-size:13px}"
  "fieldset{border:1px solid #1A2334;border-radius:8px;padding:14px 14px 4px;"
  "margin:0 0 16px;background:#0B1220}"
  "legend{color:#5C6B82;font-size:12px;letter-spacing:.12em;padding:0 6px}"
  "label{display:block;margin:0 0 12px}"
  "span{display:block;font-size:12px;color:#5C6B82;margin-bottom:4px}"
  "input[type=text],input[type=password],input[type=number],select{width:100%;"
  "padding:9px 10px;border:1px solid #1A2334;border-radius:6px;background:#05070E;"
  "color:#E6EDF7;font:inherit}"
  "input:focus,select:focus{outline:none;border-color:#00E5FF}"
  ".row{display:flex;gap:10px}.row label{flex:1}"
  "label.chk{display:flex;align-items:center;gap:8px;color:#E6EDF7;font-size:14px}"
  "label.chk span{margin:0;color:#E6EDF7;font-size:14px}"
  "button{width:100%;padding:12px;border:0;border-radius:6px;background:#00E5FF;"
  "color:#05070E;font:600 15px/1 inherit;letter-spacing:.04em;cursor:pointer}"
  "a.reset{display:block;text-align:center;margin-top:14px;color:#FF4D6D;font-size:13px}"
  "p.hint{margin:-4px 0 12px;font-size:12px;line-height:1.45;color:#5C6B82}"
  "</style>";

static String field(const char *label, const char *name, const char *value,
                    const char *type = "text") {
  String s = F("<label><span>");
  s += label;
  s += F("</span><input type=");
  s += type;
  s += F(" name=");
  s += name;
  s += F(" value=\"");
  s += value;                      // values here are our own stored config
  s += F("\"></label>");
  return s;
}

static void handle_root() {
  String p = FPSTR(PAGE_CSS);
  p += F("<main><h1>PeekESP</h1><p class=sub>");
  p += g_ap_ssid;
  p += F(" &middot; settings are saved to flash and survive a reflash of the"
         " sketch.</p><form method=POST action=/save>");

  p += F("<fieldset><legend>WIFI</legend>");
  p += F("<label><span>Network (SSID)</span><input type=text name=ssid list=nets value=\"");
  p += cfg.wifi_ssid;
  p += F("\"><datalist id=nets>");
  const int n = WiFi.scanComplete();
  for (int i = 0; i < n && i < 20; i++) {
    p += F("<option value=\"");
    p += WiFi.SSID(i);
    p += F("\">");
  }
  p += F("</datalist></label>");
  p += field("Password", "pass", cfg.wifi_pass, "password");
  p += F("</fieldset>");

  p += F("<fieldset><legend>HOW TO REACH THE HOST</legend>"
         "<label><span>Transport</span><select name=tport>"
         "<option value=0");
  if (cfg.transport == TRANSPORT_DIRECT) p += F(" selected");
  p += F(">Direct - WireGuard tunnel or LAN</option><option value=1");
  if (cfg.transport == TRANSPORT_RELAY) p += F(" selected");
  p += F(">Cloudflare relay - host pushes, no port forward</option></select></label>"
         "<p class=hint>Direct needs the host reachable from outside: a port "
         "forward, or a tunnel ending on it. Pick the relay if the host is "
         "behind CGNAT or on a network you do not administer.</p></fieldset>");

  p += F("<fieldset><legend>CLOUDFLARE RELAY</legend>");
  p += field("Worker URL", "rurl", cfg.relay_url);
  p += field("Read token", "rtok", cfg.relay_token);
  p += F("<label class=chk><input type=checkbox name=tlsv value=1 ");
  if (cfg.tls_verify) p += F("checked");
  p += F("><span>Verify TLS certificate</span></label>"
         "<p class=hint>Leave verification on. Turn it off only if Cloudflare "
         "rotates to a CA that is not pinned in ca_certs.h - it makes the "
         "connection interceptable.</p></fieldset>");

  p += F("<fieldset><legend>WIREGUARD TUNNEL</legend><label class=chk>"
         "<input type=checkbox name=wgen value=1 ");
  if (cfg.wg_enabled) p += F("checked");
  p += F("><span>Route telemetry through the tunnel</span></label>");
  p += field("This device's tunnel IP", "wgip", cfg.wg_local_ip);
  p += field("Private key (this device)", "wgpriv", cfg.wg_priv);
  p += field("Peer public key (DietPi)", "wgpub", cfg.wg_peer_pub);
  p += F("<div class=row>");
  p += field("Endpoint host", "wghost", cfg.wg_host);
  p += F("</div>");
  p += F("<label><span>Endpoint port</span><input type=number name=wgport value=");
  p += cfg.wg_port;
  p += F("></label></fieldset>");

  p += F("<fieldset><legend>TELEMETRY</legend>");
  p += field("Host (DietPi Tailscale IP)", "host", cfg.host);
  p += F("<div class=row><label><span>Port</span><input type=number name=port value=");
  p += cfg.port;
  p += F("></label><label><span>Poll seconds</span><input type=number name=poll min=1 max=600 value=");
  p += cfg.poll_s;
  p += F("></label></div>");
  p += field("Path", "path", cfg.path);
  p += F("</fieldset>");

  p += F("<button type=submit>SAVE &amp; REBOOT</button></form>"
         "<a class=reset href=/reset>erase all settings</a></main>");

  server.send(200, "text/html", p);
}

static void copy_arg(const char *name, char *dst, size_t n) {
  if (!server.hasArg(name)) return;
  strlcpy(dst, server.arg(name).c_str(), n);
}

static void handle_save() {
  copy_arg("ssid",   cfg.wifi_ssid,   sizeof cfg.wifi_ssid);
  copy_arg("pass",   cfg.wifi_pass,   sizeof cfg.wifi_pass);
  copy_arg("wgip",   cfg.wg_local_ip, sizeof cfg.wg_local_ip);
  copy_arg("wgpriv", cfg.wg_priv,     sizeof cfg.wg_priv);
  copy_arg("wgpub",  cfg.wg_peer_pub, sizeof cfg.wg_peer_pub);
  copy_arg("wghost", cfg.wg_host,     sizeof cfg.wg_host);
  copy_arg("host",   cfg.host,        sizeof cfg.host);
  copy_arg("path",   cfg.path,        sizeof cfg.path);
  copy_arg("rurl",   cfg.relay_url,   sizeof cfg.relay_url);
  copy_arg("rtok",   cfg.relay_token, sizeof cfg.relay_token);

  if (server.hasArg("tport")) cfg.transport = server.arg("tport").toInt() ? TRANSPORT_RELAY
                                                                         : TRANSPORT_DIRECT;
  cfg.tls_verify = server.hasArg("tlsv");
  cfg.wg_enabled = server.hasArg("wgen");     // an unchecked box sends nothing
  if (server.hasArg("wgport")) cfg.wg_port = server.arg("wgport").toInt();
  if (server.hasArg("port"))   cfg.port    = server.arg("port").toInt();
  if (server.hasArg("poll"))   cfg.poll_s  = server.arg("poll").toInt();
  if (cfg.poll_s < 1)   cfg.poll_s = 1;
  if (cfg.poll_s > 600) cfg.poll_s = 600;

  config_save();

  String p = FPSTR(PAGE_CSS);
  p += F("<main><h1>Saved</h1><p class=sub>Rebooting into the dashboard. This"
         " access point is about to disappear - that is expected.</p></main>");
  server.send(200, "text/html", p);

  // Restart from the task loop, not here: the response still has to drain.
  g_reboot_at = millis() + 1200;
}

static void handle_reset() {
  config_erase();
  String p = FPSTR(PAGE_CSS);
  p += F("<main><h1>Erased</h1><p class=sub>Back to factory defaults."
         " Rebooting into setup mode.</p></main>");
  server.send(200, "text/html", p);
  g_reboot_at = millis() + 1200;
}

static void setupTask(void *arg) {
  (void)arg;

  // AP_STA rather than plain AP so the scan that fills the SSID dropdown can
  // run without tearing the access point down under the phone that is on it.
  WiFi.mode(WIFI_AP_STA);
  WiFi.softAP(g_ap_ssid, g_ap_pass);
  vTaskDelay(pdMS_TO_TICKS(300));
  WiFi.scanNetworks(true);                 // async; the form reads whatever is ready

  dns.setErrorReplyCode(DNSReplyCode::NoError);
  dns.start(53, "*", WiFi.softAPIP());     // wildcard -> captive portal prompt

  server.on("/", handle_root);
  server.on("/save", HTTP_POST, handle_save);
  server.on("/reset", handle_reset);
  server.onNotFound(handle_root);          // any URL lands on the form
  server.begin();

  Serial.printf("[setup] AP %s / %s at %s\n",
                g_ap_ssid, g_ap_pass, WiFi.softAPIP().toString().c_str());

  for (;;) {
    dns.processNextRequest();
    server.handleClient();
    if (g_reboot_at && (int32_t)(millis() - g_reboot_at) >= 0) {
      server.stop();
      dns.stop();
      vTaskDelay(pdMS_TO_TICKS(100));
      esp_restart();
    }
    vTaskDelay(pdMS_TO_TICKS(5));
  }
}

// ============================================================================
//  Entry points
// ============================================================================
void setup() {
  Serial.begin(115200);
  delay(100);
  Serial.println("\n[peek] booting");

  pinMode(PIN_BTN_L, INPUT_PULLUP);
  pinMode(PIN_BTN_R, INPUT);      // GPIO35 is input-only; the board pulls it up

  g_lock = xSemaphoreCreateMutex();
  config_load();

  const uint64_t mac = ESP.getEfuseMac();
  snprintf(g_ap_ssid, sizeof g_ap_ssid, "PeekESP-%04X", (unsigned)(mac >> 32) & 0xFFFF);
  snprintf(g_ap_pass, sizeof g_ap_pass, "peek%06X",     (unsigned)(mac & 0xFFFFFF));

  // Three ways into setup mode: nothing configured yet, the left button held
  // at power-on, or a request left in NVS by a long-press during normal use.
  const bool held = (digitalRead(PIN_BTN_L) == LOW);
  g_setup_mode = !config_usable() || held || consume_setup_request();

  tft.init();
  tft.setRotation(1);             // landscape, 240x135
  tft.fillScreen(TFT_BLACK);

  // tft.init() drives the backlight pin directly; take it over for PWM and
  // hold it dark so the first frame is never a flash of uninitialised RAM.
  backlight_init();
  backlight_set(0);

  lv_init();
  lv_disp_draw_buf_init(&draw_buf, lv_buf, NULL, SCREEN_W * 40);

  static lv_disp_drv_t disp_drv;
  lv_disp_drv_init(&disp_drv);
  disp_drv.hor_res  = SCREEN_W;
  disp_drv.ver_res  = SCREEN_H;
  disp_drv.flush_cb = disp_flush;
  disp_drv.draw_buf = &draw_buf;
  lv_disp_drv_register(&disp_drv);

  if (g_setup_mode) {
    build_setup_ui();
  } else {
    // A paired device with no code yet has just been flashed: invent one,
    // keep it, and derive the URL and token from it. Nothing to type in.
    if (cfg.transport == TRANSPORT_PAIRED) {
      if (cfg.pair_code[0] == '\0') {
        pair_new_code(cfg.pair_code, sizeof cfg.pair_code);
        config_save();
        Serial.printf("[pair] new code %s\n", cfg.pair_code);
      }
      pair_apply(cfg);
    }

    build_dashboard_ui();
    // The overlay sits on top until the first reading arrives, so a freshly
    // flashed device shows its code rather than a dashboard full of zeroes.
    if (cfg.transport == TRANSPORT_PAIRED) build_pair_overlay(lv_scr_act());
    lv_timer_create(ui_sync_cb, 120, NULL);
  }

  // Paint frame 0 before the lights come up. lv_refr_now() rather than
  // lv_timer_handler() because no ticks have elapsed yet, so the refresh
  // timer would not consider itself due and the fade-in would reveal an
  // uninitialised panel.
  lv_refr_now(NULL);

  const uint8_t level = BL_LEVELS[cfg.bl_idx];
  for (int d = 0; d <= level; d += 5) {   // ~400 ms fade-in
    backlight_set(d);
    delay(8);
  }
  backlight_set(level);

  // Core 0: everything that can block. Core 1: everything that must not.
  // 16 KB rather than 10: an mbedTLS handshake for the relay transport is
  // stack-hungry, and overflowing it shows up as a boot loop rather than an
  // error message.
  xTaskCreatePinnedToCore(g_setup_mode ? setupTask : netTask,
                          "net", 16384, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(uiTask, "ui", 8192, NULL, 2, NULL, 1);
}

void loop() {
  // Buttons only. This runs on core 1 alongside uiTask, so it must not call
  // into LVGL - it sets flags and pokes the backlight, nothing more.
  static uint32_t left_down  = 0;
  static bool     left_fired = false;
  static uint32_t last_right = 0;

  const uint32_t now = millis();

  // --- left: tap to refresh, hold to drop into setup mode ---
  if (digitalRead(PIN_BTN_L) == LOW) {
    if (!left_down) {
      left_down  = now;
      left_fired = false;
    } else if (!left_fired && now - left_down > SETUP_HOLD_MS && !g_setup_mode) {
      left_fired = true;
      Serial.println("[peek] entering setup mode");
      request_setup_mode();
    }
  } else {
    if (left_down && !left_fired && now - left_down > 30) g_force = true;
    left_down = 0;
  }

  // --- right: cycle brightness, remembered across reboots ---
  if (digitalRead(PIN_BTN_R) == LOW && now - last_right > 250) {
    last_right = now;
    cfg.bl_idx = (cfg.bl_idx + 1) % BL_LEVEL_COUNT;
    backlight_set(BL_LEVELS[cfg.bl_idx]);
    config_save_backlight();
  }

  // A reboot requested from the dashboard side lands here; the setup portal
  // handles its own inside setupTask so it can shut the server down first.
  if (!g_setup_mode && g_reboot_at && (int32_t)(now - g_reboot_at) >= 0) {
    esp_restart();
  }

  vTaskDelay(pdMS_TO_TICKS(20));
}

/* ============================================================================
 *  A note on "connecting the ESP32 to Tailscale"
 * ----------------------------------------------------------------------------
 *  An ESP32 cannot join a tailnet directly. Tailscale is WireGuard plus a
 *  control plane - node registration, rotating keys, NAT traversal and DERP
 *  relays - and none of that has an embedded client. tailscaled is Go, the
 *  keys are issued and rotated by the coordination server, and the relay
 *  fallback is a second transport on top.
 *
 *  What this project does instead: the DietPi runs a plain WireGuard listener
 *  (wg0) next to its existing tailscale0 interface, and the ESP32 dials that.
 *  Because the address the ESP32 asks for (the DietPi's own 100.x.x.x) lives
 *  on that same machine, the kernel answers regardless of which interface the
 *  packet arrived on - no forwarding or NAT rules needed. Run
 *  dietpi-wireguard-setup.sh on the DietPi and it sets all of this up and
 *  prints the keys to paste into the setup portal.
 * ==========================================================================*/
