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
                            corner pieces stay in their corners.
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
import math
import re
import sys
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
        Only geometry (x/y/width/height), font_size and shape corner/stroke
        sizes change; content, fonts, colours, assets and fit are untouched.
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
_DESCENDERS = set("gjpqyQ,;()[]{}|/@$")


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
    letter_spacing: float
    align: str
    cache: dict = field(default_factory=dict)


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
    for i in range(len(wrapped) + 1):
        if i == len(wrapped) or wrapped[i] == "\n":
            seg = wrapped[start:i]
            w = sum(widths[start:i])
            if len(seg) > 1:
                w += st.letter_spacing * (len(seg) - 1)
            lines.append(seg)
            line_w.append(w)
            start = i + 1
    asc, desc, cap = st.font.metrics(fs)
    lead = fs * st.line_height
    n = len(lines)
    baseline = (n - 1) * lead + asc
    has_desc = any(c in _DESCENDERS for c in lines[-1])
    fit = _TextFit(
        fs=fs,
        width=width,
        lines=lines,
        line_w=line_w,
        height=(n - 1) * lead + asc + desc,
        ink_top=max(0.0, asc - cap),
        baseline=baseline,
        ink_bottom=baseline + (min(desc, 0.22 * fs) if has_desc else 0.0),
    )
    st.cache[key] = fit
    return fit


def _fit_width(st: _TextStyle, fs: int, max_w: float, balance: bool = True) -> _TextFit:
    """Wrap at `max_w`, then shrink the box to the text: a single line is
    shrink-wrapped, several lines are balanced (the narrowest width that
    keeps the same line count), so headlines don't end on a lone word.
    The returned box is never narrower than its widest drawn line."""
    max_w = max(1.0, max_w)
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


def _starts_with_verb(text: str) -> bool:
    words = re.findall(r"[a-zA-Z]+", text.lower())
    return bool(words) and words[0] in _CTA_VERBS and len(words) <= 6


@dataclass
class _Plan:
    """A block measured at some scale: its box size, where its visible
    content starts and ends vertically, and how to write it out."""
    w: float
    h: float
    ink_top: float
    ink_bottom: float
    emit: Callable[[float, float], dict[int, dict[str, Any]]]


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
    grow: float = 1.0
    container: _Item | None = None
    label: _Item | None = None
    pad_x: float = 0.0

    def measure(self, s: float, max_w: float) -> _Plan:
        return getattr(self, "_measure_" + self.kind)(s, max(1.0, max_w))

    def _measure_text(self, s: float, max_w: float) -> _Plan:
        it = self.items[0]
        fs = max(1, round(it.fs * s))
        want = min(max_w, max(1.0, it.rect.w * s))
        while True:
            fit = _fit_width(it.style, fs, want)
            if fit.text_w <= max_w + 0.5 or fs == 1:
                break
            fs = max(1, min(fs - 1, int(fs * max_w / fit.text_w)))

        def emit(x: float, y: float) -> dict[int, dict[str, Any]]:
            return {it.idx: {"x": x, "y": y, "width": fit.width, "height": fit.height, "font_size": fs}}

        return _Plan(fit.width, fit.height, fit.ink_top, fit.ink_bottom, emit)

    def _measure_image(self, s: float, max_w: float) -> _Plan:
        it = self.items[0]
        k = s * self.grow
        w, h = it.rect.w * k, it.rect.h * k
        if w > max_w:
            w, h = max_w, h * max_w / w
        _, t, _, b = it.frac

        def emit(x: float, y: float) -> dict[int, dict[str, Any]]:
            return {it.idx: {"x": x, "y": y, "width": w, "height": h}}

        return _Plan(w, h, t * h, b * h, emit)

    def _measure_line(self, s: float, max_w: float) -> _Plan:
        it = self.items[0]
        w, h = min(it.rect.w * s, max_w), max(1.0, it.rect.h * s)

        def emit(x: float, y: float) -> dict[int, dict[str, Any]]:
            return {it.idx: {"x": x, "y": y, "width": w, "height": h, **_shape_props(it, s)}}

        return _Plan(w, h, 0.0, h, emit)

    def _measure_button(self, s: float, max_w: float) -> _Plan:
        c, lab = self.container, self.label
        multi = lab.fit.n > 1
        for _ in range(8):
            fs = max(1, round(lab.fs * s))
            if multi:
                fit = _fit_width(lab.style, fs, min(max_w, max(1.0, lab.rect.w * s)))
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

    def _measure_rigid(self, s: float, max_w: float) -> _Plan:
        k = s
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

        groups: dict[str, list[_Item]] = {}
        for it in live:
            if it.group is not None:
                groups.setdefault(it.group, []).append(it)
        groups = {k: v for k, v in groups.items() if len(v) > 1}
        singles = [it for it in live if it.group not in groups]

        # A text sitting on a snug, solid shape is a button/badge even
        # without a group_id.
        shapes = [it for it in singles if it.type == "shape" and not it.rot and it.opacity >= 0.3]
        owner: dict[int, str] = {}
        for t in (it for it in singles if it.type == "text" and not it.rot):
            best = None
            for sh in shapes:
                if (sh.z, sh.idx) > (t.z, t.idx) or sh.rect.area <= 0:
                    continue
                if sh.rect.area > 12 * max(t.vis.area, 1.0):
                    continue
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
                elif it.fs <= 0.45 * fmax and it.fs <= 0.02 * min(W, H):
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
# ---------------------------------------------------------------------------

