#!/usr/bin/env python3
"""
Write redrawn images back into an MMF2 executable, in place.

    python src/patch.py --exe game.exe --bank dump/bank.bin --images redrawn/ -o game_zh.exe

`redrawn/` must contain PNGs named `<handle>.png`, matching what extract.py produced.
Only the images you actually changed need to be present; everything else is copied
through untouched.

The hard rule (see docs/FORMAT.md section 5): the game keeps a per-image offset table,
so EVERY image must keep its exact original compressed size. This tool enforces that by
compressing to fit and, when necessary, quantising the palette step by step. If an image
still will not fit it is left unchanged and reported. The executable's total size never
changes.
"""
from __future__ import annotations

import argparse
import struct
import sys
import zlib
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imagebank  # noqa: E402
import walk_chunks  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", type=Path, required=True)
    ap.add_argument("--bank", type=Path, required=True,
                    help="bank.bin produced by extract.py")
    ap.add_argument("--images", type=Path, required=True,
                    help="directory of redrawn PNGs named <handle>.png")
    ap.add_argument("-o", "--out", type=Path, required=True)
    args = ap.parse_args()

    exe = args.exe.read_bytes()
    bank = args.bank.read_bytes()
    recs = imagebank.parse(bank)
    by_handle = {r.tight_handle: r for r in recs}
    print(f"executable : {args.exe}  ({len(exe):,} bytes)")
    print(f"bank       : {len(recs):,} images, {len(bank):,} bytes")

    redrawn = {}
    for p in sorted(args.images.glob("*.png")):
        try:
            redrawn[int(p.stem)] = p
        except ValueError:
            print(f"  skipping {p.name}: filename is not a handle")
    print(f"redrawn    : {len(redrawn):,} candidate PNGs")

    patched = quantised = 0
    skipped: list[tuple[int, str]] = []
    for handle, path in redrawn.items():
        rec = by_handle.get(handle)
        if rec is None:
            skipped.append((handle, "no such handle in this bank"))
            continue
        img = Image.open(path).convert("RGBA")
        if img.size != (rec.width, rec.height):
            skipped.append((handle, f"size {img.size[0]}x{img.size[1]} != "
                                    f"{rec.width}x{rec.height}"))
            continue
        rgba = np.array(img)
        try:
            payload = rec.encode(rgba)
        except ValueError as exc:
            skipped.append((handle, str(exc)))
            continue
        if len(payload) != len(rec.payload):
            skipped.append((handle, "payload length changed"))
            continue

        before = rec.compressed
        blob = imagebank.compress_fitting(
            payload, rec.comp_size, rgba=rgba, transparent=rec.transparent_rgb,
            w=rec.width, h=rec.height, data_size=rec.data_size, flags=rec.flags)
        if blob is None or len(blob) != rec.comp_size:
            skipped.append((handle, f"won't fit in {rec.comp_size} bytes"))
            continue
        if zlib.decompress(blob) != payload:
            skipped.append((handle, "recompressed payload does not decode back"))
            continue
        if blob[:2] != before[:2]:
            quantised += 1
        rec.compressed = blob
        rec.payload = payload
        patched += 1

    print(f"\npatched    : {patched:,} images ({quantised:,} needed palette quantisation)")
    if skipped:
        print(f"left as-is : {len(skipped):,}")
        for h, why in skipped[:10]:
            print(f"   h{h}: {why}")

    new_bank = imagebank.serialise(recs)
    if len(new_bank) != len(bank):
        print(f"\nFATAL: bank size would change {len(bank):,} -> {len(new_bank):,}")
        return 2

    found = walk_chunks.find_image_bank(exe, walk_chunks.game_data_offset(exe))
    if found is None:
        print("\nFATAL: image bank chunk not found in the executable")
        return 2
    pos, flag, size = found
    if size != len(bank):
        print(f"\nFATAL: chunk size {size:,} != bank file {len(bank):,}")
        return 2

    out = (exe[:pos]
           + struct.pack("<hhI", 26214, flag, len(new_bank)) + new_bank
           + exe[pos + 8 + size:])
    if len(out) != len(exe):
        print(f"\nFATAL: exe size would change {len(exe):,} -> {len(out):,}")
        return 2

    args.out.write_bytes(out)
    print(f"\nexe size unchanged: {len(out):,}")
    print(f"written -> {args.out}")
    print("\nNow run:  python src/verify.py " + str(args.exe) + " " + str(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
