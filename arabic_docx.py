"""Repair Arabic text direction after a pdf2docx conversion.

pdf2docx is deliberately kept as the layout engine because it preserves
tables and images well.  Some PDFs produced by browsers, however, store RTL
glyphs in logical order while positioning Arabic-Indic numbers right-to-left.
The resulting DOCX then contains reversed dates and amounts.  This module
uses the original glyph coordinates to restore those numeric tokens, applies
the OOXML RTL flags that Word expects, and recovers invoice total lines that
pdf2docx may discard as overlapping text.
"""
from __future__ import annotations

import os
import re
import unicodedata
import uuid
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass

import fitz
import pdfplumber
from bidi.algorithm import get_display
from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph


ARABIC_RE = re.compile(r"[\u0600-\u06ff]")
ARABIC_LETTER = r"[\u0621-\u064a]"
ARABIC_MARK = r"[\u064b-\u065f\u0670]"
ARABIC_NUMBER_RE = re.compile(
    r"(?<![\u0660-\u0669])"
    r"[\u0660-\u0669](?:[\u0660-\u0669\u066b\u066c/.,:%+-]*[\u0660-\u0669])?"
    r"(?![\u0660-\u0669])"
)
TOTAL_WORDS = ("الإجمالي", "الاجمالي", "الإجمالى", "المجموع", "المستحق")


def _dedupe_and_order_marks(match: re.Match[str]) -> str:
    """Return one canonically ordered copy of every combining mark."""
    marks = sorted(set(match.group(0)), key=unicodedata.combining)
    return "".join(marks)


def repair_arabic_combining_marks(text: str) -> str:
    """Repair common zero-width glyph artefacts emitted by PyMuPDF.

    Browser-generated PDFs can expose a combining mark immediately before
    its base letter.  A duplicated zero-width glyph may also appear beside
    the visible glyph.  The transformations below are intentionally narrow:
    normal Arabic words with correctly placed marks are left unchanged.
    """
    # A mark at the start of a word belongs to the following base letter.
    text = re.sub(
        rf"(^|[^\u0600-\u06ff])({ARABIC_MARK}+)({ARABIC_LETTER})",
        lambda match: match.group(1) + match.group(3) + match.group(2),
        text,
    )

    # In browser PDFs, marks placed on the right edge of the next glyph may
    # be reported after a hamza carrier.  Move them onto that following glyph.
    text = re.sub(
        rf"([ؤئءأإ])({ARABIC_MARK}+)({ARABIC_LETTER})",
        lambda match: match.group(1) + match.group(3) + match.group(2),
        text,
    )

    text = re.sub(rf"{ARABIC_MARK}+", _dedupe_and_order_marks, text)

    # Collapse the precise pattern produced when a visible marked glyph is
    # accompanied by a duplicate zero-width copy of the same glyph.
    text = re.sub(
        rf"({ARABIC_LETTER})({ARABIC_MARK}+)\1\1",
        lambda match: match.group(1) + match.group(2) + match.group(1),
        text,
    )

    # A narrow, confirmed-safe fix for a letter-pair transposition the
    # get_display() bidi round-trip (used throughout this module to
    # restore logical order) introduces around a lam-alef ligature with a
    # hamza carrier — e.g. "الإيرادات"/"الأرقام" come back as
    # "اإليرادات"/"األرقام" (ل and the hamza-carrier swapped). The
    # sequence "ا" + hamza-carrier + "ل" essentially never occurs
    # correctly in genuine Arabic text, so this is safe to always apply.
    text = re.sub(r"ا([إأآ])ل", r"ال\1", text)

    return unicodedata.normalize("NFC", text)


def extract_numeric_replacements(pdf_path: str) -> dict[str, str]:
    """Map stored Arabic-number tokens to their visual left-to-right value.

    This is safer than reversing every number: PDFs that already store a
    token correctly simply map it to itself.
    """
    replacements: dict[str, str] = {}
    with fitz.open(pdf_path) as document:
        for page in document:
            for block in page.get_text("rawdict").get("blocks", []):
                for line in block.get("lines", []):
                    chars = [
                        char
                        for span in line.get("spans", [])
                        for char in span.get("chars", [])
                    ]
                    stored = "".join(char.get("c", "") for char in chars)
                    for match in ARABIC_NUMBER_RE.finditer(stored):
                        token_chars = chars[match.start() : match.end()]
                        visual = "".join(
                            char.get("c", "")
                            for char in sorted(
                                token_chars,
                                key=lambda char: (
                                    float(char.get("bbox", (0, 0, 0, 0))[0]),
                                    float(char.get("bbox", (0, 0, 0, 0))[1]),
                                ),
                            )
                        )
                        replacements[match.group(0)] = visual
    return replacements


