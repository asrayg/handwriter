"""Lay text out on pages, deforming every single glyph instance differently."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .font import HandFont
from .style import Style


@dataclass
class Glyph:
    contours: list[np.ndarray]  # pixel coords


@dataclass
class Stroke:
    points: np.ndarray  # pixel coords polyline
    width: float


@dataclass
class Page:
    ops: list
    baselines: list[float]
    x_height: float
    grow: float = 0.0  # px to fatten strokes so hairline fonts look like a real pen


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


def _warp_fn(rng: np.random.Generator, amp: float):
    """Random smooth displacement field over glyph space -> every glyph bends uniquely."""
    k = 3
    fx = rng.uniform(1.5, 4.5, (2, k)) * rng.choice([-1, 1], (2, k))
    fy = rng.uniform(1.5, 4.5, (2, k)) * rng.choice([-1, 1], (2, k))
    ph = rng.uniform(0, 2 * math.pi, (2, k))
    a = rng.normal(0, amp / math.sqrt(k), (2, k))

    def f(p: np.ndarray) -> np.ndarray:
        x, y = p[:, :1], p[:, 1:2]
        dx = (a[0] * np.sin(fx[0] * x + fy[0] * y + ph[0])).sum(1)
        dy = (a[1] * np.sin(fx[1] * x + fy[1] * y + ph[1])).sum(1)
        return p + np.column_stack([dx, dy])

    return f


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
    elif kind == 1:  # drop a letter (stopped early)
        w = w[: i + 1]
    else:  # neighbour key
        c = w[i].lower()
        for row in _KEYS:
            j = row.find(c)
            if j >= 0:
                n = row[max(0, min(len(row) - 1, j + rng.choice([-1, 1])))]
                w[i] = n.upper() if w[i].isupper() else n
                break
    out = "".join(w)
    return out if out != word else word[:-1]


class Writer:
    def __init__(self, style: Style):
        self.st = style
        self.rng = np.random.default_rng(style.seed)
        self.font = HandFont(style.font)
        dpi = style.dpi
        self.W = int(style.page_inches[0] * dpi)
        self.H = int(style.page_inches[1] * dpi)
        self.xh = style.x_height_mm / 25.4 * dpi
        self.line_h = style.line_spacing_mm / 25.4 * dpi
        ml, mr, mt, mb = (m * dpi for m in style.margins_in)
        self.left, self.right, self.top, self.bottom = ml, self.W - mr, mt, self.H - mb
        # a ballpoint line is ~0.4mm; fonts thinner than that get fattened
        pen = max(style.pen_mm / 25.4 * dpi, 0.11 * self.xh)
        self.grow = max(0.0, (pen - self.font.stroke * self.xh) / 2)

    # ---- per-line state -------------------------------------------------
    def _messiness(self, page_progress: float) -> float:
        return self.st.messiness * (1 + self.st.fatigue * page_progress)

    def _start_line(self, idx: int, ruled_y: float):
        st, r = self.st, self.rng
        m = self._messiness((ruled_y - self.top) / max(1, self.bottom - self.top))
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

    def _baseline(self, x: float) -> float:
        return self.base_y + math.tan(self.angle) * (x - self.line_x0) + self.base_drift(x)

    # ---- glyphs ---------------------------------------------------------
    def _char_scale(self, x: float) -> float:
        return self.xh * (1 + self.size_drift(x))

    def _measure(self, word: str) -> float:
        s = self._char_scale(self.x) * self.st.tracking
        return sum(self.font.glyph(c)[1] for c in word) * s

    def _place_char(self, ch: str, ops: list) -> tuple[float, float]:
        st, r, m = self.st, self.rng, self.m
        contours, adv = self.font.glyph(ch)
        s = self._char_scale(self.x) * (1 + r.normal(0, st.glyph_scale_jitter * m))
        sx = 1 + r.normal(0, st.glyph_squash * m)
        slant = math.radians(st.slant_deg + self.slant_drift(self.x) + r.normal(0, st.glyph_slant_jitter * m))
        rot = math.radians(r.normal(0, st.glyph_rotation_jitter * m)) + self.angle
        by = self._baseline(self.x) + r.normal(0, st.glyph_baseline_jitter * m) * self.xh
        warp = _warp_fn(r, st.glyph_warp * math.sqrt(m))  # sqrt: sloppier, but stays legible
        cr, sr, sh = math.cos(rot), math.sin(rot), math.tan(slant)

        placed = []
        for c in contours:
            p = warp(c.copy())
            p[:, 0] = p[:, 0] * sx - p[:, 1] * sh
            x = p[:, 0] * cr - p[:, 1] * sr
            y = p[:, 0] * sr + p[:, 1] * cr
            placed.append(np.column_stack([x * s + self.x, y * s + by]))
        if placed:
            ops.append(Glyph(placed))
        x0 = self.x
        self.x += adv * s * st.tracking * sx * (1 + r.normal(0, st.glyph_spacing_jitter * m))
        return x0, self.x

    def _space(self):
        r = self.rng
        k = max(0.7, r.normal(1.0, 0.18 * self.m))
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
            ops.append(Stroke(np.column_stack([xs, ys]), width=r.uniform(0.11, 0.16) * self.xh))

    # ---- main -----------------------------------------------------------
    def write(self, text: str) -> list[Page]:
        pages: list[Page] = []
        ops: list = []
        baselines: list[float] = []
        line_idx = 0

        def ruled(i):
            return self.top + (i + 1) * self.line_h

        def new_page():
            nonlocal ops, baselines, line_idx
            if ops or baselines:
                pages.append(Page(ops, baselines, self.xh, self.grow))
            ops, baselines, line_idx = [], [], 0
            self._start_line(0, ruled(0))

        def new_line():
            nonlocal line_idx
            line_idx += 1
            y = ruled(line_idx)
            if self.st.paper == "blank":
                y += self.rng.normal(0, 0.1) * self.xh
            if y > self.bottom:
                new_page()
            else:
                self._start_line(line_idx, y)

        new_page()
        for pi, para in enumerate(text.replace("\r\n", "\n").split("\n")):
            if pi > 0:
                new_line()
            words = para.split(" ")
            first = True
            for word in words:
                if word == "":
                    self._space()
                    continue
                wrong = None
                if len(word) > 3 and self.rng.random() < self.st.mistakes * self.m:
                    wrong = _misspell(word, self.rng)
                need = self._measure(word) + (self._measure(wrong) + self.font.space * self.xh if wrong else 0)
                if not first and self.x + need > self.right_limit and self.x > self.line_x0:
                    new_line()
                elif not first:
                    self._space()
                first = False
                if wrong:
                    x0 = self.x
                    for ch in wrong:
                        self._place_char(ch, ops)
                    self._strike(x0, self.x, ops)
                    self._space()
                for ch in word:
                    self._place_char(ch, ops)
            baselines.append(self.base_y)
        pages.append(Page(ops, baselines, self.xh, self.grow))
        return pages
