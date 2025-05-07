import os
import io
import base64
import qrcode

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify
)
from flask_sqlalchemy import SQLAlchemy
from google.cloud import storage
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

# GCS setup (public bucket: resultexyx)
GCS_BUCKET_NAME = 'resultexyx'
storage_client  = storage.Client()
bucket          = storage_client.bucket(GCS_BUCKET_NAME)
DB_BLOB         = bucket.blob(DB_FILENAME)
DOC_PREFIX      = 'documents/'

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
    if DB_BLOB.exists():
        DB_BLOB.download_to_filename(LOCAL_DB_PATH)
        app.logger.info("Downloaded mapping.db from GCS")

def upload_db():
    DB_BLOB.upload_from_filename(LOCAL_DB_PATH)
    app.logger.info("Uploaded mapping.db to GCS")

def upload_document(fn):
    local_path = os.path.join(DOC_FOLDER, fn)
    blob = bucket.blob(f"{DOC_PREFIX}{fn}")
    blob.upload_from_filename(local_path)
    app.logger.info(f"Uploaded document {fn} to GCS")

def delete_document_blob(fn):
    blob = bucket.blob(f"{DOC_PREFIX}{fn}")
    blob.delete(if_exists=True)
    app.logger.info(f"Deleted document {fn} from GCS")

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

# ==== PUBLIC ROUTES ====
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/get-document/<string:unique_id>')
def get_document(unique_id):
    download_db()  # Ensure up-to-date DB
    doc = Document.query.filter_by(unique_id=unique_id).first()
    if not doc:
        return jsonify({'error': 'Document not found'}), 404
    # Public URL for a public bucket
    public_url = f"https://storage.googleapis.com/{GCS_BUCKET_NAME}/{DOC_PREFIX}{doc.filename}"
    return jsonify({'imageUrl': public_url})

@app.route('/api/get-qr/<string:unique_id>')
def get_qr(unique_id):
    download_db()
    doc = Document.query.filter_by(unique_id=unique_id).first()
    if not doc or not doc.qr_data:
        return ('', 404)
    qr_img = qrcode.make(doc.qr_data)
    buf = io.BytesIO()
    qr_img.save(buf, format='PNG')
    data_uri = base64.b64encode(buf.getvalue()).decode()
    return jsonify({'qr_png': f"data:image/png;base64,{data_uri}"})

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
    message = ''
    edit_doc = None

    if request.method == 'POST':
        # DELETE
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

        # CREATE / UPDATE
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

    # Search/filter
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
        message=message, search=search
    )

# ==== LOCAL DEV ONLY ====
if __name__ == '__main__':
    app.run(debug=True)
