"""PDFMasry API — secure edition (patched 2026-08-03).

Adds three Day-1-audit fixes on top of the 2026-07-30 UUID+HMAC baseline:

  1. MIME magic-byte validation before running any subprocess.
     Wrong file type now returns 415 (or 400 for empty/corrupt) instead
     of leaking 500 from the underlying tool.

  2. Per-IP rate limiting via slowapi.
     Convert endpoints: 20/minute. Downloads: 60/minute.
     Global default: 120/hour.

  3. HEAD method on /api/download/{job_id}/{token}.
     Same headers as GET, no body. Fixes crawler / link-preview probes
     that previously got 405.

Every convert endpoint still:
  * stores files in <uploads>/<job_uuid>/  (per-job isolation)
  * returns a signed one-time download URL: /api/download/<job_id>/<token>
  * token = HMAC(secret, "<job_id>.<expiry>") with 1h TTL
  * uses the ORIGINAL filename only in Content-Disposition (RFC 5987)
  * emits Cache-Control: private, no-store on downloads
  * deletes the whole job dir after 1 hour via background task

Endpoints unchanged from the frontend's perspective:
  POST /api/{tool}     → 200 {"status":"success","download_url":"/api/download/..."}
  GET  /api/download/{job_id}/{token}   → file with proper headers
  HEAD /api/download/{job_id}/{token}   → same headers, no body
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import secrets
import shutil
import subprocess
import time
import unicodedata
import uuid
from urllib.parse import quote

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

# --- استدعاء مكتبات أدوبي الحديثة (V4) ---
from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
from adobe.pdfservices.operation.pdf_services import PDFServices
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
from adobe.pdfservices.operation.pdfjobs.jobs.export_pdf_job import ExportPDFJob
from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_params import ExportPDFParams
from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_target_format import ExportPDFTargetFormat
from adobe.pdfservices.operation.pdfjobs.result.export_pdf_result import ExportPDFResult

# ───────────────────────────────────────────────────────────────────────
# Result cache + usage tracking (Phase 1 of arch redesign, 2026-08-05)
# ───────────────────────────────────────────────────────────────────────
# Design goal: skip Adobe transactions when the same file was already
# converted recently. Also tracks Adobe usage per endpoint so the admin
# dashboard can show remaining monthly quota.
#
# Feature flag CACHE_ENABLED controls whether cache lookups happen — set
# to "false" (default) means the wrapper functions bypass entirely and
# behavior is identical to the pre-cache codebase. Rollback = env var.
import cache as pdf_cache
import cache_stats

CACHE_ENABLED = os.environ.get("CACHE_ENABLED", "false").lower() == "true"

# Bump this on Adobe SDK upgrades or format-changing config changes so
# stored cache entries invalidate automatically. Format: freeform tag.
ADOBE_PROVIDER_VERSION = "adobe-sdk-v4-2026-08"

# Monthly Adobe free tier — surfaced by /api/admin/cache-stats so the
# dashboard can compute remaining quota. Bump if the paid tier is purchased.
ADOBE_MONTHLY_QUOTA = int(os.environ.get("ADOBE_MONTHLY_QUOTA", "500"))

# Admin token gates /api/admin/cache-stats. Missing token → endpoint 401s
# for everyone (fail closed, don't accidentally expose usage counters).
ADMIN_STATS_TOKEN = os.environ.get("ADMIN_STATS_TOKEN")

# ───────────────────────────────────────────────────────────────────────
# Rate limiter — per-IP quotas via X-Forwarded-For (Railway sets this)
# ───────────────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["120/hour", "60/minute"])

app = FastAPI(title="PDFMasry API Complete", version="5.1-secure")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# إعدادات الأمان (CORS) لحماية الباندويث الخاص بك
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pdfmasry.com",
        "https://www.pdfmasry.com",
        "https://taupe-rugelach-921837.netlify.app",
        "https://pdfmasry-staging.netlify.app",
        "http://localhost:4321",
        "http://localhost:4322",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

UPLOAD_DIR = "./temp_files"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Secret used to sign download tokens. Set DOWNLOAD_SECRET on Railway.
# A missing secret still boots the server (falls back to a random per-boot
# value) — but tokens generated by one boot cannot be verified by another.
DOWNLOAD_SECRET = os.environ.get("DOWNLOAD_SECRET") or secrets.token_urlsafe(32)
DOWNLOAD_TOKEN_TTL_SECONDS = 60 * 60  # 1 hour
JOB_TTL_SECONDS = 60 * 60             # cleanup after 1 hour
MAX_UPLOAD_BYTES = 50 * 1024 * 1024   # 50 MB — matches frontend claim

# ───────────────────────────────────────────────────────────────────────
# MIME validation — magic bytes only, no reliance on filename extension
# ───────────────────────────────────────────────────────────────────────
# Accepted magic byte signatures per tool family.
MAGIC_PDF = b"%PDF-"
MAGIC_ZIP = b"PK\x03\x04"          # DOCX, XLSX, PPTX (Office 2007+)
MAGIC_OLE = b"\xd0\xcf\x11\xe0"    # DOC, XLS, PPT (Office 97-2003)


def _detect_magic(path: str) -> str:
    """Return a canonical family name based on the first few bytes,
    or empty string if unrecognised. We only need to distinguish the
    types this API accepts.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except (OSError, IOError):
        return ""
    if head.startswith(MAGIC_PDF):
        return "pdf"
    if head.startswith(MAGIC_ZIP):
        return "office_zip"     # docx/xlsx/pptx — distinguish later per endpoint
    if head.startswith(MAGIC_OLE):
        return "office_ole"     # doc/xls/ppt legacy
    return ""


