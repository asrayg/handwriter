# handwriter

Turns text into handwriting that looks like a real person wrote it with a real pen, not like a font.

Handwriting fonts look fake because every "a" is identical, every line is perfectly straight, and the ink is a flat, perfect fill. handwriter uses a font only as a skeleton. It writes as one consistent but imperfect **writer**, using a physically modelled **pen**.

## The writer

Variation happens in three layers, the same way it does in real handwriting:

| Layer | What varies |
|---|---|
| **Personal habits** (fixed for the whole document) | 2 personal variants of every letter, used in a habitual ratio · where the i/j dots land (usually right and high), and whether they're dots or little flicks · words trailing off (last letter smaller, drifting up or down) · first letter slightly bigger |
| **Slow drift** (per line) | baseline wander, line tilt, size and slant drift · fatigue: messier toward the bottom of the page |
| **Instance noise** (per letter) | small warp, scale, rotation, spacing jitter · hand tremor that roughens stroke edges |

It also crams a word in near the margin instead of wrapping when the word *almost* fits, and leaves a ragged right edge. Crossed-out typos are available but off by default (`--mistakes 0.01`).

## The pen

Ink is modelled as coverage × density with Beer–Lambert absorption, so overlapping strokes and blobs get darker and more saturated, like real dye.

| Effect | Based on |
|---|---|
| **Burr striations**: fine ink-free streaks running *along* the stroke (line-integral convolution over local stroke direction) | forensic descriptions of ballpoint lines [1][2] |
| **Gooping**: ink blobs at sharp turns, sitting near the stroke edge | [1][3] |
| **Inkless starts**: ink fades in on the first letter of a word as the ball primes | [2][4] |
| **Skips**: short gaps where the ball lost contact | [2] |
| **Heavier downstrokes**: vertical strokes darker than upstrokes | upstrokes have ~14% less pen pressure [5] |
| **Pressure**: stroke width *and* darkness drift smoothly as you write | |
| **Overlap darkening**: double ink where letters touch or cross | |
| **Paper**: fibre-direction texture, tooth (ink misses the valleys, strongly for pencil), bleed and feathering, edge pooling for wet inks | |

Pens: `ballpoint` (default), `gel`, `fountain` (direction-dependent broad nib + feathering), `pencil`, `marker`.

## Pen plotter / 3D printer G-code

Put a pen on your 3D printer and have it actually write the text. Default target: **Bambu Lab P1P** (profiles also for P1S, X1C, A1 and Ender 3).

```bash
python -m handwriter -f note.txt --page a5 --gcode note.gcode --bambu-template cube.gcode.3mf --pen-down-z 1.5
#  -> note.gcode, note.gcode.3mf (send this one), note_preview.png (what the pen will draw)
```

A pen draws lines, not filled shapes, so each letter is thinned to its centreline and traced into pen strokes. All the writer's variation is kept, and the real pen supplies the ink texture. Strokes are ordered to cut pen-up travel, and the page is centred on the bed.

