// ============================================================================
//  clock_faces.h - the clock pages, kept out of the main sketch so a face can
//  be redesigned without reading past a thousand lines of networking.
//
//  Two faces, both swipeable:
//
//    DATED    time, seconds, weekday and full date. What you want on a desk.
//    VANILLA  the time and nothing else, as large as the panel allows. What
//             you want across a room, or at three in the morning.
//
//  Both run on Dhaka time (UTC+6, no daylight saving) from the NTP sync the
//  TLS handshake needed anyway. The board has no battery-backed RTC: the
//  ESP32's internal RTC keeps counting between syncs but drifts, and loses
//  everything on a power cut. Until the first sync lands, both faces show
//  dashes rather than 01:00 on the 1st of January 1970 - which is what an
//  unsynced ESP32 sincerely believes the time to be.
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

// A header shared by both faces, so they cannot drift apart visually.
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


// ---------------------------------------------------------------------------
//  Face 2: vanilla
//
//  Deliberately almost empty. No seconds, no date, no progress bar - the point
//  of this face is that there is nothing on it to read except the time, so it
//  stays legible from further away than anything else this device draws. The
//  header is the only concession, and it is dim.
// ---------------------------------------------------------------------------
static lv_obj_t *g_clkp_panel = nullptr;
static lv_obj_t *g_clkp_time  = nullptr;
static lv_obj_t *g_clkp_dot   = nullptr;

static void build_clock_vanilla(lv_obj_t *scr) {
  g_clkp_panel = clock_page(scr);
  clock_header(g_clkp_panel, "// TIME");

  g_clkp_time = lv_label_create(g_clkp_panel);
  lv_label_set_text(g_clkp_time, "--:--");
  lv_obj_set_style_text_font(g_clkp_time, F_HUGE, 0);
  lv_obj_set_style_text_color(g_clkp_time, COL_TEXT, 0);
  lv_obj_align(g_clkp_time, LV_ALIGN_CENTER, 0, 8);

  // One pixel of proof that the device has not frozen. A clock with no
  // seconds and no second hand is indistinguishable from a screenshot for up
  // to a minute at a time, which is unsettling on a device whose whole job is
  // being live - so a single dot breathes once a second and nothing else moves.
  g_clkp_dot = lv_obj_create(g_clkp_panel);
  lv_obj_remove_style_all(g_clkp_dot);
  lv_obj_set_size(g_clkp_dot, 6, 6);
  lv_obj_align(g_clkp_dot, LV_ALIGN_BOTTOM_MID, 0, -10);
  lv_obj_set_style_radius(g_clkp_dot, LV_RADIUS_CIRCLE, 0);
  lv_obj_set_style_bg_opa(g_clkp_dot, LV_OPA_COVER, 0);
  lv_obj_set_style_bg_color(g_clkp_dot, COL_CYAN, 0);
}

static void render_clock_vanilla() {
  struct tm t;
  if (!clock_now(&t)) {
    lv_label_set_text(g_clkp_time, "--:--");
    lv_obj_set_style_bg_opa(g_clkp_dot, LV_OPA_20, 0);
    return;
  }

  lv_label_set_text_fmt(g_clkp_time, "%02d:%02d", t.tm_hour, t.tm_min);
  // Even seconds bright, odd seconds dim. A blinking colon would be the
  // classic choice, but Montserrat is proportional - a colon and a space are
  // different widths, so swapping them makes the whole time shuffle sideways
  // twice a second.
  lv_obj_set_style_bg_opa(g_clkp_dot, (t.tm_sec & 1) ? LV_OPA_20 : LV_OPA_COVER, 0);
}
