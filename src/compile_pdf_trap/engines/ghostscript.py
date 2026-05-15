"""Ghostscript trap engine — gated by the ``[trap-gs]`` extra.

Bootstrap fallback if the pure-python engine has gaps. v1 ships
disabled; calling :func:`apply` raises :class:`TrapEngineError` with a
clear pointer at the install path.
"""

from __future__ import annotations

from compile_pdf_trap.engines.pure_python import TrapEngineError, TrapEngineResult
from compile_pdf_trap.policy_schema import TrapPolicy

ENGINE_NAME = "ghostscript"
ENGINE_VERSION = "1.0.0"


def engine_fingerprint() -> str:
    return f"{ENGINE_NAME}@{ENGINE_VERSION}"


def apply(input_bytes: bytes, policy: TrapPolicy) -> TrapEngineResult:
    """Always raises until the ``[trap-gs]`` extra wires Ghostscript.

    The error mentions both the install path and the ``COMPILE_TRAP_ENGINE``
    fallback so operators have a single-line recovery story.
    """
    _ = input_bytes, policy  # surfaced for future signature stability
    raise TrapEngineError(
        "ghostscript trap engine not installed; "
        "either pip install compile-pdf[trap-gs] or set COMPILE_TRAP_ENGINE=pure_python"
    )


__all__ = [
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "apply",
    "engine_fingerprint",
]
