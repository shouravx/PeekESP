/**
 * lv_conf.h — minimal LVGL v8.3 config for the TTGO T-Display dashboard.
 *
 * Only overrides what matters for this project; everything else falls back
 * to LVGL's own defaults (see lv_conf_internal.h inside the lvgl library).
 * Picked up automatically because platformio.ini sets LV_CONF_INCLUDE_SIMPLE.
 */
#ifndef LV_CONF_H
#define LV_CONF_H

#include <stdint.h>

/*------------------
 * Color
 *-----------------*/
#define LV_COLOR_DEPTH     16
#define LV_COLOR_16_SWAP   0   /* we swap bytes in the flush_cb via TFT_eSPI's pushColors(..., true) instead */

/*------------------
 * Memory
 *-----------------*/
#define LV_MEM_CUSTOM      0
#define LV_MEM_SIZE        (64U * 1024U)   /* internal LVGL heap for objects/styles/anims */

/*------------------
 * Timing
 *-----------------*/
#define LV_TICK_CUSTOM           0   /* we call lv_tick_inc() manually from the UI task */
#define LV_DISP_DEF_REFR_PERIOD  20  /* ms, ~50 fps ceiling — plenty for 500ms sweeps */
#define LV_INDEV_DEF_READ_PERIOD 30

/*------------------
 * Features used by this dashboard
 *-----------------*/
#define LV_USE_ARC     1
#define LV_USE_BAR     1
#define LV_USE_LABEL   1
#define LV_USE_SPINNER 1   /* the in-flight request ring in the top-right corner */
#define LV_USE_ANIMIMG 0

/*------------------
 * Fonts
 *-----------------*/
#define LV_FONT_MONTSERRAT_12  1
#define LV_FONT_MONTSERRAT_14  1
#define LV_FONT_MONTSERRAT_20  1
#define LV_FONT_DEFAULT        &lv_font_montserrat_14

/*------------------
 * Debug / misc — keep it lean on a 4MB-flash part
 *-----------------*/
#define LV_USE_LOG          0
#define LV_USE_ASSERT_NULL  1
#define LV_USE_PERF_MONITOR 0
#define LV_USE_MEM_MONITOR  0

#endif /*LV_CONF_H*/
