# PeekESP on a Raspberry Pi

The same dashboard the ESP32 shows, on a screen attached to a Pi — colour TFT,
mono OLED, e-paper, a 16×2 character LCD, or an LED matrix.

Same pairing code, same relay, no port forward. A Pi can watch the machines
*and* be one of them: run [`dietpi/peek-agent.py`](../dietpi/) alongside this
and it appears in its own carousel.

```bash
curl -fsSL https://raw.githubusercontent.com/shouravx/PeekESP/main/pi/install.sh | sudo sh
```

It asks which panel is wired up, installs **only** that panel's driver, enables
the bus it needs, and starts a systemd service.

---

## Supported panels

| `PEEK_PANEL` | Hardware | Bus | Default size | Needs |
|---|---|---|---|---|
| `ili9341` | 2.4″ / 2.8″ colour TFT | SPI | 320×240 | `luma.lcd` |
| `st7789` | colour TFT | SPI | 240×240 | `luma.lcd` |
| `st7735` | 1.8″ colour TFT | SPI | 160×128 | `luma.lcd` |
| `gc9a01` | 1.28″ **round** TFT | SPI | 240×240 | `luma.lcd` |
| `ssd1306` | 0.96″ mono OLED | I²C or SPI | 128×64 | `luma.oled` |
| `sh1106` | 1.3″ mono OLED | I²C or SPI | 128×64 | `luma.oled` |
| `ssd1309` | 2.4″ mono OLED | I²C or SPI | 128×64 | `luma.oled` |
| `epaper` | Waveshare e-paper | SPI | from the panel | Waveshare SDK |
| `hd44780` | 16×2 character LCD | I²C backpack | 16×2 cells | `RPLCD` |
| `max7219` | 8×8 LED matrix chain | SPI | 32×8 | `luma.led_matrix` |
| `png` | none — writes a file | — | 240×135 | Pillow only |

Aliases: `oled`, `tft`, `round`, `16x2`, `lcd`, `matrix`, `eink`, `none`.

Any size can be overridden — the same driver ships behind several glass sizes:

```
PEEK_PANEL=ssd1306
PEEK_PANEL_WIDTH=128
PEEK_PANEL_HEIGHT=32
```

---

## Three kinds of display, three presentations

The hardware genuinely is three kinds, so pretending one layout fits produces a
grey smear on two of them.

**Framebuffer panels** — every colour TFT, the OLEDs, e-paper. These get the
dashboard. The layout is computed from the panel's dimensions rather than
hand-placed, so a size nobody has tested still comes out readable: above
240×240 the gauges are arcs, below that they are bars, and below 128×64 the
storage row and footer are dropped rather than crushed. A round panel insets
every row to the circle's actual chord at that height, so nothing is written
under the bezel.

**Character LCDs** — 16×2 is thirty-two cells. Line 1 holds the hostname and
which machine you are on; line 2 rotates through CPU, RAM, disk, temperature,
network, battery and uptime. Every line is padded to the full width, because a
character LCD clears nothing on its own — write "9%" over "100%" and you get
`9%0%` until something longer overwrites it.

**LED matrices** — eight rows of dots. Everything scrolls, once, in one pass.

---

## Wiring

Pin numbers are BCM. The defaults below are what the driver assumes; every one
is overridable with `PEEK_PANEL_GPIO_DC`, `PEEK_PANEL_GPIO_RST` and so on.

**SPI colour TFT** (ILI9341, ST7789, GC9A01)

| Panel | Pi BCM | Header pin |
|---|---|---|
| VCC | 3V3 | 1 |
| GND | GND | 6 |
| SCK / SCL | GPIO 11 | 23 |
| MOSI / SDA | GPIO 10 | 19 |
| CS | GPIO 8 (CE0) | 24 |
| DC | GPIO 25 | 22 |
| RST | GPIO 27 | 13 |
| LED / BL | 3V3 | 17 |

**I²C OLED** (SSD1306, SH1106)

| Panel | Pi BCM | Header pin |
|---|---|---|
| VCC | 3V3 | 1 |
| GND | GND | 6 |
| SDA | GPIO 2 | 3 |
| SCL | GPIO 3 | 5 |

Address is `0x3C` for most modules and `0x3D` for some. `i2cdetect -y 1` says
which; set `PEEK_PANEL_ADDRESS=0x3D` if needed.

**MAX7219 matrix**

| Panel | Pi BCM | Header pin |
|---|---|---|
| VCC | 5V | 2 |
| GND | GND | 6 |
| DIN | GPIO 10 | 19 |
| CS | GPIO 7 (CE1) | 26 |
| CLK | GPIO 11 | 23 |

If the modules are rotated or mirrored, `PEEK_PANEL_BLOCK_ORIENTATION=-90`
(or `0`, `90`) fixes it. Set `PEEK_PANEL_CASCADED` to how many are chained.

**16×2 character LCD, I²C backpack**

