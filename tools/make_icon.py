#!/usr/bin/env python3
"""
make_icon.py - build a multi-resolution Windows .ico from the logo.

    python tools/make_icon.py img/logo.png img/logo.ico

A .ico is a container, and Windows picks the size it needs from inside it:
16 for the title bar and tray, 32 for the taskbar and small Explorer views,
48 for medium icons, 256 for the extra-large view and the Alt-Tab card. An
icon holding only 32x32 is not "small" - it is *missing* every other size, so
Windows upscales that one bitmap and the result is visibly blocky.

Sizes are rendered from the full-resolution PNG rather than from each other,
so the 16px version is a proper downsample of the original rather than a
shrunken 32px one.
"""

import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("needs Pillow:  pip install pillow")

# What Windows actually asks for, smallest first.
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def main():
    if len(sys.argv) != 3:
        sys.exit("usage: make_icon.py <source.png> <out.ico>")
    src, dst = sys.argv[1], sys.argv[2]

    im = Image.open(src).convert("RGBA")
    if im.width != im.height:
        side = max(im.size)
        square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        square.paste(im, ((side - im.width) // 2, (side - im.height) // 2), im)
        im = square

    frames = [im.resize((s, s), Image.LANCZOS) for s in SIZES]
    # Pillow writes every frame given in sizes=; passing the pre-rendered list
    # as append_images keeps each one a downsample of the original.
    frames[-1].save(dst, format="ICO",
                    sizes=[(s, s) for s in SIZES],
                    append_images=frames[:-1])

    check = Image.open(dst)
    got = sorted(check.info.get("sizes", []))
    print(f"{dst}: {len(got)} sizes {[w for w, _ in got]}")
    missing = [s for s in SIZES if (s, s) not in got]
    if missing:
        sys.exit(f"expected sizes missing from the icon: {missing}")


if __name__ == "__main__":
    main()
