"""Trap engine — end-to-end behavior across pure_python + selectors."""

from __future__ import annotations

import io

import pikepdf
import pytest
from pikepdf import Name

from compile_pdf_trap.engine import TrapEngineError, apply_policy
from compile_pdf_trap.policy_schema import InkPairRule, TrapPolicy, TrapZone


def _policy(**kwargs) -> TrapPolicy:
    return TrapPolicy(
        default_trap_width_pt=0.5,
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(100, 100, 300, 300), from_ink="Y", to_ink="K"),
        ],
        **kwargs,
    )


def test_apply_emits_one_overlay_per_zone(simple_pdf: bytes) -> None:
    result = apply_policy(simple_pdf, _policy())
    out = pikepdf.open(io.BytesIO(result.output_bytes))
    try:
        contents = out.pages[0].obj.get(Name.Contents)
        assert isinstance(contents, pikepdf.Array)
        # Original input has 1 stream; engine appends 1 per zone → 2 total.
        assert len(contents) == 2
    finally:
        out.close()


def test_engine_fingerprint_includes_codex_version(simple_pdf: bytes) -> None:
    result = apply_policy(simple_pdf, _policy())
    assert result.engine == "pure_python"
    assert result.engine_fingerprint.startswith("pure_python@1.0.0+codex_pdf@")


def test_apply_is_deterministic(simple_pdf: bytes) -> None:
    a = apply_policy(simple_pdf, _policy())
    b = apply_policy(simple_pdf, _policy())
    assert a.output_bytes == b.output_bytes


def test_zone_referencing_missing_page_rejected(simple_pdf: bytes) -> None:
    policy = TrapPolicy(
        trap_zones=[
            TrapZone(page_index=99, rect_pt=(0, 0, 10, 10), from_ink="Y", to_ink="K"),
        ]
    )
    with pytest.raises(TrapEngineError, match="page_index"):
        apply_policy(simple_pdf, policy)


def test_explicit_engine_choice_honored(simple_pdf: bytes) -> None:
    policy = _policy(engine="pure_python")
    result = apply_policy(simple_pdf, policy)
    assert result.engine == "pure_python"


def test_ghostscript_engine_raises_until_extra_installed(simple_pdf: bytes) -> None:
    policy = _policy(engine="ghostscript")
    with pytest.raises(TrapEngineError, match=r"trap-gs"):
        apply_policy(simple_pdf, policy)


def test_external_engine_raises_until_extra_installed(simple_pdf: bytes) -> None:
    policy = _policy(engine="external")
    with pytest.raises(TrapEngineError, match=r"trap-external"):
        apply_policy(simple_pdf, policy)


def test_env_var_picks_engine_when_policy_auto(simple_pdf: bytes, monkeypatch) -> None:
    monkeypatch.setenv("COMPILE_TRAP_ENGINE", "ghostscript")
    policy = _policy()  # engine='auto'
    with pytest.raises(TrapEngineError, match=r"trap-gs"):
        apply_policy(simple_pdf, policy)


def test_auto_direction_resolves_lighter_to_darker(simple_pdf: bytes) -> None:
    """Y is lighter than K → auto should resolve to 'spread'."""
    policy = TrapPolicy(
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(50, 50, 100, 100), from_ink="Y", to_ink="K"),
        ]
    )
    result = apply_policy(simple_pdf, policy)
    assert result.operations[0].direction == "spread"


def test_auto_direction_resolves_darker_to_lighter_as_choke(simple_pdf: bytes) -> None:
    """K → Y: from is darker, so it should choke."""
    policy = TrapPolicy(
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(50, 50, 100, 100), from_ink="K", to_ink="Y"),
        ]
    )
    result = apply_policy(simple_pdf, policy)
    assert result.operations[0].direction == "choke"


def test_explicit_rule_overrides_auto(simple_pdf: bytes) -> None:
    policy = TrapPolicy(
        ink_pair_rules=[
            InkPairRule(from_ink="Y", to_ink="K", width_pt=1.0, direction="choke"),
        ],
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(50, 50, 100, 100), from_ink="Y", to_ink="K"),
        ],
    )
    result = apply_policy(simple_pdf, policy)
    assert result.operations[0].direction == "choke"
    assert result.operations[0].width_pt == pytest.approx(1.0)


def test_trap_polygon_expands_for_spread(simple_pdf: bytes) -> None:
    """A spread polygon at width 0.5 over (50,50)-(100,100) should
    expand to (49.5,49.5)-(100.5,100.5)."""
    result = apply_policy(simple_pdf, _policy())
    op = result.operations[0]
    xs = [p[0] for p in op.trap_polygon_pt]
    ys = [p[1] for p in op.trap_polygon_pt]
    assert min(xs) < 100  # expanded outside the original 100..300 box
    assert max(xs) > 300
    assert min(ys) < 100
    assert max(ys) > 300


def test_unknown_engine_in_env_rejected(simple_pdf: bytes, monkeypatch) -> None:
    monkeypatch.setenv("COMPILE_TRAP_ENGINE", "wat")
    policy = _policy()
    with pytest.raises(TrapEngineError, match="unknown trap engine"):
        apply_policy(simple_pdf, policy)


def test_spread_choke_override_forces_direction(simple_pdf: bytes) -> None:
    """Policy-level spread_choke='choke' forces all auto-pairs to choke
    regardless of which ink is lighter."""
    policy = TrapPolicy(
        spread_choke="choke",
        trap_zones=[
            # Y is lighter than K — auto would spread, but override wins.
            TrapZone(page_index=0, rect_pt=(50, 50, 100, 100), from_ink="Y", to_ink="K"),
        ],
    )
    result = apply_policy(simple_pdf, policy)
    assert result.operations[0].direction == "choke"


def test_spread_choke_override_spread(simple_pdf: bytes) -> None:
    """Policy-level spread_choke='spread' forces spread even when from_ink is darker."""
    policy = TrapPolicy(
        spread_choke="spread",
        trap_zones=[
            # K is darker than Y — auto would choke, but override wins.
            TrapZone(page_index=0, rect_pt=(50, 50, 100, 100), from_ink="K", to_ink="Y"),
        ],
    )
    result = apply_policy(simple_pdf, policy)
    assert result.operations[0].direction == "spread"


def test_explicit_rule_overrides_spread_choke(simple_pdf: bytes) -> None:
    """Explicit per-pair rule direction takes precedence over spread_choke override."""
    policy = TrapPolicy(
        spread_choke="spread",
        ink_pair_rules=[
            InkPairRule(from_ink="Y", to_ink="K", width_pt=0.5, direction="choke"),
        ],
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(50, 50, 100, 100), from_ink="Y", to_ink="K"),
        ],
    )
    result = apply_policy(simple_pdf, policy)
    assert result.operations[0].direction == "choke"


def test_neutral_density_source_operator_uses_luminance(simple_pdf: bytes) -> None:
    """neutral_density_source='operator' falls back to RGB luminance; Y→K still spreads."""
    policy = TrapPolicy(
        neutral_density_source="operator",
        trap_zones=[
            TrapZone(page_index=0, rect_pt=(50, 50, 100, 100), from_ink="Y", to_ink="K"),
        ],
    )
    result = apply_policy(simple_pdf, policy)
    # Y has higher luminance than K under both RGB and Lab, so direction is unaffected.
    assert result.operations[0].direction == "spread"
