"""Regression tests for Arabic PDF-to-Word post-processing."""
from __future__ import annotations

import os

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt

import arabic_docx


def test_repairs_browser_pdf_combining_mark_artifacts():
    damaged = "اختبار الحروف العربية والتشكيل: ُمؤَّسَّسسة الصيانة السريعة."

    assert arabic_docx.repair_arabic_combining_marks(damaged) == (
        "اختبار الحروف العربية والتشكيل: مُؤسَّسة الصيانة السريعة."
    )


def test_repairs_lam_alef_hamza_transposition_from_bidi_round_trip():
    # Found live: get_display()'s bidi round-trip (used throughout this
    # module to restore logical order) transposes the lam and the hamza
    # carrier around a lam-alef ligature.
    assert arabic_docx.repair_arabic_combining_marks("اإليرادات") == "الإيرادات"
    assert arabic_docx.repair_arabic_combining_marks("األرقام") == "الأرقام"
    assert arabic_docx.repair_arabic_combining_marks("اإلجمالي") == "الإجمالي"
    # Unaffected text stays untouched.
    assert arabic_docx.repair_arabic_combining_marks("القاهرة") == "القاهرة"


def test_repairs_negation_la_after_a_conjunction_letter():
    # Found live on the user's real test file: "وال ترتبط بأي شركة" should
    # be "ولا ترتبط بأي شركة" — same lam/alef transposition as the hamza
    # case above, but for the bare negation particle "لا" ("not").
    assert arabic_docx.repair_arabic_combining_marks("وال ترتبط بأي شركة") == "ولا ترتبط بأي شركة"
    assert arabic_docx.repair_arabic_combining_marks("فال يوجد شيء") == "فلا يوجد شيء"
    assert arabic_docx.repair_arabic_combining_marks("بال شك") == "بلا شك"
    assert arabic_docx.repair_arabic_combining_marks("ال يوجد.") == "لا يوجد."
    assert arabic_docx.repair_arabic_combining_marks("ال، ولا شيء آخر") == "لا، ولا شيء آخر"


def test_does_not_touch_the_definite_article():
    # The fix above must never fire on an ordinary "ال..." word — the
    # definite article is always glued directly to its noun, with no
    # word boundary right after "ال", unlike the negation particle.
    assert arabic_docx.repair_arabic_combining_marks("الفرع الرئيسي") == "الفرع الرئيسي"
    assert arabic_docx.repair_arabic_combining_marks("والفرع الرئيسي") == "والفرع الرئيسي"
    assert arabic_docx.repair_arabic_combining_marks("فالقاهرة كبيرة") == "فالقاهرة كبيرة"
    assert arabic_docx.repair_arabic_combining_marks("بالتالي سنفعل") == "بالتالي سنفعل"
    assert arabic_docx.repair_arabic_combining_marks("القاهرة") == "القاهرة"


def test_restore_logical_line_keeps_parentheses_correctly_oriented():
    # Found live: running get_display() on a pure-Arabic line (no
    # embedded Latin/digits) mirrors parenthesis characters — a
    # rendering-time decision that shouldn't be reapplied when recovering
    # logical text from an already-visual extraction. A plain reversal
    # is correct for this case; this is the exact line from the user's
    # real test file (physical/visual order, as pdfplumber extracts it).
    raw = ")ةبرجتلا ضارغلأ ةيمهو ةكرش( عيزوتلاو ةراجتلل لاثملا ةكرش"
    corrected = arabic_docx._restore_logical_line(raw)
    assert corrected.count("(") == 1
    assert corrected.count(")") == 1
    assert corrected.index("(") < corrected.index(")")


def test_restore_logical_line_keeps_a_trailing_number_at_the_end():
    # Found live: a title ending in a year came back with the year moved
    # to the front and no space ("2026 تقرير..." or "...السنوي2026"
    # depending on the exact bidi call) instead of "...السنوي 2026".
    raw = "2026 يونسلا تاعيبملا ريرقت"
    corrected = arabic_docx._restore_logical_line(raw)
    assert corrected == "تقرير المبيعات السنوي 2026"


