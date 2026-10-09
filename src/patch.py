#!/usr/bin/env python3
"""
Write redrawn images back into an MMF2 executable, in place.

Hard rule (see docs/FORMAT.md section 5): the game holds a per-image offset table
(chunk 21845), so EVERY image must keep its exact original compressed size. This tool
enforces that: it compresses to fit, quantises the palette if needed, and if an image
still will not fit it is left untouched. The executable's total size never changes.

    python src/patch.py --exe game.exe --bank bank.bin --map handles.json \
                        --images ./redrawn --out game_zh.exe

`--map` is a JSON object {"<md5 of the original dumped PNG>": <handle>} so the tool can
tell which bank record each redrawn file belongs to. Build it by hashing CTFAK's
handle-keyed dump (see docs/SETUP.md section 5).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import imagebank  # noqa: E402
import walk_chunks  # noqa: E402


def md5_png(path: Path) -> str:
    return hashlib.md5(Image.open(path).convert("RGBA").tobytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--exe", type=Path, required=True)
    ap.add_argument("--bank", type=Path, required=True,
                    help="decrypted chunk 26214 payload (CTFAK_DUMP_CHUNKS)")
    ap.add_argument("--map", type=Path, required=True,
                    help='JSON {"<md5 of original PNG>": handle}')
    ap.add_argument("--images", type=Path, required=True,
                    help="directory of redrawn PNGs")
    ap.add_argument("--original-dumps", type=Path,
                    help="directory of the ORIGINAL handle-keyed PNGs; used with --map "
                         "to resolve handle -> redrawn file. Optional if --map maps to "
                         "relative filenames instead of handles.")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    exe = args.exe.read_bytes()
    bank_bytes = args.bank.read_bytes()
    records = imagebank.parse(bank_bytes)
    print(f"bank: {len(records):,} images, {len(bank_bytes):,} bytes")

    # ---- work out which redrawn file belongs to which handle ----------------
    h2h = {k: int(v) for k, v in json.loads(args.map.read_text(encoding="utf-8")).items()}
    redrawn = {md5_png(p): p for p in args.images.glob("*.png")}
    print(f"redrawn files: {len(redrawn):,}")

    handle2file: dict[int, Path] = {}
    if args.original_dumps:
        for p in args.original_dumps.glob("*.png"):
            h = h2h.get(md5_png(p))
            if h is None:
                continue
            rec = next((r for r in records if r.tight_handle == h), None)
            if rec is None:
                continue
            # the redrawn file is the one whose *original* counterpart we can identify:
            # match by the file naming convention used by the inventory step
            handle2file.setdefault(h, args.images / p.name)
    else:
        for digest, handle in h2h.items():
            cand = redrawn.get(digest)
            if cand is not None:
                handle2file.setdefault(handle, cand)

    handle2file = {h: f for h, f in handle2file.items() if f.exists()}
    print(f"resolved {len(handle2file):,} handle -> redrawn file mappings")

    # ---- rebuild, preserving every compressed size --------------------------
    patched = quantised = 0
    skipped: list[tuple[int, str]] = []
    for rec in records:
        target = handle2file.get(rec.tight_handle)
        if target is None:
            continue
        try:
            rgba = np.array(Image.open(target).convert("RGBA"))
        except Exception as exc:                                   # noqa: BLE001
            skipped.append((rec.tight_handle, f"unreadable: {exc}"))
            continue
        if rgba.shape[1] != rec.width or rgba.shape[0] != rec.height:
            skipped.append((rec.tight_handle,
                            f"size {rgba.shape[1]}x{rgba.shape[0]} != {rec.width}x{rec.height}"))
            continue
        payload = rec.encode(rgba)
        if len(payload) != len(rec.payload):
            skipped.append((rec.tight_handle, "payload length changed"))
            continue

        blob = imagebank.compress_fitting(
            payload, rec.comp_size, rgba=rgba, transparent=rec.transparent,
            w=rec.width, h=rec.height, data_size=rec.data_size, flags=rec.flags)
        if blob is None:
            skipped.append((rec.tight_handle, f"won't fit in {rec.comp_size} bytes"))
            continue
        if len(blob) != rec.comp_size:
            skipped.append((rec.tight_handle, "internal size error"))
            continue
        if blob[:2] != rec.compressed[:2]:
            quantised += 1
        rec.compressed = blob
        rec.payload = payload
        patched += 1

    print(f"\npatched {patched:,} images ({quantised:,} needed palette quantisation)")
    if skipped:
        print(f"left untouched: {len(skipped):,}")
        for h, why in skipped[:10]:
            print(f"   h{h}: {why}")

    # ---- splice -------------------------------------------------------------
    new_bank = imagebank.serialise(records)
    if len(new_bank) != len(bank_bytes):
        print(f"FATAL: bank size changed {len(bank_bytes):,} -> {len(new_bank):,}")
        return 2

    found = walk_chunks.find_image_bank(exe, walk_chunks.game_data_offset(exe))
    if found is None:
        print("FATAL: image bank chunk not found in the executable")
        return 2
    pos, flag, size = found
    if size != len(bank_bytes):
        print(f"FATAL: chunk size {size:,} != bank file {len(bank_bytes):,}")
        return 2

    new_chunk = struct.pack("<hhI", 26214, flag, len(new_bank)) + new_bank
    out = exe[:pos] + new_chunk + exe[pos + 8 + size:]
    if len(out) != len(exe):
        print(f"FATAL: exe size changed {len(exe):,} -> {len(out):,}")
        return 2

    args.out.write_bytes(out)
    print(f"\nexe size unchanged: {len(out):,}")
    print(f"written -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
