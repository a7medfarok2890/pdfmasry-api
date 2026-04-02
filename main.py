from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pdf2docx import Converter
import tempfile
import os
import shutil

app = FastAPI(title="PDFMasry API")

# ================= CORS =================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pdfmasry.com",
        "https://www.pdfmasry.com"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = tempfile.mkdtemp(prefix="pdfmasry_")


def save_upload(upload: UploadFile):
    safe_name = os.path.basename(upload.filename or "file.pdf")
    work_dir = tempfile.mkdtemp(dir=UPLOAD_DIR)
    input_path = os.path.join(work_dir, safe_name)
    return work_dir, input_path, safe_name


@app.get("/")
def root():
    return {"status": "PDFMasry API is running 🚀"}


@app.get("/health")
def health():
    return {"status": "ok"}


# ================= PDF → WORD =================
@app.post("/pdf-to-word")
async def pdf_to_word(file: UploadFile = File(...)):
    work_dir, input_path, safe_name = save_upload(file)

    with open(input_path, "wb") as f:
        f.write(await file.read())

    output_path = os.path.join(work_dir, "output.docx")

    try:
        converter = Converter(input_path)
        converter.convert(output_path)
        converter.close()

        return FileResponse(
            output_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename="converted.docx"
        )

    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ================= PDF → IMAGE =================
@app.post("/pdf-to-image")
async def pdf_to_image(file: UploadFile = File(...)):
    return JSONResponse({"message": "Coming soon"})


# ================= PDF → TEXT =================
@app.post("/pdf-to-text")
async def pdf_to_text(file: UploadFile = File(...)):
    return JSONResponse({"message": "Coming soon"})


# ================= START SERVER =================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
