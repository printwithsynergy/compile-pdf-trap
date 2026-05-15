"""CLI tests for ``compile-pdf trap`` + ``trap-diff``."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from compile_pdf.cli import cli


def test_trap_cli_round_trips(tmp_path: Path, simple_pdf: bytes) -> None:
    in_path = tmp_path / "in.pdf"
    out_path = tmp_path / "out.pdf"
    diff_path = tmp_path / "trap-diff.json"
    policy_path = tmp_path / "policy.json"
    in_path.write_bytes(simple_pdf)
    policy_path.write_text(
        json.dumps(
            {
                "default_trap_width_pt": 0.5,
                "trap_zones": [
                    {
                        "page_index": 0,
                        "rect_pt": [100, 100, 300, 300],
                        "from_ink": "Y",
                        "to_ink": "K",
                    }
                ],
            }
        )
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "trap",
            "--policy",
            str(policy_path),
            "--trap-diff",
            str(diff_path),
            str(in_path),
            str(out_path),
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["engine"] == "pure_python"
    assert payload["operations_count"] == 1
    assert diff_path.exists()
    diff = json.loads(diff_path.read_text())
    assert diff["operations"][0]["from_ink"] == "Y"


def test_trap_cli_rejects_invalid_policy(tmp_path: Path, simple_pdf: bytes) -> None:
    in_path = tmp_path / "in.pdf"
    out_path = tmp_path / "out.pdf"
    policy_path = tmp_path / "policy.json"
    in_path.write_bytes(simple_pdf)
    policy_path.write_text(json.dumps({"engine": "wat"}))

    runner = CliRunner()
    result = runner.invoke(cli, ["trap", "--policy", str(policy_path), str(in_path), str(out_path)])
    assert result.exit_code == 3


def test_trap_cli_rejects_unapplicable_policy(tmp_path: Path, simple_pdf: bytes) -> None:
    in_path = tmp_path / "in.pdf"
    out_path = tmp_path / "out.pdf"
    policy_path = tmp_path / "policy.json"
    in_path.write_bytes(simple_pdf)
    policy_path.write_text(
        json.dumps(
            {
                "trap_zones": [
                    {
                        "page_index": 99,
                        "rect_pt": [0, 0, 10, 10],
                        "from_ink": "Y",
                        "to_ink": "K",
                    }
                ]
            }
        )
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["trap", "--policy", str(policy_path), str(in_path), str(out_path)])
    assert result.exit_code == 4


def test_trap_schema_dumps_json_schema() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["trap-schema"])
    assert result.exit_code == 0
    schema = json.loads(result.output)
    assert "properties" in schema


def test_trap_diff_subcommand_prints_artifact(tmp_path: Path) -> None:
    diff_path = tmp_path / "diff.json"
    payload = {
        "schema_version": "1.0.0",
        "engine": "pure_python",
        "engine_fingerprint": "pure_python@1.0.0+codex_pdf@1.7.0",
        "operations": [],
    }
    diff_path.write_text(json.dumps(payload))
    runner = CliRunner()
    result = runner.invoke(cli, ["trap-diff", str(diff_path)])
    assert result.exit_code == 0
    assert "pure_python@1.0.0+codex_pdf@1.7.0" in result.output


def test_top_level_help_lists_trap() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "trap" in result.output
    assert "trap-diff" in result.output
