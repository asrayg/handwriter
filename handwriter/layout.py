"""Lay text out on pages as one consistent (but imperfect) writer would.

Three layers of variation, mirroring how real handwriting varies:
  1. Writer habits   - fixed for the whole document: personal letter shapes (2 allographs per
                       character), where i-dots land, whether words trail off at the end, etc.
  2. Slow drift      - per line: baseline wander, tilt, size and slant drift, fatigue.
  3. Instance noise  - per letter: small warp, jitter, tremor.
Plus pen events (ink blobs, skips, faint starts) recorded for the renderer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .font import HandFont
from .style import Style

# ---------------------------------------------------------------- page ops


@dataclass
class Glyph:
    contours: list[np.ndarray]  # pixel coords


@dataclass
class Stroke:
    points: np.ndarray  # pixel coords polyline
    width: float


@dataclass
class Blob:  # ballpoint "gooping": ink spot after a sharp turn
    x: float
    y: float
    rx: float
    ry: float
    angle: float


@dataclass
class Skip:  # ball lost contact: short gap across the stroke
    x: float
    y: float
    rx: float
    ry: float
    angle: float


@dataclass
class Ramp:  # inkless start: ink fades in over the first bit of a word
    x0: float
    length: float
    y0: float
    y1: float
    start: float  # density at x0 (0..1)


@dataclass
class Page:
    ops: list
    x_height: float
    grow: float = 0.0  # px to fatten strokes so hairline fonts look like a real pen
    stroke: float = 3.0  # typical stroke width, px
    events: list = field(default_factory=list)


# ---------------------------------------------------------------- helpers


class Drift:
    """Smooth 1-D noise: a few random sinusoids. Used for wandering baselines, size, slant."""

    def __init__(self, rng: np.random.Generator, amp: float, wavelength: float, terms: int = 3):
        self.amp = amp
        self.f = rng.uniform(0.6, 1.8, terms) * 2 * math.pi / wavelength
        self.p = rng.uniform(0, 2 * math.pi, terms)
        self.w = rng.uniform(0.4, 1.0, terms)
        self.w /= np.sqrt((self.w**2).sum() / 2)

    def __call__(self, x: float) -> float:
        return float(self.amp * (self.w * np.sin(self.f * x + self.p)).sum())


def _warp_fn(rng: np.random.Generator, amp: float, k: int = 3, fmin=1.5, fmax=4.5):
    """Random smooth displacement field over glyph space -> a glyph bends uniquely."""
    fx = rng.uniform(fmin, fmax, (2, k)) * rng.choice([-1, 1], (2, k))
    fy = rng.uniform(fmin, fmax, (2, k)) * rng.choice([-1, 1], (2, k))
    ph = rng.uniform(0, 2 * math.pi, (2, k))
    a = rng.normal(0, amp / math.sqrt(k), (2, k))

    def f(p: np.ndarray) -> np.ndarray:
        x, y = p[:, :1], p[:, 1:2]
        dx = (a[0] * np.sin(fx[0] * x + fy[0] * y + ph[0])).sum(1)
        dy = (a[1] * np.sin(fx[1] * x + fy[1] * y + ph[1])).sum(1)
        return p + np.column_stack([dx, dy])

    return f


def _tremor(c: np.ndarray, rng, amp: float) -> np.ndarray:
    if amp <= 0 or len(c) < 6:
        return c
    n = rng.normal(0, 1, c.shape)
    for _ in range(3):  # smooth along the contour so it's a wobble, not jaggies
        n = (np.roll(n, 1, 0) + n + np.roll(n, -1, 0)) / 3
    n /= n.std() + 1e-9
    return c + n * amp


def _inside(pt, contours) -> bool:
    """Even-odd point-in-polygon over all contours of a glyph."""
    x, y = pt
    inside = False
    for c in contours:
        x1, y1 = c[:, 0], c[:, 1]
        x2, y2 = np.roll(x1, -1), np.roll(y1, -1)
        cond = ((y1 > y) != (y2 > y)) & (x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1)
        inside ^= bool(cond.sum() % 2)
    return inside


def _edge_point(contours, rng, sharp: bool):
    """Pick a point on a glyph's outline (biased to sharp turns if `sharp`), return point, inward normal, tangent."""
    lens = np.array([len(c) for c in contours], float)
    c = contours[rng.choice(len(contours), p=lens / lens.sum())]
    d1 = np.roll(c, -2, 0) - c
    d0 = c - np.roll(c, 2, 0)
    if sharp:
        ang = np.abs(np.arctan2(d0[:, 0] * d1[:, 1] - d0[:, 1] * d1[:, 0], (d0 * d1).sum(1)))
        w = ang**2 + 1e-3
        i = rng.choice(len(c), p=w / w.sum())
    else:
        i = rng.integers(len(c))
    t = d1[i] + d0[i]
    t = t / (np.linalg.norm(t) + 1e-9)
    n = np.array([-t[1], t[0]])
    return c[i], n, t


