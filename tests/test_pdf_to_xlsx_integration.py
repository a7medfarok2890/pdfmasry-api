"""Integration tests for the real PDF->Excel pipeline (extract_structured_
content + build_workbook, as wired into main.process_pdf_to_xlsx) against
real PDF fixtures — not mocks. Fixtures live in tests/fixtures/:

  sales_report_ar.pdf — Arabic 4-page report: title + intro paragraph,
    a short table, a narrative paragraph, then a table spanning 3 pages
    (tests continuation-merging and repeated-header dropping), matching
    the structure reported as broken in production.
  english_table.pdf   — a plain English table with a title+intro.
  from_excel.pdf       — produced from a real .xlsx file (not a Word
    document), with borders, to test the "PDF that came from Excel"
    scenario explicitly.

This exercises the exact checklist from the bug report:
  - sheet count == table count (+ a "Text" sheet if there's leftover text),
    never "Page N"
  - the first revenue-like cell is a real number (int/float), not str,
    with thousands-separator formatting
  - header row has a fill and bold font, column widths > 10
  - a growth percentage like "+12.4%" becomes 0.124 with percent format
  - a phone number / invoice code stays text
  - the Arabic file comes out with no corrupted or missing letters
"""
from __future__ import annotations

import os

import openpyxl
import pytest

import xlsx_builder as xb

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture
def arabic_workbook(tmp_path):
    content = xb.extract_structured_content(os.path.join(FIXTURES, "sales_report_ar.pdf"))
    wb = xb.build_workbook(content)
    out = tmp_path / "out.xlsx"
    wb.save(out)
    return openpyxl.load_workbook(out), content


def test_arabic_report_sheet_count_matches_table_count_no_page_sheets(arabic_workbook):
    wb, content = arabic_workbook
    assert len(content.tables) == 2  # branch summary table + merged monthly table
    expected = {"جدول 1", "جدول 2"}
    if content.text_blocks:
        expected.add("نص")
    assert set(wb.sheetnames) == expected
    assert not any(name.startswith("Page") for name in wb.sheetnames)


def test_arabic_report_monthly_table_merged_across_its_three_pages(arabic_workbook):
    _, content = arabic_workbook
    monthly = content.tables[1]
    # 1 header + 12 months * 5 products + 1 totals row, no repeated headers
    # or stray page-boundary fragments from the 3-page split.
    assert len(monthly.rows) == 62
    assert monthly.rows[0] == ["الشهر", "المنتج", "الكمية", "رقم الفاتورة", "رقم الهاتف", "الإجمالي"]
    assert monthly.rows.count(monthly.rows[0]) == 1  # header not duplicated


def test_arabic_report_title_and_intro_paragraph_both_reach_the_sheet(arabic_workbook):
    _, content = arabic_workbook
    branch_table = content.tables[0]
    assert len(branch_table.preceding_paragraphs) >= 2
    joined = " ".join(branch_table.preceding_paragraphs)
    assert "تقرير المبيعات" in joined
    assert "2026" in joined
    assert "يلخص أداء المبيعات" in joined  # the intro paragraph itself


def test_arabic_report_revenue_cell_is_a_real_number_with_thousands_format(arabic_workbook):
    wb, _ = arabic_workbook
    ws = wb["جدول 1"]
    # Row layout: 3 preceding-paragraph rows, header row 4, first data row 5.
    header_row = 4
    assert ws.cell(row=header_row, column=2).value == "الإيرادات"
    revenue_cell = ws.cell(row=header_row + 1, column=2)
    assert isinstance(revenue_cell.value, int)
    assert revenue_cell.value == 1250000
    assert revenue_cell.number_format == "#,##0"


def test_arabic_report_growth_percentage_converts_correctly(arabic_workbook):
    wb, _ = arabic_workbook
    ws = wb["جدول 1"]
    growth_cell = ws.cell(row=5, column=3)
    assert growth_cell.value == pytest.approx(0.124)
    assert growth_cell.number_format == "0.0%"


def test_arabic_report_phone_and_invoice_numbers_stay_text(arabic_workbook):
    wb, _ = arabic_workbook
    ws = wb["جدول 2"]
    header_row = 3  # 2 preceding-paragraph rows + header on row 3
    assert ws.cell(row=header_row, column=4).value == "رقم الفاتورة"
    assert ws.cell(row=header_row, column=5).value == "رقم الهاتف"
    first_invoice = ws.cell(row=header_row + 1, column=4).value
    first_phone = ws.cell(row=header_row + 1, column=5).value
    assert isinstance(first_invoice, str) and first_invoice.startswith("INV-")
    assert isinstance(first_phone, str) and first_phone == "01012345"


