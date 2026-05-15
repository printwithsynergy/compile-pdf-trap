"""Trap determinism — pure_python is bit-deterministic across re-runs."""

from __future__ import annotations

from compile_pdf_trap.engine import apply_policy
from compile_pdf_trap.policy_schema import InkPairRule, TrapPolicy, TrapZone


def test_pure_python_is_deterministic(simple_pdf: bytes) -> None:
    policy = TrapPolicy(
        default_trap_width_pt=0.5,
        ink_pair_rules=[
            InkPairRule(from_ink="Y", to_ink="K", width_pt=0.5, direction="spread"),
        ],
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(100, 100, 300, 300), from_ink="Y", to_ink="K"),
        ],
    )
    runs = [apply_policy(simple_pdf, policy).pdf_sha256 for _ in range(3)]
    assert len(set(runs)) == 1


def test_engine_fingerprint_is_stable(simple_pdf: bytes) -> None:
    policy = TrapPolicy(
        default_trap_width_pt=0.5,
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(50, 50, 100, 100), from_ink="C", to_ink="M"),
        ],
    )
    fps = [apply_policy(simple_pdf, policy).engine_fingerprint for _ in range(3)]
    assert len(set(fps)) == 1
