import os
import io
import base64
import qrcode
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.secret_key = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///mapping.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['DOCUMENT_FOLDER'] = os.path.join(app.root_path, 'documents')
db = SQLAlchemy(app)


class Document(db.Model):
    id           = db.Column(db.Integer, primary_key=True)
    unique_id    = db.Column(db.String(80), unique=True, nullable=False)
    display_name = db.Column(db.String(200), nullable=True)
    filename     = db.Column(db.String(200), nullable=False)
    qr_data      = db.Column(db.Text, nullable=True)


# Initialize DB & folders
with app.app_context():
    os.makedirs(app.config['DOCUMENT_FOLDER'], exist_ok=True)
    db.create_all()


# Simple login decorator
def login_required(fn):
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return fn(*args, **kwargs)
    return wrapper


# --- Public Routes ---

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/get-document/<string:unique_id>')
def get_document(unique_id):
    doc = Document.query.filter_by(unique_id=unique_id).first()
    if not doc:
        return jsonify({'error': 'Document not found'}), 404
    return jsonify({'imageUrl': url_for('serve_document', filename=doc.filename)})


@app.route('/documents/<path:filename>')
def serve_document(filename):
    return send_from_directory(app.config['DOCUMENT_FOLDER'], filename)


@app.route('/api/get-qr/<string:unique_id>')
def get_qr(unique_id):
    doc = Document.query.filter_by(unique_id=unique_id).first()
    if not doc or not doc.qr_data:
        return ('', 404)
    # generate QR as PNG in-memory
    qr_img = qrcode.make(doc.qr_data)
    buf = io.BytesIO()
    qr_img.save(buf, format='PNG')
    data_uri = base64.b64encode(buf.getvalue()).decode()
    return jsonify({'qr_png': f"data:image/png;base64,{data_uri}"})


# --- Auth Routes ---

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


# --- Admin Panel ---

@app.route('/admin', methods=['GET', 'POST'])
@login_required
def admin():
    message = ''
    edit_doc = None

    if request.method == 'POST':
        # Delete
        if 'delete_id' in request.form:
            doc = db.session.get(Document, int(request.form['delete_id']))
            if doc:
                db.session.delete(doc)
                db.session.commit()
            return redirect(url_for('admin'))

        # Create / Update
        uid     = request.form.get('unique_id','').strip()
        name    = request.form.get('display_name','').strip()
        qrdata  = request.form.get('qr_data','').strip()
        f       = request.files.get('file')
        edit_id = request.form.get('doc_id')

        if not uid:
            message = 'Unique ID is required.'
        else:
            # UPDATE
            if edit_id:
                doc = db.session.get(Document, int(edit_id))
                if doc:
                    doc.unique_id    = uid
                    doc.display_name = name
                    doc.qr_data      = qrdata
                    if f:
                        fn = f.filename
                        f.save(os.path.join(app.config['DOCUMENT_FOLDER'], fn))
                        doc.filename = fn
                    db.session.commit()
                    return redirect(url_for('admin'))
                else:
                    message = 'Document not found for editing.'
            # CREATE
            else:
                if not f:
                    message = 'File is required for new upload.'
                else:
                    fn = f.filename
                    f.save(os.path.join(app.config['DOCUMENT_FOLDER'], fn))
                    new_doc = Document(
                        unique_id=uid,
                        display_name=name,
                        filename=fn,
                        qr_data=qrdata
                    )
                    db.session.add(new_doc)
                    db.session.commit()
                    return redirect(url_for('admin'))

    # GET — check edit_id
    if request.method == 'GET' and 'edit_id' in request.args:
        edit_doc = db.session.get(Document, int(request.args['edit_id']))

    # GET — optional search
    search = request.args.get('search','').strip()
    query = Document.query
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


if __name__ == '__main__':
    app.run(debug=True)
