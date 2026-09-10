"""The render loop.

Polling and drawing are deliberately on one thread. The ESP32 splits them
across two cores because a blocking TLS handshake there would drop an LVGL
frame; here there are no frames to drop - a Pi redrawing a static dashboard
every five seconds is not animating anything, and a thread would buy nothing
but a race over the snapshot.
"""

from __future__ import annotations

import logging
import time

from . import character, layout, panels
from .relay import AuthError, Relay, RelayError

log = logging.getLogger("peekdisplay")


class Display:
    """Owns a panel and a relay, and keeps them in step."""

    def __init__(self, panel, relay, interval=5, rotate_seconds=8,
                 offline_after_s=150):
        self.panel = panel
        self.relay = relay
        self.interval = max(1.0, float(interval))
        self.rotate_seconds = max(1.0, float(rotate_seconds))
        self.offline_after_s = int(offline_after_s)

        self.index = 0
        self.step = 0
        self._layout = None
        self._last_draw = 0.0
        self._last_rotate = time.monotonic()
        self._error = None

        if panel.kind == panels.KIND_PIXEL:
            width, height = panel.size
            self._layout = layout.Layout.for_panel(
                width, height, mono=panel.mono, round_=panel.round
            )

    # -- one pass -----------------------------------------------------------

    def poll(self):
        snapshot, error = self.relay.poll()
        if error is not None and not isinstance(error, AuthError):
            log.warning("%s", error)
        self._error = error
        return snapshot

    def advance(self, snapshot):
        """Move to the next machine once its time is up."""
        count = len(snapshot.machines)
        now = time.monotonic()
        if count > 1 and now - self._last_rotate >= self.rotate_seconds:
            self.index = (self.index + 1) % count
            self._last_rotate = now
        elif count <= 1:
            self.index = 0
        # The character panels rotate through fields rather than machines, and
        # do it on the same clock so the two never fight over the screen.
        self.step = int(now // self.rotate_seconds)

    def message_for(self, snapshot):
        """What to show instead of a dashboard, or None to draw one."""
        if isinstance(self._error, AuthError):
            return ("Pairing failed",
                    "the code was rejected",
                    "check the device's screen")
        if not snapshot.machines:
            if self._error is not None:
                return ("No connection", str(self._error)[:60])
            return ("No machines", "are reporting yet")
        return None

    def draw(self, snapshot):
        panel = self.panel

        if panel.kind == panels.KIND_MARQUEE:
            message = self.message_for(snapshot)
            if message:
                panel.display("  ".join(message))
            else:
                panel.display(character.marquee(
                    snapshot.machines, snapshot, self.offline_after_s))
            return

        if panel.kind == panels.KIND_TEXT:
            cols, rows = panel.size
            message = self.message_for(snapshot)
            if message:
                lines = [str(m)[:cols].ljust(cols) for m in message[:rows]]
                panel.display(lines + [" " * cols] * (rows - len(lines)))
                return
            machine = snapshot.machines[self.index % len(snapshot.machines)]
            renderer = character.two_line if rows >= 2 else character.one_line
            panel.display(renderer(
                machine, snapshot, self.step, cols=cols,
                offline_after_s=self.offline_after_s,
                **({"machine_index": self.index % len(snapshot.machines),
                    "machine_count": len(snapshot.machines)} if rows >= 2 else {})
            ))
            return

        image = layout.render(
            self._layout, snapshot.machines, self.index, snapshot,
            offline_after_s=self.offline_after_s,
            message=self.message_for(snapshot),
        )
        if panel.round:
            image = layout.circular_mask(image)
        if panel.mono:
            image = layout.to_mono(image)
        panel.display(image)

    def tick(self):
        """Poll if due, then draw. Returns the snapshot that was drawn."""
        snapshot = self.poll()
        self.advance(snapshot)

        now = time.monotonic()
        if now - self._last_draw >= self.panel.min_interval:
            self.draw(snapshot)
            self._last_draw = now
        return snapshot

    # -- forever ------------------------------------------------------------

    def run(self):
        """Until interrupted. A relay failure ages the data, it does not stop."""
        # The rotation is usually faster than the poll, so the loop runs on the
        # shorter of the two and only fetches when the interval is actually up.
        beat = min(self.interval, self.rotate_seconds)
        if self.panel.kind == panels.KIND_MARQUEE:
            # A scroll blocks for as long as it takes to read; adding a sleep
            # on top would leave the matrix dark between passes.
            beat = 0.0

        next_poll = 0.0
        snapshot = self.relay.last
        try:
            while True:
                now = time.monotonic()
                if now >= next_poll:
                    snapshot = self.poll()
                    next_poll = now + self.interval
                self.advance(snapshot)

                if time.monotonic() - self._last_draw >= self.panel.min_interval:
                    self.draw(snapshot)
                    self._last_draw = time.monotonic()

                if beat:
                    # Sleep only as long as is left, so a slow draw does not
                    # push the poll later and later.
                    slack = beat - (time.monotonic() - now)
                    if slack > 0:
                        time.sleep(slack)
        except KeyboardInterrupt:
            log.info("stopping")


def self_test(panel):
    """Draw something whose correctness is obvious across the room.

    The point of this is wiring. A panel with its RGB order wrong, its rotation
    wrong, or one data line not connected still shows *something* when handed a
    dashboard - and a dashboard that is subtly wrong looks like a bug in the
    software. A named colour bar and a labelled frame do not: if the box is not
    on all four edges the geometry is wrong, and if the swatches are in the
    wrong order the colour order is.
    """
    from PIL import Image, ImageDraw

    if panel.kind == panels.KIND_MARQUEE:
        panel.display("PEEK SELF TEST 0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ  ")
        return "scrolled the alphabet - every character should be complete"

    if panel.kind == panels.KIND_TEXT:
        cols, rows = panel.size
        lines = ["".join(str(i % 10) for i in range(cols))]
        lines.append(("PEEK SELF TEST " * 3)[:cols])
        panel.display(lines[:rows])
        return ("row 0 counts 0-9 across every cell; if it stops short, cols "
                "is wrong")

    width, height = panel.size
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)

    # A one-pixel frame on the exact boundary. Any rotation or offset error
    # takes an edge off it.
    draw.rectangle((0, 0, width - 1, height - 1), outline=(255, 255, 255))

    swatches = [("R", (255, 0, 0)), ("G", (0, 255, 0)), ("B", (0, 0, 255)),
                ("W", (255, 255, 255))]
    band = max(6, height // 6)
    step = width // len(swatches)
    for i, (label, colour) in enumerate(swatches):
        x0 = i * step
        draw.rectangle((x0 + 2, height - band - 2, x0 + step - 2, height - 3),
                       fill=colour)
        draw.text((x0 + 4, height - band - 14), label, fill=(255, 255, 255))

    fnt = layout.font(max(10, min(width, height) // 8), True)
    caption = "%dx%d" % (width, height)
    w, h = layout.text_size(draw, caption, fnt)
    draw.text(((width - w) // 2, (height - h) // 2 - 6), caption,
              font=fnt, fill=(255, 255, 255))
    # Corner ticks, so a panel cropping a few columns is visible.
    for cx, cy in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        draw.line((cx, cy, cx, cy + (8 if cy == 0 else -8)), fill=(255, 0, 255))
        draw.line((cx, cy, cx + (8 if cx == 0 else -8), cy), fill=(255, 0, 255))

    if panel.round:
        image = layout.circular_mask(image)
    if panel.mono:
        image = layout.to_mono(image)
    panel.display(image)
    return ("a full white frame with magenta corner ticks, R G B W left to "
            "right, and the size in the middle")