_OPEN_PAREN_SPACE_RE = re.compile(r"(\()\s+")
_CLOSE_PAREN_SPACE_RE = re.compile(r"\s+(\))")


def _fix_paren_spacing(text: str) -> str:
    """Removes a stray space right after "(" or right before ")" — an
    artifact of pdf2docx/bidi's own handling of mirrored bracket
    characters, not something a real document intentionally does.
    """
    text = _OPEN_PAREN_SPACE_RE.sub(r"\1", text)
    text = _CLOSE_PAREN_SPACE_RE.sub(r"\1", text)
    return text


def repair_extracted_text(text: str, numeric_replacements: dict[str, str]) -> str:
    """Repair a DOCX text run without disturbing Latin text or styling."""
    text = ARABIC_NUMBER_RE.sub(
        lambda match: numeric_replacements.get(match.group(0), match.group(0)),
        text,
    )
    return _fix_paren_spacing(repair_arabic_combining_marks(text))


def extract_logical_lines(pdf_path: str) -> list[str]:
    """Return source lines in logical reading order."""
    lines: list[str] = []
    with pdfplumber.open(pdf_path) as document:
        for page in document.pages:
            for visual_line in (page.extract_text() or "").splitlines():
                logical = repair_arabic_combining_marks(
                    get_display(visual_line, base_dir="L")
                ).strip()
                if logical:
                    lines.append(logical)
    return lines


def extract_missing_total_lines(pdf_path: str) -> list[str]:
    """Extract logical Arabic invoice totals from the source PDF.

    pdfplumber exposes the physical left-to-right line; applying the Unicode
    bidirectional algorithm reconstructs its logical reading order.  Only
    numeric total/amount-summary lines are considered, which avoids adding
    ordinary page text that pdf2docx intentionally grouped elsewhere.
    """
    totals: list[str] = []
    for logical in extract_logical_lines(pdf_path):
        if (
            ARABIC_NUMBER_RE.search(logical)
            and any(word in logical for word in TOTAL_WORDS)
            and logical not in totals
        ):
            totals.append(logical)
    return totals


def _iter_cell_paragraphs(cell: _Cell) -> Iterator[Paragraph]:
    for paragraph in cell.paragraphs:
        yield paragraph
    for table in cell.tables:
        yield from _iter_table_paragraphs(table)


def _iter_table_paragraphs(table: Table) -> Iterator[Paragraph]:
    seen_cells: set[object] = set()
    for row in table.rows:
        for cell in row.cells:
            # Keep the XML element itself.  ``cell._tc`` proxies are short
            # lived, so storing only ``id(cell._tc)`` lets Python reuse an id
            # and can incorrectly skip a different cell later in the table.
            cell_element = cell._tc
            if cell_element in seen_cells:
                continue
            seen_cells.add(cell_element)
            yield from _iter_cell_paragraphs(cell)


def _iter_all_paragraphs(document) -> Iterator[Paragraph]:
    yield from document.paragraphs
    for table in document.tables:
        yield from _iter_table_paragraphs(table)
    for section in document.sections:
        yield from section.header.paragraphs
        yield from section.footer.paragraphs


def _ensure_child(parent, tag: str):
    child = parent.find(qn(tag))
    if child is None:
        child = OxmlElement(tag)
        parent.append(child)
    return child


