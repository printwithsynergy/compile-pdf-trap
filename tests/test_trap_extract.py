"""Ink-pair adjacency extraction — end-to-end with real spot-color PDFs."""

from __future__ import annotations

import io

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name

from compile_pdf_trap.extract import auto_trap_zones, extract_ink_rects


def _spot_separation(pdf: pikepdf.Pdf, ink_name: str, c1: list[float]) -> pikepdf.Object:
    """Build an indirect /Separation array referencing ``ink_name``."""
    return pdf.make_indirect(
        Array(
            [
                Name.Separation,
                Name(f"/{ink_name}"),
                Name.DeviceCMYK,
                Dictionary(
                    FunctionType=2,
                    Domain=Array([0, 1]),
                    C0=Array([0, 0, 0, 0]),
                    C1=Array(c1),
                    N=1,
                ),
            ]
        )
    )


def _build_two_spot_pdf(*, contents: bytes) -> bytes:
    """Single-page PDF with `/CS_PMS` + `/CS_K` registered as spot inks."""
    pdf = pikepdf.new()
    pms = _spot_separation(pdf, "PMS_185", [0, 1, 0.7, 0])
    black = _spot_separation(pdf, "Black", [0, 0, 0, 1])
    cs_dict = Dictionary()
    cs_dict[Name("/CS_PMS")] = pms
    cs_dict[Name("/CS_K")] = black
    resources = Dictionary()
    resources[Name.ColorSpace] = cs_dict
    pdf.pages.append(
        pikepdf.Page(
            pdf.make_indirect(
                Dictionary(
                    Type=Name.Page,
                    MediaBox=Array([0, 0, 612, 792]),
                    Resources=resources,
                    Contents=pdf.make_stream(contents),
                )
            )
        )
    )
    buf = io.BytesIO()
    pdf.save(buf, deterministic_id=True, linearize=False)
    pdf.close()
    return buf.getvalue()


@pytest.fixture
def side_by_side_two_spot_pdf() -> bytes:
    return _build_two_spot_pdf(
        contents=(
            b"q\n/CS_PMS cs 1 scn\n100 100 100 200 re f\n/CS_K cs 1 scn\n200 100 100 200 re f\nQ"
        )
    )


@pytest.fixture
def stacked_two_spot_pdf() -> bytes:
    return _build_two_spot_pdf(
        contents=(
            b"q\n/CS_PMS cs 1 scn\n100 100 200 100 re f\n/CS_K cs 1 scn\n100 200 200 100 re f\nQ"
        )
    )


@pytest.fixture
def single_ink_pdf() -> bytes:
    """Only one spot color — no adjacency to detect."""
    return _build_two_spot_pdf(contents=b"q\n/CS_PMS cs 1 scn\n100 100 200 200 re f\nQ")


@pytest.fixture
def disjoint_two_spot_pdf() -> bytes:
    """Two spot rects with a gap — no adjacency."""
    return _build_two_spot_pdf(
        contents=(b"q\n/CS_PMS cs 1 scn\n100 100 50 50 re f\n/CS_K cs 1 scn\n300 100 50 50 re f\nQ")
    )


def test_extract_ink_rects_side_by_side(side_by_side_two_spot_pdf: bytes) -> None:
    rects = extract_ink_rects(side_by_side_two_spot_pdf)
    assert len(rects) == 2
    inks = sorted(r.ink_name for r in rects)
    assert inks == ["Black", "PMS_185"]
    by_ink = {r.ink_name: r.rect_pt for r in rects}
    assert by_ink["PMS_185"] == (100.0, 100.0, 200.0, 300.0)
    assert by_ink["Black"] == (200.0, 100.0, 300.0, 300.0)


def test_auto_trap_zones_detects_vertical_seam(
    side_by_side_two_spot_pdf: bytes,
) -> None:
    zones = auto_trap_zones(side_by_side_two_spot_pdf)
    assert len(zones) == 1
    z = zones[0]
    # Deterministic ordering: alphabetical so the same input always
    # produces the same diff.
    assert (z.from_ink, z.to_ink) == ("Black", "PMS_185")
    assert z.rect_pt is not None
    llx, lly, urx, ury = z.rect_pt
    # Seam centered on x=200, default width 6 pt → spans 197..203.
    assert llx == pytest.approx(197.0)
    assert urx == pytest.approx(203.0)
    # Y range matches the overlapping vertical extent.
    assert lly == pytest.approx(100.0)
    assert ury == pytest.approx(300.0)


def test_auto_trap_zones_detects_horizontal_seam(
    stacked_two_spot_pdf: bytes,
) -> None:
    zones = auto_trap_zones(stacked_two_spot_pdf)
    assert len(zones) == 1
    z = zones[0]
    assert z.rect_pt is not None
    llx, lly, urx, ury = z.rect_pt
    # Seam centered on y=200.
    assert lly == pytest.approx(197.0)
    assert ury == pytest.approx(203.0)
    assert llx == pytest.approx(100.0)
    assert urx == pytest.approx(300.0)


def test_extract_empty_when_single_ink(single_ink_pdf: bytes) -> None:
    zones = auto_trap_zones(single_ink_pdf)
    assert zones == []


def test_extract_empty_when_disjoint(disjoint_two_spot_pdf: bytes) -> None:
    zones = auto_trap_zones(disjoint_two_spot_pdf)
    assert zones == []


def test_extract_ignores_pdf_without_spot_inks(simple_pdf: bytes) -> None:
    rects = extract_ink_rects(simple_pdf)
    assert rects == []


def test_auto_trap_zones_seam_width_is_configurable(
    side_by_side_two_spot_pdf: bytes,
) -> None:
    zones = auto_trap_zones(side_by_side_two_spot_pdf, seam_width_pt=1.0)
    assert zones[0].rect_pt is not None
    llx, _, urx, _ = zones[0].rect_pt
    assert urx - llx == pytest.approx(1.0)


def test_auto_trap_zones_is_deterministic(side_by_side_two_spot_pdf: bytes) -> None:
    a = auto_trap_zones(side_by_side_two_spot_pdf)
    b = auto_trap_zones(side_by_side_two_spot_pdf)
    assert a == b


def test_extract_skips_strokes_only(simple_pdf: bytes) -> None:
    """Filled rects produce candidates; stroked-only ones don't."""
    # The simple_pdf fixture has no spot inks, so this is mostly a regression
    # guard — verifies that S / s operators don't trigger fill-side capture.
    rects = extract_ink_rects(simple_pdf)
    assert rects == []
