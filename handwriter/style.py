"""All the knobs that make writing look human. Every amount scales with `messiness`."""
from __future__ import annotations

from dataclasses import dataclass, field

INKS = {
    "blue": (28, 48, 128),
    "black": (22, 22, 28),
    "pencil": (85, 85, 92),
    "red": (150, 30, 35),
    "gel-blue": (20, 40, 170),
}


@dataclass
class Style:
    font: str = "fonts/Caveat.ttf"
    seed: int | None = None

    # page
    dpi: int = 200
    page_inches: tuple[float, float] = (8.5, 11.0)
    paper: str = "lined"  # lined | blank | grid
    margins_in: tuple[float, float, float, float] = (1.25, 0.6, 1.0, 0.7)  # left, right, top, bottom
    scan: bool = True  # slight page tilt, uneven lighting, grain

    # writing size
    x_height_mm: float = 2.4
    line_spacing_mm: float = 7.1  # college ruled
    tracking: float = 0.96  # letter spacing multiplier (people write tighter than fonts)
    word_spacing: float = 1.35
    slant_deg: float = 4.0  # positive = leans right

    # the human part
    messiness: float = 1.0  # global multiplier for all randomness
    fatigue: float = 0.25  # how much messier the bottom of a page is than the top
    mistakes: float = 0.006  # per-word chance of a crossed-out misspelling

    # ink
    ink: str = "blue"
    pen_mm: float = 0.35  # minimum pen line width; thin fonts are thickened to this
    weight: float = 0.0  # -0.2 thinner .. +0.2 thicker
    pressure_variation: float = 1.0

    # per-glyph variation (in x-heights / degrees), before messiness
    glyph_scale_jitter: float = 0.018
    glyph_rotation_jitter: float = 0.7
    glyph_slant_jitter: float = 1.2
    glyph_baseline_jitter: float = 0.018
    glyph_spacing_jitter: float = 0.04
    glyph_warp: float = 0.03
    glyph_squash: float = 0.025

    # per-line drift
    line_angle_deg: float = 0.3
    baseline_wave: float = 0.05
    size_wave: float = 0.035
    slant_wave_deg: float = 1.5

    extra: dict = field(default_factory=dict)

    @property
    def ink_rgb(self):
        return INKS.get(self.ink, INKS["blue"])