def _mark_paragraph_rtl(
    paragraph: Paragraph,
    *,
    force: bool = False,
    align_to_start: bool = False,
) -> None:
    """Write Word's RTL properties directly on a paragraph.

    ``align_to_start`` intentionally emits ``w:jc w:val=\"left\"`` together
    with ``w:bidi``.  That is the OOXML combination used by the iLovePDF
    reference: Word and LibreOffice interpret ``left`` as the logical start
    edge once bidi is active, so the paragraph is placed on the physical
    right.  A style-only setting is not enough for every renderer, therefore
    Document AI output also calls this helper with ``force=True``.
    """
    if not force and not ARABIC_RE.search(paragraph.text):
        return

    paragraph.alignment = (
        WD_ALIGN_PARAGRAPH.LEFT if align_to_start else WD_ALIGN_PARAGRAPH.RIGHT
    )
    paragraph_properties = paragraph._p.get_or_add_pPr()
    bidi = _ensure_child(paragraph_properties, "w:bidi")
    bidi.set(qn("w:val"), "1")

    for run in paragraph.runs:
        # Keep pure Latin/numeric fragments LTR inside the RTL paragraph.
        if not ARABIC_RE.search(run.text):
            continue
        run_properties = run._r.get_or_add_rPr()
        rtl = _ensure_child(run_properties, "w:rtl")
        rtl.set(qn("w:val"), "1")
        language = _ensure_child(run_properties, "w:lang")
        language.set(qn("w:bidi"), "ar-SA")


def _mark_table_rtl(table: Table, *, mirror_columns: bool = True) -> None:
    if any(ARABIC_RE.search(cell.text) for row in table.rows for cell in row.cells):
        bidi_visual = table._tbl.tblPr.find(qn("w:bidiVisual"))
        if mirror_columns:
            _ensure_child(table._tbl.tblPr, "w:bidiVisual")
        elif bidi_visual is not None:
            # Document AI already returns cells in the intended logical
            # order.  Mirroring that order again swaps the Arabic report's
            # first and last columns compared with the iLovePDF reference.
            table._tbl.tblPr.remove(bidi_visual)
    for row in table.rows:
        for cell in row.cells:
            for nested in cell.tables:
                _mark_table_rtl(nested, mirror_columns=mirror_columns)


