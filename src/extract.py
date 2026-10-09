#!/usr/bin/env python3
"""
Extract every image from an MMF2 executable — no CTFAK required.

    python src/extract.py game.exe -o dump/

Writes:
    dump/<handle>.png     one PNG per image, named by its logical handle
    dump/bank.bin         the raw ImageBank chunk payload (feed this to patch.py)
    dump/index.json       per-image metadata (size, flags, offsets)

The handle in the filename is the LOGICAL handle (what CTFAK calls it). The value stored
on disk is one higher for build >= 284; see docs/FORMAT.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imagebank  # noqa: E402
import walk_chunks  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("exe", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--no-png", action="store_true", help="only dump bank.bin and index.json")
    args = ap.parse_args()

    data = args.exe.read_bytes()
    found = walk_chunks.find_image_bank(data, walk_chunks.game_data_offset(data))
    if found is None:
        print("error: no ImageBank chunk (26214) found in this executable")
        return 2
    pos, flag, size = found
    print(f"executable   : {args.exe}  ({len(data):,} bytes)")
    print(f"image bank   : chunk at 0x{pos:08X}, flag={flag}, size={size:,}")

    bank = data[pos + 8:pos + 8 + size]
    if flag != 0:
        print(f"error: bank flag is {flag}; payload is encrypted and/or compressed.")
        print("       decrypt it first (see docs/FORMAT.md section 3).")
        return 2

    recs = imagebank.parse(bank)
    print(f"images       : {len(recs):,}")

    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "bank.bin").write_bytes(bank)

    index = []
    ok = failed = 0
    for r in recs:
        entry = {
            "handle": r.tight_handle,
            "width": r.width, "height": r.height,
            "graphic_mode": r.graphic_mode, "flags": r.flags,
            "data_size": r.data_size, "comp_size": r.comp_size,
        }
        try:
            rgba = r.to_rgba()
        except ValueError as exc:
            entry["error"] = str(exc)
            index.append(entry)
            failed += 1
            continue
        index.append(entry)
        ok += 1
        if not args.no_png:
            Image.fromarray(rgba, "RGBA").save(args.out / f"{r.tight_handle}.png")

    (args.out / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"decoded      : {ok:,} ok, {failed:,} failed")
    print(f"written      : {args.out}/bank.bin, index.json"
          + ("" if args.no_png else f", {ok:,} PNGs"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