### Bambu P1P setup
1. **Holder:** print a pen holder that clips to the toolhead. A spring-loaded one is very forgiving. The nozzle stays **cold**; the G-code turns all heaters and fans off.
2. **Template (once):** Bambu printers only accept `.gcode.3mf`. In Bambu Studio, slice any tiny model for the P1P, then *File → Export → Export plate sliced file* to get e.g. `cube.gcode.3mf`. handwriter copies it and swaps in its own G-code and checksum. Send the result via SD card or Bambu Studio. (If the printer rejects it, re-export the template in the current Studio version.)
3. **Homing:** the P1P homes Z by touching the *nozzle* to the bed centre. Keep the pen tip **above** the nozzle tip during homing (or use a spring-loaded holder), otherwise the pen hits the bed first. `--no-home` skips G28 if the printer is already homed.
4. **Calibrate Z:** after homing, jog Z down until the pen just touches the paper and note the Z value, then pass `--pen-down-z <that>`. Pen-up defaults to pen-down + 3 mm.
5. **Paper:** tape it to the plate, centred (or set `--offset XxY` for the page's bottom-left corner).

Plotter options:

```
--gcode FILE            write G-code (+ preview); one file per page
--printer p1p           p1p | p1s | x1c | a1 | ender3
--bambu-template F      plate-sliced .gcode.3mf from Bambu Studio -> also writes FILE.gcode.3mf
--page a5               must fit the bed (P1P 256x256: a5, a6, postcard, index, 250x250...; letter/A4 are too tall)
--pen-down-z Z          pen touching the paper (calibrate!)   --pen-up-z Z   travel height
--offset XxY            page bottom-left on the bed [centred]  --bed WxH   override bed size
--draw-speed 3000       mm/min while drawing                   --travel-speed 12000
--no-home               skip G28
```

Writing size matters for plotting. The default 2.4 mm x-height suits a 0.5 mm pen; use `--size 3` or more with thicker pens.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python -m handwriter "Hey, running late. Back by 6!" -o note.png
python -m handwriter -f letter.txt -o letter.pdf                     # multi-page PDF
python -m handwriter -f letter.txt -o letter.png --pen fountain --ink black
echo "hello" | python -m handwriter -o hi.png
```

Options:

```
--font Caveat|IndieFlower|NothingYouCouldDo|ReenieBeanie|RockSalt|path/to/font.ttf
--pen ballpoint|gel|fountain|pencil|marker
--ink blue|black|pencil|red|gel-blue      --paper lined|blank|grid
--messiness 1.0     0 = font-perfect, 1 = normal, 1.5 = rushed
--habits 1.0        strength of the writer's personal quirks
--pen-defects 1.0   0 = flawless pen, 2 = cheap pen
--fatigue 0.25      how much worse the bottom of the page gets
--mistakes 0        per-word chance of a crossed-out typo (off by default)
--slant 4           degrees, positive leans right
--size 2.4          x-height in mm     --line-spacing 7.1 (mm, college ruled)
--weight 0          -0.2 thin .. 0.2 thick
--seed N            same seed = same writer and same page
--page letter|a4|a5|a6|index|postcard|WxH   (mm)
--no-scan           clean digital output (no tilt/lighting/grain)
--dpi 300
--list-fonts
```

From Python:

```python
from handwriter import generate, Style
pages = generate("Dear Alex, ...", Style(font="fonts/IndieFlower.ttf", pen="gel", ink="black", seed=4))
pages[0].save("letter.png")
```

Every knob, including the pen physics table, is in `handwriter/style.py`.

## Most realistic results

1. **Use your own handwriting.** Make a font from your writing (Calligraphr is free), drop the .ttf in `fonts/`, then `--font YourName`.
2. Keep `messiness` between 0.8 and 1.3.
3. The seed sets the writer. Keep the same seed for one "person", and pick a different one for a different person.

## How it works

- `font.py`: loads glyph outlines with fontTools, flattens and resamples them so they can bend, normalises to x-height = 1, measures stroke width, and finds i/j dots.
- `layout.py`: the writer. Holds habits and allographs, per-line drift and per-letter noise, word wrap with margin cramming and typos, and records pen events (blobs, skips, faint starts).
- `render.py`: the pen and paper. Supersampled rasterisation, pressure-driven stroke width, structure-tensor stroke orientation, striations via LIC, Beer–Lambert ink, paper fibres and tooth, scan simulation.

## References

1. Ball pen stroke characteristics (gooping after curves, direction changes): *Ball Pens and Inks*, core.ac.uk/download/231003512.pdf
2. Characteristics of commonly used pens (inkless start, gooping, skipping, burr striations): aditya.ac.in forensic QD project
3. AAFS 2007 J2: ball housing defects and line defects
4. Variations in line quality of handwritten strokes, *Arab J. Forensic Sci. & Forensic Med.* (2022): ballpoint priming leaves un-inked starts
5. Caligiuri & Mohammed, kinematic analyses of stroke direction: upstrokes 19% longer, 15% faster, 14% lower pressure
6. Snape (1980), direction of ballpoint motion from burr striations in curved strokes
