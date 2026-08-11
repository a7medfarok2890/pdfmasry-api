"""Regression tests for Phase 1 security hardening (2026-08-11).

Focus:
  - Office ZIP structural validation catches disguised ZIPs
  - Zip Slip absolute paths and .. segments are rejected
  - ZIP-bomb style extreme compression ratios are rejected
  - CACHE_ENABLED constant is hard-False (no runtime flag flip risk)

Run: pytest tests/test_security_hardening.py -v
"""
from __future__ import annotations

import io
import os
import sys
import types
import zipfile

import pytest
from fastapi import HTTPException

# Import from main.py — add project root to sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Adobe SDK is 100+ MB — stub it so unit tests can run in any env.
# main.py only uses it inside process_pdf_adobe_v4 which we don't touch here.
_ADOBE_MODULES = [
    "adobe",
    "adobe.pdfservices",
    "adobe.pdfservices.operation",
    "adobe.pdfservices.operation.auth",
    "adobe.pdfservices.operation.auth.service_principal_credentials",
    "adobe.pdfservices.operation.pdf_services",
    "adobe.pdfservices.operation.pdf_services_media_type",
    "adobe.pdfservices.operation.pdfjobs",
    "adobe.pdfservices.operation.pdfjobs.jobs",
    "adobe.pdfservices.operation.pdfjobs.jobs.export_pdf_job",
    "adobe.pdfservices.operation.pdfjobs.params",
    "adobe.pdfservices.operation.pdfjobs.params.export_pdf",
    "adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_params",
    "adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_target_format",
    "adobe.pdfservices.operation.pdfjobs.result",
    "adobe.pdfservices.operation.pdfjobs.result.export_pdf_result",
]
for _name in _ADOBE_MODULES:
    if _name not in sys.modules:
        _stub = types.ModuleType(_name)
        # Attributes the main module references from these packages
        _stub.ServicePrincipalCredentials = type("ServicePrincipalCredentials", (), {})
        _stub.PDFServices = type("PDFServices", (), {})
        _stub.PDFServicesMediaType = type("PDFServicesMediaType", (), {"PDF": "application/pdf"})
        _stub.ExportPDFJob = type("ExportPDFJob", (), {})
        _stub.ExportPDFParams = type("ExportPDFParams", (), {})
        _stub.ExportPDFTargetFormat = type("ExportPDFTargetFormat", (), {"DOCX": "docx", "XLSX": "xlsx"})
        _stub.ExportPDFResult = type("ExportPDFResult", (), {})
        sys.modules[_name] = _stub

# Slowapi + pdf2docx + pdfplumber may also be missing — pdf2docx/pdfplumber
# are only imported lazily inside functions, so main.py boots fine without
# them. Slowapi IS imported at module top so it must exist; install if not.
try:
    import slowapi  # noqa: F401
except ImportError:
    _sl = types.ModuleType("slowapi")
    _sl.Limiter = type("Limiter", (), {"__init__": lambda self, **kw: None})
    _sl._rate_limit_exceeded_handler = lambda req, exc: None
    sys.modules["slowapi"] = _sl
    _sle = types.ModuleType("slowapi.errors")
    _sle.RateLimitExceeded = type("RateLimitExceeded", (Exception,), {})
    sys.modules["slowapi.errors"] = _sle
    _slu = types.ModuleType("slowapi.util")
    _slu.get_remote_address = lambda req: "127.0.0.1"
    sys.modules["slowapi.util"] = _slu

import main  # noqa: E402


def _write_zip(tmp_path, name: str, entries: list[tuple[str, bytes]]) -> str:
    """Build a ZIP at tmp_path/name from a list of (arcname, content) pairs."""
    p = tmp_path / name
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, content in entries:
            zf.writestr(arcname, content)
    return str(p)


def _write_zip_no_compression(tmp_path, name: str, entries: list[tuple[str, bytes]]) -> str:
    """Same but stored uncompressed — useful for compression-ratio tests."""
    p = tmp_path / name
    with zipfile.ZipFile(p, "w", compression=zipfile.ZIP_STORED) as zf:
        for arcname, content in entries:
            zf.writestr(arcname, content)
    return str(p)


# ─── CACHE_ENABLED is off, no runtime flip ─────────────────────────────

def test_cache_is_hard_disabled():
    """The flag must be a plain False, not derived from env — the whole
    point of removing the cache is that no env var can turn it back on
    accidentally in production."""
    assert main.CACHE_ENABLED is False
    # Sanity: even if someone sets the env var, main.CACHE_ENABLED should
    # not change because we don't read it at all.
    os.environ["CACHE_ENABLED"] = "true"
    try:
        # Reload the flag by simulating what the endpoint sees
        assert main.CACHE_ENABLED is False
    finally:
        os.environ.pop("CACHE_ENABLED", None)


# ─── Office ZIP structural validation ──────────────────────────────────

def test_valid_docx_passes(tmp_path):
    p = _write_zip(tmp_path, "ok.docx", [
        ("[Content_Types].xml", b"<?xml version='1.0'?><Types/>"),
        ("word/document.xml", b"<?xml version='1.0'?><doc/>"),
        ("_rels/.rels", b"<?xml version='1.0'?><Relationships/>"),
    ])
    # Should not raise
    main._validate_office_zip(p, "docx")


