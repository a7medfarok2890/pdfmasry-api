"""Unit tests for xlsx_builder.py's numeric classification and worksheet
styling — the pure, provider-agnostic half of the PDF→Excel pipeline."""
from __future__ import annotations

import openpyxl
import pytest

import xlsx_builder as xb


# ---------------------------------------------------------------------------
# classify_numeric_cell
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected_value,expected_format",
    [
        ("1,250,000", 1250000, "#,##0"),
        ("(1,120,000)", -1120000, "#,##0;(#,##0)"),
        ("+12.4%", pytest.approx(0.124), "0.0%"),
        ("-3.1%", pytest.approx(-0.031), "0.0%"),
        ("1234.56", 1234.56, "#,##0.00"),
        ("-1234.56", -1234.56, "#,##0.00"),
        ("(1,120,000.50)", -1120000.50, "#,##0.00;(#,##0.00)"),
        ("42", 42, "#,##0"),
        ("0", 0, "#,##0"),
        ("-7", -7, "#,##0"),
    ],
)
def test_classify_numeric_cell_converts_formatted_numbers(raw, expected_value, expected_format):
    result = xb.classify_numeric_cell(raw)
    assert result is not None
    value, fmt = result
    assert value == expected_value
    assert fmt == expected_format


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "01012345678",      # Egyptian mobile number, no separators
        "0123",             # leading zero — likely a code
        "1234567",          # 7 bare digits, no separators — phone/code-shaped
        "INV-1001",         # invoice code
        "A123",
        "2026-01-15",       # date
        "15/01/2026",       # date
        "٢٠٢٦",             # Eastern Arabic digits currently unhandled — stays text, not misread
        "رقم الفاتورة 1001",
    ],
)
def test_classify_numeric_cell_leaves_ambiguous_values_as_text(raw):
    assert xb.classify_numeric_cell(raw) is None


def test_classify_numeric_cell_never_raises_on_arbitrary_text():
    for raw in ["مرحبا", "---", "N/A", "()", "%", "+-"]:
        assert xb.classify_numeric_cell(raw) is None


# ---------------------------------------------------------------------------
# is_arabic_heavy
# ---------------------------------------------------------------------------

def test_is_arabic_heavy_true_for_arabic_document():
    assert xb.is_arabic_heavy("تقرير المبيعات السنوي لعام 2026 شامل جميع الفروع") is True


def test_is_arabic_heavy_false_for_english_document():
    assert xb.is_arabic_heavy("Annual Sales Report 2026 covering all branches") is False


def test_is_arabic_heavy_false_for_empty_text():
    assert xb.is_arabic_heavy("") is False


# ---------------------------------------------------------------------------
# write_table_block / build_workbook
# ---------------------------------------------------------------------------

def test_write_table_block_converts_numbers_and_styles_header(tmp_path):
    block = xb.TableBlock(
        rows=[
            ["المنتج", "الإيرادات", "النمو"],
            ["قهوة", "1,250,000", "+12.4%"],
            ["شاي", "(1,120,000)", "-3.1%"],
        ],
        preceding_paragraphs=["تقرير المبيعات"],
    )
    wb = xb.build_workbook(xb.DocumentContent(tables=[block]))
    out = tmp_path / "out.xlsx"
    wb.save(out)

    reloaded = openpyxl.load_workbook(out)
    assert reloaded.sheetnames == ["جدول 1"]
    ws = reloaded["جدول 1"]

    # Row 1 is the merged title, row 2 is the header, row 3+ is data.
    assert ws.cell(row=1, column=1).value == "تقرير المبيعات"
    assert ws.cell(row=2, column=1).value == "المنتج"
    assert ws.cell(row=2, column=1).font.bold is True

    revenue_cell = ws.cell(row=3, column=2)
    assert revenue_cell.value == 1250000
    assert isinstance(revenue_cell.value, int)
    assert revenue_cell.number_format == "#,##0"

    negative_cell = ws.cell(row=4, column=2)
    assert negative_cell.value == -1120000

    growth_cell = ws.cell(row=3, column=3)
    assert growth_cell.value == pytest.approx(0.124)
    assert growth_cell.number_format == "0.0%"

    assert ws.sheet_view.rightToLeft is True
    assert ws.freeze_panes == "A3"  # frozen just below the header row (title occupies row 1)


