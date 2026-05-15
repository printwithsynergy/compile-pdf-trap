"""Trap producer — ink-pair spread/choke trap with three engine slots.

Per spec §5.1–§5.7 + §1.11b trap exception:

- ``pure_python`` (default) — uses Codex ``polygon_offset`` (1.1.0)
- ``ghostscript`` — gated by the optional ``[trap-gs]`` extra
- ``external`` — gated by ``[trap-external]``; vendor licensing required

Engine selected via ``COMPILE_TRAP_ENGINE`` env var. Default
``pure_python`` once Codex 1.5 lands; ``ghostscript`` as documented
bootstrap fallback if the bump slips.

Codex surface consumed (color resolver + geometry — no Compile-side math):

- :class:`codex_pdf.color.CodexSpotIntent` — spot-ink declaration.
- :func:`codex_pdf.color.resolve_spot_swatch_color` — intent → device color.
- :func:`codex_pdf.color.delta_e_2000` — color-difference metric for
  trap quality verification.
- :func:`codex_pdf.geom.polygon_offset` — spread / choke offsets.
"""

from __future__ import annotations

from codex_pdf.color import CodexSpotIntent, delta_e_2000, resolve_spot_swatch_color
from codex_pdf.geom import polygon_offset

from compile_pdf_core.version import TRAP_SCHEMA_VERSION

__all__ = [
    "CodexSpotIntent",
    "TRAP_SCHEMA_VERSION",
    "delta_e_2000",
    "polygon_offset",
    "resolve_spot_swatch_color",
]