def test_restore_logical_line_keeps_embedded_numbers_in_order():
    raw = "حبر يفاص م.ج 600,000 | فيلاكت م.ج 2,480,000 | تاداريإ م.ج 3,080,000 :يلامجلإا"
    corrected = arabic_docx._restore_logical_line(raw)
    assert "3,080,000" in corrected
    assert "2,480,000" in corrected
    assert "600,000" in corrected


def test_repairs_only_numbers_confirmed_by_source_coordinates():
    replacements = {
        "١٢/٨٠/٦٢٠٢": "٢٠٢٦/٠٨/٢١",
        "٠٥٫٥٢١": "١٢٥٫٥٠",
        "٠٠٫٦٢١٬١": "١٬١٢٦٫٠٠",
    }

    assert arabic_docx.repair_extracted_text(
        "التاريخ: ١٢/٨٠/٦٢٠٢ — السعر ٠٥٫٥٢١",
        replacements,
    ) == "التاريخ: ٢٠٢٦/٠٨/٢١ — السعر ١٢٥٫٥٠"

    # A token that is not proven reversed by source coordinates is untouched.
    assert arabic_docx.repair_extracted_text("رقم ٢٠٢٦", replacements) == "رقم ٢٠٢٦"


def test_repair_extracted_text_removes_stray_space_after_opening_paren():
    assert (
        arabic_docx.repair_extracted_text("( شركة وهمية لأغراض التجربة)", {})
        == "(شركة وهمية لأغراض التجربة)"
    )


def test_repair_extracted_text_removes_stray_space_before_closing_paren():
    assert (
        arabic_docx.repair_extracted_text("(ج.م )", {}) == "(ج.م)"
    )


def test_repair_extracted_text_leaves_normal_parenthetical_text_alone():
    assert (
        arabic_docx.repair_extracted_text("(شركة وهمية لأغراض التجربة)", {})
        == "(شركة وهمية لأغراض التجربة)"
    )


def test_repair_docx_adds_rtl_and_restores_missing_total(tmp_path, monkeypatch):
    docx_path = tmp_path / "converted.docx"
    source_path = tmp_path / "source.pdf"
    source_path.write_bytes(b"%PDF-test-fixture")

    document = Document()
    document.add_paragraph("العميل: أحمد — التاريخ: ١٢/٨٠/٦٢٠٢")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "الصنف"
    table.cell(0, 1).text = "الإجمالي"
    table.cell(1, 0).text = "فلتر زيت"
    table.cell(1, 1).text = "٠٠٫١٥٢ / ٠٥٫٥٢١"
    document.add_paragraph(" جنيه.")
    document.save(docx_path)

    monkeypatch.setattr(
        arabic_docx,
        "extract_numeric_replacements",
        lambda _path: {
            "١٢/٨٠/٦٢٠٢": "٢٠٢٦/٠٨/٢١",
            "٠٠٫١٥٢": "٢٥١٫٠٠",
            "٠٥٫٥٢١": "١٢٥٫٥٠",
        },
    )
    monkeypatch.setattr(
        arabic_docx,
        "extract_logical_lines",
        lambda _path: ["الإجمالي المستحق: ١٬١٢٦٫٠٠ جنيه."],
    )
    monkeypatch.setattr(arabic_docx, "extract_table_cell_lines", lambda _path: [])

    arabic_docx.repair_arabic_docx(str(source_path), str(docx_path))

    repaired = Document(docx_path)
    text = "\n".join(
        [paragraph.text for paragraph in repaired.paragraphs]
        + [cell.text for row in repaired.tables[0].rows for cell in row.cells]
    )
    assert "٢٠٢٦/٠٨/٢١" in text
    assert "٢٥١٫٠٠" in text
    assert "١٢٥٫٥٠" in text
    assert "الإجمالي المستحق: ١٬١٢٦٫٠٠ جنيه." in text
    assert "١٢/٨٠/٦٢٠٢" not in text
    assert "٠٠٫١٥٢" not in text
    assert "٠٥٫٥٢١" not in text

    arabic_paragraph = repaired.paragraphs[0]
    assert arabic_paragraph._p.pPr.find(qn("w:bidi")) is not None
    assert arabic_paragraph.runs[0]._r.rPr.find(qn("w:rtl")) is not None
    assert repaired.tables[0]._tbl.tblPr.find(qn("w:bidiVisual")) is not None


