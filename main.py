from fastapi import FastAPI, File, UploadFile, Form, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
import tempfile
import os
import shutil
import asyncio
import time
import logging

# =============================================
#  CONFIGURATION
# =============================================
API_KEY = os.getenv("PDFCO_API_KEY", "a7medfarok36@gmail.com_MvEXnMN6HVzzWK4SdBjuTnCQdjL8rLmFaDEo3dbK1lEWB9foPJu07JgGhhoU6ujx")
PDFCO_BASE = "https://api.pdf.co/v1"
UPLOAD_DIR = tempfile.mkdtemp(prefix="pdfmasry_")
FILE_TTL   = 3600          # حذف الملفات بعد ساعة
MAX_FILE_MB = 50
POLL_INTERVAL = 2          # ثانيتين بين كل poll
MAX_POLL_WAIT = 120        # أقصى وقت انتظار 120 ثانية

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("pdfmasry")

# =============================================
#  APP
# =============================================
app = FastAPI(title="PDFMasry API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pdfmasry.com",
        "https://www.pdfmasry.com",
        "http://localhost",
        "http://localhost:3000",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================
#  HELPERS
# =============================================
def make_workdir():
    return tempfile.mkdtemp(dir=UPLOAD_DIR)

async def save_upload(file: UploadFile, work_dir: str) -> str:
    safe_name = os.path.basename(file.filename or "upload.pdf")
    path = os.path.join(work_dir, safe_name)
    content = await file.read()
    if len(content) > MAX_FILE_MB * 1024 * 1024:
        raise ValueError(f"الملف أكبر من {MAX_FILE_MB} MB")
    with open(path, "wb") as f:
        f.write(content)
    return path

def cleanup_later(path: str, delay: int = FILE_TTL):
    """حذف مجلد العمل بعد مدة"""
    async def _delete():
        await asyncio.sleep(delay)
        try:
            shutil.rmtree(path, ignore_errors=True)
            log.info(f"Cleaned up: {path}")
        except Exception:
            pass
    asyncio.create_task(_delete())

def err(msg: str, code: int = 500):
    return JSONResponse({"error": msg, "success": False}, status_code=code)

# =============================================
#  PDF.CO API HELPERS
# =============================================
async def pdfco_upload(local_path: str) -> str:
    """رفع ملف إلى pdf.co والحصول على URL مؤقت"""
    async with httpx.AsyncClient(timeout=60) as client:
        # Step 1: طلب presigned upload URL
        r = await client.get(
            f"{PDFCO_BASE}/file/upload/get-presigned-url",
            params={"name": os.path.basename(local_path), "contenttype": "application/octet-stream"},
            headers={"x-api-key": API_KEY}
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise RuntimeError(f"Upload URL error: {data.get('message', data)}")

        upload_url = data["presignedUrl"]
        file_url   = data["url"]

        # Step 2: رفع الملف
        with open(local_path, "rb") as f:
            put = await client.put(
                upload_url,
                content=f.read(),
                headers={"content-type": "application/octet-stream"}
            )
        put.raise_for_status()
        return file_url


async def pdfco_job(endpoint: str, payload: dict) -> dict:
    """إرسال job إلى pdf.co وانتظار النتيجة (async polling)"""
    # Enable async mode for long jobs
    payload["async"] = True

    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{PDFCO_BASE}/{endpoint}",
            json=payload,
            headers={"x-api-key": API_KEY, "Content-Type": "application/json"}
        )
        r.raise_for_status()
        data = r.json()

    if data.get("error"):
        raise RuntimeError(data.get("message", "pdf.co error"))

    # إذا كان sync وجاء بـ url مباشرة
    if data.get("url") or data.get("urls"):
        return data

    # Polling لو async
    job_id = data.get("jobId")
    if not job_id:
        raise RuntimeError("No jobId returned from pdf.co")

    deadline = time.time() + MAX_POLL_WAIT
    async with httpx.AsyncClient(timeout=30) as client:
        while time.time() < deadline:
            await asyncio.sleep(POLL_INTERVAL)
            check = await client.get(
                f"{PDFCO_BASE}/job/check",
                params={"jobid": job_id},
                headers={"x-api-key": API_KEY}
            )
            check.raise_for_status()
            status = check.json()
            log.info(f"Job {job_id} status: {status.get('status')}")

            if status.get("status") == "success":
                return status
            if status.get("status") in ("error", "failed", "aborted"):
                raise RuntimeError(f"Job failed: {status.get('message', status)}")

    raise TimeoutError("انتهت مدة الانتظار — الملف كبير جداً أو الخادم مشغول")


async def pdfco_download(url: str, out_path: str):
    """تحميل الملف الناتج من pdf.co — S3 URLs لا تحتاج API key"""
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        # S3 presigned URLs ترفض الـ headers الإضافية — نرسل بدون API key
        if "s3" in url or "amazonaws" in url or "X-Amz" in url:
            r = await client.get(url)
        else:
            r = await client.get(url, headers={"x-api-key": API_KEY})
        r.raise_for_status()
        with open(out_path, "wb") as f:
            f.write(r.content)


async def full_convert(
    file: UploadFile,
    endpoint: str,
    extra_payload: dict,
    out_filename: str,
    media_type: str,
    bg: BackgroundTasks
) -> FileResponse | JSONResponse:
    """الدالة الرئيسية: رفع ← تحويل ← تحميل ← إرسال"""
    work_dir = make_workdir()
    try:
        # 1. حفظ الملف
        in_path = await save_upload(file, work_dir)
        log.info(f"Saved upload: {in_path}")

        # 2. رفع إلى pdf.co
        file_url = await pdfco_upload(in_path)
        log.info(f"Uploaded to pdf.co: {file_url}")

        # 3. تحويل
        payload = {"url": file_url, **extra_payload}
        result  = await pdfco_job(endpoint, payload)
        log.info(f"Conversion done: {result}")

        # 4. رابط الناتج
        result_url = result.get("url") or (result.get("urls") or [""])[0]
        if not result_url:
            return err("لم يُعاد رابط الملف الناتج من pdf.co")

        # 5. تحميل الناتج
        out_path = os.path.join(work_dir, out_filename)
        await pdfco_download(result_url, out_path)
        log.info(f"Downloaded result: {out_path}")

        # 6. حذف تلقائي بعد ساعة
        bg.add_task(cleanup_later, work_dir)

        return FileResponse(out_path, media_type=media_type, filename=out_filename)

    except ValueError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return err(str(e), 400)
    except RuntimeError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return err(str(e), 502)
    except TimeoutError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return err(str(e), 504)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        log.exception("Unexpected error")
        return err(f"خطأ غير متوقع: {str(e)}", 500)

# =============================================
#  ROUTES — STATUS
# =============================================
@app.get("/")
def root():
    return {"status": "PDFMasry API v2.0 🚀", "docs": "/docs"}

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": int(time.time())}

# =============================================
#  1. PDF → WORD
# =============================================
@app.post("/pdf-to-word")
async def pdf_to_word(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    ocr: bool = Form(False)
):
    """تحويل PDF إلى Word (.docx) مع دعم OCR اختياري"""
    payload = {
        "name": "output.docx",
        "lang": "ara+eng",
        "ocr"  : ocr,
        "inline": False,
    }
    return await full_convert(
        file, "pdf/convert/to/doc", payload,
        "converted.docx",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        bg
    )

# =============================================
#  2. PDF → EXCEL
# =============================================
@app.post("/pdf-to-excel")
async def pdf_to_excel(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    ocr: bool = Form(False)
):
    """تحويل PDF إلى Excel (.xlsx) مع استخراج الجداول"""
    payload = {
        "name": "output.xlsx",
        "lang": "ara+eng",
        "ocr"  : ocr,
        "inline": False,
    }
    return await full_convert(
        file, "pdf/convert/to/xls", payload,
        "converted.xlsx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        bg
    )

# =============================================
#  3. COMPRESS PDF
# =============================================
@app.post("/compress-pdf")
async def compress_pdf(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
):
    """ضغط PDF بأقصى جودة"""
    payload = {
        "name"   : "compressed.pdf",
        "inline" : False,
    }
    return await full_convert(
        file, "pdf/optimize", payload,
        "compressed.pdf",
        "application/pdf",
        bg
    )

# =============================================
#  4. PDF → JPG (صفحة واحدة أو ZIP)
# =============================================
@app.post("/pdf-to-jpg")
async def pdf_to_jpg(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    pages: str = Form("1"),      # "1-3" أو "0" كل الصفحات
    dpi: int   = Form(150),
):
    """تحويل صفحات PDF إلى صور JPG"""
    work_dir = make_workdir()
    try:
        in_path  = await save_upload(file, work_dir)
        file_url = await pdfco_upload(in_path)

        payload = {
            "url"    : file_url,
            "pages"  : pages,
            "dpi"    : dpi,
            "type"   : "jpg",
            "name"   : "page.jpg",
            "async"  : True,
        }
        result = await pdfco_job("pdf/convert/to/jpg", payload)

        urls = result.get("urls") or ([result.get("url")] if result.get("url") else [])
        if not urls:
            return err("لم يُعاد رابط الصورة من pdf.co")

        if len(urls) == 1:
            # صورة واحدة
            out_path = os.path.join(work_dir, "page.jpg")
            await pdfco_download(urls[0], out_path)
            bg.add_task(cleanup_later, work_dir)
            return FileResponse(out_path, media_type="image/jpeg", filename="page.jpg")
        else:
            # عدة صور → ZIP
            import zipfile
            zip_path = os.path.join(work_dir, "pages.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                for i, u in enumerate(urls, 1):
                    img_path = os.path.join(work_dir, f"page_{i}.jpg")
                    await pdfco_download(u, img_path)
                    zf.write(img_path, f"page_{i}.jpg")
            bg.add_task(cleanup_later, work_dir)
            return FileResponse(zip_path, media_type="application/zip", filename="pages.zip")

    except ValueError as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        return err(str(e), 400)
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        log.exception("pdf-to-jpg error")
        return err(str(e))

# =============================================
#  5. MERGE PDF
# =============================================
@app.post("/merge-pdf")
async def merge_pdf(
    bg: BackgroundTasks,
    files: list[UploadFile] = File(...),
):
    """دمج عدة ملفات PDF في ملف واحد"""
    if len(files) < 2:
        return err("يرجى رفع ملفين على الأقل", 400)
    if len(files) > 10:
        return err("الحد الأقصى 10 ملفات", 400)

    work_dir = make_workdir()
    try:
        urls = []
        for f in files:
            path = await save_upload(f, work_dir)
            u    = await pdfco_upload(path)
            urls.append(u)
            log.info(f"Uploaded for merge: {u}")

        payload = {
            "url"    : urls,
            "name"   : "merged.pdf",
            "inline" : False,
            "async"  : True,
        }
        result   = await pdfco_job("pdf/merge2", payload)
        out_url  = result.get("url") or (result.get("urls") or [""])[0]
        if not out_url:
            return err("لم يُعاد رابط الملف المدموج")

        out_path = os.path.join(work_dir, "merged.pdf")
        await pdfco_download(out_url, out_path)
        bg.add_task(cleanup_later, work_dir)
        return FileResponse(out_path, media_type="application/pdf", filename="merged.pdf")

    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        log.exception("merge-pdf error")
        return err(str(e))

# =============================================
#  6. SPLIT PDF
# =============================================
@app.post("/split-pdf")
async def split_pdf(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    pages: str = Form("1,2,3"),  # "1-3" أو "1,2,3"
):
    """تقسيم PDF إلى ملفات منفصلة"""
    work_dir = make_workdir()
    try:
        in_path  = await save_upload(file, work_dir)
        file_url = await pdfco_upload(in_path)

        payload = {
            "url"    : file_url,
            "pages"  : pages,
            "name"   : "split.pdf",
            "inline" : False,
            "async"  : True,
        }
        result = await pdfco_job("pdf/split", payload)
        urls   = result.get("urls") or ([result.get("url")] if result.get("url") else [])

        if not urls:
            return err("لم يُعاد رابط الملفات المقسّمة")

        if len(urls) == 1:
            out_path = os.path.join(work_dir, "split.pdf")
            await pdfco_download(urls[0], out_path)
            bg.add_task(cleanup_later, work_dir)
            return FileResponse(out_path, media_type="application/pdf", filename="split.pdf")
        else:
            import zipfile
            zip_path = os.path.join(work_dir, "split_pages.zip")
            with zipfile.ZipFile(zip_path, "w") as zf:
                for i, u in enumerate(urls, 1):
                    p = os.path.join(work_dir, f"part_{i}.pdf")
                    await pdfco_download(u, p)
                    zf.write(p, f"part_{i}.pdf")
            bg.add_task(cleanup_later, work_dir)
            return FileResponse(zip_path, media_type="application/zip", filename="split_pages.zip")

    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        log.exception("split-pdf error")
        return err(str(e))

# =============================================
#  7. PROTECT PDF (كلمة مرور)
# =============================================
@app.post("/protect-pdf")
async def protect_pdf(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
    password: str = Form(...),
    owner_password: str = Form(""),
):
    """إضافة كلمة مرور لملف PDF"""
    if len(password) < 4:
        return err("كلمة المرور يجب أن تكون 4 أحرف على الأقل", 400)

    work_dir = make_workdir()
    try:
        in_path  = await save_upload(file, work_dir)
        file_url = await pdfco_upload(in_path)

        payload = {
            "url"           : file_url,
            "ownerPassword" : owner_password or password,
            "userPassword"  : password,
            "name"          : "protected.pdf",
            "inline"        : False,
            "async"         : True,
        }
        result  = await pdfco_job("pdf/security/add", payload)
        out_url = result.get("url") or (result.get("urls") or [""])[0]

        out_path = os.path.join(work_dir, "protected.pdf")
        await pdfco_download(out_url, out_path)
        bg.add_task(cleanup_later, work_dir)
        return FileResponse(out_path, media_type="application/pdf", filename="protected.pdf")

    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        log.exception("protect-pdf error")
        return err(str(e))

# =============================================
#  8. JPG → PDF
# =============================================
@app.post("/jpg-to-pdf")
async def jpg_to_pdf(
    bg: BackgroundTasks,
    files: list[UploadFile] = File(...),
):
    """تحويل صور JPG/PNG إلى PDF"""
    work_dir = make_workdir()
    try:
        urls = []
        for f in files:
            path = os.path.join(work_dir, os.path.basename(f.filename or "img.jpg"))
            content = await f.read()
            with open(path, "wb") as fp:
                fp.write(content)
            u = await pdfco_upload(path)
            urls.append(u)

        payload = {
            "url"    : urls,
            "name"   : "output.pdf",
            "inline" : False,
            "async"  : True,
        }
        result  = await pdfco_job("pdf/convert/from/img", payload)
        out_url = result.get("url") or (result.get("urls") or [""])[0]

        out_path = os.path.join(work_dir, "output.pdf")
        await pdfco_download(out_url, out_path)
        bg.add_task(cleanup_later, work_dir)
        return FileResponse(out_path, media_type="application/pdf", filename="output.pdf")

    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        log.exception("jpg-to-pdf error")
        return err(str(e))

# =============================================
#  9. PDF INFO (عدد الصفحات + معلومات)
# =============================================
@app.post("/pdf-info")
async def pdf_info(
    bg: BackgroundTasks,
    file: UploadFile = File(...),
):
    """استخراج معلومات PDF: عدد الصفحات، الحجم، هل مشفر"""
    work_dir = make_workdir()
    try:
        in_path  = await save_upload(file, work_dir)
        file_url = await pdfco_upload(in_path)

        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(
                f"{PDFCO_BASE}/pdf/info",
                params={"url": file_url},
                headers={"x-api-key": API_KEY}
            )
            r.raise_for_status()
            data = r.json()

        bg.add_task(cleanup_later, work_dir)
        return JSONResponse({
            "success"   : True,
            "pages"     : data.get("pageCount", 0),
            "encrypted" : data.get("encrypted", False),
            "version"   : data.get("pdfVersion", ""),
            "author"    : data.get("author", ""),
            "title"     : data.get("title", ""),
            "size_bytes": os.path.getsize(in_path),
        })

    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        log.exception("pdf-info error")
        return err(str(e))

# =============================================
#  START
# =============================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", 8000)), reload=False)