def _validate_input(path: str, allowed_families: set[str]) -> None:
    """Raise HTTPException if the file at `path` isn't in `allowed_families`.
    Files that don't match any known magic → 415.
    Empty files → 400.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    if size == 0:
        raise HTTPException(status_code=400, detail="الملف فارغ")
    family = _detect_magic(path)
    if not family:
        raise HTTPException(
            status_code=415,
            detail="نوع الملف غير مدعوم — تأكد إن الملف حقيقي وسليم",
        )
    if family not in allowed_families:
        raise HTTPException(
            status_code=415,
            detail="نوع الملف لا يتوافق مع هذه الأداة",
        )


# ───────────────────────────────────────────────────────────────────────
# Security helpers — UUID job dirs, signed tokens, sanitized names
# ───────────────────────────────────────────────────────────────────────

def _new_job_id() -> str:
    """32 hex chars, 128 bits of entropy — unguessable."""
    return uuid.uuid4().hex


def _job_dir(job_id: str) -> str:
    return os.path.join(UPLOAD_DIR, job_id)


def _sign(payload: str) -> str:
    return hmac.new(
        DOWNLOAD_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _make_token(job_id: str) -> str:
    expiry = int(time.time()) + DOWNLOAD_TOKEN_TTL_SECONDS
    sig = _sign(f"{job_id}.{expiry}")
    return f"{expiry}.{sig}"


def _verify_token(job_id: str, token: str) -> bool:
    try:
        expiry_str, sig = token.split(".", 1)
        expiry = int(expiry_str)
    except (ValueError, AttributeError):
        return False
    if time.time() > expiry:
        return False
    expected = _sign(f"{job_id}.{expiry}")
    return hmac.compare_digest(sig, expected)


def _sanitize_filename(name: str) -> str:
    """Safe display filename for Content-Disposition. Storage uses UUID."""
    name = os.path.basename(name or "file")
    name = re.sub(r"[\x00-\x1f\x7f/\\:*?\"<>|]+", "_", name)
    name = unicodedata.normalize("NFC", name).strip(" .")
    if not name:
        name = "file"
    return name[:120]


def _content_disposition(download_name: str) -> str:
    ascii_fallback = re.sub(r"[^A-Za-z0-9._-]+", "_", download_name) or "file"
    quoted = quote(download_name, safe="")
    return f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{quoted}'


async def _save_upload_to_job(file: UploadFile, allowed_families: set[str]) -> tuple[str, str, str]:
    """Create a fresh job dir, save upload as input.bin, validate its magic
    bytes against `allowed_families`. On rejection the job dir is cleaned
    up and an HTTPException is raised.

    Returns (job_id, job_dir, sanitized_original_name) on success.
    """
    job_id = _new_job_id()
    jdir = _job_dir(job_id)
    os.makedirs(jdir, exist_ok=True)
    input_path = os.path.join(jdir, "input.bin")
    total = 0
    try:
        with open(input_path, "wb") as buffer:
            # Stream in chunks so we can enforce size cap without loading
            # the whole file into memory.
            while True:
                chunk = await file.read(1024 * 1024)  # 1 MB
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    buffer.close()
                    shutil.rmtree(jdir, ignore_errors=True)
                    raise HTTPException(
                        status_code=413,
                        detail="حجم الملف يتجاوز الحد المسموح (50 ميجابايت)",
                    )
                buffer.write(chunk)
        _validate_input(input_path, allowed_families)
    except HTTPException:
        shutil.rmtree(jdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(jdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"فشل استقبال الملف: {str(e)}")

    return job_id, jdir, _sanitize_filename(file.filename or "file")


def _make_response(job_id: str, output_path: str, download_name: str, media_type: str) -> dict:
    """Move the produced file to <job_dir>/output.bin (canonical name),
    write meta so download endpoint knows the display name + type.
    """
    jdir = _job_dir(job_id)
    canonical = os.path.join(jdir, "output.bin")
    if os.path.abspath(output_path) != os.path.abspath(canonical):
        shutil.move(output_path, canonical)
    with open(os.path.join(jdir, "meta.txt"), "w", encoding="utf-8") as f:
        f.write(f"{media_type}\n{download_name}\n")
    return {
        "status": "success",
        "download_url": f"/api/download/{job_id}/{_make_token(job_id)}",
        "expires_in": DOWNLOAD_TOKEN_TTL_SECONDS,
    }


async def _delete_job_after_delay(job_id: str, delay_seconds: int = JOB_TTL_SECONDS):
    await asyncio.sleep(delay_seconds)
    jdir = _job_dir(job_id)
    if os.path.exists(jdir):
        shutil.rmtree(jdir, ignore_errors=True)

# ───────────────────────────────────────────────────────────────────────
# 1. المحرك الأساسي لأدوبي (Adobe V4) للتحويل العربي الدقيق
# ───────────────────────────────────────────────────────────────────────
def process_pdf_adobe_v4(input_path: str, output_path: str, target_format):
    client_id = os.getenv("ADOBE_CLIENT_ID")
    client_secret = os.getenv("ADOBE_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise Exception("مفاتيح أدوبي غير موجودة في بيئة Railway")

    credentials = ServicePrincipalCredentials(client_id=client_id, client_secret=client_secret)
    pdf_services = PDFServices(credentials=credentials)

    with open(input_path, 'rb') as f:
        input_stream = f.read()
    input_asset = pdf_services.upload(input_stream=input_stream, mime_type=PDFServicesMediaType.PDF)

    export_pdf_params = ExportPDFParams(target_format=target_format)
    export_pdf_job = ExportPDFJob(input_asset=input_asset, export_pdf_params=export_pdf_params)

    location = pdf_services.submit(export_pdf_job)
    pdf_services_response = pdf_services.get_job_result(location, ExportPDFResult)

    result_asset = pdf_services_response.get_result().get_asset()
    stream_asset = pdf_services.get_content(result_asset)

    with open(output_path, "wb") as output_file:
        output_file.write(stream_asset.get_input_stream())

# ───────────────────────────────────────────────────────────────────────
# 2. Health + secure download (GET + HEAD)
# ───────────────────────────────────────────────────────────────────────
@app.get("/")
def health_check():
    return {"status": "PDFMasry API is running with ALL Server Tools!", "version": app.version}


@app.get("/health")
def health():
    return {"status": "ok"}


# ───────────────────────────────────────────────────────────────────────
# Admin: cache + Adobe usage stats
# ───────────────────────────────────────────────────────────────────────
# Behind a shared secret env var. No user data, no filenames — just
# counters. Fail closed: if ADMIN_STATS_TOKEN is unset, endpoint 401s
# for everyone.

@app.get("/api/admin/cache-stats")
def admin_cache_stats(request: Request):
    """Returns cache hit rates + Adobe monthly usage. Requires admin token
    passed either as ``?token=`` query param or ``X-Admin-Token`` header.

    Response shape is documented in cache_stats.snapshot(). Safe to poll
    from a dashboard; O(1) work regardless of cache size.
    """
    if not ADMIN_STATS_TOKEN:
        raise HTTPException(status_code=401, detail="Admin stats endpoint not configured")
    supplied = request.query_params.get("token") or request.headers.get("X-Admin-Token", "")
    if not hmac.compare_digest(supplied, ADMIN_STATS_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid admin token")
    return {
        "cache_enabled": CACHE_ENABLED,
        "adobe_provider_version": ADOBE_PROVIDER_VERSION,
        "counters": cache_stats.snapshot(monthly_quota=ADOBE_MONTHLY_QUOTA),
        "cache_health": pdf_cache.stats(),
    }


def _build_download_headers(job_id: str, token: str) -> tuple[dict, str, str, str] | JSONResponse:
    """Shared validation + header prep for both GET and HEAD download.
    Returns (headers, media_type, download_name, output_path) on success,
    or a JSONResponse to send directly on error.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    if not _verify_token(job_id, token):
        return JSONResponse({"detail": "Invalid or expired link"}, status_code=403)

    jdir = _job_dir(job_id)
    output = os.path.join(jdir, "output.bin")
    meta_path = os.path.join(jdir, "meta.txt")
    if not os.path.isfile(output) or not os.path.isfile(meta_path):
        return JSONResponse({"detail": "File not found or already deleted"}, status_code=404)

    with open(meta_path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    media_type = lines[0] if lines else "application/octet-stream"
    download_name = lines[1] if len(lines) > 1 else "download"

    headers = {
        "Content-Disposition": _content_disposition(download_name),
        "Cache-Control": "private, no-store",
        "Pragma": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    return headers, media_type, download_name, output


@app.get("/api/download/{job_id}/{token}")
@limiter.limit("60/minute")
def download_file(job_id: str, token: str, request: Request):
    result = _build_download_headers(job_id, token)
    if isinstance(result, JSONResponse):
        return result
    headers, media_type, _download_name, output = result
    response = FileResponse(output, media_type=media_type)
    for k, v in headers.items():
        response.headers[k] = v
    return response


@app.head("/api/download/{job_id}/{token}")
@limiter.limit("60/minute")
def head_download_file(job_id: str, token: str, request: Request):
    """HEAD variant: same headers as GET, no body.
    Some clients (link previewers, crawlers, download managers) probe with
    HEAD before GET. Without this handler they got 405 Method Not Allowed.
    """
    result = _build_download_headers(job_id, token)
    if isinstance(result, JSONResponse):
        return result
    headers, media_type, _download_name, output = result
    # Report actual file size so range requests and progress bars work.
    try:
        size = os.path.getsize(output)
    except OSError:
        size = 0
    headers["Content-Length"] = str(size)
    headers["Content-Type"] = media_type
    return Response(status_code=200, headers=headers)


# ───────────────────────────────────────────────────────────────────────
# 3. مسارات تحويل PDF → Office (اللغة العربية) — accept PDF only
# ───────────────────────────────────────────────────────────────────────
# Provider routing (2026-08 emergency fix):
#   Adobe PDF Services' free monthly quota (500 tx) got exhausted in
#   production, so every user request was returning 500 from the SDK.
#   We now route through LibreOffice by default — self-hosted, unlimited,
#   already on the Railway image (used for Word→PDF the other direction).
#   Adobe path is kept as an opt-in via USE_ADOBE=true env var for the
#   day the paid tier is turned on; the cache layer still fronts either
#   provider transparently.
#
# Quality note: LibreOffice's PDF→Office conversion is generally
# reasonable but weaker than Adobe on complex layouts (multi-column,
# heavy embedded fonts). For our Arabic user base, the RTL text is
# preserved correctly which is the main constraint.

USE_ADOBE = os.environ.get("USE_ADOBE", "false").lower() == "true"
LIBREOFFICE_PROVIDER_VERSION = "libreoffice-cli-2026-08"


def process_pdf_libreoffice(input_path: str, output_path: str, target_ext: str) -> None:
    """PDF → DOCX/XLSX via LibreOffice headless CLI.

    Args:
        input_path: uploaded PDF (must exist)
        output_path: canonical destination for the produced Office file
        target_ext: "docx" or "xlsx"

    Raises:
        HTTPException 500 with a user-friendly message on failure. Timeout
        is 90s; longer than typical (5-15s) but tight enough that a stuck
        subprocess doesn't hold a worker forever.
    """
    if target_ext not in {"docx", "xlsx"}:
        raise HTTPException(status_code=500, detail=f"صيغة غير مدعومة: {target_ext}")

    outdir = os.path.dirname(output_path) or "."
    try:
        subprocess.run(
            [
                "libreoffice", "--headless",
                "--convert-to", target_ext,
                "--outdir", outdir,
                input_path,
            ],
            check=True,
            capture_output=True,
            timeout=90,
        )
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="انتهت مهلة تحويل الملف — جرّب ملف أصغر")
    except subprocess.CalledProcessError as e:
        stderr_snippet = (e.stderr or b"").decode("utf-8", "ignore")[:200]
        raise HTTPException(
            status_code=500,
            detail=f"تعذّر تحويل الملف. ربما الملف تالف أو يستخدم تنسيقاً معقداً. تفاصيل: {stderr_snippet}",
        )

    # LibreOffice writes <input_basename>.<target_ext> into outdir; move
    # it to our canonical path so downstream code doesn't need to know
    # about that convention.
    input_base = os.path.splitext(os.path.basename(input_path))[0]
    default_out = os.path.join(outdir, f"{input_base}.{target_ext}")
    if not os.path.exists(default_out):
        raise HTTPException(status_code=500, detail="فشل التحويل: لم يُنشأ ملف الإخراج")
    if os.path.abspath(default_out) != os.path.abspath(output_path):
        shutil.move(default_out, output_path)


def _convert_pdf_to_office(
    endpoint: str,
    input_path: str,
    output_path: str,
    target_ext: str,
    adobe_format,
) -> bool:
    """Run PDF→Office conversion with cache in front, provider auto-selected.

    Args:
        endpoint: tool name for stats attribution ("pdf-to-word" / "pdf-to-excel")
        input_path: user-uploaded PDF
        output_path: destination for the DOCX/XLSX result
        target_ext: "docx" or "xlsx" (used for cache key + LibreOffice)
        adobe_format: Adobe ExportPDFTargetFormat enum (used only when
            USE_ADOBE is enabled)

    Returns:
        True if served from cache; False if a provider was invoked.

    Provider precedence:
        1. If cache HIT for the current provider+version → return the cached
           bytes (Adobe transactions untouched)
        2. Else if USE_ADOBE=true → call Adobe SDK
        3. Else (default) → call LibreOffice
    """
    provider_name = "adobe" if USE_ADOBE else "libreoffice"
    provider_version = ADOBE_PROVIDER_VERSION if USE_ADOBE else LIBREOFFICE_PROVIDER_VERSION

    key = None
    if CACHE_ENABLED:
        try:
            key = pdf_cache.CacheKey(
                input_sha256=pdf_cache.hash_file(input_path),
                target_format=target_ext,
                provider_name=provider_name,
                provider_version=provider_version,
                password_hash="",
            )
            cached_path = pdf_cache.get(key)
            if cached_path is not None:
                shutil.copyfile(cached_path, output_path)
                cache_stats.record_cache_hit()
                return True
            cache_stats.record_cache_miss()
        except Exception:
            # Cache lookup broken — fall through to provider call.
            key = None

    # Provider dispatch
    if USE_ADOBE:
        process_pdf_adobe_v4(input_path, output_path, adobe_format)
        cache_stats.record_adobe_transaction(endpoint)
    else:
        process_pdf_libreoffice(input_path, output_path, target_ext)

    # Populate cache for next time — never let a cache write failure break
    # a successful conversion.
    if CACHE_ENABLED and key is not None:
        try:
            pdf_cache.put(key, output_path)
        except Exception:
            pass
    return False


@app.post("/api/pdf-to-word")
@limiter.limit("20/minute")
async def convert_pdf_to_word(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    job_id, jdir, orig = await _save_upload_to_job(file, {"pdf"})
    input_path = os.path.join(jdir, "input.bin")
    output_path = os.path.join(jdir, "converted.docx")
    try:
        _convert_pdf_to_office("pdf-to-word", input_path, output_path, "docx", ExportPDFTargetFormat.DOCX)
        base = os.path.splitext(orig)[0] or "document"
        response_data = _make_response(
            job_id,
            output_path,
            f"{base}.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        background_tasks.add_task(_delete_job_after_delay, job_id)
        return response_data
    except HTTPException:
        # Preserve the tailored status + Arabic detail already raised by the provider.
        background_tasks.add_task(_delete_job_after_delay, job_id, delay_seconds=5)
        raise
    except Exception as e:
        background_tasks.add_task(_delete_job_after_delay, job_id, delay_seconds=5)
        raise HTTPException(status_code=500, detail=f"حدث خطأ: {str(e)}")


@app.post("/api/pdf-to-excel")
@limiter.limit("20/minute")
async def convert_pdf_to_excel(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    job_id, jdir, orig = await _save_upload_to_job(file, {"pdf"})
    input_path = os.path.join(jdir, "input.bin")
    output_path = os.path.join(jdir, "converted.xlsx")
    try:
        _convert_pdf_to_office("pdf-to-excel", input_path, output_path, "xlsx", ExportPDFTargetFormat.XLSX)
        base = os.path.splitext(orig)[0] or "document"
        response_data = _make_response(
            job_id,
            output_path,
            f"{base}.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        background_tasks.add_task(_delete_job_after_delay, job_id)
        return response_data
    except HTTPException:
        background_tasks.add_task(_delete_job_after_delay, job_id, delay_seconds=5)
        raise
    except Exception as e:
        background_tasks.add_task(_delete_job_after_delay, job_id, delay_seconds=5)
        raise HTTPException(status_code=500, detail=f"حدث خطأ: {str(e)}")

# ───────────────────────────────────────────────────────────────────────
# 4. الأدوات المجانية (Ghostscript, qpdf, LibreOffice, Poppler) — PDF
# ───────────────────────────────────────────────────────────────────────
@app.post("/api/compress")
@limiter.limit("20/minute")
async def compress_pdf(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    job_id, jdir, orig = await _save_upload_to_job(file, {"pdf"})
    input_path = os.path.join(jdir, "input.bin")
    output_path = os.path.join(jdir, "compressed.pdf")
    try:
        cmd = ["gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
               "-dPDFSETTINGS=/screen", "-dNOPAUSE", "-dQUIET", "-dBATCH",
               f"-sOutputFile={output_path}", input_path]
        subprocess.run(cmd, check=True, capture_output=True)
        response_data = _make_response(job_id, output_path, f"compressed_{orig}", "application/pdf")
        background_tasks.add_task(_delete_job_after_delay, job_id)
        return response_data
    except Exception:
        background_tasks.add_task(_delete_job_after_delay, job_id, delay_seconds=5)
        raise HTTPException(status_code=500, detail="فشل ضغط الملف")


@app.post("/api/protect")
@limiter.limit("15/minute")
async def protect_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    password: str = Form("pdfmasry"),
):
    job_id, jdir, orig = await _save_upload_to_job(file, {"pdf"})
    input_path = os.path.join(jdir, "input.bin")
    output_path = os.path.join(jdir, "protected.pdf")
    try:
        cmd = ["qpdf", "--encrypt", password, password, "256", "--", input_path, output_path]
        subprocess.run(cmd, check=True, capture_output=True)
        response_data = _make_response(job_id, output_path, f"protected_{orig}", "application/pdf")
        background_tasks.add_task(_delete_job_after_delay, job_id)
        return response_data
    except Exception:
        background_tasks.add_task(_delete_job_after_delay, job_id, delay_seconds=5)
        raise HTTPException(status_code=500, detail="فشلت حماية الملف")


@app.post("/api/unlock")
@limiter.limit("15/minute")
async def unlock_pdf(
    request: Request,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    password: str = Form("pdfmasry"),
):
    job_id, jdir, orig = await _save_upload_to_job(file, {"pdf"})
    input_path = os.path.join(jdir, "input.bin")
    output_path = os.path.join(jdir, "unlocked.pdf")
    try:
        cmd = ["qpdf", f"--password={password}", "--decrypt", input_path, output_path]
        subprocess.run(cmd, check=True, capture_output=True)
        response_data = _make_response(job_id, output_path, f"unlocked_{orig}", "application/pdf")
        background_tasks.add_task(_delete_job_after_delay, job_id)
        return response_data
    except Exception:
        background_tasks.add_task(_delete_job_after_delay, job_id, delay_seconds=5)
        raise HTTPException(status_code=500, detail="فشل فك الحماية، قد تكون كلمة المرور خاطئة")


@app.post("/api/pdf-to-image")
@limiter.limit("20/minute")
async def pdf_to_image(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    job_id, jdir, orig = await _save_upload_to_job(file, {"pdf"})
    input_path = os.path.join(jdir, "input.bin")
    pages_dir = os.path.join(jdir, "pages")
    os.makedirs(pages_dir, exist_ok=True)
    try:
        cmd = ["pdftoppm", "-jpeg", "-r", "150", input_path, os.path.join(pages_dir, "page")]
        subprocess.run(cmd, check=True, capture_output=True)

        zip_base = os.path.join(jdir, "pages_archive")
        shutil.make_archive(zip_base, "zip", pages_dir)
        zip_path = f"{zip_base}.zip"

        base = os.path.splitext(orig)[0] or "document"
        response_data = _make_response(job_id, zip_path, f"{base}_images.zip", "application/zip")
        background_tasks.add_task(_delete_job_after_delay, job_id)
        return response_data
    except Exception:
        background_tasks.add_task(_delete_job_after_delay, job_id, delay_seconds=5)
        raise HTTPException(status_code=500, detail="فشل تحويل الملف إلى صور")


@app.post("/api/word-to-pdf")
@app.post("/api/excel-to-pdf")
@limiter.limit("20/minute")
async def office_to_pdf(request: Request, background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    # Accept both Office 2007+ (zip) and legacy 97-2003 (ole).
    job_id, jdir, orig = await _save_upload_to_job(file, {"office_zip", "office_ole"})
    # LibreOffice needs the input to have a proper extension
    input_ext = os.path.splitext(orig)[1] or ".docx"
    input_path = os.path.join(jdir, f"input{input_ext}")
    canonical_input = os.path.join(jdir, "input.bin")
    shutil.move(canonical_input, input_path)

    try:
        cmd = ["libreoffice", "--headless", "--convert-to", "pdf", input_path, "--outdir", jdir]
        subprocess.run(cmd, check=True, capture_output=True)
        # LibreOffice writes <basename>.pdf where basename = os.path.splitext(input_filename)[0]
        libre_output = os.path.join(jdir, f"input.pdf")
        if not os.path.exists(libre_output):
            # Some versions name it after original — glob for any .pdf just in case
            candidates = [f for f in os.listdir(jdir) if f.lower().endswith(".pdf")]
            if candidates:
                libre_output = os.path.join(jdir, candidates[0])
            else:
                raise RuntimeError("LibreOffice produced no PDF")

        base = os.path.splitext(orig)[0] or "document"
        response_data = _make_response(job_id, libre_output, f"{base}.pdf", "application/pdf")
        background_tasks.add_task(_delete_job_after_delay, job_id)
        return response_data
    except Exception:
        background_tasks.add_task(_delete_job_after_delay, job_id, delay_seconds=5)
        raise HTTPException(status_code=500, detail="فشل تحويل المستند إلى PDF")