@dataclass(eq=False)
class _Row:
    """Flow blocks that sit side by side in the source (e.g. a badge strip)."""
    blocks: list[_Block]
    vis: _Rect
    align: str
    hgap: float


def _rows(d: _Design, blocks: list[_Block]) -> list[_Row]:
    rows: list[list[_Block]] = []
    for b in sorted(blocks, key=lambda b: (b.vis.y0, b.vis.x0)):
        for row in rows:
            if all(_band_overlap(o.vis, b.vis) >= 0.5 and (o.vis.x1 <= b.vis.x0 or b.vis.x1 <= o.vis.x0)
                   for o in row):
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


def _measure_row(d: _Design, row: _Row, s: float, max_w: float) -> _Plan:
    def line_w(ln: list[_Plan], g: float) -> float:
        return sum(p.w for p in ln) + g * (len(ln) - 1)

    base_gap = max(row.hgap, 0.015 * min(d.W, d.H))
    plans = [b.measure(s, max_w) for b in row.blocks]
    gap = base_gap * s
    # A strip that is a little too wide shrinks to fit (up to 20%) rather
    # than wrapping; one that is far too wide wraps into even lines.
    k = s
    for _ in range(3):
        over = line_w(plans, gap)
        if len(plans) < 2 or over <= max_w + 0.5 or k < 0.8 * s:
            break
        k *= max_w / over * 0.995
        plans = [b.measure(k, max_w) for b in row.blocks]
        gap = base_gap * k
    if line_w(plans, gap) > max_w + 0.5 and k < 0.8 * s:
        plans = [b.measure(s, max_w) for b in row.blocks]
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
    y = 0.0
    for ln, lw in zip(lines, widths):
        lh = max(p.h for p in ln)
        x = {"left": 0.0, "right": row_w - lw}.get(row.align, (row_w - lw) / 2)
        for p in ln:
            placed.append((p, x, y + (lh - p.h) / 2))
            x += p.w + gap
        y += lh + gap
    row_h = y - gap
    ink_top = min(oy + p.ink_top for p, _, oy in placed)
    ink_bottom = max(oy + p.ink_bottom for p, _, oy in placed)

    def emit(x: float, y: float) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for p, ox, oy in placed:
            out.update(p.emit(x + ox, y + oy))
        return out

    return _Plan(row_w, row_h, ink_top, ink_bottom, emit)


def _stack(d: _Design, rows: list[_Row], s: float, max_w: float) -> tuple[float, list[_Plan], list[float]]:
    """Measure rows stacked top to bottom at scale s. Gaps keep the source's
    visual rhythm (measured between visible pixels, not boxes)."""
    plans = [_measure_row(d, r, s, max_w) for r in rows]
    floor = 0.02 * min(d.W, d.H)
    gaps = []
    for k in range(1, len(rows)):
        want = max(rows[k].vis.y0 - rows[k - 1].vis.y1, floor) * s
        prev, nxt = plans[k - 1], plans[k]
        gaps.append(max(0.0, want - (prev.h - prev.ink_bottom) - nxt.ink_top))
    return sum(p.h for p in plans) + sum(gaps), plans, gaps


