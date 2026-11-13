"""CLI:  python -m handwriter "Dear Sam, ..." -o letter.png
       python -m handwriter -f note.txt --page a5 --gcode note.gcode
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import INKS, PENS, Style
from .gcode import Plotter, page_paths, preview, to_gcode
from .layout import Writer
from .render import compose

FONT_DIR = Path(__file__).resolve().parent.parent / "fonts"

PAGES_MM = {
    "letter": (215.9, 279.4),
    "a4": (210.0, 297.0),
    "a5": (148.0, 210.0),
    "a6": (105.0, 148.0),
    "index": (127.0, 76.2),  # 5x3 index card
    "postcard": (152.4, 101.6),  # 6x4
}


def _page_size(s: str) -> tuple[float, float]:
    s = s.lower()
    if s in PAGES_MM:
        return PAGES_MM[s]
    m = re.fullmatch(r"(\d+(?:\.\d+)?)x(\d+(?:\.\d+)?)", s)
    if not m:
        raise argparse.ArgumentTypeError(f"page must be one of {', '.join(PAGES_MM)} or WxH in mm, e.g. 200x200")
    return float(m.group(1)), float(m.group(2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="handwriter", description="Generate realistic, imperfect handwriting.")
    p.add_argument("text", nargs="?", help="text to write (or use -f / stdin)")
    p.add_argument("-f", "--file", help="read text from a file")
    p.add_argument("-o", "--out", help="output .png (one file per page) or .pdf  [default out.png unless --gcode]")
    p.add_argument("--font", default="Caveat", help="font name in fonts/ or path to a .ttf/.otf")
    p.add_argument("--seed", type=int, help="random seed (same seed = same writer and page)")
    p.add_argument("--page", type=_page_size, default="letter", help="letter|a4|a5|a6|index|postcard or WxH mm")
    p.add_argument("--paper", choices=["lined", "blank", "grid"], default="lined")
    p.add_argument("--ink", choices=list(INKS), default="blue")
    p.add_argument("--pen", choices=list(PENS), default="ballpoint", help="instrument physics")
    p.add_argument("--pen-defects", type=float, default=1.0, help="0 = flawless pen, 1 = normal, 2 = cheap pen")
    p.add_argument("--habits", type=float, default=1.0, help="strength of the writer's personal quirks")
    p.add_argument("--messiness", type=float, default=1.0, help="0 = font-perfect, 1 = normal, 2 = rushed")
    p.add_argument("--fatigue", type=float, default=0.25, help="how much worse it gets down the page")
    p.add_argument("--mistakes", type=float, default=0.0, help="chance per word of a crossed-out typo (off by default)")
    p.add_argument("--slant", type=float, default=4.0, help="degrees, positive leans right")
    p.add_argument("--size", type=float, default=2.4, help="x-height in mm")
    p.add_argument("--line-spacing", type=float, default=7.1, help="mm between lines")
    p.add_argument("--margin", type=float, help="left margin in mm (default scales with page)")
    p.add_argument("--weight", type=float, default=0.0, help="-0.2 thin .. 0.2 thick")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--no-scan", action="store_true", help="skip page tilt/lighting/grain")
    p.add_argument("--list-fonts", action="store_true")

    g = p.add_argument_group("pen plotter / G-code (Creality Ender 3 etc.)")
    g.add_argument("--gcode", help="write pen-plotter G-code to this file (+ a _preview.png)")
    g.add_argument("--bed", type=_page_size, default="220x220", help="printable bed size WxH mm [220x220]")
    g.add_argument("--offset", type=_page_size, default="0x0", help="page bottom-left corner on the bed, XxY mm")
    g.add_argument("--pen-down-z", type=float, default=0.0, help="Z where the pen touches the paper")
    g.add_argument("--pen-up-z", type=float, default=3.0, help="Z for travel moves")
    g.add_argument("--draw-speed", type=float, default=1500, help="mm/min while drawing")
    g.add_argument("--travel-speed", type=float, default=4000, help="mm/min pen-up moves")
    g.add_argument("--no-home", action="store_true", help="don't send G28")
    a = p.parse_args(argv)
    if isinstance(a.page, str):
        a.page = _page_size(a.page)
    for k in ("bed", "offset"):
        if isinstance(getattr(a, k), str):
            setattr(a, k, _page_size(getattr(a, k)))

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

    pw, ph = a.page
    k = min(1.0, pw / 215.9)  # scale default margins down on small pages
    left = (a.margin / 25.4) if a.margin is not None else 1.25 * k
    margins = (left, 0.6 * k, 1.0 * min(1.0, ph / 279.4), 0.7 * min(1.0, ph / 279.4))

    st = Style(
        font=font, seed=a.seed, paper=a.paper, ink=a.ink, messiness=a.messiness, fatigue=a.fatigue,
        mistakes=a.mistakes, slant_deg=a.slant, x_height_mm=a.size, line_spacing_mm=a.line_spacing,
        weight=a.weight, dpi=a.dpi, pen=a.pen, pen_defects=a.pen_defects, habits=a.habits, scan=not a.no_scan,
        page_inches=(pw / 25.4, ph / 25.4), margins_in=margins,
    )
    writer = Writer(st)
    pages = writer.write(text)

    if a.gcode:
        pl = Plotter(bed_x=a.bed[0], bed_y=a.bed[1], offset_x=a.offset[0], offset_y=a.offset[1],
                     pen_down_z=a.pen_down_z, pen_up_z=a.pen_up_z, draw_speed=a.draw_speed,
                     travel_speed=a.travel_speed, home=not a.no_home)
        gpath = Path(a.gcode)
        for i, pg in enumerate(pages):
            try:
                code, bed_paths = to_gcode(page_paths(pg, st.dpi), pw, ph, pl)
            except ValueError as e:
                p.error(str(e))
            suffix = "" if i == 0 else f"_{i + 1}"
            out = gpath.with_name(f"{gpath.stem}{suffix}{gpath.suffix or '.gcode'}")
            out.write_text(code)
            prev = out.with_name(f"{out.stem}_preview.png")
            preview(bed_paths, pw, ph, pl).save(prev)
            print(out, "|", code.splitlines()[3].lstrip("; "))
            print(prev)

    if a.out or not a.gcode:
        images = [compose(pg, writer, writer.rng) for pg in pages]
        out = Path(a.out or "out.png")
        if out.suffix.lower() == ".pdf":
            images[0].save(out, save_all=True, append_images=images[1:], resolution=a.dpi)
            print(out)
        else:
            for i, im in enumerate(images):
                path = out if i == 0 else out.with_name(f"{out.stem}_{i + 1}{out.suffix}")
                im.save(path, dpi=(a.dpi, a.dpi))
                print(path)


if __name__ == "__main__":
    main()
