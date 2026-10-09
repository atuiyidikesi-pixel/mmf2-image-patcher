#!/usr/bin/env python3
"""
Find the images that actually contain text, so you know how much work a translation is.

    python src/inventory.py dump/ -o inventory/

Groups the extracted images by pixel size, de-duplicates them by content, and ranks the
sizes by how "text-like" they are: wide and flat, repeated many times (a label reused
across screens), or very small (a font glyph). It also writes a contact sheet per size so
you can confirm by eye instead of trusting the heuristic.

Output:
    inventory/catalog.json     every size group with its unique images and where they occur
    inventory/<W>x<H>.png      contact sheet for that size group
    inventory/summary.txt      human-readable ranking
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    FONT = ImageFont.truetype("consola.ttf", 13)
except OSError:
    FONT = ImageFont.load_default()


def digest(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def score(w: int, h: int, instances: int, unique: int) -> float:
    """
    Rough text-likelihood. Only a hint - always confirm on the contact sheet.

    Note that extract.py dumps one PNG per *bank record*, so a label reused on twenty
    screens still appears once; `unique` is the meaningful count, not `instances`.
    """
    if h == 0 or w == 0:
        return 0.0
    s = 0.0
    ratio = w / h
    if 3.0 <= ratio <= 14:            # wide and flat: text strips, buttons, name bars
        s += 2.0
    if h <= 30:                       # short: one or two lines of glyphs
        s += 1.5
    if w <= 12 and h <= 12:           # a font glyph
        s += 1.5
    if 60 <= w <= 460:                # big enough to hold a sentence
        s += 0.5
    s += min(unique, 150) / 150.0 * 2.0   # a real text set has many distinct strings
    return s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dump", type=Path, help="directory produced by extract.py")
    ap.add_argument("-o", "--out", type=Path, default=Path("inventory"))
    ap.add_argument("--min-instances", type=int, default=4,
                    help="ignore size groups rarer than this (default 4)")
    args = ap.parse_args()

    files = sorted(args.dump.glob("*.png"))
    if not files:
        print(f"error: no PNGs in {args.dump}")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"scanning {len(files):,} images ...")

    by_size: dict[tuple[int, int], list[Path]] = collections.defaultdict(list)
    for p in files:
        try:
            with Image.open(p) as im:
                by_size[im.size].append(p)
        except Exception:
            continue

    catalog = {}
    rows = []
    for size, paths in by_size.items():
        if len(paths) < args.min_instances:
            continue
        buckets: dict[str, list[Path]] = collections.OrderedDict()
        for p in paths:
            buckets.setdefault(digest(p), []).append(p)
        entries = []
        for i, (h, ps) in enumerate(sorted(buckets.items(), key=lambda kv: -len(kv[1]))):
            entries.append({
                "idx": i, "md5": h, "occurrences": len(ps),
                "sample": str(ps[0].relative_to(args.dump)),
                "handles": sorted(int(x.stem) for x in ps),
            })
        key = f"{size[0]}x{size[1]}"
        catalog[key] = {"width": size[0], "height": size[1],
                        "instances": len(paths), "unique": len(entries),
                        "entries": entries}
        rows.append((score(size[0], size[1], len(paths), len(entries)),
                     size, len(paths), len(entries)))

    rows.sort(reverse=True)
    lines = ["score   size      instances  unique   (higher score = more likely to be text)",
             "-" * 70]
    for s, size, inst, uniq in rows:
        lines.append(f"{s:5.1f}   {size[0]:>4}x{size[1]:<4} {inst:>8}  {uniq:>6}")
    summary = "\n".join(lines) + "\n"
    (args.out / "summary.txt").write_text(summary, encoding="utf-8")
    (args.out / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
    print(summary)

    # contact sheets for the top candidates
    for s, size, inst, uniq in rows[:8]:
        if s < 2.0:
            break
        entries = catalog[f"{size[0]}x{size[1]}"]["entries"]
        picks = entries[:24]
        w, h = size
        cols = 1 if w > 200 else (2 if w > 90 else 4)
        z = 4 if w <= 40 else (3 if w <= 140 else 1)
        cw, ch = w * z + 6, h * z + 6
        nrows = (len(picks) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cw + 150, nrows * ch), (24, 26, 38))
        d = ImageDraw.Draw(sheet)
        for i, e in enumerate(picks):
            r, c = divmod(i, cols)
            im = Image.open(args.dump / e["sample"]).convert("RGBA")
            bg = Image.new("RGBA", im.size, (24, 26, 38, 255))
            im = Image.alpha_composite(bg, im).convert("RGB").resize((w * z, h * z), Image.NEAREST)
            sheet.paste(im, (c * cw + 2, r * ch + 2))
            d.text((cols * cw + 6, r * ch + 6), f"{e['idx']:03d} x{e['occurrences']}",
                   fill=(150, 220, 255), font=FONT)
        sheet.save(args.out / f"{size[0]}x{size[1]}.png")

    print(f"written -> {args.out}/catalog.json, summary.txt and contact sheets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
