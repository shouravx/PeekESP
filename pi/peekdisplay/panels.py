"""Panel adapters: the same frame, onto whatever hardware is wired up.

Every import of a display library is deferred into the factory that needs it.
A Pi driving one SSD1306 should not need luma.lcd, RPLCD and a Waveshare SDK
installed to start, and importing them at module scope would mean exactly that
- or an ImportError at startup naming a panel the user does not own.

Three kinds, because the hardware genuinely is three kinds:

    pixel     a framebuffer. Takes a PIL image.
    text      character cells. Takes a list of strings.
    marquee   too small for either. Takes one string and scrolls it.
"""

from __future__ import annotations

import time

KIND_PIXEL = "pixel"
KIND_TEXT = "text"
KIND_MARQUEE = "marquee"


class PanelError(RuntimeError):
    """The panel could not be opened. The message names the fix."""


def _need(module, package, panel):
    """Import or explain. A traceback for a missing wheel helps nobody."""
    try:
        return __import__(module, fromlist=["_"])
    except ImportError as err:
        raise PanelError(
            "the %s panel needs the '%s' package:\n"
            "    sudo pip3 install %s\n"
            "(%s)" % (panel, package, package, err)
        ) from err


class Panel:
    """What the render loop is allowed to assume about a display."""

    kind = KIND_PIXEL
    size = (0, 0)
    mono = False
    round = False
    # A floor on how often display() may be called. Zero for anything backed by
    # RAM; e-paper sets it because a full refresh takes seconds and the panel
    # has a finite number of them in it.
    min_interval = 0.0
    name = "panel"

    def display(self, payload):
        raise NotImplementedError

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------------------------------------------------------------------------
#  No hardware: renders to a file
# ---------------------------------------------------------------------------


class PngPanel(Panel):
    """Writes each frame to a PNG. How this is tested without a panel wired up.

    Also genuinely useful on a headless Pi: point it at somewhere a web server
    serves and the dashboard is a URL, which is a reasonable thing to want
    before committing to a screen.
    """

    name = "png"

    def __init__(self, path="peek.png", width=240, height=135, mono=False,
                 round=False, scale=1, **_):
        self.path = path
        self.size = (int(width), int(height))
        self.mono = bool(mono)
        self.round = bool(round)
        self.scale = max(1, int(scale))
        self.frames = 0

    def display(self, image):
        from PIL import Image

        out = image
        if self.scale > 1:
            out = out.resize(
                (out.width * self.scale, out.height * self.scale),
                Image.NEAREST,       # a pixel panel previewed as pixels
            )
        out.convert("RGB").save(self.path)
        self.frames += 1


# ---------------------------------------------------------------------------
#  luma-backed colour and mono panels
# ---------------------------------------------------------------------------
# luma.lcd, luma.oled and luma.led_matrix all expose the same device object and
# all take a PIL image, which is why one renderer covers an ILI9341, an
# SSD1306 and a round GC9A01 without knowing the difference.


def _spi(port=0, device=0, gpio_dc=25, gpio_rst=27, gpio_light=None,
         bus_speed_hz=32000000, panel="spi"):
    core = _need("luma.core.interface.serial", "luma.core", panel)
    kwargs = {
        "port": int(port),
        "device": int(device),
        "gpio_DC": int(gpio_dc),
        "gpio_RST": int(gpio_rst),
        "bus_speed_hz": int(bus_speed_hz),
    }
    if gpio_light is not None:
        kwargs["gpio_LIGHT"] = int(gpio_light)
    return core.spi(**kwargs)


def _i2c(port=1, address=0x3C, panel="i2c"):
    core = _need("luma.core.interface.serial", "luma.core", panel)
    return core.i2c(port=int(port), address=int(address))


class LumaPanel(Panel):
    """Any luma device. The renderer never learns which."""

    def __init__(self, device, width, height, mono=False, round=False,
                 name="luma"):
        self._device = device
        self.size = (int(width), int(height))
        self.mono = bool(mono)
        self.round = bool(round)
        self.name = name

    def display(self, image):
        # luma expects the image to match the device exactly; a mismatch is a
        # silently offset picture rather than an error.
        if image.size != self.size:
            image = image.resize(self.size)
        self._device.display(image.convert("RGB") if not self.mono
                             else image.convert("1"))

    def close(self):
        try:
            self._device.cleanup()
        except Exception:       # noqa: BLE001 - closing must not mask a real error
            pass


