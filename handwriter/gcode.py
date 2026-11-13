"""Turn a laid-out page into pen-plotter G-code (Marlin, e.g. Creality Ender 3 with a pen holder).

A pen draws lines, not filled shapes, so every glyph outline is rasterised, thinned to its
centreline (skeletonize), traced into polylines, pruned of skeleton spurs, smoothed and
simplified. All the human variation from the layout (habit shapes, drift, slant, i-dots...)
is kept; the physical pen supplies the ink texture itself.
"""
from __future__ import annotations

import hashlib
import zipfile
from dataclasses import dataclass, replace

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import convolve
from skimage.morphology import skeletonize

from .layout import Glyph, Page, Stroke


@dataclass
class Plotter:
    name: str = "ender3"
    bed_x: float = 220.0  # printable area, mm
    bed_y: float = 220.0
    offset_x: float = 0.0  # where the page's bottom-left corner sits on the bed, mm
    offset_y: float = 0.0
    pen_down_z: float = 0.0  # Z where the pen just touches the paper (calibrate this!)
    pen_up_z: float = 3.0
    draw_speed: float = 1500.0  # mm/min
    travel_speed: float = 4000.0
    z_speed: float = 300.0  # Ender 3 Z axis is slow (~5 mm/s)
    home: bool = True
    flavor: str = "marlin"  # marlin | bambu
    # G-code point that is the physical plate centre. Bambu's P1/X1 profiles carry extruder_offset 0x2,
    # so a model centred on the plate is printed around G-code (128, 126): we centre pages there too,
    # which makes the printed postcard frame and the pen G-code line up exactly.
    center_x: float = 0.0
    center_y: float = 0.0
    # where the pen tip is relative to the nozzle (mm). Pen in front of the nozzle = negative Y.
    pen_dx: float = 0.0
    pen_dy: float = 0.0


PRINTERS = {
    "p1p": Plotter(name="Bambu Lab P1P", bed_x=256, bed_y=256, draw_speed=3000, travel_speed=12000, z_speed=1200,
                   flavor="bambu", center_x=128, center_y=126),
    "p1s": Plotter(name="Bambu Lab P1S", bed_x=256, bed_y=256, draw_speed=3000, travel_speed=12000, z_speed=1200,
                   flavor="bambu", center_x=128, center_y=126),
    "x1c": Plotter(name="Bambu Lab X1C", bed_x=256, bed_y=256, draw_speed=3000, travel_speed=12000, z_speed=1200,
                   flavor="bambu", center_x=128, center_y=126),
    "a1": Plotter(name="Bambu Lab A1", bed_x=256, bed_y=256, draw_speed=3000, travel_speed=12000, z_speed=1200,
                  flavor="bambu", center_x=128, center_y=128),
    "ender3": Plotter(name="Creality Ender 3", bed_x=220, bed_y=220, draw_speed=1500, travel_speed=4000, z_speed=300,
                      center_x=110, center_y=110),
}


def printer(key: str, **overrides) -> Plotter:
    return replace(PRINTERS[key], **{k: v for k, v in overrides.items() if v is not None})


