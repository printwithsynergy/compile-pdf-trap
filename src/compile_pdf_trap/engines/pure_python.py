"""Pure-Python trap engine — codex-driven spread/choke geometry.

Uses ``codex_pdf.geom.polygon_offset`` (1.1.0) for the spread/choke
operation and ``codex_pdf.color.resolve_spot_swatch_color`` for the
ink-name → device color resolution. Bit-deterministic by construction:
no wall-clock time, no random IDs, ``Pdf.save(deterministic_id=True)``.

Engine fingerprint pattern (consumed by lineage / trap-diff):

    pure_python@1.0.0+codex_pdf@<wheel-version>

The fingerprint is the only non-deterministic-looking value the engine
emits; cross-engine determinism is not claimed (spec §5.5).
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

import pikepdf
from codex_pdf.color import (
    CodexSpotIntent,
    SpotSwatchResolution,
    delta_e_2000,
    resolve_spot_swatch_color,
)
from codex_pdf.geom import Box, Path, polygon_offset

from compile_pdf_trap.policy_schema import (
    InkPairRule,
    NeutralDensitySource,
    TrapDirection,
    TrapPolicy,
    TrapZone,
)

ENGINE_NAME = "pure_python"
ENGINE_VERSION = "1.0.0"


@dataclass(frozen=True)
class TrapApplication:
    """One trap operation record — the building block of trap-diff."""

    page_index: int
    rect_pt: tuple[float, float, float, float]
    from_ink: str
    to_ink: str
    direction: TrapDirection
    width_pt: float
    from_rgb: tuple[int, int, int]
    to_rgb: tuple[int, int, int]
    delta_e: float
    trap_polygon_pt: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class TrapEngineResult:
    """Outcome of one engine run."""

    output_bytes: bytes
    pdf_sha256: str
    operations: tuple[TrapApplication, ...]
    engine_fingerprint: str


class TrapEngineError(ValueError):
    """The policy or input is incompatible with this engine. Raised
    before any mutation is committed."""


def engine_fingerprint() -> str:
    """Stable identifier the lineage record + trap-diff carry."""
    try:
        from codex_pdf import __version__ as codex_version
    except ImportError:  # pragma: no cover — codex-pdf is a hard dep
        codex_version = "unknown"
    return f"{ENGINE_NAME}@{ENGINE_VERSION}+codex_pdf@{codex_version}"


def apply(input_bytes: bytes, policy: TrapPolicy) -> TrapEngineResult:
    """Apply trap zones onto pages of ``input_bytes`` and return the
    rewritten PDF + per-operation diff records."""
    rules_by_pair = _index_rules(policy)

    pdf = pikepdf.open(io.BytesIO(input_bytes))
    ocg: pikepdf.Object | None = None
    if policy.output_trap_layer:
        ocg = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.OCG,
                Name=pikepdf.String("Traps"),
                Intent=pikepdf.Array([pikepdf.Name.View, pikepdf.Name.Design]),
            )
        )
        _ensure_ocg_in_root(pdf, ocg)
    operations: list[TrapApplication] = []
    try:
        for zone in policy.trap_zones:
            if zone.page_index >= len(pdf.pages):
                raise TrapEngineError(
                    f"trap_zone.page_index {zone.page_index} >= page count {len(pdf.pages)}"
                )
            rule = rules_by_pair.get((zone.from_ink, zone.to_ink))
            width = rule.width_pt if rule is not None else policy.default_trap_width_pt
            direction = _resolve_direction(zone, rule, policy)

            from_resolution = _resolve_ink(zone.from_ink)
            to_resolution = _resolve_ink(zone.to_ink)

            trap_polygon = _compute_trap_polygon_for_zone(zone, direction, width)
            page = pdf.pages[zone.page_index]
            _stamp_overlap(pdf, page, trap_polygon, from_resolution, ocg)

            operations.append(
                TrapApplication(
                    page_index=zone.page_index,
                    rect_pt=zone.rect_pt or _polygon_bbox(zone.polygon_pt or ()),
                    from_ink=zone.from_ink,
                    to_ink=zone.to_ink,
                    direction=direction,
                    width_pt=width,
                    from_rgb=from_resolution.rgb,
                    to_rgb=to_resolution.rgb,
                    delta_e=_delta_e_between(from_resolution, to_resolution),
                    trap_polygon_pt=trap_polygon,
                )
            )

        out = io.BytesIO()
        pdf.save(out, deterministic_id=True, linearize=False)
    finally:
        pdf.close()

    output_bytes = out.getvalue()
    return TrapEngineResult(
        output_bytes=output_bytes,
        pdf_sha256=hashlib.sha256(output_bytes).hexdigest(),
        operations=tuple(operations),
        engine_fingerprint=engine_fingerprint(),
    )


# --- Helpers ------------------------------------------------------------


def _index_rules(policy: TrapPolicy) -> dict[tuple[str, str], InkPairRule]:
    return {(r.from_ink, r.to_ink): r for r in policy.ink_pair_rules}


def _resolve_direction(
    zone: TrapZone, rule: InkPairRule | None, policy: TrapPolicy
) -> TrapDirection:
    """Resolve the effective spread/choke direction for a zone.

    Priority order:
    1. Explicit per-pair rule direction (non-"auto") always wins.
    2. Policy-level ``spread_choke`` override (non-"auto") wins over density.
    3. Density-based auto: Lab L* from codex when
       ``neutral_density_source="codex_extract"`` (default); RGB luminance
       approximation when ``"operator"`` (operator declares rules; auto
       pairs fall back gracefully).
    """
    if rule is not None and rule.direction != "auto":
        return rule.direction
    if policy.spread_choke != "auto":
        return policy.spread_choke
    return _density_direction(zone.from_ink, zone.to_ink, policy.neutral_density_source)


# Canonical process-ink colors. The Codex spot resolver hashes unknown
# names, which is correct for arbitrary spot inks but unhelpful for the
# standard process-ink abbreviations (C, M, Y, K, Process Cyan, ...).
# Apply a thin shim so callers using shorthand ink names get sensible
# colors and delta_e values.
_PROCESS_INK_INTENTS: dict[str, CodexSpotIntent] = {
    "C": CodexSpotIntent(cmyk=(100, 0, 0, 0), lab=(60.5, -34.0, -50.0)),
    "M": CodexSpotIntent(cmyk=(0, 100, 0, 0), lab=(48.0, 75.0, -6.0)),
    "Y": CodexSpotIntent(cmyk=(0, 0, 100, 0), lab=(89.0, -5.0, 93.0)),
    "K": CodexSpotIntent(cmyk=(0, 0, 0, 100), lab=(0.0, 0.0, 0.0)),
}


def _resolve_ink(ink_name: str) -> SpotSwatchResolution:
    """Resolve an ink name with process-ink shorthand support."""
    intent = _PROCESS_INK_INTENTS.get(ink_name.upper())
    return resolve_spot_swatch_color(ink_name, codex_intent=intent)


def _density_direction(
    from_ink: str,
    to_ink: str,
    source: NeutralDensitySource = "codex_extract",
) -> TrapDirection:
    """Lighter ink (lower neutral density) spreads into darker.

    When ``source="codex_extract"`` (default), derives lightness from
    the Lab L* value returned by codex's spot-color resolver (which
    includes AI Lab enrichment for unknown ink names). When
    ``source="operator"``, falls back to an RGB-luminance approximation
    — this is the correct degraded-mode path when the operator declares
    all important pairs explicitly and AI Lab values aren't needed.
    """
    from_res = _resolve_ink(from_ink)
    to_res = _resolve_ink(to_ink)
    if source == "codex_extract":
        # Lab L* is the authoritative lightness signal; 0 = black, 100 = white.
        from_l = from_res.lab[0] if from_res.lab else _luminance(from_res.rgb) / 255.0 * 100
        to_l = to_res.lab[0] if to_res.lab else _luminance(to_res.rgb) / 255.0 * 100
    else:
        from_l = _luminance(from_res.rgb)
        to_l = _luminance(to_res.rgb)
    return "spread" if from_l > to_l else "choke"


def _luminance(rgb: tuple[int, int, int]) -> float:
    """ITU-R BT.709 luminance over 0..255 RGB. Stable across runs."""
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _compute_trap_polygon(
    rect: tuple[float, float, float, float],
    direction: TrapDirection,
    width_pt: float,
) -> tuple[tuple[float, float], ...]:
    """Use ``codex.polygon_offset`` to inflate (spread) or deflate
    (choke) the boundary rectangle by ``width_pt``."""
    box = Box(*rect)
    distance = width_pt if direction == "spread" else -width_pt
    offset = polygon_offset(Path.from_box(box), distance)
    if not offset.rings:
        raise TrapEngineError(
            f"polygon_offset collapsed rect {rect} at distance {distance:+.4f} pt; "
            "increase trap rect or reduce width_pt."
        )
    return tuple(offset.rings[0])


def _compute_trap_polygon_for_zone(
    zone: TrapZone,
    direction: TrapDirection,
    width_pt: float,
) -> tuple[tuple[float, float], ...]:
    """Dispatch zone shape to the rect fast-path or the polygon path.

    Both shapes now route through ``codex_pdf.geom.polygon_offset``;
    the rect path uses ``Path.from_box`` for clarity (codex's internal
    fast-path detects axis-aligned rectangles automatically).
    """
    if zone.rect_pt is not None:
        return _compute_trap_polygon(zone.rect_pt, direction, width_pt)
    assert zone.polygon_pt is not None  # schema invariant

    distance = width_pt if direction == "spread" else -width_pt
    polygon_path = Path(rings=(list(zone.polygon_pt),))
    offset = polygon_offset(polygon_path, distance)
    if not offset.rings:
        raise TrapEngineError(
            f"polygon_offset collapsed polygon at distance {distance:+.4f} pt; "
            "increase polygon size or reduce width_pt."
        )
    return tuple(offset.rings[0])


def _polygon_bbox(
    polygon: tuple[tuple[float, float], ...],
) -> tuple[float, float, float, float]:
    """Axis-aligned bounding box of ``polygon``, for diff reporting."""
    if not polygon:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


def _delta_e_between(
    from_resolution: SpotSwatchResolution, to_resolution: SpotSwatchResolution
) -> float:
    """CIEDE2000 between the two ink Lab triplets.

    When neither swatch carried Lab data, fall back to a Lab synthesized
    from the resolved RGB via a simple sRGB→Lab approximation. Result is
    monotonic in perceptual difference, sufficient for the verify Layer 6
    tolerance check.
    """
    lab_a = from_resolution.lab or _approx_lab(from_resolution.rgb)
    lab_b = to_resolution.lab or _approx_lab(to_resolution.rgb)
    return float(delta_e_2000(lab_a, lab_b))


def _approx_lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    """Approximate sRGB → Lab without dragging colormath in. The L
    component is the dominant signal for trap delta_e checks; we
    approximate from BT.709 luminance and leave a/b at 0.

    This is intentionally conservative (over-reports neutral pairs as
    similar). Production callers that care about chromaticity should
    pass policy zones referencing inks with explicit ``CodexSpotIntent``
    Lab values via the (future) extra_pantone_overrides surface.
    """
    lum = _luminance(rgb) / 255.0
    # Rough Lab L for sRGB linear luminance: L* = 116 * Y^(1/3) - 16 (Y normalized).
    l_star = 116.0 * lum ** (1.0 / 3.0) - 16.0 if lum > 0.008856 else 903.3 * lum
    return (l_star, 0.0, 0.0)


def _ensure_ocg_in_root(pdf: pikepdf.Pdf, ocg: pikepdf.Object) -> None:
    """Add ``ocg`` to ``pdf.Root.OCProperties``, creating the structure if absent."""
    if pikepdf.Name.OCProperties not in pdf.Root:
        pdf.Root.OCProperties = pikepdf.Dictionary(
            OCGs=pikepdf.Array([ocg]),
            D=pikepdf.Dictionary(
                ON=pikepdf.Array([ocg]),
                OFF=pikepdf.Array(),
                Order=pikepdf.Array([ocg]),
                RBGroups=pikepdf.Array(),
            ),
        )
    else:
        ocgs = pdf.Root.OCProperties.OCGs
        if ocg not in ocgs:
            ocgs.append(ocg)
        on = pdf.Root.OCProperties.D.ON
        if ocg not in on:
            on.append(ocg)


def _stamp_overlap(
    pdf: pikepdf.Pdf,
    page: pikepdf.Page,
    polygon: tuple[tuple[float, float], ...],
    color: SpotSwatchResolution,
    ocg: pikepdf.Object | None = None,
) -> None:
    """Append a content stream that fills ``polygon`` with the resolved
    RGB color. When ``ocg`` is provided, wraps the content in a BDC/EMC
    marked-content sequence so PDF viewers can toggle the trap layer."""
    r, g, b = color.rgb
    norm = (r / 255.0, g / 255.0, b / 255.0)
    if ocg is not None:
        resources = page.Resources
        if pikepdf.Name.Properties not in resources:
            resources[pikepdf.Name.Properties] = pikepdf.Dictionary()
        resources[pikepdf.Name.Properties][pikepdf.Name.TrapLayer] = ocg
        parts = [
            "/OC /TrapLayer BDC\n",
            "q\n",
            f"{norm[0]:.4f} {norm[1]:.4f} {norm[2]:.4f} rg\n",
        ]
        parts.append(f"{polygon[0][0]:.4f} {polygon[0][1]:.4f} m\n")
        for px, py in polygon[1:]:
            parts.append(f"{px:.4f} {py:.4f} l\n")
        parts.append("h f Q\n")
        parts.append("EMC\n")
    else:
        parts = ["q\n", f"{norm[0]:.4f} {norm[1]:.4f} {norm[2]:.4f} rg\n"]
        parts.append(f"{polygon[0][0]:.4f} {polygon[0][1]:.4f} m\n")
        for px, py in polygon[1:]:
            parts.append(f"{px:.4f} {py:.4f} l\n")
        parts.append("h f Q\n")
    overlay = "".join(parts).encode("ascii")
    page.contents_add(pdf.make_stream(overlay), prepend=False)


# Used by selectors that need to coerce a Pydantic intent without
# importing the full trap stack.
__all__ = [
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "TrapApplication",
    "TrapEngineError",
    "TrapEngineResult",
    "apply",
    "engine_fingerprint",
]
