import os
import io
import base64
import qrcode

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_file
)
from flask_sqlalchemy import SQLAlchemy
from google.cloud import storage
from google.api_core.exceptions import NotFound
from werkzeug.utils import secure_filename

# ==== CONFIG ====
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

# SQLite DB file locally
DB_FILENAME   = 'mapping.db'
LOCAL_DB_PATH = os.path.join(app.root_path, DB_FILENAME)
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{LOCAL_DB_PATH}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Documents folder locally (for temp uploads)
DOC_FOLDER = os.path.join(app.root_path, 'documents')
os.makedirs(DOC_FOLDER, exist_ok=True)
app.config['DOCUMENT_FOLDER'] = DOC_FOLDER

# GCS setup (public bucket: resultxxxx)
GCS_BUCKET_NAME = 'resultxxxx'
DOC_PREFIX      = 'documents/'
storage_client  = None
bucket          = None
DB_BLOB         = None

try:
    storage_client = storage.Client()
    bucket         = storage_client.bucket(GCS_BUCKET_NAME)
    DB_BLOB        = bucket.blob(DB_FILENAME)
except Exception as e:
    app.logger.warning(f"GCS client init note: {e}")

db = SQLAlchemy(app)

# ==== MODEL ====
class Document(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    unique_id    = db.Column(db.String(80), unique=True, nullable=False)
    display_name = db.Column(db.String(200), nullable=True)
    filename     = db.Column(db.String(200), nullable=False)
    qr_data      = db.Column(db.Text, nullable=True)

# ==== GCS SYNC HELPERS ====
def download_db():
    if DB_BLOB:
        try:
            if DB_BLOB.exists():
                DB_BLOB.download_to_filename(LOCAL_DB_PATH)
                app.logger.info("Downloaded mapping.db from GCS")
        except Exception as e:
            app.logger.warning(f"Could not download DB from GCS: {e}")

def upload_db():
    if DB_BLOB:
        try:
            DB_BLOB.upload_from_filename(LOCAL_DB_PATH)
            app.logger.info("Uploaded mapping.db to GCS")
        except Exception as e:
            app.logger.warning(f"Could not upload DB to GCS: {e}")

def upload_document(fn):
    if bucket:
        try:
            local_path = os.path.join(DOC_FOLDER, fn)
            blob = bucket.blob(f"{DOC_PREFIX}{fn}")
            blob.upload_from_filename(local_path)
            app.logger.info(f"Uploaded document {fn} to GCS")
        except Exception as e:
            app.logger.warning(f"Could not upload document {fn} to GCS: {e}")

def delete_document_blob(fn):
    if bucket:
        try:
            blob = bucket.blob(f"{DOC_PREFIX}{fn}")
            blob.delete()
            app.logger.info(f"Deleted document {fn} from GCS")
        except NotFound:
            app.logger.warning(f"Document {fn} not found in GCS during delete")
        except Exception as e:
            app.logger.warning(f"Could not delete document {fn} from GCS: {e}")

# ==== INITIAL SYNC ON STARTUP ====
with app.app_context():
    download_db()
    db.create_all()
    upload_db()

# ==== AUTH DECORATOR ====
def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper

# ==== HELPERS ====
def get_base_url():
    base = os.environ.get('BASE_URL')
    if base:
        return base.rstrip('/')
    proto = request.headers.get('X-Forwarded-Proto', request.scheme)
    host = request.headers.get('X-Forwarded-Host', request.host)
    return f"{proto}://{host}"

# ==== PUBLIC ROUTES ====
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/documents/<path:filename>')
def serve_document(filename):
    # 1. Check local documents directory
    local_path = os.path.join(DOC_FOLDER, filename)
    if os.path.exists(local_path):
        return send_file(local_path)

    # 2. Proxy/stream directly from GCS so storage.googleapis.com is NEVER shown
    if bucket:
        try:
            blob = bucket.blob(f"{DOC_PREFIX}{filename}")
            if blob.exists():
                buf = io.BytesIO()
                blob.download_to_file(buf)
                buf.seek(0)
                # Cache locally for high performance
                try:
                    with open(local_path, 'wb') as f:
                        f.write(buf.getvalue())
                except Exception:
                    pass
                content_type = blob.content_type or 'image/jpeg'
                return send_file(buf, mimetype=content_type)
        except Exception as e:
            app.logger.warning(f"Error streaming document from GCS: {e}")

    # 3. If file not found in local storage or GCS, return 404 on website
    return ("Document image file not found on server.", 404)

@app.route('/document/<string:unique_id>')
def view_document_direct(unique_id):
    download_db()
    doc = Document.query.filter_by(unique_id=unique_id).first()
    if not doc:
        return jsonify({'error': 'Document not found'}), 404
    return redirect(url_for('serve_document', filename=doc.filename))

@app.route('/api/get-document/<string:unique_id>')
def get_document(unique_id):
    download_db()
    doc = Document.query.filter_by(unique_id=unique_id).first()
    if not doc:
        return jsonify({'error': 'Document not found'}), 404
    doc_url = f"{get_base_url()}/documents/{doc.filename}"
    return jsonify({'imageUrl': doc_url})

@app.route('/api/get-qr/<string:unique_id>')
def get_qr(unique_id):
    download_db()
    doc = Document.query.filter_by(unique_id=unique_id).first()
    if not doc:
        return ('', 404)

    # When scanned with phone camera, direct to your website URL: https://<domain>/documents/<filename>
    if doc.qr_data and (doc.qr_data.startswith('http://') or doc.qr_data.startswith('https://')):
        target_url = doc.qr_data
    else:
        target_url = f"{get_base_url()}/documents/{doc.filename}"

    qr_img = qrcode.make(target_url)
    buf = io.BytesIO()
    qr_img.save(buf, format='PNG')
    data_uri = base64.b64encode(buf.getvalue()).decode()
    return jsonify({
        'qr_png': f"data:image/png;base64,{data_uri}",
        'target_url': target_url
    })

@app.route('/qr/<string:unique_id>.png')
def serve_qr_png(unique_id):
    download_db()
    doc = Document.query.filter_by(unique_id=unique_id).first()
    if not doc:
        return ('Document not found', 404)

    if doc.qr_data and (doc.qr_data.startswith('http://') or doc.qr_data.startswith('https://')):
        target_url = doc.qr_data
    else:
        target_url = f"{get_base_url()}/documents/{doc.filename}"

    qr_img = qrcode.make(target_url)
    buf = io.BytesIO()
    qr_img.save(buf, format='PNG')
    buf.seek(0)
    return send_file(
        buf,
        mimetype='image/png',
        as_attachment=('download' in request.args),
        download_name=f"{doc.unique_id}_qr.png"
    )

# ==== AUTH ROUTES ====
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if (request.form['username'] == 'superadmin' and
            request.form['password'] == '88ayWHVAheuc'):
            session['logged_in'] = True
            return redirect(url_for('admin'))
        flash('Invalid credentials', 'error')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))

