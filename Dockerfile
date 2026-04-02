# استخدام نسخة بايثون خفيفة وسريعة
FROM python:3.10-slim

# إعداد بيئة العمل داخل السيرفر
WORKDIR /app

# تحديث النظام وتثبيت الأدوات المساعدة المجانية (للدمج، التقسيم، واستخراج الصور)
RUN apt-get update && apt-get install -y \
    poppler-utils \
    qpdf \
    ghostscript \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

# نسخ ملف المتطلبات وتثبيت المكتبات
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# نسخ باقي ملفات المشروع إلى السيرفر
COPY . .

# أمر تشغيل سيرفر FastAPI مع توافق تام مع بورتات Railway
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
