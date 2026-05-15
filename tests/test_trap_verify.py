"""Trap verifier — four-layer post-condition checks."""

from __future__ import annotations

import io

import pikepdf
from pikepdf import String

from compile_pdf_trap.engine import apply_policy
from compile_pdf_trap.policy_schema import TrapPolicy, TrapZone
from compile_pdf_trap.verify import verify_trap


def _policy() -> TrapPolicy:
    return TrapPolicy(
        default_trap_width_pt=0.5,
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(100, 100, 300, 300), from_ink="Y", to_ink="K"),
        ],
    )


def test_verify_passes_on_clean_apply(simple_pdf: bytes) -> None:
    policy = _policy()
    result = apply_policy(simple_pdf, policy)
    v = verify_trap(input_bytes=simple_pdf, result=result, policy=policy)
    assert v.layer1_schema and v.layer2_determinism and v.layer3_unchanged and v.layer6_delta_e
    assert v.passed, v.failures


def test_verify_strict_delta_e_flags_high_contrast(simple_pdf: bytes) -> None:
    policy = TrapPolicy(
        default_trap_width_pt=0.5,
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(100, 100, 300, 300), from_ink="Y", to_ink="K"),
        ],
        delta_e_tolerance=5.0,  # Y/K is ~88; this should fail
    )
    result = apply_policy(simple_pdf, policy)
    v = verify_trap(input_bytes=simple_pdf, result=result, policy=policy, strict_delta_e=True)
    assert not v.layer6_delta_e
    assert any("L6" in f for f in v.failures)


def test_verify_layer2_skipped_for_non_pure_python(simple_pdf: bytes) -> None:
    """Verify accepts ghostscript / external engines on the fingerprint
    contract alone — replay byte-identity is not required cross-engine."""
    # We can't actually run those engines (they raise), but we can craft
    # a TrapResult by hand and confirm L2 honors the carve-out.
    from compile_pdf_trap.engine import TrapResult

    result = apply_policy(simple_pdf, _policy())
    forged = TrapResult(
        output_bytes=result.output_bytes,
        pdf_sha256=result.pdf_sha256,
        operations=result.operations,
        engine="ghostscript",
        engine_fingerprint="ghostscript@1.0.0",
        trap_diff=result.trap_diff,
    )
    v = verify_trap(input_bytes=simple_pdf, result=forged, policy=_policy())
    assert v.layer2_determinism


def test_verify_layer3_detects_metadata_change(simple_pdf: bytes) -> None:
    policy = _policy()
    result = apply_policy(simple_pdf, policy)
    pdf = pikepdf.open(io.BytesIO(result.output_bytes))
    pdf.docinfo[pikepdf.Name.Title] = String("Tampered title")
    out = io.BytesIO()
    pdf.save(out, deterministic_id=True, linearize=False)
    pdf.close()
    tampered = out.getvalue()

    from compile_pdf_trap.engine import TrapResult

    bogus = TrapResult(
        output_bytes=tampered,
        pdf_sha256=result.pdf_sha256,
        operations=result.operations,
        engine=result.engine,
        engine_fingerprint=result.engine_fingerprint,
        trap_diff=result.trap_diff,
    )
    v = verify_trap(input_bytes=simple_pdf, result=bogus, policy=policy)
    assert not v.layer3_unchanged
    assert any("L3" in f for f in v.failures)


def test_verify_layer2_skip_when_replay_disabled(simple_pdf: bytes) -> None:
    policy = _policy()
    result = apply_policy(simple_pdf, policy)
    v = verify_trap(
        input_bytes=simple_pdf,
        result=result,
        policy=policy,
        determinism_replay=False,
    )
    assert v.layer2_determinism


def test_output_contains_traps_ocg(simple_pdf: bytes) -> None:
    """output_trap_layer=True (the default) must produce an OCG named 'Traps'."""
    policy = TrapPolicy(
        default_trap_width_pt=0.5,
        output_trap_layer=True,
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(100, 100, 300, 300), from_ink="C", to_ink="M"),
        ],
    )
    result = apply_policy(simple_pdf, policy)
    pdf = pikepdf.open(io.BytesIO(result.output_bytes))
    try:
        assert pikepdf.Name.OCProperties in pdf.Root, "No OCProperties in Root"
        ocgs = list(pdf.Root.OCProperties.OCGs)
        assert len(ocgs) == 1, f"Expected 1 OCG, got {len(ocgs)}"
        assert str(ocgs[0].Name) == "Traps", f"OCG name was {ocgs[0].Name!r}"
    finally:
        pdf.close()


def test_output_no_ocg_when_layer_disabled(simple_pdf: bytes) -> None:
    """output_trap_layer=False must not write OCProperties."""
    policy = TrapPolicy(
        default_trap_width_pt=0.5,
        output_trap_layer=False,
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(100, 100, 300, 300), from_ink="C", to_ink="M"),
        ],
    )
    result = apply_policy(simple_pdf, policy)
    pdf = pikepdf.open(io.BytesIO(result.output_bytes))
    try:
        assert pikepdf.Name.OCProperties not in pdf.Root, "OCProperties present but flag was False"
    finally:
        pdf.close()
