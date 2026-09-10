#pragma once
// ---------------------------------------------------------------------------
//  layout_metrics.h - where things go, as arithmetic instead of constants.
// ---------------------------------------------------------------------------
//
// Every position in the dashboard used to be a literal tuned by eye for a
// 240x135 T-Display: make_label(scr, F_SM, COL_CYAN, 136, 3, ""), a rule
// 224 px wide at y=19, a bar 226x8 at (7, 108). Sixty-four of them. That is a
// perfectly good way to lay out one screen and the reason a second screen size
// was a rewrite.
//
// So each one is now a fraction of the panel, anchored on the design that
// exists. The reference is the T-Display, and every expression below evaluates
// to exactly the number it replaced at 240x135 - which is not a claim, it is
// the static_asserts at the bottom of this file. If a change here would move a
// single pixel on the board this project was built on, the build stops.
//
// On any other panel this is a proportional scaling of a design drawn for a
// 16:9 strip. That is a real limitation and worth stating plainly: it will be
// laid out sensibly and it will not be tuned. Nothing here has run on a panel
// other than the T-Display.
// ---------------------------------------------------------------------------

#include "panel_profiles.h"

// The panel the literals were measured on. Only ever used as a denominator.
#define REF_W 240
#define REF_H 135

// Rounded rather than truncated. Halving a 66 px gauge with integer division
// gives 33 and loses a pixel on the way back up, and a column of controls each
// losing a different pixel is how a layout goes subtly crooked.
#define SCALE_X(v)  (((PANEL_W) * (v) + (REF_W) / 2) / (REF_W))
#define SCALE_Y(v)  (((PANEL_H) * (v) + (REF_H) / 2) / (REF_H))

// Anything square - gauges, the status dot - scales by whichever axis has less
// room, so a 240x320 portrait panel does not get a gauge taller than it is
// wide.
#define SCALE_MIN(v) (SCALE_X(v) < SCALE_Y(v) ? SCALE_X(v) : SCALE_Y(v))

// ---- the bands ------------------------------------------------------------
//  header   name, page, ping, activity dot
//  ---- rule
//  content  two gauges and the temperature/throughput panel
//  storage  caption, percentage, bar
//  footer   host message, connection state

#define LAY_PAD           SCALE_X(8)
#define LAY_HEAD_Y        SCALE_Y(3)
#define LAY_RULE_Y        SCALE_Y(19)
#define LAY_RULE_W        (PANEL_W - LAY_PAD * 2)
#define LAY_CONTENT_Y     SCALE_Y(23)

#define LAY_GAUGE_D       SCALE_MIN(66)
#define LAY_GAUGE_ARC_W   (LAY_GAUGE_D / 11)          // 6 at 66
#define LAY_GAUGE_L_X     SCALE_X(6)
#define LAY_GAUGE_R_X     SCALE_X(78)
#define LAY_GAUGE_CAP_DY  SCALE_MIN(14)
#define LAY_GAUGE_VAL_DY  (-SCALE_MIN(5))

#define LAY_STAT_X        SCALE_X(150)
#define LAY_STAT_W        SCALE_X(84)
#define LAY_STAT_H        LAY_GAUGE_D
#define LAY_STAT_PAD      SCALE_X(4)
#define LAY_STAT_TEMP_Y   SCALE_Y(13)
#define LAY_STAT_NET_Y    SCALE_Y(40)

#define LAY_STORE_Y       SCALE_Y(92)
#define LAY_STORE_W       SCALE_X(150)
#define LAY_PCT_X         SCALE_X(162)
#define LAY_PCT_W         SCALE_X(70)

#define LAY_BAR_X         SCALE_X(7)
#define LAY_BAR_Y         SCALE_Y(108)
#define LAY_BAR_W         (PANEL_W - LAY_BAR_X * 2)
#define LAY_BAR_H         SCALE_Y(8)

#define LAY_FOOT_Y        SCALE_Y(118)
#define LAY_STATE_X       SCALE_X(124)
#define LAY_STATE_W       SCALE_X(108)

