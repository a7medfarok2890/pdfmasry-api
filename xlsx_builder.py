"""Shared PDF→Excel table/text structuring and formatting.

Used by both the self-hosted pdfplumber provider (main.py) and the
Document AI provider (docai.py) so a user gets the same quality output
— real numbers, styled headers, one sheet per logical table, reassembled
paragraphs — no matter which extraction backend actually ran.

Design split:
  - Numeric cell classification (classify_numeric_cell) and worksheet
    styling (style_table, write_text_sheet) are pure/provider-agnostic.
  - extract_structured_content() is the pdfplumber-specific reader that
    turns a PDF into an ordered stream of TableBlock/paragraph content,
    using each table's bounding box to figure out reading order against
    surrounding text and to catch a table that continues onto the next
    page.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from arabic_docx import ARABIC_RE

# ---------------------------------------------------------------------------
# Numeric cell classification
# ---------------------------------------------------------------------------
# Conservative by design: a value only converts to a real number when its
# formatting unambiguously signals "this is a number for humans to read"
# (thousands separators, a percent sign, parenthesized-negative accounting
# style, or a plain decimal). A bare run of 6+ digits with no separator, or
# anything with a leading zero beyond a single "0", reads exactly like a
# phone number or an internal code, so it is left as text. Anything
# containing a letter (invoice codes like "INV-1001", unit labels) is
# rejected immediately and never reaches the digit patterns below.

_LETTER_RE = re.compile(r"[A-Za-z؀-ۿ]")
_PERCENT_RE = re.compile(r"^([+-])?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*%$")
_PAREN_RE = re.compile(r"^\(\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*\)$")
_THOUSANDS_RE = re.compile(r"^([+-])?(\d{1,3}(?:,\d{3})+(?:\.\d+)?)$")
_DECIMAL_RE = re.compile(r"^([+-])?(\d{1,6}\.\d+)$")
_SMALL_INT_RE = re.compile(r"^([+-])?([1-9]\d{0,4}|0)$")


def classify_numeric_cell(raw: str | None) -> tuple[int | float, str] | None:
    """Returns (value, excel_number_format) if `raw` is unambiguously a
    formatted number, else None (caller keeps the original string as-is).
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    if _LETTER_RE.search(s):
        return None

    m = _PERCENT_RE.match(s)
    if m:
        sign, num = m.groups()
        value = float(num.replace(",", "")) / 100.0
        if sign == "-":
            value = -value
        return value, "0.0%"

    m = _PAREN_RE.match(s)
    if m:
        num = m.group(1)
        is_decimal = "." in num
        raw_value = float(num.replace(",", "")) if is_decimal else int(num.replace(",", ""))
        value = -raw_value
        fmt = "#,##0.00;(#,##0.00)" if is_decimal else "#,##0;(#,##0)"
        return value, fmt

    m = _THOUSANDS_RE.match(s)
    if m:
        sign, num = m.groups()
        is_decimal = "." in num
        value = float(num.replace(",", "")) if is_decimal else int(num.replace(",", ""))
        if sign == "-":
            value = -value
        fmt = "#,##0.00" if is_decimal else "#,##0"
        return value, fmt

    m = _DECIMAL_RE.match(s)
    if m:
        sign, num = m.groups()
        value: float = float(num)
        if sign == "-":
            value = -value
        return value, "#,##0.00"

    m = _SMALL_INT_RE.match(s)
    if m:
        sign, num = m.groups()
        value = int(num)
        if sign == "-":
            value = -value
        return value, "#,##0"

    return None


def is_arabic_heavy(text: str) -> bool:
    """True if Arabic letters make up a meaningful share of the text —
    decides sheet-name language and sheet RTL layout for the whole file."""
    if not text:
        return False
    arabic_count = len(ARABIC_RE.findall(text))
    return arabic_count >= 10 or (len(text) > 0 and arabic_count / max(len(text), 1) > 0.15)


# ---------------------------------------------------------------------------
# Structured content model
# ---------------------------------------------------------------------------

@dataclass
class TableBlock:
    rows: list[list[str]]
    # Every paragraph that immediately preceded the table in the source
    # (its title plus any intro text) — the user's own report: "the title
    # and the paragraph before the table weren't carried into the file".
    # Each becomes its own merged row above the header; the first is styled
    # as the title.
    preceding_paragraphs: list[str] = field(default_factory=list)


