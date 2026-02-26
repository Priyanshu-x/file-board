from gevent import monkey
monkey.patch_all()

# Patch psycopg2 for gevent
try:
    from psycogreen.gevent import patch_psycopg
    patch_psycopg()
except ImportError:
    pass

import os
import uuid
import shutil
import logging
import socket
from urllib.parse import urlparse
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

# --- Directory Setup (Absolute Paths) ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, 'instance')
UPLOAD_DIR = os.path.join(INSTANCE_DIR, 'uploads')

os.makedirs(INSTANCE_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(os.path.join(UPLOAD_DIR, '_chunks'), exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_DIR
app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'super-secret-key')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Robust Engine Options
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def get_safe_url(url):
    if not url: return "None"
    try:
        if '@' in url:
            return url.split('@')[0].rsplit(':', 1)[0] + ':****@' + url.split('@')[1]
    except: pass
    return url

# --- DATABASE FALLBACK LOGIC ---
db_url = os.getenv('DATABASE_URL')
db_fallback = False

if db_url:
    try:
        hostname = urlparse(db_url).hostname
        if hostname and hostname != 'localhost':
            logger.info(f"Testing DNS for database host: {hostname}")
            socket.gethostbyname(hostname)
            logger.info("DNS resolution successful.")
    except Exception as e:
        logger.warning(f"Database host unreachable via DNS: {e}. Falling back to SQLite.")
        db_fallback = True

if not db_url or db_fallback:
    # Use 4 slashes for absolute path on Linux/Docker
    app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(INSTANCE_DIR, 'files.db')}"
    logger.info("Using local SQLite storage (fallback).")
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url

logger.info(f"SQLALCHEMY_DATABASE_URI: {get_safe_url(app.config['SQLALCHEMY_DATABASE_URI'])}")

db = SQLAlchemy(app)
moment = Moment(app)
csrf = CSRFProtect(app)
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins="*")
login_manager = LoginManager(app)
login_manager.login_view = 'admin_login'

ADMIN_USER = os.getenv('ADMIN_USER', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASS', 'admin123')

# --- REDIS FALLBACK LOGIC ---
redis_url = os.getenv('REDIS_URL')
if redis_url:
    try:
        r_host = urlparse(redis_url).hostname
        if r_host and r_host != 'localhost':
            socket.gethostbyname(r_host)
    except:
        logger.warning("Redis host unreachable. Falling back to memory storage.")
        redis_url = 'memory://'
else:
    redis_url = 'memory://'

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=redis_url,
    strategy="fixed-window",
    storage_options={"socket_connect_timeout": 5},
    swallow_errors=True,
    in_memory_fallback_enabled=True,
)

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
    upload_time = db.Column(db.DateTime, nullable=False, default=datetime.now, index=True)
    is_permanent = db.Column(db.Integer, default=0)
    size_bytes = db.Column(db.BigInteger, nullable=True)

class Chunk(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(db.String, db.ForeignKey('file.id'), nullable=False)
    chunk_index = db.Column(db.Integer, nullable=False)
    __table_args__ = (db.UniqueConstraint('file_id', 'chunk_index', name='_file_chunk_uc'),)

@login_manager.user_loader
def load_user(user_id):
    try:
        return User.query.get(int(user_id))
    except: return None

def delete_expired_files():
    with app.app_context():
        try:
            # Simple lock to prevent multi-process thrashing if using local FS
            lock_file = os.path.join(INSTANCE_DIR, ".cleanup.lock")
            if os.path.exists(lock_file):
                # Check if lock is stale (older than 5 mins)
                if datetime.now().timestamp() - os.path.getmtime(lock_file) < 300:
                    return
            
            with open(lock_file, 'w') as f: f.write(str(os.getpid()))
            
            try:
                limit_time = datetime.now() - timedelta(minutes=15)
                # Fetching all non-permanent to be type-safe during schema transition
                all_files = File.query.filter_by(is_permanent=0).all()
                for file_obj in all_files:
                    ut = file_obj.upload_time
                    if isinstance(ut, str):
                        try: ut = datetime.fromisoformat(ut)
                        except: ut = datetime.now()
                    
                    if ut < limit_time:
                        file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_obj.id)
                        if os.path.exists(file_path):
                            os.remove(file_path)
                        db.session.delete(file_obj)
                        socketio.emit('file_deleted', {'id': file_obj.id})
                
                db.session.commit()
                logger.info("Cleanup: Atomic transaction complete.")
            finally:
                if os.path.exists(lock_file): os.remove(lock_file)
                
        except Exception:
            logger.exception("Scheduled deletion failed unexpectedly")

