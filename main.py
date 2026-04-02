import os
import shutil
import asyncio
from fastapi import FastAPI, File, UploadFile, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# مكتبات أدوبي
from adobe.pdfservices.operation.auth.credentials import Credentials
from adobe.pdfservices.operation.execution_context import ExecutionContext
from adobe.pdfservices.operation.io.file_ref import FileRef
from adobe.pdfservices.operation.pdfops.export_pdf_operation import ExportPDFOperation
from adobe.pdfservices.operation.pdfops.options.exportpdf.export_pdf_target_format import ExportPDFTargetFormat

app = FastAPI(title="PDFMasry API", version="2.0")

# إعدادات الأمان (CORS) - لحماية السيرفر من الاستخدام الخارجي
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pdfmasry.com", 
        "https://www.pdfmasry.com", 
        "https://taupe-rugelach-921837.netlify.app"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# مجلد الملفات المؤقتة
UPLOAD_DIR = "./temp_files"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# 1. دالة حذف الملفات للتنظيف التلقائي (حماية خصوصية المستخدمين)
async def delete_file_after_delay(file_path: str, delay_seconds: int = 1800):
    """حذف الملفات بعد 30 دقيقة"""
    await asyncio.sleep(delay_seconds)
    if os.path.exists(file_path):
        os.remove(file_path)

# 2. دالة جلب مفاتيح أدوبي من إعدادات Railway
def get_adobe_credentials():
    client_id = os.getenv("ADOBE_CLIENT_ID")
    client_secret = os.getenv("ADOBE_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        raise HTTPException(status_code=500, detail="مفاتيح أدوبي غير مجهزة في السيرفر")
        
    return Credentials.service_principal_credentials_builder() \
        .with_client_id(client_id) \
        .with_client_secret(client_secret) \
        .build()

# 3. مسار فحص صحة السيرفر
@app.get("/")
def health_check():
    return {"status": "PDFMasry API is running fast and secure!"}

# 4. مسار تحميل الملفات بعد المعالجة
@app.get("/api/download/{filename}")
def download_file(filename: str):
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    raise HTTPException(status_code=404, detail="الملف غير موجود أو انتهت صلاحيته وتم حذفه")

# 5. أداة تحويل PDF إلى Word
@app.post("/api/pdf-to-word")
async def convert_pdf_to_word(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="يجب رفع ملف بصيغة PDF")

    input_path = os.path.join(UPLOAD_DIR, file.filename)
    output_filename = f"word_{file.filename.replace('.pdf', '.docx')}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    with open(input_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        credentials = get_adobe_credentials()
        execution_context = ExecutionContext.create(credentials)
        export_pdf_operation = ExportPDFOperation.create_new(ExportPDFTargetFormat.DOCX)
        
        source_file_ref = FileRef.create_from_local_file(input_path)
        export_pdf_operation.set_input(source_file_ref)
        result = export_pdf_operation.execute(execution_context)
        result.save_as(output_path)

        background_tasks.add_task(delete_file_after_delay, input_path)
        background_tasks.add_task(delete_file_after_delay, output_path)

        return {"status": "success", "download_url": f"/api/download/{output_filename}"}

    except Exception as e:
        background_tasks.add_task(delete_file_after_delay, input_path, delay_seconds=5)
        raise HTTPException(status_code=500, detail=f"خطأ أثناء التحويل: {str(e)}")

# 6. أداة تحويل PDF إلى Excel (لجداول المحاسبة)
@app.post("/api/pdf-to-excel")
async def convert_pdf_to_excel(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="يجب رفع ملف بصيغة PDF")

    input_path = os.path.join(UPLOAD_DIR, file.filename)
    output_filename = f"excel_{file.filename.replace('.pdf', '.xlsx')}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    with open(input_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        credentials = get_adobe_credentials()
        execution_context = ExecutionContext.create(credentials)
        export_pdf_operation = ExportPDFOperation.create_new(ExportPDFTargetFormat.XLSX)
        
        source_file_ref = FileRef.create_from_local_file(input_path)
        export_pdf_operation.set_input(source_file_ref)
        result = export_pdf_operation.execute(execution_context)
        result.save_as(output_path)

        background_tasks.add_task(delete_file_after_delay, input_path)
        background_tasks.add_task(delete_file_after_delay, output_path)

        return {"status": "success", "download_url": f"/api/download/{output_filename}"}

    except Exception as e:
        background_tasks.add_task(delete_file_after_delay, input_path, delay_seconds=5)
        raise HTTPException(status_code=500, detail=f"خطأ أثناء التحويل: {str(e)}")
