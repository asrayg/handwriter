"""Rasterise glyph outlines as ink on paper, with pressure, bleed, grain and scan artifacts."""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageDraw
from scipy.ndimage import distance_transform_edt, gaussian_filter

from .layout import Glyph, Page, Stroke, Writer
from .style import Style

SS = 3  # supersampling


def noise(rng, h, w, cell) -> np.ndarray:
    """Smooth value noise with ~unit std."""
    gh, gw = int(h / cell) + 3, int(w / cell) + 3
    g = rng.standard_normal((gh, gw)).astype(np.float32)
    img = Image.fromarray(g, mode="F").resize((int(gw * cell), int(gh * cell)), Image.BICUBIC)
    a = np.asarray(img)[:h, :w]
    return (a - a.mean()) / (a.std() + 1e-6)


def _raster_ink(page: Page, W: int, H: int) -> np.ndarray:
    mask = np.zeros((H * SS, W * SS), np.uint8)
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
            mask[y0:y1, x0:x1] |= acc
        elif isinstance(op, Stroke):
            im = Image.fromarray(mask * 255)
            d = ImageDraw.Draw(im)
            d.line([tuple(p) for p in op.points * SS], fill=255, width=max(1, int(op.width * SS)), joint="curve")
            mask = (np.asarray(im) > 127).astype(np.uint8)
    return mask


def render_ink(page: Page, st: Style, W: int, H: int, rng) -> np.ndarray:
    """Return ink coverage alpha (H, W) float in [0, 1]."""
    xh = page.x_height
    mask = _raster_ink(page, W, H)
    if page.grow > 0.15:
        mask = (distance_transform_edt(mask == 0) <= page.grow * SS).astype(np.uint8)
    mask = mask.astype(np.float32)

    # Stroke thickness varies with pen pressure: threshold a blurred mask with a spatial field.
    f = gaussian_filter(mask, sigma=0.5 * SS)
    pv = st.pressure_variation
    thr = 0.5 - st.weight - 0.045 * pv * noise(rng, H * SS, W * SS, xh * SS * 4)
    thr -= 0.035 * pv * noise(rng, H * SS, W * SS, xh * SS * 0.35)
    alpha = np.clip((f - thr) / 0.12 + 0.5, 0, 1)
    del f, thr, mask

    alpha = alpha.reshape(H, SS, W, SS).mean((1, 3))

    # Darkness varies with pressure too (lighter where the pen skated).
    dark = 0.88 + 0.12 * np.tanh(noise(rng, H, W, xh * 2.2) * pv)
    # Ink grain / paper tooth: a few tiny dropouts
    pencil = st.ink == "pencil"
    tooth = noise(rng, H, W, 1.2) * (0.25 if pencil else 0.08)
    alpha = np.clip(alpha * dark * (1 - np.clip(tooth, 0, None)), 0, 1)

    # Slight bleed into the paper fibres.
    if not pencil:
        alpha = np.maximum(alpha, gaussian_filter(alpha, 0.7) * 0.85)
    return alpha


def render_paper(st: Style, W: int, H: int, page: Page, rng, line_h: float, left_margin: float, top: float) -> np.ndarray:
    base = np.array([250, 248, 241], np.float32)
    img = np.ones((H, W, 3), np.float32) * base
    lum = 2.5 * noise(rng, H, W, 120) + 1.2 * noise(rng, H, W, 3)
    img += lum[..., None]

    if st.paper in ("lined", "grid"):
        line_col = np.array([160, 190, 225], np.float32)
        ys = np.arange(top + line_h, H - 1, line_h)
        if st.paper == "lined":
            for y in ys:
                yi = int(round(y))
                for yy, a in ((yi, 0.75), (yi + 1, 0.25)):
                    if 0 <= yy < H:
                        img[yy] = img[yy] * (1 - a) + line_col * a
            mx = int(left_margin - 0.2 * st.dpi)
            red = np.array([225, 140, 150], np.float32)
            img[:, mx : mx + 2] = img[:, mx : mx + 2] * 0.3 + red * 0.7
        else:
            step = line_h / 2
            for y in np.arange(top % step, H, step):
                img[int(y)] = img[int(y)] * 0.6 + line_col * 0.4
            for x in np.arange(0, W, step):
                img[:, int(x)] = img[:, int(x)] * 0.6 + line_col * 0.4
    return img


def compose(page: Page, writer: Writer, rng) -> Image.Image:
    st, W, H = writer.st, writer.W, writer.H
    paper = render_paper(st, W, H, page, rng, writer.line_h, writer.left, writer.top)
    alpha = render_ink(page, st, W, H, rng)
    ink = np.array(st.ink_rgb, np.float32) / 255.0
    # multiply blend: ink darkens whatever is underneath (ruled lines show through slightly)
    out = paper * (1 - alpha[..., None] * (1 - ink))

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
