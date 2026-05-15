"""FastAPI router for the trap producer.

Mounts under ``/v1/trap`` from :mod:`compile_pdf.api.main`. Single
endpoint today: ``POST /v1/trap/apply``.
"""

from __future__ import annotations

import base64
import hashlib

import structlog
from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from compile_pdf_core.cache import compute_cache_key, hash_canonical_plan
from compile_pdf_core.retention import (
    parse_consent,
    persist_if_opted_in,
    resolve_tenant,
)
from compile_pdf_trap.engine import TrapEngineError, apply_policy
from compile_pdf_trap.policy_schema import TrapPolicy
from compile_pdf_trap.verify import verify_trap
from compile_pdf_core.version import (
    CODEX_DOCUMENT_SCHEMA_VERSION_PIN,
    TRAP_SCHEMA_VERSION,
    VERSION,
)

logger = structlog.get_logger(__name__)

router = APIRouter()


class TrapApplyRequest(BaseModel):
    """Request envelope: an inline base64-encoded PDF + a trap policy."""

    model_config = {"extra": "forbid"}

    input_pdf_b64: str = Field(min_length=1)
    policy: TrapPolicy


class TrapApplyResponse(BaseModel):
    model_config = {"extra": "forbid"}

    output_pdf_b64: str
    pdf_sha256: str
    input_sha256: str
    policy_sha256: str
    cache_key: str
    cache_hit: bool = False
    engine: str
    engine_fingerprint: str
    operations_count: int
    trap_diff: dict[str, object]
    schema_version: str = TRAP_SCHEMA_VERSION
    compile_version: str = VERSION


@router.post("/apply", response_model=TrapApplyResponse, status_code=status.HTTP_200_OK)
async def trap_apply(payload: TrapApplyRequest, request: Request) -> TrapApplyResponse:
    """Apply a trap policy to an inline base64-encoded PDF."""
    try:
        input_bytes = base64.b64decode(payload.input_pdf_b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"input_pdf_b64 is not valid base64: {exc}",
        ) from exc

    if not input_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="input is empty")

    input_sha256 = hashlib.sha256(input_bytes).hexdigest()
    policy_sha256 = hash_canonical_plan(payload.policy.model_dump(mode="json"))

    try:
        from codex_pdf.color import COLOR_SCHEMA_VERSION
        from codex_pdf.geom import GEOM_SCHEMA_VERSION
    except ImportError as exc:  # pragma: no cover — codex-pdf is a hard dep
        raise HTTPException(
            status_code=500, detail=f"codex-pdf surface unavailable: {exc}"
        ) from exc

    cache_key = compute_cache_key(
        producer="trap",
        input_sha256=input_sha256,
        canonical_plan_sha256=policy_sha256,
        codex_pdf_package_version=_resolve_codex_pdf_version(),
        color_schema_version=COLOR_SCHEMA_VERSION,
        geom_schema_version=GEOM_SCHEMA_VERSION,
        codex_document_schema_version=CODEX_DOCUMENT_SCHEMA_VERSION_PIN,
    )

    logger.info(
        "trap.apply.start",
        zones=len(payload.policy.trap_zones),
        engine=payload.policy.engine,
        input_sha256=input_sha256[:16],
        policy_sha256=policy_sha256[:16],
        cache_key=cache_key[:16],
    )

    try:
        result = apply_policy(input_bytes, payload.policy)
    except TrapEngineError as exc:
        raise HTTPException(status_code=422, detail=f"policy rejected: {exc}") from exc

    verify = verify_trap(
        input_bytes=input_bytes,
        result=result,
        policy=payload.policy,
        determinism_replay=False,
    )
    if not (verify.layer1_schema and verify.layer3_unchanged):
        logger.error("trap.apply.verify_failed", failures=verify.failures)
        raise HTTPException(
            status_code=500,
            detail={"error": "verify failed", "failures": verify.failures},
        )

    consent = parse_consent(request)
    response = TrapApplyResponse(
        output_pdf_b64=base64.b64encode(result.output_bytes).decode("ascii"),
        pdf_sha256=result.pdf_sha256,
        input_sha256=input_sha256,
        policy_sha256=policy_sha256,
        cache_key=cache_key,
        cache_hit=False,
        engine=result.engine,
        engine_fingerprint=result.engine_fingerprint,
        operations_count=len(result.operations),
        trap_diff=result.trap_diff,
    )
    retained = persist_if_opted_in(
        consent=consent,
        producer="trap",
        tenant=resolve_tenant(request),
        input_bytes=input_bytes,
        output_bytes=result.output_bytes,
        result=response.model_dump(mode="json"),
        input_sha256=input_sha256,
    )
    logger.info(
        "trap.apply.ok",
        engine=result.engine,
        output_sha256=result.pdf_sha256[:16],
        operations=len(result.operations),
        consent=consent,
        retained=retained,
    )
    return response


def _resolve_codex_pdf_version() -> str:
    """Read codex_pdf wheel version Compile was deployed against."""
    try:
        from codex_pdf import __version__ as codex_version
    except ImportError:
        return "unknown"
    return str(codex_version)
