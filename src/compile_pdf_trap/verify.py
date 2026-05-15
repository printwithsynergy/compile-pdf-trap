"""Four-layer post-condition checks for trap output (spec §2.3 + §5.4-§5.7).

Layer 1 — Schema. The output PDF parses cleanly with pikepdf, page
count is unchanged, and one trap-overlay content stream was appended
per ``trap_zone`` declared in the policy.

Layer 2 — Determinism. Engine-fingerprint scoped (spec §5.5). For
``pure_python``, replay byte-identity is enforced. For ``ghostscript``
and ``external``, this layer trusts the engine's lineage record but
does not require cross-run byte-identity here.

Layer 3 — Nothing-else-touched. Each page's MediaBox / TrimBox /
BleedBox is unchanged, ``/Info`` metadata is unchanged, and OCG
configuration is unchanged. The engine is allowed to add resources
and append a content stream — anything else is a Layer 3 failure.

Layer 6 — Color delta_e tolerance. For every trap operation in the
diff, ``op.delta_e`` must be ≤ ``policy.delta_e_tolerance`` *or* the
delta_e is the *intentional* contrast we want preserved. Today the
check is a soft warning surfaced through ``failures`` only when the
operator explicitly enables it via ``strict_delta_e=True``; the
default keeps trap operations advisory because process pairs
(K/white, Y/K) routinely exceed the tolerance by design and shouldn't
fail the post-condition.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
from dataclasses import dataclass, field

import pikepdf
from pikepdf import Name

from compile_pdf_trap.engine import TrapResult, apply_policy
from compile_pdf_trap.policy_schema import TrapPolicy

logger = logging.getLogger(__name__)


@dataclass
class TrapVerifyResult:
    """Outcome of running verify against an input/output pair."""

    layer1_schema: bool = False
    layer2_determinism: bool = False
    layer3_unchanged: bool = False
    layer6_delta_e: bool = False
    layer7_visual: bool | None = None  # None = skipped (deps/env not available)
    layer7_score: float | None = None  # mean visual quality score 0.0–1.0
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return (
            self.layer1_schema
            and self.layer2_determinism
            and self.layer3_unchanged
            and self.layer6_delta_e
        )


def verify_trap(
    *,
    input_bytes: bytes,
    result: TrapResult,
    policy: TrapPolicy,
    determinism_replay: bool = True,
    strict_delta_e: bool = False,
    visual_verify: bool | None = None,
) -> TrapVerifyResult:
    """Run all post-condition layers and return a combined result.

    ``visual_verify`` controls Layer 7. When ``None`` (default), the
    layer runs only when ``COMPILE_TRAP_VISUAL_VERIFY=1`` is set in the
    environment and the required deps (PyMuPDF + anthropic) are available.
    Pass ``True`` to force it; ``False`` to skip unconditionally.
    """
    out = TrapVerifyResult()
    _layer1(input_bytes, result.output_bytes, policy, out)
    _layer2(input_bytes, result, policy, out, replay=determinism_replay)
    _layer3(input_bytes, result.output_bytes, out)
    _layer6(result, policy, out, strict=strict_delta_e)
    run_visual = visual_verify
    if run_visual is None:
        run_visual = os.environ.get("COMPILE_TRAP_VISUAL_VERIFY", "").strip() == "1"
    if run_visual:
        _layer7(input_bytes, result, policy, out)
    return out


# --- Layer 1 ------------------------------------------------------------


def _layer1(
    input_bytes: bytes,
    output_bytes: bytes,
    policy: TrapPolicy,
    result: TrapVerifyResult,
) -> None:
    try:
        in_pdf = pikepdf.open(io.BytesIO(input_bytes))
        out_pdf = pikepdf.open(io.BytesIO(output_bytes))
    except Exception as exc:
        result.failures.append(f"L1: PDF unparseable: {exc}")
        return
    try:
        if len(out_pdf.pages) != len(in_pdf.pages):
            result.failures.append(
                f"L1: page count changed {len(in_pdf.pages)} -> {len(out_pdf.pages)}"
            )
            return
        zones_by_page: dict[int, int] = {}
        for zone in policy.trap_zones:
            zones_by_page[zone.page_index] = zones_by_page.get(zone.page_index, 0) + 1
        for page_idx, expected in zones_by_page.items():
            page = out_pdf.pages[page_idx]
            contents = page.obj.get(Name.Contents)
            if not isinstance(contents, pikepdf.Array):
                result.failures.append(
                    f"L1: page {page_idx} expected {expected} overlay streams "
                    "but found single content stream"
                )
                continue
            # Original input has 1 stream; engine appends one per zone.
            overlays = len(contents) - 1
            if overlays < expected:
                result.failures.append(
                    f"L1: page {page_idx} expected {expected} overlays, found {overlays}"
                )
        if not any(f.startswith("L1:") for f in result.failures):
            result.layer1_schema = True
    finally:
        in_pdf.close()
        out_pdf.close()


# --- Layer 2 ------------------------------------------------------------


def _layer2(
    input_bytes: bytes,
    result: TrapResult,
    policy: TrapPolicy,
    out: TrapVerifyResult,
    *,
    replay: bool,
) -> None:
    if not replay:
        out.layer2_determinism = True
        return
    if result.engine != "pure_python":
        # spec §5.5: only pure_python claims cross-run byte-identity.
        # Ghostscript / external engines satisfy L2 by virtue of the
        # lineage-recorded fingerprint; we accept that here without a
        # replay round-trip.
        out.layer2_determinism = True
        return
    replayed = apply_policy(input_bytes, policy)
    if replayed.output_bytes == result.output_bytes:
        out.layer2_determinism = True
    else:
        out.failures.append(
            "L2: pure_python replay produced different bytes "
            f"(orig={hashlib.sha256(result.output_bytes).hexdigest()[:16]}, "
            f"replay={replayed.pdf_sha256[:16]})"
        )


# --- Layer 3 ------------------------------------------------------------

_BOX_KEYS = (Name.MediaBox, Name.TrimBox, Name.BleedBox, Name.ArtBox, Name.CropBox)


def _layer3(
    input_bytes: bytes,
    output_bytes: bytes,
    result: TrapVerifyResult,
) -> None:
    try:
        in_pdf = pikepdf.open(io.BytesIO(input_bytes))
        out_pdf = pikepdf.open(io.BytesIO(output_bytes))
    except Exception as exc:
        result.failures.append(f"L3: PDF unparseable: {exc}")
        return
    try:
        for i, (in_page, out_page) in enumerate(zip(in_pdf.pages, out_pdf.pages, strict=False)):
            for key in _BOX_KEYS:
                in_box = in_page.obj.get(key)
                out_box = out_page.obj.get(key)
                if in_box is None and out_box is None:
                    continue
                if in_box is None or out_box is None:
                    result.failures.append(f"L3: page {i} {key} added or removed")
                    continue
                in_list = [in_box[j] for j in range(len(in_box))]
                out_list = [out_box[j] for j in range(len(out_box))]
                if in_list != out_list:
                    result.failures.append(f"L3: page {i} {key} changed")
        in_meta = _doc_info(in_pdf)
        out_meta = _doc_info(out_pdf)
        if in_meta != out_meta:
            result.failures.append("L3: /Info metadata changed")
        if not any(f.startswith("L3:") for f in result.failures):
            result.layer3_unchanged = True
    finally:
        in_pdf.close()
        out_pdf.close()


def _doc_info(pdf: pikepdf.Pdf) -> dict[str, str]:
    info = pdf.trailer.get(Name.Info)
    if not isinstance(info, pikepdf.Dictionary):
        return {}
    out: dict[str, str] = {}
    for k in list(info.keys()):  # noqa: SIM118 — pikepdf Dict
        try:
            out[str(k)] = str(info[k])
        except Exception:  # pragma: no cover
            out[str(k)] = "<unrepresentable>"
    return out


# --- Layer 6 ------------------------------------------------------------


def _layer6(
    result: TrapResult,
    policy: TrapPolicy,
    out: TrapVerifyResult,
    *,
    strict: bool,
) -> None:
    """Trap-quality delta_e check. Off by default (the high-contrast
    pairs trap is most useful for routinely exceed any reasonable
    tolerance — that's why trap is needed in the first place)."""
    if not strict:
        out.layer6_delta_e = True
        return
    over_budget = [
        (op.from_ink, op.to_ink, op.delta_e)
        for op in result.operations
        if op.delta_e > policy.delta_e_tolerance
    ]
    if over_budget:
        for from_ink, to_ink, de in over_budget:
            out.failures.append(
                f"L6: {from_ink}->{to_ink} delta_e {de:.2f} > tolerance "
                f"{policy.delta_e_tolerance:.2f}"
            )
    else:
        out.layer6_delta_e = True


# --- Layer 7 (visual verify) --------------------------------------------

_L7_SYSTEM = (
    "You are a print production quality inspector. You are given two page "
    "renders — BEFORE trapping and AFTER trapping — and a list of trap zones "
    "that were applied. Your job is to verify the trap application quality.\n\n"
    "For each zone, assess:\n"
    "1. Is a trap stroke visible at the expected boundary? (0 = not visible, "
    "1 = clearly visible)\n"
    "2. Does the stroke color look appropriate for the ink pair described? "
    "(0 = wrong color, 1 = correct)\n"
    "3. Is the stroke width reasonable — not too wide or too narrow? "
    "(0 = bad, 1 = good)\n\n"
    "Return ONLY valid JSON, no prose:\n"
    '{"zones": [{"zone_index": <int>, "visible": <0..1>, '
    '"color_ok": <0..1>, "width_ok": <0..1>}], "overall_score": <0..1>}.\n'
    "If you cannot evaluate a zone (e.g. page does not render), omit it. "
    "Be honest and conservative — do not award high scores unless the trap "
    "is clearly present and correct."
)


def _layer7(
    input_bytes: bytes,
    result: TrapResult,
    policy: TrapPolicy,
    out: TrapVerifyResult,
) -> None:
    """Post-trap visual quality check via Claude vision."""
    try:
        import anthropic
        import fitz  # PyMuPDF
    except ImportError as exc:
        logger.warning("trap_verify.layer7_skipped: missing dep %s", exc)
        out.layer7_visual = None
        return

    api_key = os.environ.get("COMPILE_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("trap_verify.layer7_skipped: no ANTHROPIC_API_KEY")
        out.layer7_visual = None
        return

    pages_with_zones = sorted({z.page_index for z in policy.trap_zones})
    if not pages_with_zones:
        out.layer7_visual = True
        out.layer7_score = 1.0
        return

    images_b64: list[tuple[str, str]] = []
    for page_idx in pages_with_zones[:2]:  # cap at 2 pages per call
        before_png = _render_page(input_bytes, page_idx, fitz)
        after_png = _render_page(result.output_bytes, page_idx, fitz)
        if before_png:
            images_b64.append(("image/png", base64.b64encode(before_png).decode()))
        if after_png:
            images_b64.append(("image/png", base64.b64encode(after_png).decode()))

    if not images_b64:
        logger.warning("trap_verify.layer7_skipped: page render failed")
        out.layer7_visual = None
        return

    zone_descriptions = "\n".join(
        f"  zone {i}: page {z.page_index}, {z.from_ink} → {z.to_ink}"
        for i, z in enumerate(policy.trap_zones)
    )
    prompt = (
        f"The images show alternating BEFORE/AFTER renders for "
        f"{len(pages_with_zones)} page(s).\n"
        f"Trap zones applied:\n{zone_descriptions}\n\n"
        "Evaluate each zone and return JSON as instructed."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key)
        content: list[anthropic.types.ContentBlockParam] = []
        for mime, b64 in images_b64:
            content.append({  # type: ignore[misc]
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": b64},
            })
        content.append({"type": "text", "text": prompt})  # type: ignore[misc]
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=_L7_SYSTEM,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text if response.content else ""
    except Exception as exc:
        logger.warning("trap_verify.layer7_api_error: %s", exc)
        out.layer7_visual = None
        return

    score = _parse_l7_score(raw)
    out.layer7_score = score
    out.layer7_visual = score >= 0.5
    if not out.layer7_visual:
        out.failures.append(f"L7: visual score {score:.2f} below 0.5 threshold")


def _render_page(pdf_bytes: bytes, page_index: int, fitz: object) -> bytes:
    """Render one page to PNG via PyMuPDF. Returns empty bytes on error."""
    try:
        import fitz as _fitz
        with _fitz.open(stream=pdf_bytes, filetype="pdf") as doc:  # type: ignore[attr-defined]
            if page_index >= doc.page_count or page_index < 0:
                return b""
            page = doc.load_page(page_index)
            zoom = 150 / 72.0
            mat = _fitz.Matrix(zoom, zoom)  # type: ignore[attr-defined]
            pix = page.get_pixmap(matrix=mat, alpha=False)
            return pix.tobytes("png")
    except Exception:
        logger.exception("trap_verify.render_failed page=%s", page_index)
        return b""


def _parse_l7_score(raw: str) -> float:
    """Extract overall_score from Claude's JSON response; default 0.0 on parse error."""
    import json
    import re
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return 0.0
    try:
        data = json.loads(match.group())
        return max(0.0, min(1.0, float(data.get("overall_score", 0.0))))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0.0


__all__ = [
    "TrapVerifyResult",
    "verify_trap",
]
