"""
CHAMELEON resize engine: "move the furniture, don't stretch the picture".

resize() runs in three stages:

  1. Analyse   (_Design)    Normalise every element, measure text exactly the
                            way the renderer will draw it, and work out what
                            each piece is (background, decoration, logo, hero
                            image, headline, button, badge, fine print...)
                            from its type, geometry and stacking alone.
  2. Lay out   (_layout)    Rebuild the design for the target canvas from
                            those pieces: text re-flows, images keep their
                            proportions, grouped pieces travel together,
                            corner pieces stay in their corners, cards wrap
                            their contents, and text and key images keep a
                            readable minimum size. Several candidate layouts
                            (one column, 2-6 side-by-side columns, the
                            source's own arrangement) are scored and the
                            best one wins.
  3. Finalise  (_finalise)  Hard guarantees that hold whatever the layout
                            did: every id exactly once, everything inside the
                            canvas, image aspect ratios kept, integer font
                            sizes, text boxes tall enough for their text.

If stages 1-2 ever raise, resize() falls back to a proportional "fit
inside" layout that still passes every structural check, so a task never
fails outright.
"""

from __future__ import annotations

import copy
import itertools
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:  # ships with cr-renderer; the renderer measures and draws text with it
    import skia  # type: ignore
except Exception:  # pragma: no cover - depends on the environment
    skia = None

try:
    from PIL import Image, ImageFont
except Exception:  # pragma: no cover
    Image = ImageFont = None

# Set CHAMELEON_DEBUG=1 to print the top-scoring candidate layouts per task.
_DEBUG = bool(os.environ.get("CHAMELEON_DEBUG"))

# Seconds of layout search per task; past it, the best layout found so far
# is used (typical tasks finish in well under half a second).
_TIME_BUDGET = 2.5

# A decoration that bleeds off the source canvas (like Level 2's corner
# circle) keeps bleeding by the same share. The starter README calls that
# sample's off-canvas warning "an expected one". Set False to force every
# element fully inside the canvas instead.
_KEEP_BLEED = True

# An image whose asset file is missing would render as a grey "missing
# asset" box. It is kept (same id, valid geometry) but hidden with opacity 0,
# which the Level 3 sample's note allows ("skip it, placeholder it"). Only
# done when other images in the same design load, i.e. the assets folder is
# the right one. Set False to keep the placeholder visible instead.
_HIDE_MISSING_ASSETS = True