def _fit_stack(d: _Design, rows: list[_Row], area: _Rect, s_hi: float):
    """Largest scale (up to s_hi) at which the stacked rows fit `area`,
    then let the hero image soak up some of any spare height."""
    for b in (b for r in rows for b in r.blocks):
        b.grow = 1.0
    if not rows:
        return s_hi, 0.0, [], []
    total, plans, gaps = _stack(d, rows, s_hi, area.w)
    s = s_hi
    if total > area.h:
        lo, hi, best = s_hi * 0.02, s_hi, None
        for _ in range(22):
            mid = (lo + hi) / 2
            t, p, g = _stack(d, rows, mid, area.w)
            if t <= area.h:
                lo, best = mid, (mid, t, p, g)
            else:
                hi = mid
        s, total, plans, gaps = best or (lo, *_stack(d, rows, lo, area.w))

    heroes = [b for r in rows for b in r.blocks if b.role == "hero"]
    spare = area.h - total
    if heroes and spare > 0.08 * area.h:
        budget = total + 0.5 * spare
        lo, hi = 1.0, 1.8
        for _ in range(14):
            mid = (lo + hi) / 2
            for b in heroes:
                b.grow = mid
            if _stack(d, rows, s, area.w)[0] <= budget:
                lo = mid
            else:
                hi = mid
        for b in heroes:
            b.grow = lo
        total, plans, gaps = _stack(d, rows, s, area.w)
    return s, total, plans, gaps


def _row_positions(d: _Design, rows: list[_Row], plans: list[_Plan], gaps: list[float],
                   total: float, area: _Rect) -> list[tuple[float, float]]:
    """Top-left of each row. Spare height is shared between the top, the
    gaps and the bottom in the same proportions as the source's spacing."""
    if not rows:
        return []
    spare = max(0.0, area.h - total)
    floor = 0.01 * d.H
    src = [rows[0].vis.y0]
    src += [max(rows[k].vis.y0 - rows[k - 1].vis.y1, 0.0) for k in range(1, len(rows))]
    src.append(d.H - rows[-1].vis.y1)
    weights = [max(v, floor) for v in src]
    extra = [spare * w / sum(weights) for w in weights]
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
    plan = b.measure(s, 0.45 * tw)
    if plan.h > 0.3 * th:
        plan = b.measure(s * 0.3 * th / plan.h, 0.45 * tw)
    off_x = _clamp((b.box.x0 if b.corner[1] == "l" else d.W - b.box.x1) * s, 0.5 * m, 2 * m)
    off_y = _clamp((b.box.y0 if b.corner[0] == "t" else d.H - b.box.y1) * s, 0.5 * m, 2 * m)
    x = off_x if b.corner[1] == "l" else tw - off_x - plan.w
    y = off_y if b.corner[0] == "t" else th - off_y - plan.h
    return plan, _Rect.xywh(x, y, plan.w, plan.h)


def _place_decor(d: _Design, it: _Item, sd: float, tw: int, th: int) -> dict[str, Any]:
    """Decorations keep their relationship to the canvas edges: a shape that
    bleeds off a corner hugs that corner, a full-width band stays full width,
    anything else keeps its relative position."""
    r = it.rect
    seen = r.intersect(_Rect(0, 0, d.W, d.H)) or r
    if it.type == "image":
        seen = r  # images must keep their proportions
    w, h = seen.w * sd, seen.h * sd
    left, right = r.x0 <= 1, r.x1 >= d.W - 1
    top, bottom = r.y0 <= 1, r.y1 >= d.H - 1
    band = it.type == "shape" and it.el.get("shape_type", "rectangle") == "rectangle"
    if band and left and right:
        w = tw
    if band and top and bottom:
        h = th
    f = min(1.0, tw / max(w, 1e-6), th / max(h, 1e-6))
    if not band or it.type == "image":
        w, h = w * f, h * f
    else:
        w, h = min(w, tw), min(h, th)
    x = 0.0 if left else tw - w if right else seen.cx / d.W * tw - w / 2
    y = 0.0 if top else th - h if bottom else seen.cy / d.H * th - h / 2
    x, y = _clamp(x, 0, tw - w), _clamp(y, 0, th - h)
    return {"x": x, "y": y, "width": w, "height": h, **_shape_props(it, sd)}