scheduler = BackgroundScheduler()
scheduler.add_job(delete_expired_files, 'interval', minutes=1)

def run_migrations():
    with app.app_context():
        try:
            # Check for missing columns in 'file' table
            from sqlalchemy import text
            with db.engine.connect() as conn:
                # Add size_bytes if missing
                try:
                    conn.execute(text("ALTER TABLE file ADD COLUMN size_bytes BIGINT"))
                    conn.commit()
                    logger.info("Migration: Added size_bytes to file table.")
                except: pass
                
                # Add upload_time index if missing
                try:
                    conn.execute(text("CREATE INDEX IF NOT EXISTS ix_file_upload_time ON file (upload_time)"))
                    conn.commit()
                except: pass
        except Exception as e:
            logger.warning(f"Auto-migration skipped or failed: {e}")

with app.app_context():
    try:
        db.create_all()
        run_migrations()
        if not User.query.filter_by(username=ADMIN_USER).first():
            admin = User(username=ADMIN_USER)
            admin.set_password(ADMIN_PASS)
            db.session.add(admin)
            db.session.commit()
        if not scheduler.running:
            scheduler.start()
    except Exception as e:
        logger.error(f"Post-boot initialization failed: {e}")

class UploadForm(FlaskForm):
    submit = SubmitField('Upload')

class AdminLoginForm(FlaskForm):
    username = StringField('Username', validators=[DataRequired(), Length(min=4, max=20)])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6, max=100)])
    submit = SubmitField('Login')

@app.errorhandler(RequestEntityTooLarge)
def handle_too_large(e):
    flash('File too large (Max 1GB).')
    return redirect(url_for('index'))

@app.errorhandler(429)
def ratelimit_handler(e):
    flash('Rate limit exceeded.')
    return redirect(url_for('index'))

@app.errorhandler(Exception)
def handle_exception(e):
    from sqlalchemy.exc import OperationalError
    if isinstance(e, OperationalError):
        logger.error(f"Database connection error: {e}")
        if request.path.startswith('/request_upload') or request.path.startswith('/upload_chunk'):
            return {"error": "Database error"}, 503
        return render_template('db_error.html'), 503
    logger.error(f"Unhandled exception: {e}", exc_info=True)
    if request.path.startswith('/request_upload') or request.path.startswith('/upload_chunk') or request.is_json:
        return {"error": str(e)}, 500
    try:
        return render_template('500.html'), 500
    except:
        return "Internal Server Error", 500

@app.route('/')
def index():
    try:
        limit_time = datetime.now() - timedelta(minutes=10)
        # Type-agnostic filtering
        all_files = File.query.all()
        files = []
        for f in all_files:
            ut = f.upload_time
            if isinstance(ut, str):
                try: ut = datetime.fromisoformat(ut)
                except: ut = datetime.now()
            
            if f.is_permanent == 1 or ut > limit_time:
                f.upload_time = ut # Patch in-memory for template
                files.append(f)
                
    except Exception:
        logger.exception("Index route failure")
        return render_template('db_error.html'), 503
        
    return render_template('index.html', files=files, form=UploadForm())

@app.route('/request_upload', methods=['POST'])
@limiter.limit("10 per minute")
def request_upload():
    if not request.is_json:
        return {"error": "Request must be JSON"}, 400
    data = request.get_json()
    if not data:
        return {"error": "Invalid or missing JSON body"}, 400
    filename = secure_filename(data.get('filename'))
    if not filename: return {"error": "Missing filename"}, 400
    
    # Disk Quota Check
    _, _, free = shutil.disk_usage(app.config['UPLOAD_FOLDER'])
    if free < 2 * (1024**3): # 2GB buffer
        return {"error": "Server storage full. Please try again later."}, 507

    file_id = str(uuid.uuid4())
    new_file = File(id=file_id, filename=filename, upload_time=datetime.now())
    db.session.add(new_file)
    db.session.commit()
    
    return {"file_id": file_id}, 200

