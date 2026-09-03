# Prebuilt firmware

`PeekESP-merged.bin` is the compiled sketch, ready to write at offset `0x0`.
Flashing it needs **no ESP32 core, no libraries and no Arduino IDE**:

```bash
python ../tools/flash.py
```

It is a single image containing all four pieces the ESP32 expects:

| Offset | |
|---|---|
| `0x01000` | bootloader |
| `0x08000` | partition table |
| `0x0e000` | `boot_app0` — which OTA slot to start |
| `0x10000` | the sketch |

Merged rather than shipped as four files so there is one thing to download and
one offset to get right.

## Rebuilding it

**This does not update itself.** After changing `PeekESP/PeekESP.ino`, run:

```bash
python tools/export_firmware.py
```

or the committed image stays the previous firmware while the source says
otherwise — which is a confusing way to spend an evening.

Built for **LilyGo T-Display**, partition scheme **Huge APP (3MB No OTA/1MB
SPIFFS)**. Flashing it onto a board with a different partition layout will not
boot.
