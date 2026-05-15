"""Ink-pair adjacency extraction for trap.

Scans a PDF's content streams for axis-aligned rectangles drawn with
spot-ink colors, then computes pairwise adjacency between rects with
different inks. Emits suggested :class:`TrapZone` entries that the
operator can paste into a policy — or pipe directly into the engine
via :func:`auto_trap_zones`.

Scope (v1)
~~~~~~~~~~

* Only ``re`` rectangle paths followed by ``f`` / ``B`` (fill) ops
  are recognized. Curved paths, text, and clip-restricted geometry
  are out of scope; operator-declared ``trap_zones`` remain the
  fallback for those.
* Only ``Separation`` and ``DeviceN`` spot inks resolved through
  ``/Resources/ColorSpace`` are tracked. Process-only artwork
  (DeviceCMYK with no spots) is ignored — there's nothing to trap.
* Adjacency is detected on bounding rectangles. Two rects with
  different inks are adjacent when they share a vertical or
  horizontal edge within ``edge_tolerance_pt`` and their cross-axis
  ranges overlap; the suggested trap zone covers the seam.

The extractor is intentionally conservative: false-negatives (a
boundary that should have been trapped but wasn't detected) are
the right failure mode — operator review catches them. False-positives
(suggesting trap where none is needed) are also acceptable because
the engine's verifier confirms the trap doesn't violate the color
budget regardless.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pikepdf
from pikepdf import Name, Object

from compile_pdf_trap.policy_schema import TrapZone


@dataclass(frozen=True)
class InkRect:
    """One filled rectangle on a page, tagged with its resolved ink name."""

    page_index: int
    ink_name: str
    rect_pt: tuple[float, float, float, float]  # (llx, lly, urx, ury)


def extract_ink_rects(input_bytes: bytes) -> list[InkRect]:
    """Walk every page's content stream and collect spot-ink rectangles.

    Returns the list ordered by ``(page_index, drawing order)``. Pages
    without spot inks produce no entries.
    """
    out: list[InkRect] = []
    pdf = pikepdf.open(io.BytesIO(input_bytes))
    try:
        for page_index, page in enumerate(pdf.pages):
            ink_aliases = _spot_ink_aliases(page.obj.get(Name.Resources))
            if not ink_aliases:
                continue
            for ink_name, rect in _iter_ink_rects(page, ink_aliases):
                out.append(InkRect(page_index=page_index, ink_name=ink_name, rect_pt=rect))
    finally:
        pdf.close()
    return out


def auto_trap_zones(
    input_bytes: bytes,
    *,
    edge_tolerance_pt: float = 0.5,
    seam_width_pt: float = 6.0,
) -> list[TrapZone]:
    """Run the extractor and emit one :class:`TrapZone` per adjacency.

    ``edge_tolerance_pt`` controls how far apart two edges may be and
    still count as adjacent. ``seam_width_pt`` controls how wide the
    suggested trap rectangle is — the seam is centered on the shared
    edge.
    """
    ink_rects = extract_ink_rects(input_bytes)
    by_page: dict[int, list[InkRect]] = {}
    for r in ink_rects:
        by_page.setdefault(r.page_index, []).append(r)

    zones: list[TrapZone] = []
    for page_index, page_rects in by_page.items():
        for i, a in enumerate(page_rects):
            for b in page_rects[i + 1 :]:
                if a.ink_name == b.ink_name:
                    continue
                seam = _vertical_seam(a, b, edge_tolerance_pt, seam_width_pt)
                if seam is None:
                    seam = _horizontal_seam(a, b, edge_tolerance_pt, seam_width_pt)
                if seam is None:
                    continue
                # Order the pair deterministically (alphabetical) so re-running
                # the extractor produces identical zone records.
                from_ink, to_ink = sorted((a.ink_name, b.ink_name))
                zones.append(
                    TrapZone(
                        page_index=page_index,
                        rect_pt=seam,
                        from_ink=from_ink,
                        to_ink=to_ink,
                    )
                )
    return zones


# --- Resource walking ---------------------------------------------------


def _spot_ink_aliases(resources: Object | None) -> dict[str, str]:
    """Map page-local color-space aliases (``/PMS_185``) to ink names.

    Walks ``/Resources/ColorSpace`` and returns ``{alias: ink_name}``
    for every Separation or DeviceN entry. Process-only colorants are
    skipped — there's nothing to trap on those.
    """
    if resources is None or not isinstance(resources, pikepdf.Dictionary):
        return {}
    cs_dict = resources.get(Name.ColorSpace)
    if not isinstance(cs_dict, pikepdf.Dictionary):
        return {}
    aliases: dict[str, str] = {}
    for alias in list(cs_dict.keys()):
        space = cs_dict[alias]
        ink = _resolve_spot_name(space)
        if ink is not None:
            key = str(alias)
            aliases[key.removeprefix("/")] = ink
    return aliases


def _resolve_spot_name(space: Object) -> str | None:
    """If ``space`` is a Separation or DeviceN array, return the ink
    name (first colorant). Returns ``None`` for non-spot spaces."""
    if not isinstance(space, pikepdf.Array) or len(space) < 2:
        return None
    family = space[0]
    if family == Name.Separation:
        return _name_to_str(space[1])
    if family == Name.DeviceN:
        names = space[1]
        if isinstance(names, pikepdf.Array) and len(names) >= 1:
            return _name_to_str(names[0])
    return None


def _name_to_str(obj: Object) -> str | None:
    s = str(obj)
    # pikepdf Names render as "/Foo"; strip the leading slash.
    return s.removeprefix("/") if s.startswith("/") else s


# --- Content-stream walking ---------------------------------------------


def _iter_ink_rects(
    page: pikepdf.Page,
    ink_aliases: dict[str, str],
) -> list[tuple[str, tuple[float, float, float, float]]]:
    """Walk the page's content stream tokens, return ``(ink, rect)`` tuples.

    Graphics state tracked: current fill color-space alias (via ``cs``),
    pending rectangles (``re``), and fill operators (``f``/``F``/``B``/
    ``f*``/``B*``). A rect emits a record only when followed by a fill
    that resolves through one of the page's spot aliases.
    """
    out: list[tuple[str, tuple[float, float, float, float]]] = []
    current_cs: str | None = None
    pending_rects: list[tuple[float, float, float, float]] = []

    try:
        instructions = pikepdf.parse_content_stream(page)
    except Exception:  # pragma: no cover — malformed content streams
        return out

    for operands, operator in instructions:  # type: ignore[misc]
        op = bytes(operator).decode("latin-1") if hasattr(operator, "__bytes__") else str(operator)
        op = op.strip()

        if op == "cs":
            current_cs = _operand_name(operands[0]) if operands else None
        elif op == "CS":
            # Stroke color-space change — we only care about fills for trap.
            pass
        elif op == "re" and len(operands) >= 4:
            x = float(operands[0])
            y = float(operands[1])
            w = float(operands[2])
            h = float(operands[3])
            pending_rects.append((x, y, x + w, y + h))
        elif op in {"f", "F", "f*", "B", "B*"}:
            if current_cs is not None and current_cs in ink_aliases:
                ink = ink_aliases[current_cs]
                for rect in pending_rects:
                    out.append((ink, _normalize_rect(rect)))
            pending_rects.clear()
        elif op == "S" or op == "s":
            # Stroke-only — no fill, no trap candidate.
            pending_rects.clear()
        elif op == "n":
            # No-op path painter (used for clipping).
            pending_rects.clear()
        # ``q`` / ``Q`` (graphics-state push/pop) are intentionally not
        # tracked at the cs level for v1 — the extractor is conservative
        # rather than complete.
    return out


def _operand_name(obj: object) -> str | None:
    if obj is None:
        return None
    s = str(obj)
    return s.removeprefix("/") if s.startswith("/") else s


def _normalize_rect(
    rect: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """Coerce negative width/height (``re`` accepts them) to canonical form."""
    x0, y0, x1, y1 = rect
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


# --- Adjacency detection ------------------------------------------------


def _vertical_seam(
    a: InkRect, b: InkRect, tol: float, width: float
) -> tuple[float, float, float, float] | None:
    """Return the seam rect if ``a`` and ``b`` share a vertical edge."""
    ax0, ay0, ax1, ay1 = a.rect_pt
    bx0, by0, bx1, by1 = b.rect_pt
    # Cross-axis (y) overlap.
    y0 = max(ay0, by0)
    y1 = min(ay1, by1)
    if y1 - y0 < tol:
        return None
    # a's right edge meets b's left edge.
    if abs(ax1 - bx0) <= tol:
        cx = (ax1 + bx0) / 2
        return (cx - width / 2, y0, cx + width / 2, y1)
    # a's left edge meets b's right edge.
    if abs(ax0 - bx1) <= tol:
        cx = (ax0 + bx1) / 2
        return (cx - width / 2, y0, cx + width / 2, y1)
    return None


def _horizontal_seam(
    a: InkRect, b: InkRect, tol: float, width: float
) -> tuple[float, float, float, float] | None:
    ax0, ay0, ax1, ay1 = a.rect_pt
    bx0, by0, bx1, by1 = b.rect_pt
    x0 = max(ax0, bx0)
    x1 = min(ax1, bx1)
    if x1 - x0 < tol:
        return None
    if abs(ay1 - by0) <= tol:
        cy = (ay1 + by0) / 2
        return (x0, cy - width / 2, x1, cy + width / 2)
    if abs(ay0 - by1) <= tol:
        cy = (ay0 + by1) / 2
        return (x0, cy - width / 2, x1, cy + width / 2)
    return None


__all__ = [
    "InkRect",
    "auto_trap_zones",
    "extract_ink_rects",
]
