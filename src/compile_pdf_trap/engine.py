"""Trap engine top-level dispatch — pick a backend per spec §5.3.

Selector precedence:

1. ``policy.engine`` if not ``"auto"``
2. ``COMPILE_TRAP_ENGINE`` env var
3. ``"pure_python"`` (the default once codex 1.5+ ships, which it has)

The ``apply_policy`` entrypoint forwards to the chosen backend module
and unifies the return shape into :class:`TrapResult` (engine result +
the trap-diff JSON-ready dict).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Protocol

from compile_pdf_trap.engines import external as external_engine
from compile_pdf_trap.engines import ghostscript as gs_engine
from compile_pdf_trap.engines import pure_python as pp_engine
from compile_pdf_trap.engines.pure_python import (
    TrapApplication,
    TrapEngineError,
    TrapEngineResult,
)
from compile_pdf_trap.extract import auto_trap_zones
from compile_pdf_trap.policy_schema import EngineSelector, TrapPolicy, TrapZone

logger = logging.getLogger(__name__)


class _EngineModule(Protocol):
    def apply(self, input_bytes: bytes, policy: TrapPolicy) -> TrapEngineResult: ...
    def engine_fingerprint(self) -> str: ...


_ENGINES: dict[str, _EngineModule] = {
    "pure_python": pp_engine,
    "ghostscript": gs_engine,
    "external": external_engine,
}


@dataclass(frozen=True)
class TrapResult:
    """Top-level outcome — output PDF + trap-diff artifact dict."""

    output_bytes: bytes
    pdf_sha256: str
    operations: tuple[TrapApplication, ...]
    engine: str
    engine_fingerprint: str
    trap_diff: dict[str, object]


def apply_policy(input_bytes: bytes, policy: TrapPolicy) -> TrapResult:
    """Apply ``policy`` to ``input_bytes`` and return the trapped PDF
    plus a populated trap-diff artifact.

    Zone resolution order:
    1. Codex AI zones (when ``trap_zones_source='codex_extract'`` + ``codex_job_id``).
    2. Operator-declared ``policy.trap_zones`` (always augments/overrides codex zones).
    3. Content-stream auto-detection (when ``auto_detect_zones=True`` and step 1+2 yield no zones).
    """
    effective_policy = policy
    auto_detected_count = 0

    if policy.trap_zones_source == "codex_extract" and policy.codex_job_id:
        codex_zones = _fetch_codex_zones(policy.codex_job_id)
        if codex_zones:
            # Codex zones seed the list; declared policy zones augment/override.
            merged = codex_zones + list(policy.trap_zones)
            effective_policy = policy.model_copy(update={"trap_zones": merged})
            logger.info(
                "trap_engine.codex_zones_merged",
                codex_job_id=policy.codex_job_id,
                codex_zone_count=len(codex_zones),
                total_zone_count=len(merged),
            )

    if not effective_policy.trap_zones and effective_policy.auto_detect_zones:
        detected = auto_trap_zones(input_bytes)
        if detected:
            effective_policy = effective_policy.model_copy(update={"trap_zones": detected})
            auto_detected_count = len(detected)
            logger.info(
                "trap_engine.auto_zones_detected",
                zone_count=auto_detected_count,
            )

    engine_name = _resolve_engine(effective_policy.engine)
    engine = _ENGINES.get(engine_name)
    if engine is None:
        raise TrapEngineError(
            f"unknown trap engine {engine_name!r}; "
            "expected one of pure_python | ghostscript | external"
        )
    result = engine.apply(input_bytes, effective_policy)
    diff = _build_trap_diff(effective_policy, engine_name, result, auto_detected_count)
    return TrapResult(
        output_bytes=result.output_bytes,
        pdf_sha256=result.pdf_sha256,
        operations=result.operations,
        engine=engine_name,
        engine_fingerprint=result.engine_fingerprint,
        trap_diff=diff,
    )


def _resolve_engine(policy_value: EngineSelector) -> str:
    """Selector chain: explicit policy → env var → pure_python."""
    if policy_value != "auto":
        return policy_value
    env_value = os.environ.get("COMPILE_TRAP_ENGINE", "").strip().lower()
    if env_value:
        return env_value
    return "pure_python"


# --- codex zone fetch ---------------------------------------------------

_CODEX_CONFIDENCE_THRESHOLD = 0.7


def _fetch_codex_zones(codex_job_id: str) -> list[TrapZone]:
    """Fetch trap_zone_candidates from the codex API for a given document.

    Filters candidates below ``_CODEX_CONFIDENCE_THRESHOLD`` and converts
    the remainder to :class:`TrapZone` polygon entries. Returns an empty
    list (never raises) on any fetch or parse failure — callers fall back
    to the declared ``policy.trap_zones`` silently.
    """
    try:
        import httpx
    except ImportError:
        logger.warning("trap_engine.codex_fetch_skipped: httpx not installed")
        return []
    url = os.environ.get("CODEX_API_URL", "http://localhost:8001").rstrip("/")
    try:
        resp = httpx.get(f"{url}/v1/documents/{codex_job_id}", timeout=15.0)
        resp.raise_for_status()
        data: Any = resp.json()
    except Exception as exc:
        logger.warning("trap_engine.codex_fetch_failed", codex_job_id=codex_job_id, error=str(exc))
        return []
    return _parse_codex_zones(data)


def _parse_codex_zones(data: Any) -> list[TrapZone]:
    if not isinstance(data, dict):
        return []
    zones: list[TrapZone] = []
    for page in data.get("pages", []):
        if not isinstance(page, dict):
            continue
        for cand in page.get("trap_zone_candidates", []):
            if not isinstance(cand, dict):
                continue
            if float(cand.get("confidence", 0)) < _CODEX_CONFIDENCE_THRESHOLD:
                continue
            from_ink = cand.get("from_ink")
            to_ink = cand.get("to_ink")
            if not isinstance(from_ink, str) or not from_ink:
                continue
            if not isinstance(to_ink, str) or not to_ink:
                continue
            raw_poly = cand.get("polygon_pt", [])
            if not isinstance(raw_poly, list) or len(raw_poly) < 3:
                continue
            try:
                polygon: tuple[tuple[float, float], ...] = tuple(
                    (float(pt[0]), float(pt[1])) for pt in raw_poly
                )
            except (TypeError, ValueError, IndexError):
                continue
            try:
                page_index = int(cand["page_index"])
            except (KeyError, TypeError, ValueError):
                continue
            try:
                zones.append(
                    TrapZone(
                        page_index=page_index,
                        polygon_pt=polygon,
                        from_ink=from_ink,
                        to_ink=to_ink,
                    )
                )
            except Exception:
                continue
    return zones


# --- trap-diff artifact -------------------------------------------------


TRAP_DIFF_SCHEMA_VERSION = "1.0.0"


def _build_trap_diff(
    policy: TrapPolicy,
    engine_name: str,
    result: TrapEngineResult,
    auto_detected_zone_count: int = 0,
) -> dict[str, object]:
    """Produce a JSON-serializable trap-diff record per spec §5.7."""
    return {
        "schema_version": TRAP_DIFF_SCHEMA_VERSION,
        "engine": engine_name,
        "engine_fingerprint": result.engine_fingerprint,
        "policy_default_trap_width_pt": policy.default_trap_width_pt,
        "delta_e_tolerance": policy.delta_e_tolerance,
        "auto_detected_zone_count": auto_detected_zone_count,
        "operations": [_op_to_dict(op) for op in result.operations],
    }


def _op_to_dict(op: TrapApplication) -> dict[str, object]:
    return {
        "page_index": op.page_index,
        "rect_pt": list(op.rect_pt),
        "from_ink": op.from_ink,
        "to_ink": op.to_ink,
        "direction": op.direction,
        "width_pt": op.width_pt,
        "from_rgb": list(op.from_rgb),
        "to_rgb": list(op.to_rgb),
        "delta_e": op.delta_e,
        "trap_polygon_pt": [list(p) for p in op.trap_polygon_pt],
    }


__all__ = [
    "TRAP_DIFF_SCHEMA_VERSION",
    "TrapEngineError",
    "TrapResult",
    "apply_policy",
]