def _layout(d: _Design, tw: int, th: int) -> dict[int, dict[str, Any]]:
    """Milestone-1 layout: one centred column (the source's reading order,
    top to bottom) with corner pieces in their corners. Works for square
    and portrait targets; wide and extreme targets come in later milestones."""
    props: dict[int, dict[str, Any]] = {}
    for it in d.backgrounds:
        props[it.idx] = {"x": 0, "y": 0, "width": tw, "height": th}

    rx, ry = tw / d.W, th / d.H
    s_hi = min(max(rx, ry), 1.1 * math.sqrt(rx * ry))
    m = d.margin_ratio() * min(tw, th)
    rows = _rows(d, d.flow)
    corners = d.corners
    reserve_top = reserve_bottom = 0.0
    gap = 0.02 * min(tw, th)
    for _ in range(4):
        area = _Rect(m, m + reserve_top, tw - m, th - m - reserve_bottom)
        s, total, plans, gaps = _fit_stack(d, rows, area, s_hi)
        pos = _row_positions(d, rows, plans, gaps, total, area)
        row_rects = [_Rect.xywh(x, y, p.w, p.h) for (x, y), p in zip(pos, plans)]
        placed = [(b, *_place_corner(d, b, s, tw, th, m)) for b in corners]
        changed = False
        for b, plan, rect in placed:
            if not any(rect.intersects(rr, gap) for rr in row_rects):
                continue
            if b.corner[0] == "t" and rect.y1 + gap - m > reserve_top:
                reserve_top, changed = rect.y1 + gap - m, True
            elif b.corner[0] == "b" and th - rect.y0 + gap - m > reserve_bottom:
                reserve_bottom, changed = th - rect.y0 + gap - m, True
        if not changed or reserve_top + reserve_bottom > 0.45 * th:
            break

    for (x, y), plan in zip(pos, plans):
        props.update(plan.emit(x, y))
    for b, plan, rect in placed:
        props.update(plan.emit(rect.x0, rect.y0))
    sd = max(s, min(rx, ry))
    for it in d.decor:
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

    for _ in range(60):
        if it.type == "text":
            w = round(w, 2)
            fit = _measure(it.style, fs, w)
            if fit.text_w > w + 0.5:  # an unbreakable word: widen about the centre
                grow = fit.text_w + 0.5 - w
                x, w = x - grow / 2, round(w + grow, 2)
                fit = _measure(it.style, fs, w)
            if it.rot:
                y, h = y + h / 2 - max(h, fit.height) / 2, max(h, fit.height)
            else:
                h = fit.height
        bounds = _Rect.xywh(x, y, w, h).union(_rot_aabb(x, y, w, h, it.rot))
        if bounds.w <= tw + 0.01 and bounds.h <= th + 0.01:
            break
        f = min(tw / bounds.w, th / bounds.h) * 0.999
        cx, cy = x + w / 2, y + h / 2
        w, h = w * f, h * f
        x, y = cx - w / 2, cy - h / 2
        if fs is not None:
            fs = max(1, min(fs - 1, int(fs * f)))

    x, y, w, h = round(x, 2), round(y, 2), round(w, 2), round(h, 2)
    bounds = _Rect.xywh(x, y, w, h).union(_rot_aabb(x, y, w, h, it.rot))
    if bounds.x0 < 0:
        x -= bounds.x0
    elif bounds.x1 > tw:
        x -= bounds.x1 - tw
    if bounds.y0 < 0:
        y -= bounds.y0
    elif bounds.y1 > th:
        y -= bounds.y1 - th
    x, y = round(_clamp(x, 0, max(0.0, tw - w)), 2), round(_clamp(y, 0, max(0.0, th - h)), 2)
    out = {"x": x, "y": y, "width": w, "height": h}
    if fs is not None:
        out["font_size"] = fs
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