def _lcd(driver, width, height, round=False, rotate=0, **opts):
    lcd = _need("luma.lcd.device", "luma.lcd", driver)
    serial = _spi(panel=driver, **{
        k: opts[k] for k in
        ("port", "device", "gpio_dc", "gpio_rst", "gpio_light", "bus_speed_hz")
        if k in opts
    })
    cls = getattr(lcd, driver, None)
    if cls is None:
        raise PanelError(
            "this luma.lcd does not have a '%s' driver - upgrade it:\n"
            "    sudo pip3 install -U luma.lcd" % driver
        )
    kwargs = {"width": int(width), "height": int(height), "rotate": int(rotate)}
    if "active_low" in opts:
        kwargs["active_low"] = bool(opts["active_low"])
    device = cls(serial, **kwargs)
    return LumaPanel(device, width, height, round=round, name=driver)


def _oled(driver, width, height, rotate=0, **opts):
    oled = _need("luma.oled.device", "luma.oled", driver)
    if opts.get("interface", "i2c") == "spi":
        serial = _spi(panel=driver, **{
            k: opts[k] for k in
            ("port", "device", "gpio_dc", "gpio_rst", "bus_speed_hz")
            if k in opts
        })
    else:
        serial = _i2c(port=opts.get("port", 1),
                      address=opts.get("address", 0x3C), panel=driver)
    cls = getattr(oled, driver)
    device = cls(serial, width=int(width), height=int(height), rotate=int(rotate))
    return LumaPanel(device, width, height, mono=True, name=driver)


# ---------------------------------------------------------------------------
#  E-paper
# ---------------------------------------------------------------------------


class EPaperPanel(Panel):
    """A Waveshare e-paper panel, refreshed as rarely as it deserves.

    The constraint here is not the renderer, it is the panel. A full refresh
    takes one to three seconds of visible inversion, and the display has a
    finite number of them in it - a five-second poll would flash all day and
    wear the panel out for numbers nobody was watching that closely.

    So min_interval is minutes, not seconds, and a frame identical to the one
    already on the glass is not drawn at all. Nothing is lost: e-paper holds
    its image with the power off, which is what it is for.
    """

    kind = KIND_PIXEL
    mono = True

    def __init__(self, module="waveshare_epd.epd2in13_V3", rotate=0,
                 min_interval=180, **_):
        epd_module = _need(module, "waveshare-epaper (or the Waveshare "
                                   "e-Paper repo on your PYTHONPATH)", "epaper")
        self._epd = epd_module.EPD()
        self._epd.init()
        try:
            self._epd.Clear(0xFF)
        except TypeError:                      # some models take no argument
            self._epd.Clear()
        # Waveshare reports the panel's native portrait size. A 2.13" is
        # 122x250 upright and 250x122 on its side, and which one the caller
        # wants depends on how it is mounted - so rotation decides the size
        # rather than being applied to a frame already drawn the wrong shape.
        w, h = int(self._epd.width), int(self._epd.height)
        self.size = (h, w) if int(rotate) % 180 == 90 else (w, h)
        self.rotate = int(rotate)
        self.min_interval = float(min_interval)
        self.name = module.rsplit(".", 1)[-1]
        self._last = None

    def display(self, image):
        frame = image.convert("1")
        if self.rotate:
            frame = frame.rotate(-self.rotate, expand=True)
        raw = frame.tobytes()
        if raw == self._last:
            return                              # identical: do not spend a refresh
        self._last = raw
        self._epd.display(self._epd.getbuffer(frame))

    def close(self):
        try:
            self._epd.sleep()
        except Exception:       # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
#  Character LCD
# ---------------------------------------------------------------------------


