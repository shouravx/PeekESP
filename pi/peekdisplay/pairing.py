"""Derive the relay stream and tokens from a pairing code.

A deliberate copy rather than a shared import. This package is installed on its
own onto a Raspberry Pi, with no access to the rest of the repository, and the
three lines below are the entire contract - a dependency on another directory
would buy nothing and cost the ability to install this alone.

The copies are held together by tests/test_pi_display.py, which checks this
agrees with the Windows agent, the Linux agent, the Home Assistant integration
and the Worker's JavaScript. A one-character drift would mean pushing to a
stream nothing reads, with every request still looking valid from both ends.

    stream = SHA-256("peek-stream:" + CODE)  first 16 hex
    push   = SHA-256("peek-push:"   + CODE)  first 48 hex
    read   = SHA-256("peek-read:"   + CODE)  first 48 hex
"""

from __future__ import annotations

import hashlib
import re

# No I, O, 0 or 1: a code is read off a small screen and typed on a phone, and
# those four are the pairs people get wrong.
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIR_CODE_LEN = 10


class InvalidPairCode(ValueError):
    """The code is not ten characters from the alphabet."""


def normalise(code):
    """Dashes and case are decoration: "k7m2-p4qx-9r" is "K7M2P4QX9R"."""
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def format_code(code):
    """Group as the device shows it: K7M2-P4QX-9R."""
    c = normalise(code)
    return "-".join(filter(None, (c[0:4], c[4:8], c[8:12])))


def derive(code):
    """Return {code, stream, push, read}, or raise InvalidPairCode."""
    c = normalise(code)
    if len(c) != PAIR_CODE_LEN or any(ch not in PAIR_ALPHABET for ch in c):
        raise InvalidPairCode(
            "pairing code must be %d characters from %s "
            "(dashes and case are ignored)" % (PAIR_CODE_LEN, PAIR_ALPHABET)
        )

    def h(prefix, n):
        return hashlib.sha256((prefix + c).encode("ascii")).hexdigest()[:n]

    return {
        "code": c,
        "stream": h("peek-stream:", 16),
        "push": h("peek-push:", 48),
        "read": h("peek-read:", 48),
    }
