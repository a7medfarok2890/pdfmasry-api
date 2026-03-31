from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import subprocess
import os
import uuid
import zipfile
import glob

app = Flask(__name__)
CORS(app)

UPLOAD_FOLDER = '/tmp/pdfmasry'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def get_temp_path(ext='pdf'):
    return os.path.join(UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")

def cleanup(*paths):
    for p in paths:
        try:
            if os.path.exists(p): os.remove(p)
        except: pass

@app.route('/', methods=['GET'])
def health():
    return jsonify({'status': 'pdfmasry API running ✅'})

@app.route('/protect', methods=['POST'])
def protect():
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    password = request.form.get('password', '')
    if not password: return jsonify({'error': 'No password'}), 400
    original_name = os.path.splitext(file.filename)[0]
    input_path = get_temp_path(); output_path = get_temp_path()
    file.save(input_path)
    result = subprocess.run(['qpdf','--encrypt',password,password,'256','--',input_path,output_path], capture_output=True)
    if result.returncode != 0:
        cleanup(input_path, output_path)
        return jsonify({'error': result.stderr.decode()}), 500
    @after_this_request
    def rm(r): cleanup(input_path, output_path); return r
    return send_file(output_path, as_attachment=True, download_name=f"{original_name}-protected.pdf", mimetype='application/pdf')

@app.route('/unlock', methods=['POST'])
def unlock():
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    password = request.form.get('password', '')
    original_name = os.path.splitext(file.filename)[0]
    input_path = get_temp_path(); output_path = get_temp_path()
    file.save(input_path)
    result = subprocess.run(['qpdf','--decrypt',f'--password={password}',input_path,output_path], capture_output=True)
    if result.returncode != 0:
        err = result.stderr.decode()
        cleanup(input_path, output_path)
        if 'invalid password' in err.lower(): return jsonify({'error': 'كلمة المرور خاطئة'}), 400
        return jsonify({'error': err}), 500
    @after_this_request
    def rm(r): cleanup(input_path, output_path); return r
    return send_file(output_path, as_attachment=True, download_name=f"{original_name}-unlocked.pdf", mimetype='application/pdf')

@app.route('/pdf-to-image', methods=['POST'])
def pdf_to_image():
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    original_name = os.path.splitext(file.filename)[0]
    dpi = request.form.get('dpi', '150')
    input_path = get_temp_path()
    file.save(input_path)
    output_prefix = os.path.join(UPLOAD_FOLDER, str(uuid.uuid4()))
    subprocess.run(['pdftoppm', '-jpeg', '-r', dpi, input_path, output_prefix], capture_output=True)
    images = sorted(glob.glob(f"{output_prefix}*.jpg") + glob.glob(f"{output_prefix}*.jpeg") + glob.glob(f"{output_prefix}*.ppm"))
    if not images:
        cleanup(input_path)
        return jsonify({'error': 'No images generated'}), 500
    if len(images) == 1:
        img_path = images[0]
        @after_this_request
        def rm(r): cleanup(input_path, img_path); return r
        return send_file(img_path, as_attachment=True, download_name=f"{original_name}-page1.jpg", mimetype='image/jpeg')
    zip_path = get_temp_path('zip')
    with zipfile.ZipFile(zip_path, 'w') as zf:
        for i, img in enumerate(images, 1):
            zf.write(img, f"{original_name}-page{i}.jpg")
    @after_this_request
    def rm(r): cleanup(input_path, zip_path, *images); return r
    return send_file(zip_path, as_attachment=True, download_name=f"{original_name}-images.zip", mimetype='application/zip')

@app.route('/pdf-to-text', methods=['POST'])
def pdf_to_text():
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    input_path = get_temp_path(); output_path = get_temp_path('txt')
    file.save(input_path)
    result = subprocess.run(['pdftotext', '-enc', 'UTF-8', input_path, output_path], capture_output=True)
    if not os.path.exists(output_path):
        cleanup(input_path)
        return jsonify({'error': 'Failed to extract text'}), 500
    with open(output_path, 'r', encoding='utf-8', errors='ignore') as f:
        text = f.read()
    cleanup(input_path, output_path)
    if not text.strip():
        return jsonify({'error': 'الملف لا يحتوي على نص قابل للاستخراج'}), 400
    return jsonify({'text': text})

@app.route('/pdf-to-word', methods=['POST'])
def pdf_to_word():
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    original_name = os.path.splitext(file.filename)[0]
    input_path = get_temp_path()
    file.save(input_path)
    subprocess.run(['libreoffice','--headless','--convert-to','docx','--outdir',UPLOAD_FOLDER,input_path], capture_output=True, timeout=120)
    output_path = input_path.replace('.pdf', '.docx')
    if not os.path.exists(output_path):
        cleanup(input_path)
        return jsonify({'error': 'Conversion failed'}), 500
    @after_this_request
    def rm(r): cleanup(input_path, output_path); return r
    return send_file(output_path, as_attachment=True, download_name=f"{original_name}.docx", mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document')

@app.route('/word-to-pdf', methods=['POST'])
def word_to_pdf():
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    original_name = os.path.splitext(file.filename)[0]
    ext = file.filename.rsplit('.', 1)[-1].lower()
    input_path = get_temp_path(ext)
    file.save(input_path)
    subprocess.run(['libreoffice','--headless','--convert-to','pdf','--outdir',UPLOAD_FOLDER,input_path], capture_output=True, timeout=120)
    output_path = input_path.rsplit('.', 1)[0] + '.pdf'
    if not os.path.exists(output_path):
        cleanup(input_path)
        return jsonify({'error': 'Conversion failed'}), 500
    @after_this_request
    def rm(r): cleanup(input_path, output_path); return r
    return send_file(output_path, as_attachment=True, download_name=f"{original_name}.pdf", mimetype='application/pdf')

@app.route('/compress', methods=['POST'])
def compress():
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    original_name = os.path.splitext(file.filename)[0]
    input_path = get_temp_path(); output_path = get_temp_path()
    file.save(input_path)
    subprocess.run(['gs','-sDEVICE=pdfwrite','-dCompatibilityLevel=1.4','-dPDFSETTINGS=/ebook','-dNOPAUSE','-dQUIET','-dBATCH',f'-sOutputFile={output_path}',input_path], capture_output=True)
    if not os.path.exists(output_path):
        cleanup(input_path, output_path)
        return jsonify({'error': 'Compression failed'}), 500
    @after_this_request
    def rm(r): cleanup(input_path, output_path); return r
    return send_file(output_path, as_attachment=True, download_name=f"{original_name}-compressed.pdf", mimetype='application/pdf')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
