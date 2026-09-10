"""Draw a dashboard for any pixel panel, from 128x32 mono to 320x240 colour.

One adaptive layout rather than one per panel. The alternative - a hand-tuned
layout per display - is what the ESP32 firmware does, and it is why adding a
second screen size there means moving two hundred hardcoded coordinates. Here
every position is a fraction of the panel, so a size nobody has tested still
produces something readable rather than something clipped.

What does vary by panel is what is worth drawing at all: an arc gauge on a
128x64 mono OLED is a grey smudge, so below a threshold the same value is a
bar. Those decisions live in Layout.for_panel().
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
#  Colour
# ---------------------------------------------------------------------------
# Chosen to match the ESP32's palette, so two displays under one pairing code
# do not look like two different products.


@dataclass
class Palette:
    bg: tuple = (10, 14, 20)
    text: tuple = (230, 237, 243)
    dim: tuple = (125, 133, 144)
    accent: tuple = (34, 211, 238)
    ok: tuple = (63, 185, 80)
    warn: tuple = (210, 153, 34)
    crit: tuple = (248, 81, 73)
    track: tuple = (33, 42, 54)

    @classmethod
    def mono(cls):
        """One bit per pixel: everything on, or everything off.

        A mono panel given greys dithers them, and a dithered gauge track on a
        128x64 OLED reads as dirt on the glass. So the track is off and
        everything else is on, and severity is carried by the number rather
        than by a colour that cannot exist here.
        """
        on = (255, 255, 255)
        off = (0, 0, 0)
        return cls(bg=off, text=on, dim=on, accent=on,
                   ok=on, warn=on, crit=on, track=off)


def severity(palette, percent):
    """Green, amber, red. The thresholds the firmware uses."""
    if percent is None:
        return palette.dim
    if percent >= 90:
        return palette.crit
    if percent >= 75:
        return palette.warn
    return palette.ok


# ---------------------------------------------------------------------------
#  Fonts
# ---------------------------------------------------------------------------
# DejaVu ships with Raspberry Pi OS and with Pillow itself, so this normally
# finds a real scalable font. The bitmap fallback is ugly at large sizes but
# keeps the program running on a minimal image rather than refusing to start
# over a font.

_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "DejaVuSans.ttf",
)
_FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
)

_font_cache = {}


def font(size, bold=False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    for path in (_FONT_BOLD_CANDIDATES if bold else _FONT_CANDIDATES):
        try:
            loaded = ImageFont.truetype(path, size)
            break
        except (OSError, IOError):
            continue
    else:
        loaded = ImageFont.load_default()
    _font_cache[key] = loaded
    return loaded


def text_size(draw, text, fnt):
    """(w, h). textbbox is Pillow 8+; the fallback keeps older ones working."""
    try:
        left, top, right, bottom = draw.textbbox((0, 0), text, font=fnt)
        return right - left, bottom - top
    except AttributeError:                       # pragma: no cover - old Pillow
        return draw.textsize(text, font=fnt)


def fit_text(draw, text, fnt, max_w, ellipsis="…"):
    """Trim to width. A clipped hostname is worse than a shortened one."""
    if text_size(draw, text, fnt)[0] <= max_w:
        return text
    trimmed = text
    while trimmed and text_size(draw, trimmed + ellipsis, fnt)[0] > max_w:
        trimmed = trimmed[:-1]
    return (trimmed + ellipsis) if trimmed else ""


# ---------------------------------------------------------------------------
#  Formatting
# ---------------------------------------------------------------------------


def fmt_capacity(gb):
    if gb is None:
        return "--"
    if gb >= 1024:
        return "%.1fT" % (gb / 1024.0)
    return "%.0fG" % gb


def fmt_rate(kbps):
    if kbps is None:
        return "--"
    if kbps >= 1000:
        return "%.1fM" % (kbps / 1000.0)
    return "%.0fk" % kbps


def fmt_ago(seconds):
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh" % (seconds // 3600)
    return "%dd" % (seconds // 86400)


def fmt_uptime(seconds):
    if seconds is None:
        return "--"
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    if days:
        return "%dd %dh" % (days, hours)
    minutes = rem // 60
    if hours:
        return "%dh %dm" % (hours, minutes)
    return "%dm" % minutes


# ---------------------------------------------------------------------------
#  Layout
# ---------------------------------------------------------------------------


@dataclass
class Layout:
    """How much detail this panel can carry, and at what scale."""

    width: int
    height: int
    palette: Palette
    round: bool = False
    arcs: bool = False          # radial gauges, or bars
    show_footer: bool = True    # temperature and throughput
    show_storage: bool = True
    pad: int = 4
    f_tiny: int = 8
    f_small: int = 10
    f_body: int = 12
    f_big: int = 22

    @classmethod
    def for_panel(cls, width, height, mono=False, round_=False):
        palette = Palette.mono() if mono else Palette()
        area = width * height

        if round_:
            # A circular panel wastes its corners, so everything is pulled in
            # and the arcs earn their place - they are the one gauge shape that
            # actually suits a circle.
            return cls(width, height, palette, round=True, arcs=True,
                       show_footer=height >= 200,
                       pad=max(4, width // 12),
                       f_tiny=max(8, width // 26), f_small=max(9, width // 22),
                       f_body=max(11, width // 18), f_big=max(18, width // 8))

        if height <= 40:
            # A 128x32 strip. A 10px header would take a third of the glass, so
            # everything shrinks to one size and the storage row and footer are
            # dropped rather than crushed - two legible rows beat four illegible
            # ones.
            return cls(width, height, palette, arcs=False,
                       show_footer=False, show_storage=False,
                       pad=1, f_tiny=8, f_small=8, f_body=8, f_big=12)

        if area <= 128 * 64:
            # 128x64 and smaller: three bars and a header is all that fits at a
            # size anyone can read across a desk.
            return cls(width, height, palette, arcs=False,
                       show_footer=height >= 64, show_storage=height >= 48,
                       pad=2, f_tiny=8, f_small=8, f_body=10, f_big=14)

        if height <= 150:
            # The T-Display shape: wide and short. Bars, because an arc big
            # enough to read would eat the whole height.
            return cls(width, height, palette, arcs=False,
                       pad=4, f_tiny=9, f_small=10, f_body=12, f_big=20)

        # 240x240, 320x240 and up.
        scale = min(width, height) / 240.0
        return cls(width, height, palette, arcs=True,
                   pad=int(6 * scale) or 4,
                   f_tiny=max(9, int(11 * scale)),
                   f_small=max(10, int(13 * scale)),
                   f_body=max(12, int(16 * scale)),
                   f_big=max(20, int(34 * scale)))


# ---------------------------------------------------------------------------
#  Drawing
# ---------------------------------------------------------------------------


def _bar(draw, box, percent, palette, radius=2):
    x0, y0, x1, y1 = box
    draw.rectangle(box, fill=palette.track)
    if percent is not None and percent > 0:
        filled = x0 + int((x1 - x0) * min(100.0, percent) / 100.0)
        if filled > x0:
            draw.rectangle((x0, y0, filled, y1), fill=severity(palette, percent))
    # An outline so an empty bar is still visibly a bar rather than nothing.
    draw.rectangle(box, outline=palette.dim if palette.track != palette.bg
                   else palette.text)


def _arc(draw, centre, radius, percent, palette, width, label, value_font,
         label_font, caption):
    cx, cy = centre
    box = (cx - radius, cy - radius, cx + radius, cy + radius)
    # 135 degrees to 405: an open bottom, so the gap reads as a gauge rather
    # than as a ring someone failed to close.
    start, extent = 135, 270
    draw.arc(box, start, start + extent, fill=palette.track, width=width)
    if percent is not None and percent > 0:
        sweep = extent * min(100.0, percent) / 100.0
        draw.arc(box, start, start + sweep,
                 fill=severity(palette, percent), width=width)

    shown = "--" if percent is None else "%d" % round(percent)
    w, h = text_size(draw, shown, value_font)
    draw.text((cx - w / 2, cy - h / 2 - 2), shown, font=value_font, fill=palette.text)
    w, h = text_size(draw, caption, label_font)
    draw.text((cx - w / 2, cy + radius - h - 2), caption,
              font=label_font, fill=palette.dim)


def render(layout, machines, index, snapshot, offline_after_s=150,
           message=None):
    """Draw one frame. Returns a PIL image sized to the panel.

    ``message`` replaces the dashboard entirely - used for pairing, for "no
    machines yet" and for a fatal error, all of which are more useful as a
    sentence than as a dashboard full of dashes.
    """
    p = layout.palette
    image = Image.new("RGB", (layout.width, layout.height), p.bg)
    draw = ImageDraw.Draw(image)

    if message is not None:
        _draw_message(draw, layout, message)
        return image

    if not machines:
        _draw_message(draw, layout, ("No machines", "are reporting yet"))
        return image

    machine = machines[index % len(machines)]
    age = snapshot.age_of(machine)
    online = age < offline_after_s

    pad = layout.pad
    f_tiny, f_small = font(layout.f_tiny), font(layout.f_small)
    f_body, f_big = font(layout.f_body, True), font(layout.f_big, True)

    # ---- header ----------------------------------------------------------
    head_y = pad
    left = pad
    right = layout.width - pad
    if layout.round:
        # The corners of the framebuffer are behind the bezel, so a header
        # placed at the rectangle's edge is written and never seen. Inset to
        # the circle's actual chord at the header's own height rather than to
        # a guessed fraction of the width - a fixed inset is either wasteful
        # at the middle or clipped at the top, depending on the guess.
        head_y = int(layout.height * 0.16)
        radius = min(layout.width, layout.height) / 2.0 - 1
        half = chord_half_width(radius, radius - head_y - layout.f_body / 2.0)
        left = int(layout.width / 2.0 - half) + 2
        right = int(layout.width / 2.0 + half) - 2

    # Which of several, and how fresh. Measured first, because the hostname is
    # the part that can be shortened: at 128 px "kushtia-server" and "1/2
    # OFFLINE" both want the full width, and giving the name a fixed fraction
    # let them overlap.
    marks = []
    if len(machines) > 1:
        marks.append("%d/%d" % (index % len(machines) + 1, len(machines)))
    marks.append(fmt_ago(age) if online else "OFFLINE")
    mark = "  ".join(marks)
    mark_w, _ = text_size(draw, mark, f_small)
    draw.text((right - mark_w, head_y + 1), mark, font=f_small,
              fill=p.text if online else p.crit)

    gap = max(4, pad)
    name = fit_text(draw, machine.host, f_body, right - left - mark_w - gap)
    draw.text((left, head_y), name, font=f_body, fill=p.text)

    top = head_y + layout.f_body + pad
    if not layout.round:
        draw.line((pad, top - pad // 2, layout.width - pad, top - pad // 2),
                  fill=p.track if p.track != p.bg else p.dim)

    # A machine that has stopped reporting keeps its last numbers on screen,
    # dimmed and captioned, rather than being blanked. What it was doing when
    # it stopped is the most useful thing left to show.
    if not online:
        _draw_offline(draw, layout, machine, age, top)
        return image

    bottom = layout.height - pad
    if layout.show_footer:
        bottom -= layout.f_small + pad

    if layout.arcs:
        _draw_arcs(draw, layout, machine, top, bottom)
    else:
        _draw_bars(draw, layout, machine, top, bottom)

    if layout.show_footer:
        _draw_footer(draw, layout, machine, bottom + pad // 2)

    return image


def _draw_arcs(draw, layout, machine, top, bottom):
    p = layout.palette
    f_small, f_tiny = font(layout.f_small), font(layout.f_tiny)
    f_val = font(layout.f_big, True)

    band = bottom - top
    bar_h = max(6, layout.height // 26)
    caption_h = text_size(draw, "0G free", f_small)[1]

    # The storage row is reserved out of the band before the arcs are sized,
    # rather than drawn wherever the arcs happen to end. Placing it at
    # cy + radius put it past `bottom` on a round panel and straight through
    # the footer, because nothing had told the arcs the footer existed.
    storage_h = (bar_h + caption_h + max(4, layout.pad)) if layout.show_storage else 0
    arc_band = band - storage_h

    radius = min(int(arc_band * 0.45), layout.width // 5)
    stroke = max(3, radius // 5)
    cy = top + arc_band // 2

    _arc(draw, (layout.width // 4, cy), radius, machine.cpu_percent, p,
         stroke, "CPU", f_val, f_tiny, "CPU")
    _arc(draw, (layout.width * 3 // 4, cy), radius, machine.ram_percent, p,
         stroke, "RAM", f_val, f_tiny, "RAM")

    if layout.show_storage:
        y = top + arc_band + max(2, layout.pad // 2)
        if layout.round:
            # Inset to the circle's chord at the bar's own height, so the bar
            # ends where the glass does rather than under the bezel.
            radius_panel = min(layout.width, layout.height) / 2.0 - 1
            half = chord_half_width(radius_panel,
                                    abs(y + bar_h / 2.0 - layout.height / 2.0))
            inset = int(layout.width / 2.0 - half) + 3
        else:
            inset = layout.pad * 2
        _bar(draw, (inset, y, layout.width - inset, y + bar_h),
             machine.storage_percent, p)
        free = "%s free" % fmt_capacity(machine.storage_free_gb)
        w, _ = text_size(draw, free, f_small)
        draw.text(((layout.width - w) // 2, y + bar_h + 2), free,
                  font=f_small, fill=p.dim)


def _draw_bars(draw, layout, machine, top, bottom):
    p = layout.palette
    f_small, f_tiny = font(layout.f_small), font(layout.f_tiny)

    rows = [("CPU", machine.cpu_percent), ("RAM", machine.ram_percent)]
    if layout.show_storage:
        rows.append(("DSK", machine.storage_percent))

    pad = layout.pad
    label_w = max(text_size(draw, r[0], f_tiny)[0] for r in rows) + 4
    value_w = text_size(draw, "100%", f_small)[0] + 2

    # The row pitch is driven by the tallest thing in the row, which is the
    # text and not the bar. Sizing it from the bar alone is what made the
    # percentages on a 128x64 OLED land on top of the row beneath them: an 8 px
    # font in a 6 px row overflows by two pixels every time.
    text_h = max(text_size(draw, "100%", f_small)[1],
                 text_size(draw, "CPU", f_tiny)[1])

    # A caption under the storage bar, if there is room for a whole extra row.
    caption = None
    if layout.show_storage and machine.storage_free_gb is not None:
        caption = "%s free of %s" % (fmt_capacity(machine.storage_free_gb),
                                     fmt_capacity(machine.storage_total_gb))

    band = bottom - top
    slots = len(rows) + (1 if caption else 0)
    pitch = band // slots
    if pitch < text_h + 2:
        # Not enough height for the caption as its own row - drop it rather
        # than let it collide with the footer.
        caption = None
        slots = len(rows)
        pitch = band // slots

    bar_h = max(4, min(layout.height // 10, pitch - text_h + text_h // 2))
    bar_h = min(bar_h, max(4, pitch - 2))

    y = top
    for label, value in rows:
        row_mid = y + pitch // 2
        draw.text((pad, row_mid - text_h // 2), label, font=f_tiny, fill=p.dim)
        _bar(draw, (pad + label_w, row_mid - bar_h // 2,
                    layout.width - pad - value_w, row_mid + bar_h - bar_h // 2),
             value, p)
        shown = "--" if value is None else "%d%%" % round(value)
        w, _ = text_size(draw, shown, f_small)
        draw.text((layout.width - pad - w, row_mid - text_h // 2), shown,
                  font=f_small, fill=p.text)
        y += pitch

    if caption:
        shown = fit_text(draw, caption, f_tiny, layout.width - pad * 2)
        w, _ = text_size(draw, shown, f_tiny)
        draw.text(((layout.width - w) // 2, y + max(0, (pitch - text_h) // 2)),
                  shown, font=f_tiny, fill=p.dim)


def _draw_footer(draw, layout, machine, y):
    p = layout.palette
    f_small = font(layout.f_small)

    bits = []
    if machine.cpu_temp_c is not None:
        bits.append("%.0f°C" % machine.cpu_temp_c)

    # ASCII, not arrows. The obvious glyphs here are U+2193/U+2191, and they
    # render as empty boxes in any font that lacks them - which included the
    # font this was first tested against. On a 1-bit OLED a missing glyph is a
    # solid rectangle, so the failure is not subtle. RX/TX costs four
    # characters and cannot go wrong.
    if layout.width >= 200:
        bits.append("RX %s  TX %s" % (fmt_rate(machine.rx_kbps),
                                      fmt_rate(machine.tx_kbps)))
    else:
        bits.append("%s/%s" % (fmt_rate(machine.rx_kbps),
                               fmt_rate(machine.tx_kbps)))
    if machine.has_battery:
        bits.append("%d%%%s" % (machine.battery_percent,
                                "+" if machine.battery_charging else ""))
    elif machine.uptime_seconds is not None and layout.width >= 200:
        # No battery to report, so the space goes to uptime instead of sitting
        # empty. Dropped on a narrow panel, where it would crowd the throughput.
        bits.append("up %s" % fmt_uptime(machine.uptime_seconds))

    line = "   ".join(bits)
    if layout.round:
        radius = min(layout.width, layout.height) / 2.0 - 1
        half = chord_half_width(
            radius, abs(y + layout.f_small / 2.0 - layout.height / 2.0)
        )
        inset = int(layout.width / 2.0 - half) + 3
    else:
        inset = layout.pad
    line = fit_text(draw, line, f_small, layout.width - inset * 2)
    w, _ = text_size(draw, line, f_small)
    draw.text(((layout.width - w) // 2, y), line, font=f_small, fill=p.dim)


def _draw_offline(draw, layout, machine, age, top):
    p = layout.palette
    f_big, f_small = font(layout.f_big, True), font(layout.f_small)
    f_tiny = font(layout.f_tiny)

    band = layout.height - top - layout.pad
    label = "OFFLINE"
    w, h = text_size(draw, label, f_big)
    draw.text(((layout.width - w) // 2, top + band // 2 - h),
              label, font=f_big, fill=p.crit)

    since = "silent for %s" % fmt_ago(age)
    w, _ = text_size(draw, since, f_small)
    draw.text(((layout.width - w) // 2, top + band // 2 + 4), since,
              font=f_small, fill=p.dim)

    if layout.height >= 100 and machine.cpu_percent is not None:
        last = "last: %d%% cpu  %d%% ram" % (round(machine.cpu_percent),
                                             round(machine.ram_percent or 0))
        w, _ = text_size(draw, last, f_tiny)
        draw.text(((layout.width - w) // 2, top + band - layout.f_tiny - 2),
                  last, font=f_tiny, fill=p.dim)


def _draw_message(draw, layout, lines):
    p = layout.palette
    if isinstance(lines, str):
        lines = [lines]
    lines = list(lines)

    # The first line is the headline; the rest are explanation. On a very small
    # panel only the headline fits, and a truncated explanation is worse than
    # none.
    head_font = font(layout.f_body, True)
    body_font = font(layout.f_small)
    if layout.height < 48:
        lines = lines[:1]

    heights = []
    for i, line in enumerate(lines):
        fnt = head_font if i == 0 else body_font
        heights.append(text_size(draw, line, fnt)[1] + 2)
    y = (layout.height - sum(heights)) // 2

    for i, line in enumerate(lines):
        fnt = head_font if i == 0 else body_font
        shown = fit_text(draw, line, fnt, layout.width - layout.pad * 2)
        w, h = text_size(draw, shown, fnt)
        draw.text(((layout.width - w) // 2, y), shown, font=fnt,
                  fill=p.text if i == 0 else p.dim)
        y += h + 2


def circular_mask(image):
    """Black out the corners of a square frame bound for a round panel.

    A GC9A01 has a square framebuffer behind a circular window, so anything in
    the corners is written, paid for over SPI, and never seen. Clearing them
    keeps a stray descender from half-appearing at the rim.
    """
    w, h = image.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, w - 1, h - 1), fill=255)
    out = Image.new("RGB", (w, h), (0, 0, 0))
    out.paste(image, (0, 0), mask)
    return out


def to_mono(image, threshold=128):
    """Flatten to 1-bit for an OLED or e-paper panel.

    Thresholded rather than dithered on purpose: dithering a dashboard turns
    flat panels into noise, and at 128x64 the noise is larger than the text.
    """
    return image.convert("L").point(lambda v: 255 if v >= threshold else 0, mode="1")


def inscribed_square(diameter):
    """The largest square that fits inside a circle, for round panels."""
    return int(diameter / math.sqrt(2))


def chord_half_width(radius, dy):
    """Half the width of a circle's horizontal chord, dy above or below centre.

    What a round panel actually needs in order to place anything near its top
    or bottom: at dy = 0 the glass is the full diameter wide, and at the rim it
    is a point. A fixed inset guessed as a fraction of the width is either
    wasteful in the middle or clipped at the ends.
    """
    dy = abs(dy)
    if dy >= radius:
        return 0.0
    return math.sqrt(radius * radius - dy * dy)
