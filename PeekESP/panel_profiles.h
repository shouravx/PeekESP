#pragma once
// ---------------------------------------------------------------------------
//  panel_profiles.h - which screen this build is for.
// ---------------------------------------------------------------------------
//
// Selected with a build flag, because TFT_eSPI decides its driver at compile
// time. That is not a preference and there is no way around it: the driver is
// chosen by #define, the pins are #defines, and the geometry is a #define. One
// firmware image can drive exactly one panel, so this produces one image per
// panel rather than pretending otherwise.
//
//     pio run -e ttgo-t-display
//     pio run -e ili9341-320x240
//     pio run -e gc9a01-round
//
// The panel's own TFT_eSPI flags live in platformio.ini beside each env, where
// the pins are. What lives here is everything the *user interface* needs to
// know, which is geometry and shape - the two things the layout cannot guess.
//
// Adding a panel means: a block here, an env in platformio.ini, and a line in
// the CI matrix. It does not mean touching the dashboard.
// ---------------------------------------------------------------------------

// Capability tiers. What changes is not the driver but what is worth drawing:
// an arc gauge needs room to be read, and below a certain size the same number
// is better as a bar.
#define PEEK_TIER_RICH   2   // arcs, animation, colour
#define PEEK_TIER_COMPACT 1  // bars, colour, little or no animation

// PANEL_ROTATION is handed to TFT_eSPI's setRotation(). It is part of the
// profile rather than a constant in setup() because TFT_WIDTH and TFT_HEIGHT
// are the panel's *native* orientation, and which rotation yields the geometry
// the layout expects depends on the panel. A 240x320 ILI9341 shown at rotation
// 1 is 320x240; the same panel at rotation 0 is 240x320. Getting this wrong
// does not fail to build - it draws the whole dashboard sideways, off the
// glass, which is exactly the kind of thing --self-test exists to make obvious.

#if defined(PEEK_PANEL_ILI9341_320X240)
  #define PANEL_NAME       "ILI9341 320x240"
  #define PANEL_W          320
  #define PANEL_H          240
  #define PANEL_ROUND      0
  #define PANEL_TIER       PEEK_TIER_RICH
  #define PANEL_ROTATION   1

#elif defined(PEEK_PANEL_ILI9341_240X320)
  #define PANEL_NAME       "ILI9341 240x320"
  #define PANEL_W          240
  #define PANEL_H          320
  #define PANEL_ROUND      0
  #define PANEL_TIER       PEEK_TIER_RICH
  #define PANEL_ROTATION   0

#elif defined(PEEK_PANEL_ST7789_240X240)
  #define PANEL_NAME       "ST7789 240x240"
  #define PANEL_W          240
  #define PANEL_H          240
  #define PANEL_ROUND      0
  #define PANEL_TIER       PEEK_TIER_RICH
  #define PANEL_ROTATION   0

#elif defined(PEEK_PANEL_ST7789_240X320)
  #define PANEL_NAME       "ST7789 240x320"
  #define PANEL_W          240
  #define PANEL_H          320
  #define PANEL_ROUND      0
  #define PANEL_TIER       PEEK_TIER_RICH
  #define PANEL_ROTATION   0

#elif defined(PEEK_PANEL_GC9A01_ROUND)
  // 1.28" circular. The framebuffer is square and the glass is not, so the
  // corners are written, paid for over SPI, and never seen. layout_metrics.h
  // insets every row to the circle's own chord at that height.
  #define PANEL_NAME       "GC9A01 240 round"
  #define PANEL_W          240
  #define PANEL_H          240
  #define PANEL_ROUND      1
  #define PANEL_TIER       PEEK_TIER_RICH
  #define PANEL_ROTATION   0

#elif defined(PEEK_PANEL_ST7735_160X128)
  #define PANEL_NAME       "ST7735 160x128"
  #define PANEL_W          160
  #define PANEL_H          128
  #define PANEL_ROUND      0
  #define PANEL_TIER       PEEK_TIER_COMPACT
  #define PANEL_ROTATION   1

#else
  // The LilyGO TTGO T-Display, and the default. This is the board the project
  // was built on and the only one any of this has run on.
  #ifndef PEEK_PANEL_TTGO_T_DISPLAY
    #define PEEK_PANEL_TTGO_T_DISPLAY 1
  #endif
  #define PANEL_NAME       "TTGO T-Display 240x135"
  #define PANEL_W          240
  #define PANEL_H          135
  #define PANEL_ROUND      0
  #define PANEL_TIER       PEEK_TIER_COMPACT
  #define PANEL_ROTATION   1
#endif

// A round panel that is not square is a panel profile someone got wrong, and
// the chord arithmetic in layout_metrics.h would quietly produce nonsense
// rather than fail.
#if PANEL_ROUND && (PANEL_W != PANEL_H)
  #error "a round panel must be square - check PANEL_W and PANEL_H"
#endif

// LVGL's draw buffer is sized from the width, and the sketch keeps it in
// internal RAM. Catching an implausible profile here is better than a heap
// allocation failing at boot with no message.
#if PANEL_W < 80 || PANEL_W > 480 || PANEL_H < 64 || PANEL_H > 480
  #error "panel geometry is outside what this firmware is built for"
#endif
