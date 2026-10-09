#!/usr/bin/env python3
"""
Byte-level regression check between an original and a patched executable.

    python src/verify.py game.exe game_zh.exe

Decodes the ImageBank out of both files and reports exactly which images changed and
whether anything else moved. Use this instead of "the game still boots": a patch that
shifts image offsets can boot fine and render a black screen, and a patch that writes the
wrong handle changes an image you never meant to touch.

Exit code 0 when the two banks are structurally identical, 1 otherwise.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imagebank  # noqa: E402
import walk_chunks  # noqa: E402


def load(exe: Path):
    data = exe.read_bytes()
    found = walk_chunks.find_image_bank(data, walk_chunks.game_data_offset(data))
    if found is None:
        raise SystemExit(f"{exe}: no ImageBank chunk found")
    pos, flag, size = found
    bank = data[pos + 8:pos + 8 + size]
    return len(data), pos, flag, size, imagebank.parse(bank)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original", type=Path)
    ap.add_argument("patched", type=Path)
    ap.add_argument("--dump-changed", type=Path,
                    help="write changed images here as <handle>_before.png / _after.png")
    args = ap.parse_args()

    o_len, o_pos, o_flag, o_size, o_recs = load(args.original)
    p_len, p_pos, p_flag, p_size, p_recs = load(args.patched)

    print(f"original : {args.original}  ({o_len:,} bytes)")
    print(f"patched  : {args.patched}  ({p_len:,} bytes)")
    print()
    problems = []
    if o_len != p_len:
        problems.append(f"executable size changed: {o_len:,} -> {p_len:,}")
    if o_size != p_size:
        problems.append(f"bank size changed: {o_size:,} -> {p_size:,}  "
                        f"(this shifts every later image and breaks the offset table)")
    if o_pos != p_pos:
        problems.append(f"bank offset moved: 0x{o_pos:08X} -> 0x{p_pos:08X}")
    if len(o_recs) != len(p_recs):
        problems.append(f"image count changed: {len(o_recs):,} -> {len(p_recs):,}")

    if problems:
        print("STRUCTURAL PROBLEMS (these will break the game):")
        for p in problems:
            print("  ! " + p)
        print()

    changed = []
    n = min(len(o_recs), len(p_recs))
    for i in range(n):
        a, b = o_recs[i], p_recs[i]
        if a.tight_handle != b.tight_handle:
            problems.append(f"record {i} handle changed {a.tight_handle} -> {b.tight_handle}")
            continue
        if a.payload != b.payload:
            changed.append(b.tight_handle)

    print(f"images            : {len(o_recs):,}")
    print(f"payloads changed  : {len(changed):,}")
    print(f"payloads identical: {len(o_recs) - len(changed):,}")

    same_size = sum(1 for i in range(n) if o_recs[i].comp_size == p_recs[i].comp_size)
    print(f"comp_size preserved: {same_size:,} / {n:,}   "
          f"{'(all images keep their original offset)' if same_size == n else '(SOME IMAGES RESIZED - game may render black)'}")

    if args.dump_changed and changed:
        args.dump_changed.mkdir(parents=True, exist_ok=True)
        for h in changed:
            for tag, recs in (("before", o_recs), ("after", p_recs)):
                r = next(x for x in recs if x.tight_handle == h)
                try:
                    Image.fromarray(r.to_rgba(), "RGBA").save(
                        args.dump_changed / f"{h}_{tag}.png")
                except ValueError:
                    pass
        print(f"changed images -> {args.dump_changed}")

    if problems or same_size != n:
        print("\nRESULT: FAIL")
        return 1
    print("\nRESULT: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
