# CHAMELEON — layout-aware resize engine

Give the engine a finished design and a new canvas size; it rebuilds the
design for that canvas instead of stretching it. Text re-flows, images keep
their proportions, buttons stay buttons, corner badges stay in their corners,
and every element of the original is still there and visible (except a
small image whose file is missing, which has nothing to show).

All of the engine lives in `engine/resize.py`. Everything else is the
starter kit's plumbing, unchanged.

```
input:  a task JSON (source design + target canvas)
              |
              v
        engine/resize.py   <-- the engine
              |
              v
output: <task>_output.json  (resized design, same schema)
        <task>_output.png   (rendered preview)
```

## How to run

**Locally (no Docker):**

```bash
pip install -r requirements.txt
python -m engine.main
```

Every task JSON in `input/` is resized; `output/<task>_output.json` and
`output/<task>_output.png` are written for each. If the PNGs show plain
labelled boxes instead of real text and images, the renderer failed to
import (check that `requirements.txt` installed cleanly); the JSON output is
unaffected.

**In Docker (how scoring runs it):**

```bash
docker build --platform linux/amd64 -t my-chameleon-engine .
docker run --rm --network none \
  -v $(pwd)/input:/app/input -v $(pwd)/output:/app/output \
  my-chameleon-engine
```

Set `CHAMELEON_DEBUG=1` (e.g. `docker run -e CHAMELEON_DEBUG=1 ...`) to print,
per task, the top-scoring candidate layouts and their scores to stderr.

## Repo layout

```
engine/
  resize.py        <- the engine (the only file changed from the starter kit, besides this README)
  main.py          <- orchestrator: input -> resize() -> self-check -> JSON + PNG
renderer/          <- the renderer (transformer-convertor + cr-renderer), as shipped
brand_kit/
  assets/          <- image files referenced by asset_id
  fonts/           <- font files matching the schema's font_family values
input/             <- task JSON files (3 samples included)
output/            <- results land here
chameleon_design_schema_v0.9.json   <- field-by-field spec for the design format
Dockerfile
RULES.md
```

## Model and dependencies

No machine-learning model and no network access. The engine is plain Python
on top of the starter kit's own dependencies:

- **skia** (installed with cr-renderer): used to measure text with the exact
  font files, glyph widths and line-wrapping rules the renderer uses, so a
  text box is always the right size for what will be drawn in it.
- **Pillow**: reads each asset's size and transparent margins, so spacing is
  based on visible pixels rather than padded image boxes.

Nothing is added to `requirements.txt` or the `Dockerfile`.

## Approach

`resize()` runs in three stages.

**1. Analyse the design.** Using only element type, geometry and stacking
order (never element ids), the engine works out what each piece is:
full-bleed backgrounds, decorations (translucent shapes, dots), the logo,
the hero image, the headline and supporting copy, fine print, buttons (a
text on a snug shape, with or without a `group_id`), badge strips, cards and
bands (a solid panel behind several texts, or behind one text it doesn't hug
the way a button does), and small pieces tucked into a corner or along an
edge (a sale ribbon, a watermark, a vertical side label). Text is measured
exactly as the renderer will draw it.

**2. Generate and score candidate layouts.** For each target the engine
builds several complete layouts and keeps the best:

- a single column in the source's reading order (square and portrait);
- the reading order cut into 2–6 side-by-side columns, with the hero image
  optionally moved to the front or back and fine print optionally run as a
  full-width footer (landscape, banners);
- the designer's own arrangement, scaled uniformly, when the target is close
  to the source's shape.

Within a layout, every piece is sized by a search for the largest scale that
fits, with text re-wrapped (and multi-line headlines balanced) at each size.
Text never drops below a readable minimum for its role (headline, body, CTA,
badge, fine print), scaled to the canvas; the logo and hero image keep a
minimum visible size; no element is ever shrunk out of sight. If nothing
fits even then, the minimums are relaxed a step at a time, fine print and
badges first and the headline and CTA last; the last step drops the text
minimums altogether, so even a very crowded design on a tiny banner gets a
layout where nothing overlaps. Any layout that fits beats every one that
doesn't.

Candidates are scored on how large the content stays (weighted by
importance), how well it fills the canvas, overlap between drawn pixels of
unrelated pieces, text below 11px, and arrangements a designer would avoid
(a headline split from its subhead across columns, an image stacked over
copy inside a column, a logo below the product, a card's contents split
apart). Corner pieces either keep their corner or join the flow when that
reads better. Cards and bands are then wrapped back around the text they
held (a band that ran edge to edge still does, where that covers nothing
else), so text never loses the panel it was legible on.