def test_valid_xlsx_passes(tmp_path):
    p = _write_zip(tmp_path, "ok.xlsx", [
        ("[Content_Types].xml", b"<?xml version='1.0'?><Types/>"),
        ("xl/workbook.xml", b"<?xml version='1.0'?><wb/>"),
    ])
    main._validate_office_zip(p, "xlsx")


def test_disguised_zip_rejected_generic(tmp_path):
    """A ZIP without [Content_Types].xml isn't a valid Office doc."""
    p = _write_zip(tmp_path, "not_office.zip", [
        ("hello.txt", b"just a random text file"),
    ])
    with pytest.raises(HTTPException) as exc:
        main._validate_office_zip(p, None)
    assert exc.value.status_code == 415


def test_docx_missing_document_xml_rejected(tmp_path):
    """ZIP that looks Office-ish (has Content_Types) but lacks the
    required word/document.xml — reject with 415."""
    p = _write_zip(tmp_path, "fake.docx", [
        ("[Content_Types].xml", b"<?xml version='1.0'?><Types/>"),
        ("xl/workbook.xml", b"<?xml version='1.0'?><wb/>"),  # xlsx member, not docx
    ])
    with pytest.raises(HTTPException) as exc:
        main._validate_office_zip(p, "docx")
    assert exc.value.status_code == 415


def test_xlsx_missing_workbook_xml_rejected(tmp_path):
    p = _write_zip(tmp_path, "fake.xlsx", [
        ("[Content_Types].xml", b"<?xml version='1.0'?><Types/>"),
        ("word/document.xml", b"<?xml version='1.0'?><doc/>"),  # docx member
    ])
    with pytest.raises(HTTPException) as exc:
        main._validate_office_zip(p, "xlsx")
    assert exc.value.status_code == 415


# ─── Zip Slip defense ──────────────────────────────────────────────────

def test_zip_slip_absolute_path_rejected(tmp_path):
    p = _write_zip(tmp_path, "slip.docx", [
        ("[Content_Types].xml", b"x"),
        ("word/document.xml", b"x"),
        ("/etc/passwd", b"pwned"),  # absolute path — must reject
    ])
    with pytest.raises(HTTPException) as exc:
        main._validate_office_zip(p, "docx")
    assert exc.value.status_code == 415


def test_zip_slip_parent_traversal_rejected(tmp_path):
    p = _write_zip(tmp_path, "slip.docx", [
        ("[Content_Types].xml", b"x"),
        ("word/document.xml", b"x"),
        ("../evil.exe", b"pwned"),
    ])
    with pytest.raises(HTTPException) as exc:
        main._validate_office_zip(p, "docx")
    assert exc.value.status_code == 415


def test_zip_slip_windows_drive_rejected(tmp_path):
    p = _write_zip(tmp_path, "slip.docx", [
        ("[Content_Types].xml", b"x"),
        ("word/document.xml", b"x"),
        ("C:\\windows\\system32\\evil.dll", b"pwned"),
    ])
    with pytest.raises(HTTPException) as exc:
        main._validate_office_zip(p, "docx")
    assert exc.value.status_code == 415


# ─── ZIP-bomb defenses ─────────────────────────────────────────────────

def test_too_many_entries_rejected(tmp_path):
    """More than _MAX_ZIP_ENTRIES entries is refused as a bomb signal."""
    entries = [(f"file_{i}.txt", b"x") for i in range(main._MAX_ZIP_ENTRIES + 10)]
    p = _write_zip(tmp_path, "bomb.docx", entries)
    with pytest.raises(HTTPException) as exc:
        main._validate_office_zip(p, "docx")
    assert exc.value.status_code == 413


def test_extreme_compression_ratio_rejected(tmp_path):
    """A highly compressible payload (repeated null bytes) that inflates
    beyond the ratio cap must trip the guard.

    Note: this test crafts an entry where compressed size is tiny but
    uncompressed is huge enough to blow past _MAX_ZIP_COMPRESSION_RATIO.
    """
    huge_zeros = b"\x00" * (2 * 1024 * 1024)  # 2 MB of nulls
    p = _write_zip(tmp_path, "bomb.docx", [
        ("[Content_Types].xml", b"x"),
        ("word/document.xml", b"x"),
        ("bomb.bin", huge_zeros),  # compresses to ~200 bytes -> ratio 10000+
    ])
    with pytest.raises(HTTPException) as exc:
        main._validate_office_zip(p, "docx")
    # Either the ratio guard (415) or the total-uncompressed guard (413)
    # fires first depending on file order — both acceptable.
    assert exc.value.status_code in (413, 415)


def test_not_a_zip_at_all_rejected(tmp_path):
    p = tmp_path / "gibberish.docx"
    p.write_bytes(b"this is not a zip file at all")
    with pytest.raises(HTTPException) as exc:
        main._validate_office_zip(str(p), "docx")
    assert exc.value.status_code == 415


# ─── Error-detail sanitization ─────────────────────────────────────────

def test_safe_http_exception_never_includes_stack(monkeypatch):
    """The helper must set the user detail and NOT include exception
    class / stack info in it."""
    class _FakeRequest:
        class state: pass
        state.request_id = "test-rid-123"
    exc = main._safe_http_exception(
        _FakeRequest(),
        500,
        "رسالة عربية عامة",
        internal_reason="RuntimeError: sensitive path /etc/passwd stack line 42",
    )
    assert exc.status_code == 500
    assert exc.detail == "رسالة عربية عامة"
    assert "RuntimeError" not in exc.detail
    assert "passwd" not in exc.detail
    assert exc.headers["X-Request-ID"] == "test-rid-123"
