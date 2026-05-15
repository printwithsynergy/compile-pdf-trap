"""Click subcommand registration for ``compile-pdf trap`` + ``trap-diff``."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from compile_pdf_core.lineage.store import LineageNotFoundError, default_store
from compile_pdf_trap.engine import TrapEngineError, apply_policy
from compile_pdf_trap.policy_schema import TrapPolicy, trap_policy_json_schema
from compile_pdf_trap.verify import verify_trap


def register(group: click.Group) -> None:
    """Attach the ``trap``, ``trap-schema``, and ``trap-diff`` subcommands."""

    @group.command("trap", help="Apply a trap policy to a PDF.")
    @click.option(
        "--policy",
        "policy_path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
        required=True,
        help="JSON trap-policy document.",
    )
    @click.option(
        "--trap-diff",
        "diff_path",
        type=click.Path(dir_okay=False, path_type=Path),
        default=None,
        help="Write the trap-diff JSON artifact to this path.",
    )
    @click.option(
        "--verify/--no-verify",
        default=True,
        help="Run four-layer post-condition checks before writing output.",
    )
    @click.argument(
        "input_path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
    )
    @click.argument(
        "output_path",
        type=click.Path(dir_okay=False, path_type=Path),
    )
    def trap_cmd(
        policy_path: Path,
        input_path: Path,
        output_path: Path,
        diff_path: Path | None,
        verify: bool,
    ) -> None:
        policy_dict = json.loads(policy_path.read_text(encoding="utf-8"))
        try:
            policy = TrapPolicy.model_validate(policy_dict)
        except Exception as exc:
            click.echo(f"policy validation failed: {exc}", err=True)
            sys.exit(3)

        input_bytes = input_path.read_bytes()
        try:
            result = apply_policy(input_bytes, policy)
        except TrapEngineError as exc:
            click.echo(f"policy rejected: {exc}", err=True)
            sys.exit(4)

        if verify:
            check = verify_trap(input_bytes=input_bytes, result=result, policy=policy)
            if not check.passed:
                click.echo("verify failed:", err=True)
                for failure in check.failures:
                    click.echo(f"  - {failure}", err=True)
                sys.exit(4)

        output_path.write_bytes(result.output_bytes)
        if diff_path is not None:
            diff_path.write_text(json.dumps(result.trap_diff, indent=2), encoding="utf-8")

        click.echo(
            json.dumps(
                {
                    "engine": result.engine,
                    "engine_fingerprint": result.engine_fingerprint,
                    "operations_count": len(result.operations),
                    "pdf_sha256": result.pdf_sha256,
                    "output": str(output_path),
                    "trap_diff": str(diff_path) if diff_path else None,
                },
                indent=2,
            )
        )

    @group.command("trap-schema", hidden=True, help="Dump the trap-policy JSON Schema.")
    def trap_schema_cmd() -> None:
        click.echo(json.dumps(trap_policy_json_schema(), indent=2))

    @group.command(
        "trap-extract",
        help=(
            "Walk a PDF's content streams and emit suggested trap_zones for "
            "every spot-ink adjacency. Output is JSON ready to paste into a "
            "trap-policy document."
        ),
    )
    @click.argument(
        "input_path",
        type=click.Path(exists=True, dir_okay=False, path_type=Path),
    )
    @click.option(
        "--seam-width-pt",
        type=float,
        default=6.0,
        help="Width of the suggested trap seam, in PDF points.",
    )
    @click.option(
        "--edge-tolerance-pt",
        type=float,
        default=0.5,
        help="Maximum gap between two edges that still counts as adjacent.",
    )
    def trap_extract_cmd(
        input_path: Path,
        seam_width_pt: float,
        edge_tolerance_pt: float,
    ) -> None:
        from compile_pdf_trap.extract import auto_trap_zones

        zones = auto_trap_zones(
            input_path.read_bytes(),
            edge_tolerance_pt=edge_tolerance_pt,
            seam_width_pt=seam_width_pt,
        )
        click.echo(
            json.dumps(
                [z.model_dump(mode="json", exclude_none=True) for z in zones],
                indent=2,
            )
        )

    @group.command(
        "trap-diff",
        help=(
            "Print a trap-diff artifact. Argument is either a path to a "
            "JSON file or a lineage_id from a previously-run CJD job."
        ),
    )
    @click.argument("source", type=str)
    @click.option(
        "--from",
        "source_kind",
        type=click.Choice(["auto", "file", "lineage"]),
        default="auto",
        help=(
            "Where to read the diff from. 'auto' (default) tries the path "
            "first then falls back to lineage; 'file'/'lineage' force one mode."
        ),
    )
    def trap_diff_cmd(source: str, source_kind: str) -> None:
        """Resolve trap-diff via file or lineage lookup."""
        as_path = Path(source)
        if source_kind in ("file", "auto") and as_path.exists():
            click.echo(as_path.read_text(encoding="utf-8"))
            return
        if source_kind == "file":
            click.echo(f"file not found: {source}", err=True)
            sys.exit(2)

        try:
            chain = default_store().get(source)
        except LineageNotFoundError:
            click.echo(f"lineage_id not found: {source}", err=True)
            sys.exit(5)
        for step in reversed(chain.steps):
            if step.trap_diff is not None:
                click.echo(json.dumps(step.trap_diff, indent=2))
                return
        click.echo(f"lineage {source} has no trap step", err=True)
        sys.exit(6)
