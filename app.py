from flask import Flask, render_template, request, jsonify, send_from_directory, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
import os

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///mapping.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['DOCUMENT_FOLDER'] = os.path.join(app.root_path, 'documents')
db = SQLAlchemy(app)

class Document(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    unique_id = db.Column(db.String(80), unique=True, nullable=False)
    display_name = db.Column(db.String(200), nullable=True)
    filename = db.Column(db.String(200), nullable=False)

# Ensure DB & folder exist
with app.app_context():
    os.makedirs(app.config['DOCUMENT_FOLDER'], exist_ok=True)
    db.create_all()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/get-document/<string:unique_id>')
def get_document(unique_id):
    doc = Document.query.filter_by(unique_id=unique_id).first()
    if not doc:
        return jsonify({'error': 'Document not found'}), 404
    return jsonify({'imageUrl': f'/documents/{doc.filename}'})

@app.route('/documents/<path:filename>')
def serve_document(filename):
    return send_from_directory(app.config['DOCUMENT_FOLDER'], filename)

@app.route('/admin', methods=['GET', 'POST'])
def admin():
    message = ''
    edit_doc = None

    # Handle POST
    if request.method == 'POST':
        # DELETE action
        if 'delete_id' in request.form:
            did = int(request.form['delete_id'])
            doc = db.session.get(Document, did)
            if doc:
                db.session.delete(doc)
                db.session.commit()
            return redirect(url_for('admin'))

        # CREATE or UPDATE
        uid = request.form.get('unique_id', '').strip()
        name = request.form.get('display_name', '').strip()
        file = request.files.get('file')
        edit_id = request.form.get('doc_id')

        if not uid:
            message = 'Unique ID is required.'
        else:
            # UPDATE existing
            if edit_id:
                doc = db.session.get(Document, int(edit_id))
                if doc:
                    doc.unique_id = uid
                    doc.display_name = name
                    if file:
                        filename = file.filename
                        file.save(os.path.join(app.config['DOCUMENT_FOLDER'], filename))
                        doc.filename = filename
                    db.session.commit()
                    return redirect(url_for('admin'))
                else:
                    message = 'Document not found for editing.'

            # CREATE new
            else:
                if file:
                    filename = file.filename
                    file.save(os.path.join(app.config['DOCUMENT_FOLDER'], filename))
                    new_doc = Document(unique_id=uid, display_name=name, filename=filename)
                    db.session.add(new_doc)
                    db.session.commit()
                    return redirect(url_for('admin'))
                else:
                    message = 'File is required for new upload.'

    # If editing: load that record
    if request.method == 'GET' and 'edit_id' in request.args:
        edit_doc = db.session.get(Document, int(request.args['edit_id']))

    # List all docs
    docs = db.session.execute(db.select(Document).order_by(Document.id.desc())).scalars().all()
    return render_template('admin.html', docs=docs, edit_doc=edit_doc, message=message)

if __name__ == '__main__':
    app.run(debug=True)