_KEYS = "qwertyuiop asdfghjkl zxcvbnm".split()


def _misspell(word: str, rng: np.random.Generator) -> str:
    letters = [i for i, c in enumerate(word) if c.isalpha()]
    if len(letters) < 3:
        return word
    w = list(word)
    kind = rng.integers(3)
    i = letters[rng.integers(1, len(letters) - 1)]
    if kind == 0 and i + 1 < len(w) and w[i + 1].isalpha():  # swap
        w[i], w[i + 1] = w[i + 1], w[i]
    elif kind == 1:  # stopped early, realised
        w = w[: i + 1]
    else:  # neighbouring key / letter
        c = w[i].lower()
        for row in _KEYS:
            j = row.find(c)
            if j >= 0:
                n = row[max(0, min(len(row) - 1, j + rng.choice([-1, 1])))]
                w[i] = n.upper() if w[i].isupper() else n
                break
    out = "".join(w)
    return out if out != word else word[:-1]


# ---------------------------------------------------------------- writer


class Writer:
    def __init__(self, style: Style):
        self.st = style
        self.rng = np.random.default_rng(style.seed)
        self.font = HandFont(style.font)
        self.pen = style.pen_profile
        dpi = style.dpi
        self.W = int(style.page_inches[0] * dpi)
        self.H = int(style.page_inches[1] * dpi)
        self.xh = style.x_height_mm / 25.4 * dpi
        self.line_h = style.line_spacing_mm / 25.4 * dpi
        ml, mr, mt, mb = (m * dpi for m in style.margins_in)
        self.left, self.right, self.top, self.bottom = ml, self.W - mr, mt, self.H - mb
        # a ballpoint line is ~0.35mm; fonts thinner than that get fattened
        pen_px = max(style.pen_mm / 25.4 * dpi, 0.11 * self.xh)
        self.grow = max(0.0, (pen_px - self.font.stroke * self.xh) / 2)
        self.sw = max(pen_px, self.font.stroke * self.xh)

        # ---- writer habits: drawn once, consistent across the whole document
        h = style.habits
        hseed = self.rng.integers(1 << 31)  # always drawn so seeds stay reproducible
        if style.writer_seed is not None:
            hseed = np.random.default_rng(style.writer_seed).integers(1 << 31)
        r = np.random.default_rng(hseed)
        self._habit_rng = r
        self.hab = dict(
            dot_dx=r.uniform(0.0, 0.28) * h,  # most people dot i's a touch to the right
            dot_dy=r.uniform(-0.18, 0.04) * h,  # ...and a bit high
            dot_dash=r.uniform(0.0, 0.7) * h,  # chance a dot becomes a little flick
            dot_spread=0.05 + 0.04 * h,
            end_rise=r.normal(0.0, 0.05) * h,  # last letter of word drifts up/down
            end_shrink=r.uniform(0.0, 0.07) * h,  # words trail off
            start_boost=r.uniform(0.0, 0.05) * h,  # first letter a bit bigger
            allo_bias=r.uniform(0.55, 0.85),  # how often variant A is used over B
        )
        self._allographs: dict[str, list] = {}

    def _allograph(self, ch: str):
        """This writer's persistent way(s) of shaping `ch`."""
        if ch not in self._allographs:
            a = self.st.glyph_habit_warp * self.st.habits
            self._allographs[ch] = [_warp_fn(self._habit_rng, a, k=4, fmin=1.0, fmax=3.5) for _ in range(2)]
        pair = self._allographs[ch]
        return pair[0] if self.rng.random() < self.hab["allo_bias"] else pair[1]

    # ---- per-line state -------------------------------------------------
    def _start_line(self, ruled_y: float):
        st, r = self.st, self.rng
        progress = (ruled_y - self.top) / max(1, self.bottom - self.top)
        m = st.messiness * (1 + st.fatigue * progress)
        self.m = m
        blank = st.paper == "blank"
        self.angle = math.radians(r.normal(0, st.line_angle_deg * m * (1.8 if blank else 1)))
        self.base_drift = Drift(r, st.baseline_wave * self.xh * m, 6 * self.xh)
        self.size_drift = Drift(r, st.size_wave * m, 10 * self.xh)
        self.slant_drift = Drift(r, st.slant_wave_deg * m, 14 * self.xh)
        self.base_y = ruled_y - abs(r.normal(0.04, 0.05 * m)) * self.xh
        self.line_x0 = self.left + abs(r.normal(0, 0.25 * m)) * self.xh
        self.x = self.line_x0
        self.right_limit = self.right - abs(r.normal(0, 0.8)) * self.xh
        self.squeeze = 1.0

    def _baseline(self, x: float) -> float:
        return self.base_y + math.tan(self.angle) * (x - self.line_x0) + self.base_drift(x)

    # ---- glyphs ---------------------------------------------------------
    def _char_scale(self, x: float) -> float:
        return self.xh * (1 + self.size_drift(x))

    def _measure(self, word: str) -> float:
        s = self._char_scale(self.x) * self.st.tracking
        return sum(self.font.glyph(c).advance for c in word) * s

    def _place_char(self, ch: str, ops: list, pos: int, n: int):
        st, r, m, hab = self.st, self.rng, self.m, self.hab
        g = self.font.glyph(ch)
        s = self._char_scale(self.x) * (1 + r.normal(0, st.glyph_scale_jitter * m))
        by = self._baseline(self.x) + r.normal(0, st.glyph_baseline_jitter * m) * self.xh
        if n > 2 and pos == 0:
            s *= 1 + hab["start_boost"]
        if n > 2 and pos == n - 1:
            s *= 1 - hab["end_shrink"]
            by -= hab["end_rise"] * self.xh
        sx = 1 + r.normal(0, st.glyph_squash * m)
        slant = math.radians(st.slant_deg + self.slant_drift(self.x) + r.normal(0, st.glyph_slant_jitter * m))
        rot = math.radians(r.normal(0, st.glyph_rotation_jitter * m)) + self.angle
        habit = self._allograph(ch)
        warp = _warp_fn(r, st.glyph_warp * math.sqrt(m))
        cr, sr, sh = math.cos(rot), math.sin(rot), math.tan(slant)
        tremor = 0.008 * st.tremor * math.sqrt(m)

        placed, body = [], []
        for c, is_dot in zip(g.contours, g.is_dot):
            p = c.copy()
            if is_dot:
                ctr = p.mean(0)
                if r.random() < hab["dot_dash"]:  # dot written as a quick flick
                    rad = max(np.ptp(p[:, 0]), np.ptp(p[:, 1])) / 2
                    t = np.linspace(0, 2 * math.pi, 28, endpoint=False)
                    a = math.radians(r.normal(-25, 12))
                    ex, ey = np.cos(t) * rad * r.uniform(1.6, 2.4), np.sin(t) * rad * 0.55
                    p = np.column_stack([ex * math.cos(a) - ey * math.sin(a), ex * math.sin(a) + ey * math.cos(a)]) + ctr
                spread = hab["dot_spread"] * m
                p = p + [hab["dot_dx"] + r.normal(0, spread), hab["dot_dy"] + r.normal(0, spread)]
            else:
                p = warp(habit(p))
                p = _tremor(p, r, tremor)
            p[:, 0] = p[:, 0] * sx - p[:, 1] * sh
            x = p[:, 0] * cr - p[:, 1] * sr
            y = p[:, 0] * sr + p[:, 1] * cr
            q = np.column_stack([x * s + self.x, y * s + by])
            placed.append(q)
            if not is_dot:
                body.append(q)
        if placed:
            ops.append(Glyph(placed))
            if body:
                self._pen_events(body, ops, pos)
        self.x += g.advance * s * st.tracking * self.squeeze * sx * (1 + r.normal(0, st.glyph_spacing_jitter * m))

    def _pen_events(self, body, ops, pos):
        pen, r, k, sw = self.pen, self.rng, self.st.pen_defects, self.sw
        allp = np.concatenate(body)
        if pos == 0 and r.random() < pen.inkless_start * k:
            x0 = allp[:, 0].min()
            ops.append(Ramp(x0, r.uniform(0.12, 0.45) * self.xh, allp[:, 1].min(), allp[:, 1].max(), r.uniform(0.1, 0.55)))
        if r.random() < pen.goop * k:
            pt, nrm, _ = _edge_point(body, r, sharp=True)
            if not _inside(pt + nrm * sw * 0.4, body):
                nrm = -nrm
            c = pt + nrm * sw * r.uniform(0.0, 0.35)
            rad = sw * r.uniform(0.35, 0.6)
            ops.append(Blob(c[0], c[1], rad * r.uniform(1.0, 1.6), rad, r.uniform(0, math.pi)))
        if r.random() < pen.skip * k:
            pt, nrm, t = _edge_point(body, r, sharp=False)
            if not _inside(pt + nrm * sw * 0.4, body):
                nrm = -nrm
            c = pt + nrm * sw * 0.5
            ops.append(Skip(c[0], c[1], sw * r.uniform(0.9, 1.3), sw * r.uniform(0.2, 0.6), math.atan2(nrm[1], nrm[0])))

    def _space(self):
        r = self.rng
        k = max(0.82, r.normal(1.0, 0.15 * self.m))
        self.x += self.font.space * self._char_scale(self.x) * self.st.word_spacing * k

    def _strike(self, x0: float, x1: float, ops: list):
        r = self.rng
        passes = 1 + int(r.random() < 0.45)
        for _ in range(passes):
            n = 24
            xs = np.linspace(x0 - 0.15 * self.xh, x1 + 0.15 * self.xh, n)
            mid = r.uniform(0.35, 0.6)
            ys = np.array([self._baseline(x) for x in xs]) - mid * self.xh
            ys += np.cumsum(r.normal(0, 0.03 * self.xh, n))
            ys += np.linspace(0, r.normal(0, 0.15) * self.xh, n)
            ops.append(Stroke(np.column_stack([xs, ys]), width=self.sw * r.uniform(0.85, 1.1)))

    def _word(self, word: str, ops: list):
        for i, ch in enumerate(word):
            self._place_char(ch, ops, i, len(word))

    # ---- main -----------------------------------------------------------
    def write(self, text: str) -> list[Page]:
        pages: list[Page] = []
        ops: list = []
        line_idx = 0

        def ruled(i):
            return self.top + (i + 1) * self.line_h

        def page():
            return Page(ops, self.xh, self.grow, self.sw)

        def new_page():
            nonlocal ops, line_idx
            if ops:
                pages.append(page())
            ops, line_idx = [], 0
            self._start_line(ruled(0))

        def new_line():
            nonlocal line_idx
            line_idx += 1
            y = ruled(line_idx)
            if self.st.paper == "blank":
                y += self.rng.normal(0, 0.1) * self.xh
            if y > self.bottom:
                new_page()
            else:
                self._start_line(y)

        new_page()
        for pi, para in enumerate(text.replace("\r\n", "\n").split("\n")):
            if pi > 0:
                new_line()
            first = True
            for word in para.split(" "):
                if word == "":
                    self._space()
                    continue
                wrong = None
                if len(word) > 3 and self.rng.random() < self.st.mistakes * self.m:
                    wrong = _misspell(word, self.rng)
                need = self._measure(word) + (self._measure(wrong) + self.font.space * self.xh if wrong else 0)
                self.squeeze = 1.0
                if not first:
                    room = self.right_limit - self.x - self.font.space * self.xh
                    if need > room and self.x > self.line_x0:
                        # people cram a word in rather than wrap if it *almost* fits
                        if room > 0 and need < room * 1.15 and len(word) > 2:
                            self._space()
                            self.squeeze = room / need
                        else:
                            new_line()
                    else:
                        self._space()
                first = False
                if wrong:
                    x0 = self.x
                    self._word(wrong, ops)
                    self._strike(x0, self.x, ops)
                    self._space()
                self._word(word, ops)
        pages.append(page())
        return pages
