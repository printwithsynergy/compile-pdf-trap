"""``compile-pdf trap-diff`` accepts a lineage_id (in addition to a file path)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from compile_pdf.cli import cli
from compile_pdf_core.lineage.store import reset_default_store


@pytest.fixture(autouse=True)
def _clear_lineage_store():
    reset_default_store()
    yield
    reset_default_store()


def _run_cjd_with_trap(tmp_path: Path, simple_pdf: bytes) -> str:
    """Drive a CJD job that includes a trap step and return its lineage_id."""
    job_path = tmp_path / "job.json"
    out_path = tmp_path / "out.pdf"
    job_path.write_text(
        json.dumps(
            {
                "input_pdf_b64": base64.b64encode(simple_pdf).decode("ascii"),
                "steps": [
                    {
                        "type": "trap",
                        "policy": {
                            "trap_zones": [
                                {
                                    "page_index": 0,
                                    "rect_pt": [50, 50, 100, 100],
                                    "from_ink": "Y",
                                    "to_ink": "K",
                                }
                            ]
                        },
                    }
                ],
            }
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["cjd", "--job", str(job_path), str(out_path)])
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["lineage_id"]


def test_trap_diff_resolves_via_lineage_id(tmp_path: Path, simple_pdf: bytes) -> None:
    lineage_id = _run_cjd_with_trap(tmp_path, simple_pdf)
    runner = CliRunner()
    result = runner.invoke(cli, ["trap-diff", lineage_id])
    assert result.exit_code == 0, result.output
    diff = json.loads(result.output)
    assert diff["operations"][0]["from_ink"] == "Y"


def test_trap_diff_explicit_lineage_mode(tmp_path: Path, simple_pdf: bytes) -> None:
    lineage_id = _run_cjd_with_trap(tmp_path, simple_pdf)
    runner = CliRunner()
    result = runner.invoke(cli, ["trap-diff", "--from", "lineage", lineage_id])
    assert result.exit_code == 0


def test_trap_diff_file_mode_still_works(tmp_path: Path) -> None:
    diff_path = tmp_path / "diff.json"
    diff_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0.0",
                "engine": "pure_python",
                "engine_fingerprint": "pure_python@1.0.0+codex_pdf@1.7.0",
                "operations": [],
            }
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["trap-diff", str(diff_path)])
    assert result.exit_code == 0
    assert "pure_python@1.0.0" in result.output


def test_trap_diff_unknown_lineage_id_exits_5() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["trap-diff", "nope"])
    # 'auto' tries file first; missing path falls through to lineage lookup.
    assert result.exit_code == 5


def test_trap_diff_lineage_without_trap_step_exits_6(tmp_path: Path, printer_pdf: bytes) -> None:
    """A CJD that has no trap step shouldn't surface a trap-diff."""
    job_path = tmp_path / "job.json"
    out_path = tmp_path / "out.pdf"
    job_path.write_text(
        json.dumps(
            {
                "input_pdf_b64": base64.b64encode(printer_pdf).decode("ascii"),
                "steps": [{"type": "rewrite", "plan": {"ops": []}}],
            }
        )
    )
    runner = CliRunner()
    cjd = runner.invoke(cli, ["cjd", "--job", str(job_path), str(out_path)])
    assert cjd.exit_code == 0
    lineage_id = json.loads(cjd.output)["lineage_id"]

    result = runner.invoke(cli, ["trap-diff", lineage_id])
    assert result.exit_code == 6


def test_trap_diff_force_file_mode_with_missing_path_exits_2(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["trap-diff", "--from", "file", str(tmp_path / "missing.json")])
    assert result.exit_code == 2
