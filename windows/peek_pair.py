"""
peek_pair.py - turn a pairing code into a stream id and a token pair.

The device shows a code on its screen. You type it into the app. Both sides
run this same derivation locally, so nothing but the code ever passes between
them and the relay never sees it - which is why pairing needs no secrets
configured on the Worker at all.

    stream = SHA-256("peek-stream:" + CODE)  first 16 hex
    push   = SHA-256("peek-push:"   + CODE)  first 48 hex
    read   = SHA-256("peek-read:"   + CODE)  first 48 hex

CODE is upper-cased with everything outside A-Z0-9 stripped first, so
"k7m2-p4qx-9r" and "K7M2P4QX9R" are the same code. Typing the dashes is
optional and case does not matter.

The same derivation exists in cloudflare/src/index.js (JavaScript) and in the
firmware (C++ via mbedTLS). cloudflare/test/worker.test.mjs asserts the first
two agree byte for byte; a drift would mean the PC and the device silently
derive different streams and never meet, with every request looking valid.
"""

import hashlib
import re
import secrets

# 32 characters, with I, O, 0 and 1 removed: those are the pairs people
# mistype when copying a code off a 1.14" screen.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LEN = 10                      # 32^10 = 2^50
STREAM_HEX = 16
TOKEN_HEX = 48


def normalise(code: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def is_valid(code: str) -> bool:
    c = normalise(code)
    return len(c) == CODE_LEN and all(ch in ALPHABET for ch in c)


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))


def format_code(code: str) -> str:
    """K7M2P4QX9R -> K7M2-P4QX-9R, which is what the device displays."""
    c = normalise(code)
    return "-".join(filter(None, (c[0:4], c[4:8], c[8:12])))


def _h(prefix: str, code: str, n: int) -> str:
    return hashlib.sha256((prefix + code).encode("ascii")).hexdigest()[:n]


def derive(code: str) -> dict:
    """{'code', 'stream', 'push', 'read'} - or raises on a malformed code."""
    c = normalise(code)
    if not is_valid(c):
        raise ValueError(
            f"pairing code must be {CODE_LEN} characters from {ALPHABET} "
            "(dashes and case are ignored)")
    return {
        "code": c,
        "stream": _h("peek-stream:", c, STREAM_HEX),
        "push": _h("peek-push:", c, TOKEN_HEX),
        "read": _h("peek-read:", c, TOKEN_HEX),
    }


def urls(base: str, stream: str) -> dict:
    b = (base or "").rstrip("/")
    return {"ingest": f"{b}/ingest/{stream}", "telemetry": f"{b}/telemetry/{stream}"}


if __name__ == "__main__":
    import sys
    code = sys.argv[1] if len(sys.argv) > 1 else new_code()
    d = derive(code)
    print(f"code   {format_code(d['code'])}")
    print(f"stream {d['stream']}")
    print(f"push   {d['push']}")
    print(f"read   {d['read']}")
