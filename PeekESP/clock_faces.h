// ============================================================================
//  clock_faces.h - the clock page, kept out of the main sketch so the face can
//  be redesigned without reading past a thousand lines of networking.
//
//  One face: time, seconds, weekday and full date.
//
//  There was a second, time-only face for reading across a room. It went
//  because it earned a whole page of the carousel to show strictly less than
//  the page beside it, and every extra page is another press between you and
//  the machine you actually wanted to look at.
//
//  Runs on Dhaka time (UTC+6, no daylight saving) from the NTP sync the
//  TLS handshake needed anyway. The board has no battery-backed RTC: the
//  ESP32's internal RTC keeps counting between syncs but drifts, and loses
//  everything on a power cut. Until the first sync lands it shows dashes
//  rather than 01:00 on the 1st of January 1970 - which is what an unsynced
//  ESP32 sincerely believes the time to be.
//
//  Included by PeekESP.ino after the palette, the fonts and make_label().
// ============================================================================

// Same test the NTP wait uses. Anything below this is the epoch, not a clock.
#define CLOCK_PLAUSIBLE_EPOCH 1700000000

static const char *CLK_WEEKDAY[] = {
  "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"
};
static const char *CLK_MONTH[] = {
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December"
};

static bool clock_now(struct tm *out) {
  const time_t now = time(nullptr);
  if (now < CLOCK_PLAUSIBLE_EPOCH) return false;
  localtime_r(&now, out);
  return true;
}

static void clock_header(lv_obj_t *panel, const char *title) {
  make_label(panel, F_SM, COL_CYAN, 8, 6, "PEEK");
  make_label(panel, F_SM, COL_TEXT_DIM, 44, 6, title);

  lv_obj_t *rule = lv_obj_create(panel);
  lv_obj_remove_style_all(rule);
  lv_obj_set_size(rule, 224, 1);
  lv_obj_set_pos(rule, 8, 22);
  lv_obj_set_style_bg_color(rule, COL_CYAN, 0);
  lv_obj_set_style_bg_opa(rule, LV_OPA_30, 0);
}

static lv_obj_t *clock_page(lv_obj_t *scr) {
  lv_obj_t *p = lv_obj_create(scr);
  lv_obj_remove_style_all(p);
  lv_obj_set_size(p, SCREEN_W, SCREEN_H);
  lv_obj_set_pos(p, 0, 0);
  lv_obj_set_style_bg_color(p, COL_BG, 0);
  lv_obj_set_style_bg_opa(p, LV_OPA_COVER, 0);
  lv_obj_clear_flag(p, LV_OBJ_FLAG_SCROLLABLE);
  lv_obj_add_flag(p, LV_OBJ_FLAG_HIDDEN);
  return p;
}


// ---------------------------------------------------------------------------
//  Face 1: dated
// ---------------------------------------------------------------------------
static lv_obj_t *g_clk_panel = nullptr;
static lv_obj_t *g_clk_time  = nullptr;
static lv_obj_t *g_clk_secs  = nullptr;
static lv_obj_t *g_clk_date  = nullptr;
static lv_obj_t *g_clk_sub   = nullptr;
static lv_obj_t *g_clk_bar   = nullptr;

static void build_clock_dated(lv_obj_t *scr) {
  g_clk_panel = clock_page(scr);
  clock_header(g_clk_panel, "// DHAKA");

  // Aligned rather than positioned: a 44 px face is wide enough that a
  // hardcoded x would need recomputing the moment the format changes, and
  // "13:05" is narrower than "23:45" in a proportional font anyway.
  g_clk_time = lv_label_create(g_clk_panel);
  lv_label_set_text(g_clk_time, "--:--");
  lv_obj_set_style_text_font(g_clk_time, F_HUGE, 0);
  lv_obj_set_style_text_color(g_clk_time, COL_TEXT, 0);
  lv_obj_align(g_clk_time, LV_ALIGN_CENTER, -14, -14);

  // Seconds sit beside the minutes rather than inside the big number: they
  // change sixty times as often, and at 44 px that is the only thing the eye
  // would follow.
  g_clk_secs = lv_label_create(g_clk_panel);
  lv_label_set_text(g_clk_secs, "--");
  lv_obj_set_style_text_font(g_clk_secs, F_BIG, 0);
  lv_obj_set_style_text_color(g_clk_secs, COL_CYAN, 0);
  lv_obj_align_to(g_clk_secs, g_clk_time, LV_ALIGN_OUT_RIGHT_BOTTOM, 6, -4);

  g_clk_date = lv_label_create(g_clk_panel);
  lv_label_set_text(g_clk_date, "waiting for time");
  lv_obj_set_style_text_font(g_clk_date, F_MD, 0);
  lv_obj_set_style_text_color(g_clk_date, COL_TEXT, 0);
  lv_obj_align(g_clk_date, LV_ALIGN_CENTER, 0, 26);

  g_clk_sub = lv_label_create(g_clk_panel);
  lv_label_set_text(g_clk_sub, "");
  lv_obj_set_style_text_font(g_clk_sub, F_SM, 0);
  lv_obj_set_style_text_color(g_clk_sub, COL_TEXT_DIM, 0);
  lv_obj_align(g_clk_sub, LV_ALIGN_CENTER, 0, 44);

  // A minute, drawn. The only thing on this page that moves continuously, and
  // the reason it does not look frozen between minute changes.
  g_clk_bar = lv_obj_create(g_clk_panel);
  lv_obj_remove_style_all(g_clk_bar);
  lv_obj_set_size(g_clk_bar, 0, 2);
  lv_obj_set_pos(g_clk_bar, 8, 128);
  lv_obj_set_style_radius(g_clk_bar, 1, 0);
  lv_obj_set_style_bg_opa(g_clk_bar, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(g_clk_bar, COL_CYAN, 0);
}

static void render_clock_dated() {
  struct tm t;
  if (!clock_now(&t)) {
    lv_label_set_text(g_clk_time, "--:--");
    lv_label_set_text(g_clk_secs, "--");
    lv_label_set_text(g_clk_date, "waiting for time");
    lv_label_set_text(g_clk_sub, "no NTP sync yet");
    lv_obj_set_width(g_clk_bar, 0);
    return;
  }

  lv_label_set_text_fmt(g_clk_time, "%02d:%02d", t.tm_hour, t.tm_min);
  lv_label_set_text_fmt(g_clk_secs, "%02d", t.tm_sec);
  lv_label_set_text(g_clk_date, CLK_WEEKDAY[t.tm_wday % 7]);
  lv_label_set_text_fmt(g_clk_sub, "%d %s %d",
                        t.tm_mday, CLK_MONTH[t.tm_mon % 12], t.tm_year + 1900);

  // 224 px of width for the 60 seconds of a minute.
  lv_obj_set_width(g_clk_bar, (224 * t.tm_sec) / 59);
}