@dataclass
class DocumentContent:
    tables: list[TableBlock] = field(default_factory=list)
    text_blocks: list[str] = field(default_factory=list)  # paragraphs with no table of their own


# ---------------------------------------------------------------------------
# Worksheet styling (shared by every provider)
# ---------------------------------------------------------------------------

HEADER_FILL_COLOR = "0F4C81"
BAND_FILL_COLOR = "F1F6FA"
MIN_COL_WIDTH = 10
MAX_COL_WIDTH = 50
WRAP_THRESHOLD = 25
ZEBRA_MIN_ROWS = 8


def _column_letter(idx: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(idx)


def write_table_block(ws, block: TableBlock, *, rtl: bool) -> None:
    """Writes one TableBlock (optional title + header row + data rows) onto
    a fresh worksheet with number formatting, header styling, borders,
    zebra banding, auto column width, wrapped long cells, and frozen header.
    """
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    rows = block.rows
    num_cols = max((len(r) for r in rows), default=1)
    current_row = 1

    for i, paragraph in enumerate(block.preceding_paragraphs):
        ws.cell(row=current_row, column=1, value=paragraph)
        para_cell = ws.cell(row=current_row, column=1)
        # The first preceding paragraph is treated as the table's title;
        # any further ones are its intro text, in regular weight.
        para_cell.font = Font(bold=True, size=12) if i == 0 else Font(size=11)
        para_cell.alignment = Alignment(horizontal="right" if rtl else "left", wrap_text=True)
        if num_cols > 1:
            ws.merge_cells(start_row=current_row, start_column=1, end_row=current_row, end_column=num_cols)
        current_row += 1

    header_row_idx = current_row
    header_fill = PatternFill(start_color=HEADER_FILL_COLOR, end_color=HEADER_FILL_COLOR, fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="B9C4CE")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    band_fill = PatternFill(start_color=BAND_FILL_COLOR, end_color=BAND_FILL_COLOR, fill_type="solid")

    col_max_len = [0] * num_cols

    for r_offset, row in enumerate(rows):
        excel_row = current_row + r_offset
        is_header = r_offset == 0
        is_banded = (not is_header) and ZEBRA_MIN_ROWS <= len(rows) and (r_offset % 2 == 0)
        for c_idx in range(num_cols):
            raw = row[c_idx] if c_idx < len(row) else ""
            raw = raw if raw is not None else ""
            cell = ws.cell(row=excel_row, column=c_idx + 1)
            classified = None if is_header else classify_numeric_cell(raw)
            if classified is not None:
                value, number_format = classified
                cell.value = value
                cell.number_format = number_format
                display_len = len(raw)
            else:
                cell.value = raw
                display_len = len(raw)
            col_max_len[c_idx] = max(col_max_len[c_idx], display_len)

            cell.border = border
            if is_header:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            else:
                if is_banded:
                    cell.fill = band_fill
                wrap = display_len > WRAP_THRESHOLD
                cell.alignment = Alignment(
                    horizontal="right" if rtl else "left",
                    vertical="center",
                    wrap_text=wrap,
                )

    for c_idx in range(num_cols):
        width = max(MIN_COL_WIDTH, min(MAX_COL_WIDTH, col_max_len[c_idx] + 2))
        ws.column_dimensions[_column_letter(c_idx + 1)].width = width

    ws.freeze_panes = ws.cell(row=header_row_idx + 1, column=1).coordinate
    ws.sheet_view.rightToLeft = rtl


def write_text_sheet(ws, paragraphs: list[str], *, rtl: bool) -> None:
    from openpyxl.styles import Alignment

    ws.column_dimensions["A"].width = MAX_COL_WIDTH
    for i, para in enumerate(paragraphs, start=1):
        cell = ws.cell(row=i, column=1, value=para)
        cell.alignment = Alignment(horizontal="right" if rtl else "left", vertical="top", wrap_text=True)
    ws.sheet_view.rightToLeft = rtl


def _unique_sheet_title(wb, base: str) -> str:
    base = base[:31]
    if base not in wb.sheetnames:
        return base
    n = 2
    while True:
        candidate = f"{base[:28]} {n}"[:31]
        if candidate not in wb.sheetnames:
            return candidate
        n += 1


def build_workbook(content: DocumentContent):
    """Builds the final Workbook: one sheet per table (titled "جدول N" /
    "Table N"), plus one "نص" / "Text" sheet for any leftover paragraphs.
    Language (and therefore RTL layout) is decided from all extracted text.
    """
    from openpyxl import Workbook

    full_text = "\n".join(
        [" ".join(cell or "" for row in t.rows for cell in row) for t in content.tables]
        + [" ".join(t.preceding_paragraphs) for t in content.tables]
        + content.text_blocks
    )
    rtl = is_arabic_heavy(full_text)
    table_label = "جدول" if rtl else "Table"
    text_label = "نص" if rtl else "Text"

    wb = Workbook()
    wb.remove(wb.active)

    for i, block in enumerate(content.tables, start=1):
        ws = wb.create_sheet(title=_unique_sheet_title(wb, f"{table_label} {i}"))
        write_table_block(ws, block, rtl=rtl)

    if content.text_blocks:
        ws = wb.create_sheet(title=_unique_sheet_title(wb, text_label))
        write_text_sheet(ws, content.text_blocks, rtl=rtl)

    if not wb.sheetnames:
        wb.create_sheet(title="Empty" if not rtl else "فارغ")

    return wb


# ---------------------------------------------------------------------------
# pdfplumber-based extraction (self-hosted provider)
# ---------------------------------------------------------------------------
# pdfplumber (like most naive PDF text extractors) reads glyphs in physical
# left-to-right position, which for Arabic is the REVERSE of logical
# reading order — both word order and the letters within each word. This
# project already has a tested fix for that at the whole-line level
# (arabic_docx.extract_logical_lines, via a double bidi pass); the same
# technique is reused here down at the table-cell level.

_LAM_ALEF_HAMZA_RE = re.compile(r"ا([إأآ])ل")
_SENTENCE_END_RE = re.compile(r"[.!؟:،]\s*$")
_HEADING_MAX_WORDS = 6


def _fix_rtl_text(raw: str | None) -> str:
    """Restores logical reading order for one cell/line of pdfplumber
    output. Non-Arabic text (numbers, English, invoice codes) is returned
    untouched. A cell that wrapped onto multiple lines inside a narrow PDF
    table column (e.g. a phone number broken across two lines) is rejoined
    with no separator if the result is all digits, else with a space.
    """
    if not raw:
        return raw or ""
    if "\n" in raw:
        parts = [p for p in raw.split("\n") if p != ""]
        tight = "".join(parts)
        raw = tight if tight.isdigit() else " ".join(parts)
    if not ARABIC_RE.search(raw):
        return raw

    from bidi.algorithm import get_display

    from arabic_docx import repair_arabic_combining_marks

    fixed = repair_arabic_combining_marks(get_display(raw, base_dir="L"))
    # Narrow, targeted fix for a consistent lam-alef/hamza letter swap this
    # bidi round-trip introduces (e.g. "الإيرادات" -> "اإليرادات",
    # "الإجمالي" -> "اإلجمالي"). Confirmed via live extraction testing;
    # the sequence "ا"+hamza-carrier+"ل" essentially never occurs correctly
    # in genuine Arabic text, so this is safe to always apply.
    return _LAM_ALEF_HAMZA_RE.sub(lambda m: "ال" + m.group(1), fixed)


def _group_into_paragraphs(lines: list[str]) -> list[str]:
    """Joins wrapped PDF lines back into whole paragraphs instead of one
    row per visual line. A short line with no sentence-ending punctuation
    is treated as its own heading rather than merged into the surrounding
    paragraph, which is what keeps a table's own heading distinct from the
    narrative paragraph before it.

    Known limitation: two consecutive full body paragraphs with no blank
    line and no short heading between them in the source are merged into
    one cell, since pdfplumber gives no other boundary signal to split on.
    """
    paragraphs: list[str] = []
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            paragraphs.append(" ".join(buffer))
            buffer.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        words = stripped.split()
        is_heading_like = len(words) <= _HEADING_MAX_WORDS and not _SENTENCE_END_RE.search(stripped)
        if is_heading_like:
            flush()
            paragraphs.append(stripped)
        else:
            buffer.append(stripped)
    flush()
    return paragraphs


@dataclass
class _RawTable:
    rows: list[list[str]]
    is_first_on_page: bool
    is_last_on_page: bool


def _extract_page_segments(page) -> list[tuple[str, list[str] | _RawTable]]:
    """Returns one page's content in reading order as a list of
    ("text", list[str]) / ("table", _RawTable) tuples, using each table's
    bounding box to crop out the paragraph text before/between/after it.
    """
    tables = sorted(page.find_tables(), key=lambda t: t.bbox[1])
    segments: list[tuple[str, list[str] | _RawTable]] = []

    def crop_paragraphs(top: float, bottom: float) -> list[str]:
        top = max(0.0, top)
        bottom = min(page.height, bottom)
        if bottom <= top:
            return []
        cropped = page.within_bbox((0, top, page.width, bottom))
        text = cropped.extract_text() or ""
        lines = [_fix_rtl_text(line) for line in text.splitlines()]
        return _group_into_paragraphs(lines)

    cursor = 0.0
    for i, table in enumerate(tables):
        top = table.bbox[1]
        paragraphs = crop_paragraphs(cursor, top)
        if paragraphs:
            segments.append(("text", paragraphs))
        raw_rows = [[_fix_rtl_text(cell) for cell in row] for row in table.extract()]
        segments.append((
            "table",
            _RawTable(rows=raw_rows, is_first_on_page=(not paragraphs and i == 0), is_last_on_page=False),
        ))
        cursor = table.bbox[3]

    trailing = crop_paragraphs(cursor, page.height)
    if trailing:
        segments.append(("text", trailing))
    elif segments and segments[-1][0] == "table":
        segments[-1][1].is_last_on_page = True  # type: ignore[union-attr]

    # A table immediately followed by another table (no text segment
    # between them) also counts as "last on page" for the first one.
    for i in range(len(segments) - 1):
        kind, item = segments[i]
        if kind == "table" and segments[i + 1][0] == "table":
            item.is_last_on_page = True  # type: ignore[union-attr]

    return segments


def _tables_are_continuation(prev: _RawTable, nxt: _RawTable) -> bool:
    if not prev.is_last_on_page or not nxt.is_first_on_page:
        return False
    if not prev.rows or not nxt.rows:
        return False
    return len(prev.rows[0]) == len(nxt.rows[0])


def extract_structured_content(pdf_path: str) -> DocumentContent:
    """Reads a PDF into an ordered DocumentContent: one TableBlock per
    logical table (continuation pages merged, repeated header row
    dropped), each carrying the paragraph(s) that immediately preceded it
    on the same page, plus a flat list of any other paragraphs in reading
    order for the "نص"/"Text" sheet.
    """
    import pdfplumber

    flat: list[tuple[int, str, list[str] | _RawTable]] = []
    with pdfplumber.open(pdf_path) as pdf:
        if not pdf.pages:
            return DocumentContent()
        for page_idx, page in enumerate(pdf.pages):
            for kind, data in _extract_page_segments(page):
                flat.append((page_idx, kind, data))

    tables: list[TableBlock] = []
    text_blocks: list[str] = []
    active: TableBlock | None = None
    active_raw: _RawTable | None = None

    def flush_active() -> None:
        nonlocal active, active_raw
        if active is not None:
            tables.append(active)
        active = None
        active_raw = None

    i = 0
    n = len(flat)
    while i < n:
        page_idx, kind, data = flat[i]

        if kind == "table":
            raw_table = data  # type: ignore[assignment]
            if active_raw is not None and _tables_are_continuation(active_raw, raw_table):
                body = raw_table.rows
                if body and active_raw.rows and body[0] == active_raw.rows[0]:
                    body = body[1:]
                active_raw.rows.extend(body)
                active.rows = active_raw.rows  # type: ignore[union-attr]
                active_raw.is_last_on_page = raw_table.is_last_on_page
            else:
                flush_active()
                active_raw = raw_table
                active = TableBlock(rows=raw_table.rows, preceding_paragraphs=[])
            i += 1
            continue

        # kind == "text"
        paragraphs = data  # type: ignore[assignment]
        next_is_table_same_page = i + 1 < n and flat[i + 1][1] == "table" and flat[i + 1][0] == page_idx
        if next_is_table_same_page:
            flush_active()  # a text segment always breaks any continuation
            next_raw_table = flat[i + 1][2]
            active_raw = next_raw_table  # type: ignore[assignment]
            active = TableBlock(rows=next_raw_table.rows, preceding_paragraphs=list(paragraphs))  # type: ignore[arg-type]
            i += 2
            continue

        flush_active()
        text_blocks.extend(paragraphs)  # type: ignore[arg-type]
        i += 1

    flush_active()
    return DocumentContent(tables=tables, text_blocks=text_blocks)

    return wb
