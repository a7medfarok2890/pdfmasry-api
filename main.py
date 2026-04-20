import os
import shutil
import asyncio
import subprocess
from fastapi import FastAPI, File, UploadFile, BackgroundTasks, HTTPException, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# --- استدعاء مكتبات أدوبي الحديثة (V4) ---
from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
from adobe.pdfservices.operation.pdf_services import PDFServices
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
from adobe.pdfservices.operation.pdfjobs.jobs.export_pdf_job import ExportPDFJob
from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_params import ExportPDFParams
from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_target_format import ExportPDFTargetFormat
from adobe.pdfservices.operation.pdfjobs.result.export_pdf_result import ExportPDFResult

app = FastAPI(title="PDFMasry API Complete", version="4.0")

# إعدادات الأمان (CORS) لحماية الباندويث الخاص بك
app.add_middleware(
    CORSMiddleware,
              allow_origins=[
        "https://pdfmasry.com",
        "https://www.pdfmasry.com",
        "https://taupe-rugelach-921837.netlify.app",
        "https://pdfmasry-staging.netlify.app",
        "http://localhost:4321",
        "http://localhost:4322"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

UPLOAD_DIR = "./temp_files"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# دالة الحذف التلقائي للملفات بعد نصف ساعة للحفاظ على الخصوصية والمساحة
async def delete_file_after_delay(file_path: str, delay_seconds: int = 1800):
    await asyncio.sleep(delay_seconds)
    if os.path.exists(file_path):
        if os.path.isdir(file_path):
            shutil.rmtree(file_path)
        else:
            os.remove(file_path)

# ---------------------------------------------------------
# 1. المحرك الأساسي لأدوبي (Adobe V4) للتحويل العربي الدقيق
# ---------------------------------------------------------
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

# ---------------------------------------------------------
# 2. مسارات فحص السيرفر والتحميل
# ---------------------------------------------------------
@app.get("/")
def health_check():
    return {"status": "PDFMasry API is running with ALL Server Tools!"}

@app.get("/api/download/{filename}")
def download_file(filename: str):
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    raise HTTPException(status_code=404, detail="عذراً، انتهت صلاحية الرابط وتم حذف الملف")

# ---------------------------------------------------------
# 3. مسارات أدوبي (للغة العربية)
# ---------------------------------------------------------
@app.post("/api/pdf-to-word")
async def convert_pdf_to_word(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    input_path = os.path.join(UPLOAD_DIR, file.filename)
    output_filename = f"word_{file.filename.replace('.pdf', '.docx')}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)
    with open(input_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)

    try:
        process_pdf_adobe_v4(input_path, output_path, ExportPDFTargetFormat.DOCX)
        background_tasks.add_task(delete_file_after_delay, input_path)
        background_tasks.add_task(delete_file_after_delay, output_path)
        return {"status": "success", "download_url": f"/api/download/{output_filename}"}
    except Exception as e:
        background_tasks.add_task(delete_file_after_delay, input_path, delay_seconds=5)
        raise HTTPException(status_code=500, detail=f"حدث خطأ: {str(e)}")

@app.post("/api/pdf-to-excel")
async def convert_pdf_to_excel(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    input_path = os.path.join(UPLOAD_DIR, file.filename)
    output_filename = f"excel_{file.filename.replace('.pdf', '.xlsx')}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)
    with open(input_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)

    try:
        process_pdf_adobe_v4(input_path, output_path, ExportPDFTargetFormat.XLSX)
        background_tasks.add_task(delete_file_after_delay, input_path)
        background_tasks.add_task(delete_file_after_delay, output_path)
        return {"status": "success", "download_url": f"/api/download/{output_filename}"}
    except Exception as e:
        background_tasks.add_task(delete_file_after_delay, input_path, delay_seconds=5)
        raise HTTPException(status_code=500, detail=f"حدث خطأ: {str(e)}")

# ---------------------------------------------------------
# 4. مسارات الأدوات المجانية (Ghostscript, qpdf, LibreOffice, Poppler)
# ---------------------------------------------------------
@app.post("/api/compress")
async def compress_pdf(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    input_path = os.path.join(UPLOAD_DIR, f"in_{file.filename}")
    output_filename = f"compressed_{file.filename}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)
    with open(input_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)

    try:
        # استخدام Ghostscript لضغط الملف بجودة مناسبة للشاشات
        cmd = ["gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4", "-dPDFSETTINGS=/screen", "-dNOPAUSE", "-dQUIET", "-dBATCH", f"-sOutputFile={output_path}", input_path]
        subprocess.run(cmd, check=True, capture_output=True)
        
        background_tasks.add_task(delete_file_after_delay, input_path)
        background_tasks.add_task(delete_file_after_delay, output_path)
        return {"status": "success", "download_url": f"/api/download/{output_filename}"}
    except Exception as e:
        background_tasks.add_task(delete_file_after_delay, input_path, delay_seconds=5)
        raise HTTPException(status_code=500, detail="فشل ضغط الملف")

@app.post("/api/protect")
async def protect_pdf(background_tasks: BackgroundTasks, file: UploadFile = File(...), password: str = Form("pdfmasry")):
    input_path = os.path.join(UPLOAD_DIR, f"in_{file.filename}")
    output_filename = f"protected_{file.filename}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)
    with open(input_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)

    try:
        # استخدام qpdf لتشفير الملف (AES-256)
        cmd = ["qpdf", "--encrypt", password, password, "256", "--", input_path, output_path]
        subprocess.run(cmd, check=True, capture_output=True)
        
        background_tasks.add_task(delete_file_after_delay, input_path)
        background_tasks.add_task(delete_file_after_delay, output_path)
        return {"status": "success", "download_url": f"/api/download/{output_filename}"}
    except Exception as e:
        background_tasks.add_task(delete_file_after_delay, input_path, delay_seconds=5)
        raise HTTPException(status_code=500, detail="فشلت حماية الملف")

@app.post("/api/unlock")
async def unlock_pdf(background_tasks: BackgroundTasks, file: UploadFile = File(...), password: str = Form("pdfmasry")):
    input_path = os.path.join(UPLOAD_DIR, f"in_{file.filename}")
    output_filename = f"unlocked_{file.filename}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)
    with open(input_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)

    try:
        # استخدام qpdf لفك التشفير
        cmd = ["qpdf", f"--password={password}", "--decrypt", input_path, output_path]
        subprocess.run(cmd, check=True, capture_output=True)
        
        background_tasks.add_task(delete_file_after_delay, input_path)
        background_tasks.add_task(delete_file_after_delay, output_path)
        return {"status": "success", "download_url": f"/api/download/{output_filename}"}
    except Exception as e:
        background_tasks.add_task(delete_file_after_delay, input_path, delay_seconds=5)
        raise HTTPException(status_code=500, detail="فشل فك الحماية، قد تكون كلمة المرور خاطئة")

@app.post("/api/pdf-to-image")
async def pdf_to_image(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    input_path = os.path.join(UPLOAD_DIR, f"in_{file.filename}")
    folder_name = file.filename.replace('.pdf', '')
    output_folder = os.path.join(UPLOAD_DIR, folder_name)
    os.makedirs(output_folder, exist_ok=True)
    with open(input_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)

    try:
        # استخدام poppler لاستخراج الصفحات كصور عالية الجودة (JPEG)
        cmd = ["pdftoppm", "-jpeg", "-r", "150", input_path, os.path.join(output_folder, "page")]
        subprocess.run(cmd, check=True, capture_output=True)
        
        # ضغط الصور في ملف Zip
        zip_filename = f"{folder_name}_images"
        shutil.make_archive(os.path.join(UPLOAD_DIR, zip_filename), 'zip', output_folder)
        final_zip_name = f"{zip_filename}.zip"

        background_tasks.add_task(delete_file_after_delay, input_path)
        background_tasks.add_task(delete_file_after_delay, output_folder)
        background_tasks.add_task(delete_file_after_delay, os.path.join(UPLOAD_DIR, final_zip_name))
        
        return {"status": "success", "download_url": f"/api/download/{final_zip_name}"}
    except Exception as e:
        background_tasks.add_task(delete_file_after_delay, input_path, delay_seconds=5)
        raise HTTPException(status_code=500, detail="فشل تحويل الملف إلى صور")

@app.post("/api/word-to-pdf")
@app.post("/api/excel-to-pdf")
async def office_to_pdf(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    input_path = os.path.join(UPLOAD_DIR, file.filename)
    output_filename = f"{os.path.splitext(file.filename)[0]}.pdf"
    output_path = os.path.join(UPLOAD_DIR, output_filename)
    with open(input_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)

    try:
        # استخدام LibreOffice لتحويل ملفات الأوفيس إلى PDF مجاناً
        cmd = ["libreoffice", "--headless", "--convert-to", "pdf", input_path, "--outdir", UPLOAD_DIR]
        subprocess.run(cmd, check=True, capture_output=True)
        
        background_tasks.add_task(delete_file_after_delay, input_path)
        background_tasks.add_task(delete_file_after_delay, output_path)
        return {"status": "success", "download_url": f"/api/download/{output_filename}"}
    except Exception as e:
        background_tasks.add_task(delete_file_after_delay, input_path, delay_seconds=5)
        raise HTTPException(status_code=500, detail="فشل تحويل المستند إلى PDF")
