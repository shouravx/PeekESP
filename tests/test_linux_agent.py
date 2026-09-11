#!/usr/bin/env python3
"""Tests for the Linux agent's display commands and update check.

    python -m unittest discover -s tests -v

Standard library only. The relay and the GitHub releases API are both played by
a small HTTP server on 127.0.0.1, so nothing here touches the network - and the
stub records exactly what arrived, which is the part worth checking. The relay
parses the raw body and trusts the token, so a JSON body or the wrong token
looks perfectly fine from this end and fails silently at the other.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
AGENT = REPO / "dietpi" / "peek-agent.py"
CLI = REPO / "dietpi" / "peekesp"

_spec = importlib.util.spec_from_file_location("peek_agent_commands", AGENT)
agent = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent)

# From the documented alphabet. The derivations are one-way, so this is nobody's
# real code and reveals nothing about any device.
CODE = "K7M2P4QX9R"
KEYS = agent.pair_derive(CODE)


class StubServer:
    """Plays both the relay and api.github.com, and remembers every request."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.release = {"tag_name": "v1.2.0"}
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, code, payload):
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                stub.requests.append({
                    "method": "POST",
                    "path": self.path,
                    "body": self.rfile.read(length).decode("latin-1"),
                    "auth": self.headers.get("Authorization"),
                    "type": self.headers.get("Content-Type"),
                    "agent": self.headers.get("User-Agent"),
                })
                self._reply(stub.status, {"ok": stub.status == 200})

            def do_GET(self):
                stub.requests.append({"method": "GET", "path": self.path,
                                      "agent": self.headers.get("User-Agent")})
                self._reply(stub.status, stub.release)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class TestVocabulary(unittest.TestCase):
    def test_bare_verbs(self):
        for verb in agent.COMMANDS:
            with self.subTest(verb=verb):
                self.assertEqual(agent.normalise_command(verb), verb)

    def test_case_and_whitespace_do_not_matter(self):
        self.assertEqual(agent.normalise_command("  REBOOT "), "reboot")
        self.assertEqual(agent.normalise_command("Bright", " 2 "), "bright:2")

    def test_numbers_as_a_second_word_or_after_a_colon(self):
        self.assertEqual(agent.normalise_command("page", 2), "page:2")
        self.assertEqual(agent.normalise_command("page", "0"), "page:0")
        self.assertEqual(agent.normalise_command("bright:3"), "bright:3")

    def test_bright_stops_at_the_levels_the_display_has(self):
        """The relay accepts up to 15 and the firmware ignores anything past
        its four levels. Accepted and silently ignored is the worst way for a
        command to fail, so it is refused here, with the range in the message."""
        with self.assertRaises(ValueError) as caught:
            agent.normalise_command("bright", 4)
        self.assertIn("0-3", str(caught.exception))
        with self.assertRaises(ValueError):
            agent.normalise_command("bright", -1)

    def test_page_stops_at_the_relays_bound(self):
        self.assertEqual(agent.normalise_command("page", 15), "page:15")
        with self.assertRaises(ValueError):
            agent.normalise_command("page", 16)

    def test_refuses_what_is_not_a_command(self):
        for verb, arg in (("rm -rf /", None), ("", None), ("reboot", "1"),
                          ("reboot:1", None), ("bright", None), ("page", "two"),
                          ("shutdown", None)):
            with self.subTest(verb=verb, arg=arg):
                with self.assertRaises(ValueError):
                    agent.normalise_command(verb, arg)

    def test_the_unknown_verb_message_lists_the_real_ones(self):
        with self.assertRaises(ValueError) as caught:
            agent.normalise_command("shutdown")
        for verb in agent.COMMANDS:
            self.assertIn(verb, str(caught.exception))

    def test_matches_the_worker(self):
        """Kept in step with COMMANDS in the Worker. A verb here that the
        relay does not know is a command that can only ever fail with a 400."""
        src = (REPO / "cloudflare" / "src" / "index.js").read_text(encoding="utf-8")
        found = re.search(r"const COMMANDS = new Set\(\[(.*?)\]\);", src, re.S)
        self.assertIsNotNone(found, "COMMANDS is not where it was in the Worker")
        worker = set(re.findall(r'"([a-z]+)"', found.group(1)))
        self.assertEqual(set(agent.COMMANDS) | set(agent.ARG_COMMANDS), worker)


