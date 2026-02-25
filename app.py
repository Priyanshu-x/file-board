import os
import uuid
import shutil
import logging
from datetime import datetime, timedelta
from flask import Flask, request, render_template, send_file, redirect, url_for, flash
from flask_wtf import FlaskForm
from wtforms import FileField, SubmitField, StringField, PasswordField
from wtforms.validators import DataRequired, Length
from werkzeug.utils import secure_filename
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO, emit
from flask_wtf.csrf import CSRFProtect
from dotenv import load_dotenv
from flask_sqlalchemy import SQLAlchemy
from flask_moment import Moment

load_dotenv()

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'instance/uploads'
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'super-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///files.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)
moment = Moment(app)
csrf = CSRFProtect(app)
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins="*")
login_manager = LoginManager(app)
login_manager.login_view = 'admin_login'

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
logger.info(f"SECRET_KEY loaded: {'*****' if app.config['SECRET_KEY'] else 'Not Set (using default)'}")
logger.info(f"DATABASE_URL loaded: {app.config['SQLALCHEMY_DATABASE_URI']}")

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ADMIN_USER = os.getenv('ADMIN_USER', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASS', 'admin123')

limiter = Limiter(app=app, key_func=get_remote_address, default_limits=["200 per day", "50 per hour"], storage_uri="redis://redis:6379" if os.getenv('REDIS_URL') else "memory://")

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class File(db.Model):
    id = db.Column(db.String, primary_key=True)
    filename = db.Column(db.String, nullable=False)
    upload_time = db.Column(db.String, nullable=False)
    is_permanent = db.Column(db.Integer, default=0)

    def __repr__(self):
        return f'<File {self.filename}>'

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def delete_expired_files():
    with app.app_context():
        ten_minutes_ago = datetime.now() - timedelta(minutes=15)
        expired_files = File.query.filter(File.upload_time < ten_minutes_ago.isoformat(), File.is_permanent == 0).all()
        for file_obj in expired_files:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_obj.id)
            if os.path.exists(file_path):
                os.remove(file_path)
            db.session.delete(file_obj)
            db.session.commit() # Commit deletion before emitting to ensure consistency
            socketio.emit('file_deleted', {'id': file_obj.id})

scheduler = BackgroundScheduler()
scheduler.add_job(delete_expired_files, 'interval', minutes=1)
# Removed scheduler.start() from here

with app.app_context():
    try:
        db.create_all()
        # Create Admin User
        if not User.query.filter_by(username=ADMIN_USER).first():
            admin = User(username=ADMIN_USER)
            admin.set_password(ADMIN_PASS)
            db.session.add(admin)
            db.session.commit()
            logger.info(f"Admin user '{ADMIN_USER}' created/verified.")
        logger.info("Database tables created successfully.")
        
        # Start scheduler after DB is ready
        if not scheduler.running:
            scheduler.start()
            logger.info("Background scheduler started.")
            
    except Exception as e:
        logger.error(f"Error creating database tables or admin user: {e}", exc_info=True)

class UploadForm(FlaskForm):
    submit = SubmitField('Upload')

class AdminLoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=4, max=20)])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6, max=100)])
    submit = SubmitField('Login')

@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(e):
    logger.warning(f"Request entity too large: {e}")
    flash('File too large. Maximum size is 1GB.')
    return redirect(url_for('index'))

@app.errorhandler(429)
def ratelimit_handler(e):
    logger.warning(f"Rate limit exceeded for IP: {get_remote_address()}")
    flash('Too many uploads. Please try again in a minute.')
    return redirect(url_for('index'))

@app.errorhandler(404)
def page_not_found(e):
    logger.warning(f"404 Not Found: {request.path}")
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_server_error(e):
    logger.error(f"500 Internal Server Error: {e}", exc_info=True)
    return render_template('500.html'), 500

@app.route('/')
def index():
    ten_minutes_ago = (datetime.now() - timedelta(minutes=10)).isoformat()
    files = File.query.filter((File.is_permanent == 1) | (File.upload_time > ten_minutes_ago)).all()
    # Convert upload_time strings to datetime objects for Flask-Moment
    for file_obj in files:
        file_obj.upload_time = datetime.fromisoformat(file_obj.upload_time)
    return render_template('index.html', files=files, form=UploadForm())