// ---- header fields --------------------------------------------------------
#define LAY_HOST_X        SCALE_X(44)
#define LAY_HOST_W        SCALE_X(92)
#define LAY_WHICH_X       SCALE_X(136)
#define LAY_WHICH_W       SCALE_X(26)
#define LAY_PING_X        SCALE_X(162)
#define LAY_PING_W        SCALE_X(44)

#define LAY_SPIN_X        SCALE_X(217)
#define LAY_SPIN_D        SCALE_MIN(20)
#define LAY_DOT_X         SCALE_X(222)
#define LAY_DOT_Y         SCALE_Y(5)
#define LAY_DOT_D         SCALE_MIN(10)

// ---------------------------------------------------------------------------
//  Round panels
// ---------------------------------------------------------------------------
// A GC9A01 has a square framebuffer behind a circular window. Anything in the
// corners is drawn, clocked out over SPI, and never seen - and a label that
// starts at x = 8 on a 240 px circle starts behind the bezel.
//
// Runtime rather than constexpr: this needs a square root, and a compile-time
// one in C++11 is a recursive template that buys nothing here. It is called a
// handful of times while the UI is built, not per frame.

#if PANEL_ROUND
static inline int16_t lay_chord_half(int16_t dy) {
  const float r = (float)PANEL_W / 2.0f - 1.0f;
  const float d = (float)(dy < 0 ? -dy : dy);
  if (d >= r) return 0;
  return (int16_t)sqrtf(r * r - d * d);
}

// Leftmost drawable x for a row whose vertical centre is at y.
static inline int16_t lay_inset(int16_t y, int16_t height) {
  const int16_t mid  = (int16_t)(y + height / 2 - PANEL_H / 2);
  const int16_t half = lay_chord_half(mid);
  const int16_t x    = (int16_t)(PANEL_W / 2 - half + 2);
  return x < 0 ? 0 : x;
}
static inline int16_t lay_width_at(int16_t y, int16_t height) {
  return (int16_t)(PANEL_W - lay_inset(y, height) * 2);
}
#else
// Square panels: the row is as wide as the panel, less the padding. Written as
// functions with the same signatures so the dashboard has one spelling.
static inline int16_t lay_inset(int16_t, int16_t) { return LAY_PAD; }
static inline int16_t lay_width_at(int16_t, int16_t) {
  return (int16_t)(PANEL_W - LAY_PAD * 2);
}
#endif

// Map an x measured against the full-width design onto the room a given row
// actually has. This is what the dashboard calls; lay_inset() on its own was
// defined, correct, and never invoked, which meant a round build compiled
// perfectly and still drew its header behind the bezel.
//
// On a square panel it returns x unchanged - not approximately, exactly - so
// the T-Display keeps producing the same image and the static_asserts below
// stay meaningful.
static inline int16_t lay_row_x(int16_t x, int16_t row_y, int16_t row_h) {
#if PANEL_ROUND
  const int16_t inset = lay_inset(row_y, row_h);
  const int16_t span  = (int16_t)(PANEL_W - inset * 2);
  const int16_t base  = (int16_t)(PANEL_W - LAY_PAD * 2);
  if (base <= 0 || span <= 0) return x;
  // Proportional rather than a shift: shifting keeps the design's width and
  // pushes the right-hand elements off the far side of the circle instead of
  // the near one.
  return (int16_t)(inset + (int32_t)(x - LAY_PAD) * span / base);
#else
  (void)row_y;
  (void)row_h;
  return x;
#endif
}

// The same mapping for a width.
static inline int16_t lay_row_w(int16_t w, int16_t row_y, int16_t row_h) {
#if PANEL_ROUND
  const int16_t base = (int16_t)(PANEL_W - LAY_PAD * 2);
  if (base <= 0) return w;
  return (int16_t)((int32_t)w * lay_width_at(row_y, row_h) / base);
#else
  (void)row_y;
  (void)row_h;
  return w;
#endif
}

