FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    qpdf \
    ghostscript \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

CMD gunicorn --bind 0.0.0.0:$PORT main:app