def test_arabic_report_totals_row_stays_one_cell_not_scattered(arabic_workbook):
    wb, _ = arabic_workbook
    ws = wb["جدول 2"]
    last_row = ws.max_row
    assert ws.cell(row=last_row, column=1).value == "الإجمالي"
    assert ws.cell(row=last_row, column=2).value is None
    total_value = ws.cell(row=last_row, column=6).value
    assert isinstance(total_value, int)
    assert total_value == 1050000


def test_arabic_report_header_row_styled_and_columns_wide_enough(arabic_workbook):
    wb, _ = arabic_workbook
    ws = wb["جدول 1"]
    header_row = 4
    header_cell = ws.cell(row=header_row, column=1)
    assert header_cell.font.bold is True
    assert header_cell.fill.start_color.rgb == "00" + xb.HEADER_FILL_COLOR
    for col in "ABC":
        assert ws.column_dimensions[col].width > 10


def test_arabic_report_sheet_is_right_to_left(arabic_workbook):
    wb, _ = arabic_workbook
    for name in wb.sheetnames:
        assert wb[name].sheet_view.rightToLeft is True


def test_arabic_report_no_corrupted_or_missing_arabic_letters(arabic_workbook):
    _, content = arabic_workbook
    all_text = " ".join(
        cell
        for t in content.tables
        for row in t.rows
        for cell in row
        if cell
    ) + " ".join(
        p for t in content.tables for p in t.preceding_paragraphs
    ) + " ".join(content.text_blocks)

    # Every expected Arabic word from the source document must survive
    # intact and correctly spelled — this is the real check that nothing
    # was dropped or scrambled into mojibake.
    expected_words = [
        "المبيعات", "السنوي", "الفرع", "الإيرادات", "القاهرة",
        "الإسكندرية", "الجيزة", "الشهر", "المنتج", "الكمية",
        "يناير", "فبراير", "مارس", "قهوة", "شاي", "سكر", "أرز", "زيت",
    ]
    for word in expected_words:
        assert word in all_text, f"expected Arabic word {word!r} missing or corrupted in extracted text"

    # No Unicode replacement character or private-use mojibake markers.
    assert "�" not in all_text


def test_english_table_title_intro_and_total_preserved(tmp_path):
    content = xb.extract_structured_content(os.path.join(FIXTURES, "english_table.pdf"))
    assert len(content.tables) == 1
    table = content.tables[0]
    assert "Quarterly Sales Report" in table.preceding_paragraphs[0]
    assert any("summarizes quarterly sales" in p for p in table.preceding_paragraphs)

    wb = xb.build_workbook(content)
    assert wb.sheetnames == ["Table 1"]
    ws = wb["Table 1"]
    assert ws.sheet_view.rightToLeft is False

    header_row = 1 + len(table.preceding_paragraphs)
    revenue_cell = ws.cell(row=header_row + 1, column=2)
    assert revenue_cell.value == 1250000
    growth_cell = ws.cell(row=header_row + 1, column=3)
    assert growth_cell.value == pytest.approx(0.124)

    last_row = ws.max_row
    assert ws.cell(row=last_row, column=1).value == "Total"
    assert ws.cell(row=last_row, column=3).value == pytest.approx(1005500.50)


def test_pdf_from_real_excel_file_detects_the_table_and_formats_numbers(tmp_path):
    content = xb.extract_structured_content(os.path.join(FIXTURES, "from_excel.pdf"))
    assert len(content.tables) == 1
    table = content.tables[0]
    assert table.rows[0] == ["Category", "Amount", "Change"]

    wb = xb.build_workbook(content)
    ws = wb["Table 1"]
    rent_amount = ws.cell(row=2, column=2).value
    assert rent_amount == 45000
    phone_row = [r for r in table.rows if r[0] == "Phone"][0]
    assert phone_row[1] == "01099998888"  # stays text, not misread as a number


def test_main_process_pdf_to_xlsx_end_to_end(tmp_path):
    """The actual function main.py's /api/pdf-to-excel endpoint calls."""
    import main

    out = tmp_path / "converted.xlsx"
    main.process_pdf_to_xlsx(os.path.join(FIXTURES, "sales_report_ar.pdf"), str(out))
    assert out.exists() and out.stat().st_size > 0
    wb = openpyxl.load_workbook(out)
    assert not any(name.startswith("Page") for name in wb.sheetnames)
