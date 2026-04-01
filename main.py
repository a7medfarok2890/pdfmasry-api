from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pdf2docx import Converter
import camelot
import pandas as pd
import subprocess
import tempfile
import os
import shutil
import zipfile

app = FastAPI(title="PDFMasry API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = tempfile.mkdtemp(prefix="pdfmasry_")


def cleanup(*paths):
    for path in paths:
        try:
            if os.path.isfile(path):
                os.remove(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
        except Exception:
            pass


def save_upload(upload: UploadFile, prefix="input_"):
    safe_name = os.path.basename(upload.filename or "file")
    work_dir = tempfile.mkdtemp(dir=UPLOAD_DIR)
    input_path = os.path.join(work_dir, prefix + safe_name)
    return work_dir, input_path, safe_name


@app.get("/")
def root():
    return {"status": "PDFMasry API is running 🚀"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/compress")
async def compress_pdf(file: UploadFile = File(...), level: str = Form("screen")):
    allowed = ["screen", "ebook", "printer", "prepress"]
    if level not in allowed:
        level = "screen"

    work_dir, input_path, safe_name = save_upload(file)
    output_path = os.path.join(work_dir, f"compressed_{safe_name}")

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
        if not os.path.exists(output_path):
            raise RuntimeError("Compression output file was not created")
        return FileResponse(output_path, media_type="application/pdf", filename=f"compressed_{safe_name}")
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode(errors="replace")}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/protect")
async def protect_pdf(file: UploadFile = File(...), password: str = Form(...)):
    work_dir, input_path, safe_name = save_upload(file)
    output_path = os.path.join(work_dir, f"protected_{safe_name}")

    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        subprocess.run(["qpdf", "--encrypt", password, password, "256", "--", input_path, output_path], check=True, capture_output=True)
        if not os.path.exists(output_path):
            raise RuntimeError("Protected output file was not created")
        return FileResponse(output_path, media_type="application/pdf", filename=f"protected_{safe_name}")
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode(errors="replace")}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/unlock")
async def unlock_pdf(file: UploadFile = File(...), password: str = Form(...)):
    work_dir, input_path, safe_name = save_upload(file)
    output_path = os.path.join(work_dir, f"unlocked_{safe_name}")

    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        subprocess.run(["qpdf", "--decrypt", f"--password={password}", input_path, output_path], check=True, capture_output=True)
        if not os.path.exists(output_path):
            raise RuntimeError("Unlocked output file was not created")
        return FileResponse(output_path, media_type="application/pdf", filename=f"unlocked_{safe_name}")
    except subprocess.CalledProcessError:
        return JSONResponse({"error": "كلمة المرور غير صحيحة أو الملف تالف"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/word-to-pdf")
async def word_to_pdf(file: UploadFile = File(...)):
    work_dir, input_path, safe_name = save_upload(file, prefix="")
    with open(input_path, "wb") as f:
        f.write(await file.read())

    base_name = os.path.splitext(safe_name)[0]
    output_path = os.path.join(work_dir, f"{base_name}.pdf")

    try:
        subprocess.run(["libreoffice", "--headless", "--convert-to", "pdf", "--outdir", work_dir, input_path], check=True, capture_output=True)
        if not os.path.exists(output_path):
            raise RuntimeError("LibreOffice did not create the PDF file")
        return FileResponse(output_path, media_type="application/pdf", filename=f"{base_name}.pdf")
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode(errors="replace")}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/pdf-to-word")
async def pdf_to_word(file: UploadFile = File(...)):
    work_dir, input_path, safe_name = save_upload(file, prefix="")
    with open(input_path, "wb") as f:
        f.write(await file.read())

    base_name = os.path.splitext(safe_name)[0]
    output_path = os.path.join(work_dir, f"{base_name}.docx")

    converter = None
    try:
        converter = Converter(input_path)
        converter.convert(output_path)
        if not os.path.exists(output_path):
            raise RuntimeError("DOCX output file was not created")
        return FileResponse(
            output_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=f"{base_name}.docx",
        )
    except Exception as e:
        return JSONResponse({"error": f"PDF to Word conversion failed: {str(e)}"}, status_code=500)
    finally:
        if converter is not None:
            try:
                converter.close()
            except Exception:
                pass


@app.post("/pdf-to-image")
async def pdf_to_image(file: UploadFile = File(...), dpi: int = Form(150), fmt: str = Form("png")):
    if fmt not in ["png", "jpg", "jpeg"]:
        fmt = "png"
    dpi = max(72, min(dpi, 300))

    work_dir, input_path, _ = save_upload(file)
    out_prefix = os.path.join(work_dir, "page")

    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        ppm_fmt = "png" if fmt == "png" else "jpeg"
        subprocess.run(["pdftoppm", f"-{ppm_fmt}", "-r", str(dpi), input_path, out_prefix], check=True, capture_output=True)

        images = sorted([os.path.join(work_dir, fn) for fn in os.listdir(work_dir) if fn.startswith("page")])
        if not images:
            raise RuntimeError("No images were generated")

        if len(images) == 1:
            ext = "png" if fmt == "png" else "jpg"
            return FileResponse(images[0], media_type=f"image/{ext}", filename=f"page-1.{ext}")

        zip_path = os.path.join(work_dir, "pdf-pages.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for i, img_path in enumerate(images, 1):
                ext = "png" if fmt == "png" else "jpg"
                zf.write(img_path, f"page-{i}.{ext}")

        return FileResponse(zip_path, media_type="application/zip", filename="pdf-pages.zip")
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode(errors="replace")}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/pdf-to-text")
async def pdf_to_text(file: UploadFile = File(...), layout: bool = Form(False)):
    work_dir, input_path, _ = save_upload(file)
    output_path = os.path.join(work_dir, "output.txt")

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

        return JSONResponse({"text": text, "characters": len(text), "words": len(text.split())})
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": e.stderr.decode(errors="replace")}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/pdf-to-excel")
async def pdf_to_excel(file: UploadFile = File(...)):
    work_dir, input_path, safe_name = save_upload(file, prefix="")
    base_name = os.path.splitext(safe_name)[0]
    output_path = os.path.join(work_dir, f"{base_name}.xlsx")

    with open(input_path, "wb") as f:
        f.write(await file.read())

    try:
        tables = camelot.read_pdf(input_path, pages="all", flavor="lattice")
        if tables.n == 0:
            tables = camelot.read_pdf(input_path, pages="all", flavor="stream")
        if tables.n == 0:
            return JSONResponse({"error": "لم يتم العثور على جداول في الملف"}, status_code=400)

        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            for i, table in enumerate(tables):
                sheet_name = f"Table_{i+1}"
                table.df.to_excel(writer, sheet_name=sheet_name, index=False, header=False)

        return FileResponse(
            output_path,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"{base_name}.xlsx"
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
