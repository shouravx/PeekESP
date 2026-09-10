#!/usr/bin/env python3
"""Tests for the Raspberry Pi display host.

    python -m unittest discover -s tests -v

Pillow is the only thing beyond the standard library, and the rendering tests
skip themselves without it. No display library is needed: the panel adapters
are exercised through the png panel, which is the same code path as a real one
right up to the point the bytes go out over SPI.

What this cannot cover is SPI and I2C themselves - whether a GC9A01 is wired to
the right pins, whether an SSD1306 is at 0x3C or 0x3D. That is what
--self-test is for, and it needs the panel in your hand.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "pi"))

from peekdisplay import character, config, panels          # noqa: E402
from peekdisplay import pairing as pi_pairing              # noqa: E402
from peekdisplay.relay import Machine, Snapshot, RelayError, parse_payload  # noqa: E402

try:
    from peekdisplay import layout
    HAVE_PIL = True
except ImportError:                                        # pragma: no cover
    HAVE_PIL = False

CODE = "K7M2P4QX9R"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ONLINE = Machine(host="kushtia-server", age_s=3, cpu_percent=42.5,
                 ram_percent=78.1, storage_percent=61.0,
                 storage_total_gb=1117.9, storage_free_gb=436.0,
                 cpu_temp_c=48.3, rx_kbps=1284.0, tx_kbps=112.9,
                 uptime_seconds=271830)
LAPTOP = Machine(host="dhaka-laptop", age_s=7, cpu_percent=91.2,
                 ram_percent=55.0, storage_percent=93.0, cpu_temp_c=71.0,
                 battery_percent=64, battery_charging=True, uptime_seconds=9300)


class TestPairingAgreesEverywhere(unittest.TestCase):
    """Five implementations of three lines. A drift is invisible from both ends."""

    def test_matches_the_home_assistant_integration(self):
        ha = _load("ha_pairing",
                   REPO / "custom_components" / "peekesp" / "pairing.py")
        self.assertEqual(pi_pairing.derive(CODE), ha.derive(CODE))

    def test_matches_the_windows_agent(self):
        theirs = _load("peek_pair", REPO / "windows" / "peek_pair.py").derive(CODE)
        mine = pi_pairing.derive(CODE)
        for key in ("stream", "push", "read"):
            with self.subTest(key=key):
                self.assertEqual(mine[key], theirs[key])

    def test_matches_the_linux_agent(self):
        theirs = _load("peek_agent", REPO / "dietpi" / "peek-agent.py").pair_derive(CODE)
        mine = pi_pairing.derive(CODE)
        for key in ("stream", "push"):
            with self.subTest(key=key):
                self.assertEqual(mine[key], theirs[key])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_matches_the_worker(self):
        script = f"""
        const enc = new TextEncoder();
        async function h(s) {{
          const d = await crypto.subtle.digest("SHA-256", enc.encode(s));
          return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2,"0")).join("");
        }}
        const c = {json.dumps(CODE)};
        (async () => console.log(JSON.stringify({{
          stream: (await h("peek-stream:" + c)).slice(0, 16),
          push:   (await h("peek-push:"   + c)).slice(0, 48),
          read:   (await h("peek-read:"   + c)).slice(0, 48),
        }})))();
        """
        out = subprocess.run(["node", "--input-type=module", "-e", script],
                             capture_output=True, text=True, check=True)
        theirs = json.loads(out.stdout)
        mine = pi_pairing.derive(CODE)
        for key in ("stream", "push", "read"):
            with self.subTest(key=key):
                self.assertEqual(mine[key], theirs[key])


class TestSnapshot(unittest.TestCase):
    def test_a_lost_network_ages_every_machine(self):
        """The failure the ESP32 had: without this a disconnected display shows
        every machine as permanently fresh, which is exactly backwards."""
        snap = Snapshot(machines=[ONLINE], stale_s=300)
        self.assertEqual(snap.age_of(ONLINE), 303)
        self.assertFalse(snap.is_online(ONLINE, 150))

    def test_fresh_data_is_online(self):
        snap = Snapshot(machines=[ONLINE])
        self.assertTrue(snap.is_online(ONLINE, 150))

    def test_machines_come_back_sorted_by_name(self):
        payload = {"devices": [{"host": "zeta"}, {"host": "alpha"},
                               {"host": "mid"}]}
        snap = parse_payload(payload)
        self.assertEqual([m.host for m in snap.machines],
                         ["alpha", "mid", "zeta"])

    def test_duplicates_are_collapsed(self):
        snap = parse_payload({"devices": [{"host": "a"}, {"host": "a"}]})
        self.assertEqual(len(snap.machines), 1)

    def test_the_legacy_flat_shape(self):
        snap = parse_payload({"host": "solo", "cpu_percent": 5})
        self.assertEqual([m.host for m in snap.machines], ["solo"])

    def test_sentinels_and_junk(self):
        m = Machine.parse({"host": "x", "cpu_temp_c": -1, "battery_percent": -1,
                           "ram_percent": "warm", "cpu_percent": True})
        self.assertIsNone(m.cpu_temp_c)
        self.assertIsNone(m.battery_percent)
        self.assertIsNone(m.ram_percent)
        self.assertIsNone(m.cpu_percent)      # bool is not a number here
        self.assertFalse(m.has_battery)

    def test_a_non_object_reply_is_an_error(self):
        with self.assertRaises(RelayError):
            parse_payload(["not", "an", "object"])


class TestCharacterPanels(unittest.TestCase):
    def test_every_line_is_exactly_the_panel_width(self):
        """Short strings must be padded, or the tail of the previous value
        stays on the glass - a character LCD clears nothing on its own."""
        snap = Snapshot(machines=[ONLINE])
        for step in range(8):
            lines = character.two_line(ONLINE, snap, step, cols=16)
            self.assertEqual(len(lines), 2)
            for line in lines:
                with self.subTest(step=step, line=line):
                    self.assertEqual(len(line), 16)

    def test_a_long_hostname_is_truncated_not_wrapped(self):
        long_host = Machine(host="a-very-long-hostname-indeed", age_s=1,
                            cpu_percent=5)
        lines = character.two_line(long_host, Snapshot(machines=[long_host]),
                                   0, cols=16)
        self.assertEqual(len(lines[0]), 16)

    def test_the_page_marker_never_overruns_the_line(self):
        snap = Snapshot(machines=[ONLINE, LAPTOP])
        lines = character.two_line(ONLINE, snap, 0, cols=16,
                                   machine_index=0, machine_count=2)
        self.assertEqual(len(lines[0]), 16)
        self.assertIn("1/2", lines[0])

    def test_offline_says_so_on_the_second_line(self):
        snap = Snapshot(machines=[ONLINE], stale_s=900)
        lines = character.two_line(ONLINE, snap, 0, cols=16)
        self.assertIn("OFFLINE", lines[1])

    def test_the_rotation_visits_every_field(self):
        rows = character.fields(ONLINE)
        seen = set()
        for step in range(len(rows) * 2):
            seen.add(character.two_line(ONLINE, Snapshot(machines=[ONLINE]),
                                        step, cols=16)[1].strip())
        self.assertGreaterEqual(len(seen), len(rows))

    def test_one_line_panels(self):
        lines = character.one_line(ONLINE, Snapshot(machines=[ONLINE]), 0, cols=16)
        self.assertEqual(len(lines), 1)
        self.assertEqual(len(lines[0]), 16)

    def test_the_marquee_names_every_machine(self):
        text = character.marquee([ONLINE, LAPTOP], Snapshot(machines=[ONLINE, LAPTOP]))
        self.assertIn("kushtia-server", text)
        self.assertIn("dhaka-laptop", text)

    def test_the_marquee_says_nothing_rather_than_lying(self):
        self.assertIn("no machines", character.marquee([], Snapshot()))


class TestConfig(unittest.TestCase):
    def test_panel_options_are_typed(self):
        cfg = config.Config({
            "PEEK_PANEL": "ssd1306",
            "PEEK_PANEL_ADDRESS": "0x3D",
            "PEEK_PANEL_ROTATE": "2",
            "PEEK_PANEL_ACTIVE_LOW": "false",
            "PEEK_PANEL_NAME": "left",
        })
        options = cfg.panel_options
        self.assertEqual(options["address"], 0x3D)     # hex survives as hex
        self.assertEqual(options["rotate"], 2)
        self.assertIs(options["active_low"], False)
        self.assertEqual(options["name"], "left")

    def test_empty_panel_options_are_dropped(self):
        cfg = config.Config({"PEEK_PANEL_WIDTH": ""})
        self.assertNotIn("width", cfg.panel_options)

    def test_offline_threshold_never_drops_below_a_minute(self):
        """A 1-second poll must not declare a machine dead between two of its
        own 5-second pushes."""
        cfg = config.Config({"PEEK_INTERVAL": "1", "PEEK_OFFLINE_AFTER_POLLS": "5"})
        self.assertEqual(cfg.offline_after_s, 60)

    def test_offline_threshold_follows_a_slow_poll(self):
        cfg = config.Config({"PEEK_INTERVAL": "30", "PEEK_OFFLINE_AFTER_POLLS": "5"})
        self.assertEqual(cfg.offline_after_s, 150)

    def test_reading_a_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("# a comment\n"
                     "PEEK_PANEL=gc9a01\n"
                     "PEEK_RELAY_BASE=https://example.invalid   # trailing\n"
                     "\n"
                     "PEEK_PANEL_WIDTH=240\n")
            path = fh.name
        try:
            values = config.read_file(path)
            self.assertEqual(values["PEEK_PANEL"], "gc9a01")
            # The trailing comment must not become part of the URL.
            self.assertEqual(values["PEEK_RELAY_BASE"], "https://example.invalid")
            self.assertEqual(values["PEEK_PANEL_WIDTH"], "240")
        finally:
            Path(path).unlink()

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(config.read_file("/nonexistent/peek.conf"), {})


class TestPanelRegistry(unittest.TestCase):
    def test_every_documented_panel_is_registered(self):
        for name in ("ili9341", "st7789", "gc9a01", "ssd1306", "sh1106",
                     "epaper", "hd44780", "max7219", "png"):
            with self.subTest(panel=name):
                self.assertIn(name, panels.names())

    def test_aliases_resolve(self):
        self.assertEqual(panels.ALIASES["round"], "gc9a01")
        self.assertEqual(panels.ALIASES["16x2"], "hd44780")

    def test_an_unknown_panel_lists_the_known_ones(self):
        with self.assertRaises(panels.PanelError) as caught:
            panels.create("definitely-not-a-panel")
        self.assertIn("ssd1306", str(caught.exception))

    def test_a_missing_library_names_the_package(self):
        """The failure people will actually hit, on a Pi with nothing
        installed. A bare ImportError traceback helps nobody."""
        try:
            panels.create("ssd1306")
        except panels.PanelError as err:
            self.assertIn("luma.oled", str(err))
        except Exception:            # noqa: BLE001 - luma present, or no I2C bus
            pass


@unittest.skipUnless(HAVE_PIL, "Pillow is not installed")
class TestRendering(unittest.TestCase):
    GEOMETRIES = [
        (320, 240, False, False), (240, 320, False, False),
        (240, 240, False, False), (240, 135, False, False),
        (160, 128, False, False), (240, 240, False, True),
        (128, 64, True, False), (128, 32, True, False),
        (250, 122, True, False), (296, 128, True, False),
    ]

    def _render(self, w, h, mono, round_, machines, snap, **kw):
        lay = layout.Layout.for_panel(w, h, mono=mono, round_=round_)
        return layout.render(lay, machines, 0, snap, **kw)

    def test_every_geometry_renders_at_its_exact_size(self):
        snap = Snapshot(machines=[ONLINE, LAPTOP])
        for w, h, mono, round_ in self.GEOMETRIES:
            with self.subTest(size="%dx%d" % (w, h), round=round_):
                image = self._render(w, h, mono, round_, [ONLINE, LAPTOP], snap)
                self.assertEqual(image.size, (w, h))

    def test_the_states_that_are_not_a_dashboard(self):
        """Offline, empty and error all have to fit too - they are what is on
        screen when something is wrong, which is when it is being read."""
        cases = [
            ("offline", [ONLINE], Snapshot(machines=[ONLINE], stale_s=900), {}),
            ("empty", [], Snapshot(), {}),
            ("message", [ONLINE], Snapshot(machines=[ONLINE]),
             {"message": ("Pairing failed", "the code was rejected")}),
        ]
        for w, h, mono, round_ in self.GEOMETRIES:
            for label, machines, snap, kw in cases:
                with self.subTest(size="%dx%d" % (w, h), case=label):
                    image = self._render(w, h, mono, round_, machines, snap, **kw)
                    self.assertEqual(image.size, (w, h))

    def test_a_machine_with_no_sensors_still_renders(self):
        bare = Machine(host="minimal", age_s=1)
        snap = Snapshot(machines=[bare])
        for w, h, mono, round_ in self.GEOMETRIES:
            with self.subTest(size="%dx%d" % (w, h)):
                self.assertEqual(self._render(w, h, mono, round_, [bare], snap).size,
                                 (w, h))

    def test_mono_conversion_is_one_bit(self):
        snap = Snapshot(machines=[ONLINE])
        image = layout.to_mono(self._render(128, 64, True, False, [ONLINE], snap))
        self.assertEqual(image.mode, "1")

    def test_the_round_mask_clears_the_corners(self):
        snap = Snapshot(machines=[ONLINE])
        image = layout.circular_mask(
            self._render(240, 240, False, True, [ONLINE], snap))
        for xy in ((0, 0), (239, 0), (0, 239), (239, 239)):
            with self.subTest(corner=xy):
                self.assertEqual(image.getpixel(xy), (0, 0, 0))

    def test_the_chord_helper(self):
        self.assertAlmostEqual(layout.chord_half_width(10, 0), 10.0)
        self.assertAlmostEqual(layout.chord_half_width(10, 6), 8.0)
        self.assertEqual(layout.chord_half_width(10, 10), 0.0)
        self.assertEqual(layout.chord_half_width(10, 99), 0.0)

    def test_capacity_and_rate_formatting(self):
        self.assertEqual(layout.fmt_capacity(436.0), "436G")
        self.assertEqual(layout.fmt_capacity(2048.0), "2.0T")
        self.assertEqual(layout.fmt_capacity(None), "--")
        self.assertEqual(layout.fmt_rate(1284.0), "1.3M")
        self.assertEqual(layout.fmt_rate(88.0), "88k")

    def test_no_drawn_glyph_is_missing_from_the_font(self):
        """The first version of the footer used U+2193 and U+2191 for the
        throughput arrows. DejaVu on the machine it was tested on had neither,
        so both rendered as filled boxes - and on a 1-bit OLED a missing glyph
        is the largest, blackest thing on the panel.

        Every string this draws is captured and checked against the font's own
        character map, so the next tempting symbol fails here rather than on
        someone's desk.
        """
        from PIL import ImageDraw

        captured = []
        original = ImageDraw.ImageDraw.text

        def spy(self, xy, text, *args, **kwargs):
            captured.append(str(text))
            return original(self, xy, text, *args, **kwargs)

        ImageDraw.ImageDraw.text = spy
        try:
            snap = Snapshot(machines=[ONLINE, LAPTOP])
            for w, h, mono, round_ in self.GEOMETRIES:
                self._render(w, h, mono, round_, [ONLINE, LAPTOP], snap)
            self._render(240, 135, False, False, [ONLINE],
                         Snapshot(machines=[ONLINE], stale_s=900))
        finally:
            ImageDraw.ImageDraw.text = original

        self.assertTrue(captured, "nothing was drawn - the spy missed")

        # An allowlist, not a probe of the font. The obvious test - "does this
        # glyph produce any ink" - passes for a missing character, because what
        # a font draws for one is a filled box, and a box has plenty of ink.
        # Asking the font proves nothing; restricting what we draw does.
        #
        # ASCII plus the two symbols checked by hand against both the DejaVu
        # that Raspberry Pi OS ships and Pillow's bitmap fallback, which is
        # what this runs on when DejaVu is absent.
        allowed = set(chr(c) for c in range(0x20, 0x7F)) | {"°", "…"}
        drawn = set("".join(captured))
        outside = sorted(ch for ch in drawn - allowed if ch.strip())
        self.assertEqual(
            outside, [],
            "drawn but not known-safe in every fallback font: %r "
            "(add it to the allowlist only after checking it renders)" % outside,
        )


@unittest.skipUnless(HAVE_PIL, "Pillow is not installed")
class TestPngPanelAndSelfTest(unittest.TestCase):
    def test_the_png_panel_writes_a_file_of_the_right_size(self):
        from peekdisplay import app
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "frame.png"
            panel = panels.create("png", width=240, height=135, path=str(out))
            display = app.Display(panel, relay=None)
            display.draw(Snapshot(machines=[ONLINE, LAPTOP]))
            self.assertTrue(out.exists())
            from PIL import Image
            with Image.open(out) as image:
                self.assertEqual(image.size, (240, 135))

    def test_self_test_runs_for_every_panel_kind(self):
        from peekdisplay import app
        with tempfile.TemporaryDirectory() as tmp:
            for w, h, round_ in ((240, 135, False), (240, 240, True),
                                 (128, 64, False)):
                with self.subTest(size="%dx%d" % (w, h)):
                    out = Path(tmp) / ("t%dx%d.png" % (w, h))
                    panel = panels.create("png", width=w, height=h,
                                          path=str(out), round=round_)
                    note = app.self_test(panel)
                    self.assertTrue(out.exists())
                    self.assertTrue(note)


if __name__ == "__main__":
    unittest.main(verbosity=2)
