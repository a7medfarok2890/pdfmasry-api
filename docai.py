"""Google Cloud Document AI integration — optional quality upgrade for the
Arabic PDF→Word and PDF→Excel converters.

This module is fully opt-in and fails soft: every entry point raises
``DocAIUnavailable`` on *any* problem (missing/invalid credentials, network
error, quota exhausted, malformed response, unsupported page count, ...).
Callers (main.py) catch that and fall back to the existing self-hosted /
Adobe pipeline — a user must never see a Document AI-specific error, and
the tool must keep working exactly as before if the env vars below are
never set.

Required environment variables (all five must be set, or ``is_configured()``
returns False and callers skip this module entirely):

  GOOGLE_APPLICATION_CREDENTIALS_JSON  — the full service-account JSON as a
                                          string (NOT a file path). Parsed
                                          straight into memory since
                                          serverless/container filesystems
                                          aren't guaranteed persistent.
  GOOGLE_CLOUD_PROJECT_ID
  GOOGLE_CLOUD_LOCATION                — e.g. "us"
  GOOGLE_DOCAI_OCR_PROCESSOR_ID        — "Document OCR" processor, used by
                                          PDF→Word
  GOOGLE_DOCAI_FORM_PROCESSOR_ID       — "Form Parser" processor, used by
                                          PDF→Excel

Optional:
  GOOGLE_DOCAI_TIMEOUT_SECONDS         — per-request timeout (default 60)
"""
from __future__ import annotations

import json
import os
import threading
from typing import Optional

DOCAI_PROVIDER_VERSION = "docai-v1-2026-08"
DOCAI_TIMEOUT_SECONDS = int(os.environ.get("GOOGLE_DOCAI_TIMEOUT_SECONDS", "60"))

# Document AI's synchronous processDocument call is capped at 15 pages for
# these processor types. Larger files would need the async batch API (GCS
# input/output buckets), which isn't provisioned here — so we bail out to
# the fallback engine instead of trying and failing mid-request.
MAX_SYNC_PAGES = 15


class DocAIUnavailable(Exception):
    """Any condition that should trigger fallback to the existing engine."""


def _env(name: str) -> Optional[str]:
    val = os.environ.get(name)
    return val if val else None


def is_configured() -> bool:
    """True only if every required env var is present. Cheap — safe to call
    on every request; this is what lets the tools keep working unmodified
    when the credentials haven't been added yet."""
    return all([
        _env("GOOGLE_APPLICATION_CREDENTIALS_JSON"),
        _env("GOOGLE_CLOUD_PROJECT_ID"),
        _env("GOOGLE_CLOUD_LOCATION"),
        _env("GOOGLE_DOCAI_OCR_PROCESSOR_ID"),
        _env("GOOGLE_DOCAI_FORM_PROCESSOR_ID"),
    ])


_client_lock = threading.Lock()
_client_cache: dict[str, object] = {}


def _get_client():
    """Lazily build (and cache for the process lifetime) a
    DocumentProcessorServiceClient from the JSON in
    GOOGLE_APPLICATION_CREDENTIALS_JSON. Never touches disk."""
    with _client_lock:
        cached = _client_cache.get("client")
        if cached is not None:
            return cached

        try:
            from google.cloud import documentai
            from google.oauth2 import service_account
        except ImportError as exc:
            raise DocAIUnavailable(f"مكتبة Document AI غير مثبتة: {exc}")

        raw = _env("GOOGLE_APPLICATION_CREDENTIALS_JSON")
        try:
            info = json.loads(raw)
        except (json.JSONDecodeError, TypeError) as exc:
            raise DocAIUnavailable(f"GOOGLE_APPLICATION_CREDENTIALS_JSON غير صالح: {exc}")

        try:
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            location = _env("GOOGLE_CLOUD_LOCATION")
            client = documentai.DocumentProcessorServiceClient(
                credentials=credentials,
                client_options={"api_endpoint": f"{location}-documentai.googleapis.com"},
            )
        except Exception as exc:
            raise DocAIUnavailable(f"فشل تهيئة عميل Document AI: {exc}")

        _client_cache["client"] = client
        return client


def _processor_name(processor_id: str) -> str:
    project = _env("GOOGLE_CLOUD_PROJECT_ID")
    location = _env("GOOGLE_CLOUD_LOCATION")
    return f"projects/{project}/locations/{location}/processors/{processor_id}"