def _normalized_for_compare(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def _character_signature(text: str) -> Counter[str]:
    return Counter(_normalized_for_compare(text))


def _group_runs_by_target_texts(
    paragraph: Paragraph,
    target_texts: list[str],
) -> list[list] | None:
    """Splits a paragraph's runs into consecutive groups, one per entry in
    ``target_texts``, each group's combined character signature matching
    that entry's. Returns None if the runs can't be partitioned this way
    (every run must be consumed, in order, with an exact signature match
    at each boundary). Shared by the text-realignment pass and the
    PDF-style-transfer pass below — both need to know which runs
    correspond to which original source line.
    """
    content_runs = [run for run in paragraph.runs if _normalized_for_compare(run.text)]
    groups: list[list] = []
    cursor = 0
    for target_text in target_texts:
        target_signature = _character_signature(target_text)
        current_signature: Counter[str] = Counter()
        group = []
        while cursor < len(content_runs):
            run = content_runs[cursor]
            cursor += 1
            group.append(run)
            current_signature.update(_character_signature(run.text))
            if current_signature == target_signature:
                break
        if current_signature != target_signature:
            return None
        groups.append(group)

    if cursor != len(content_runs):
        return None
    return groups


def _align_paragraph_to_source_lines(
    paragraph: Paragraph,
    logical_lines: list[str],
) -> bool:
    """Restore line boundaries and logical order without flattening styles.

    pdf2docx sometimes concatenates adjacent source lines and may keep two
    RTL spans in physical rather than logical order.  We match by character
    multiset, then place each source line in the first run of its matching
    group.  Requiring at least two matched lines keeps this conservative and
    prevents ordinary one-line paragraphs from being rewritten.
    """
    paragraph_signature = _character_signature(paragraph.text)
    if not paragraph_signature:
        return False

    matched_lines: list[str] | None = None
    for start in range(len(logical_lines)):
        combined = Counter()
        for end in range(start, min(len(logical_lines), start + 8)):
            combined.update(_character_signature(logical_lines[end]))
            if end > start and combined == paragraph_signature:
                matched_lines = logical_lines[start : end + 1]
                break
        if matched_lines is not None:
            break

    if matched_lines is None:
        return False

    groups = _group_runs_by_target_texts(paragraph, matched_lines)
    if groups is None:
        return False

    for run in paragraph.runs:
        run.text = ""
    for index, (source_line, group) in enumerate(zip(matched_lines, groups)):
        is_last = index == len(groups) - 1
        # A real w:br (not a literal "\n" stuffed into w:t, which several
        # XML-text readers — including the one used to compare this tool's
        # own output against a reference — silently drop) is what actually
        # renders as a line break in Word. The line itself still needs an
        # ordinary space character in its own text too, since a line that
        # wrapped mid-sentence (e.g. "...جميع" / "الأرقام...") is two source
        # lines of ONE flowing sentence, not a deliberate break, and must
        # read as one sentence for any reader that only sees w:t content.
        # Skipped when the line already ends in a hyphen/dash, where the
        # words are meant to run together with no space at all.
        ends_with_hyphen = not is_last and source_line.rstrip()[-1:] in ("-", "–", "—", "ـ")
        group[0].text = source_line if (is_last or ends_with_hyphen) else source_line + " "
        if not is_last:
            group[0].add_break()
    return True


def _replace_paragraph_text(paragraph: Paragraph, text: str) -> None:
    if paragraph.runs:
        paragraph.runs[0].text = text
        for run in paragraph.runs[1:]:
            run.text = ""
    else:
        paragraph.add_run(text)


def _restore_missing_totals(document, totals: list[str]) -> None:
    paragraphs = list(_iter_all_paragraphs(document))
    document_text = "\n".join(paragraph.text for paragraph in paragraphs)
    normalized_document = _normalized_for_compare(document_text)

    for total in totals:
        normalized_total = _normalized_for_compare(total)
        if normalized_total and normalized_total in normalized_document:
            continue

        # pdf2docx commonly retains only the trailing currency word when the
        # numeric total overlaps the table boundary.  Reuse that paragraph so
        # the recovered total stays in the correct place instead of moving to
        # the end of the document.
        replacement = None
        for paragraph in paragraphs:
            normalized_paragraph = _normalized_for_compare(paragraph.text)
            if (
                normalized_paragraph
                and len(normalized_paragraph) <= 12
                and normalized_total.endswith(normalized_paragraph)
            ):
                replacement = paragraph
                break

        if replacement is None:
            replacement = document.add_paragraph()
            paragraphs.append(replacement)

        _replace_paragraph_text(replacement, total)
        _mark_paragraph_rtl(replacement)
        normalized_document += normalized_total


def mark_paragraph_rtl(
    paragraph: Paragraph,
    *,
    force: bool = False,
    align_to_start: bool = False,
) -> None:
    """Public wrapper around ``_mark_paragraph_rtl`` for other conversion
    providers (e.g. docai.py) that build a docx paragraph from scratch and
    need the same RTL alignment/bidi flags applied."""
    _mark_paragraph_rtl(
        paragraph,
        force=force,
        align_to_start=align_to_start,
    )


def mark_table_rtl(table: Table, *, mirror_columns: bool = True) -> None:
    """Public wrapper around ``_mark_table_rtl`` — see ``mark_paragraph_rtl``."""
    _mark_table_rtl(table, mirror_columns=mirror_columns)


def ensure_rtl_paragraph_style(
    document: Document,
    style_id: str,
    font_name: str = "Arial",
    spacing_before_twips: int | None = None,
):
    """Get-or-create a paragraph style with RTL start alignment and bidi baked
    into the style definition itself, rather than decided per-paragraph
    from that paragraph's own text (which is what ``mark_paragraph_rtl``
    does, correctly, for the self-hosted repair path where a document can
    contain a real mix of Arabic and Latin paragraphs).

    Some documents are RTL by construction end to end — every paragraph
    in them should render right-aligned, including one whose entire text
    is a number or a percentage with no Arabic characters of its own,
    because it's a cell in an Arabic-oriented table, not because that
    specific string happens to contain Arabic letters. Baking the
    alignment into the style (as other Word-generation tools do) gets
    this right without a per-paragraph content check.

    Idempotent — a repeat call with the same style_id on the same
    document returns the existing style instead of erroring.
    """
    styles = document.styles
    try:
        return styles[style_id]
    except KeyError:
        pass

    style = styles.add_style(style_id, WD_STYLE_TYPE.PARAGRAPH)
    style.base_style = styles["Normal"]
    # The iLovePDF reference uses jc="left" + bidi="1".  In an RTL
    # paragraph this is the logical start edge, rendered on the right.
    style.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.LEFT
    style.font.name = font_name

    style_pPr = style.element.get_or_add_pPr()
    style_bidi = _ensure_child(style_pPr, "w:bidi")
    style_bidi.set(qn("w:val"), "1")
    if spacing_before_twips is not None:
        spacing = _ensure_child(style_pPr, "w:spacing")
        spacing.set(qn("w:before"), str(spacing_before_twips))

    style_rPr = style.element.get_or_add_rPr()
    fonts = _ensure_child(style_rPr, "w:rFonts")
    fonts.set(qn("w:ascii"), font_name)
    fonts.set(qn("w:hAnsi"), font_name)
    fonts.set(qn("w:cs"), font_name)
    language = _ensure_child(style_rPr, "w:lang")
    language.set(qn("w:bidi"), "ar-SA")

    return style


# ---------------------------------------------------------------------------
# Formatting transfer: font size/bold/color, cell shading, heading styles,
# and column widths, read directly from the source PDF
# ---------------------------------------------------------------------------
# pdf2docx captures some of this (cell fill color, sometimes) but not font
# size or bold for Arabic runs at all — confirmed by converting a fully
# styled source document to PDF and inspecting the DOCX pdf2docx produced:
# every run came back at the Normal style's base size, regardless of its
# size in the source. This instead reads the PDF's own text spans and
# filled rectangles and reapplies them, matched to the (already text-
# repaired) DOCX by content, independent of whatever pdf2docx preserved.
#
# Word (and LibreOffice) render Arabic text using the *complex-script*
# run properties (w:szCs, w:bCs), not the plain ones (w:sz, w:b) that
# python-docx's own run.font.size/bold setters write — confirmed live: a
# run with only font.size set rendered at the paragraph style's size, not
# the one just set, because the run's actual (Arabic) text is drawn using
# its unset w:szCs instead. Every size/bold change below sets both.

_BOLD_FLAG = 1 << 4
DEFAULT_FONT_NAME = "Arial"
_HEADING_MAX_WORDS = 8
_PRINTABLE_WIDTH_INCHES = 6.5


@dataclass
class StyledLine:
    text: str
    size_pt: float
    bold: bool
    color_hex: str


@dataclass
class TableLayout:
    column_widths_pt: list[float]
    cell_fills: list[list[str | None]]


def _span_color_hex(color_int: int) -> str:
    return f"{color_int:06X}"


def extract_styled_lines(pdf_path: str) -> list[StyledLine]:
    """One entry per visual PDF line, in reading order, carrying its
    dominant span's size/bold/color. "Dominant" = the longest span on
    that line, so a lone differently-styled leading tab or trailing
    punctuation span doesn't skew the line's represented style.
    """
    lines: list[StyledLine] = []
    with fitz.open(pdf_path) as document:
        for page in document:
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    spans = [s for s in line.get("spans", []) if (s.get("text") or "").strip()]
                    if not spans:
                        continue
                    raw_text = "".join(s.get("text", "") for s in spans)
                    fixed_text = repair_arabic_combining_marks(
                        get_display(raw_text, base_dir="L")
                    ).strip()
                    if not fixed_text:
                        continue
                    dominant = max(spans, key=lambda s: len(s.get("text", "")))
                    lines.append(
                        StyledLine(
                            text=fixed_text,
                            size_pt=round(float(dominant.get("size", 0)), 1),
                            bold=bool(int(dominant.get("flags", 0)) & _BOLD_FLAG),
                            color_hex=_span_color_hex(int(dominant.get("color", 0))),
                        )
                    )
    return lines


def _best_matching_styled_lines(
    paragraph_text: str,
    styled_lines: list[StyledLine],
) -> list[StyledLine] | None:
    """Same sliding-window character-signature search as source-line
    realignment above, generalized to also accept a single-line exact
    match (needed here: most paragraphs/cells are one line, and that's
    exactly the common case formatting transfer most needs to get right).
    """
    paragraph_signature = _character_signature(paragraph_text)
    if not paragraph_signature:
        return None
    for start in range(len(styled_lines)):
        combined: Counter[str] = Counter()
        for end in range(start, min(len(styled_lines), start + 8)):
            combined.update(_character_signature(styled_lines[end].text))
            if combined == paragraph_signature:
                return styled_lines[start : end + 1]
    return None


def _apply_run_style(run, styled_line: StyledLine) -> None:
    run.font.size = Pt(styled_line.size_pt)
    run.font.bold = styled_line.bold
    try:
        run.font.color.rgb = RGBColor.from_string(styled_line.color_hex)
    except ValueError:
        pass

    rPr = run._r.get_or_add_rPr()
    sz_cs = _ensure_child(rPr, "w:szCs")
    sz_cs.set(qn("w:val"), str(int(round(styled_line.size_pt * 2))))
    existing_bold_cs = rPr.find(qn("w:bCs"))
    if styled_line.bold:
        if existing_bold_cs is None:
            rPr.append(OxmlElement("w:bCs"))
    elif existing_bold_cs is not None:
        rPr.remove(existing_bold_cs)


def _maybe_promote_to_heading(document, paragraph: Paragraph, styled_line: StyledLine, body_size: float) -> None:
    """Promotes a short, visually larger-or-bold single-line paragraph to
    Heading 1/2 — conservative on purpose: a normal paragraph that just
    happens to be short is never touched, since it won't be both short
    AND meaningfully larger/bolder than the body text.
    """
    if len(styled_line.text.split()) > _HEADING_MAX_WORDS:
        return
    if styled_line.size_pt >= body_size * 1.35:
        paragraph.style = document.styles["Heading 1"]
    elif styled_line.bold and styled_line.size_pt >= body_size * 1.1:
        paragraph.style = document.styles["Heading 2"]


def apply_pdf_styles(document, pdf_path: str) -> None:
    """Applies font size/bold/color from the source PDF onto every
    paragraph and table-cell paragraph in the DOCX, matched by text
    content, and promotes qualifying short paragraphs to Heading 1/2.
    """
    styled_lines = extract_styled_lines(pdf_path)
    if not styled_lines:
        return
    body_size = Counter(line.size_pt for line in styled_lines).most_common(1)[0][0]

    def style_one(paragraph: Paragraph, allow_heading: bool) -> None:
        if not paragraph.runs:
            return
        matched = _best_matching_styled_lines(paragraph.text, styled_lines)
        if matched is None:
            return
        groups = _group_runs_by_target_texts(paragraph, [line.text for line in matched])
        if groups is None:
            return
        for styled_line, group in zip(matched, groups):
            for run in group:
                _apply_run_style(run, styled_line)
        if allow_heading and len(matched) == 1:
            _maybe_promote_to_heading(document, paragraph, matched[0], body_size)

    for paragraph in document.paragraphs:
        style_one(paragraph, allow_heading=True)
    for table in document.tables:
        for paragraph in _iter_table_paragraphs(table):
            style_one(paragraph, allow_heading=False)


def _rect_fill_hex_at(rects, cx: float, cy: float) -> str | None:
    for rect in rects:
        if rect["x0"] <= cx <= rect["x1"] and rect["top"] <= cy <= rect["bottom"]:
            color = rect.get("non_stroking_color")
            if color and len(color) >= 3:
                return "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in color[:3])
    return None


def extract_table_layouts(pdf_path: str) -> list[TableLayout]:
    """One TableLayout per PDF table (reading order across all pages),
    with each cell's fill color — read from the filled rectangle drawn
    behind it, if any — and each column's width, from the same cell
    bounding boxes pdfplumber already uses to detect the table.
    """
    layouts: list[TableLayout] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            rects = [r for r in page.rects if r.get("fill")]
            for table in page.find_tables():
                rows = table.rows
                if not rows or not rows[0].cells or any(c is None for c in rows[0].cells):
                    continue
                column_widths = [cell[2] - cell[0] for cell in rows[0].cells]
                fills: list[list[str | None]] = []
                for row in rows:
                    row_fills: list[str | None] = []
                    for cell in row.cells:
                        if cell is None:
                            row_fills.append(None)
                            continue
                        cx = (cell[0] + cell[2]) / 2
                        cy = (cell[1] + cell[3]) / 2
                        row_fills.append(_rect_fill_hex_at(rects, cx, cy))
                    fills.append(row_fills)
                layouts.append(TableLayout(column_widths_pt=column_widths, cell_fills=fills))
    return layouts


def _set_cell_shading(cell, color_hex: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    existing = tc_pr.find(qn("w:shd"))
    if existing is not None:
        tc_pr.remove(existing)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), color_hex)
    tc_pr.append(shd)


