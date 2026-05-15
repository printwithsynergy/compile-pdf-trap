"""Trap policy schema — acceptance + JSON Schema export."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compile_pdf_trap.policy_schema import (
    InkPairRule,
    TrapPolicy,
    TrapZone,
    trap_policy_json_schema,
)


def test_minimum_policy_accepted() -> None:
    policy = TrapPolicy()
    assert policy.schema_version == "1.0.0"
    assert policy.default_trap_width_pt == pytest.approx(0.144)
    assert policy.ink_pair_rules == []
    assert policy.trap_zones == []
    assert policy.engine == "auto"


def test_full_policy_round_trips() -> None:
    policy = TrapPolicy(
        default_trap_width_pt=0.5,
        ink_pair_rules=[
            InkPairRule(from_ink="Y", to_ink="K", width_pt=0.5, direction="spread"),
            InkPairRule(from_ink="C", to_ink="M", width_pt=0.25, direction="auto"),
        ],
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(10, 10, 100, 100), from_ink="Y", to_ink="K"),
        ],
        delta_e_tolerance=10.0,
        engine="pure_python",
    )
    j = policy.model_dump_json()
    restored = TrapPolicy.model_validate_json(j)
    assert restored == policy


def test_unknown_direction_rejected() -> None:
    with pytest.raises(ValidationError):
        InkPairRule(from_ink="A", to_ink="B", width_pt=1.0, direction="sideways")  # type: ignore[arg-type]


def test_unknown_engine_rejected() -> None:
    with pytest.raises(ValidationError):
        TrapPolicy(engine="proprietary")  # type: ignore[arg-type]


def test_negative_width_rejected() -> None:
    with pytest.raises(ValidationError):
        InkPairRule(from_ink="A", to_ink="B", width_pt=-0.1)


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        TrapPolicy(bogus=True)  # type: ignore[call-arg]


def test_json_schema_exports() -> None:
    schema = trap_policy_json_schema()
    assert "$defs" in schema
    assert "ink_pair_rules" in schema["properties"]
    assert "trap_zones" in schema["properties"]
