"""Load a font and turn its glyphs into dense polylines we can deform."""
from __future__ import annotations

import numpy as np
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont


class _FlattenPen(BasePen):
    def __init__(self, glyphset, steps: int = 14):
        super().__init__(glyphset)
        self.steps = steps
        self.contours: list[np.ndarray] = []
        self._cur: list | None = None

    def _moveTo(self, p):
        self._cur = [p]

    def _lineTo(self, p):
        self._cur.append(p)

    def _curveToOne(self, p1, p2, p3):
        p0 = np.array(self._cur[-1], float)
        p1, p2, p3 = (np.array(p, float) for p in (p1, p2, p3))
        t = np.linspace(0, 1, self.steps + 1)[1:, None]
        pts = (1 - t) ** 3 * p0 + 3 * (1 - t) ** 2 * t * p1 + 3 * (1 - t) * t**2 * p2 + t**3 * p3
        self._cur.extend(map(tuple, pts))

    def _qCurveToOne(self, p1, p2):
        p0 = np.array(self._cur[-1], float)
        p1, p2 = np.array(p1, float), np.array(p2, float)
        t = np.linspace(0, 1, self.steps + 1)[1:, None]
        pts = (1 - t) ** 2 * p0 + 2 * (1 - t) * t * p1 + t**2 * p2
        self._cur.extend(map(tuple, pts))

    def _closePath(self):
        if self._cur and len(self._cur) > 2:
            self.contours.append(np.array(self._cur, float))
        self._cur = None

    _endPath = _closePath


def _resample(c: np.ndarray, step: float) -> np.ndarray:
    """Resample a closed contour at even spacing so straight segments can bend."""
    closed = np.vstack([c, c[:1]])
    seg = np.linalg.norm(np.diff(closed, axis=0), axis=1)
    cum = np.concatenate([[0], np.cumsum(seg)])
    total = cum[-1]
    if total == 0:
        return c
    n = max(8, int(total / step))
    s = np.linspace(0, total, n, endpoint=False)
    return np.column_stack([np.interp(s, cum, closed[:, 0]), np.interp(s, cum, closed[:, 1])])


class HandFont:
    """Glyph outlines normalised so that x-height == 1, baseline at y=0, y pointing down."""

    def __init__(self, path: str):
        self.path = path
        self.tt = TTFont(path)
        self.gs = self.tt.getGlyphSet()
        self.cmap = self.tt.getBestCmap()
        self.hmtx = self.tt["hmtx"]
        self._cache: dict[str, tuple[list[np.ndarray], float]] = {}

        upm = self.tt["head"].unitsPerEm
        xh = None
        if "x" in [chr(c) for c in self.cmap if c < 0x110000]:
            raw = self._raw("x")[0]
            if raw:
                ys = np.concatenate([c[:, 1] for c in raw])
                xh = ys.max() - max(ys.min(), 0)
        self.xheight = xh if xh and xh > 0 else 0.5 * upm

        self.stroke = self._stroke_width()
        self.space = max(self.glyph(" ")[1] if ord(" ") in self.cmap else 0.5, 0.55)

    def _stroke_width(self) -> float:
        """Average stroke thickness (in x-heights) estimated as 2*area/perimeter over a few letters."""
        widths = []
        for ch in "nomulie":
            if not self.has(ch):
                continue
            cs = self.glyph(ch)[0]
            if not cs:
                continue
            area = abs(sum(0.5 * np.sum(c[:, 0] * np.roll(c[:, 1], -1) - np.roll(c[:, 0], -1) * c[:, 1]) for c in cs))
            perim = sum(np.linalg.norm(np.diff(np.vstack([c, c[:1]]), axis=0), axis=1).sum() for c in cs)
            if perim > 0:
                widths.append(2 * area / perim)
        return float(np.median(widths)) if widths else 0.12

    def _raw(self, ch: str):
        name = self.cmap.get(ord(ch))
        if name is None:
            return None, 0
        pen = _FlattenPen(self.gs)
        self.gs[name].draw(pen)
        return pen.contours, self.hmtx[name][0]

    def has(self, ch: str) -> bool:
        return ord(ch) in self.cmap

    def glyph(self, ch: str) -> tuple[list[np.ndarray], float]:
        if ch in self._cache:
            return self._cache[ch]
        contours, adv = self._raw(ch)
        if contours is None:
            contours, adv = self._raw("?") if self.has("?") else ([], self.xheight * 0.5)
        k = 1.0 / self.xheight
        out = []
        for c in contours or []:
            c = c * k
            c[:, 1] *= -1  # y down
            out.append(_resample(c, 0.03))
        res = (out, adv * k)
        self._cache[ch] = res
        return res
