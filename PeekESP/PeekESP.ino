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
 *  ----------------------------------------------------------------------
 *  ARDUINO IDE SETUP (do these once, or nothing will compile / display)
 *  ----------------------------------------------------------------------
 *  1. Boards Manager -> "esp32" by Espressif, install 2.0.17.
 *       Core 3.x is built on ESP-IDF 5, which removed the tcpip_adapter API
 *       that WireGuard-ESP32 still uses. Stay on 2.0.x.
 *     Board: "LilyGo T-Display" (or "ESP32 Dev Module" - both verified)
 *
 *  2. Library Manager: "TFT_eSPI", "lvgl" (8.3.x - NOT 9.x), "ArduinoJson".
 *     Library Manager offers lvgl 9.x first; pick 8.3.9. This sketch uses the
 *     v8 API and will not build against v9.
 *
 *  3. WireGuard-ESP32: Sketch -> Include Library -> Add .ZIP Library, using
 *       https://github.com/ciniml/WireGuard-ESP32-Arduino  (Code -> Download ZIP)
 *
 *  4. TFT_eSPI pin config. Edit
 *       <Arduino>/libraries/TFT_eSPI/User_Setup_Select.h
 *     comment out   #include <User_Setup.h>
 *     uncomment     #include <User_Setups/Setup25_TTGO_T_Display.h>
 *     That header already carries the MOSI=19 SCLK=18 CS=5 DC=16 RST=23 BL=4
 *     pinout and, importantly, the CGRAM offset the 135x240 panel needs.
 *
 *  5. LVGL config. Copy this repo's lv_conf.h to
 *       <Arduino>/libraries/lv_conf.h     (next to the lvgl folder, NOT inside it)
 *     It is already set for 16-bit colour, a 64 KB LVGL heap, manual ticks,
 *     and the Montserrat 12/14/20 fonts this sketch uses.
 *
 *  6. Copy secrets.example.h to secrets.h in this sketch folder and fill it
 *     in. secrets.h is gitignored so your WireGuard private key stays out of
 *     the repo. Without it, the defaults below are used.
 * ============================================================================
 */

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <time.h>
#include <math.h>
#include <esp_system.h>

#include <TFT_eSPI.h>
#include <lvgl.h>
#include <ArduinoJson.h>
#include <WireGuard-ESP32.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"

// ============================================================================
//  CONFIG
//  Anything defined in secrets.h wins; the fallbacks below keep the sketch
//  compiling out of the box.
// ============================================================================
#if __has_include("secrets.h")
  #include "secrets.h"
#endif

#ifndef WIFI_SSID
  #define WIFI_SSID              "YOUR_WIFI_SSID"
#endif
#ifndef WIFI_PASSWORD
  #define WIFI_PASSWORD          "YOUR_WIFI_PASSWORD"
#endif

// --- WireGuard tunnel into the DietPi ---------------------------------------
// Run dietpi-wireguard-setup.sh on the DietPi; it prints every value below.
#ifndef WG_LOCAL_IP
  #define WG_LOCAL_IP            "10.10.44.2"                  // the ESP32 inside the tunnel
#endif
#ifndef WG_PRIVATE_KEY
  #define WG_PRIVATE_KEY         "ESP32_PRIVATE_KEY_HERE"      // ttgo-dashboard.key
#endif
#ifndef WG_PEER_PUBLIC_KEY
  #define WG_PEER_PUBLIC_KEY     "DIETPI_WG_PUBLIC_KEY_HERE"   // dietpi_wg.pub
#endif
#ifndef WG_ENDPOINT_HOST
  #define WG_ENDPOINT_HOST       "your-home.ddns.net"          // DietPi public IP / DDNS
#endif
#ifndef WG_ENDPOINT_PORT
  #define WG_ENDPOINT_PORT       51820
#endif