def test_write_table_block_keeps_every_preceding_paragraph_not_just_the_title(tmp_path):
    # The user's own report: both the table's title AND the intro
    # paragraph before it were being dropped entirely — both must land as
    # rows above the table, not just the title.
    block = xb.TableBlock(
        rows=[["H"], ["1"]],
        preceding_paragraphs=["تقرير المبيعات السنوي 2026", "هذا ملخص الأداء لعام 2026 بالكامل."],
    )
    wb = xb.build_workbook(xb.DocumentContent(tables=[block]))
    ws = wb["جدول 1"]
    assert ws.cell(row=1, column=1).value == "تقرير المبيعات السنوي 2026"
    assert ws.cell(row=1, column=1).font.bold is True
    assert ws.cell(row=2, column=1).value == "هذا ملخص الأداء لعام 2026 بالكامل."
    assert ws.cell(row=3, column=1).value == "H"  # header now on row 3
    assert ws.freeze_panes == "A4"


def test_write_table_block_keeps_mixed_column_cells_independent(tmp_path):
    block = xb.TableBlock(
        rows=[
            ["الصنف", "الكمية"],
            ["أ", "42"],
            ["ب", "غير متوفر"],
        ]
    )
    wb = xb.build_workbook(xb.DocumentContent(tables=[block]))
    ws = wb["جدول 1"]
    assert ws.cell(row=2, column=2).value == 42
    assert ws.cell(row=3, column=2).value == "غير متوفر"


def test_build_workbook_no_file_centric_column_padding_or_merged_data_cells():
    # Guards against the iLovePDF flaw of merging cells that shouldn't be
    # merged: only the title row may be merged, never data/body cells.
    block = xb.TableBlock(rows=[["A", "B"], ["1,000", "2,000"]], preceding_paragraphs=["Title"])
    wb = xb.build_workbook(xb.DocumentContent(tables=[block]))
    ws = wb["Table 1"]
    merged_ranges = [str(r) for r in ws.merged_cells.ranges]
    assert len(merged_ranges) == 1  # only the title row
    assert "A1:B1" in merged_ranges


def test_build_workbook_sheet_per_table_not_per_page():
    tables = [
        xb.TableBlock(rows=[["A"], ["1"]]),
        xb.TableBlock(rows=[["B"], ["2"]]),
    ]
    wb = xb.build_workbook(xb.DocumentContent(tables=tables, text_blocks=["Some paragraph."]))
    assert wb.sheetnames == ["Table 1", "Table 2", "Text"]
    assert not any(name.startswith("Page") for name in wb.sheetnames)


def test_build_workbook_text_sheet_keeps_each_paragraph_as_one_cell():
    content = xb.DocumentContent(
        text_blocks=["This is a full paragraph that was wrapped across three lines in the source PDF."]
    )
    wb = xb.build_workbook(content)
    ws = wb["Text"]
    assert ws.cell(row=1, column=1).value == (
        "This is a full paragraph that was wrapped across three lines in the source PDF."
    )


def test_build_workbook_empty_content_still_produces_a_sheet():
    wb = xb.build_workbook(xb.DocumentContent())
    assert len(wb.sheetnames) == 1


def test_build_workbook_zebra_banding_only_for_large_tables():
    small = xb.TableBlock(rows=[["H"], ["1"], ["2"]])
    big_rows = [["H"]] + [[str(i)] for i in range(20)]
    big = xb.TableBlock(rows=big_rows)
    wb = xb.build_workbook(xb.DocumentContent(tables=[small, big]))
    ws_small = wb["Table 1"]
    ws_big = wb["Table 2"]
    # No banding fill anywhere in the small table's body rows.
    assert all(
        ws_small.cell(row=r, column=1).fill.start_color.rgb in (None, "00000000")
        for r in range(2, 4)
    )
    # At least one banded row in the big table.
    assert any(
        ws_big.cell(row=r, column=1).fill.start_color.rgb == "00" + xb.BAND_FILL_COLOR
        for r in range(2, 22)
    )


def test_build_workbook_column_width_bounds():
    long_text = "x" * 200
    block = xb.TableBlock(rows=[["H1", "H2"], ["short", long_text]])
    wb = xb.build_workbook(xb.DocumentContent(tables=[block]))
    ws = wb["Table 1"]
    assert ws.column_dimensions["A"].width >= xb.MIN_COL_WIDTH
    assert ws.column_dimensions["B"].width == xb.MAX_COL_WIDTH