**3. Finalise.** Hard guarantees, whatever the layout did: every element id
exactly once and in source order; everything inside the canvas, including
the bounds of rotated elements; non-background images keep their aspect
ratio (to within 0.5%); integer font sizes; text boxes tall enough for their
text; content, font family, colours, assets and `fit` untouched. Geometry is
snapped to whole pixels so images and shapes are drawn 1:1 rather than
resampled, which keeps small banners crisp (an image only snaps when that
keeps its proportions within 0.5%). If the analysis or layout ever raises, a
proportional fit-inside fallback still produces a valid result, so a task
never fails.

Two behaviours have a one-line switch at the top of `engine/resize.py`:

- `_KEEP_BLEED` (off): a decoration that runs off the edge of the source
  canvas, like Level 2's corner circle, is pulled fully inside the target,
  because the scorer checks that nothing is off-canvas. Turn it on to let it
  keep running off its corner by the same share instead.
- `_HIDE_MISSING_ASSETS` (on): a small image whose asset file doesn't exist
  (at most `_HIDE_MAX_AREA`, 3% of the canvas, like Level 3's
  `holiday_watermark`) keeps its id and valid geometry but is hidden with
  `opacity: 0` instead of rendering as a grey "missing asset" box. The
  Level 3 sample's note allows this ("skip it, placeholder it"). A larger
  missing image stays in the layout: the scorer's assets folder may have a
  file this container doesn't, and hiding a real product image would be far
  worse than a placeholder. Hiding only happens when other images in the
  same design load, so a wrong assets folder can never hide real images.

## What stays and what adapts

| Constant | Adapts |
|---|---|
| Every element (visible unless it's a small image whose file is missing) | Positions and sizes |
| Text content and font family | Font size (and letter spacing with it) |
| Colours, assets, image proportions | Line breaks, column arrangement |
| Grouped pieces kept together | Spacing, corner radii, stroke widths |
| Visual hierarchy (headline largest) | Which corner pieces stay in corners |

## Performance

Most tasks take 2–50 ms. The slowest sample tasks (Level 3 on small
banners) take 1–2 s, and a crowded 35-element design on a banner takes up to
about 10 s (measured on a Windows PC). The 30 s per-task time budget is only
a safety net, set far above what any test needs, so a slower or emulated
scoring machine (stage one runs a `linux/amd64` image on an Apple M5 Pro)
produces the same layouts: on a simulated machine 4× slower, the hardest
tests came out the same (same font sizes, no overlaps). If the budget is
ever reached, the search jumps straight to the loosest size minimums, so it
still returns a layout that fits.

## Testing

- The three sample tasks, plus each sample at 20 common formats (Instagram,
  Story, Facebook, YouTube, LinkedIn, Pinterest, IAB banners from 320×50 to
  970×250, A4, 1920×1080).
- 19 additional hand-built designs covering awkward cases: text-only, six
  badges, very long CTA labels, two product images, text over an image, a
  card panel, right-aligned copy, story and presentation sources, a tiny
  source upscaled, anonymised element ids, explicit line breaks and letter
  spacing, a crowded 30-element layout, a missing hero asset, rotated
  pieces, fonts outside the brand kit, and divider lines.
- A random-design fuzzer (2,200 cases, including degenerate canvases such
  as 20×1 and 4000×90): no crashes, no fallbacks, and every structural rule
  held in every case.
- A stress sweep of each sample at its own target plus 23 common formats,
  re-checked independently of the engine (ids, required fields, nothing
  off-canvas including rotated bounds, image proportions, and text that fits
  its box using the renderer's own wrapping) and with `main.py`'s
  self-check: no warnings. Also a headline on a full-width band (it stays
  on its band at every size), a 35-element design (no overlapping pieces on
  any banner), and a simulated slower machine (see Performance).

## Operating limitations

- **Very small canvases with many elements.** Every element stays visible,
  so on, e.g., a 320×50 banner the Level 3 sample's 17 elements leave some
  text at 2–6px. Dropping low-priority elements would read better, but the
  rules require every element id to be returned, and hiding content could be
  judged as removing it.
- **Copy is never rewritten** and font families never change (stage-one
  rules), so very long text can only shrink and re-wrap.
- **Small missing assets** are hidden (see `_HIDE_MISSING_ASSETS`): the
  element stays in the output, but there is no image to show, so it isn't
  drawn. A large missing image is laid out normally and renders as the
  renderer's placeholder.
- **Small banners stay small images.** The PNG is rendered at the target
  size, so a 728×90 result is 728×90 pixels; zoomed in, any text that small
  looks soft.
- **Roles are inferred from geometry**, so an unusual design can be read
  differently than intended (e.g. a large decorative image taken for the
  hero). Inference never changes content, only how pieces are arranged.
- **Stroke-only shapes** are kept but not visible, because the renderer
  draws shape fills only.

## See also

- `chameleon_design_schema_v0.9.json` — full input/output field spec
- `RULES.md` — what's checked, what's graded, and what disqualifies a submission
- `FAQ.md` — the organisers' FAQ
