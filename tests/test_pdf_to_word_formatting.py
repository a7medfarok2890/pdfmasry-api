"""Integration tests for PDF->Word formatting transfer (font size/bold/
color, cell shading, heading styles, column widths) — run through the
REAL pdf2docx + arabic_docx.repair_arabic_docx pipeline against real PDF
fixtures (not mocks). Fixtures live in tests/fixtures/:

  word_sales_report_ar.pdf — Arabic report matching the bug report's
    structure: 20pt bold colored title, 13pt bold section headings, 11pt
    body text, two tables each with a dark-blue bold-white header row,
    alternating row shading, and distinctly different (non-equal) column
    widths per table.
  word_english.pdf  — the same structure in English.
  word_mixed.pdf    — Arabic report with embedded English company name
    and numbers mid-sentence.
  word_scanned.pdf  — an image-only page with no text layer at all
    (simulates a scanned document), to confirm formatting transfer never
    crashes the conversion when there's nothing to transfer.
"""
from __future__ import annotations

import os
import re

import fitz
import pytest
from docx import Document
from docx.oxml.ns import qn
from pdf2docx import Converter

import arabic_docx

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
SHD_TAG = qn("w:shd")
FILL_ATTR = qn("w:fill")


def _convert(pdf_name: str, tmp_path) -> Document:
    pdf_path = os.path.join(FIXTURES, pdf_name)
    docx_path = str(tmp_path / "out.docx")
    cv = Converter(pdf_path)
    cv.convert(docx_path)
    cv.close()
    arabic_docx.repair_arabic_docx(pdf_path, docx_path)
    return Document(docx_path)


@pytest.fixture(scope="module")
def arabic_doc(tmp_path_factory):
    return _convert("word_sales_report_ar.pdf", tmp_path_factory.mktemp("ar"))


def _cell_shd_fill(cell) -> str | None:
    shd = cell._tc.get_or_add_tcPr().find(SHD_TAG)
    return shd.get(FILL_ATTR) if shd is not None else None


def _first_run_with_text(paragraph):
    for run in paragraph.runs:
        if run.text.strip():
            return run
    return None


# ---------------------------------------------------------------------------
# Title: size, bold, color
# ---------------------------------------------------------------------------

def test_title_is_larger_bold_and_the_source_color(arabic_doc):
    title_para = next(p for p in arabic_doc.paragraphs if "تقرير المبيعات" in p.text)
    body_para = next(p for p in arabic_doc.paragraphs if "يلخص أداء المبيعات" in p.text)

    title_run = _first_run_with_text(title_para)
    body_run = _first_run_with_text(body_para)

    assert title_run.font.size > body_run.font.size
    assert title_run.font.bold is True
    assert str(title_run.font.color.rgb) == "0F4C81"


# ---------------------------------------------------------------------------
# Table header cells: shading + bold white text
# ---------------------------------------------------------------------------

def test_table_header_cells_have_shading_and_bold_white_text(arabic_doc):
    for table in arabic_doc.tables:
        header_row = table.rows[0]
        for cell in header_row.cells:
            fill = _cell_shd_fill(cell)
            assert fill is not None and fill.upper() == "0F4C81"
            run = _first_run_with_text(cell.paragraphs[0])
            assert run.font.bold is True
            assert str(run.font.color.rgb) == "FFFFFF"


def test_table_data_rows_have_alternating_shading(arabic_doc):
    table = arabic_doc.tables[0]
    fills = [_cell_shd_fill(row.cells[0]) for row in table.rows[1:]]
    # Not every data row is shaded — only the alternating ones, matching
    # the source's F2F6FA banding.
    assert any(f and f.upper() == "F2F6FA" for f in fills)
    assert any(f is None for f in fills)


# ---------------------------------------------------------------------------
# Heading styles
# ---------------------------------------------------------------------------

def test_document_has_at_least_one_heading1(arabic_doc):
    assert any(p.style.name == "Heading 1" for p in arabic_doc.paragraphs)


def test_section_titles_promoted_to_heading2_not_body_paragraphs(arabic_doc):
    heading_names = {p.style.name for p in arabic_doc.paragraphs if "ملخص الفروع" in p.text or "تفاصيل المنتجات" in p.text}
    assert heading_names == {"Heading 2"}


def test_ordinary_paragraphs_are_not_turned_into_headings(arabic_doc):
    body_para = next(p for p in arabic_doc.paragraphs if "يلخص أداء المبيعات" in p.text)
    assert body_para.style.name == "Normal"