_N8 = [(-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (-1, 1), (1, -1), (1, 1)]  # 4-neighbours first


def _trace(sk: np.ndarray) -> list[list[tuple[int, int]]]:
    """Trace a 1-px skeleton into polylines (pixel coords, row/col)."""
    pts = set(zip(*np.nonzero(sk)))
    if not pts:
        return []
    deg_img = convolve(sk.astype(np.uint8), np.array([[1, 1, 1], [1, 0, 1], [1, 1, 1]]), mode="constant") * sk
    deg = {p: int(deg_img[p]) for p in pts}

    def nbrs(p):
        return [(p[0] + dy, p[1] + dx) for dy, dx in _N8 if (p[0] + dy, p[1] + dx) in pts]

    seen: set = set()

    def walk(start, nxt):
        path, prev, cur = [start, nxt], start, nxt
        seen.add((start, nxt)); seen.add((nxt, start))
        while deg[cur] == 2:
            cands = [q for q in nbrs(cur) if q != prev and (cur, q) not in seen]
            if not cands:
                break
            q = cands[0]
            seen.add((cur, q)); seen.add((q, cur))
            path.append(q)
            prev, cur = cur, q
        return path

    paths = []
    nodes = [p for p in pts if deg[p] != 2]
    for n in nodes:
        if deg[n] == 0:
            paths.append([n])
            continue
        for nb in nbrs(n):
            if (n, nb) not in seen:
                paths.append(walk(n, nb))
    for p in pts:  # closed loops with no endpoints (e.g. a clean 'o')
        for nb in nbrs(p):
            if (p, nb) not in seen:
                paths.append(walk(p, nb))
    return paths


def _rdp(p: np.ndarray, eps: float) -> np.ndarray:
    if len(p) < 3:
        return p
    a, b = p[0], p[-1]
    ab = b - a
    n = np.linalg.norm(ab)
    d = np.abs(np.cross(ab, p - a)) / n if n > 1e-9 else np.linalg.norm(p - a, axis=1)
    i = int(np.argmax(d))
    if d[i] > eps:
        return np.vstack([_rdp(p[: i + 1], eps)[:-1], _rdp(p[i:], eps)])
    return np.array([a, b])


def _smooth(p: np.ndarray) -> np.ndarray:
    if len(p) < 5:
        return p
    q = p.copy()
    closed = np.linalg.norm(p[0] - p[-1]) < 1.5
    for _ in range(2):
        if closed:
            q = (np.roll(q, 1, 0) + 2 * q + np.roll(q, -1, 0)) / 4
        else:
            q[1:-1] = (q[:-2] + 2 * q[1:-1] + q[2:]) / 4
    return q


def page_paths(page: Page, dpi: int, res: float = 25.0) -> list[np.ndarray]:
    """Centreline polylines for a page, in mm with y pointing down from the page top."""
    mm_per_px = 25.4 / dpi
    sw_px = page.stroke * mm_per_px * res  # stroke width at skeleton resolution
    out: list[np.ndarray] = []
    for op in page.ops:
        if isinstance(op, Stroke):
            out.append(op.points * mm_per_px)
            continue
        if not isinstance(op, Glyph):
            continue
        cs = [c * mm_per_px * res for c in op.contours]
        allp = np.concatenate(cs)
        o = np.floor(allp.min(0)) - 3
        size = (np.ceil(allp.max(0)) - o + 3).astype(int)
        acc = np.zeros((size[1], size[0]), np.uint8)
        for c in cs:
            im = Image.new("L", (int(size[0]), int(size[1])), 0)
            ImageDraw.Draw(im).polygon([tuple(p) for p in c - o], fill=1)
            acc ^= np.asarray(im)
        sk = skeletonize(acc.astype(bool))
        paths = _trace(sk)
        # prune short spurs (endpoint -> junction) that skeletons grow at stroke corners
        degs = convolve(sk.astype(np.uint8), np.ones((3, 3)), mode="constant") * sk - 1
        kept = []
        for p in paths:
            ends_free = [degs[p[0]] == 1, degs[p[-1]] == 1]
            if len(p) < sw_px * 0.9 and any(ends_free) and not all(ends_free):
                continue
            kept.append(p)
        if not kept and paths:  # tiny mark (an i-dot): keep the longest bit
            kept = [max(paths, key=len)]
        for p in kept:
            xy = np.array([(c, r) for r, c in p], float)
            xy = _smooth(xy)
            xy = _rdp(xy, 0.04 * res) if len(xy) > 2 else xy
            out.append((xy + o) / res)
    return out


def _order(paths: list[np.ndarray]) -> list[np.ndarray]:
    """Greedy nearest-neighbour ordering (paths may be reversed) to cut pen-up travel."""
    remaining = [p for p in paths if len(p)]
    if not remaining:
        return []
    starts = np.array([p[0] for p in remaining])
    ends = np.array([p[-1] for p in remaining])
    used = np.zeros(len(remaining), bool)
    order, cur = [], np.array([0.0, 0.0])
    for _ in range(len(remaining)):
        ds = np.linalg.norm(starts - cur, axis=1)
        de = np.linalg.norm(ends - cur, axis=1)
        ds[used] = de[used] = np.inf
        i_s, i_e = int(np.argmin(ds)), int(np.argmin(de))
        if ds[i_s] <= de[i_e]:
            p = remaining[i_s]; used[i_s] = True
        else:
            p = remaining[i_e][::-1]; used[i_e] = True
        order.append(p)
        cur = p[-1]
    return order


def to_gcode(paths_mm: list[np.ndarray], page_w_mm: float, page_h_mm: float, pl: Plotter) -> tuple[str, list]:
    """Return G-code text and the ordered paths in bed coordinates (for previews)."""
    if page_w_mm + pl.offset_x > pl.bed_x + 1e-6 or page_h_mm + pl.offset_y > pl.bed_y + 1e-6 or min(
            pl.offset_x, pl.offset_y) < 0:
        raise ValueError(
            f"page {page_w_mm:.0f}x{page_h_mm:.0f} mm at offset ({pl.offset_x},{pl.offset_y}) does not fit the "
            f"{pl.bed_x:.0f}x{pl.bed_y:.0f} mm bed - use a smaller --page (e.g. a5, or 200x200)"
        )
    bed = [np.column_stack([p[:, 0] + pl.offset_x, (page_h_mm - p[:, 1]) + pl.offset_y]) for p in paths_mm]
    bed = _order(bed)  # pen-tip positions; the nozzle is commanded to pen - (pen_dx, pen_dy)
    nozzle = np.concatenate(bed) - [pl.pen_dx, pl.pen_dy] if bed else np.zeros((1, 2))
    if nozzle.min() < 0 or nozzle[:, 0].max() > pl.bed_x or nozzle[:, 1].max() > pl.bed_y:
        raise ValueError(f"with pen offset ({pl.pen_dx}, {pl.pen_dy}) the nozzle would leave the bed "
                         f"(X {nozzle[:, 0].min():.0f}..{nozzle[:, 0].max():.0f}, "
                         f"Y {nozzle[:, 1].min():.0f}..{nozzle[:, 1].max():.0f})")
    ox, oy = pl.pen_dx, pl.pen_dy

    up, down = f"G0 Z{pl.pen_up_z:.2f} F{pl.z_speed:.0f}", f"G1 Z{pl.pen_down_z:.2f} F{pl.z_speed:.0f}"
    g = [
        f"; handwriter pen-plot G-code for {pl.name}",
        f"; page {page_w_mm:.1f} x {page_h_mm:.1f} mm, bottom-left at bed ({pl.offset_x}, {pl.offset_y})",
        f"; pen down Z={pl.pen_down_z}  pen up Z={pl.pen_up_z}  pen offset=({pl.pen_dx}, {pl.pen_dy})",
        "G21 ; mm",
        "G90 ; absolute",
        "M104 S0 ; hotend off (cold nozzle, pen does the work)",
        "M140 S0 ; bed off",
        "M106 S0 ; part fan off",
    ]
    if pl.flavor == "bambu":
        g += ["M106 P2 S0 ; aux fan off", "M106 P3 S0 ; chamber fan off", "M17 ; motors on"]
        # Bambu homes Z by touching the NOZZLE to the bed centre: the pen tip must sit above
        # the nozzle tip (or be spring-loaded) while this runs.
    if pl.home:
        if pl.flavor != "bambu":  # Bambu firmware handles Z clearance itself during G28
            g.append(f"G0 Z{pl.pen_up_z + 5:.2f} F{pl.z_speed:.0f}")
        g.append("G28 ; home all axes")
    g.append(f"G0 Z{pl.pen_up_z + 5:.2f} F{pl.z_speed:.0f}")

    pen_is_down = False
    last = None
    draw_len = travel_len = 0.0
    lifts = 0
    for p in bed:
        if last is None or np.linalg.norm(p[0] - last) > 0.15:  # need to lift and travel
            if pen_is_down or last is None:
                g.append(up)
            if last is not None:
                travel_len += float(np.linalg.norm(p[0] - last))
            lifts += 1
            g.append(f"G0 X{p[0, 0] - ox:.3f} Y{p[0, 1] - oy:.3f} F{pl.travel_speed:.0f}")
            g.append(down)
            pen_is_down = True
        for q in p[1:]:
            g.append(f"G1 X{q[0] - ox:.3f} Y{q[1] - oy:.3f} F{pl.draw_speed:.0f}")
        if len(p) > 1:
            draw_len += float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
        last = p[-1]
    g += [
        f"G0 Z{pl.pen_up_z + 10:.2f} F{pl.z_speed:.0f}",
        "M400",
        f"G0 X{pl.bed_x / 2:.0f} Y{pl.bed_y - 6:.0f} F{pl.travel_speed:.0f} ; park at the back, page free",
        "M400",
        "M18 ; motors off" if pl.flavor == "bambu" else "M84 ; motors off",
    ]
    minutes = (draw_len / pl.draw_speed + travel_len / pl.travel_speed
               + lifts * 2 * abs(pl.pen_up_z - pl.pen_down_z) / pl.z_speed) * 1.15  # + accel overhead
    g.insert(3, f"; drawing {draw_len / 1000:.2f} m, {lifts} pen lifts, est. {minutes:.0f} min")
    if pl.flavor == "bambu":  # the printer reads this block for its time estimate / progress
        m, sec = divmod(int(minutes * 60), 60)
        g = ["; HEADER_BLOCK_START", "; BambuStudio 02.07.01.62", f"; model printing time: {m}m {sec}s; "
             f"total estimated time: {m}m {sec}s", "; total layer number: 1", "; total filament weight [g] : 0.00",
             f"; max_z_height: {pl.pen_up_z + 10:.2f}", "; HEADER_BLOCK_END", ""] + g
    return "\n".join(g) + "\n", bed


def calibration_paths(page_w_mm: float, page_h_mm: float) -> list[np.ndarray]:
    """Crosshair at the page centre, L-marks 5 mm inside each corner, and a 10 mm square near the
    centre. Plot it on a card in the frame: if the marks are shifted, adjust --pen-offset."""
    cx, cy = page_w_mm / 2, page_h_mm / 2
    paths = [np.array([[cx - 10, cy], [cx + 10, cy]]), np.array([[cx, cy - 10], [cx, cy + 10]]),
             np.array([[cx + 15, cy - 5], [cx + 25, cy - 5], [cx + 25, cy + 5], [cx + 15, cy + 5], [cx + 15, cy - 5]])]
    m, L = 5.0, 10.0
    for (x, y, sx, sy) in ((m, m, 1, 1), (page_w_mm - m, m, -1, 1), (m, page_h_mm - m, 1, -1),
                           (page_w_mm - m, page_h_mm - m, -1, -1)):
        paths.append(np.array([[x + sx * L, y], [x, y], [x, y + sy * L]]))
    return paths


def preview(bed_paths: list, page_w_mm: float, page_h_mm: float, pl: Plotter, px_per_mm: float = 6) -> Image.Image:
    """Picture of what the plotter will draw (in page orientation)."""
    W, H = int(page_w_mm * px_per_mm), int(page_h_mm * px_per_mm)
    im = Image.new("RGB", (W, H), (250, 248, 241))
    d = ImageDraw.Draw(im)
    for p in bed_paths:
        pts = [((x - pl.offset_x) * px_per_mm, (page_h_mm - (y - pl.offset_y)) * px_per_mm) for x, y in p]
        if len(pts) == 1:
            d.point(pts[0], fill=(30, 50, 130))
        else:
            d.line(pts, fill=(30, 50, 130), width=max(1, int(0.4 * px_per_mm)), joint="curve")
    return im


DEFAULT_BAMBU_TEMPLATE = __import__("pathlib").Path(__file__).parent / "data" / "p1p_template.gcode.3mf"


def write_bambu_3mf(code: str, template: str | None, out: str, thumb: Image.Image | None = None) -> None:
    """Inject G-code into a plate-sliced .gcode.3mf exported from Bambu Studio.

    Bambu printers only accept .gcode.3mf. The most reliable way to get the container right is to
    export any tiny model once (File > Export > Export plate sliced file) and reuse it as a template:
    every entry is copied, and Metadata/plate_1.gcode and its .md5 are replaced with ours.
    """
    import io

    template = template or str(DEFAULT_BAMBU_TEMPLATE)
    data = code.encode()
    md5 = hashlib.md5(data).hexdigest().upper()
    thumbs = {}
    if thumb is not None:  # show the handwriting on the printer screen instead of the template's model
        for n, size in (("Metadata/plate_1.png", 512), ("Metadata/plate_1_small.png", 128),
                        ("Metadata/plate_no_light_1.png", 512)):
            t = thumb.copy()
            t.thumbnail((size, size))
            canvas = Image.new("RGB", (size, size), (40, 40, 40))
            canvas.paste(t, ((size - t.width) // 2, (size - t.height) // 2))
            buf = io.BytesIO()
            canvas.save(buf, "PNG")
            thumbs[n] = buf.getvalue()
    with zipfile.ZipFile(template) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        names = zin.namelist()
        gname = next((n for n in names if n.startswith("Metadata/plate_") and n.endswith(".gcode")), None)
        if gname is None:
            raise ValueError(f"{template} has no Metadata/plate_N.gcode - export a *plate sliced file* from Bambu Studio")
        for n in names:
            if n == gname:
                zout.writestr(n, data)
            elif n == gname + ".md5":
                zout.writestr(n, md5)
            elif n in thumbs:
                zout.writestr(n, thumbs[n])
            else:
                zout.writestr(zin.getinfo(n), zin.read(n))
        if gname + ".md5" not in names:
            zout.writestr(gname + ".md5", md5)
