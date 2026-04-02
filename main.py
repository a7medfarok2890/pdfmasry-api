import os
import shutil
import asyncio
from fastapi import FastAPI, File, UploadFile, BackgroundTasks, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# --- استدعاء مكتبات أدوبي الحديثة (الإصدار الرابع V4) ---
from adobe.pdfservices.operation.auth.service_principal_credentials import ServicePrincipalCredentials
from adobe.pdfservices.operation.pdf_services import PDFServices
from adobe.pdfservices.operation.pdf_services_media_type import PDFServicesMediaType
from adobe.pdfservices.operation.pdfjobs.jobs.export_pdf_job import ExportPDFJob
from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_params import ExportPDFParams
from adobe.pdfservices.operation.pdfjobs.params.export_pdf.export_pdf_target_format import ExportPDFTargetFormat
from adobe.pdfservices.operation.pdfjobs.result.export_pdf_result import ExportPDFResult

app = FastAPI(title="PDFMasry API", version="3.0")

# إعدادات الأمان (CORS) لحماية الباندويث الخاص بك
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

UPLOAD_DIR = "./temp_files"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# دالة الحذف التلقائي للملفات بعد نصف ساعة للحفاظ على المساحة والخصوصية
async def delete_file_after_delay(file_path: str, delay_seconds: int = 1800):
    await asyncio.sleep(delay_seconds)
    if os.path.exists(file_path):
        os.remove(file_path)

# المحرك الأساسي لتحويل الملفات باستخدام Adobe V4
def process_pdf_adobe_v4(input_path: str, output_path: str, target_format):
    client_id = os.getenv("ADOBE_CLIENT_ID")
    client_secret = os.getenv("ADOBE_CLIENT_SECRET")
    
    if not client_id or not client_secret:
        raise Exception("مفاتيح أدوبي غير موجودة في بيئة Railway")
        
    # 1. إعداد الاتصال
    credentials = ServicePrincipalCredentials(client_id=client_id, client_secret=client_secret)
    pdf_services = PDFServices(credentials=credentials)
    
    # 2. قراءة الملف ورفعه
    with open(input_path, 'rb') as f:
        input_stream = f.read()
    input_asset = pdf_services.upload(input_stream=input_stream, mime_type=PDFServicesMediaType.PDF)
    
    # 3. تجهيز المهمة
    export_pdf_params = ExportPDFParams(target_format=target_format)
    export_pdf_job = ExportPDFJob(input_asset=input_asset, export_pdf_params=export_pdf_params)
    
    # 4. تنفيذ المهمة وجلب النتيجة
    location = pdf_services.submit(export_pdf_job)
    pdf_services_response = pdf_services.get_job_result(location, ExportPDFResult)
    
    # 5. تحميل الملف الناتج
    result_asset = pdf_services_response.get_result().get_asset()
    stream_asset = pdf_services.get_content(result_asset)
    
    # 6. حفظ الملف في السيرفر
    with open(output_path, "wb") as output_file:
        output_file.write(stream_asset.get_input_stream())

@app.get("/")
def health_check():
    return {"status": "PDFMasry API is running fast with Adobe V4 Engine!"}

@app.get("/api/download/{filename}")
def download_file(filename: str):
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    raise HTTPException(status_code=404, detail="عذراً، انتهت صلاحية الرابط وتم حذف الملف")

@app.post("/api/pdf-to-word")
async def convert_pdf_to_word(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="يجب رفع ملف PDF")

    input_path = os.path.join(UPLOAD_DIR, file.filename)
    output_filename = f"word_{file.filename.replace('.pdf', '.docx')}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    with open(input_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

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
    if not file.filename.lower().endswith('.pdf'):
        raise HTTPException(status_code=400, detail="يجب رفع ملف PDF")

    input_path = os.path.join(UPLOAD_DIR, file.filename)
    output_filename = f"excel_{file.filename.replace('.pdf', '.xlsx')}"
    output_path = os.path.join(UPLOAD_DIR, output_filename)

    with open(input_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    try:
        process_pdf_adobe_v4(input_path, output_path, ExportPDFTargetFormat.XLSX)
        background_tasks.add_task(delete_file_after_delay, input_path)
        background_tasks.add_task(delete_file_after_delay, output_path)
        return {"status": "success", "download_url": f"/api/download/{output_filename}"}
    except Exception as e:
        background_tasks.add_task(delete_file_after_delay, input_path, delay_seconds=5)
        raise HTTPException(status_code=500, detail=f"حدث خطأ: {str(e)}")