def _guard_page_count(pdf_path: str) -> None:
    try:
        import fitz
        with fitz.open(pdf_path) as doc:
            if doc.page_count > MAX_SYNC_PAGES:
                raise DocAIUnavailable(
                    f"الملف {doc.page_count} صفحة — يتجاوز حد المعالجة الفورية "
                    f"لـ Document AI ({MAX_SYNC_PAGES} صفحة)"
                )
    except DocAIUnavailable:
        raise
    except Exception:
        # Can't even open it to count pages — let the real API call fail
        # naturally below and surface as DocAIUnavailable there.
        pass


def _process_document(pdf_path: str, processor_id: str):
    from google.api_core import exceptions as google_exceptions
    from google.cloud import documentai

    client = _get_client()
    name = _processor_name(processor_id)

    with open(pdf_path, "rb") as f:
        content = f.read()

    request = documentai.ProcessRequest(
        name=name,
        raw_document=documentai.RawDocument(content=content, mime_type="application/pdf"),
    )

    try:
        result = client.process_document(request=request, timeout=DOCAI_TIMEOUT_SECONDS)
    except google_exceptions.ResourceExhausted as exc:
        raise DocAIUnavailable(f"تجاوز الحصة المجانية لـ Document AI: {exc}")
    except google_exceptions.GoogleAPICallError as exc:
        raise DocAIUnavailable(f"خطأ Document AI: {exc}")
    except Exception as exc:
        raise DocAIUnavailable(f"فشل استدعاء Document AI: {exc}")

    return result.document


def _layout_text(document_text: str, layout) -> str:
    """Resolve a Document AI Layout's text_anchor into the substring of the
    document's full text it refers to. Document AI already returns this in
    correct logical reading order (including for Arabic), which is the
    whole reason this module exists — no bidi post-processing needed here,
    unlike the self-hosted pdf2docx/pdfplumber path."""
    if layout is None or not layout.text_anchor.text_segments:
        return ""
    parts = []
    for seg in layout.text_anchor.text_segments:
        start = int(seg.start_index) if seg.start_index else 0
        end = int(seg.end_index)
        parts.append(document_text[start:end])
    return "".join(parts).strip()


def _text_anchor_range(layout) -> tuple[int, int]:
    """Return the (min_start, max_end) character offsets a Layout's
    text_anchor covers in the document's full text. Used to detect overlap
    between a paragraph and a table so the same text isn't emitted twice."""
    if layout is None or not layout.text_anchor.text_segments:
        return (0, 0)
    starts = []
    ends = []
    for seg in layout.text_anchor.text_segments:
        starts.append(int(seg.start_index) if seg.start_index else 0)
        ends.append(int(seg.end_index))
    return (min(starts), max(ends))


def _table_range(table) -> tuple[int, int]:
    """Union of every cell's text_anchor range in a Document AI table —
    the span of full-document text this table already accounts for."""
    starts = []
    ends = []
    for row in list(table.header_rows) + list(table.body_rows):
        for cell in row.cells:
            start, end = _text_anchor_range(cell.layout)
            if end > start:
                starts.append(start)
                ends.append(end)
    if not starts:
        return (0, 0)
    return (min(starts), max(ends))


def _add_docx_table(docx_doc, full_text: str, table) -> None:
    """Append a real Word table (w:tbl) built from a Document AI table,
    with the same RTL marking used everywhere else in the document."""
    import arabic_docx

    rows = list(table.header_rows) + list(table.body_rows)
    col_count = max((len(row.cells) for row in rows), default=0)
    if not rows or col_count == 0:
        return

    docx_table = docx_doc.add_table(rows=0, cols=col_count)
    docx_table.style = "Table Grid"
    for row in rows:
        row_cells = docx_table.add_row().cells
        for i, cell in enumerate(row.cells):
            if i >= col_count:
                break
            row_cells[i].text = _layout_text(full_text, cell.layout) if cell.layout else ""
            for paragraph in row_cells[i].paragraphs:
                arabic_docx.mark_paragraph_rtl(paragraph)
    arabic_docx.mark_table_rtl(docx_table)


