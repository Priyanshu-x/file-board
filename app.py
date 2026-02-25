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

# Robust Engine Options for Production
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

db = SQLAlchemy(app)
moment = Moment(app)
csrf = CSRFProtect(app)
socketio = SocketIO(app, async_mode='gevent', cors_allowed_origins="*")
login_manager = LoginManager(app)
login_manager.login_view = 'admin_login'

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Sanitize sensitive logs
def get_safe_url(url):
    if not url: return "None"
    if '@' in url:
        return url.split('@')[0].rsplit(':', 1)[0] + ':****@' + url.split('@')[1]
    return url

logger.info(f"SECRET_KEY loaded: {'*****' if app.config['SECRET_KEY'] else 'Not Set'}")
logger.info(f"DATABASE_URL loaded: {get_safe_url(app.config['SQLALCHEMY_DATABASE_URI'])}")

os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

ADMIN_USER = os.getenv('ADMIN_USER', 'admin')
ADMIN_PASS = os.getenv('ADMIN_PASS', 'admin123')

storage_uri = os.getenv('REDIS_URL', 'memory://')
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri=storage_uri,
    strategy="fixed-window",
    storage_options={"socket_connect_timeout": 30},
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
    upload_time = db.Column(db.String, nullable=False)
    is_permanent = db.Column(db.Integer, default=0)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

def delete_expired_files():
    with app.app_context():
        limit_time = (datetime.now() - timedelta(minutes=15)).isoformat()
        expired_files = File.query.filter(File.upload_time < limit_time, File.is_permanent == 0).all()
        for file_obj in expired_files:
            file_path = os.path.join(app.config['UPLOAD_FOLDER'], file_obj.id)
            if os.path.exists(file_path):
                os.remove(file_path)
            db.session.delete(file_obj)
            db.session.commit()
            socketio.emit('file_deleted', {'id': file_obj.id})

scheduler = BackgroundScheduler()
scheduler.add_job(delete_expired_files, 'interval', minutes=1)

with app.app_context():
    try:
        db.create_all()
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
    # Handle SQLAlchemy OperationalError (DNS/Connection issues)
    from sqlalchemy.exc import OperationalError
    if isinstance(e, OperationalError):
        logger.error(f"Database connection error: {e}")
        return render_template('db_error.html'), 503
    logger.error(f"Unhandled exception: {e}", exc_info=True)
    return render_template('500.html'), 500

@app.route('/')
def index():
    limit_time = (datetime.now() - timedelta(minutes=10)).isoformat()
    try:
        files = File.query.filter((File.is_permanent == 1) | (File.upload_time > limit_time)).all()
    except Exception as e:
        logger.error(f"Index route DB failure: {e}")
        return render_template('db_error.html'), 503
        
    display_files = []
    for f in files:
        display_files.append({
            'id': f.id,
            'filename': f.filename,
            'upload_time': datetime.fromisoformat(f.upload_time)
        })
    return render_template('index.html', files=display_files, form=UploadForm())

@app.route('/upload_chunk', methods=['POST'])
@limiter.limit("200 per minute")
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

    if len(os.listdir(chunk_dir)) == total_chunks:
        final_path = os.path.join(app.config['UPLOAD_FOLDER'], file_id)
        if not os.path.exists(final_path):
            temp_final = final_path + ".tmp"
            try:
                with open(temp_final, 'wb') as target_file:
                    for i in range(total_chunks):
                        c_path = os.path.join(chunk_dir, str(i))
                        with open(c_path, 'rb') as f:
                            target_file.write(f.read())
                os.replace(temp_final, final_path) # Atomic Rename
                shutil.rmtree(chunk_dir)
                
                new_file = File(id=file_id, filename=filename, upload_time=datetime.now().isoformat(), is_permanent=0)
                db.session.add(new_file)
                db.session.commit()
                socketio.emit('new_file', {'id': file_id, 'filename': filename, 'upload_time': new_file.upload_time})
                return {"status": "finished"}, 200
            except Exception as e:
                if os.path.exists(temp_final): os.remove(temp_final)
                return {"status": "error", "message": str(e)}, 500
        return {"status": "finished"}, 200
    return {"status": "chunk_received"}, 200

@app.route('/download/<file_id>')
def download_file(file_id):
    file_obj = File.query.get(file_id)
    if not file_obj:
        flash('File not found')
        return redirect(url_for('index'))
    
    response = app.make_response("")
    response.headers['X-Accel-Redirect'] = f'/internal_uploads/{file_id}'
    response.headers['Content-Disposition'] = f'attachment; filename="{file_obj.filename}"'
    return response

@app.route('/admin')
@login_required
def admin_panel():
    files = File.query.all()
    display_files = []
    for f in files:
        display_files.append({
            'id': f.id,
            'filename': f.filename,
            'upload_time': datetime.fromisoformat(f.upload_time),
            'is_permanent': f.is_permanent
        })
    _, used, free = shutil.disk_usage(app.config['UPLOAD_FOLDER'])
    storage_info = {'used': f'{used / (1024**3):.2f} GB', 'free': f'{free / (1024**3):.2f} GB'}
    return render_template('admin.html', files=display_files, storage_info=storage_info)

@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if current_user.is_authenticated: return redirect(url_for('admin_panel'))
    form = AdminLoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            return redirect(url_for('admin_panel'))
        flash('Invalid login')
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
    file_obj = File.query.get(file_id)
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
    return redirect(url_for('admin_panel'))

@app.route('/health')
def health_check():
    try:
        db.session.execute(db.select(File)).first()
        return "OK", 200
    except:
        return "ERROR", 500

if __name__ == '__main__':
    socketio.run(app, debug=True)
