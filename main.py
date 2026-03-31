from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
import subprocess
import tempfile
import os
import shutil
import zipfile
import base64

app = FastAPI(title="PDFMasry API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = tempfile.mkdtemp()


def cleanup(*paths):
    for path in paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception:
            pass


@app.get("/")
def root():
    return {"status": "PDFMasry API is running 🚀"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ─────────────────────────────────────────────
# ضغط PDF
# ─────────────────────────────────────────────
@app.post("/compress")
async def compress_pdf(file: UploadFile = File(...), level: str = Form("screen")):
    allowed = ["screen", "ebook", "printer", "prepress"]
    if level not in allowed:
        level = "screen"

    input_path = os.path.join(UPLOAD_DIR, f"input_{file.filename}")
    output_path = os.path.join(UPLOAD_DIR, f"compressed_{file.filename}")

    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        subprocess.run(
            [
                "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
                f"-dPDFSETTINGS=/{level}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                f"-sOutputFile={output_path}", input_path,
            ],
            check=True, capture_output=True,
        )
        return FileResponse(
            output_path,
            media_type="application/pdf",
            filename=f"compressed_{file.filename}",
            background=None,
        )
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode()}, status_code=500)
    finally:
        cleanup(input_path)


# ─────────────────────────────────────────────
# حماية PDF
# ─────────────────────────────────────────────
@app.post("/protect")
async def protect_pdf(file: UploadFile = File(...), password: str = Form(...)):
    input_path = os.path.join(UPLOAD_DIR, f"input_{file.filename}")
    output_path = os.path.join(UPLOAD_DIR, f"protected_{file.filename}")

    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        subprocess.run(
            [
                "qpdf", "--encrypt", password, password, "256",
                "--", input_path, output_path,
            ],
            check=True, capture_output=True,
        )
        return FileResponse(
            output_path,
            media_type="application/pdf",
            filename=f"protected_{file.filename}",
        )
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode()}, status_code=500)
    finally:
        cleanup(input_path)


# ─────────────────────────────────────────────
# فتح حماية PDF
# ─────────────────────────────────────────────
@app.post("/unlock")
async def unlock_pdf(file: UploadFile = File(...), password: str = Form(...)):
    input_path = os.path.join(UPLOAD_DIR, f"input_{file.filename}")
    output_path = os.path.join(UPLOAD_DIR, f"unlocked_{file.filename}")

    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        subprocess.run(
            [
                "qpdf", "--decrypt", f"--password={password}",
                input_path, output_path,
            ],
            check=True, capture_output=True,
        )
        return FileResponse(
            output_path,
            media_type="application/pdf",
            filename=f"unlocked_{file.filename}",
        )
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": "كلمة المرور غير صحيحة أو الملف تالف"}, status_code=500)
    finally:
        cleanup(input_path)


# ─────────────────────────────────────────────
# Word → PDF
# ─────────────────────────────────────────────
@app.post("/word-to-pdf")
async def word_to_pdf(file: UploadFile = File(...)):
    input_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "pdf",
             "--outdir", UPLOAD_DIR, input_path],
            check=True, capture_output=True,
        )
        base_name = os.path.splitext(file.filename)[0]
        output_path = os.path.join(UPLOAD_DIR, f"{base_name}.pdf")
        return FileResponse(
            output_path,
            media_type="application/pdf",
            filename=f"{base_name}.pdf",
        )
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode()}, status_code=500)
    finally:
        cleanup(input_path)


# ─────────────────────────────────────────────
# PDF → Word
# ─────────────────────────────────────────────
@app.post("/pdf-to-word")
async def pdf_to_word(file: UploadFile = File(...)):
    input_path = os.path.join(UPLOAD_DIR, file.filename)
    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        subprocess.run(
            ["libreoffice", "--headless", "--convert-to", "docx",
             "--outdir", UPLOAD_DIR, input_path],
            check=True, capture_output=True,
        )
        base_name = os.path.splitext(file.filename)[0]
        output_path = os.path.join(UPLOAD_DIR, f"{base_name}.docx")
        return FileResponse(
            output_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=f"{base_name}.docx",
        )
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode()}, status_code=500)
    finally:
        cleanup(input_path)


# ─────────────────────────────────────────────
# PDF → صور  ✨ جديد
# ─────────────────────────────────────────────
@app.post("/pdf-to-image")
async def pdf_to_image(
    file: UploadFile = File(...),
    dpi: int = Form(150),
    fmt: str = Form("png"),
):
    if fmt not in ["png", "jpg", "jpeg"]:
        fmt = "png"
    if dpi < 72:
        dpi = 72
    if dpi > 300:
        dpi = 300

    input_path = os.path.join(UPLOAD_DIR, f"input_{file.filename}")
    out_dir = tempfile.mkdtemp(dir=UPLOAD_DIR)
    out_prefix = os.path.join(out_dir, "page")

    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        ppm_fmt = "png" if fmt == "png" else "jpeg"
        subprocess.run(
            [
                "pdftoppm",
                f"-{ppm_fmt}",
                "-r", str(dpi),
                input_path,
                out_prefix,
            ],
            check=True, capture_output=True,
        )

        images = sorted([
            os.path.join(out_dir, fn)
            for fn in os.listdir(out_dir)
            if fn.startswith("page")
        ])

        if len(images) == 1:
            ext = "png" if fmt == "png" else "jpg"
            return FileResponse(
                images[0],
                media_type=f"image/{ext}",
                filename=f"page-1.{ext}",
            )

        # أكثر من صفحة → ZIP
        zip_path = os.path.join(UPLOAD_DIR, "pages.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for i, img_path in enumerate(images, 1):
                ext = "png" if fmt == "png" else "jpg"
                zf.write(img_path, f"page-{i}.{ext}")

        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename="pdf-pages.zip",
        )
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode()}, status_code=500)
    finally:
        cleanup(input_path, out_dir)


# ─────────────────────────────────────────────
# PDF → نص  ✨ جديد
# ─────────────────────────────────────────────
@app.post("/pdf-to-text")
async def pdf_to_text(
    file: UploadFile = File(...),
    layout: bool = Form(False),
):
    input_path = os.path.join(UPLOAD_DIR, f"input_{file.filename}")
    output_path = os.path.join(UPLOAD_DIR, "output.txt")

    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        cmd = ["pdftotext"]
        if layout:
            cmd.append("-layout")
        cmd += [input_path, output_path]

        subprocess.run(cmd, check=True, capture_output=True)

        with open(output_path, "r", encoding="utf-8", errors="replace") as tf:
            text = tf.read()

        return JSONResponse({
            "text": text,
            "characters": len(text),
            "words": len(text.split()),
        })
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode()}, status_code=500)
    finally:
        cleanup(input_path, output_path)
