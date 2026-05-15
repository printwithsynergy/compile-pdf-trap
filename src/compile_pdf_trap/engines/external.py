"""External-vendor trap engine — gated by the ``[trap-external]`` extra.

Stub for Esko / Heidelberg integrations. Requires vendor licensing and
a running daemon. v1 ships disabled.
"""

from __future__ import annotations

from compile_pdf_trap.engines.pure_python import TrapEngineError, TrapEngineResult
from compile_pdf_trap.policy_schema import TrapPolicy

ENGINE_NAME = "external"
ENGINE_VERSION = "1.0.0"


def engine_fingerprint() -> str:
    return f"{ENGINE_NAME}@{ENGINE_VERSION}"


def apply(input_bytes: bytes, policy: TrapPolicy) -> TrapEngineResult:
    """Always raises until the ``[trap-external]`` extra is wired."""
    _ = input_bytes, policy
    raise TrapEngineError(
        "external trap engine not installed; "
        "either pip install compile-pdf[trap-external] (vendor license required) "
        "or set COMPILE_TRAP_ENGINE=pure_python"
    )


__all__ = [
    "ENGINE_NAME",
    "ENGINE_VERSION",
    "apply",
    "engine_fingerprint",
]
