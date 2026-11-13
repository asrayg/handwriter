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


@dataclass(frozen=True)
class Pen:
    """Physical behaviour of a writing instrument.

    Grounded in forensic descriptions of ink lines:
      - ballpoint: burr striations (ink-free streaks running along the stroke), gooping (ink blobs
        after curves / direction changes, often dragged to the stroke edge), inkless starts (the ball
        needs to prime when it first touches paper), heavier downstrokes than upstrokes.
      - gel: saturated, smooth, wetter edges, very few defects.
      - fountain: a flat-ish nib makes stroke width depend on direction; ink feathers into paper
        fibres and pools at stroke edges.
      - pencil: graphite only catches the peaks of the paper tooth -> grainy, grey, soft.
    """

    striation: float = 0.0  # 0..1 how much of the ink shows ink-free streaks
    goop: float = 0.0  # chance per glyph of an ink blob at a sharp turn
    inkless_start: float = 0.0  # chance per word that the first letter starts faint
    skip: float = 0.0  # chance per glyph of a short ink skip
    overlap: float = 0.3  # extra darkness where strokes cross / letters overlap
    direction: float = 0.12  # downstroke vs upstroke darkness contrast
    bleed: float = 0.3  # wicking into paper fibres
    pooling: float = 0.0  # darker edges where wet ink dries
    tooth: float = 0.08  # ink missing in paper valleys
    nib: float = 0.0  # fountain nib width as fraction of x-height (0 = round tip)
    nib_angle: float = 40.0
    density: float = 1.0  # overall optical density


PENS = {
    "ballpoint": Pen(striation=0.7, goop=0.06, inkless_start=0.22, skip=0.025, overlap=0.35, direction=0.14,
                     bleed=0.25, pooling=0.1, tooth=0.08),
    "gel": Pen(striation=0.05, goop=0.015, inkless_start=0.05, skip=0.006, overlap=0.18, direction=0.06,
               bleed=0.6, pooling=0.15, tooth=0.03, density=1.15),
    "fountain": Pen(striation=0.0, goop=0.0, inkless_start=0.0, skip=0.004, overlap=0.45, direction=0.2,
                    bleed=0.55, pooling=0.35, tooth=0.04, nib=0.07, density=0.9),
    "pencil": Pen(striation=0.0, goop=0.0, inkless_start=0.0, skip=0.0, overlap=0.45, direction=0.18,
                  bleed=0.0, tooth=0.28, density=0.8),
    "marker": Pen(striation=0.15, overlap=0.5, direction=0.03, bleed=0.8, pooling=0.25, tooth=0.02, density=1.1),
}


@dataclass
class Style:
    font: str = "fonts/Caveat.ttf"
    seed: int | None = None  # per-note randomness (None = different every time)
    writer_seed: int | None = 42  # who is writing: habits & letter shapes. 42 = the default hand

    # page
    dpi: int = 300
    page_inches: tuple[float, float] = (8.5, 11.0)
    paper: str = "blank"  # lined | blank | grid
    margins_in: tuple[float, float, float, float] = (1.25, 0.6, 1.0, 0.7)  # left, right, top, bottom
    scan: bool = True  # slight page tilt, uneven lighting, grain

    # writing size
    x_height_mm: float = 3.0
    line_spacing_mm: float = 8.5
    tracking: float = 0.96  # letter spacing multiplier (people write tighter than fonts)
    word_spacing: float = 1.35
    slant_deg: float = 4.0  # positive = leans right

    # the human part
    messiness: float = 1.0  # global multiplier for all randomness
    fatigue: float = 0.25  # how much messier the bottom of a page is than the top
    mistakes: float = 0.0  # per-word chance of a crossed-out misspelling (off by default)
    habits: float = 1.0  # strength of the writer's personal, consistent quirks
    tremor: float = 1.0  # tiny hand wobble roughening the stroke edges

    # pen & ink
    pen: str = "ballpoint"
    ink: str = "blue"
    pen_mm: float = 0.35  # minimum pen line width; thin fonts are thickened to this
    weight: float = 0.0  # -0.2 thinner .. +0.2 thicker
    pressure_variation: float = 1.0
    pen_defects: float = 1.0  # multiplier on striations / blobs / skips / faint starts

    # per-glyph variation (in x-heights / degrees), before messiness
    glyph_scale_jitter: float = 0.018
    glyph_rotation_jitter: float = 0.7
    glyph_slant_jitter: float = 1.2
    glyph_baseline_jitter: float = 0.018
    glyph_spacing_jitter: float = 0.04
    glyph_warp: float = 0.03
    glyph_squash: float = 0.025
    glyph_habit_warp: float = 0.05  # each writer's persistent per-letter deformation (their "allographs")

    # per-line drift
    line_angle_deg: float = 0.3
    baseline_wave: float = 0.05
    size_wave: float = 0.035
    slant_wave_deg: float = 1.5

    extra: dict = field(default_factory=dict)

    @property
    def ink_rgb(self):
        return INKS.get(self.ink, INKS["blue"])

    @property
    def pen_profile(self) -> Pen:
        name = "pencil" if self.ink == "pencil" else self.pen
        return PENS.get(name, PENS["ballpoint"])