def _build_docx_from_ocr(document, output_path: str) -> None:
    """Build a DOCX from a Document AI OCR response, preserving both
    running text and any table structure the processor detected.

    Document OCR (unlike Form Parser) is primarily a text processor, but it
    can still populate page.tables when it detects tabular layout — this
    reconstructs those as real Word tables (w:tbl) instead of flattening
    them into unstructured text. Paragraphs and tables are interleaved in
    original reading order using their position in the document's full
    text; paragraphs whose text falls inside a table's span are skipped so
    table content isn't duplicated as loose text.
    """
    from docx import Document as DocxDocument

    import arabic_docx

    docx_doc = DocxDocument()
    full_text = document.text or ""
    wrote_any = False
    pages = list(document.pages)

    for page_index, page in enumerate(pages):
        paragraphs = list(page.paragraphs or [])
        tables = list(page.tables or [])

        if paragraphs or tables:
            table_ranges = [_table_range(t) for t in tables]
            blocks: list[tuple[int, str, object]] = [
                (start, "table", t) for t, (start, end) in zip(tables, table_ranges)
            ]
            for para in paragraphs:
                p_start, p_end = _text_anchor_range(para.layout)
                if any(p_start < t_end and p_end > t_start for t_start, t_end in table_ranges):
                    continue  # already covered by a table — skip to avoid duplicating it as text
                text = _layout_text(full_text, para.layout)
                if text.strip():
                    blocks.append((p_start, "paragraph", text))
            blocks.sort(key=lambda b: b[0])

            for _start, kind, obj in blocks:
                if kind == "table":
                    _add_docx_table(docx_doc, full_text, obj)
                else:
                    p = docx_doc.add_paragraph(obj)
                    arabic_docx.mark_paragraph_rtl(p)
                wrote_any = True
        elif page.layout:
            for line in _layout_text(full_text, page.layout).splitlines():
                if not line.strip():
                    continue
                p = docx_doc.add_paragraph(line)
                arabic_docx.mark_paragraph_rtl(p)
                wrote_any = True

        if page_index < len(pages) - 1:
            docx_doc.add_page_break()

    if not wrote_any:
        raise DocAIUnavailable("Document AI لم يستخرج أي نص من الملف")

    docx_doc.save(output_path)


def _build_xlsx_from_form(document, output_path: str) -> None:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    full_text = document.text or ""
    wrote_any = False

    for page_num, page in enumerate(document.pages, start=1):
        ws = wb.create_sheet(title=f"Page {page_num}"[:31])
        tables = list(page.tables or [])
        if tables:
            for table in tables:
                for row in list(table.header_rows) + list(table.body_rows):
                    cells = [_layout_text(full_text, cell.layout) for cell in row.cells]
                    if any(cells):
                        ws.append(cells)
                        wrote_any = True
                ws.append([])  # blank row between tables, matches self-hosted layout
        else:
            page_text = _layout_text(full_text, page.layout) if page.layout else ""
            for line in page_text.splitlines():
                if line.strip():
                    ws.append([line])
                    wrote_any = True

    if not wb.sheetnames:
        wb.create_sheet(title="Empty")
    if not wrote_any:
        raise DocAIUnavailable("Document AI لم يستخرج أي جداول أو نص من الملف")

    wb.save(output_path)


def process_pdf_to_docx(input_path: str, output_path: str) -> None:
    """PDF → DOCX via Document AI's Document OCR processor.

    Raises DocAIUnavailable on any failure — the caller must catch this and
    fall back to the self-hosted/Adobe pipeline rather than propagate it.
    """
    try:
        _guard_page_count(input_path)
        document = _process_document(input_path, _env("GOOGLE_DOCAI_OCR_PROCESSOR_ID"))
        _build_docx_from_ocr(document, output_path)
    except DocAIUnavailable:
        raise
    except Exception as exc:
        raise DocAIUnavailable(f"خطأ غير متوقع أثناء معالجة Document AI: {exc}")

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise DocAIUnavailable("لم يُنشئ Document AI ملف Word صالح")


def process_pdf_to_xlsx(input_path: str, output_path: str) -> None:
    """PDF → XLSX via Document AI's Form Parser processor.

    Raises DocAIUnavailable on any failure — the caller must catch this and
    fall back to the self-hosted/Adobe pipeline rather than propagate it.
    """
    try:
        _guard_page_count(input_path)
        document = _process_document(input_path, _env("GOOGLE_DOCAI_FORM_PROCESSOR_ID"))
        _build_xlsx_from_form(document, output_path)
    except DocAIUnavailable:
        raise
    except Exception as exc:
        raise DocAIUnavailable(f"خطأ غير متوقع أثناء معالجة Document AI: {exc}")

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise DocAIUnavailable("لم يُنشئ Document AI ملف Excel صالح")


def _reset_client_cache_for_testing() -> None:
    """Used by unit tests to force _get_client() to rebuild. NOT wired to
    any production path."""
    with _client_lock:
        _client_cache.pop("client", None)
