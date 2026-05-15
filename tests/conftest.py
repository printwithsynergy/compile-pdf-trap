"""Shared test fixtures for the compile-pdf test suite.

Builds tiny PDFs in-memory via pikepdf — no fixture files committed,
no flaky binary assets to keep in sync with codex.
"""

from __future__ import annotations

import io

import pikepdf
import pytest


def _empty_page(pdf: pikepdf.Pdf, width: float = 612.0, height: float = 792.0) -> pikepdf.Page:
    return pikepdf.Page(
        pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Page,
                MediaBox=pikepdf.Array([0, 0, width, height]),
                Resources=pikepdf.Dictionary(),
                Contents=pdf.make_stream(b""),
            )
        )
    )


def _printer_page(pdf: pikepdf.Pdf) -> pikepdf.Page:
    """A page with declared TrimBox + BleedBox + slug margin — required
    by the marks engine for slug anchors and bleed-corner anchors."""
    return pikepdf.Page(
        pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Page,
                MediaBox=pikepdf.Array([0, 0, 612, 792]),
                TrimBox=pikepdf.Array([36, 36, 576, 756]),
                BleedBox=pikepdf.Array([18, 18, 594, 774]),
                Resources=pikepdf.Dictionary(),
                Contents=pdf.make_stream(b""),
            )
        )
    )


def build_pdf(
    *,
    pages: int = 1,
    title: str | None = None,
    author: str | None = None,
    add_ocg: str | None = None,
    add_javascript: bool = False,
    add_embedded_file: bool = False,
    printer_pages: bool = False,
) -> bytes:
    """Build a tiny PDF with the requested attributes baked in."""
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.pages.append(_printer_page(pdf) if printer_pages else _empty_page(pdf))
    if title is not None:
        pdf.docinfo[pikepdf.Name.Title] = pikepdf.String(title)
    if author is not None:
        pdf.docinfo[pikepdf.Name.Author] = pikepdf.String(author)
    if add_ocg is not None:
        ocg = pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.OCG,
                Name=pikepdf.String(add_ocg),
            )
        )
        pdf.Root[pikepdf.Name.OCProperties] = pikepdf.Dictionary(
            OCGs=pikepdf.Array([ocg]),
            D=pikepdf.Dictionary(
                ON=pikepdf.Array([ocg]),
                OFF=pikepdf.Array([]),
                BaseState=pikepdf.Name.ON,
            ),
        )
    if add_javascript:
        pdf.Root[pikepdf.Name.Names] = pikepdf.Dictionary(
            JavaScript=pikepdf.Dictionary(
                Names=pikepdf.Array(
                    [
                        pikepdf.String("greeting"),
                        pikepdf.Dictionary(
                            S=pikepdf.Name.JavaScript,
                            JS=pikepdf.String("app.alert('hi');"),
                        ),
                    ]
                ),
            ),
        )
    if add_embedded_file:
        existing_names = pdf.Root.get(pikepdf.Name.Names)
        names = (
            existing_names
            if isinstance(existing_names, pikepdf.Dictionary)
            else pikepdf.Dictionary()
        )
        names[pikepdf.Name.EmbeddedFiles] = pikepdf.Dictionary(
            Names=pikepdf.Array(
                [
                    pikepdf.String("attachment.txt"),
                    pdf.make_indirect(
                        pikepdf.Dictionary(
                            Type=pikepdf.Name.Filespec,
                            F=pikepdf.String("attachment.txt"),
                        )
                    ),
                ]
            ),
        )
        pdf.Root[pikepdf.Name.Names] = names
    buf = io.BytesIO()
    pdf.save(buf, deterministic_id=True, linearize=False, qdf=False)
    pdf.close()
    return buf.getvalue()


@pytest.fixture
def simple_pdf() -> bytes:
    return build_pdf(pages=1, title="Original")


@pytest.fixture
def three_page_pdf() -> bytes:
    return build_pdf(pages=3, title="Three Pages", author="Tester")


@pytest.fixture
def ocg_pdf() -> bytes:
    return build_pdf(pages=1, add_ocg="Bleed")


@pytest.fixture
def js_pdf() -> bytes:
    return build_pdf(pages=1, add_javascript=True)


@pytest.fixture
def embedded_files_pdf() -> bytes:
    return build_pdf(pages=1, add_embedded_file=True)


@pytest.fixture
def printer_pdf() -> bytes:
    """Single-page PDF with declared TrimBox/BleedBox + slug margin."""
    return build_pdf(pages=1, printer_pages=True, title="Printer Page")


@pytest.fixture
def two_page_printer_pdf() -> bytes:
    return build_pdf(pages=2, printer_pages=True, title="Two-up Printer")


def _content_page(pdf: pikepdf.Pdf, marker: str) -> pikepdf.Page:
    """Page with a unique stroke pattern in its content stream so per-cell
    extract checks can distinguish source pages."""
    return pikepdf.Page(
        pdf.make_indirect(
            pikepdf.Dictionary(
                Type=pikepdf.Name.Page,
                MediaBox=pikepdf.Array([0, 0, 612, 792]),
                Resources=pikepdf.Dictionary(),
                Contents=pdf.make_stream(
                    (f"q 100 100 m 200 200 l S Q  % {marker}").encode("ascii")
                ),
            )
        )
    )


def build_content_pdf(*, pages: int) -> bytes:
    """Multi-page PDF where every page has a unique content stream marker.
    Used by impose tests for per-cell extract verification."""
    pdf = pikepdf.new()
    for i in range(pages):
        pdf.pages.append(_content_page(pdf, f"page-{i}"))
    buf = io.BytesIO()
    pdf.save(buf, deterministic_id=True, linearize=False, qdf=False)
    pdf.close()
    return buf.getvalue()


@pytest.fixture
def four_page_content_pdf() -> bytes:
    return build_content_pdf(pages=4)


@pytest.fixture
def two_page_content_pdf() -> bytes:
    return build_content_pdf(pages=2)