@app.route('/upload_chunk', methods=['POST'])
@limiter.limit("200 per minute")
def upload_chunk():
    file_id = request.form.get('file_id')
    chunk_index = int(request.form.get('chunk_index'))
    total_chunks = int(request.form.get('total_chunks'))
    file_chunk = request.files['file']

    # Validate ID exists and is owned/recent
    f_entry = File.query.get(file_id)
    if not f_entry: return {"error": "Invalid upload session"}, 403

    chunk_dir = os.path.join(app.config['UPLOAD_FOLDER'], "_chunks", file_id)
    os.makedirs(chunk_dir, exist_ok=True)
    
    chunk_path = os.path.join(chunk_dir, str(chunk_index))
    file_chunk.save(chunk_path)

    # Track chunk in DB
    try:
        if not Chunk.query.filter_by(file_id=file_id, chunk_index=chunk_index).first():
            db.session.add(Chunk(file_id=file_id, chunk_index=chunk_index))
            db.session.commit()
    except Exception:
        db.session.rollback()

    # Verify if all chunks are in DB
    if Chunk.query.filter_by(file_id=file_id).count() == total_chunks:
        final_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
        if not os.path.exists(final_path):
            temp_final = final_path + ".tmp"
            try:
                with open(temp_final, 'wb') as target_file:
                    for i in range(total_chunks):
                        c_path = os.path.join(chunk_dir, str(i))
                        with open(c_path, 'rb') as f:
                            target_file.write(f.read())
                
                os.replace(temp_final, final_path)
                f_entry.size_bytes = os.path.getsize(final_path)
                f_entry.upload_time = datetime.now() # Reset to final assembly time
                
                # Cleanup chunks from DB and FS
                Chunk.query.filter_by(file_id=file_id).delete()
                db.session.commit()
                shutil.rmtree(chunk_dir)
                
                socketio.emit('new_file', {'id': file_id, 'filename': f_entry.filename, 'upload_time': f_entry.upload_time.isoformat()})
                return {"status": "finished"}, 200
            except Exception:
                logger.exception(f"Assembly failed for {file_id}")
                if os.path.exists(temp_final): os.remove(temp_final)
                return {"status": "error", "message": "Assembly failed"}, 500
        return {"status": "finished"}, 200
    return {"status": "chunk_received"}, 200

@app.route('/download/<file_id>')
def download_file(file_id):
    try:
        # Use simple get for string PK
        file_obj = File.query.filter_by(id=file_id).first()
        if not file_obj:
            flash('File no longer available')
            return redirect(url_for('index'))
        
        response = app.make_response("")
        response.headers['X-Accel-Redirect'] = f'/internal_uploads/{file_id}'
        response.headers['Content-Disposition'] = f'attachment; filename="{file_obj.filename}"'
        return response
    except Exception:
        logger.exception(f"Download error for {file_id}")
        return redirect(url_for('index'))

@app.route('/admin')
@login_required
def admin_panel():
    try:
        files = File.query.all()
        display_files = []
        for f in files:
            # Handle case where upload_time might be a string in existing DB
            ut = f.upload_time
            if isinstance(ut, str):
                try: ut = datetime.fromisoformat(ut)
                except: ut = datetime.now()
            
            display_files.append({
                'id': f.id,
                'filename': f.filename,
                'upload_time': ut,
                'is_permanent': f.is_permanent
            })
        _, used, free = shutil.disk_usage(app.config['UPLOAD_FOLDER'])
        storage_info = {'used': f'{used / (1024**3):.2f} GB', 'free': f'{free / (1024**3):.2f} GB'}
        return render_template('admin.html', files=display_files, storage_info=storage_info)
    except:
        return "Admin panel unreachable", 500

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated: return redirect(url_for('admin_panel'))
    form = AdminLoginForm()
    if form.validate_on_submit():
        try:
            user = User.query.filter_by(username=form.username.data).first()
            if user and user.check_password(form.password.data):
                login_user(user)
                return redirect(url_for('admin_panel'))
            flash('Invalid login')
        except:
            flash('Database error')
    return render_template('admin_login.html', form=form)

@app.route('/admin/logout')
@login_required
def admin_logout():
    logout_user()
    return redirect(url_for('admin_login'))

@app.route('/admin/manage', methods=['POST'])
@login_required
def admin_manage():
    file_id = request.form.get('file_id')
    action = request.form.get('action')
    try:
        file_obj = File.query.filter_by(id=file_id).first()
        if file_obj:
            if action == 'delete':
                file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
                if os.path.exists(file_path): os.remove(file_path)
                db.session.delete(file_obj)
                db.session.commit()
                socketio.emit('file_deleted', {'id': file_id})
            elif action == 'make_permanent':
                file_obj.is_permanent = 1
                db.session.commit()
    except Exception:
        logger.exception(f"Admin management failure for {file_id}")
        flash("Management action failed")
    return redirect(url_for('admin_panel'))

@app.route('/health')
def health_check():
    try:
        db.session.execute(db.select(File)).first()
        return "OK", 200
    except:
        return "DEGRADED", 200

if __name__ == '__main__':
    socketio.run(app, debug=True)