// --- Telemetry endpoint, reached through the tunnel -------------------------
// The DietPi's own Tailscale address (`tailscale ip -4`). It answers because
// the tunnel terminates on the very box that owns that address - see the note
// on Tailscale at the bottom of this file.
#ifndef DIETPI_HOST
  #define DIETPI_HOST            "100.64.12.3"
#endif
#ifndef DIETPI_PORT
  #define DIETPI_PORT            8080
#endif
#ifndef DIETPI_PATH
  #define DIETPI_PATH            "/telemetry"
#endif

// --- Behaviour --------------------------------------------------------------
#define USE_WIREGUARD            1     // 0 = plain LAN HTTP, handy for bring-up
#define POLL_INTERVAL_MS         5000
#define ANIM_MS                  500   // the sweep duration the arcs/bar use
#define HTTP_TIMEOUT_MS          4000
#define MAX_CONSECUTIVE_FAILURES 12    // ~60 s of nothing -> reboot the stack

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

// The built-in Montserrat faces are optional in lv_conf.h. Fall back to the
// default face rather than failing to build if someone trims their config.
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
//  State shared between core 0 (network) and core 1 (UI)
// ============================================================================
struct Telemetry {
  float    cpu_percent     = 0;
  float    ram_percent     = 0;
  float    storage_percent = 0;
  float    cpu_temp_c      = -1;      // <0 = host did not report one
  float    rx_kbps         = 0;
  float    tx_kbps         = 0;
  uint32_t uptime_seconds  = 0;
  char     host[20]        = "dietpi";
};

enum NetState : uint8_t {
  NET_BOOT, NET_WIFI, NET_TIME, NET_TUNNEL, NET_ONLINE, NET_ERROR
};

// Gauge belongs with the widgets further down, but it has to be declared up
// here: the Arduino IDE generates prototypes for every function in the sketch
// and injects them immediately before the FIRST function definition. Any type
// used in a signature must therefore be declared above that point, or the
// generated prototypes fail to compile with "'Gauge' was not declared in this
// scope" pointing at a line that looks perfectly valid.
struct Gauge {
  lv_obj_t  *arc   = nullptr;
  lv_obj_t  *value = nullptr;
  lv_color_t base  = COL_CYAN;
  int32_t    shown = 0;   // current on-screen value, 0..1000 (tenths of a %)
};

static Telemetry         g_telemetry;              // guarded by g_lock
static SemaphoreHandle_t g_lock = nullptr;

static volatile NetState g_state    = NET_BOOT;
static volatile bool     g_busy     = false;       // an HTTP GET is in flight
static volatile uint32_t g_seq      = 0;           // bumped on every good parse
static volatile uint32_t g_latency  = 0;           // ms for the last GET
static volatile bool     g_force    = false;       // button-triggered refresh

static WireGuard wg;

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
static const int PIN_BTN_L   = 0;    // "BOOT" - force an immediate refresh
static const int PIN_BTN_R   = 35;   // cycle backlight brightness
static const int BL_CHANNEL  = 0;

static const uint8_t BL_LEVELS[] = { 255, 150, 70, 20 };
static uint8_t s_bl_idx = 0;

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
//  Widgets   (struct Gauge is declared up in the shared-state section — see
//             the note there about Arduino's generated prototypes)
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

static void build_ui() {
  lv_obj_t *scr = lv_scr_act();
  lv_obj_set_style_bg_color(scr, COL_BG, 0);
  lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
  lv_obj_set_style_pad_all(scr, 0, 0);
  lv_obj_set_style_border_width(scr, 0, 0);
  lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_set_scrollbar_mode(scr, LV_SCROLLBAR_MODE_OFF);

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
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

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
#if USE_WIREGUARD
  g_state = NET_TUNNEL;
  IPAddress local;
  if (!local.fromString(WG_LOCAL_IP)) {
    Serial.println("[wg] WG_LOCAL_IP is not a valid address");
    return false;
  }
  const bool ok = wg.begin(local, WG_PRIVATE_KEY, WG_ENDPOINT_HOST,
                           WG_PEER_PUBLIC_KEY, WG_ENDPOINT_PORT);
  // begin() only builds the interface; the handshake happens asynchronously
  // and the library retries it on its own, so the first GET or two below can
  // legitimately time out before traffic starts flowing.
  Serial.printf("[wg] begin -> %s\n", ok ? "interface up" : "FAILED");
  return ok;
#else
  return true;
#endif
}