# ==== ADMIN PANEL ====
@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin():
    message = request.args.get('message', '')
    edit_doc = None
    generated_doc = None

    if 'generated_id' in request.args:
        generated_doc = Document.query.filter_by(unique_id=request.args['generated_id']).first()

    if request.method == 'POST':
        # 1. QUICK IMAGE TO QR GENERATOR (Upload any image -> get scanable QR)
        if 'quick_upload' in request.form:
            f = request.files.get('quick_image')
            if not f or not f.filename:
                message = 'Please select an image file to upload.'
            else:
                custom_id = request.form.get('quick_id', '').strip()
                if custom_id:
                    uid = custom_id
                    existing = Document.query.filter_by(unique_id=uid).first()
                    if existing:
                        return redirect(url_for('admin', message=f'Unique ID "{uid}" already exists. Please choose a different ID or leave blank to auto-generate.'))
                else:
                    import random
                    uid = str(random.randint(1000000, 9999999))
                    while Document.query.filter_by(unique_id=uid).first():
                        uid = str(random.randint(1000000, 9999999))

                title = request.form.get('quick_title', '').strip() or f"Document {uid}"
                raw_fn = secure_filename(f.filename) or f"{uid}.jpg"

                # Check filename collisions
                fn = raw_fn
                if os.path.exists(os.path.join(DOC_FOLDER, fn)):
                    fn = f"{uid}_{raw_fn}"

                local_path = os.path.join(DOC_FOLDER, fn)
                f.save(local_path)
                upload_document(fn)

                new_doc = Document(
                    unique_id=uid,
                    display_name=title,
                    filename=fn,
                    qr_data=None  # Auto-points to: https://<domain>/documents/<filename>
                )
                db.session.add(new_doc)
                db.session.commit()
                upload_db()
                return redirect(url_for('admin', generated_id=uid))

        # 2. DELETE
        if 'delete_id' in request.form:
            doc = db.session.get(Document, int(request.form['delete_id']))
            if doc:
                try:
                    os.remove(os.path.join(DOC_FOLDER, doc.filename))
                except FileNotFoundError:
                    pass
                delete_document_blob(doc.filename)
                db.session.delete(doc)
                db.session.commit()
                upload_db()
            return redirect(url_for('admin'))

        # 3. CREATE / UPDATE (MANUAL STUDENT RECORD)
        uid     = request.form.get('unique_id','').strip()
        name    = request.form.get('display_name','').strip()
        qrdata  = request.form.get('qr_data','').strip()
        f       = request.files.get('file')
        edit_id = request.form.get('doc_id')

        if not uid:
            message = 'Unique ID is required.'
        else:
            if edit_id:
                doc = db.session.get(Document, int(edit_id))
                if doc:
                    doc.unique_id    = uid
                    doc.display_name = name
                    doc.qr_data      = qrdata
                    if f:
                        fn = secure_filename(f.filename)
                        f.save(os.path.join(DOC_FOLDER, fn))
                        delete_document_blob(doc.filename)
                        upload_document(fn)
                        doc.filename = fn
                    db.session.commit()
                    upload_db()
                    return redirect(url_for('admin'))
                else:
                    message = 'Document not found for editing.'
            else:
                if not f:
                    message = 'File is required for new upload.'
                else:
                    # Check if unique_id already exists
                    existing_doc = Document.query.filter_by(unique_id=uid).first()
                    if existing_doc:
                        message = f'Unique ID "{uid}" already exists. Please use a different ID or edit the existing document.'
                    else:
                        fn = secure_filename(f.filename)
                        f.save(os.path.join(DOC_FOLDER, fn))
                        upload_document(fn)
                        new_doc = Document(
                            unique_id=uid,
                            display_name=name,
                            filename=fn,
                            qr_data=qrdata
                        )
                        db.session.add(new_doc)
                        db.session.commit()
                        upload_db()
                        return redirect(url_for('admin'))

    if 'edit_id' in request.args:
        edit_doc = db.session.get(Document, int(request.args['edit_id']))

    search = request.args.get('search','').strip()
    query  = Document.query
    if search:
        like = f"%{search}%"
        query = query.filter(
            db.or_(
                Document.unique_id.ilike(like),
                Document.display_name.ilike(like)
            )
        )
    docs = query.order_by(Document.id.desc()).all()

    return render_template('admin.html',
        docs=docs, edit_doc=edit_doc,
        generated_doc=generated_doc,
        message=message, search=search
    )

# ==== LOCAL DEV ONLY ====
if __name__ == '__main__':
    app.run(debug=True)
