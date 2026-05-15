"""``compile-pdf trap-extract`` CLI coverage."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pikepdf
from click.testing import CliRunner
from pikepdf import Array, Dictionary, Name

from compile_pdf.cli import cli


def _two_spot_pdf() -> bytes:
    pdf = pikepdf.new()

    def _sep(ink: str, c1: list[float]) -> pikepdf.Object:
        return pdf.make_indirect(
            Array(
                [
                    Name.Separation,
                    Name(f"/{ink}"),
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

    cs_dict = Dictionary()
    cs_dict[Name("/CS_PMS")] = _sep("PMS_185", [0, 1, 0.7, 0])
    cs_dict[Name("/CS_K")] = _sep("Black", [0, 0, 0, 1])
    resources = Dictionary()
    resources[Name.ColorSpace] = cs_dict
    pdf.pages.append(
        pikepdf.Page(
            pdf.make_indirect(
                Dictionary(
                    Type=Name.Page,
                    MediaBox=Array([0, 0, 612, 792]),
                    Resources=resources,
                    Contents=pdf.make_stream(
                        b"q /CS_PMS cs 1 scn 100 100 100 200 re f "
                        b"/CS_K cs 1 scn 200 100 100 200 re f Q"
                    ),
                )
            )
        )
    )
    buf = io.BytesIO()
    pdf.save(buf, deterministic_id=True, linearize=False)
    pdf.close()
    return buf.getvalue()


def test_trap_extract_emits_pasteable_zones(tmp_path: Path) -> None:
    in_path = tmp_path / "in.pdf"
    in_path.write_bytes(_two_spot_pdf())

    runner = CliRunner()
    result = runner.invoke(cli, ["trap-extract", str(in_path)])
    assert result.exit_code == 0, result.output
    zones = json.loads(result.output)
    assert len(zones) == 1
    assert zones[0]["from_ink"] == "Black"
    assert zones[0]["to_ink"] == "PMS_185"
    assert "rect_pt" in zones[0]


def test_trap_extract_seam_width_flag(tmp_path: Path) -> None:
    in_path = tmp_path / "in.pdf"
    in_path.write_bytes(_two_spot_pdf())

    runner = CliRunner()
    result = runner.invoke(cli, ["trap-extract", "--seam-width-pt", "2.0", str(in_path)])
    assert result.exit_code == 0
    zones = json.loads(result.output)
    llx, _, urx, _ = zones[0]["rect_pt"]
    assert urx - llx == 2.0


def test_top_level_help_lists_trap_extract() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert "trap-extract" in result.output
