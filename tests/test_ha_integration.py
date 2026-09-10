#!/usr/bin/env python3
"""Tests for the Home Assistant integration's pure logic.

    python -m unittest discover -s tests -v

Standard library only, and no Home Assistant import anywhere in the modules
under test - so this runs on a clean checkout with nothing installed, which is
the only way a test suite stays runnable long enough to be worth having.

What it cannot cover is the Home Assistant surface itself: entity registration,
the config flow's dialogs, the coordinator's HTTP. Those need a Home Assistant
instance. What it does cover is the part most likely to be wrong - parsing a
payload that four agent versions on three operating systems produce - and the
pairing derivation, where a one-character drift from the other implementations
would mean reading a stream nothing pushes to, with every request still looking
perfectly valid from both ends.
"""

import importlib.util
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "custom_components" / "peekesp"))

import model            # noqa: E402
import pairing          # noqa: E402


def _load(name: str, path: Path):
    """Import a module by path, so the agents can be compared without packaging."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A code from the documented alphabet. Not a real pairing - the derivations are
# one-way, so this reveals nothing about any device.
CODE = "K7M2P4QX9R"
CODE_MESSY = "  k7m2-p4qx-9r  "


class TestPairing(unittest.TestCase):
    def test_normalise_ignores_dashes_and_case(self):
        self.assertEqual(pairing.normalise(CODE_MESSY), CODE)
        self.assertEqual(pairing.normalise("K7M2 P4QX 9R"), CODE)
        self.assertEqual(pairing.normalise(None), "")

    def test_format_is_the_grouping_the_device_shows(self):
        self.assertEqual(pairing.format_code(CODE), "K7M2-P4QX-9R")
        self.assertEqual(pairing.format_code(CODE_MESSY), "K7M2-P4QX-9R")

    def test_derive_is_stable_and_one_way(self):
        keys = pairing.derive(CODE)
        self.assertEqual(len(keys["stream"]), 16)
        self.assertEqual(len(keys["push"]), 48)
        self.assertEqual(len(keys["read"]), 48)
        # Three different prefixes must not collide into one secret.
        self.assertNotEqual(keys["push"], keys["read"])
        self.assertFalse(keys["stream"] in keys["push"])

    def test_messy_input_derives_the_same_keys(self):
        self.assertEqual(pairing.derive(CODE), pairing.derive(CODE_MESSY))

    def test_rejects_codes_that_are_not_codes(self):
        for bad in ("", "SHORT", "K7M2P4QX9R0", "K7M2P4QXI9", "K7M2-P4QX-9!"):
            with self.subTest(code=bad):
                with self.assertRaises(pairing.InvalidPairCode):
                    pairing.derive(bad)

    def test_rejects_the_excluded_letters(self):
        # I, O, 0 and 1 are deliberately absent from the alphabet because they
        # are misread off a 1.14" screen. A code containing one is a typo, and
        # accepting it would derive a stream nothing pushes to.
        for ch in "IO01":
            with self.subTest(char=ch):
                with self.assertRaises(pairing.InvalidPairCode):
                    pairing.derive("K7M2P4QX9" + ch)


class TestDerivationMatchesTheOtherImplementations(unittest.TestCase):
    """A drift here is invisible from either end and breaks everything."""

    def test_matches_the_windows_agent(self):
        peek_pair = _load("peek_pair", REPO / "windows" / "peek_pair.py")
        theirs = peek_pair.derive(CODE)
        mine = pairing.derive(CODE)
        for key in ("stream", "push", "read"):
            with self.subTest(key=key):
                self.assertEqual(mine[key], theirs[key])

    def test_matches_the_linux_agent(self):
        agent = _load("peek_agent", REPO / "dietpi" / "peek-agent.py")
        theirs = agent.pair_derive(CODE)
        mine = pairing.derive(CODE)
        # The Linux agent pushes and never reads, so it derives no read token.
        for key in ("stream", "push"):
            with self.subTest(key=key):
                self.assertEqual(mine[key], theirs[key])

    @unittest.skipUnless(shutil.which("node"), "node is not installed")
    def test_matches_the_worker(self):
        """The Worker is the one implementation in another language."""
        script = f"""
        const enc = new TextEncoder();
        async function sha256hex(s) {{
          const d = await crypto.subtle.digest("SHA-256", enc.encode(s));
          return [...new Uint8Array(d)].map(b => b.toString(16).padStart(2, "0")).join("");
        }}
        const c = {json.dumps(CODE)};
        (async () => console.log(JSON.stringify({{
          stream: (await sha256hex("peek-stream:" + c)).slice(0, 16),
          push:   (await sha256hex("peek-push:"   + c)).slice(0, 48),
          read:   (await sha256hex("peek-read:"   + c)).slice(0, 48),
        }})))();
        """
        out = subprocess.run(
            ["node", "--input-type=module", "-e", script],
            capture_output=True, text=True, check=True,
        )
        theirs = json.loads(out.stdout)
        mine = pairing.derive(CODE)
        for key in ("stream", "push", "read"):
            with self.subTest(key=key):
                self.assertEqual(mine[key], theirs[key])


class TestMachineParse(unittest.TestCase):
    FULL = {
        "host": "dietpi", "cpu_percent": 12.5, "ram_percent": 43.2,
        "storage_percent": 61.0, "storage_total_gb": 117.9,
        "storage_free_gb": 46.0, "cpu_temp_c": 48.3, "battery_percent": 78,
        "battery_charging": True, "battery_ac": True, "battery_minutes": 134,
        "uptime_seconds": 271830, "net_rx_kbps": 128.4, "net_tx_kbps": 12.9,
        "age_s": 3,
    }

    def test_a_complete_reading(self):
        m = model.Machine.parse(self.FULL)
        self.assertEqual(m.host, "dietpi")
        self.assertAlmostEqual(m.cpu_percent, 12.5)
        self.assertAlmostEqual(m.cpu_temp_c, 48.3)
        self.assertEqual(m.battery_percent, 78)
        self.assertTrue(m.battery_charging)
        self.assertEqual(m.uptime_seconds, 271830)
        self.assertEqual(m.age_s, 3)
        self.assertTrue(m.has_battery)

    def test_negative_sentinels_become_unknown(self):
        """-1 means "this machine has no such sensor", not minus one degree."""
        raw = dict(self.FULL, cpu_temp_c=-1.0, battery_percent=-1,
                   battery_minutes=-1)
        m = model.Machine.parse(raw)
        self.assertIsNone(m.cpu_temp_c)
        self.assertIsNone(m.battery_percent)
        self.assertIsNone(m.battery_minutes)
        self.assertFalse(m.has_battery)

    def test_missing_fields_are_unknown_not_zero(self):
        """An older agent omits fields. Zero would be a plausible-looking lie."""
        m = model.Machine.parse({"host": "old-agent"})
        self.assertEqual(m.host, "old-agent")
        self.assertIsNone(m.cpu_percent)
        self.assertIsNone(m.storage_total_gb)
        self.assertIsNone(m.uptime_seconds)
        self.assertEqual(m.age_s, 0)

    def test_a_nameless_reading_still_parses(self):
        m = model.Machine.parse({})
        self.assertEqual(m.host, "unknown")

    def test_booleans_are_not_mistaken_for_numbers(self):
        # bool is a subclass of int in Python, so a stray True in a numeric
        # field would otherwise read as 1.0 - a believable CPU percentage.
        m = model.Machine.parse({"host": "x", "cpu_percent": True})
        self.assertIsNone(m.cpu_percent)

    def test_garbage_does_not_propagate(self):
        m = model.Machine.parse({"host": "x", "cpu_percent": "hot",
                                 "ram_percent": None, "uptime_seconds": []})
        self.assertIsNone(m.cpu_percent)
        self.assertIsNone(m.ram_percent)
        self.assertIsNone(m.uptime_seconds)

    def test_nan_and_infinity_are_rejected(self):
        # json.loads accepts bare NaN and Infinity. Either one poisons every
        # average, graph and long-term statistic downstream of it.
        raw = json.loads('{"host":"x","cpu_percent":NaN,"ram_percent":Infinity}')
        m = model.Machine.parse(raw)
        self.assertIsNone(m.cpu_percent)
        self.assertIsNone(m.ram_percent)

    def test_a_long_host_name_is_bounded(self):
        m = model.Machine.parse({"host": "h" * 500})
        self.assertLessEqual(len(m.host), 32)


class TestParsePayload(unittest.TestCase):
    def test_the_devices_array(self):
        payload = {
            "host": "b", "cpu_percent": 9,
            "devices": [
                {"host": "a", "cpu_percent": 1, "age_s": 2},
                {"host": "b", "cpu_percent": 9, "age_s": 0},
            ],
            "device_count": 2,
            "latest_fw": "1.2.0",
        }
        data = model.parse_payload(payload, offline_after_s=60)
        self.assertEqual(sorted(data.machines), ["a", "b"])
        self.assertEqual(data.latest_fw, "1.2.0")

    def test_the_legacy_flat_shape(self):
        """A Worker older than per-host slots returns one machine, no array."""
        data = model.parse_payload(
            {"host": "solo", "cpu_percent": 5, "age_s": 1}, offline_after_s=60
        )
        self.assertEqual(list(data.machines), ["solo"])

    def test_an_empty_array_falls_back_to_the_flat_shape(self):
        data = model.parse_payload(
            {"host": "solo", "devices": [], "age_s": 1}, offline_after_s=60
        )
        self.assertEqual(list(data.machines), ["solo"])

    def test_nothing_at_all(self):
        data = model.parse_payload({"devices": []}, offline_after_s=60)
        self.assertEqual(data.machines, {})
        self.assertIsNone(data.latest_fw)

    def test_junk_rows_are_skipped_not_fatal(self):
        payload = {"devices": [{"host": "a"}, "nonsense", None, 42]}
        data = model.parse_payload(payload, offline_after_s=60)
        self.assertEqual(list(data.machines), ["a"])

    def test_a_non_object_reply_is_an_error(self):
        for bad in ([], "text", None, 7):
            with self.subTest(payload=bad):
                with self.assertRaises(ValueError):
                    model.parse_payload(bad, offline_after_s=60)

    def test_duplicate_hosts_keep_the_last(self):
        payload = {"devices": [{"host": "a", "cpu_percent": 1},
                               {"host": "a", "cpu_percent": 2}]}
        data = model.parse_payload(payload, offline_after_s=60)
        self.assertEqual(len(data.machines), 1)
        self.assertAlmostEqual(data.machines["a"].cpu_percent, 2)


class TestOnlineThreshold(unittest.TestCase):
    def _data(self, age_s, offline_after_s=150):
        return model.parse_payload(
            {"devices": [{"host": "a", "age_s": age_s}]},
            offline_after_s=offline_after_s,
        )

    def test_fresh_is_online(self):
        self.assertTrue(self._data(3).is_online("a"))

    def test_stale_is_offline(self):
        self.assertFalse(self._data(600).is_online("a"))

    def test_the_boundary_is_exclusive(self):
        self.assertFalse(self._data(150).is_online("a"))
        self.assertTrue(self._data(149).is_online("a"))

    def test_a_machine_that_was_never_there(self):
        self.assertFalse(self._data(3).is_online("ghost"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
