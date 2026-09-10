"""Derive the relay stream and tokens from a pairing code.

This is the fourth implementation of the same three lines - C++ on the device,
Python in the Windows app, Python in the Linux agent, JavaScript in the Worker,
and now here. They are pinned against vectors generated independently by
``openssl``, because a drift between any two would mean this integration
reading a stream nothing pushes to, with every request still looking perfectly
valid from both ends.

    stream = SHA-256("peek-stream:" + CODE)  first 16 hex
    push   = SHA-256("peek-push:"   + CODE)  first 48 hex
    read   = SHA-256("peek-read:"   + CODE)  first 48 hex

Standard library only, and no I/O: the whole point of pairing is that the code
never leaves the machine that typed it. What travels is the derived stream id
and a token, and neither can be turned back into the code.
"""

from __future__ import annotations

import hashlib
import re

# No I, O, 0 or 1. A code is read off a 1.14" screen and typed on a phone, and
# those four are the pairs people get wrong.
PAIR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIR_CODE_LEN = 10


class InvalidPairCode(ValueError):
    """The code is not ten characters from the alphabet."""


def normalise(code: str | None) -> str:
    """Dashes and case are decoration: "k7m2-p4qx-9r" is "K7M2P4QX9R"."""
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def format_code(code: str) -> str:
    """Group as the device shows it: K7M2-P4QX-9R."""
    c = normalise(code)
    return "-".join(filter(None, (c[0:4], c[4:8], c[8:12])))


def derive(code: str) -> dict[str, str]:
    """Return {code, stream, push, read}, or raise InvalidPairCode."""
    c = normalise(code)
    if len(c) != PAIR_CODE_LEN or any(ch not in PAIR_ALPHABET for ch in c):
        raise InvalidPairCode(
            f"pairing code must be {PAIR_CODE_LEN} characters from "
            f"{PAIR_ALPHABET} (dashes and case are ignored)"
        )

    def h(prefix: str, n: int) -> str:
        return hashlib.sha256((prefix + c).encode("ascii")).hexdigest()[:n]

    return {
        "code": c,
        "stream": h("peek-stream:", 16),
        "push": h("peek-push:", 48),
        "read": h("peek-read:", 48),
    }