static bool fetch(Telemetry &out, uint32_t &latency_ms) {
  char url[128];
  snprintf(url, sizeof url, "http://%s:%u%s",
           DIETPI_HOST, (unsigned)DIETPI_PORT, DIETPI_PATH);

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
  strlcpy(out.host, doc["host"] | "dietpi", sizeof out.host);
  return true;
}

static void netTask(void *arg) {
  (void)arg;

  while (!wifi_connect(30000)) {
    g_state = NET_ERROR;
    vTaskDelay(pdMS_TO_TICKS(2000));
  }
  if (!time_sync(20000)) Serial.println("[net] NTP timed out - handshake may be rejected");
  tunnel_up();

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
      g_state  = NET_ONLINE;
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
    const uint32_t wake = millis() + POLL_INTERVAL_MS;
    while ((int32_t)(millis() - wake) < 0 && !g_force) {
      vTaskDelay(pdMS_TO_TICKS(50));
    }
    g_force = false;
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

  build_ui();
  lv_timer_create(ui_sync_cb, 120, NULL);

  // Paint frame 0 before the lights come up. lv_refr_now() rather than
  // lv_timer_handler() because no ticks have elapsed yet, so the refresh
  // timer would not consider itself due and the fade-in would reveal an
  // uninitialised panel.
  lv_refr_now(NULL);

  for (int d = 0; d <= BL_LEVELS[0]; d += 5) {   // ~400 ms fade-in
    backlight_set(d);
    delay(8);
  }
  backlight_set(BL_LEVELS[0]);

  // Core 0: everything that can block. Core 1: everything that must not.
  xTaskCreatePinnedToCore(netTask, "net", 10240, NULL, 1, NULL, 0);
  xTaskCreatePinnedToCore(uiTask,  "ui",   8192, NULL, 2, NULL, 1);
}

void loop() {
  // Buttons only. This runs on core 1 alongside uiTask, so it must not call
  // into LVGL - it sets flags and pokes the backlight, nothing more.
  static uint32_t last_press = 0;
  const uint32_t now = millis();

  if (now - last_press > 220) {
    if (digitalRead(PIN_BTN_L) == LOW) {
      g_force = true;                       // refresh now instead of waiting
      last_press = now;
    } else if (digitalRead(PIN_BTN_R) == LOW) {
      s_bl_idx = (s_bl_idx + 1) % (sizeof(BL_LEVELS) / sizeof(BL_LEVELS[0]));
      backlight_set(BL_LEVELS[s_bl_idx]);
      last_press = now;
    }
  }
  vTaskDelay(pdMS_TO_TICKS(20));
}

/* ============================================================================
 *  A note on "connecting the ESP32 to Tailscale"
 * ----------------------------------------------------------------------------
 *  An ESP32 cannot join a tailnet directly. Tailscale is WireGuard plus a
 *  control plane - node registration, rotating keys, NAT traversal and DERP
 *  relays - and none of that has an embedded client.
 *
 *  What this project does instead: the DietPi runs a plain WireGuard listener
 *  (wg0) next to its existing tailscale0 interface, and the ESP32 dials that.
 *  Because the address the ESP32 asks for (the DietPi's own 100.x.x.x) lives
 *  on that same machine, the kernel answers regardless of which interface the
 *  packet arrived on - no forwarding or NAT rules needed. Run
 *  dietpi-wireguard-setup.sh on the DietPi and it sets all of this up and
 *  prints the keys to paste into secrets.h.
 * ==========================================================================*/