# ---------------------------------------------------------------------------
# Column widths
# ---------------------------------------------------------------------------

def test_column_widths_are_not_equal_when_the_source_isnt(arabic_doc):
    for table in arabic_doc.tables:
        widths = [cell.width for cell in table.rows[0].cells]
        assert len(set(widths)) > 1, "columns came out equal-width, source had distinct widths"


def test_both_tables_get_independently_correct_widths(arabic_doc):
    # Table 0's source proportions were 180:144:108pt: widest column first.
    widths0 = [c.width for c in arabic_doc.tables[0].rows[0].cells]
    assert widths0[0] > widths0[1] > widths0[2]
    # Table 1's source proportions were 108:122.4:93.6:122.4pt.
    widths1 = [c.width for c in arabic_doc.tables[1].rows[0].cells]
    assert abs(widths1[1] - widths1[3]) < 1000  # equal within EMU rounding
    assert widths1[1] > widths1[0] > widths1[2]


# ---------------------------------------------------------------------------
# The two small text bugs
# ---------------------------------------------------------------------------

def test_wrapped_sentence_keeps_a_space_at_the_line_boundary(arabic_doc):
    full_text = "\n".join(p.text for p in arabic_doc.paragraphs)
    assert "جميع الأرقام" in full_text
    assert "جميعالأرقام" not in full_text


def test_parenthetical_text_has_no_stray_space_and_no_injected_characters(arabic_doc):
    # The stored logical text for a parenthetical that displays correctly
    # in RTL (bidi mirrors bracket glyphs at render time, confirmed via a
    # visual render) isn't necessarily "(" before ")" as a raw string, and
    # a paren that closes a phrase legitimately has a space before the
    # *next* word — so this checks the specific reported pattern (the
    # paren touching the first word of what it encloses, "(شركة") rather
    # than assuming a space next to any paren anywhere is wrong.
    full_text = "\n".join(p.text for p in arabic_doc.paragraphs)
    assert "ج.م" in full_text
    assert re.search(r"[()]شركة", full_text)
    assert not re.search(r"[()]\sشركة", full_text)
    assert "&" not in full_text


# ---------------------------------------------------------------------------
# Font name
# ---------------------------------------------------------------------------

def test_every_run_gets_an_explicit_font_name(arabic_doc):
    for paragraph in arabic_doc.paragraphs:
        for run in paragraph.runs:
            if not run.text.strip():
                continue
            rPr = run._r.get_or_add_rPr()
            fonts = rPr.find(qn("w:rFonts"))
            assert fonts is not None
            assert fonts.get(qn("w:ascii")) == "Arial"
            assert fonts.get(qn("w:cs")) == "Arial"


# ---------------------------------------------------------------------------
# English, mixed, and scanned documents
# ---------------------------------------------------------------------------

def test_english_document_gets_the_same_formatting_treatment(tmp_path):
    doc = _convert("word_english.pdf", tmp_path)
    title_para = next(p for p in doc.paragraphs if "Quarterly Sales Report" in p.text)
    title_run = _first_run_with_text(title_para)
    assert title_run.font.bold is True
    assert str(title_run.font.color.rgb) == "0F4C81"
    for table in doc.tables:
        header_cell = table.rows[0].cells[0]
        assert _cell_shd_fill(header_cell).upper() == "0F4C81"
        widths = [c.width for c in table.rows[0].cells]
        assert len(set(widths)) > 1


def test_mixed_arabic_english_document_styles_correctly(tmp_path):
    doc = _convert("word_mixed.pdf", tmp_path)
    title_para = next(p for p in doc.paragraphs if "Acme Corp" in p.text and "تقرير" in p.text)
    title_run = _first_run_with_text(title_para)
    assert title_run.font.bold is True
    assert str(title_run.font.color.rgb) == "0F4C81"
    # The embedded Latin company name/numbers must survive untouched.
    full_text = "\n".join(p.text for p in doc.paragraphs)
    assert "Acme Corp" in full_text
    assert "12.4" in full_text


def test_scanned_image_only_pdf_does_not_crash_the_conversion(tmp_path):
    # No text layer at all — formatting transfer must be a silent no-op,
    # never an exception that breaks the whole conversion.
    doc = _convert("word_scanned.pdf", tmp_path)
    assert hasattr(doc, "paragraphs")
