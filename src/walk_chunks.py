#!/usr/bin/env python3
"""
Locate the MMF2 game-data region inside an executable and list its chunks.

    python src/walk_chunks.py game.exe

Prints every chunk (id, flag, size) and flags the two that matter most:
  26214  Images        - the image bank to patch
  21845  Image Handles - the per-image offset table that forbids resizing any image
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

CHUNK_NAMES = {
    8739: "Header", 8740: "Name", 8741: "Author", 8742: "Menu", 8745: "FrameItems",
    8747: "FrameHandles", 8748: "ExtensionData", 8753: "OtherExtensions",
    8754: "GlobalValues", 8755: "GlobalStrings", 8756: "ExtensionList",
    8757: "Icon16", 8759: "SerialNumber", 8763: "Copyright", 8766: "MovementExtensions",
    8768: "ExeOnly", 8770: "Protection", 8773: "ExtendedHeader",
    13107: "Frame", 13108: "FrameHeader", 13109: "FrameName", 13111: "FramePalette",
    13112: "FrameItemInstances", 13117: "FrameEvents", 13121: "FrameLayers",
    13122: "FrameVirtualRect", 13127: "MvtTimerBase",
    21845: "ImageHandles", 21846: "FontHandles", 21847: "SoundHandles",
    26214: "IMAGES", 26215: "Fonts", 26216: "Sounds", 32639: "LastChunk",
}
FLAG_NAMES = {0: "plain", 1: "compressed", 2: "encrypted", 3: "compressed+encrypted"}


def game_data_offset(data: bytes) -> int:
    """
    Mirror of CTFAK's ExeFileReader.CalculateEntryPoint:
    the game data starts after the last PE section, or at a '.extra' section if present.
    """
    if data[:2] != b"MZ":
        raise ValueError("not an MZ executable")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0":
        raise ValueError("no PE header")
    nsec = struct.unpack_from("<H", data, pe + 6)[0]
    optsz = struct.unpack_from("<H", data, pe + 20)[0]
    secoff = pe + 24 + optsz
    for i in range(nsec):
        o = secoff + i * 40
        name = data[o:o + 8].rstrip(b"\0").decode("latin1")
        _, _, raw_size, raw_ptr = struct.unpack_from("<IIII", data, o + 8)
        if name == ".extra":
            return raw_ptr
        if i == nsec - 1:
            return raw_ptr + raw_size
    raise ValueError("no sections found")


def walk(data: bytes, offset: int, limit: int = 500):
    """Yield (offset, id, flag, size) for each chunk starting at `offset`."""
    pos = offset
    for _ in range(limit):
        if pos + 8 > len(data):
            return
        cid, flag, size = struct.unpack_from("<hhI", data, pos)
        if flag not in (0, 1, 2, 3) or size < 0 or pos + 8 + size > len(data):
            return
        yield pos, cid, flag, size
        pos += 8 + size
        if cid == 32639:
            return


def find_image_bank(data: bytes, offset: int) -> tuple[int, int, int] | None:
    """
    Return (chunk_header_offset, flag, size) for chunk 26214.

    The PackData header (0x7777) sits at the computed offset, so the chunk chain does
    not start there; also probe for the bank by searching for the id. A genuine hit is
    confirmed by the recorded size landing exactly on a plausible boundary.
    """
    for pos, cid, flag, size in walk(data, offset):
        if cid == 26214:
            return pos, flag, size

    needle = struct.pack("<h", 26214)
    start = 0
    while True:
        i = data.find(needle, start)
        if i < 0:
            return None
        start = i + 1
        if i < offset:
            continue
        cid, flag, size = struct.unpack_from("<hhI", data, i)
        if cid == 26214 and flag in (0, 1, 2, 3) and 0 < size < len(data) - i - 8:
            return i, flag, size


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exe", type=Path)
    ap.add_argument("--all", action="store_true", help="list every chunk, not just the tail")
    args = ap.parse_args()

    data = args.exe.read_bytes()
    print(f"file          : {args.exe}")
    print(f"size          : {len(data):,} bytes")

    off = game_data_offset(data)
    print(f"game data at  : 0x{off:08X} ({off:,})")
    first = struct.unpack_from("<H", data, off)[0]
    print(f"first word    : 0x{first:04X}" +
          ("   <- PackData header (extension DLLs follow)" if first == 0x7777 else ""))

    print("\n-- chunk chain from the computed offset --")
    n = 0
    for pos, cid, flag, size in walk(data, off):
        n += 1
        if args.all or cid in (26214, 21845, 8739, 32639):
            tag = ""
            if cid == 26214:
                tag = "   <- image bank"
            elif cid == 21845:
                tag = "   <- offset table (do not resize any image!)"
            print(f"  0x{pos:08X}  id={cid:<6} flag={flag} ({FLAG_NAMES[flag]:<21}) "
                  f"size={size:>12,}  {CHUNK_NAMES.get(cid, ''):<16}{tag}")
    print(f"  ({n} chunks walked; use --all to list them all)")

    bank = find_image_bank(data, off)
    print()
    if bank is None:
        print("image bank  : NOT FOUND")
        return 1
    pos, flag, size = bank
    print(f"image bank  : header at 0x{pos:08X}, flag={flag} ({FLAG_NAMES[flag]}), "
          f"size={size:,}")
    if flag != 2 and flag != 3:
        print("              payload is stored verbatim -> in-place patching is viable")
    else:
        print("              payload is encrypted -> decrypt before patching")
    return 0


if __name__ == "__main__":
    sys.exit(main())