def apply_table_layouts(document, layouts: list[TableLayout]) -> None:
    """Applies per-cell shading and proportional column widths, matched to
    document.tables by position — skipping any table whose row/column
    count doesn't match what was detected in the PDF, which is safer than
    risking a shading/width applied to the wrong cells on a table
    pdf2docx happened to structure differently.
    """
    for table, layout in zip(document.tables, layouts):
        if len(table.rows) != len(layout.cell_fills):
            continue
        if not table.rows or len(table.rows[0].cells) != len(layout.column_widths_pt):
            continue

        table.autofit = False
        total_pt = sum(layout.column_widths_pt) or 1.0
        for col_idx, width_pt in enumerate(layout.column_widths_pt):
            width_inches = _PRINTABLE_WIDTH_INCHES * (width_pt / total_pt)
            # Both are needed: a cell's own w:tcW is what that cell uses,
            # but Word (and LibreOffice) lay out a w:tblLayout="fixed"
            # table from w:tblGrid's gridCol widths, which cell.width
            # alone does NOT update — confirmed live: setting only
            # cell.width per-cell left every column rendering at its old
            # equal width, because the stale tblGrid still said so.
            table.columns[col_idx].width = Inches(width_inches)
            for row in table.rows:
                row.cells[col_idx].width = Inches(width_inches)

        for r, row in enumerate(table.rows):
            for c, cell in enumerate(row.cells):
                fill = layout.cell_fills[r][c] if c < len(layout.cell_fills[r]) else None
                if fill:
                    _set_cell_shading(cell, fill)


