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

import os
from dataclasses import dataclass
from typing import Protocol

from compile_pdf_trap.engines import external as external_engine
from compile_pdf_trap.engines import ghostscript as gs_engine
from compile_pdf_trap.engines import pure_python as pp_engine
from compile_pdf_trap.engines.pure_python import (
    TrapApplication,
    TrapEngineError,
    TrapEngineResult,
)
from compile_pdf_trap.policy_schema import EngineSelector, TrapPolicy


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
    plus a populated trap-diff artifact."""
    engine_name = _resolve_engine(policy.engine)
    engine = _ENGINES.get(engine_name)
    if engine is None:
        raise TrapEngineError(
            f"unknown trap engine {engine_name!r}; "
            "expected one of pure_python | ghostscript | external"
        )
    result = engine.apply(input_bytes, policy)
    diff = _build_trap_diff(policy, engine_name, result)
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


# --- trap-diff artifact -------------------------------------------------


TRAP_DIFF_SCHEMA_VERSION = "1.0.0"


def _build_trap_diff(
    policy: TrapPolicy,
    engine_name: str,
    result: TrapEngineResult,
) -> dict[str, object]:
    """Produce a JSON-serializable trap-diff record per spec §5.7."""
    return {
        "schema_version": TRAP_DIFF_SCHEMA_VERSION,
        "engine": engine_name,
        "engine_fingerprint": result.engine_fingerprint,
        "policy_default_trap_width_pt": policy.default_trap_width_pt,
        "delta_e_tolerance": policy.delta_e_tolerance,
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