@app.route('/upload', methods=['POST'])
@limiter.limit("5 per minute")
def upload_file():
    if 'files[]' not in request.files:
        logger.warning('No files part in the request.')
        flash('No files selected')
        return redirect(url_for('index'))
    
    files = request.files.getlist('files[]')
    if not files or all(f.filename == '' for f in files):
        logger.warning('No selected file for upload.')
        flash('No file selected')
        return redirect(url_for('index'))

    MIN_FREE_SPACE_GB = int(os.getenv('MIN_FREE_SPACE_GB', 2)) # Default to 2 GB
    total, used, free = shutil.disk_usage(app.config['UPLOAD_FOLDER'])
    if free < MIN_FREE_SPACE_GB * 1024 * 1024 * 1024:
        logger.error(f'Server storage low. Free space: {free / (1024**3):.2f} GB. Required: {MIN_FREE_SPACE_GB} GB.')
        flash(f'Server storage low. Please try again later. Minimum free space required: {MIN_FREE_SPACE_GB} GB.')
        return redirect(url_for('index'))

    uploaded_count = 0
    for file in files:
        if file.filename == '':
            continue
        
        file_id = str(uuid.uuid4())
        filename = secure_filename(file.filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
        try:
            file.save(file_path)
            upload_time = datetime.now().isoformat()
            new_file = File(id=file_id, filename=filename, upload_time=upload_time, is_permanent=0)
            db.session.add(new_file)
            db.session.commit()
            
            socketio.emit('new_file', {'id': file_id, 'filename': filename, 'upload_time': upload_time})
            logger.info(f'File {filename} ({file_id}) uploaded successfully.')
            uploaded_count += 1
        except Exception as e:
            logger.error(f'Error uploading file {filename} ({file_id}): {e}')
            flash(f'Error uploading {filename}. Please try again.')
    
    if uploaded_count > 0:
        flash(f'{uploaded_count} file(s) uploaded successfully')
    else:
        flash('No files were uploaded.')
    
    return redirect(url_for('index'))

@app.route('/upload_chunk', methods=['POST'])
@limiter.limit("100 per minute")
def upload_chunk():
    file_id = request.form.get('file_id')
    chunk_index = int(request.form.get('chunk_index'))
    total_chunks = int(request.form.get('total_chunks'))
    filename = secure_filename(request.form.get('filename'))
    file_chunk = request.files['file']

    chunk_dir = os.path.join(app.config['UPLOAD_FOLDER'], "_chunks", file_id)
    os.makedirs(chunk_dir, exist_ok=True)
    
    chunk_path = os.path.join(chunk_dir, str(chunk_index))
    file_chunk.save(chunk_path)

    # Check if all chunks are uploaded
    if len(os.listdir(chunk_dir)) == total_chunks:
        final_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
        with open(final_path, 'wb') as target_file:
            for i in range(total_chunks):
                chunk_file_path = os.path.join(chunk_dir, str(i))
                with open(chunk_file_path, 'rb') as f:
                    target_file.write(f.read())
                os.remove(chunk_file_path)
        
        os.rmdir(chunk_dir)
        
        upload_time = datetime.now().isoformat()
        new_file = File(id=file_id, filename=filename, upload_time=upload_time, is_permanent=0)
        db.session.add(new_file)
        db.session.commit()
        
        socketio.emit('new_file', {'id': file_id, 'filename': filename, 'upload_time': upload_time})
        return {"status": "finished"}, 200

    return {"status": "chunk_received"}, 200

@app.route('/download/<file_id>')
def download_file(file_id):
    file_obj = File.query.get(file_id)
    if not file_obj:
        logger.warning(f'Attempted download of non-existent or expired file: {file_id}')
        flash('File not found or expired')
        return redirect(url_for('index'))
    
    if not file_obj.is_permanent and datetime.fromisoformat(file_obj.upload_time) < datetime.now() - timedelta(minutes=10):
        logger.warning(f'Attempted download of expired file: {file_id}')
        flash('File has expired')
        return redirect(url_for('index'))
    
    filename = file_obj.filename
    # Use X-Accel-Redirect to let Nginx serve the file efficiently
    response = app.make_response("")
    response.headers['X-Accel-Redirect'] = f'/internal_uploads/{file_id}'
    response.headers['Content-Type'] = 'application/octet-stream'
    response.headers['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response

@app.route('/admin', methods=['GET'])
@login_required
def admin_panel():
    files = File.query.all()
    # Convert upload_time strings to datetime objects for Flask-Moment
    for file_obj in files:
        file_obj.upload_time = datetime.fromisoformat(file_obj.upload_time)
    total, used, free = shutil.disk_usage(app.config['UPLOAD_FOLDER'])
    storage_info = {
        'used': f'{used / (1024**3):.2f} GB',
        'free': f'{free / (1024**3):.2f} GB'
    }
    return render_template('admin.html', files=files, storage_info=storage_info)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_panel'))
    form = AdminLoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            flash('Logged in successfully.', 'success')
            next_page = request.args.get('next')
            return redirect(next_page or url_for('admin_panel'))
        else:
            flash('Invalid username or password.', 'danger')
    return render_template('admin_login.html', form=form)

@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    flash('Logged out successfully.')
    return redirect(url_for('admin_login'))

@app.route('/admin/manage', methods=['POST'])
@login_required
def admin_manage():
    file_id = request.form.get('file_id')
    action = request.form.get('action')
    file_obj = File.query.get(file_id)
    if file_obj:
        if action == 'delete':
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f'Admin deleted file from disk: {file_obj.filename} ({file_id})')
            db.session.delete(file_obj)
            db.session.commit()
            socketio.emit('file_deleted', {'id': file_id})
            flash('File deleted')
            logger.info(f'Admin deleted file record: {file_obj.filename} ({file_id})')
        elif action == 'make_permanent':
            file_obj.is_permanent = 1
            db.session.commit()
            flash('File marked as permanent')
            logger.info(f'Admin marked file as permanent: {file_obj.filename} ({file_id})')
    return redirect(url_for('admin_panel'))

@app.route('/health')
def health_check():
    try:
        # Attempt to query the database to check connection
        db.session.query(File).first()
        return "OK", 200
    except Exception as e:
        logger.error(f"Health check failed: {e}", exc_info=True)
        return "Error", 500

if __name__ == '__main__':
    socketio.run(app, debug=True)
