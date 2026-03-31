FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    qpdf \
    ghostscript \
    libreoffice \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 5000
CMD gunicorn --bind 0.0.0.0:$PORT --timeout 120 main:app
