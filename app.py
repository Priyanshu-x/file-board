from gevent import monkey
# Use standard gevent patching, fully enabling c-ares DNS which allows non-blocking resolution.
monkey.patch_all()
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

# --- STRICT DATABASE CONNECTION ---
db_url = os.getenv('DATABASE_URL')
if not db_url:
    logger.critical("FATAL: DATABASE_URL is not set!")
    
# Enforce connection timeout on strictly postgres URLs to prevent infinite hangs
if db_url and db_url.startswith("postgres"):
    # keepalives: detect dead connections quickly (idle 10s + 3 probes * 5s = 25s detection)
    pq_opts = "connect_timeout=10&keepalives=1&keepalives_idle=10&keepalives_interval=5&keepalives_count=3"
    if "?" in db_url:
        db_url += f"&{pq_opts}"
    else:
        db_url += f"?{pq_opts}"

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    'pool_pre_ping': True,      # Test connections before use; instantly discard zombies
    'pool_recycle': 280,         # Recycle connections before Render's 5-min idle kill
    'pool_size': 3,              # Small pool to avoid exhausting free-tier connection limits
    'max_overflow': 2,
    'pool_timeout': 5,           # Fail fast instead of hanging 30s waiting for a connection
}

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
        import redis
        # Use a short timeout so we don't block boot for long if Redis is dead
        r = redis.Redis.from_url(redis_url, socket_connect_timeout=2)
        r.ping()
        logger.info("Redis connection and ping successful.")
    except Exception as e:
        logger.warning(f"Redis host unreachable or ping failed: {e}. Falling back to memory storage.")
        redis_url = 'memory://'
else:
    redis_url = 'memory://'

# Use pure in-memory rate limiting to absolutely guarantee no Redis connection timeouts.
# The user issue points to the first `/request_upload` hitting an unresponsive Redis.
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri="memory://",
    strategy="fixed-window",
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
                # Set a short statement timeout so this query can NEVER block the pool (PostgreSQL only)
                from sqlalchemy import text
                if 'postgresql' in str(db.engine.url):
                    db.session.execute(text("SET statement_timeout = '15s'"))
                all_files = File.query.all()
                files_to_check = [f for f in all_files if f.is_permanent == 0]
            except Exception as e:
                # During cold boots on Render, this will throw a DNS error gracefully
                logger.warning(f"Cleanup skipped: Database not ready ({e})")
                db.session.rollback()
                return
            
            try:
                deleted_count = 0
                for file_obj in files_to_check:
                    ut = file_obj.upload_time
                    if isinstance(ut, str):
                        try: ut = datetime.fromisoformat(ut)
                        except: ut = datetime.now()
                    
                    if ut < limit_time:
                        file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_obj.id)
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            
                        # Cleanup abandoned chunks if they exist
                        chunk_path = os.path.join(app.config['UPLOAD_FOLDER'], "_chunks", file_obj.id)
                        if os.path.exists(chunk_path):
                            shutil.rmtree(chunk_path)
                        Chunk.query.filter_by(file_id=file_obj.id).delete()
                            
                        db.session.delete(file_obj)
                        socketio.emit('file_deleted', {'id': file_obj.id})
                        deleted_count += 1
                
                db.session.commit()
                if deleted_count > 0:
                    logger.info(f"Cleanup: Deleted {deleted_count} expired file(s).")
            finally:
                if os.path.exists(lock_file): os.remove(lock_file)
                
        except Exception:
            logger.exception("Scheduled deletion failed unexpectedly")
        finally:
            # MUST manually teardown session in background threads to avoid connection leaks
            db.session.remove()

def background_cleanup_loop():
    import gevent
    while True:
        gevent.sleep(300)  # Every 5 min (files expire at 15m; no need to check every minute)
        delete_expired_files()

import gevent
gevent.spawn(background_cleanup_loop)

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
        # Ensure admin user exists and password matches current env var
        admin_user = User.query.filter_by(username=ADMIN_USER).first()
        if not admin_user:
            admin_user = User(username=ADMIN_USER)
            admin_user.set_password(ADMIN_PASS)
            db.session.add(admin_user)
            db.session.commit()
            logger.info("Created default admin user.")
        else:
            # Update password if it was changed in environment variables
            if not admin_user.check_password(ADMIN_PASS):
                admin_user.set_password(ADMIN_PASS)
                db.session.commit()
                logger.info("Updated admin password from environment variables.")
    except Exception as e:
        logger.error(f"Post-boot initialization failed: {e}")

# --- Pool Warmup: pre-establish connections so the first user request is instant ---
with app.app_context():
    try:
        from sqlalchemy import text
        for i in range(3):  # Fill pool_size=3 connections
            db.session.execute(text('SELECT 1'))
        db.session.remove()  # Return connections to pool
        logger.info("Pool warmup: 3 connections pre-established and ready.")
    except Exception as e:
        logger.warning(f"Pool warmup skipped (DB may still be starting): {e}")

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
    if request.path.startswith('/request_upload') or request.path.startswith('/upload_chunk'):
        return {"error": "Rate limit exceeded. Please wait a minute."}, 429
    flash('Rate limit exceeded.')
    return redirect(url_for('index'))