def resize(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    """
    Args:
        source: {"canvas": {"width": int, "height": int}, "elements": [...]}
                 — the design at its original size (see
                 chameleon_design_schema_v0.9.json).
        target_canvas: {"width": int, "height": int} — the shape to resize into.

    Returns:
        {"canvas": target_canvas, "elements": [...]} with every source
        element exactly once, in source order, re-laid-out for the target.
        Only geometry (x/y/width/height), font_size, letter_spacing (in
        step with font_size) and shape corner/stroke sizes change; content,
        fonts, colours, assets and fit are untouched.
    """
    tw = max(1, int(round(_num(target_canvas.get("width"), 1))))
    th = max(1, int(round(_num(target_canvas.get("height"), 1))))
    try:
        design = _Design(source)
        return _finalise(design, tw, th, _layout(design, tw, th))
    except Exception as exc:  # never fail a task outright
        print(f"[resize] layout failed ({exc!r}); using proportional fallback", file=sys.stderr)
        return _fit_inside(source, tw, th)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _num(value: Any, default: float = 0.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class _Rect:
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def xywh(cls, x: float, y: float, w: float, h: float) -> "_Rect":
        return cls(x, y, x + w, y + h)

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def union(self, o: "_Rect") -> "_Rect":
        return _Rect(min(self.x0, o.x0), min(self.y0, o.y0), max(self.x1, o.x1), max(self.y1, o.y1))

    def overlap(self, o: "_Rect") -> float:
        """Area of the intersection."""
        return max(0.0, min(self.x1, o.x1) - max(self.x0, o.x0)) * max(0.0, min(self.y1, o.y1) - max(self.y0, o.y0))

    def intersect(self, o: "_Rect") -> "_Rect | None":
        r = _Rect(max(self.x0, o.x0), max(self.y0, o.y0), min(self.x1, o.x1), min(self.y1, o.y1))
        return r if r.w > 0 and r.h > 0 else None

    def contains_pt(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1

    def intersects(self, o: "_Rect", pad: float = 0.0) -> bool:
        return (self.x0 < o.x1 + pad and o.x0 < self.x1 + pad
                and self.y0 < o.y1 + pad and o.y0 < self.y1 + pad)


def _union_all(rects: list[_Rect]) -> _Rect:
    out = rects[0]
    for r in rects[1:]:
        out = out.union(r)
    return out


def _rot_aabb(x: float, y: float, w: float, h: float, deg: float) -> _Rect:
    """Axis-aligned bounds of a w*h box rotated `deg` about its centre
    (the renderer rotates about the element centre too)."""
    if not deg:
        return _Rect(x, y, x + w, y + h)
    a = math.radians(deg)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    hw, hh = (w * c + h * s) / 2, (w * s + h * c) / 2
    cx, cy = x + w / 2, y + h / 2
    return _Rect(cx - hw, cy - hh, cx + hw, cy + hh)


def _band_overlap(a: _Rect, b: _Rect) -> float:
    """Vertical overlap as a fraction of the shorter of the two."""
    inter = min(a.y1, b.y1) - max(a.y0, b.y0)
    return max(0.0, inter) / max(1e-6, min(a.h, b.h))


# ---------------------------------------------------------------------------
# Fonts and text: a faithful copy of how the renderer measures and wraps
# (renderer/converter/wrap.py + brand_fonts.py + cr_renderer's FontManager)
# ---------------------------------------------------------------------------

_FONT_WEIGHTS = {"thin", "light", "extralight", "regular", "medium", "semibold", "bold", "extrabold", "black"}
_FONT_STYLES = {"regular", "bold", "italic", "bolditalic"}
_BOLD_WEIGHTS = {"bold", "semibold", "extrabold", "black"}


def _brand_dir(name: str) -> Path | None:
    """brand_kit/<name>, resolved the way engine/main.py resolves it (from the
    working directory), falling back to this repo's root."""
    for base in (Path.cwd(), Path(__file__).resolve().parent.parent):
        p = base / "brand_kit" / name
        if p.is_dir():
            return p
    return None


def _norm_family(name: str) -> str:
    """cr_renderer.fonts.normalize_family, minus its table of legacy aliases."""
    name = name.replace("_", " ").title()
    for token in (" Bold", " Regular", " Light", " Italic", " Medium"):
        name = name.replace(token, "")
    return name


def _guess_advance(ch: str, serif: bool) -> float:
    """Rough advance width in em; only used when no font file is usable."""
    if ch == " ":
        return 0.27
    if ch in "il.,:;!|'":
        return 0.28
    if ch.isupper() or ch in "mwMW@":
        return 0.7
    if ch.isdigit():
        return 0.6
    return 0.52 if serif else 0.56


class _Font:
    """One font file, measured with skia exactly like the renderer (PIL and
    then a character-class estimate are fallbacks if skia is unavailable)."""

    def __init__(self, path: Path | None, family: str) -> None:
        self.path = path
        self.serif = "serif" in family.lower() and "sans" not in family.lower()
        self._typeface = None
        self._skia: dict[int, Any] = {}
        self._pil: dict[int, Any] = {}
        self._widths: dict[tuple[int, str], list[float]] = {}
        self._metrics: dict[int, tuple[float, float, float]] = {}
        if path is not None and skia is not None:
            try:
                # skia reads the font straight out of this buffer without
                # copying it, so the bytes must live as long as the typeface.
                self._data = path.read_bytes()
                self._typeface = skia.Typeface.MakeFromData(self._data)
            except Exception:
                self._typeface = None

    def _skia_font(self, size: int):
        f = self._skia.get(size)
        if f is None:
            f = self._skia[size] = skia.Font(self._typeface, size)
        return f

    def _pil_font(self, size: int):
        f = self._pil.get(size)
        if f is None:
            f = self._pil[size] = ImageFont.truetype(str(self.path), size)
        return f

    def widths(self, size: int, text: str) -> list[float]:
        key = (size, text)
        hit = self._widths.get(key)
        if hit is not None:
            return hit
        out = None
        if self._typeface is not None:
            try:
                f = self._skia_font(size)
                out = list(f.getWidths(f.textToGlyphs(text)))
            except Exception:
                out = None
        if out is None and self.path is not None and ImageFont is not None:
            try:
                f = self._pil_font(size)
                out = [f.getlength(c) for c in text]
            except Exception:
                out = None
        if out is None:
            out = [size * _guess_advance(c, self.serif) for c in text]
        self._widths[key] = out
        return out

    def metrics(self, size: int) -> tuple[float, float, float]:
        """(ascent, descent, cap height) in px, all positive."""
        m = self._metrics.get(size)
        if m is not None:
            return m
        if self._typeface is not None:
            try:
                fm = self._skia_font(size).getMetrics()
                m = (-fm.fAscent, fm.fDescent, fm.fCapHeight or 0.7 * size)
            except Exception:
                m = None
        if m is None and self.path is not None and ImageFont is not None:
            try:
                asc, desc = self._pil_font(size).getmetrics()
                m = (float(asc), float(desc), 0.7 * size)
            except Exception:
                m = None
        if m is None:
            m = (1.0 * size, 0.3 * size, 0.7 * size)
        self._metrics[size] = m
        return m


class _Fonts:
    """The brand-kit fonts folder, indexed like renderer/converter/brand_fonts.py
    and looked up with cr_renderer's FontManager.lookup() fallback rules."""

    _CAMEL = re.compile(r"(?<!^)(?=[A-Z])")

    def __init__(self) -> None:
        self.table: dict[str, list[tuple[str, str, Path]]] = {}
        self._faces: dict[tuple[Path | None, str], _Font] = {}
        folder = _brand_dir("fonts")
        if folder is None:
            return
        for path in sorted(folder.glob("*")):
            if path.suffix.lower() not in (".ttf", ".otf"):
                continue
            prefix, _, suffix = path.stem.partition("-")
            family = self._CAMEL.sub(" ", prefix).strip()
            suffix = suffix or "Regular"
            italic = "italic" in suffix.lower()
            token = re.sub("italic", "", suffix, flags=re.IGNORECASE).strip().lower() or "regular"
            weight = token if token in _FONT_WEIGHTS else "regular"
            if italic:
                style = "bolditalic" if weight == "bold" else "italic"
            else:
                style = "bold" if weight == "bold" else "regular"
            self.table.setdefault(_norm_family(family), []).append((weight, style, path))

    def lookup(self, family: str, weight: str, style: str) -> _Font:
        entries = self.table.get(_norm_family(family)) or self.table.get("Montserrat")
        if not entries and self.table:
            entries = next(iter(self.table.values()))
        path = None
        if entries:
            token = family.replace("_", " ").lower().split(" ")[-1]
            weight = token if token in _FONT_WEIGHTS else weight
            style = token if token in _FONT_STYLES else style
            path = next((p for w, s, p in entries if w == weight and s == style), entries[0][2])
        key = (path, family)
        face = self._faces.get(key)
        if face is None:
            face = self._faces[key] = _Font(path, family)
        return face


_FONTS: _Fonts | None = None


def _fonts() -> _Fonts:
    global _FONTS
    if _FONTS is None:
        _FONTS = _Fonts()
    return _FONTS


@dataclass(eq=False)
class _TextStyle:
    content: str
    font: _Font
    line_height: float
    letter_spacing: float   # px at the source font size
    base_fs: float          # the source font size
    align: str
    cache: dict = field(default_factory=dict)

    def tracking(self, fs: int) -> float:
        """Letter spacing at font size fs: tracking is a proportion of the
        type size, so it scales with it."""
        return round(self.letter_spacing * fs / self.base_fs, 2) if self.letter_spacing else 0.0


def _text_style(el: dict) -> _TextStyle:
    # Same weight/style derivation as renderer/converter/convert.py, so the
    # face measured here is the face the renderer wraps with.
    weight = str(el.get("font_weight") or "regular").lower()
    if weight not in _FONT_WEIGHTS:
        weight = "regular"
    bold = weight in _BOLD_WEIGHTS
    italic_raw = el.get("font_style") or el.get("italic")
    italic = italic_raw is True or italic_raw == "italic"
    style = "bolditalic" if bold and italic else "bold" if bold else "italic" if italic else "regular"
    return _TextStyle(
        content=str(el.get("content", "")),
        font=_fonts().lookup(str(el.get("font_family") or "Montserrat"), weight, style),
        line_height=_num(el.get("line_height"), 1.2),
        letter_spacing=_num(el.get("letter_spacing"), 0.0),
        base_fs=max(1.0, _num(el.get("font_size"), 24.0)),
        align=str(el.get("text_align") or "left"),
    )


def _wrap(text: str, widths: list[float], max_width: float) -> str:
    """Line-for-line port of renderer/converter/wrap.py:wrap_text (greedy,
    breaks only at spaces, a too-long word overflows its line)."""
    if not text or max_width <= 0:
        return text
    chars = list(text)
    line_width = 0.0
    last_space = None
    for i, c in enumerate(chars):
        if c == "\n":
            line_width = 0.0
            last_space = None
            continue
        w = widths[i] if i < len(widths) else 0.0
        if line_width + w > max_width and last_space is not None:
            chars[last_space] = "\n"
            line_width = sum(
                widths[j] for j in range(last_space + 1, i + 1) if j < len(widths) and chars[j] != "\n"
            )
            last_space = None
        else:
            line_width += w
        if c == " ":
            last_space = i
    return "".join(chars)


@dataclass
class _TextFit:
    """Text laid out at one font size and box width, as the renderer will
    draw it: lines from the top of the box, `font_size * line_height` apart."""
    fs: int
    width: float          # the box width it was wrapped against
    lines: list[str]
    line_w: list[float]   # drawn width of each line
    height: float         # box height that contains every line's glyph box
    ink_top: float        # box top -> cap height of the first line
    baseline: float       # box top -> baseline of the last line
    ink_bottom: float     # box top -> lowest ink of the last line

    @property
    def n(self) -> int:
        return len(self.lines)

    @property
    def text_w(self) -> float:
        return max(self.line_w, default=0.0)


def _measure(st: _TextStyle, fs: int, width: float) -> _TextFit:
    width = round(max(width, 0.0), 2)
    key = (fs, width)
    hit = st.cache.get(key)
    if hit is not None:
        return hit
    widths = st.font.widths(fs, st.content)
    wrapped = _wrap(st.content, widths, width)
    # The renderer gives each "\n" to the line after it and strips it.
    lines, line_w = [], []
    start = 0
    ls = st.tracking(fs)
    for i in range(len(wrapped) + 1):
        if i == len(wrapped) or wrapped[i] == "\n":
            seg = wrapped[start:i]
            w = sum(widths[start:i])
            if len(seg) > 1:
                w += ls * (len(seg) - 1)
            lines.append(seg)
            line_w.append(w)
            start = i + 1
    asc, desc, cap = st.font.metrics(fs)
    lead = fs * st.line_height
    n = len(lines)
    baseline = (n - 1) * lead + asc
    # Always allow for descenders: whether the last line happens to have one
    # depends on where it wrapped, and spacing shouldn't jump when it changes.
    fit = _TextFit(
        fs=fs,
        width=width,
        lines=lines,
        line_w=line_w,
        height=(n - 1) * lead + asc + desc,
        ink_top=max(0.0, asc - cap),
        baseline=baseline,
        ink_bottom=baseline + min(desc, 0.22 * fs),
    )
    st.cache[key] = fit
    return fit


def _fit_width(st: _TextStyle, fs: int, max_w: float, balance: bool = True) -> _TextFit:
    """Wrap at `max_w`, then shrink the box to the text: a single line is
    shrink-wrapped, several lines are balanced (the narrowest width that
    keeps the same line count), so headlines don't end on a lone word.
    The returned box is never narrower than its widest drawn line.
    Unbalanced fits are search-time estimates: their width is rounded down
    to a whole pixel (never optimistic) so repeated searches share a cache."""
    max_w = max(1.0, max_w if balance else math.floor(max_w))
    fit = _measure(st, fs, max_w)
    if fit.n == 1:
        w = fit.text_w + 0.5
    elif balance:
        lo, hi = 1.0, max_w
        for _ in range(16):
            mid = (lo + hi) / 2
            if _measure(st, fs, mid).n <= fit.n:
                hi = mid
            else:
                lo = mid
        w = hi + 0.5
    else:
        w = max_w
    for _ in range(4):
        got = _measure(st, fs, w)
        if got.text_w <= got.width + 0.01:
            return got
        w = got.text_w + 0.5
    return got


# ---------------------------------------------------------------------------
# Assets
# ---------------------------------------------------------------------------

@dataclass
class _Asset:
    exists: bool | None                         # None = no assets folder to check
    size: tuple[int, int] | None = None
    bbox: tuple[int, int, int, int] | None = None  # visible (non-transparent) pixels


_ASSETS: dict[str, _Asset] = {}


def _asset(asset_id: Any) -> _Asset:
    key = str(asset_id or "")
    hit = _ASSETS.get(key)
    if hit is not None:
        return hit
    folder = _brand_dir("assets")
    if folder is None or not key:
        info = _Asset(None)
    elif not (folder / key).is_file():
        info = _Asset(False)
    else:
        info = _Asset(True)
        if Image is not None:
            try:
                with Image.open(folder / key) as im:
                    info.size = im.size
                    info.bbox = (0, 0, im.size[0], im.size[1])
                    if "A" in im.getbands() or im.mode == "P":
                        alpha = im.convert("RGBA").getchannel("A").point(lambda v: 255 if v > 12 else 0)
                        info.bbox = alpha.getbbox() or info.bbox
            except Exception:
                pass
    _ASSETS[key] = info
    return info


def _content_frac(asset: _Asset | None, w: float, h: float, fit: str) -> tuple[float, float, float, float]:
    """Where the visible pixels of an image land inside its box, as
    (left, top, right, bottom) fractions, mirroring renderer/converter/assets.py."""
    if not asset or not asset.size or not asset.bbox or w <= 0 or h <= 0:
        return (0.0, 0.0, 1.0, 1.0)
    iw, ih = asset.size
    bx0, by0, bx1, by1 = asset.bbox
    if fit == "fill":
        return (bx0 / iw, by0 / ih, bx1 / iw, by1 / ih)
    sc = max(w / iw, h / ih) if fit == "cover" else min(w / iw, h / ih)
    ox, oy = (w - iw * sc) / 2, (h - ih * sc) / 2
    return (
        _clamp((ox + bx0 * sc) / w, 0, 1),
        _clamp((oy + by0 * sc) / h, 0, 1),
        _clamp((ox + bx1 * sc) / w, 0, 1),
        _clamp((oy + by1 * sc) / h, 0, 1),
    )


# ---------------------------------------------------------------------------
# Stage 1: analysis
# ---------------------------------------------------------------------------

_CTA_VERBS = {
    "shop", "buy", "order", "get", "try", "learn", "discover", "explore", "sign", "join", "book",
    "download", "start", "subscribe", "register", "claim", "save", "see", "view", "watch", "find",
    "call", "visit", "reserve", "apply", "grab", "taste", "read", "listen", "play", "donate",
}
_LOGO_HINTS = ("logo", "brand", "lockup", "wordmark", "monogram")

_IMPORTANCE = {
    "headline": 1.0, "hero": 0.9, "cta": 0.9, "logo": 0.85, "subhead": 0.65, "body": 0.55,
    "image": 0.6, "badge": 0.45, "ribbon": 0.45, "group": 0.5, "divider": 0.3,
    "fineprint": 0.25, "missing": 0.1,
}


@dataclass(eq=False)
class _Item:
    idx: int
    src: dict             # the element exactly as given
    el: dict              # shallow copy with defaults applied
    id: str
    type: str
    rect: _Rect
    rot: float
    z: float
    opacity: float
    group: str | None
    box: _Rect = None     # rect plus its rotated bounds
    vis: _Rect = None     # where its visible pixels are, source px
    fs: float = 0.0
    style: _TextStyle | None = None
    fit: _TextFit | None = None
    asset: _Asset | None = None
    frac: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    role: str = ""
    bleeds: bool = False  # runs off the source canvas


def _starts_with_verb(text: str) -> bool:
    words = re.findall(r"[a-zA-Z]+", text.lower())
    return bool(words) and words[0] in _CTA_VERBS and len(words) <= 6


@dataclass
class _Plan:
    """A block measured at some scale: its box size, where its visible
    content sits inside that box, and how to write it out."""
    w: float
    h: float
    ink_top: float
    ink_bottom: float
    emit: Callable[[float, float], dict[int, dict[str, Any]]]
    ink_left: float = 0.0
    ink_right: float | None = None  # None = the full width

    def ink(self, x: float, y: float) -> _Rect:
        """The visible content when the box is placed at (x, y)."""
        right = self.w if self.ink_right is None else self.ink_right
        return _Rect(x + self.ink_left, y + self.ink_top, x + right, y + self.ink_bottom)


def _shape_props(it: _Item, k: float, h: float | None = None) -> dict[str, Any]:
    """Corner radius / stroke width for a shape scaled by k. A pill stays a
    pill whatever its new height."""
    out: dict[str, Any] = {}
    if it.type != "shape":
        return out
    r = _num(it.el.get("corner_radius"), 0.0)
    if r > 0:
        if h is not None and it.rect.h > 0 and r >= 0.45 * min(it.rect.w, it.rect.h):
            out["corner_radius"] = round(h / 2, 2)
        else:
            out["corner_radius"] = round(r * k, 2)
    sw = _num(it.el.get("stroke_width"), 0.0)
    if sw > 0:
        out["stroke_width"] = round(max(1.0, sw * k), 2)
    return out


@dataclass(eq=False)
class _Block:
    """A unit the layout moves as one: a text, an image, a button (shape +
    label), a rigid group, or a divider line."""
    kind: str
    items: list[_Item]
    box: _Rect
    vis: _Rect
    role: str = ""
    importance: float = 0.5
    corner: str | None = None   # "tl"/"tr"/"bl"/"br" for corner pieces
    align: str = "center"
    grow: float = 1.0           # extra scale for the hero image
    cap_h: float = math.inf     # height limit for the hero image (column layouts)
    floor: float = 0.0          # smallest font size worth printing on this target
    k_floor: float = 0.0        # smallest scale for a logo/hero image on this target
    backdrop: int | None = None  # index into _Design.backdrops of the card it sits on
    container: _Item | None = None
    label: _Item | None = None
    pad_x: float = 0.0

    def measure(self, s: float, max_w: float, final: bool = True) -> _Plan:
        """Size this block at content scale s within max_w. `final=False`
        skips line balancing (same lines and height, only a wider box),
        which is all the layout searches need."""
        return getattr(self, "_measure_" + self.kind)(s, max(1.0, max_w), final)

    def _fs(self, it: _Item, s: float) -> int:
        """Font size at scale s, held at this target's floor for the role
        (but never pushed past the source's own size)."""
        return max(1, round(it.fs * s), round(min(self.floor, it.fs)))

    def _measure_text(self, s: float, max_w: float, final: bool) -> _Plan:
        it = self.items[0]
        fs = self._fs(it, s)
        # Wrap width follows the font size, so a floored font keeps the
        # source's line breaks rather than squeezing into a scaled box.
        want = min(max_w, max(1.0, it.rect.w * fs / it.fs))
        while True:
            fit = _fit_width(it.style, fs, want, balance=final)
            if fit.text_w <= max_w + 0.5 or fs == 1:
                break
            fs = max(1, min(fs - 1, int(fs * max_w / fit.text_w)))

        def emit(x: float, y: float) -> dict[int, dict[str, Any]]:
            return {it.idx: {"x": x, "y": y, "width": fit.width, "height": fit.height, "font_size": fs}}

        return _Plan(fit.width, fit.height, fit.ink_top, fit.ink_bottom, emit)

    def _measure_image(self, s: float, max_w: float, final: bool) -> _Plan:
        it = self.items[0]
        k = max(s * self.grow, self.k_floor)
        w, h = it.rect.w * k, it.rect.h * k
        if w > max_w:
            w, h = max_w, h * max_w / w
        if h > self.cap_h:
            w, h = w * self.cap_h / h, self.cap_h
        l, t, r, b = it.frac

        def emit(x: float, y: float) -> dict[int, dict[str, Any]]:
            return {it.idx: {"x": x, "y": y, "width": w, "height": h}}

        return _Plan(w, h, t * h, b * h, emit, l * w, r * w)

    def _measure_line(self, s: float, max_w: float, final: bool) -> _Plan:
        it = self.items[0]
        w, h = min(it.rect.w * s, max_w), max(1.0, it.rect.h * s)

        def emit(x: float, y: float) -> dict[int, dict[str, Any]]:
            return {it.idx: {"x": x, "y": y, "width": w, "height": h, **_shape_props(it, s)}}

        return _Plan(w, h, 0.0, h, emit)

    def _measure_button(self, s: float, max_w: float, final: bool) -> _Plan:
        c, lab = self.container, self.label
        multi = lab.fit.n > 1
        for _ in range(8):
            fs = self._fs(lab, s)
            if multi:
                fit = _fit_width(lab.style, fs, min(max_w, max(1.0, lab.rect.w * fs / lab.fs)), balance=final)
            else:
                fit = _fit_width(lab.style, fs, 1e6, balance=False)
            pad = max(self.pad_x * s, 0.5 * fs)
            cw = max(c.rect.w * s, fit.text_w + 2 * pad)
            ch = max(c.rect.h * s, (fit.baseline - fit.ink_top) + 0.9 * fs)
            if cw <= max_w + 0.5 or s < 1e-3:
                break
            s *= max(0.5, 0.99 * max_w / cw)
        extras = [it for it in self.items if it is not c and it is not lab]
        k = s

        def emit(x: float, y: float) -> dict[int, dict[str, Any]]:
            out = {c.idx: {"x": x, "y": y, "width": cw, "height": ch, **_shape_props(c, k, ch)}}
            if lab.style.align == "left":
                lx = x + pad
            elif lab.style.align == "right":
                lx = x + cw - pad - fit.width
            else:
                lx = x + (cw - fit.width) / 2
            # Optical centring: the span from cap height to the last baseline
            # sits on the button's centre line.
            ly = y + ch / 2 - (fit.ink_top + fit.baseline) / 2
            out[lab.idx] = {"x": lx, "y": ly, "width": fit.width, "height": fit.height, "font_size": fs}
            sx, sy = cw / max(c.rect.w, 1e-6), ch / max(c.rect.h, 1e-6)
            for it in extras:
                out[it.idx] = {
                    "x": x + (it.rect.x0 - c.rect.x0) * sx, "y": y + (it.rect.y0 - c.rect.y0) * sy,
                    "width": it.rect.w * sx, "height": it.rect.h * sy, **_shape_props(it, k),
                }
            return out

        return _Plan(cw, ch, 0.0, ch, emit)

    def _measure_rigid(self, s: float, max_w: float, final: bool, floors: bool = True) -> _Plan:
        k = s
        if floors:  # the whole group grows until its text reaches the floor
            for it in self.items:
                if it.type == "text":
                    k = max(k, min(self.floor, it.fs) / it.fs)
        if self.box.w * k > max_w:
            k = max_w / self.box.w
        w, h = self.box.w * k, self.box.h * k

        def emit(x: float, y: float) -> dict[int, dict[str, Any]]:
            out = {}
            for it in self.items:
                ex = x + (it.rect.x0 - self.box.x0) * k
                ey = y + (it.rect.y0 - self.box.y0) * k
                ew, eh = it.rect.w * k, it.rect.h * k
                p: dict[str, Any] = {"x": ex, "y": ey, "width": ew, "height": eh}
                if it.type == "text":
                    fs = max(1, round(it.fs * k))
                    fit = _measure(it.style, fs, ew)
                    if fit.n > it.fit.n or fit.text_w > ew:
                        wider = max(ew * 1.15, fit.text_w + 0.5)
                        p["x"], ew = ex - (wider - ew) / 2, wider
                        fit = _measure(it.style, fs, ew)
                    if fit.height > eh:  # grow about the centre (rotation pivot)
                        p["y"], eh = ey - (fit.height - eh) / 2, fit.height
                    p.update(width=ew, height=eh, font_size=fs)
                else:
                    p.update(_shape_props(it, k))
                out[it.idx] = p
            return out

        return _Plan(w, h, (self.vis.y0 - self.box.y0) * k, (self.vis.y1 - self.box.y0) * k, emit)


class _Design:
    """The source design, analysed: items with roles, backgrounds,
    decorations and the blocks the layout arranges."""

    def __init__(self, source: dict[str, Any]) -> None:
        canvas = source.get("canvas") or {}
        self.W = max(1.0, _num(canvas.get("width"), 1080))
        self.H = max(1.0, _num(canvas.get("height"), 1080))
        self.items = [self._item(i, el) for i, el in enumerate(source.get("elements") or [])]
        self.backgrounds: list[_Item] = []
        self.decor: list[_Item] = []
        self.backdrops: list[tuple[_Item, list[_Item]]] = []  # (card shape, what it held)
        self.hidden: list[_Item] = []                          # images with a missing asset
        self.blocks: list[_Block] = []
        self._classify()

    # -- items ---------------------------------------------------------------

    def _item(self, idx: int, src: dict) -> _Item:
        el = dict(src)
        x, y = _num(el.get("x")), _num(el.get("y"))
        w, h = max(0.0, _num(el.get("width"))), max(0.0, _num(el.get("height")))
        rot = _num(el.get("rotation"), 0.0)
        rot = (rot + 180.0) % 360.0 - 180.0
        etype = str(el.get("type", ""))
        opacity = _clamp(_num(el.get("opacity"), 1.0), 0.0, 1.0)
        it = _Item(
            idx=idx, src=src, el=el, id=str(el.get("id", idx)), type=etype,
            rect=_Rect.xywh(x, y, w, h), rot=rot, z=_num(el.get("z_index"), 0.0),
            opacity=opacity, group=(str(el["group_id"]) if el.get("group_id") not in (None, "") else None),
        )
        it.box = it.rect.union(_rot_aabb(x, y, w, h, rot))
        it.vis = it.rect
        it.bleeds = it.box.x0 < -0.5 or it.box.y0 < -0.5 or it.box.x1 > self.W + 0.5 or it.box.y1 > self.H + 0.5
        if etype == "text":
            it.fs = max(1.0, _num(el.get("font_size"), 24.0))
            it.style = _text_style(el)
            it.fit = _measure(it.style, max(1, int(it.fs)), w)
            tw = it.fit.text_w
            if it.style.align == "center":
                x0 = it.rect.cx - tw / 2
            elif it.style.align == "right":
                x0 = it.rect.x1 - tw
            else:
                x0 = it.rect.x0
            it.vis = _Rect(x0, y + it.fit.ink_top, x0 + tw, y + it.fit.ink_bottom)
        elif etype == "image":
            it.asset = _asset(el.get("asset_id"))
            it.frac = _content_frac(it.asset, w, h, str(el.get("fit") or "contain"))
            l, t, r, b = it.frac
            it.vis = _Rect(x + l * w, y + t * h, x + r * w, y + b * h)
        elif etype == "shape":
            fill = re.match(r"rgba?\([^,]+,[^,]+,[^,]+,\s*([\d.]+)\s*\)", str(el.get("fill_color", "")))
            if fill:
                it.opacity *= _clamp(_num(fill.group(1), 1.0), 0.0, 1.0)
        if rot:
            it.vis = _rot_aabb(it.vis.x0, it.vis.y0, it.vis.w, it.vis.h, rot)
        return it

    # -- classification -------------------------------------------------------

    def _classify(self) -> None:
        W, H = self.W, self.H
        area = W * H
        canvas = _Rect(0, 0, W, H)
        live = []
        for it in self.items:
            if it.type in ("image", "shape") and not it.rot and it.rect.overlap(canvas) >= 0.9 * area:
                it.role = "background"
                self.backgrounds.append(it)
            else:
                live.append(it)

        # Images whose asset file is missing leave the layout and are hidden
        # (see _HIDE_MISSING_ASSETS) - but only if other images do load, which
        # shows the assets folder being checked is the right one.
        if _HIDE_MISSING_ASSETS and any(it.asset and it.asset.exists for it in self.items):
            for it in live:
                if it.type == "image" and it.asset is not None and it.asset.exists is False:
                    it.role = "hidden"
                    self.hidden.append(it)
            live = [it for it in live if it.role != "hidden"]

        groups: dict[str, list[_Item]] = {}
        for it in live:
            if it.group is not None:
                groups.setdefault(it.group, []).append(it)
        groups = {k: v for k, v in groups.items() if len(v) > 1}
        singles = [it for it in live if it.group not in groups]

        # A large solid panel behind several texts (a card) is a backdrop:
        # it isn't laid out itself; once its contents have been placed, it
        # is wrapped around them again (see _wrap_backdrop).
        for sh in [it for it in singles if it.type == "shape" and not it.rot and it.opacity >= 0.3]:
            if sh.el.get("shape_type") == "line" or sh.rect.area < 0.06 * area:
                continue
            inside = [it for it in live if it is not sh and (it.z, it.idx) > (sh.z, sh.idx)
                      and sh.rect.contains_pt(it.vis.cx, it.vis.cy)
                      and it.vis.overlap(sh.rect) >= 0.8 * max(it.vis.area, 1.0)]
            if sum(1 for it in inside if it.type == "text") >= 2:
                sh.role = "backdrop"
                self.backdrops.append((sh, inside))
        singles = [it for it in singles if it.role != "backdrop"]

        # A text sitting on a snug, solid shape is a button/badge even
        # without a group_id.
        shapes = [it for it in singles if it.type == "shape" and not it.rot and it.opacity >= 0.3]
        owner: dict[int, str] = {}
        for t in (it for it in singles if it.type == "text" and not it.rot):
            best = None
            line = t.fit.height / t.fit.n
            for sh in shapes:
                if (sh.z, sh.idx) > (t.z, t.idx) or sh.rect.area <= 0:
                    continue
                if sh.rect.h > 3.5 * line * t.fit.n or sh.rect.w > t.fit.text_w + 8 * t.fs:
                    continue  # not snug around the text
                if t.vis.overlap(sh.rect) < 0.8 * t.vis.area or not sh.rect.contains_pt(t.vis.cx, t.vis.cy):
                    continue
                if best is None or sh.rect.area < best.rect.area:
                    best = sh
            if best is not None:
                key = owner.setdefault(best.idx, f"__auto{best.idx}")
                groups.setdefault(key, [best]).append(t)
        grouped = {id(m) for members in groups.values() for m in members}

        for members in groups.values():
            self.blocks.append(self._group_block(members))
        for it in singles:
            if id(it) in grouped:
                continue
            if it.type == "shape":
                thin = min(it.rect.w, it.rect.h) <= 0.012 * min(W, H) and max(it.rect.w, it.rect.h) >= 0.08 * min(W, H)
                if it.el.get("shape_type") == "line" or thin:
                    self.blocks.append(_Block("line", [it], it.box, it.vis))
                else:
                    it.role = "decor"
                    self.decor.append(it)
            elif it.type == "image" and it.opacity <= 0.35:
                it.role = "decor"
                self.decor.append(it)
            elif it.type in ("text", "image"):
                kind = "rigid" if it.rot else it.type
                self.blocks.append(_Block(kind, [it], it.box, it.vis))
            else:  # unknown type: keep it, move it with the layout
                self.blocks.append(_Block("rigid", [it], it.box, it.vis))

        self._assign_roles()
        self._find_corners()
        for k, (_, inside) in enumerate(self.backdrops):
            held = {id(it) for it in inside}
            for b in self.blocks:
                if any(id(it) in held for it in b.items):
                    b.backdrop = k

    def _group_block(self, members: list[_Item]) -> _Block:
        members = sorted(members, key=lambda m: (m.z, m.idx))
        box = _union_all([m.box for m in members])
        vis = _union_all([m.vis for m in members])
        texts = [m for m in members if m.type == "text"]
        shapes = [m for m in members if m.type == "shape"]
        if (not any(m.rot for m in members) and len(texts) == 1 and shapes
                and all(m.type in ("text", "shape") for m in members)):
            t = texts[0]
            holders = [s for s in shapes if s.rect.contains_pt(t.vis.cx, t.vis.cy)]
            if holders:
                c = max(holders, key=lambda s: s.rect.area)
                b = _Block("button", members, box, box)
                b.container, b.label = c, t
                b.pad_x = max(0.0, (c.rect.w - t.fit.text_w) / 2)
                return b
        return _Block("rigid", members, box, vis)

    def _assign_roles(self) -> None:
        W, H = self.W, self.H
        area = W * H

        def prominence(b: _Block) -> float:
            it = b.items[0]
            return it.fs * (1.12 if str(it.el.get("font_weight", "")).lower() in _BOLD_WEIGHTS else 1.0)

        texts = [b for b in self.blocks if b.kind == "text"]
        if texts:
            top = max(texts, key=lambda b: (prominence(b), -b.vis.y0))
            pmax, fmax = prominence(top), top.items[0].fs
            for b in texts:
                it = b.items[0]
                if b is top:
                    b.role = "headline"
                elif _starts_with_verb(it.style.content):
                    b.role = "cta"
                elif prominence(b) >= 0.6 * pmax:
                    b.role = "subhead"
                elif (it.fs <= 0.45 * fmax and it.fs <= 0.02 * min(W, H)
                      and (b.vis.cy >= 0.75 * H or len(it.style.content) >= 60)):
                    b.role = "fineprint"
                else:
                    b.role = "body"

        buttons = [b for b in self.blocks if b.kind == "button"]
        if buttons:
            def cta_score(b: _Block) -> float:
                bonus = 1.5 if _starts_with_verb(b.label.style.content) else 1.0
                return b.container.rect.area * bonus
            cta = max(buttons, key=cta_score)
            for b in buttons:
                b.role = "cta" if b is cta else "badge"

        images = [b for b in self.blocks if len(b.items) == 1 and b.items[0].type == "image"]
        present = []
        for b in images:
            it = b.items[0]
            if it.asset is not None and it.asset.exists is False:
                b.role = "missing"
                continue
            name = str(it.el.get("asset_id", "")).lower()
            aspect = it.rect.w / max(it.rect.h, 1e-6)
            small = b.box.area <= 0.08 * area
            if any(h in name for h in _LOGO_HINTS) or (small and aspect >= 1.6) or (
                    b.box.area <= 0.03 * area and b.vis.cy <= 0.25 * H):
                b.role = "logo"
            else:
                present.append(b)
        if present:
            hero = max(present, key=lambda b: b.box.area)
            for b in present:
                b.role = "hero" if b is hero and b.box.area >= 0.04 * area else "image"

        for b in self.blocks:
            if not b.role:
                b.role = "divider" if b.kind == "line" else "ribbon" if any(m.rot for m in b.items) else "group"
            b.importance = _IMPORTANCE.get(b.role, 0.5)
            for m in b.items:
                m.role = m.role or b.role

    def _find_corners(self) -> None:
        """Small pieces tucked into a canvas corner (a sale ribbon, a badge,
        a watermark) stay in that corner instead of joining the main flow."""
        W, H = self.W, self.H
        for b in self.blocks:
            if b.role in ("headline", "hero", "divider"):
                continue
            cx, cy = b.vis.cx / W, b.vis.cy / H
            # A label turned on its side along the left/right edge stays on
            # that edge, centred vertically ("ml"/"mr").
            if all(60 <= abs(m.rot) <= 120 for m in b.items) and (cx <= 0.15 or cx >= 0.85):
                b.corner = "m" + ("l" if cx < 0.5 else "r")
                continue
            if not ((cx <= 0.3 or cx >= 0.7) and (cy <= 0.2 or cy >= 0.8)):
                continue
            if b.box.area > 0.08 * W * H:
                continue
            # Part of a row of similar pieces (e.g. a badge strip): keep the row.
            if any(o is not b and o.kind == b.kind and _band_overlap(o.vis, b.vis) >= 0.5 for o in self.blocks):
                continue
            b.corner = ("t" if cy < 0.5 else "b") + ("l" if cx < 0.5 else "r")
        for b in self.blocks:
            if abs(b.vis.cx - W / 2) <= 0.06 * W:
                b.align = "center"
            else:
                b.align = "left" if b.vis.x0 <= W - b.vis.x1 else "right"

    @property
    def flow(self) -> list[_Block]:
        return [b for b in self.blocks if b.corner is None]

    @property
    def corners(self) -> list[_Block]:
        return [b for b in self.blocks if b.corner is not None]

    def margin_ratio(self) -> float:
        """The source's outer margin (closest content to an edge) as a
        fraction of its short side, clamped to a sensible range."""
        flow = self.flow
        if not flow:
            return 0.05
        d = min(min(b.vis.x0, b.vis.y0, self.W - b.vis.x1, self.H - b.vis.y1) for b in flow)
        return _clamp(d / min(self.W, self.H), 0.035, 0.08)


# ---------------------------------------------------------------------------
# Stage 2: layout
#
# Several candidate layouts are built for the target and scored, and the
# best one wins (_layout). Candidates:
#   stack    one column in the source's reading order (the natural choice
#            for square and portrait targets)
#   columns  the reading order split into 2-4 side-by-side columns, with
#            the hero image optionally moved to the front or the back, and
#            fine print optionally run as a full-width footer (landscape)
# In each, corner pieces either keep to their corners or join the flow.
# ---------------------------------------------------------------------------

@dataclass(eq=False)
class _Row:
    """Flow blocks that sit side by side in the source (e.g. a badge strip)."""
    blocks: list[_Block]
    vis: _Rect
    align: str
    hgap: float
    cache: dict = field(default_factory=dict)

    @property
    def flexible(self) -> bool:
        """Can the row get narrower (text re-flows, a strip wraps)?"""
        return len(self.blocks) > 1 or any(b.kind == "text" for b in self.blocks)

    @property
    def natural_w(self) -> float:
        """Source width of the row's boxes laid end to end."""
        return sum(b.box.w for b in self.blocks) + self.hgap * (len(self.blocks) - 1)

    @property
    def min_h(self) -> float:
        """Lower bound on the row's source height: one line of any text."""
        return max(b.items[0].fit.height / b.items[0].fit.n if b.kind == "text" else b.box.h
                   for b in self.blocks)


def _same_row(a: _Block, b: _Block) -> bool:
    """Side by side at a similar height (a tall picture beside a column of
    text is a column layout, not a row)."""
    apart = a.vis.x1 <= b.vis.x0 or b.vis.x1 <= a.vis.x0
    shared = max(0.0, min(a.vis.y1, b.vis.y1) - max(a.vis.y0, b.vis.y0))
    return apart and shared >= 0.5 * max(a.vis.h, b.vis.h)


def _rows(d: _Design, blocks: list[_Block]) -> list[_Row]:
    rows: list[list[_Block]] = []
    for b in sorted(blocks, key=lambda b: (b.vis.y0, b.vis.x0)):
        for row in rows:
            if all(_same_row(o, b) for o in row):
                row.append(b)
                break
        else:
            rows.append([b])
    out = []
    for row in rows:
        row.sort(key=lambda b: b.vis.x0)
        vis = _union_all([b.vis for b in row])
        gaps = [row[i + 1].vis.x0 - row[i].vis.x1 for i in range(len(row) - 1)]
        if len(row) == 1:
            align = row[0].align
        elif abs(vis.cx - d.W / 2) <= 0.06 * d.W:
            align = "center"
        else:
            align = "left" if vis.x0 <= d.W - vis.x1 else "right"
        out.append(_Row(row, vis, align, max(min(gaps), 0.0) if gaps else 0.0))
    out.sort(key=lambda r: r.vis.y0)
    return out


def _measure_row(d: _Design, row: _Row, s: float, max_w: float, final: bool = True) -> _Plan:
    # Candidate searches re-measure the same row at the same scales a lot.
    key = (round(s, 6), round(max_w, 2), final, tuple((b.grow, b.cap_h) for b in row.blocks))
    hit = row.cache.get(key)
    if hit is None:
        hit = row.cache[key] = _measure_row_uncached(d, row, s, max_w, final)
    return hit


def _measure_row_uncached(d: _Design, row: _Row, s: float, max_w: float, final: bool) -> _Plan:
    def line_w(ln: list[_Plan], g: float) -> float:
        return sum(p.w for p in ln) + g * (len(ln) - 1)

    # Pieces pushed to opposite edges in the source keep a sensible gap, not
    # the whole width between them.
    short = min(d.W, d.H)
    base_gap = _clamp(row.hgap, 0.015 * short, 0.08 * short)
    plans = [b.measure(s, max_w, final) for b in row.blocks]
    gap = base_gap * s
    # A strip that is a little too wide shrinks to fit (up to 20%) rather
    # than wrapping; one that is far too wide wraps into even lines.
    k = s
    for _ in range(3):
        over = line_w(plans, gap)
        if len(plans) < 2 or over <= max_w + 0.5 or k < 0.8 * s:
            break
        k *= max_w / over * 0.995
        plans = [b.measure(k, max_w, final) for b in row.blocks]
        gap = base_gap * k
    if line_w(plans, gap) > max_w + 0.5 and k < 0.8 * s:
        plans = [b.measure(s, max_w, final) for b in row.blocks]
        gap = base_gap * s
    lines = [plans]
    if line_w(plans, gap) > max_w + 0.5:
        for count in range(2, len(plans) + 1):
            per = math.ceil(len(plans) / count)
            lines = [plans[i:i + per] for i in range(0, len(plans), per)]
            if all(line_w(ln, gap) <= max_w + 0.5 for ln in lines):
                break
    widths = [line_w(ln, gap) for ln in lines]
    row_w = max(widths)
    placed: list[tuple[_Plan, float, float]] = []
    y = vgap = 0.0
    for ln, lw in zip(lines, widths):
        lh = max(p.h for p in ln)
        x = {"left": 0.0, "right": row_w - lw}.get(row.align, (row_w - lw) / 2)
        for p in ln:
            placed.append((p, x, y + (lh - p.h) / 2))
            x += p.w + gap
        vgap = max(0.015 * short * s, min(gap, 0.3 * lh))  # wrapped lines sit close
        y += lh + vgap
    row_h = y - vgap
    inks = [p.ink(ox, oy) for p, ox, oy in placed]

    def emit(x: float, y: float) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for p, ox, oy in placed:
            out.update(p.emit(x + ox, y + oy))
        return out

    return _Plan(row_w, row_h, min(r.y0 for r in inks), max(r.y1 for r in inks), emit,
                 min(r.x0 for r in inks), max(r.x1 for r in inks))


def _stack(d: _Design, rows: list[_Row], s: float, max_w: float,
           final: bool = True) -> tuple[float, list[_Plan], list[float]]:
    """Measure rows stacked top to bottom at scale s. Gaps keep the source's
    visual rhythm (measured between visible pixels, not boxes), within
    limits: very loose source spacing counts as spare room, not as a gap
    the layout must reproduce."""
    plans = [_measure_row(d, r, s, max_w, final) for r in rows]
    short = min(d.W, d.H)
    gaps = []
    for k in range(1, len(rows)):
        want = _clamp(rows[k].vis.y0 - rows[k - 1].vis.y1, 0.02 * short, 0.06 * short) * s
        prev, nxt = plans[k - 1], plans[k]
        gaps.append(max(0.0, want - (prev.h - prev.ink_bottom) - nxt.ink_top))
    return sum(p.h for p in plans) + sum(gaps), plans, gaps


def _fit_stack(d: _Design, rows: list[_Row], area: _Rect, s_hi: float):
    """Largest scale (up to s_hi) at which the stacked rows fit `area`,
    then let the hero image soak up some of any spare height."""
    for b in (b for r in rows for b in r.blocks):
        b.grow, b.cap_h = 1.0, math.inf
    if not rows:
        return s_hi, 0.0, [], []

    def height(s: float) -> float:
        return _stack(d, rows, s, area.w, final=False)[0]

    s = s_hi
    if height(s) > area.h:
        lo, hi = s_hi * 0.02, s_hi
        for _ in range(18):
            mid = (lo + hi) / 2
            lo, hi = (mid, hi) if height(mid) <= area.h else (lo, mid)
        s = lo

    heroes = [b for r in rows for b in r.blocks if b.role == "hero"]
    spare = area.h - height(s)
    if heroes and spare > 0.08 * area.h:
        budget = area.h - 0.5 * spare
        lo, hi = 1.0, 1.8
        for _ in range(12):
            mid = (lo + hi) / 2
            for b in heroes:
                b.grow = mid
            lo, hi = (mid, hi) if height(s) <= budget else (lo, mid)
        for b in heroes:
            b.grow = lo
    total, plans, gaps = _stack(d, rows, s, area.w)
    if total > area.h + 0.5 >= height(s):  # balancing must never cost a fit
        total, plans, gaps = _stack(d, rows, s, area.w, final=False)
    return s, total, plans, gaps


def _row_positions(d: _Design, rows: list[_Row], plans: list[_Plan], gaps: list[float],
                   total: float, area: _Rect, s: float) -> list[tuple[float, float]]:
    """Top-left of each row. Spare height is shared between the top, the
    gaps and the bottom in the same proportions as the source's spacing,
    except that no gap grows past three times its natural size (a tall,
    narrow canvas would otherwise leave pieces floating far apart); what
    that holds back goes to the top and bottom instead."""
    if not rows:
        return []
    spare = max(0.0, area.h - total)
    floor = 0.01 * d.H
    short = min(d.W, d.H)
    src = [rows[0].vis.y0]
    src += [max(rows[k].vis.y0 - rows[k - 1].vis.y1, 0.0) for k in range(1, len(rows))]
    src.append(d.H - rows[-1].vis.y1)
    weights = [max(v, floor) for v in src]
    # Keep the composition from going lopsided on a taller canvas: neither
    # end margin takes more than twice the other's share of the spare room.
    weights[0] = min(weights[0], 2 * weights[-1])
    weights[-1] = min(weights[-1], 2 * weights[0])
    extra = [spare * w / sum(weights) for w in weights]
    held = 0.0
    for k in range(1, len(rows)):
        cap = 2.0 * _clamp(src[k], 0.02 * short, 0.06 * short) * s
        if extra[k] > cap:
            held += extra[k] - cap
            extra[k] = cap
    ends = weights[0] + weights[-1]
    extra[0] += held * weights[0] / ends
    extra[-1] += held * weights[-1] / ends
    out = []
    y = area.y0 + extra[0]
    for k, (row, plan) in enumerate(zip(rows, plans)):
        x = {"left": area.x0, "right": area.x1 - plan.w}.get(row.align, area.x0 + (area.w - plan.w) / 2)
        out.append((x, y))
        y += plan.h
        if k < len(gaps):
            y += gaps[k] + extra[k + 1]
    return out


def _place_corner(d: _Design, b: _Block, s: float, tw: int, th: int, m: float) -> tuple[_Plan, _Rect]:
    side = b.corner[0] == "m"
    plan = b.measure(s, (0.2 if side else 0.45) * tw)
    tallest = (0.8 if side else 0.3) * th
    if plan.h > tallest:
        plan = b.measure(s * tallest / plan.h, (0.2 if side else 0.45) * tw)
    off_x = _clamp((b.box.x0 if b.corner[1] == "l" else d.W - b.box.x1) * s, 0.5 * m, 2 * m)
    x = off_x if b.corner[1] == "l" else tw - off_x - plan.w
    if side:
        y = (th - plan.h) / 2
    else:
        off_y = _clamp((b.box.y0 if b.corner[0] == "t" else d.H - b.box.y1) * s, 0.5 * m, 2 * m)
        y = off_y if b.corner[0] == "t" else th - off_y - plan.h
    return plan, _Rect.xywh(x, y, plan.w, plan.h)


def _place_decor(d: _Design, it: _Item, sd: float, tw: int, th: int) -> dict[str, Any]:
    """Decorations keep their relationship to the canvas edges. On each axis
    a piece stays the same (scaled) distance from its nearer edge, so one
    that bleeds off a corner bleeds off that corner by the same share (see
    _KEEP_BLEED); a piece near the middle stays near the middle; a band that
    spans the canvas keeps spanning it."""
    r = it.rect
    band = it.type == "shape" and it.el.get("shape_type", "rectangle") == "rectangle"

    def place(near: float, far: float, size_src: float, total_src: float, total: int) -> tuple[float, float]:
        """Start and size along one axis; near/far are the source distances
        to the two edges (negative when bleeding off that edge)."""
        size = size_src * sd
        if band and near <= 1 and far <= 1:
            return near * sd, total - (near + far) * sd
        centre = (near + size_src / 2) / total_src
        if 0.35 <= centre <= 0.65:
            return centre * total - size / 2, size
        if near <= far:
            return near * sd, size
        return total - far * sd - size, size

    x, w = place(r.x0, d.W - r.x1, r.w, d.W, tw)
    y, h = place(r.y0, d.H - r.y1, r.h, d.H, th)
    if not (_KEEP_BLEED and it.bleeds):
        if band:
            w, h = min(w, tw), min(h, th)
        else:
            f = min(1.0, tw / max(w, 1e-6), th / max(h, 1e-6))
            w, h = w * f, h * f
        x, y = _clamp(x, 0, tw - w), _clamp(y, 0, th - h)
    return {"x": x, "y": y, "width": w, "height": h, **_shape_props(it, sd)}


@dataclass
class _Ctx:
    """Target-specific constants shared by every candidate."""
    d: _Design
    tw: int
    th: int
    mx: float      # side margin
    my: float      # top/bottom margin
    gutter: float  # minimum space between columns
    clear: float   # minimum clearance between unrelated pieces
    s_hi: float    # the largest content scale worth using

    @property
    def area(self) -> _Rect:
        return _Rect(self.mx, self.my, self.tw - self.mx, self.th - self.my)


def _ctx(d: _Design, tw: int, th: int) -> _Ctx:
    m = d.margin_ratio() * min(tw, th)
    rx, ry = tw / d.W, th / d.H
    return _Ctx(
        d, tw, th,
        mx=max(m, min(0.035 * tw, 2.5 * m)), my=m,
        gutter=max(2 * m, 0.03 * tw), clear=0.02 * min(tw, th),
        # Content may grow with the canvas, but not past the geometric mean
        # of the two axis ratios (+10%), so a tall canvas doesn't blow text up.
        s_hi=min(max(rx, ry), 1.1 * math.sqrt(rx * ry)),
    )


@dataclass
class _Candidate:
    name: str
    s: float                            # content scale it achieved
    columns: int
    merged: bool                        # corner pieces joined the flow
    props: dict[int, dict[str, Any]]
    penalty: float = 0.0                # structural penalty (see _structure_penalty)
    score: float = -math.inf
    overflow: bool = False              # content didn't fit even at the smallest scale
    floored: bool = True                # honours the minimum text/image sizes


def _stack_orders(rows: list[_Row]) -> list[tuple[list[_Row], float]]:
    """Row orders worth trying for a single column, with their penalty. If
    the hero image interrupts the copy in reading order (it sat beside the
    copy in the source), also try it just before and just after the copy."""
    copy_roles = _MESSAGE | {"cta"}
    hero = next((r for r in rows if any(b.role == "hero" for b in r.blocks)), None)
    is_copy = [any(b.role in copy_roles for b in r.blocks) for r in rows]
    if hero is None:
        return [(rows, 0.0)]
    h = rows.index(hero)
    if not (any(is_copy[:h]) and any(is_copy[h + 1:])):
        return [(rows, 0.0)]
    # Only when it sat beside the copy: a hero stacked between copy in the
    # same column is a deliberate order.
    copy_vis = _union_all([r.vis for r, cp in zip(rows, is_copy) if cp])
    if min(hero.vis.x1, copy_vis.x1) - max(hero.vis.x0, copy_vis.x0) > 0.3 * hero.vis.w:
        return [(rows, 0.0)]
    out = [(rows, 0.06)]
    rest = [r for r in rows if r is not hero]
    copy_at = [i for i, r in enumerate(rest) if any(b.role in copy_roles for b in r.blocks)]
    seen = {tuple(map(id, rows))}
    for at in (copy_at[0], copy_at[-1] + 1):
        order = rest[:at] + [hero] + rest[at:]
        key = tuple(map(id, order))
        if key not in seen:
            seen.add(key)
            out.append((order, 0.01))
    return out


def _stack_candidate(c: _Ctx, merged: bool, rows: list[_Row], penalty: float) -> _Candidate:
    """One column. Corner pieces that would collide with it claim a band
    along their edge, pushing the column down (or up, or in from a side)."""
    d, tw, th = c.d, c.tw, c.th
    corners = [] if merged else d.corners
    top = bottom = left = right = 0.0
    for _ in range(4):
        area = _Rect(c.mx + left, c.my + top, tw - c.mx - right, th - c.my - bottom)
        s, total, plans, gaps = _fit_stack(d, rows, area, c.s_hi)
        pos = _row_positions(d, rows, plans, gaps, total, area, s)
        taken = [p.ink(x, y) for (x, y), p in zip(pos, plans)]
        placed = [(b, *_place_corner(d, b, s, tw, th, c.my)) for b in corners]
        changed = False
        for b, plan, rect in placed:
            if not any(plan.ink(rect.x0, rect.y0).intersects(t, c.clear) for t in taken):
                continue
            if b.corner == "ml" and rect.x1 + c.clear - c.mx > left:
                left, changed = rect.x1 + c.clear - c.mx, True
            elif b.corner == "mr" and tw - rect.x0 + c.clear - c.mx > right:
                right, changed = tw - rect.x0 + c.clear - c.mx, True
            elif b.corner[0] == "t" and rect.y1 + c.clear - c.my > top:
                top, changed = rect.y1 + c.clear - c.my, True
            elif b.corner[0] == "b" and th - rect.y0 + c.clear - c.my > bottom:
                bottom, changed = th - rect.y0 + c.clear - c.my, True
        if not changed or top + bottom > 0.45 * th or left + right > 0.4 * tw:
            break
    props: dict[int, dict[str, Any]] = {}
    for (x, y), plan in zip(pos, plans):
        props.update(plan.emit(x, y))
    for b, plan, rect in placed:
        props.update(plan.emit(rect.x0, rect.y0))
    name = "stack " + " / ".join("+".join(b.role for b in r.blocks) for r in rows)
    overflow = total > area.h + 1 or any(p.w > area.w + 1 for p in plans)
    return _Candidate(name, s, 1, merged, props, penalty, overflow=overflow)


def _keep_candidate(c: _Ctx) -> _Candidate | None:
    """For a target close to the source's shape, the designer's own
    arrangement scaled uniformly is often the most faithful answer. It gets
    a small bonus, but its collisions are scored like anyone else's, so a
    source with overlapping pieces still gets re-laid-out."""
    d = c.d
    if abs(math.log((c.tw / c.th) / (d.W / d.H))) > math.log(1.2):
        return None
    s = min(c.tw / d.W, c.th / d.H)
    ox, oy = (c.tw - d.W * s) / 2, (c.th - d.H * s) / 2
    props: dict[int, dict[str, Any]] = {}
    for b in d.blocks:
        plan = b._measure_rigid(s, math.inf, True, floors=False)
        props.update(plan.emit(ox + b.box.x0 * s, oy + b.box.y0 * s))
    return _Candidate("keep source arrangement", s, 1, False, props, penalty=-0.05, floored=False)


def _col_widths(c: _Ctx, cols: list[list[_Row]], s: float, avail: float, final: bool,
                strict: bool) -> list[float] | None:
    """Column widths at scale s: each column's natural width, with text
    columns giving up width (re-flowing) when the total doesn't fit.
    `strict` refuses any squeeze (used while growing the hero image)."""
    d = c.d
    prefs, mins = [], []
    for col in cols:
        pref = low = 0.0
        for r in col:
            w = _measure_row(d, r, s, 1e9, final).w
            pref = max(pref, w)
            low = max(low, _measure_row(d, r, s, 0.45 * w, final).w if r.flexible else w)
        prefs.append(pref)
        mins.append(min(low, pref))
    deficit = sum(prefs) - avail
    if deficit <= 0:
        return prefs
    slack = [p - m for p, m in zip(prefs, mins)]
    if strict or sum(slack) < deficit:
        return None
    return [p - deficit * sl / sum(slack) for p, sl in zip(prefs, slack)]


def _cols_attempt(c: _Ctx, cols: list[list[_Row]], footer: _Row | None, area: _Rect, s: float,
                  final: bool = False, strict: bool = False):
    """Lay the columns out at scale s inside `area`; None if they don't fit."""
    d = c.d
    col_area, fplan = area, None
    if footer is not None:
        fplan = _measure_row(d, footer, s, area.w, final)
        col_area = _Rect(area.x0, area.y0, area.x1, area.y1 - fplan.h - c.clear)
        if col_area.h <= 0:
            return None
    avail = area.w - c.gutter * (len(cols) - 1)
    widths = _col_widths(c, cols, s, avail, final, strict)
    if widths is None:
        return None
    stacks = [_stack(d, col, s, w, final) for col, w in zip(cols, widths)]
    if any(total > col_area.h + 0.5 for total, _, _ in stacks) and not strict:
        # Too tall: hand the spare width to the text columns that overflow,
        # so their copy wraps into fewer, longer lines (up to 1.8x as wide).
        spare = avail - sum(widths)
        tall = [i for i, (total, _, _) in enumerate(stacks)
                if total > col_area.h + 0.5 and any(r.flexible for r in cols[i])]
        if spare > 1 and tall:
            base = sum(widths[i] for i in tall)
            widths = [w + min(0.8 * w, spare * w / base) if i in tall else w for i, w in enumerate(widths)]
            stacks = [_stack(d, col, s, w, final) for col, w in zip(cols, widths)]
    if any(total > col_area.h + 0.5 for total, _, _ in stacks):
        return None
    return widths, stacks, fplan, col_area


def _fit_columns(c: _Ctx, cols: list[list[_Row]], footer: _Row | None, area: _Rect):
    """Largest scale at which the columns fit, then grow the hero image into
    any genuinely free space. Returns (scale, props, occupied rects)."""
    blocks = [b for col in cols for r in col for b in r.blocks] + (footer.blocks if footer else [])
    for b in blocks:
        # Beside the copy, the hero only needs to fill its column's height;
        # it shouldn't hold the copy's size hostage in a short banner.
        b.grow, b.cap_h = 1.0, (area.h if b.role == "hero" else math.inf)
    s = c.s_hi
    if _cols_attempt(c, cols, footer, area, s) is None:
        lo, hi, ok = c.s_hi * 0.02, c.s_hi, None
        if _cols_attempt(c, cols, footer, area, lo) is None:
            return None  # doesn't fit even at the smallest scale
        for _ in range(13):
            mid = (lo + hi) / 2
            if _cols_attempt(c, cols, footer, area, mid) is not None:
                lo = ok = mid
            else:
                hi = mid
        if ok is None:
            return None
        s = ok
    heroes = [b for b in blocks if b.role == "hero"]
    if heroes and _cols_attempt(c, cols, footer, area, s, strict=True) is not None:
        lo, hi = 1.0, 1.8
        for _ in range(8):
            mid = (lo + hi) / 2
            for b in heroes:
                b.grow = mid
            fits = _cols_attempt(c, cols, footer, area, s, strict=True) is not None
            lo, hi = (mid, hi) if fits else (lo, mid)
        for b in heroes:
            b.grow = lo
    got = _cols_attempt(c, cols, footer, area, s, final=True) or _cols_attempt(c, cols, footer, area, s)
    if got is None:
        return None
    widths, stacks, fplan, col_area = got

    # Columns sit side by side, each centred vertically; spare width widens
    # the gutters a little and otherwise centres the whole group.
    k = len(cols)
    free = max(0.0, area.w - sum(widths) - c.gutter * (k - 1))
    extra = min(free / (k + 1), 0.06 * c.tw)
    x = area.x0 + (free - extra * (k - 1)) / 2
    props: dict[int, dict[str, Any]] = {}
    taken: list[_Rect] = []
    for col, w, (total, plans, gaps) in zip(cols, widths, stacks):
        y = col_area.y0 + (col_area.h - total) / 2
        for i, (row, plan) in enumerate(zip(col, plans)):
            rx = {"left": x, "right": x + w - plan.w}.get(row.align, x + (w - plan.w) / 2)
            props.update(plan.emit(rx, y))
            taken.append(plan.ink(rx, y))
            y += plan.h + (gaps[i] if i < len(gaps) else 0.0)
        x += w + c.gutter + extra
    if fplan is not None:
        fx = {"left": area.x0, "right": area.x1 - fplan.w}.get(footer.align, area.x0 + (area.w - fplan.w) / 2)
        props.update(fplan.emit(fx, area.y1 - fplan.h))
        taken.append(fplan.ink(fx, area.y1 - fplan.h))
    return s, props, taken


def _columns_candidate(c: _Ctx, cols: list[list[_Row]], footer: _Row | None, corners: list[_Block],
                       merged: bool) -> _Candidate | None:
    """Columns side by side. Corner pieces that would collide with them
    claim a strip down their side of the canvas."""
    tw, th = c.tw, c.th
    left = right = 0.0
    for _ in range(3):
        area = _Rect(c.mx + left, c.my, tw - c.mx - right, th - c.my)
        fit = _fit_columns(c, cols, footer, area)
        if fit is None:
            return None
        s, props, taken = fit
        placed = [(b, *_place_corner(c.d, b, s, tw, th, c.my)) for b in corners]
        changed = False
        for b, plan, rect in placed:
            if not any(plan.ink(rect.x0, rect.y0).intersects(t, c.clear) for t in taken):
                continue
            if b.corner[1] == "l" and rect.x1 + c.clear - c.mx > left:
                left, changed = rect.x1 + c.clear - c.mx, True
            elif b.corner[1] == "r" and tw - rect.x0 + c.clear - c.mx > right:
                right, changed = tw - rect.x0 + c.clear - c.mx, True
        if not changed or left + right > 0.4 * tw:
            break
    for b, plan, rect in placed:
        props.update(plan.emit(rect.x0, rect.y0))
    name = "columns " + " | ".join("+".join(b.role for r in col for b in r.blocks) for col in cols)
    if footer is not None:
        name += " / footer"
    return _Candidate(name, s, len(cols), merged, props)


def _orders(rows: list[_Row]) -> list[list[_Row]]:
    """The reading order, plus the hero image moved to the front and to the back."""
    out = [rows]
    hero = next((r for r in rows if any(b.role == "hero" for b in r.blocks)), None)
    if hero is not None:
        rest = [r for r in rows if r is not hero]
        seen = {tuple(map(id, rows))}
        for order in ([hero] + rest, rest + [hero]):
            key = tuple(map(id, order))
            if key not in seen:
                seen.add(key)
                out.append(order)
    return out


_VISUAL = {"hero", "image"}
_MESSAGE = {"headline", "subhead", "body"}
# Pulling a ribbon/badge/watermark out of its corner changes the design, so
# it has to buy a real gain in size to be worth it.
_MERGE_PENALTY = 0.08


def _structure_penalty(cols: list[list[_Row]]) -> float:
    """Splits a designer wouldn't make: an image stacked over copy or the
    CTA inside one column of a side-by-side layout, the headline and its
    supporting copy spread over several columns, or a logo that isn't at
    the top of its column."""
    roles = [{b.role for r in col for b in r.blocks} for col in cols]
    pen = 0.0
    for rs in roles:
        if rs & _VISUAL and rs & (_MESSAGE | {"cta"}):
            pen += 0.12
    pen += 0.12 * max(0, sum(1 for rs in roles if rs & _MESSAGE) - 1)
    for col in cols:
        if any(b.role == "logo" for r in col[1:] for b in r.blocks):
            pen += 0.05
    cards = [{b.backdrop for r in col for b in r.blocks if b.backdrop is not None} for col in cols]
    for k in set().union(*cards):  # a card's contents should stay in one column
        pen += 0.25 * (sum(1 for ks in cards if k in ks) - 1)
    return pen


def _upper_bound(c: _Ctx, cols: list[list[_Row]], area: _Rect) -> float:
    """Optimistic scale for a column split (images at full size, one line
    per text, no gaps), used to skip candidates that cannot win."""
    ub = c.s_hi
    rigid = 0.0
    for col in cols:
        col = [r for r in col if not all(b.role == "hero" for b in r.blocks)]  # heroes shrink to fit
        ub = min(ub, area.h / max(sum(r.min_h for r in col), 1e-6))
        rigid += max((r.natural_w for r in col if not r.flexible), default=0.0)
    if rigid > 0:
        ub = min(ub, max(1.0, area.w - c.gutter * (len(cols) - 1)) / rigid)
    return max(ub, 1e-6)


def _effective_scale(c: _Ctx, scale_of: Callable[[_Block], float | None]) -> float:
    """Importance-weighted geometric mean of how large each piece ended up
    relative to the source (capped at the ideal scale). With font floors and
    image minimums in play this, not the layout's base scale, is what a
    viewer sees."""
    num = den = 0.0
    for b in c.d.blocks:
        k = scale_of(b)
        if k is not None and k > 0:
            num += b.importance * math.log(min(k, c.s_hi))
            den += b.importance
    return math.exp(num / den) if den else c.s_hi


def _placed_scale(b: _Block, props: dict[int, dict[str, Any]]) -> float | None:
    """How large a block came out in a candidate, relative to the source."""
    if b.kind == "text" and b.items[0].idx in props:
        return props[b.items[0].idx]["font_size"] / b.items[0].fs
    if b.kind == "button" and b.label.idx in props:
        return props[b.label.idx]["font_size"] / b.label.fs
    ratios = [props[it.idx]["width"] / it.rect.w for it in b.items if it.idx in props and it.rect.w > 0]
    return sum(ratios) / len(ratios) if ratios else None


def _scale_bound(b: _Block, ub: float) -> float:
    """The largest relative size a block can reach when the layout's base
    scale is at most ub (floors and the hero's growth included)."""
    texts = [it for it in b.items if it.type == "text"]
    if b.kind == "image":
        return max(ub * (1.8 if b.role == "hero" else 1.0), b.k_floor)
    if texts and b.kind != "line":
        return max([ub] + [min(b.floor, it.fs) / it.fs for it in texts])
    return ub


def _small_text(d: _Design, font_sizes: dict[int, float]) -> float:
    """Importance-weighted shortfall of text below 11px."""
    return sum(b.importance * max(0.0, 1.0 - font_sizes[it.idx] / 11.0)
               for b in d.blocks for it in b.items if it.type == "text" and it.idx in font_sizes)


def _column_candidates(c: _Ctx, to_beat: float, deadline: float) -> list[_Candidate]:
    """Every way of cutting the reading order into 2-4 columns (more for
    wider canvases), best-first by optimistic bound, stopping once no
    remaining split can beat the best score found."""
    d = c.d
    aspect = c.tw / c.th
    kmax = next(k for limit, k in ((1.25, 1), (1.6, 2), (2.6, 3), (4.0, 4), (6.0, 5), (math.inf, 6))
                if aspect < limit)
    if kmax < 2:
        return []
    area = c.area
    specs = []
    for merged in ((False, True) if d.corners else (False,)):
        rows = _rows(d, d.blocks if merged else d.flow)
        corners = [] if merged else d.corners
        footers = [None]
        if len(rows) > 2 and len(rows[-1].blocks) == 1 and rows[-1].blocks[0].role == "fineprint":
            footers.append(rows[-1])
        for footer in footers:
            body = rows[:-1] if footer is not None else rows
            for order in _orders(body):
                n = len(order)
                # Many rows: fewer columns, so the number of splits stays sane.
                top = min(kmax, n)
                while top > 2 and sum(math.comb(n - 1, k - 1) for k in range(2, top + 1)) > 400:
                    top -= 1
                for k in range(2, top + 1):
                    for cuts in itertools.combinations(range(1, n), k - 1):
                        bounds = (0, *cuts, n)
                        cols = [order[a:b] for a, b in zip(bounds, bounds[1:])]
                        pen = _structure_penalty(cols)
                        ub = _upper_bound(c, cols, area)
                        # No text can end up larger than at the bound's scale
                        # (or its floor, whichever is bigger).
                        small = _small_text(d, {it.idx: max(1, round(it.fs * ub), round(min(b.floor, it.fs)))
                                                for b in d.blocks for it in b.items if it.type == "text"})
                        eff = _effective_scale(c, lambda b: _scale_bound(b, ub))
                        bound = (math.log(eff / c.s_hi) + 0.1 * math.log(ub / c.s_hi) + 0.3 - pen - 0.6 * small
                                 - 0.05 * (k - 1) - (_MERGE_PENALTY if merged else 0.0))
                        specs.append((bound, cols, footer, corners, merged, pen))
    specs.sort(key=lambda sp: -sp[0])
    out = []
    # Best-first, so the cap only drops splits that already looked worst.
    for bound, cols, footer, corners, merged, pen in specs[:250]:
        if bound <= to_beat or time.monotonic() > deadline:
            break
        cand = _columns_candidate(c, cols, footer, corners, merged)
        if cand is None:
            continue
        cand.penalty = pen
        cand.score = _score(c, cand)
        to_beat = max(to_beat, cand.score)
        out.append(cand)
    return out


def _drawn_rect(it: _Item, p: dict[str, Any]) -> _Rect:
    """Where an element's pixels actually land: an image's non-transparent
    area, a text's lines from cap height to descenders."""
    r = _Rect.xywh(p["x"], p["y"], p["width"], p["height"])
    if it.type == "image":
        l, t, rr, b = it.frac
        return _Rect(r.x0 + l * r.w, r.y0 + t * r.h, r.x0 + rr * r.w, r.y0 + b * r.h)
    if it.type == "text":
        fit = _measure(it.style, int(p["font_size"]), r.w)
        x0 = {"center": r.cx - fit.text_w / 2, "right": r.x1 - fit.text_w}.get(it.style.align, r.x0)
        return _Rect(x0, r.y0 + fit.ink_top, x0 + fit.text_w, r.y0 + fit.ink_bottom)
    return r


def _score(c: _Ctx, cand: _Candidate) -> float:
    """Higher is better. Rewards keeping content large (relative to the
    most this canvas allows) and filling the canvas; penalises tiny text,
    collisions between pieces' drawn pixels, extra columns, and pulling
    corner pieces out of their corners."""
    d = c.d
    boxes, drawn = [], []
    for b in d.blocks:
        placed = [(it, cand.props[it.idx]) for it in b.items if it.idx in cand.props]
        if placed:
            boxes.append(_union_all([_Rect.xywh(p["x"], p["y"], p["width"], p["height"]) for _, p in placed]))
            drawn.append(_union_all([_drawn_rect(it, p) for it, p in placed]))
    small = _small_text(d, {i: p["font_size"] for i, p in cand.props.items() if "font_size" in p})
    fill = _union_all(boxes).area / (c.tw * c.th) if boxes else 0.0
    # Pieces that touch (closer than ~1% of the canvas) count as colliding.
    pad = 0.005 * min(c.tw, c.th)
    drawn = [_Rect(r.x0 - pad, r.y0 - pad, r.x1 + pad, r.y1 + pad) for r in drawn]
    clash = 0.0
    for i, a in enumerate(drawn):
        for o in drawn[i + 1:]:
            clash += a.overlap(o) / max(1.0, min(a.area, o.area))
    eff = _effective_scale(c, lambda b: _placed_scale(b, cand.props))
    # The base scale still counts a little: when floors make two layouts'
    # text equal, the one with more room for everything else is better.
    base = 0.1 * math.log(max(cand.s, 1e-6) / c.s_hi)
    return (math.log(max(eff, 1e-6) / c.s_hi) + base + 0.3 * fill - 0.6 * small - 1.5 * clash
            - cand.penalty - 0.05 * (cand.columns - 1) - (_MERGE_PENALTY if cand.merged else 0.0)
            - (2.0 if cand.overflow else 0.0))


# Smallest font size worth printing, per role: (px, share of the target's
# short side). The share keeps a 50px-tall banner from being asked for 16px
# headlines; the px value keeps big canvases from demanding huge text.
_FLOORS = {
    "headline": (16, 0.22), "subhead": (11, 0.14), "body": (10, 0.14), "cta": (10, 0.14),
    "badge": (8, 0.12), "ribbon": (8, 0.12), "fineprint": (7, 0.10),
}

# When nothing fits, the floors give way in order of importance: fine print
# and badges first, then supporting copy, the headline and CTA last (after
# the logo/hero image minimums, which go before any text).
_FLOOR_TIER = {"headline": 0, "cta": 0, "badge": 2, "ribbon": 2, "fineprint": 2}  # others: 1
_RELAX_STEPS = (
    # (headline/CTA, other copy, fine print/badges, logo/hero minimums)
    (1, 1, 1, 1), (1, 1, 1, 0.6), (1, 1, 1, 0), (1, 1, 0.9, 0), (0.95, 0.95, 0.85, 0),
    (0.9, 0.9, 0.85, 0), (0.85, 0.85, 0.8, 0), (0.8, 0.8, 0.75, 0), (0.75, 0.7, 0.7, 0),
    (0.7, 0.65, 0.6, 0), (0.6, 0.55, 0.5, 0), (0.5, 0.45, 0.4, 0),
)


def _candidates(c: _Ctx, deadline: float) -> list[_Candidate]:
    d = c.d
    cands = []
    for merged in ((False, True) if d.corners else (False,)):
        for order, pen in _stack_orders(_rows(d, d.blocks if merged else d.flow)):
            cands.append(_stack_candidate(c, merged, order, pen))
    keep = _keep_candidate(c)
    if keep is not None:
        cands.append(keep)
    for cand in cands:
        cand.score = _score(c, cand)
    return cands + _column_candidates(c, max(k.score for k in cands), deadline)


def _wrap_backdrop(d: _Design, card: _Item, inside: list[_Item], props: dict[int, dict[str, Any]],
                   tw: int, th: int) -> dict[str, Any]:
    """Put a card back behind what it held: around their drawn pixels, with
    the source's padding scaled by how much the contents themselves scaled.
    If the layout split the contents up, the card wraps the most important
    piece and whichever others it can reach without covering anything else."""
    placed = [(it, props[it.idx]) for it in inside if it.idx in props]
    if not placed:
        return {"x": 0, "y": 0, "width": min(tw, card.rect.w), "height": min(th, card.rect.h)}
    held = {id(it) for it in inside}
    others = [_drawn_rect(it, props[it.idx]) for b in d.blocks for it in b.items
              if id(it) not in held and it.idx in props]
    rank = {id(it): b.importance for b in d.blocks for it in b.items}
    placed.sort(key=lambda ip: -rank.get(id(ip[0]), 0.0))
    keep = [placed[0]]
    now = _drawn_rect(*placed[0])
    for it, p in placed[1:]:
        grown = now.union(_drawn_rect(it, p))
        if not any(grown.overlap(o) > 0 for o in others):
            keep.append((it, p))
            now = grown
    placed = keep
    was = _union_all([it.vis for it, _ in placed])
    k = math.sqrt(now.area / max(was.area, 1.0))
    r = _Rect(now.x0 - max(0.0, was.x0 - card.rect.x0) * k, now.y0 - max(0.0, was.y0 - card.rect.y0) * k,
              now.x1 + max(0.0, card.rect.x1 - was.x1) * k, now.y1 + max(0.0, card.rect.y1 - was.y1) * k)
    r = _Rect(max(0.0, r.x0), max(0.0, r.y0), min(float(tw), r.x1), min(float(th), r.y1))
    return {"x": r.x0, "y": r.y0, "width": r.w, "height": r.h, **_shape_props(card, k, r.h)}


def _layout(d: _Design, tw: int, th: int) -> dict[int, dict[str, Any]]:
    c = _ctx(d, tw, th)
    short = min(tw, th)
    deadline = time.monotonic() + _TIME_BUDGET
    # Text and the logo/hero image are held at readable minimum sizes. If
    # no layout fits even then, the floors are relaxed a step at a time, the
    # images' first (every element always stays visible). The best layout
    # from any round wins.
    pool: list[_Candidate] = []
    for step in _RELAX_STEPS:
        *text_relax, img_relax = step
        for b in d.blocks:
            px, share = _FLOORS.get(b.role, (9, 0.12))
            b.floor = text_relax[_FLOOR_TIER.get(b.role, 1)] * min(px, share * short)
            if b.kind == "image":
                # Minimum visible height: a brand mark or product that
                # shrinks to a speck on a small banner isn't "intact", and
                # no image may shrink out of sight at all.
                want = {"logo": min(24.0, 0.3 * th, 0.12 * tw),
                        "hero": min(160.0, 0.4 * th, 0.3 * tw)}.get(b.role, 0.0)
                b.k_floor = max(img_relax * want, min(12.0, 0.15 * short)) / max(1.0, b.vis.h)
        cands = _candidates(c, deadline)
        for k in cands:
            k.name += f" floors {step}" if step != _RELAX_STEPS[0] else ""
        pool += cands
        if any(k.floored and not k.overflow for k in cands) or time.monotonic() > deadline:
            break
    best = max(pool, key=lambda k: k.score)
    if _DEBUG:
        for k in sorted(pool, key=lambda k: -k.score)[:5]:
            print(f"[resize] {tw}x{th} {k.score:+.3f} s={k.s:.3f} {k.name}"
                  f"{' (corners in flow)' if k.merged else ''}{' OVERFLOW' if k.overflow else ''}",
                  file=sys.stderr)

    props = dict(best.props)
    for it in d.backgrounds:
        props[it.idx] = {"x": 0, "y": 0, "width": tw, "height": th}
    for card, inside in d.backdrops:
        props[card.idx] = _wrap_backdrop(d, card, inside, props, tw, th)
    sd = max(best.s, min(tw / d.W, th / d.H))
    for it in d.decor + d.hidden:
        props[it.idx] = _place_decor(d, it, sd, tw, th)
    return props


# ---------------------------------------------------------------------------
# Stage 3: finalise — the structural guarantees
# ---------------------------------------------------------------------------

def _settle(it: _Item, p: dict[str, Any], tw: int, th: int) -> dict[str, Any]:
    """Make one element's geometry valid on its own: image proportions
    kept, text box fits its text, nothing (even rotated) leaves the canvas."""
    if it.role == "background":
        if it.type == "image" and it.el.get("fit") != "cover" and it.rect.w > 0 and it.rect.h > 0:
            # A contain/fill background keeps its proportions. For "contain"
            # this draws exactly what a full-canvas box would (the renderer
            # letterboxes inside the box); for "fill" it avoids distortion.
            f = min(tw / it.rect.w, th / it.rect.h)
            w, h = it.rect.w * f, it.rect.h * f
            return {"x": round((tw - w) / 2, 2), "y": round((th - h) / 2, 2), "width": round(w, 2), "height": round(h, 2)}
        return {"x": 0, "y": 0, "width": tw, "height": th}
    x, y = _num(p.get("x")), _num(p.get("y"))
    w, h = max(0.0, _num(p.get("width"))), max(0.0, _num(p.get("height")))
    if it.rect.w > 0:
        w = max(w, 1.0)
    if it.rect.h > 0:
        h = max(h, 1.0)
    if it.type == "image" and it.el.get("fit") != "cover" and it.rect.w > 0 and it.rect.h > 0:
        aspect = it.rect.w / it.rect.h
        cx, cy = x + w / 2, y + h / 2
        if w / h > aspect:
            w = h * aspect
        else:
            h = w / aspect
        x, y = cx - w / 2, cy - h / 2
    fs = max(1, int(round(_num(p.get("font_size"), it.fs)))) if it.type == "text" else None
    canvas = _Rect(0, 0, tw, th)
    drawn = _rot_aabb(x, y, w, h, it.rot)
    # A decoration bleeding like the source stays put if enough of it shows.
    bleed = (_KEEP_BLEED and it.role == "decor" and it.bleeds
             and drawn.overlap(canvas) >= 0.1 * max(drawn.area, 1e-9))

    for _ in range(60):
        if it.type == "text":
            w = float(math.ceil(w - 0.01))  # whole pixels
            fit = _measure(it.style, fs, w)
            if fit.text_w > w + 0.5:  # an unbreakable word: widen about the centre
                grow = float(math.ceil(fit.text_w + 0.5 - w))
                x, w = x - grow / 2, w + grow
                fit = _measure(it.style, fs, w)
            if it.rot:
                y, h = y + h / 2 - max(h, fit.height) / 2, max(h, fit.height)
            else:
                h = fit.height
        bounds = _Rect.xywh(x, y, w, h).union(_rot_aabb(x, y, w, h, it.rot))
        if bleed or (bounds.w <= tw + 0.01 and bounds.h <= th + 0.01):
            break
        f = min(tw / bounds.w, th / bounds.h) * 0.999
        cx, cy = x + w / 2, y + h / 2
        w, h = w * f, h * f
        x, y = cx - w / 2, cy - h / 2
        if fs is not None:
            fs = max(1, min(fs - 1, int(fs * f)))

    # Whole pixels render crisply: images and shapes are drawn 1:1 instead of
    # being resampled, and text starts on a pixel row. (Skipped when rounding
    # up would no longer fit, e.g. a rotated piece that exactly fills the canvas.)
    sx, sy, sw, sh = float(round(x)), float(round(y)), w, h
    if it.type == "text":
        sh = float(math.ceil(h - 0.01))
    elif w >= 1 and h >= 1:
        rw, rh = float(max(1, round(w))), float(max(1, round(h)))
        exact = it.type == "image" and it.el.get("fit") != "cover"
        if not exact or abs((rw / rh) / (w / h) - 1) <= 0.02:
            sw, sh = rw, rh
    if bleed:
        shown = _rot_aabb(sx, sy, sw, sh, it.rot)
        fits = shown.overlap(canvas) >= 0.1 * max(shown.area, 1e-9)
    else:
        snapped = _Rect.xywh(sx, sy, sw, sh).union(_rot_aabb(sx, sy, sw, sh, it.rot))
        fits = snapped.w <= tw + 0.01 and snapped.h <= th + 0.01
    if fits:
        x, y, w, h = sx, sy, sw, sh
    if not bleed:
        def overhang(x: float, y: float) -> tuple[float, float]:
            b = _Rect.xywh(x, y, w, h).union(_rot_aabb(x, y, w, h, it.rot))
            return (-b.x0 if b.x0 < 0 else (tw - b.x1 if b.x1 > tw else 0.0),
                    -b.y0 if b.y0 < 0 else (th - b.y1 if b.y1 > th else 0.0))

        # Move inside on whole pixels where possible; a rotated piece that
        # fills the canvas to within a pixel moves by the exact amount.
        for _ in range(3):
            dx, dy = overhang(x, y)
            if not dx and not dy:
                break
            x = math.ceil(x + dx) if dx > 0 else math.floor(x + dx) if dx < 0 else x
            y = math.ceil(y + dy) if dy > 0 else math.floor(y + dy) if dy < 0 else y
        dx, dy = overhang(x, y)
        x, y = x + dx, y + dy
        x, y = _clamp(x, 0, max(0.0, tw - w)), _clamp(y, 0, max(0.0, th - h))
    out = {"x": round(x, 2), "y": round(y, 2), "width": round(w, 2), "height": round(h, 2)}
    if fs is not None:
        out["font_size"] = fs
        if it.style.letter_spacing:
            out["letter_spacing"] = it.style.tracking(fs)
    for k in ("corner_radius", "stroke_width"):
        if k in p:
            out[k] = round(max(0.0, _num(p[k])), 2)
    return out


def _finalise(d: _Design, tw: int, th: int, props: dict[int, dict[str, Any]]) -> dict[str, Any]:
    s = min(tw / d.W, th / d.H)
    ox, oy = (tw - d.W * s) / 2, (th - d.H * s) / 2
    elements = []
    for it in d.items:
        p = props.get(it.idx)
        if p is None:  # the layout skipped it: scale it in place
            p = {"x": ox + it.rect.x0 * s, "y": oy + it.rect.y0 * s, "width": it.rect.w * s,
                 "height": it.rect.h * s, "font_size": it.fs * s, **_shape_props(it, s)}
        el = copy.deepcopy(it.src)
        el.update(_settle(it, p, tw, th))
        if it.role == "hidden":
            el["opacity"] = 0
        elements.append(el)
    return {"canvas": {"width": tw, "height": th}, "elements": elements}


def _fit_inside(source: dict[str, Any], tw: int, th: int) -> dict[str, Any]:
    """Last-resort layout with no measurement at all: the whole design
    scaled to fit inside the target, centred, backgrounds full-bleed."""
    canvas = source.get("canvas") or {}
    sw = max(1.0, _num(canvas.get("width"), tw))
    sh = max(1.0, _num(canvas.get("height"), th))
    s = min(tw / sw, th / sh)
    ox, oy = (tw - sw * s) / 2, (th - sh * s) / 2
    elements = []
    for src in source.get("elements") or []:
        el = copy.deepcopy(src)
        x, y = _num(el.get("x")), _num(el.get("y"))
        w, h = max(0.0, _num(el.get("width"))), max(0.0, _num(el.get("height")))
        if el.get("type") in ("image", "shape") and w * h >= 0.9 * sw * sh:
            x, y, w, h = 0.0, 0.0, float(tw), float(th)
        else:
            x, y, w, h = ox + x * s, oy + y * s, w * s, h * s
            w, h = min(w, tw), min(h, th)
            x, y = _clamp(x, 0, tw - w), _clamp(y, 0, th - h)
        el.update(x=round(x, 2), y=round(y, 2), width=round(w, 2), height=round(h, 2))
        if el.get("type") == "text":
            el["font_size"] = max(1, int(round(_num(el.get("font_size"), 24.0) * s)))
        elements.append(el)
    return {"canvas": {"width": tw, "height": th}, "elements": elements}
