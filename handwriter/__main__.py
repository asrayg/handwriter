"""CLI:  python -m handwriter "Dear Sam, ..." -o letter.png"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import INKS, Style, generate

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"


def main(argv=None):
    p = argparse.ArgumentParser(prog="handwriter", description="Generate realistic, imperfect handwriting.")
    p.add_argument("text", nargs="?", help="text to write (or use -f / stdin)")
    p.add_argument("-f", "--file", help="read text from a file")
    p.add_argument("-o", "--out", default="out.png", help="output .png (one file per page) or .pdf")
    p.add_argument("--font", default="Caveat", help="font name in fonts/ or path to a .ttf/.otf")
    p.add_argument("--seed", type=int, help="random seed (same seed = same page)")
    p.add_argument("--paper", choices=["lined", "blank", "grid"], default="lined")
    p.add_argument("--ink", choices=list(INKS), default="blue")
    p.add_argument("--messiness", type=float, default=1.0, help="0 = font-perfect, 1 = normal, 2 = rushed")
    p.add_argument("--fatigue", type=float, default=0.6, help="how much worse it gets down the page")
    p.add_argument("--mistakes", type=float, default=0.012, help="chance per word of a crossed-out typo")
    p.add_argument("--slant", type=float, default=4.0, help="degrees, positive leans right")
    p.add_argument("--size", type=float, default=2.4, help="x-height in mm")
    p.add_argument("--line-spacing", type=float, default=7.1, help="mm between ruled lines")
    p.add_argument("--weight", type=float, default=0.0, help="-0.2 thin .. 0.2 thick")
    p.add_argument("--dpi", type=int, default=200)
    p.add_argument("--no-scan", action="store_true", help="skip page tilt/lighting/grain")
    p.add_argument("--list-fonts", action="store_true")
    a = p.parse_args(argv)

    if a.list_fonts:
        for f in sorted(FONT_DIR.glob("*.[ot]tf")):
            print(f.stem)
        return

    text = a.text
    if a.file:
        text = Path(a.file).read_text()
    elif text is None and not sys.stdin.isatty():
        text = sys.stdin.read()
    if not text:
        p.error("no text given")

    font = a.font
    if not Path(font).exists():
        cands = list(FONT_DIR.glob(f"{font}.*tf"))
        if not cands:
            p.error(f"font '{font}' not found; try --list-fonts or pass a path")
        font = str(cands[0])

    st = Style(
        font=font, seed=a.seed, paper=a.paper, ink=a.ink, messiness=a.messiness, fatigue=a.fatigue,
        mistakes=a.mistakes, slant_deg=a.slant, x_height_mm=a.size, line_spacing_mm=a.line_spacing,
        weight=a.weight, dpi=a.dpi, scan=not a.no_scan,
    )
    pages = generate(text, st)

    out = Path(a.out)
    if out.suffix.lower() == ".pdf":
        pages[0].save(out, save_all=True, append_images=pages[1:], resolution=a.dpi)
        print(out)
    else:
        for i, im in enumerate(pages):
            path = out if i == 0 else out.with_name(f"{out.stem}_{i + 1}{out.suffix}")
            im.save(path, dpi=(a.dpi, a.dpi))
            print(path)


if __name__ == "__main__":
    main()