> **Check your backpack before wiring this to a Pi.** An HD44780 needs 5 V for
> a readable contrast, and most PCF8574 backpacks pull SDA and SCL up to
> whatever powers them. Pulling the Pi's 3.3 V I²C lines to 5 V is out of spec
> for the SoC. Use a backpack with 3.3 V-referenced pull-ups, a level shifter,
> or remove the backpack's pull-up resistors and rely on the Pi's own. Plenty
> of people wire it straight through and get away with it; it is still out of
> spec, and it is your Pi.

| Panel | Pi BCM | Header pin |
|---|---|---|
| VCC | 5V | 2 |
| GND | GND | 6 |
| SDA | GPIO 2 | 3 |
| SCL | GPIO 3 | 5 |

Address is usually `0x27` or `0x3F` — `PEEK_PANEL_ADDRESS=0x3F`.

**E-paper** — a Waveshare HAT seats on the header and needs no wiring. The
library is not on PyPI:

```bash
git clone --depth 1 https://github.com/waveshareteam/e-Paper
sudo cp -r e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd \
          /usr/lib/python3/dist-packages/
```

Then name the exact model — there is no autodetection, and the wrong module
drives the panel at the wrong resolution:

```
PEEK_PANEL=epaper
PEEK_PANEL_MODULE=waveshare_epd.epd2in13_V4
PEEK_PANEL_ROTATE=90
```

---

## Prove the wiring before you blame the software

```bash
peek-display --self-test
```

A panel with its rotation wrong, its colour order reversed, or one data line
loose still shows *something* when handed a dashboard — and a dashboard that is
subtly wrong looks like a bug in the software. The test pattern cannot be
mistaken: a one-pixel white frame on all four edges, magenta ticks in each
corner, red-green-blue-white swatches left to right, and the panel's size in the
middle.

- An edge missing → rotation or size is wrong.
- Swatches in the wrong order → the colour order is `BGR`, not `RGB`.
- Corner ticks clipped → the panel is cropping; check width and height.
- Nothing at all → wiring, bus not enabled, or the wrong address.

Then, with invented data and no pairing code:

```bash
peek-display --demo
```

And without any hardware at all — this is how the layouts were checked:

```bash
peek-display --demo --panel png --width 240 --height 240 --out frame.png
peek-display --demo --panel png --width 128 --height 64 --scale 4
```

---

## Configuration

`/etc/peekesp/display.conf`, root-readable only, because the pairing code is the
credential.

| Key | Default | |
|---|---|---|
| `PEEK_PAIR_CODE` | — | as shown on the device |
| `PEEK_RELAY_BASE` | the public Worker | your own deployment, if you have one |
| `PEEK_PANEL` | `png` | see the table above |
| `PEEK_INTERVAL` | `5` | seconds between polls |
| `PEEK_ROTATE_SECONDS` | `8` | how long each machine stays on screen |
| `PEEK_OFFLINE_AFTER_POLLS` | `5` | missed polls before a machine reads OFFLINE |
| `PEEK_PANEL_*` | — | passed straight to the driver |

**The poll interval is a shared budget.** Cloudflare's free tier is 100,000
requests a day. Each agent at 5 s costs 17,280, each ESP32 display costs 17,280,
and so does this. Two machines and two displays is already ~69k. `PEEK_INTERVAL=15`
divides this one by three.

---

## When something is offline

A machine that stops reporting keeps its last numbers on screen, dimmed and
captioned `OFFLINE`, with how long it has been silent — what it was doing when
it stopped is the most useful thing left to show.

The age counted is the relay's own timestamp **plus** the time since this Pi
last managed a successful fetch. Without that second term a Pi that lost its
network would show every machine as permanently fresh, which is exactly
backwards: at that moment it knows nothing at all.

---

## E-paper refreshes slowly on purpose

A full refresh takes one to three seconds of visible inversion, and the panel
has a finite number of them in it. Polling every five seconds would flash all
day and wear it out for numbers nobody was watching that closely.

So `epaper` sets a floor of three minutes between redraws, and a frame identical
to the one already on the glass is not drawn at all. Nothing is lost — e-paper
holds its image with the power off, which is the point of it.

```
PEEK_PANEL_MIN_INTERVAL=600      # ten minutes, for a battery build
```

---

## Service

```bash
sudo systemctl status peek-display
journalctl -u peek-display -f
sudo systemctl restart peek-display
```

It runs as root, unlike the telemetry agent — which drops to its own account,
because reading `/proc` needs nothing. This one needs `/dev/spidev`, `/dev/i2c`
and GPIO, and adding a user to the `spi`, `i2c` and `gpio` groups does not cover
the sysfs paths some of these libraries reach for. That is a real trade-off and
it seemed better to say so than to ship a hardened-looking unit that quietly
needs root anyway.

---

## Tests

```bash
python -m unittest discover -s tests -v
```

Pillow is the only requirement, and no display library is needed — the panel
adapters are exercised through the `png` panel, which is the same code path as
a real one right up to the point the bytes go out over SPI. Every geometry in
the table is rendered and checked, in every state: online, offline, empty, and
error.

**What the tests cannot cover is SPI and I²C themselves** — whether a GC9A01 is
on the right pins, whether an OLED is at `0x3C` or `0x3D`. That is what
`--self-test` is for, and it needs the panel in your hand.
