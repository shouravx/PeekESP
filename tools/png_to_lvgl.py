#!/usr/bin/env python3
"""
png_to_lvgl.py - turn a PNG into an LVGL image you can compile into the sketch.

    python tools/png_to_lvgl.py img/logo.png PeekESP/logo_splash.h \
        --name logo_splash --size 96 --bg 05070E

Why not LVGL's online converter: this pins the choices that actually matter
here - compositing transparency onto the exact theme background, and matching
the byte order the firmware's LV_COLOR_16_SWAP setting expects - and it can be
re-run when the artwork changes instead of being a one-off download.

Output is LV_IMG_CF_TRUE_COLOR: 2 bytes per pixel, RGB565. Alpha is flattened
against --bg rather than kept, because a TRUE_COLOR_ALPHA image costs 3 bytes
per pixel and blends every frame, and the splash only ever sits on one colour.
"""

import argparse
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("needs Pillow:  pip install pillow")


def rgb565(r, g, b):
    return ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)


def main():
    ap = argparse.ArgumentParser(description="PNG -> LVGL C header")
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--name", default="img_asset", help="C identifier")
    ap.add_argument("--size", type=int, default=96, help="fit inside this square")
    ap.add_argument("--bg", default="05070E", help="background hex for flattening alpha")
    ap.add_argument("--swap", action="store_true",
                    help="emit big-endian pixels, for LV_COLOR_16_SWAP=1")
    a = ap.parse_args()

    bg = tuple(int(a.bg[i:i + 2], 16) for i in (0, 2, 4))

    im = Image.open(a.src).convert("RGBA")
    im.thumbnail((a.size, a.size), Image.LANCZOS)

    flat = Image.new("RGB", im.size, bg)
    flat.paste(im, (0, 0), im)          # alpha composited onto the theme colour

    w, h = flat.size
    px = flat.load()

    body = []
    for y in range(h):
        row = []
        for x in range(w):
            c = rgb565(*px[x, y])
            # LVGL reads these straight into lv_color_t, so the byte order has
            # to match the build. Getting it wrong shows as swapped red/blue
            # rather than an error.
            row.append((c >> 8, c & 0xFF) if a.swap else (c & 0xFF, c >> 8))
        body.append(", ".join(f"0x{lo:02x}, 0x{hi:02x}" for lo, hi in row))

    order = "big-endian (LV_COLOR_16_SWAP=1)" if a.swap else "little-endian (LV_COLOR_16_SWAP=0)"
    with open(a.dst, "w", newline="\n") as fh:
        fh.write(f"""/**
 * {a.dst.split('/')[-1]} - generated, do not edit by hand.
 *
 *   python tools/png_to_lvgl.py {a.src} {a.dst} \\
 *       --name {a.name} --size {a.size} --bg {a.bg}{' --swap' if a.swap else ''}
 *
 * {w}x{h} RGB565, {w * h * 2} bytes of flash. Transparency is already
 * flattened onto #{a.bg.upper()}, so this only looks right on that background.
 * Pixel order is {order}.
 */
#pragma once

#include <lvgl.h>

static const uint8_t {a.name}_map[] = {{
""")
        for row in body:
            fh.write("    " + row + ",\n")
        fh.write(f"""}};

static const lv_img_dsc_t {a.name} = {{
    .header = {{
        .cf = LV_IMG_CF_TRUE_COLOR,
        .always_zero = 0,
        .reserved = 0,
        .w = {w},
        .h = {h},
    }},
    .data_size = sizeof({a.name}_map),
    .data = {a.name}_map,
}};
""")

    print(f"{a.dst}: {w}x{h}, {w * h * 2} bytes ({w * h * 2 / 1024:.1f} KB of flash)")


if __name__ == "__main__":
    main()