def test_aligns_concatenated_and_physically_ordered_source_lines():
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("فاتورة تجريبية PDF مصري")
    paragraph.add_run("فاتورة تجريبية")
    paragraph.add_run("\n")
    paragraph.add_run("٢٠٢٦/٠٨/٢١")
    paragraph.add_run(" :العميل: أحمد محمد — التاريخ")

    matched = arabic_docx._align_paragraph_to_source_lines(
        paragraph,
        [
            "فاتورة تجريبية PDF مصري",
            "فاتورة تجريبية",
            "العميل: أحمد محمد — التاريخ: ٢٠٢٦/٠٨/٢١",
        ],
    )

    assert matched is True
    # A trailing space before each line break: the break itself is now a
    # real w:br (so Word still shows separate lines), not a literal "\n"
    # stuffed inside w:t — a reader that only looks at w:t content (as the
    # iLovePDF-comparison tooling does) would otherwise see no separator
    # at all between lines, which is exactly the "جميعالأرقام" bug this
    # fixes when the same join happens mid-sentence across a wrapped line.
    assert paragraph.text == (
        "فاتورة تجريبية PDF مصري \n"
        "فاتورة تجريبية \n"
        "العميل: أحمد محمد — التاريخ: ٢٠٢٦/٠٨/٢١"
    )
    # The break is a genuine OOXML element, not a character inside w:t.
    first_run = paragraph.runs[0]
    assert first_run._r.find(qn("w:br")) is not None


def test_force_rtl_matches_ilovepdf_direct_paragraph_properties():
    document = Document()
    paragraph = document.add_paragraph()
    arabic_run = paragraph.add_run("التاريخ: ")
    latin_run = paragraph.add_run("14 February 2026")

    arabic_docx.mark_paragraph_rtl(
        paragraph,
        force=True,
        align_to_start=True,
    )

    p_pr = paragraph._p.pPr
    assert p_pr.find(qn("w:bidi")).get(qn("w:val")) == "1"
    assert p_pr.find(qn("w:jc")).get(qn("w:val")) == "left"
    assert arabic_run._r.rPr.find(qn("w:rtl")).get(qn("w:val")) == "1"
    assert latin_run._r.rPr is None or latin_run._r.rPr.find(qn("w:rtl")) is None


def test_single_line_correction_refuses_a_match_that_injects_an_ampersand():
    # Found live on the real test file: this exact PDF's font decodes an
    # Arabic diacritic (tanween fath, "ً") through pdfplumber as a literal
    # "&" — invisible to the character-signature match since punctuation
    # and diacritics are both stripped before comparing, so the otherwise
    # correct (and already-correct) pdf2docx text was being overwritten by
    # a pdfplumber "ground truth" line that silently injected "&".
    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("يقدم ملخصًا للتكاليف")

    corrected = arabic_docx._correct_single_line_paragraph_text(
        paragraph, ["يقدم ملخصا& للتكاليف"]
    )

    assert corrected is False
    assert paragraph.text == "يقدم ملخصًا للتكاليف"