// The nominal height of a row of small text, for the chord lookups above. The
// exact figure matters little - a pixel or two changes the chord by a fraction
// of a pixel - but a row is not zero-height, and treating it as such would
// inset by the chord at the row's very top edge.
#define LAY_ROW_H  SCALE_MIN(14)

// ---------------------------------------------------------------------------
//  The T-Display must not move
// ---------------------------------------------------------------------------
// This is the whole safety argument for the refactor. These are the literals
// that were in build_dashboard_ui() before any of this existed; if the
// arithmetic above ever stops reproducing them, the board this was actually
// developed and tested on has silently changed, and the build fails instead.

#if PANEL_W == REF_W && PANEL_H == REF_H
static_assert(LAY_PAD        ==   8, "T-Display padding moved");
static_assert(LAY_HEAD_Y     ==   3, "T-Display header row moved");
static_assert(LAY_RULE_Y     ==  19, "T-Display rule moved");
static_assert(LAY_RULE_W     == 224, "T-Display rule changed width");
static_assert(LAY_CONTENT_Y  ==  23, "T-Display content band moved");

static_assert(LAY_GAUGE_D    ==  66, "T-Display gauge changed size");
static_assert(LAY_GAUGE_ARC_W ==  6, "T-Display gauge stroke changed");
static_assert(LAY_GAUGE_L_X  ==   6, "T-Display CPU gauge moved");
static_assert(LAY_GAUGE_R_X  ==  78, "T-Display RAM gauge moved");
static_assert(LAY_GAUGE_CAP_DY == 14, "T-Display gauge caption moved");
static_assert(LAY_GAUGE_VAL_DY == -5, "T-Display gauge value moved");

static_assert(LAY_STAT_X     == 150, "T-Display stats panel moved");
static_assert(LAY_STAT_W     ==  84, "T-Display stats panel changed width");
static_assert(LAY_STAT_H     ==  66, "T-Display stats panel changed height");
static_assert(LAY_STAT_PAD   ==   4, "T-Display stats padding changed");
static_assert(LAY_STAT_TEMP_Y ==  13, "T-Display temperature moved");
static_assert(LAY_STAT_NET_Y ==  40, "T-Display throughput moved");

static_assert(LAY_STORE_Y    ==  92, "T-Display storage caption moved");
static_assert(LAY_STORE_W    == 150, "T-Display storage caption changed width");
static_assert(LAY_PCT_X      == 162, "T-Display percentage moved");
static_assert(LAY_PCT_W      ==  70, "T-Display percentage changed width");

static_assert(LAY_BAR_X      ==   7, "T-Display bar moved");
static_assert(LAY_BAR_Y      == 108, "T-Display bar moved");
static_assert(LAY_BAR_W      == 226, "T-Display bar changed width");
static_assert(LAY_BAR_H      ==   8, "T-Display bar changed height");

static_assert(LAY_FOOT_Y     == 118, "T-Display footer moved");
static_assert(LAY_STATE_X    == 124, "T-Display state label moved");
static_assert(LAY_STATE_W    == 108, "T-Display state label changed width");

static_assert(LAY_HOST_X     ==  44, "T-Display hostname moved");
static_assert(LAY_HOST_W     ==  92, "T-Display hostname changed width");
static_assert(LAY_WHICH_X    == 136, "T-Display page marker moved");
static_assert(LAY_WHICH_W    ==  26, "T-Display page marker changed width");
static_assert(LAY_PING_X     == 162, "T-Display ping moved");
static_assert(LAY_PING_W     ==  44, "T-Display ping changed width");

static_assert(LAY_SPIN_X     == 217, "T-Display spinner moved");
static_assert(LAY_SPIN_D     ==  20, "T-Display spinner changed size");
static_assert(LAY_DOT_X      == 222, "T-Display status dot moved");
static_assert(LAY_DOT_Y      ==   5, "T-Display status dot moved");
static_assert(LAY_DOT_D      ==  10, "T-Display status dot changed size");
#endif
