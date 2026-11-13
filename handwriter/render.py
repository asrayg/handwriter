"""Rasterise glyph outlines as ink on paper.

Ink model
---------
coverage `alpha` (where is there ink) x `density` (how much ink) -> Beer-Lambert absorption:
    out = paper * exp(-alpha * density * k_ink),   k_ink = -ln(ink_rgb / 255)
so overlapping strokes and blobs get darker and more saturated, like real dye on paper.

Density is modulated by pen pressure, stroke direction (downstrokes heavier), ballpoint
burr striations (ink-free streaks running *along* the stroke, made with line-integral
convolution over the local stroke direction), gooping blobs, skips, inkless starts,
paper tooth, bleed / feathering and edge pooling, depending on the pen.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt, gaussian_filter, map_coordinates, maximum_filter

from .layout import Blob, Glyph, Page, Ramp, Skip, Stroke, Writer
from .style import Style


def noise(rng, h, w, cell, cell_x=None) -> np.ndarray:
    """Smooth value noise with ~unit std. `cell_x` > `cell` gives horizontally stretched (fibre) noise."""
    cy, cx = cell, cell_x or cell
    gh, gw = int(h / cy) + 3, int(w / cx) + 3
    g = rng.standard_normal((gh, gw)).astype(np.float32)
    img = Image.fromarray(g, mode="F").resize((int(gw * cx), int(gh * cy)), Image.BICUBIC)
    a = np.asarray(img)[:h, :w]
    return (a - a.mean()) / (a.std() + 1e-6)


def _up(a: np.ndarray, s: int) -> np.ndarray:
    return np.repeat(np.repeat(a, s, 0), s, 1)


def _down(a: np.ndarray, s: int) -> np.ndarray:
    h, w = a.shape[0] // s, a.shape[1] // s
    return a[: h * s, : w * s].reshape(h, s, w, s).mean((1, 3))


def _ellipse(x, y, rx, ry, ang, s=1.0, n=24):
    t = np.linspace(0, 2 * math.pi, n, endpoint=False)
    ex, ey = np.cos(t) * rx, np.sin(t) * ry
    ca, sa = math.cos(ang), math.sin(ang)
    return [((x + ex[i] * ca - ey[i] * sa) * s, (y + ex[i] * sa + ey[i] * ca) * s) for i in range(n)]


# ------------------------------------------------------------------ raster


def _raster(page: Page, W: int, H: int, SS: int):
    count = np.zeros((H * SS, W * SS), np.uint8)
    extra = Image.new("L", (W * SS, H * SS), 0)  # strike-throughs
    blobs = Image.new("L", (W * SS, H * SS), 0)
    ed, bd = ImageDraw.Draw(extra), ImageDraw.Draw(blobs)
    for op in page.ops:
        if isinstance(op, Glyph):
            pts = np.concatenate(op.contours) * SS
            x0, y0 = np.floor(pts.min(0)).astype(int) - 2
            x1, y1 = np.ceil(pts.max(0)).astype(int) + 2
            x0, y0 = max(x0, 0), max(y0, 0)
            x1, y1 = min(x1, W * SS), min(y1, H * SS)
            if x1 <= x0 or y1 <= y0:
                continue
            acc = np.zeros((y1 - y0, x1 - x0), np.uint8)
            for c in op.contours:  # even-odd fill keeps counters (holes in o, a, e) open
                im = Image.new("L", (x1 - x0, y1 - y0), 0)
                ImageDraw.Draw(im).polygon([tuple(p) for p in (c * SS - (x0, y0))], fill=1)
                acc ^= np.asarray(im)
            count[y0:y1, x0:x1] += acc  # counts >1 = letters overlapping = double ink
        elif isinstance(op, Stroke):
            ed.line([tuple(p) for p in op.points * SS], fill=255, width=max(1, int(op.width * SS)), joint="curve")
        elif isinstance(op, Blob):
            bd.polygon(_ellipse(op.x, op.y, op.rx, op.ry, op.angle, SS), fill=255)
    count += (np.asarray(extra) > 127).astype(np.uint8)
    return count, np.asarray(blobs, np.float32) / 255.0


def _structure(alpha: np.ndarray, sw: float):
    """Local stroke orientation from the structure tensor. Returns verticalness and unit tangent."""
    a = gaussian_filter(alpha, 0.8)
    gy, gx = np.gradient(a)
    s = max(1.0, sw * 0.6)
    jxx, jyy, jxy = gaussian_filter(gx * gx, s), gaussian_filter(gy * gy, s), gaussian_filter(gx * gy, s)
    vert = jxx / (jxx + jyy + 1e-7)  # gradient mostly horizontal -> stroke runs vertically
    theta = 0.5 * np.arctan2(2 * jxy, jxx - jyy)  # gradient direction
    return vert, -np.sin(theta), np.cos(theta)


def render_ink(page: Page, st: Style, W: int, H: int, rng, paper_fibre: np.ndarray):
    """Return (alpha, density) at output resolution."""
    pen = st.pen_profile
    SS = 3 if st.dpi <= 200 else 2
    xh, sw, k = page.x_height, page.stroke, st.pen_defects

    count, blob_ss = _raster(page, W, H, SS)
    cov = count > 0
    if page.grow > 0.15:
        cov = distance_transform_edt(~cov) <= page.grow * SS
    if pen.nib > 0:  # broad nib: thick when moving across the nib, thin along it
        L = max(3, int(pen.nib * xh * SS))
        fp = np.zeros((L, L), bool)
        a = math.radians(pen.nib_angle)
        for t in np.linspace(-0.5, 0.5, L * 2):
            fp[int(round((L - 1) / 2 - t * (L - 1) * math.sin(a))), int(round((L - 1) / 2 + t * (L - 1) * math.cos(a)))] = True
        cov = maximum_filter(cov, footprint=fp)
    cov = cov.astype(np.float32)
    np.maximum(cov, blob_ss, out=cov)

    # stroke thickness follows pen pressure: threshold a blurred mask against a smooth field
    pv = st.pressure_variation
    f = gaussian_filter(cov, sigma=0.5 * SS)
    thr = 0.5 - st.weight - _up(0.045 * pv * noise(rng, H, W, xh * 4) + 0.035 * pv * noise(rng, H, W, xh * 0.35), SS)
    alpha = _down(np.clip((f - thr) / 0.12 + 0.5, 0, 1), SS)
    overlap = _down((count > 1).astype(np.float32), SS)
    blob = gaussian_filter(_down(blob_ss, SS), 0.6)
    del f, thr, cov, count, blob_ss

    # ------------------------------------------------ density
    density = 0.88 + 0.12 * np.tanh(noise(rng, H, W, xh * 2.2) * pv)  # pressure varies as you write
    vert, tx, ty = _structure(alpha, sw)
    density *= 1 + pen.direction * (2 * vert - 1)  # downstrokes carry more pressure than upstrokes

    ys, xs = np.nonzero(alpha > 0.02)
    if pen.striation > 0 and len(ys):
        # burr striations: line-integral convolution of white noise along the stroke direction
        wn = rng.random((H, W)).astype(np.float32)
        L = int(max(4, sw * 1.6))
        dx, dy = tx[ys, xs], ty[ys, xs]
        acc = np.zeros(len(ys), np.float32)
        for j in range(-L, L + 1):
            acc += map_coordinates(wn, [ys + j * dy, xs + j * dx], order=1, mode="wrap")
        lic = (acc - acc.mean()) / (acc.std() + 1e-6)
        streak = np.clip((lic - 0.35) / 0.9, 0, 1)
        patch = np.clip(0.6 + 0.6 * noise(rng, H, W, xh * 1.3)[ys, xs], 0, 1)  # some stretches streak more
        density[ys, xs] *= 1 - min(1.0, pen.striation * k) * 0.9 * streak * patch

    events = Image.new("F", (W, H), 1.0)
    edraw = ImageDraw.Draw(events)
    has_events = False
    for op in page.ops:
        if isinstance(op, Skip):
            edraw.polygon(_ellipse(op.x, op.y, op.rx, op.ry, op.angle), fill=0.12)
            has_events = True
    if has_events:
        density *= gaussian_filter(np.asarray(events), 0.6)
    for op in page.ops:
        if isinstance(op, Ramp):  # inkless start: ink fades in as the ball primes
            x0, x1 = int(max(0, op.x0 - 2)), int(min(W, op.x0 + op.length + 1))
            y0, y1 = int(max(0, op.y0 - 2)), int(min(H, op.y1 + 2))
            if x1 > x0 and y1 > y0:
                t = np.clip((np.arange(x0, x1) - op.x0) / op.length, 0, 1)
                ramp = op.start + (1 - op.start) * t * t * (3 - 2 * t)
                density[y0:y1, x0:x1] *= ramp[None, :]

    density += pen.overlap * overlap + 0.55 * blob
    alpha = np.maximum(alpha, blob)

    # paper tooth: ink (esp. graphite) misses the valleys of the paper
    grain = noise(rng, H, W, 0.9)
    valleys = np.clip(-(0.6 * paper_fibre + 0.8 * grain) - 0.2, 0, 2) / 2
    density *= 1 - np.clip(pen.tooth * 2.2 * valleys, 0, 0.95)

    if pen.bleed > 0:  # wicking along paper fibres
        feather = gaussian_filter(alpha, 0.5 + 0.7 * pen.bleed) * np.clip(0.55 + 0.45 * paper_fibre, 0, 1)
        alpha = np.maximum(alpha, feather * min(0.9, 0.55 + 0.4 * pen.bleed))
    if pen.pooling > 0:  # wet ink dries darker at the edges
        density += pen.pooling * 2.5 * np.clip(alpha - gaussian_filter(alpha, max(1, sw * 0.4)), 0, 1)

    return alpha, density * pen.density


# ------------------------------------------------------------------ paper


def render_paper(st: Style, W: int, H: int, rng, line_h: float, left_margin: float, top: float):
    base = np.array([250, 248, 241], np.float32)
    fibre = noise(rng, H, W, 1.2, 7)  # horizontally stretched: paper fibres / machine direction
    img = np.ones((H, W, 3), np.float32) * base
    img += (2.5 * noise(rng, H, W, st.dpi * 0.6) + 0.9 * fibre + 0.8 * noise(rng, H, W, 2))[..., None]

    if st.paper in ("lined", "grid"):
        line_col = np.array([160, 190, 225], np.float32)
        lw = max(1, st.dpi / 200)
        if st.paper == "lined":
            for y in np.arange(top + line_h, H - 1, line_h):
                yi = int(y)
                for yy in range(yi, yi + int(math.ceil(lw)) + 1):
                    a = 0.8 * np.clip(1 - abs(yy + 0.5 - (y + lw / 2)) / (lw / 2 + 0.5), 0, 1)
                    if 0 <= yy < H:
                        img[yy] = img[yy] * (1 - a) + line_col * a
            mx = int(left_margin - 0.2 * st.dpi)
            red = np.array([225, 140, 150], np.float32)
            mw = int(max(2, lw * 1.5))
            img[:, mx : mx + mw] = img[:, mx : mx + mw] * 0.3 + red * 0.7
        else:
            step = line_h / 2
            for y in np.arange(top % step, H, step):
                img[int(y)] = img[int(y)] * 0.6 + line_col * 0.4
            for x in np.arange(0, W, step):
                img[:, int(x)] = img[:, int(x)] * 0.6 + line_col * 0.4
    return img, fibre


# ------------------------------------------------------------------ compose


def compose(page: Page, writer: Writer, rng) -> Image.Image:
    st, W, H = writer.st, writer.W, writer.H
    paper, fibre = render_paper(st, W, H, rng, writer.line_h, writer.left, writer.top)
    alpha, density = render_ink(page, st, W, H, rng, fibre)
    k_ink = -np.log(np.clip(np.array(st.ink_rgb, np.float32) / 255.0, 0.02, 0.98))
    od = (alpha * density)[..., None]
    out = paper * np.exp(-od * k_ink)

    if st.scan:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        g = rng.uniform(-1, 1, 2)
        shade = 1 - 0.025 * ((xx / W - 0.5) * g[0] + (yy / H - 0.5) * g[1]) - 0.03 * (
            ((xx / W - 0.5) ** 2 + (yy / H - 0.5) ** 2)
        )
        out = out * shade[..., None] + rng.normal(0, 1.6, out.shape).astype(np.float32)

    im = Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
    if st.scan:
        angle = rng.normal(0, 0.35)
        im = im.rotate(angle, resample=Image.BICUBIC, fillcolor=tuple(int(v) for v in paper[5, 5]))
    return im


def generate(text: str, style: Style | None = None) -> list[Image.Image]:
    """Text in, list of page images out."""
    style = style or Style()
    w = Writer(style)
    pages = w.write(text)
    return [compose(p, w, w.rng) for p in pages]
