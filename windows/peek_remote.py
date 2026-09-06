"""
peek_remote.py - talk to the device, and find out whether it is out of date.

Two things the app can do beyond pushing telemetry:

  * ask GitHub what the newest published version is, and
  * leave a command at the relay for the device to collect on its next poll.

Standard library only, like everything else here.
"""

import json
import threading
import time
import urllib.error
import urllib.request

import peek_pair as pair

# Cloudflare's edge bans the default "Python-urllib/3.x" user agent outright
# with error 1010, and GitHub rejects a request with no user agent at all.
USER_AGENT = "PeekESP-app/1.0 (+https://github.com/shouravx/PeekESP)"

RELEASES_API = "https://api.github.com/repos/shouravx/PeekESP/releases/latest"
RELEASES_PAGE = "https://github.com/shouravx/PeekESP/releases"

# Commands the relay will accept. Duplicated from the Worker on purpose: the
# app should refuse an unknown verb before spending a request finding out, and
# the Worker should refuse one regardless of what any client believes.
COMMANDS = ("reboot", "standby", "wake", "refresh", "identify")


def _get(url, timeout=8, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _parse_version(text):
    """"v1.10.2" -> (1, 10, 2). Unparseable parts become 0 rather than raising.

    Compared as numbers, not as strings: "1.10.0" is newer than "1.9.0" and
    sorts before it alphabetically, which is the classic way an update check
    goes quiet exactly when it starts mattering.
    """
    parts = str(text or "").strip().lstrip("vV").split(".")
    out = []
    for p in parts[:4]:
        digits = "".join(c for c in p if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out) or (0,)


def is_newer(latest, current):
    a, b = _parse_version(latest), _parse_version(current)
    n = max(len(a), len(b))
    a += (0,) * (n - len(a))
    b += (0,) * (n - len(b))
    return a > b


class UpdateCheck:
    """Checks once, in the background, and remembers the answer.

    Never blocks the UI and never raises into it: a failed check leaves
    `latest` empty, which every caller reads as "no news". An update prompt
    that appears because the network was down would teach people to ignore it.
    """

    def __init__(self, current):
        self.current = current
        self.latest = ""
        self.checked_at = 0.0
        self._lock = threading.Lock()

    @property
    def available(self):
        return bool(self.latest) and is_newer(self.latest, self.current)

    def start(self, min_interval=6 * 3600):
        with self._lock:
            if time.time() - self.checked_at < min_interval:
                return
            self.checked_at = time.time()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            data = _get(RELEASES_API,
                        headers={"Accept": "application/vnd.github+json"})
            tag = str(data.get("tag_name") or "").lstrip("vV")
            if tag:
                self.latest = tag
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
            # Deliberately silent. There is no useful thing to tell someone
            # about a failed version check that they did not ask for.
            pass


def send_command(relay_base, code, verb, timeout=10):
    """Leave a command for the device. Returns (ok, message).

    The device polls, so this does not reach it directly - the relay holds the
    command until the next poll, which is why the message says "queued" rather
    than "done". At the default interval that is under five seconds; in standby
    it is up to a minute.
    """
    verb = (verb or "").strip().lower()
    if verb.split(":", 1)[0] not in COMMANDS and not verb.startswith(("page:", "bright:")):
        return False, f"unknown command: {verb}"

    try:
        d = pair.derive(code)
    except ValueError as e:
        return False, str(e)

    base = (relay_base or "").rstrip("/")
    if not base.startswith("https://"):
        return False, "relay must be an https:// URL"
    url = f"{base}/command/{d['stream']}"

    req = urllib.request.Request(
        url, data=verb.encode("ascii"), method="POST",
        headers={"User-Agent": USER_AGENT,
                 "Content-Type": "text/plain",
                 "Authorization": "Bearer " + d["push"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True, f"{verb} queued - the device picks it up on its next poll"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "the relay refused this pairing code"
        if e.code == 400:
            return False, f"the relay rejected '{verb}'"
        return False, f"HTTP {e.code} {e.reason}"
    except Exception as e:
        return False, str(e)


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        print(send_command(
            sys.argv[1] if sys.argv[1].startswith("http")
            else "https://peek-relay.peekesp.workers.dev",
            sys.argv[-2], sys.argv[-1]))
    else:
        u = UpdateCheck("0.0.0")
        u.start()
        time.sleep(4)
        print("latest:", u.latest or "(check failed or no releases)")
