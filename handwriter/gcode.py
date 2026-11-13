"""Turn a laid-out page into pen-plotter G-code (Marlin, e.g. Creality Ender 3 with a pen holder).

A pen draws lines, not filled shapes, so every glyph outline is rasterised, thinned to its
centreline (skeletonize), traced into polylines, pruned of skeleton spurs, smoothed and
simplified. All the human variation from the layout (habit shapes, drift, slant, i-dots...)
is kept; the physical pen supplies the ink texture itself.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import convolve
from skimage.morphology import skeletonize

from .layout import Glyph, Page, Stroke


@dataclass
class Plotter:
    bed_x: float = 220.0  # Ender 3 / 3 V2 / 3 Pro printable area, mm
    bed_y: float = 220.0
    offset_x: float = 0.0  # where the page's bottom-left corner sits on the bed, mm
    offset_y: float = 0.0
    pen_down_z: float = 0.0  # Z where the pen just touches the paper (calibrate this!)
    pen_up_z: float = 3.0
    draw_speed: float = 1500.0  # mm/min
    travel_speed: float = 4000.0
    z_speed: float = 300.0  # Ender 3 Z axis is slow (~5 mm/s)
    home: bool = True


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
    if page_w_mm + pl.offset_x > pl.bed_x + 1e-6 or page_h_mm + pl.offset_y > pl.bed_y + 1e-6:
        raise ValueError(
            f"page {page_w_mm:.0f}x{page_h_mm:.0f} mm at offset ({pl.offset_x},{pl.offset_y}) does not fit the "
            f"{pl.bed_x:.0f}x{pl.bed_y:.0f} mm bed - use a smaller --page (e.g. a5, or 200x200)"
        )
    bed = [np.column_stack([p[:, 0] + pl.offset_x, (page_h_mm - p[:, 1]) + pl.offset_y]) for p in paths_mm]
    bed = _order(bed)

    up, down = f"G0 Z{pl.pen_up_z:.2f} F{pl.z_speed:.0f}", f"G1 Z{pl.pen_down_z:.2f} F{pl.z_speed:.0f}"
    g = [
        "; handwriter pen-plot G-code (Marlin / Creality Ender 3)",
        f"; page {page_w_mm:.1f} x {page_h_mm:.1f} mm, bottom-left at bed ({pl.offset_x}, {pl.offset_y})",
        f"; pen down Z={pl.pen_down_z}  pen up Z={pl.pen_up_z}  strokes={len(bed)}",
        "G21 ; mm",
        "G90 ; absolute",
        "M107 ; fan off",
        "M104 S0 ; hotend off",
        "M140 S0 ; bed off",
    ]
    if pl.home:
        g += [f"G0 Z{pl.pen_up_z + 5:.2f} F{pl.z_speed:.0f}", "G28 ; home all axes"]
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
            g.append(f"G0 X{p[0, 0]:.3f} Y{p[0, 1]:.3f} F{pl.travel_speed:.0f}")
            g.append(down)
            pen_is_down = True
        for q in p[1:]:
            g.append(f"G1 X{q[0]:.3f} Y{q[1]:.3f} F{pl.draw_speed:.0f}")
        if len(p) > 1:
            draw_len += float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum())
        last = p[-1]
    g += [
        f"G0 Z{pl.pen_up_z + 10:.2f} F{pl.z_speed:.0f}",
        f"G0 X0 Y{pl.bed_y:.0f} F{pl.travel_speed:.0f} ; present the page",
        "M84 ; motors off",
    ]
    minutes = (draw_len / pl.draw_speed + travel_len / pl.travel_speed
               + lifts * 2 * abs(pl.pen_up_z - pl.pen_down_z) / pl.z_speed) * 1.15  # + accel overhead
    g.insert(3, f"; drawing {draw_len / 1000:.2f} m, {lifts} pen lifts, est. {minutes:.0f} min")
    return "\n".join(g) + "\n", bed


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