class TestSendCommand(unittest.TestCase):
    def setUp(self):
        self.stub = StubServer()

    def tearDown(self):
        self.stub.close()

    def test_what_actually_reaches_the_relay(self):
        ok, message = agent.send_command(CODE, "identify", self.stub.base)
        self.assertTrue(ok, message)
        self.assertIn("queued", message)
        self.assertEqual(len(self.stub.requests), 1)
        req = self.stub.requests[0]
        self.assertEqual(req["path"], "/command/" + KEYS["stream"])
        # The bare verb as text. The Worker passes request.text() straight to
        # its parser, so a JSON body would be read as an unknown command.
        self.assertEqual(req["body"], "identify")
        self.assertEqual(req["type"], "text/plain")
        self.assertEqual(req["auth"], "Bearer " + KEYS["push"])
        # Cloudflare's edge refuses the stock Python-urllib agent with a 1010.
        self.assertTrue(req["agent"].startswith("PeekESP-agent/"))

    def test_a_numbered_command(self):
        ok, _ = agent.send_command(CODE, "bright:2", self.stub.base)
        self.assertTrue(ok)
        self.assertEqual(self.stub.requests[0]["body"], "bright:2")

    def test_the_pairing_code_itself_never_leaves_the_machine(self):
        agent.send_command(CODE, "reboot", self.stub.base)
        sent = json.dumps(self.stub.requests)
        self.assertNotIn(CODE, sent)
        self.assertNotIn(CODE.lower(), sent)

    def test_a_refused_credential(self):
        self.stub.status = 401
        ok, message = agent.send_command(CODE, "reboot", self.stub.base)
        self.assertFalse(ok)
        self.assertIn("refused", message)

    def test_a_rejected_verb(self):
        self.stub.status = 400
        ok, message = agent.send_command(CODE, "reboot", self.stub.base)
        self.assertFalse(ok)
        self.assertIn("rejected", message)

    def test_an_invalid_command_is_never_sent(self):
        ok, _ = agent.send_command(CODE, "format c:", self.stub.base)
        self.assertFalse(ok)
        self.assertEqual(self.stub.requests, [])

    def test_an_invalid_code_is_never_sent(self):
        ok, _ = agent.send_command("SHORT", "reboot", self.stub.base)
        self.assertFalse(ok)
        self.assertEqual(self.stub.requests, [])

    def test_plain_http_is_refused_off_this_machine(self):
        """Loopback is allowed so this can be tested and so a relay run with
        `wrangler dev` works. Anything else would carry the push token in the
        clear, and is refused before a connection is opened."""
        ok, message = agent.send_command(CODE, "reboot", "http://example.com")
        self.assertFalse(ok)
        self.assertIn("https://", message)

    def test_an_unreachable_relay(self):
        ok, message = agent.send_command(CODE, "reboot", "http://127.0.0.1:1",
                                         timeout=3)
        self.assertFalse(ok)
        self.assertIn("cannot reach", message)


