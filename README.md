# handwriter

Turns text into handwriting that looks like a real person wrote it, not like a font.

Handwriting fonts look fake because every "a" is identical, every line is perfectly straight, and the ink is perfectly uniform. handwriter uses a font only as a skeleton, then deforms **every glyph instance differently** and adds the mess a real hand makes:

| Real-hand trait | How it's simulated |
|---|---|
| No two letters identical | each glyph gets its own random smooth warp field, scale, squash, rotation, slant |
| Wandering baseline | per-line smooth drift + line tilt + per-letter bounce |
| Inconsistent size & slant | slow drift along each line |
| Uneven spacing | jittered letter and word gaps, ragged right margin |
| Pen pressure | stroke thickness *and* darkness vary smoothly across the page |
| Ink on paper | multiply blend (ruled lines show through), fibre bleed, paper tooth (strong for pencil) |
| Fatigue | writing gets messier toward the bottom of each page |
| Mistakes | the occasional misspelled word, scribbled out and rewritten |
| Scanned look | slight page tilt, uneven lighting, sensor grain |

Hairline fonts are automatically thickened to a realistic pen width.

## Setup

```bash
pip install -r requirements.txt
```

## Usage

```bash
python -m handwriter "Hey, running late. Back by 6!" -o note.png
python -m handwriter -f letter.txt -o letter.pdf          # multi-page PDF
python -m handwriter -f letter.txt -o letter.png          # letter.png, letter_2.png, ...
echo "hello" | python -m handwriter -o hi.png
```

Useful options:

```
--font Caveat|IndieFlower|NothingYouCouldDo|ReenieBeanie|RockSalt|path/to/font.ttf
--paper lined|blank|grid      --ink blue|black|pencil|red|gel-blue
--messiness 1.0   0 = font-perfect, 1 = normal, 1.5-2 = rushed
--fatigue 0.25    how much worse the bottom of the page gets
--mistakes 0.006  per-word chance of a crossed-out typo (0 to disable)
--slant 4         degrees, positive leans right
--size 2.4        x-height in mm      --line-spacing 7.1  (mm, college ruled)
--weight 0        -0.2 thin .. 0.2 thick
--seed N          same seed = same page
--no-scan         clean digital output (no tilt/lighting/grain)
--dpi 200
--list-fonts
```

From Python:

```python
from handwriter import generate, Style
pages = generate("Dear Alex, ...", Style(font="fonts/IndieFlower.ttf", ink="black", messiness=1.3, seed=4))
pages[0].save("letter.png")
```

Every knob is in `handwriter/style.py`.

## Most realistic results

1. **Use your own handwriting.** Make a font from your writing (Calligraphr is free), drop the .ttf in `fonts/`, then `--font YourName`.
2. Keep `messiness` between 0.8 and 1.4. Real people are less chaotic than you'd think.
3. Use a different `--seed` for each page so pages don't repeat.

## How it works

- `font.py`: loads glyph outlines with fontTools, flattens the curves, resamples into dense polylines so they can bend, normalises to x-height = 1, and measures stroke width.
- `layout.py`: word-wraps text and places every character with its own transform plus per-line drift functions. Adds strikethrough scribbles.
- `render.py`: rasterises at 3× supersampling, varies pressure by thresholding a blurred mask against a noise field, downsamples, and blends ink onto generated paper.

## Examples

See `examples/`.