class CharacterPanel(Panel):
    """HD44780 behind a PCF8574 I2C backpack - the common 16x2 module."""

    kind = KIND_TEXT
    name = "hd44780"

    def __init__(self, cols=16, rows=2, address=0x27, port=1,
                 expander="PCF8574", charmap="A00", backlight=True, **_):
        rplcd = _need("RPLCD.i2c", "RPLCD", "hd44780")
        self.cols, self.rows = int(cols), int(rows)
        self.size = (self.cols, self.rows)
        self._lcd = rplcd.CharLCD(
            i2c_expander=expander,
            address=int(address),
            port=int(port),
            cols=self.cols,
            rows=self.rows,
            charmap=charmap,
            backlight_enabled=bool(backlight),
        )
        self._shown = None

    def display(self, lines):
        lines = list(lines)[: self.rows]
        while len(lines) < self.rows:
            lines.append("")
        lines = [line[: self.cols].ljust(self.cols) for line in lines]
        if lines == self._shown:
            return
        # Written row by row without clearing. lcd.clear() blanks the panel for
        # a few milliseconds, and doing that on every update is a visible
        # flicker on a character LCD - the padding above is what removes the
        # previous value instead.
        for row, line in enumerate(lines):
            self._lcd.cursor_pos = (row, 0)
            self._lcd.write_string(line)
        self._shown = lines

    def close(self):
        try:
            self._lcd.clear()
            self._lcd.close(clear=True)
        except Exception:       # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
#  LED matrix
# ---------------------------------------------------------------------------


class MatrixPanel(Panel):
    """MAX7219 chain. Eight rows of dots, so the message scrolls."""

    kind = KIND_MARQUEE
    name = "max7219"

    def __init__(self, cascaded=4, block_orientation=-90, rotate=0,
                 port=0, device=1, contrast=64, scroll_delay=0.03, **_):
        led = _need("luma.led_matrix.device", "luma.led_matrix", "max7219")
        core = _need("luma.core.interface.serial", "luma.core", "max7219")
        self._legacy = _need("luma.core.legacy", "luma.core", "max7219")
        self._fonts = _need("luma.core.legacy.font", "luma.core", "max7219")

        serial = core.spi(port=int(port), device=int(device), gpio=core.noop())
        self._device = led.max7219(
            serial,
            cascaded=int(cascaded),
            block_orientation=int(block_orientation),
            rotate=int(rotate),
        )
        self._device.contrast(int(contrast))
        self.size = (8 * int(cascaded), 8)
        self.scroll_delay = float(scroll_delay)
        self.mono = True

    def display(self, message):
        # Blocking for the length of the scroll, on purpose. The alternative is
        # a thread whose message changes halfway through a pass, which reads as
        # a glitch rather than as an update.
        self._legacy.show_message(
            self._device,
            message,
            fill="white",
            font=self._fonts.proportional(self._fonts.CP437_FONT),
            scroll_delay=self.scroll_delay,
        )

    def close(self):
        try:
            self._device.cleanup()
        except Exception:       # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
#  Registry
# ---------------------------------------------------------------------------
# Sizes are the panels people actually buy. Every one can be overridden from
# the config, because the same driver ships behind several glass sizes.

REGISTRY = {
    "png":       lambda **o: PngPanel(**o),

    "ili9341":   lambda **o: _lcd("ili9341", o.pop("width", 320),
                                  o.pop("height", 240), **o),
    "st7789":    lambda **o: _lcd("st7789", o.pop("width", 240),
                                  o.pop("height", 240), **o),
    "st7735":    lambda **o: _lcd("st7735", o.pop("width", 160),
                                  o.pop("height", 128), **o),
    "gc9a01":    lambda **o: _lcd("gc9a01", o.pop("width", 240),
                                  o.pop("height", 240), round=True, **o),

    "ssd1306":   lambda **o: _oled("ssd1306", o.pop("width", 128),
                                   o.pop("height", 64), **o),
    "ssd1309":   lambda **o: _oled("ssd1309", o.pop("width", 128),
                                   o.pop("height", 64), **o),
    "sh1106":    lambda **o: _oled("sh1106", o.pop("width", 128),
                                   o.pop("height", 64), **o),

    "epaper":    lambda **o: EPaperPanel(**o),
    "hd44780":   lambda **o: CharacterPanel(**o),
    "max7219":   lambda **o: MatrixPanel(**o),
}

ALIASES = {
    "oled": "ssd1306",
    "lcd": "hd44780",
    "16x2": "hd44780",
    "round": "gc9a01",
    "matrix": "max7219",
    "eink": "epaper",
    "e-paper": "epaper",
    "tft": "ili9341",
    "none": "png",
    "file": "png",
}


def names():
    return sorted(REGISTRY)


def create(name, **options):
    key = ALIASES.get(str(name).lower(), str(name).lower())
    factory = REGISTRY.get(key)
    if factory is None:
        raise PanelError(
            "unknown panel '%s'. Known panels: %s"
            % (name, ", ".join(names()))
        )
    return factory(**options)
