/**
 * PlatformIO entry point.
 *
 * The firmware itself lives in PeekESP/PeekESP.ino so that the Arduino IDE
 * and PlatformIO builds can never drift apart — this file exists only to pull
 * that single source of truth into the PlatformIO build.
 *
 * The sketch is written as plain C++ (every function is defined before it is
 * used, and it includes Arduino.h itself), so it does not rely on the Arduino
 * IDE's automatic prototype generation and compiles unchanged here.
 *
 * platformio.ini sets `src_dir = .` and filters the build down to this file.
 * Everything else — pins, LVGL config, WireGuard settings — is supplied via
 * build_flags and PeekESP/secrets.h.
 */
#include "PeekESP/PeekESP.ino"
