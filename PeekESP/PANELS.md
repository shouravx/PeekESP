# Building the firmware for a different panel

The default is the LilyGO TTGO T-Display. Six other panels are supported:

```bash
pio run -e ttgo-t-display        # 240x135  ST7789   (the default)
pio run -e ili9341-320x240       # 320x240  ILI9341  2.4" / 2.8"
pio run -e ili9341-240x320       # 240x320  ILI9341  same panel, upright
pio run -e st7789-240x240        # 240x240  ST7789   1.3" square
pio run -e st7789-240x320        # 240x320  ST7789   2.0"
pio run -e gc9a01-round          # 240 dia  GC9A01   1.28" circular
pio run -e st7735-160x128        # 160x128  ST7735   1.8"

pio run -e gc9a01-round -t upload
```

---

## One image per panel, and why there is no way round it

TFT_eSPI chooses its driver with `#define`. So do its pins, and so does its
geometry. There is no runtime selection to add and no universal binary to
build — a firmware image drives exactly one panel.

That is the whole reason this is a list of PlatformIO environments rather than
a setting on the web page. Each env supplies its own driver and pins; the
sketch learns the panel's *shape* from
[`panel_profiles.h`](panel_profiles.h), which is all the user interface needs.

**Adding a panel** is a block in `panel_profiles.h`, an env in
`platformio.ini`, and a row in `PANELS` in `tools/ci_compile.py`. It is not a
change to the dashboard.

---

## The layout is arithmetic now

Every position used to be a literal measured against a 240×135 screen —
`make_label(scr, F_SM, COL_CYAN, 136, 3, "")`, a rule 224 px wide at `y=19`, a
bar `226×8` at `(7, 108)`. Sixty-four of them. A fine way to lay out one
screen, and the reason a second size was a rewrite.

[`layout_metrics.h`](layout_metrics.h) turns each one into a fraction of the
panel. On the T-Display every expression evaluates to exactly the number it
replaced — which is not a claim but 38 `static_assert`s, so a change that would
move a single pixel on the board this project actually runs on stops the build.

The refactor was checked the other way too: the T-Display image is
**byte-identical**, 1,353,433 bytes before and after. Same generated code.

On any other panel this is a proportional scaling of a design drawn for a 16:9
strip. It will be laid out sensibly. It will not be *tuned*, and nobody should
pretend otherwise until one has been on a desk.

Round panels get real geometry rather than a guess: `lay_inset()` returns the
circle's chord at each row's own height, so nothing is drawn under the bezel.
A fixed inset is either wasteful at the middle or clipped at the ends.

The dashboard reaches that through `lay_row_x()`, which maps an x measured
against the full-width design onto the room a given row actually has — and on a
square panel returns it unchanged, exactly, so the T-Display image stays
byte-identical. Worth saying because the first version of this shipped
`lay_inset()` correct and *never called it*: the round build compiled cleanly
and would still have drawn its header behind the bezel. Compiling is not using.

---

## What CI proves, and what it does not

```bash
python tools/ci_compile.py --all
```

Compiles the sketch at every panel's geometry and fails on any warning in our
own files. This is worth having: the first run across all seven found that a
320-pixel-wide panel **overflowed DRAM by 1,944 bytes**, because the LVGL draw
buffer was `SCREEN_W * 40` and had only ever been built at width 240. The
failure is a linker message naming `dram0_0_seg` with no source line attached
to it. The buffer is a fixed pixel budget now, so a wider panel takes fewer
lines per flush and the same RAM.

**It does not verify the driver, the pins or the rotation.** `arduino-cli`
builds against the installed TFT_eSPI setup — the LilyGO one — so across all
seven builds only the *geometry* varies. Drivers and pins come from
`platformio.ini`, and the only thing that verifies those is a panel in your
hand.

**Only `ttgo-t-display` has ever run on hardware.** Everything else compiles.
That is the entire claim.

---

## The pins each env assumes

`ttgo-t-display` is the LilyGO wiring and is fixed by the board. The rest use a
common ESP32 DevKit arrangement — change them in `platformio.ini` if yours
differs, since a generic SPI module has no standard pinout.

| | T-Display | The others |
|---|---|---|
| MOSI | 19 | 23 |
| SCLK | 18 | 18 |
| CS | 5 | 15 (−1 on the 240×240 ST7789, which has no CS) |
| DC | 16 | 2 |
| RST | 23 | 4 |
| BL | 4 | 32 |

`ili9341-*` also sets `TFT_MISO=19`; the others do not read back.

---

## If it comes up wrong

`TFT_WIDTH` and `TFT_HEIGHT` in `platformio.ini` are the panel's **native**
orientation. `PANEL_ROTATION` in `panel_profiles.h` then turns it into the
geometry the layout expects. A 240×320 ILI9341 at rotation 1 is 320×240; the
same panel at rotation 0 is 240×320.

Getting that pair wrong still builds. It draws the dashboard sideways, or off
the edge of the glass.

| Symptom | Cause |
|---|---|
| Sideways, or running off the edge | `PANEL_ROTATION` disagrees with `PANEL_W`/`PANEL_H` |
| Colours inverted | add or remove `-D TFT_INVERSION_ON=1` |
| Red and blue swapped | `-D TFT_RGB_ORDER=TFT_BGR` |
| Shifted by a few pixels | the panel needs `-D CGRAM_OFFSET=1` |
| White screen, nothing drawn | pins — check `TFT_DC` and `TFT_RST` first |
| Backlight dark | `TFT_BL`, and `TFT_BACKLIGHT_ON` (`HIGH` or `LOW`) |

---

## Panels this firmware does not drive

**SSD1306 OLED, e-paper, 16×2 character LCD and MAX7219** are not supported by
the ESP32 firmware. They are not TFT_eSPI panels and they are not framebuffers
of the same kind — a 16×2 LCD has thirty-two character cells and no pixels at
all, and a MAX7219 chain is eight rows of dots. Each needs its own driver and,
more to the point, its own presentation: the dashboard cannot be shrunk onto
them, it has to be replaced.

**The Raspberry Pi host drives all four of them today**, along with every panel
above — see [`pi/README.md`](../pi/README.md). If you have one of these
displays and a spare Pi, that is the working path right now.
