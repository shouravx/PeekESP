# Prebuilt firmware

One image per display. Each is the compiled sketch ready to write at offset
`0x0`, and flashing needs **no ESP32 core, no libraries, no Arduino IDE and no
PlatformIO** — only esptool:

```bash
python ../tools/flash.py --panel gc9a01-round
python ../tools/flash.py                      # the T-Display, the default
```

| Directory | Display | |
|---|---|---|
| `ttgo-t-display/` | LilyGO TTGO T-Display | 240×135, ST7789 |
| `ili9341-320x240/` | ILI9341, landscape | 320×240 |
| `ili9341-240x320/` | ILI9341, upright | 240×320 |
| `st7789-240x240/` | ST7789 1.3″ square | 240×240 |
| `st7789-240x320/` | ST7789 2.0″ | 240×320 |
| `gc9a01-round/` | GC9A01 1.28″ round | 240 diameter |
| `st7735-160x128/` | ST7735 1.8″ | 160×128 |

`PeekESP-merged.bin` at the top level is the T-Display image, kept where it has
always been so existing instructions and links do not break.

## These are not interchangeable

TFT_eSPI selects its driver, its pins and its geometry with `#define`, so a
firmware image drives exactly one display. The T-Display image on a GC9A01
drives an ST7789 on the wrong pins and shows nothing at all — which looks
identical to bad wiring, a dead backlight, or a broken panel.

So each build says what it is, in the first three lines of the serial log:

```
[peek] booting
[peek] firmware 1.2.0
[peek] panel    GC9A01 240 round  (240x240)
```

and again on the settings page, which is reachable over the network even when
the screen is showing nothing.

## What is in one

| Offset | |
|---|---|
| `0x01000` | bootloader |
| `0x08000` | partition table |
| `0x0e000` | `boot_app0` — which OTA slot to start |
| `0x10000` | the sketch |

Merged rather than shipped as four files so there is one thing to download and
one offset to get right.

Partition scheme **Huge APP (3MB No OTA/1MB SPIFFS)** for every panel. Flashing
onto a board with a different layout will not boot.

## Rebuilding

**These do not update themselves.** After changing `PeekESP/PeekESP.ino`:

```bash
python tools/build_panels.py                 # every panel
python tools/build_panels.py gc9a01-round    # just one
```

That needs PlatformIO (`pip install platformio`), because the driver and pins
live in `platformio.ini`'s `build_flags` and nothing else reads them.

`tools/export_firmware.py` still rebuilds the top-level T-Display image through
arduino-cli, which is the path that needs no PlatformIO.

## Verified how far

**Only the T-Display has run on hardware.** Every other image compiles, links,
and carries the driver and the pins written down in `platformio.ini` — and
nothing more than that can be claimed until one is on a desk. If a panel comes
up sideways, inverted, or with red and blue swapped,
[PeekESP/PANELS.md](../PeekESP/PANELS.md) says which flag each symptom points at.