def test_corrects_a_single_logical_line_split_across_identically_styled_runs():
    # Found live on the real test file: a totals line came back from
    # pdf2docx as 3 separate but identically-styled runs (apparently split
    # at pdf2docx's own internal text-chunk boundaries), so neither the
    # multi-line realignment (expects each run-group to match a different
    # source line) nor the single-run correction (scoped to one run)
    # caught it, leaving the totals line's words and numbers scrambled.
    document = Document()
    paragraph = document.add_paragraph()
    for run_text in (
        " |   ج.م تكاليف600,000 ج.م صافي ربح",
        " |   ج.م إيرادات2,480,000",
        " :اإلجمالي3,080,000",
    ):
        run = paragraph.add_run(run_text)
        run.font.bold = True
        run.font.size = Pt(11)

    corrected = arabic_docx._correct_multi_run_single_line_paragraph_text(
        paragraph,
        ["الإجمالي: 3,080,000 ج.م إيرادات | 2,480,000 ج.م تكاليف | 600,000 ج.م صافي ربح"],
    )

    assert corrected is True
    assert paragraph.text == (
        "الإجمالي: 3,080,000 ج.م إيرادات | 2,480,000 ج.م تكاليف | 600,000 ج.م صافي ربح"
    )


def test_does_not_collapse_runs_with_different_styling():
    # A paragraph genuinely split across different visual styles must be
    # left alone rather than silently losing that distinction.
    document = Document()
    paragraph = document.add_paragraph()
    run1 = paragraph.add_run("مرحبا")
    run1.font.bold = True
    run2 = paragraph.add_run("بالعالم")
    run2.font.bold = False

    corrected = arabic_docx._correct_multi_run_single_line_paragraph_text(
        paragraph, ["بالعالم مرحبا"]
    )

    assert corrected is False
    assert paragraph.text == "مرحبابالعالم"


def test_correct_table_cell_texts_recovers_a_dropped_hamza_by_position():
    # Found live: pdf2docx's own table-cell extraction dropped the hamza
    # carrier outright ("الإيرادات" came out as "اليرادات", with the
    # trailing "(ج.م)" also misordered) — a different failure mode than
    # the body-paragraph bidi bug, since there's no hamza character left
    # anywhere in the garbled text to merely reorder. Correction can't key
    # off a matching character signature here (the two texts legitimately
    # disagree on signature), so it matches purely by row/column position,
    # like apply_table_layouts does for shading and widths.
    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "المنطقة"
    table.cell(0, 1).text = ")اليرادات (ج.م"

    arabic_docx._correct_table_cell_texts(
        document,
        [[["المنطقة", "الإيرادات (ج.م)"]]],
    )

    assert table.cell(0, 0).text == "المنطقة"
    assert table.cell(0, 1).text == "الإيرادات (ج.م)"


def test_correct_table_cell_texts_skips_mismatched_row_or_column_counts():
    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "المنطقة"
    table.cell(0, 1).text = ")اليرادات (ج.م"

    # Only one row's worth of ground truth for a 1x2 table is fine, but a
    # mismatched column count for that row must be skipped rather than
    # partially applied.
    arabic_docx._correct_table_cell_texts(document, [[["الإيرادات (ج.م)"]]])

    assert table.cell(0, 0).text == "المنطقة"
    assert table.cell(0, 1).text == ")اليرادات (ج.م"


def test_extract_table_cell_lines_fixes_header_from_the_real_pdf():
    pdf_path = os.path.join(
        os.path.dirname(__file__), "fixtures", "word_sales_report_real.pdf"
    )
    tables = arabic_docx.extract_table_cell_lines(pdf_path)
    assert len(tables) == 2
    for rows in tables:
        header = rows[0]
        assert "الإيرادات (ج.م)" in header
        assert "التكاليف (ج.م)" in header
        assert "صافي الربح (ج.م)" in header


def test_docai_table_keeps_logical_column_order():
    document = Document()
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "التحليل"
    table.cell(0, 1).text = "البيان المالي"

    # Simulate a stale/misapplied table flag, then ensure the Document AI
    # path removes it instead of mirroring already-logical columns twice.
    arabic_docx.mark_table_rtl(table)
    assert table._tbl.tblPr.find(qn("w:bidiVisual")) is not None

    arabic_docx.mark_table_rtl(table, mirror_columns=False)
    assert table._tbl.tblPr.find(qn("w:bidiVisual")) is None
    assert [cell.text for cell in table.rows[0].cells] == [
        "التحليل",
        "البيان المالي",
    ]
