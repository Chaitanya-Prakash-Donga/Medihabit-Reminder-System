import os
import threading
import pytz
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from functools import wraps

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash, jsonify)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── App & DB setup ────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'medihabit-super-secret-key-123')

# Set timezone to IST
IST = pytz.timezone('Asia/Kolkata')

# --- GMAIL CONFIGURATION ---
# These match your Render Environment Variables exactly
GMAIL_USER = os.environ.get('GMAIL_USER')
GMAIL_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD')  # Updated to match your Render key
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', GMAIL_USER)

# Database configuration with Render/PostgreSQL fix
uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}

db = SQLAlchemy(app)

# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(IST))
    medications = db.relationship('Medication', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

class Medication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    dose = db.Column(db.String(100))
    frequency = db.Column(db.String(50))
    time1 = db.Column(db.String(5))   
    time2 = db.Column(db.String(5), nullable=True)
    recipient_email = db.Column(db.String(120))
    notes = db.Column(db.String(300))
    email_enabled = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medication_name = db.Column(db.String(200))
    recipient = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(IST))
    status = db.Column(db.String(20), default='sent')
    error = db.Column(db.String(300))

# ── Email Helper Function (SMTP) ───────────────────────────────────────────────

def send_smtp_email(to_email, subject, body):
    """Generic helper to send emails via Gmail SMTP"""
    if not GMAIL_USER or not GMAIL_PASSWORD:
        print("❌ SMTP Error: Missing GMAIL_USER or GMAIL_APP_PASSWORD environment variables.")
        return False
        
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_USER
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASSWORD)
        server.send_message(msg)
        server.quit()
        return True
    except Exception as e:
        print(f"❌ SMTP Error: {str(e)}")
        return False

# ── Helpers ───────────────────────────────────────────────────────────────────

@app.teardown_appcontext
def shutdown_session(exception=None):
    db.session.remove()

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

def send_welcome_and_admin_alert(user_email, user_name):
    """Sends registration emails using Gmail SMTP"""
    with app.app_context():
        # 1. Welcome Mail to User
        welcome_subject = "Welcome to MediHabit! 💊"
        welcome_body = f"Hi {user_name},\n\nWelcome to MediHabit! Your account is active and you can now start tracking your medications."
        send_smtp_email(user_email, welcome_subject, welcome_body)

        # 2. Admin Notification
        admin_subject = f"New User Registered: {user_name}"
        admin_body = f"User: {user_name}\nEmail: {user_email}\nJoined: {datetime.now(IST)}"
        send_smtp_email(ADMIN_EMAIL, admin_subject, admin_body)

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('dashboard')) if 'user_id' in session else redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        try:
            name = request.form.get('name')
            email = request.form.get('email').strip().lower()
            pw = request.form.get('password')
            
            if User.query.filter_by(email=email).first():
                return jsonify({"error": "Email already registered"}), 400
            
            user = User(name=name, email=email)
            user.set_password(pw)
            db.session.add(user)
            db.session.commit()
            
            threading.Thread(target=send_welcome_and_admin_alert, args=(email, name)).start()
            return jsonify({"success": True})
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": str(e)}), 500
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email, pw = request.form.get('email').strip().lower(), request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(pw):
            session.update({'user_id': user.id, 'user_name': user.name})
            return jsonify({"success": True})
        return jsonify({"error": "Invalid credentials"}), 401
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    uid = session['user_id']
    meds = Medication.query.filter_by(user_id=uid, active=True).all()
    today_ist = datetime.now(IST).date()
    logs = AlertLog.query.filter(AlertLog.user_id == uid, db.func.date(AlertLog.sent_at) == today_ist).order_by(AlertLog.sent_at.desc()).all()
    return render_template('dashboard.html', meds=meds, logs=logs)

@app.route('/medication/add', methods=['POST'])
@login_required
def add_medication():
    m = Medication(
        user_id=session['user_id'],
        name=request.form.get('name'),
        dose=request.form.get('dose'),
        frequency=request.form.get('frequency'),
        time1=request.form.get('time1'),
        time2=request.form.get('time2') or None,
        recipient_email=request.form.get('recipient_email'),
        notes=request.form.get('notes'),
        email_enabled='email_enabled' in request.form
    )
    db.session.add(m)
    db.session.commit()
    flash(f'"{m.name}" scheduled!', 'success')
    return redirect(url_for('dashboard'))

# ── Reminder Engine ───────────────────────────────────────────────────────────

def send_email_reminder(med_id):
    """Sends scheduled reminders using Gmail SMTP"""
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med or not med.email_enabled:
            return
            
        subject = f"💊 Time for {med.name}"
        body = f"Hello,\n\nIt is time for your medication: {med.name}\nDosage: {med.dose}\nNotes: {med.notes}"
        
        success = send_smtp_email(med.recipient_email, subject, body)
        
        status = 'sent' if success else 'failed'
        error_msg = None if success else "SMTP Connection Failed"
        
        db.session.add(AlertLog(user_id=med.user_id, medication_name=med.name, recipient=med.recipient_email, status=status, error=error_msg))
        db.session.commit()

def check_and_send():
    with app.app_context():
        now_str = datetime.now(IST).strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for m in meds:
            if m.time1 == now_str or m.time2 == now_str:
                threading.Thread(target=send_email_reminder, args=(m.id,), daemon=True).start()

# ── Startup ───────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

scheduler = BackgroundScheduler(timezone=IST)
scheduler.add_job(check_and_send, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