@app.errorhandler(Exception)
def handle_exception(e):
    from werkzeug.exceptions import HTTPException
    # Pass through standard HTTP errors (like 404 for favicon) without dumping 500 stacktraces
    if isinstance(e, HTTPException):
        return e

    from sqlalchemy.exc import OperationalError
    if isinstance(e, OperationalError):
        logger.warning(f"Database connection not yet established (sleeping DB).")
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
        limit_time = datetime.now() - timedelta(minutes=15)
        # Type-agnostic filtering
        all_files = File.query.all()
        files = []
        for f in all_files:
            ut = f.upload_time
            if isinstance(ut, str):
                try: ut = datetime.fromisoformat(ut)
                except: ut = datetime.now()
            
            if f.is_permanent == 1 or ut > limit_time:
                # Ghost File Protection: Check if the file actually exists on disk before showing it
                if os.path.exists(os.path.join(app.config['UPLOAD_FOLDER'], f.id)):
                    f.upload_time = ut # Patch in-memory for template
                    files.append(f)
                
    except Exception as e:
        logger.warning(f"Index route serving waking-up screen: {e}")
        return render_template('db_error.html'), 503
        
    return render_template('index.html', files=files, form=UploadForm())

@app.route('/request_upload', methods=['POST'])
@limiter.limit("10 per minute")
def request_upload():
    import time
    req_start = time.time()
    logger.info(f"TRACE: /request_upload hitting endpoint.")
    try:
        # Use FormData to bypass complex Nginx proxy buffer waiting issues with arbitrary JSON
        filename = secure_filename(request.form.get('filename', ''))
        if not filename: return {"error": "Missing filename in form data"}, 400
        
        # Disk Quota Check
        logger.info(f"TRACE: Checking disk space for {filename}")
        _, _, free = shutil.disk_usage(app.config['UPLOAD_FOLDER'])
        if free < 0.5 * (1024**3): # 500MB buffer
            return {"error": "Server storage full. Please try again later."}, 507

        logger.info(f"TRACE: Generating DB record for {filename}")
        file_id = str(uuid.uuid4())
        new_file = File(id=file_id, filename=filename, upload_time=datetime.now())
        db.session.add(new_file)
        db.session.commit()
        
        logger.info(f"TRACE: Transaction committed for {file_id}. Returning ID.")
        return {"file_id": file_id}, 200
    except Exception as e:
        logger.error(f"FATAL TRACE: /request_upload hung or errored: {e}", exc_info=True)
        db.session.rollback()
        return {"error": "Internal server error during DB transaction."}, 500

def assemble_file_async(file_id, filename, total_chunks, chunk_dir):
    with app.app_context():
        import time
        start_time = time.time()
        logger.info(f"Assembly [Async]: Starting for {filename} ({file_id})")
        
        final_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
        try:
            with open(final_path, 'wb') as target:
                for i in range(total_chunks):
                    chunk_path = os.path.join(chunk_dir, str(i))
                    if not os.path.exists(chunk_path):
                        raise Exception(f"Missing chunk {i}")
                    
                    with open(chunk_path, 'rb') as source:
                        shutil.copyfileobj(source, target)
            
            assembly_time = time.time() - start_time
            file_obj = File.query.filter_by(id=file_id).first()
            if file_obj:
                file_obj.size_bytes = os.path.getsize(final_path)
                # Cleanup chunks from DB
                Chunk.query.filter_by(file_id=file_id).delete()
                db.session.commit()
                logger.info(f"Assembly [Async]: Complete in {assembly_time:.2f}s for {filename}")
            
            # Cleanup chunks from FS
            if os.path.exists(chunk_dir):
                shutil.rmtree(chunk_dir)
            
            # Notify frontend
            socketio.emit('new_file', {
                'id': file_id,
                'filename': filename
            })
            socketio.emit('assembly_complete', {'file_id': file_id})
            
        except Exception as e:
            logger.error(f"Assembly [Async]: Failed for {file_id}: {e}")
            if os.path.exists(final_path): os.remove(final_path)
            socketio.emit('assembly_error', {'file_id': file_id, 'error': str(e)})
        finally:
            # MUST manually teardown session to return DB connection back to pool
            db.session.remove()

@app.route('/upload_chunk', methods=['POST'])
@csrf.exempt           # Chunks are protected by server-issued file_id; CSRF token can expire mid-upload
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
        # Trigger Async Assembly
        import gevent
        gevent.spawn(assemble_file_async, file_id, f_entry.filename, total_chunks, chunk_dir)
        return {"status": "assembling", "message": "All chunks received, assembling in background..."}, 200
        
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
            flash('Invalid username or password')
        except:
            flash('Database error')
    elif form.errors:
        for err_msgs in form.errors.values():
            for err in err_msgs:
                flash(err)
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