class TestUpdateCheck(unittest.TestCase):
    def test_versions_compare_as_numbers(self):
        cases = [("1.10.0", "1.9.0", True), ("1.2.0", "1.2.0", False),
                 ("v1.3", "1.2.9", True), ("1.2", "1.2.0", False),
                 ("1.2.1", "1.10.0", False), ("", "1.2.0", False),
                 ("dev", "1.2.0", False)]
        for latest, current, want in cases:
            with self.subTest(latest=latest, current=current):
                self.assertIs(agent.is_newer(latest, current), want)

    def test_the_three_answers(self):
        code, message = agent.update_status("1.3.0", "1.2.0")
        self.assertEqual(code, 10)
        self.assertIn("1.3.0 is available", message)

        code, message = agent.update_status("1.2.0", "1.2.0")
        self.assertEqual(code, 0)
        self.assertIn("up to date", message)

        # A failed check is not news, and must never read as an update.
        code, message = agent.update_status("", "1.2.0")
        self.assertEqual(code, 1)
        self.assertIn("could not", message)

    def test_reads_the_release_tag(self):
        stub = StubServer()
        try:
            stub.release = {"tag_name": "v1.4.0"}
            self.assertEqual(agent.latest_release(stub.base + "/latest"), "1.4.0")
            self.assertTrue(stub.requests[0]["agent"].startswith("PeekESP-agent/"))
        finally:
            stub.close()

    def test_anything_odd_means_unknown_not_an_error(self):
        stub = StubServer()
        try:
            stub.status = 500
            self.assertEqual(agent.latest_release(stub.base), "")
            stub.status, stub.release = 200, ["not", "an", "object"]
            self.assertEqual(agent.latest_release(stub.base), "")
            stub.release = {}
            self.assertEqual(agent.latest_release(stub.base), "")
        finally:
            stub.close()
        self.assertEqual(agent.latest_release("http://127.0.0.1:1", timeout=3), "")

    def test_the_version_is_kept_in_step(self):
        """AGENT_VERSION is one of the sites bump_version.py enforces, so
        `peekesp version` cannot drift from the release it shipped in."""
        run = subprocess.run([sys.executable, str(REPO / "tools" / "bump_version.py"),
                              "--check"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("AGENT_VERSION", run.stdout)


class TestCommandLine(unittest.TestCase):
    """The agent run the way peekesp runs it."""

    def run_agent(self, *args, env=None):
        return subprocess.run([sys.executable, str(AGENT), *args],
                              capture_output=True, text=True, env=env, timeout=30)

    def test_version(self):
        run = self.run_agent("--version")
        self.assertEqual(run.returncode, 0)
        self.assertEqual(run.stdout.strip(), agent.AGENT_VERSION)

    def test_a_command_end_to_end(self):
        stub = StubServer()
        try:
            run = self.run_agent("--pair-code", CODE, "--relay-base", stub.base,
                                 "--command", "bright", "2")
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("queued", run.stdout)
            self.assertEqual(stub.requests[0]["body"], "bright:2")
        finally:
            stub.close()

    def test_an_out_of_range_command_says_why_and_sends_nothing(self):
        stub = StubServer()
        try:
            run = self.run_agent("--pair-code", CODE, "--relay-base", stub.base,
                                 "--command", "bright", "9")
            self.assertEqual(run.returncode, 2)
            self.assertIn("0-3", run.stderr)
            self.assertEqual(stub.requests, [])
        finally:
            stub.close()

    def test_a_command_needs_a_code(self):
        env = dict(os.environ)
        env.pop("PEEK_PAIR_CODE", None)
        run = self.run_agent("--command", "reboot", env=env)
        self.assertEqual(run.returncode, 2)
        self.assertIn("pairing code", run.stderr)

    @unittest.skipUnless(shutil.which("sh"), "no POSIX sh")
    def test_peekesp_parses_and_documents_both(self):
        self.assertEqual(subprocess.run(["sh", "-n", str(CLI)]).returncode, 0)
        run = subprocess.run(["sh", str(CLI), "help"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("peekesp cmd", run.stdout)
        self.assertIn("peekesp check", run.stdout)


@unittest.skipUnless(shutil.which("sh"), "no POSIX sh")
class TestPeekespScript(unittest.TestCase):
    """The shell wrapper, run for real against a stub relay.

    The tests above cover the agent. These cover the glue: that peekesp reads
    the config, passes the code and the relay through under the flag names the
    agent actually has, and quotes a two-word command correctly. That is the
    layer where a typo passes every other test and fails on first use.

    peekesp hardcodes its install paths and wants root, so it runs from a copy
    with the paths pointed at a temporary install - and, where the test is
    about what happens after the root check, with that one check removed.
    """

    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp(prefix="peekesp-test-"))
        shutil.copy(AGENT, self.tmp / "peek-agent.py")
        (self.tmp / "etc").mkdir()
        self.stub = StubServer()
        (self.tmp / "etc" / "agent.conf").write_text(
            "PEEK_PAIR_CODE=%s\nPEEK_RELAY_BASE=%s\n" % (CODE, self.stub.base),
            encoding="ascii")

    def tearDown(self):
        self.stub.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def script(self, as_root):
        src = CLI.read_text(encoding="utf-8")
        swaps = [
            ('PREFIX="/opt/peekesp"', 'PREFIX="%s"' % self.tmp.as_posix()),
            ('CONF_DIR="/etc/peekesp"', 'CONF_DIR="%s/etc"' % self.tmp.as_posix()),
            # This interpreter, not whatever python3 resolves to - on Windows
            # that may be nothing, or the Store's alias.
            ('python3 "$AGENT"', '"%s" "$AGENT"' % Path(sys.executable).as_posix()),
        ]
        if as_root:
            swaps.append(('need_root cmd "$@"', ": root check removed for the test"))
        for old, new in swaps:
            # If the script stops containing a line this swaps, the test must
            # fail loudly rather than silently run against the real paths.
            self.assertIn(old, src, "peekesp no longer contains %r" % old)
            src = src.replace(old, new)
        path = self.tmp / ("peekesp-root" if as_root else "peekesp-user")
        path.write_text(src, encoding="utf-8", newline="\n")
        return path

    def run_sh(self, script, *args):
        return subprocess.run(["sh", str(script), *args],
                              capture_output=True, text=True, timeout=60)

    def test_cmd_reaches_the_relay_as_the_agent_would_send_it(self):
        run = self.run_sh(self.script(as_root=True), "cmd", "bright", "2")
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn("queued", run.stdout)
        self.assertEqual(len(self.stub.requests), 1)
        self.assertEqual(self.stub.requests[0]["body"], "bright:2")
        self.assertEqual(self.stub.requests[0]["path"], "/command/" + KEYS["stream"])
        self.assertEqual(self.stub.requests[0]["auth"], "Bearer " + KEYS["push"])

    def test_cmd_with_nothing_explains_itself(self):
        run = self.run_sh(self.script(as_root=False), "cmd")
        self.assertEqual(run.returncode, 1)
        self.assertIn("usage: sudo peekesp cmd", run.stderr)
        self.assertIn("bright N", run.stderr)

    def test_cmd_asks_for_root(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("running as root")
        run = self.run_sh(self.script(as_root=False), "cmd", "reboot")
        self.assertEqual(run.returncode, 1)
        self.assertIn("needs root", run.stderr)
        self.assertEqual(self.stub.requests, [])

    def test_a_bad_verb_is_refused_before_the_relay(self):
        run = self.run_sh(self.script(as_root=True), "cmd", "shutdown")
        self.assertNotEqual(run.returncode, 0)
        self.assertIn("unknown command", run.stderr)
        self.assertEqual(self.stub.requests, [])

    def test_version_reports_the_agent(self):
        run = self.run_sh(self.script(as_root=False), "version")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("agent     %s" % agent.AGENT_VERSION, run.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