def apply_default_font(document, font_name: str = DEFAULT_FONT_NAME) -> None:
    """Sets w:rFonts ascii/hAnsi/cs on every run to a known Arabic-capable
    font — pdf2docx leaves this unset, so the output otherwise depends on
    whatever font Word happens to default to on the machine opening it.
    """
    for paragraph in _iter_all_paragraphs(document):
        for run in paragraph.runs:
            rPr = run._r.get_or_add_rPr()
            fonts = _ensure_child(rPr, "w:rFonts")
            fonts.set(qn("w:ascii"), font_name)
            fonts.set(qn("w:hAnsi"), font_name)
            fonts.set(qn("w:cs"), font_name)


def repair_arabic_docx(pdf_path: str, docx_path: str) -> None:
    """Repair one converted DOCX in place using its source PDF."""
    numeric_replacements = extract_numeric_replacements(pdf_path)
    logical_lines = extract_logical_lines(pdf_path)
    totals = [
        line
        for line in logical_lines
        if ARABIC_NUMBER_RE.search(line) and any(word in line for word in TOTAL_WORDS)
    ]
    document = Document(docx_path)

    for paragraph in _iter_all_paragraphs(document):
        for run in paragraph.runs:
            run.text = repair_extracted_text(run.text, numeric_replacements)

    for paragraph in document.paragraphs:
        _align_paragraph_to_source_lines(paragraph, logical_lines)

    for paragraph in _iter_all_paragraphs(document):
        _mark_paragraph_rtl(paragraph)

    for table in document.tables:
        _mark_table_rtl(table)

    _restore_missing_totals(document, totals)

    # Best-effort: formatting transfer is an enhancement on top of the
    # text/RTL correctness this function exists to guarantee. A PDF that
    # can't be re-read by fitz/pdfplumber for some edge-case reason should
    # still come back as a correctly-repaired (if unstyled) document, not
    # a hard failure of the whole conversion.
    try:
        apply_pdf_styles(document, pdf_path)
        apply_table_layouts(document, extract_table_layouts(pdf_path))
    except Exception:
        pass
    apply_default_font(document)

    temporary_path = f"{docx_path}.{uuid.uuid4().hex}.tmp"
    try:
        document.save(temporary_path)
        os.replace(temporary_path, docx_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
