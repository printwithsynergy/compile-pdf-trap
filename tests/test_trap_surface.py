"""Surface test: trap producer must re-export codex_pdf.color resolver +
codex_pdf.geom.polygon_offset."""

from __future__ import annotations


def test_trap_module_reexports_color_and_geom() -> None:
    from compile_pdf import trap

    for symbol in (
        "CodexSpotIntent",
        "delta_e_2000",
        "polygon_offset",
        "resolve_spot_swatch_color",
    ):
        assert hasattr(trap, symbol), f"trap must re-export {symbol}"
        assert symbol in trap.__all__


def test_trap_symbols_match_canonical_imports() -> None:
    from codex_pdf import color as codex_color
    from codex_pdf import geom as codex_geom

    from compile_pdf import trap

    assert trap.CodexSpotIntent is codex_color.CodexSpotIntent
    assert trap.resolve_spot_swatch_color is codex_color.resolve_spot_swatch_color
    assert trap.delta_e_2000 is codex_color.delta_e_2000
    assert trap.polygon_offset is codex_geom.polygon_offset
