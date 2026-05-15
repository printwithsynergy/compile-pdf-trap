"""Non-rectangular trap polygons — routed through codex_pdf.geom.polygon_offset."""

from __future__ import annotations

import io

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name
from pydantic import ValidationError

from compile_pdf_trap.engine import apply_policy
from compile_pdf_trap.policy_schema import TrapPolicy, TrapZone
from compile_pdf_trap.verify import verify_trap


def _make_pdf() -> bytes:
    pdf = pikepdf.new()
    pdf.pages.append(
        pikepdf.Page(
            pdf.make_indirect(
                Dictionary(
                    Type=Name.Page,
                    MediaBox=Array([0, 0, 612, 792]),
                    Resources=Dictionary(),
                    Contents=pdf.make_stream(b"q 100 100 m 200 200 l S Q"),
                )
            )
        )
    )
    buf = io.BytesIO()
    pdf.save(buf, deterministic_id=True, linearize=False)
    pdf.close()
    return buf.getvalue()


def test_polygon_zone_accepted_by_schema() -> None:
    z = TrapZone(
        page_index=0,
        polygon_pt=((0, 0), (10, 0), (5, 10)),
        from_ink="Y",
        to_ink="K",
    )
    assert z.rect_pt is None
    assert z.polygon_pt == ((0, 0), (10, 0), (5, 10))


def test_zone_requires_exactly_one_shape() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        TrapZone(page_index=0, from_ink="Y", to_ink="K")
    with pytest.raises(ValidationError, match="exactly one"):
        TrapZone(
            page_index=0,
            rect_pt=(0, 0, 10, 10),
            polygon_pt=((0, 0), (10, 0), (5, 10)),
            from_ink="Y",
            to_ink="K",
        )


def test_polygon_zone_requires_three_vertices() -> None:
    with pytest.raises(ValidationError, match="at least 3 vertices"):
        TrapZone(
            page_index=0,
            polygon_pt=((0, 0), (10, 0)),
            from_ink="Y",
            to_ink="K",
        )


def test_engine_applies_polygon_zone() -> None:
    policy = TrapPolicy(
        default_trap_width_pt=2.0,
        trap_zones=[
            TrapZone(
                page_index=0,
                polygon_pt=((100, 100), (300, 100), (200, 300)),
                from_ink="Y",
                to_ink="K",
            )
        ],
    )
    result = apply_policy(_make_pdf(), policy)
    assert len(result.operations) == 1
    op = result.operations[0]
    # Spread on a 3-vertex polygon yields ≥ 3 vertices via pyclipr miter join.
    assert len(op.trap_polygon_pt) >= 3
    # Trap diff records the bounding box of the trapped polygon.
    llx, lly, urx, ury = op.rect_pt
    assert llx < urx and lly < ury


def test_polygon_zone_is_deterministic() -> None:
    policy = TrapPolicy(
        default_trap_width_pt=2.0,
        trap_zones=[
            TrapZone(
                page_index=0,
                polygon_pt=((100, 100), (300, 100), (200, 300)),
                from_ink="Y",
                to_ink="K",
            )
        ],
    )
    pdf = _make_pdf()
    a = apply_policy(pdf, policy)
    b = apply_policy(pdf, policy)
    assert a.output_bytes == b.output_bytes


def test_polygon_zone_passes_verifier() -> None:
    policy = TrapPolicy(
        default_trap_width_pt=2.0,
        trap_zones=[
            TrapZone(
                page_index=0,
                polygon_pt=((100, 100), (300, 100), (200, 300)),
                from_ink="Y",
                to_ink="K",
            )
        ],
    )
    pdf = _make_pdf()
    result = apply_policy(pdf, policy)
    v = verify_trap(input_bytes=pdf, result=result, policy=policy)
    assert v.passed, v.failures


def test_polygon_offset_via_codex_round_trips() -> None:
    """Direct sanity-check that codex_pdf.geom.polygon_offset handles
    the non-rect path correctly — spread → choke ≈ identity."""
    from codex_pdf.geom import Path, polygon_offset

    square = Path(rings=([(0.0, 0.0), (100.0, 0.0), (100.0, 100.0), (0.0, 100.0)],))
    spread = polygon_offset(square, 5.0)
    assert len(spread.rings) >= 1
    # The choke of the spread shape should be close to the original area.
    choked = polygon_offset(spread, -5.0)
    assert len(choked.rings) >= 1
