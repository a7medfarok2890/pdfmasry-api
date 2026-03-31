from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import subprocess
import os
import tempfile
import uuid

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = '/tmp/pdfmasry'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_temp_path(ext='pdf'):
    return os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")

# ── PROTECT PDF ──────────────────────────────────────────
@app.route('/protect', methods=['POST'])
def protect():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    password = request.form.get('password', '')
    if not password:
        return jsonify({'error': 'No password'}), 400

    original_name = os.path.splitext(file.filename)[0]
    input_path = get_temp_path()
    output_path = get_temp_path()
    file.save(input_path)

    result = subprocess.run([
        'qpdf', '--encrypt', password, password, '256', '--', input_path, output_path
    ], capture_output=True)

    if result.returncode != 0:
        return jsonify({'error': result.stderr.decode()}), 500

    return send_file(output_path, as_attachment=True,
                     download_name=f"{original_name}-protected.pdf",
                     mimetype='application/pdf')

# ── UNLOCK PDF ────────────────────────────────────────────
@app.route('/unlock', methods=['POST'])
def unlock():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    password = request.form.get('password', '')

    original_name = os.path.splitext(file.filename)[0]
    input_path = get_temp_path()
    output_path = get_temp_path()
    file.save(input_path)

    result = subprocess.run([
        'qpdf', '--decrypt', f'--password={password}', input_path, output_path
    ], capture_output=True)

    if result.returncode != 0:
        err = result.stderr.decode()
        if 'invalid password' in err.lower():
            return jsonify({'error': 'كلمة المرور خاطئة'}), 400
        return jsonify({'error': err}), 500

    return send_file(output_path, as_attachment=True,
                     download_name=f"{original_name}-unlocked.pdf",
                     mimetype='application/pdf')

# ── PDF TO WORD ───────────────────────────────────────────
@app.route('/pdf-to-word', methods=['POST'])
def pdf_to_word():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    original_name = os.path.splitext(file.filename)[0]
    input_path = get_temp_path()
    file.save(input_path)

    out_dir = UPLOAD_FOLDER
    result = subprocess.run([
        'libreoffice', '--headless', '--convert-to', 'docx',
        '--outdir', out_dir, input_path
    ], capture_output=True)

    output_path = input_path.replace('.pdf', '.docx')
    if not os.path.exists(output_path):
        return jsonify({'error': 'Conversion failed'}), 500

    return send_file(output_path, as_attachment=True,
                     download_name=f"{original_name}.docx",
                     mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')

# ── WORD TO PDF ───────────────────────────────────────────
@app.route('/word-to-pdf', methods=['POST'])
def word_to_pdf():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    original_name = os.path.splitext(file.filename)[0]
    ext = file.filename.rsplit('.', 1)[-1].lower()
    input_path = get_temp_path(ext)
    file.save(input_path)

    out_dir = UPLOAD_FOLDER
    result = subprocess.run([
        'libreoffice', '--headless', '--convert-to', 'pdf',
        '--outdir', out_dir, input_path
    ], capture_output=True)

    output_path = input_path.rsplit('.', 1)[0] + '.pdf'
    if not os.path.exists(output_path):
        return jsonify({'error': 'Conversion failed'}), 500

    return send_file(output_path, as_attachment=True,
                     download_name=f"{original_name}.pdf",
                     mimetype='application/pdf')

# ── COMPRESS PDF (high quality) ───────────────────────────
@app.route('/compress', methods=['POST'])
def compress():
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    original_name = os.path.splitext(file.filename)[0]
    input_path = get_temp_path()
    output_path = get_temp_path()
    file.save(input_path)

    result = subprocess.run([
        'gs', '-sDEVICE=pdfwrite', '-dCompatibilityLevel=1.4',
        '-dPDFSETTINGS=/ebook', '-dNOPAUSE', '-dQUIET', '-dBATCH',
        f'-sOutputFile={output_path}', input_path
    ], capture_output=True)

    if not os.path.exists(output_path):
        return jsonify({'error': 'Compression failed'}), 500

    return send_file(output_path, as_attachment=True,
                     download_name=f"{original_name}-compressed.pdf",
                     mimetype='application/pdf')

# ── HEALTH CHECK ──────────────────────────────────────────
@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'pdfmasry API running ✅'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
