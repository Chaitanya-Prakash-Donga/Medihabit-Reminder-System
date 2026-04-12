import os
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date
from functools import wraps

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash, jsonify)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── App & DB setup ────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'medihabit-super-secret-key-123')

# Database configuration with Render PostgreSQL fix
database_url = os.environ.get('DATABASE_URL', 'sqlite:///medihabit.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# FIX: Keep database connection alive and prevent SSL EOF errors
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}

# Gmail Credentials from Environment Variables
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')

db = SQLAlchemy(app)

# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
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
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='sent')
    error = db.Column(db.String(300))

# ── Auth decorator ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ── Email Utility Functions ───────────────────────────────────────────────────

def send_welcome_email(user_email, user_name):
    """Sends a thank you email after registration."""
    with app.app_context():
        try:
            print(f"📩 Attempting to send welcome email to: {user_email}")
            if not GMAIL_USER or not GMAIL_APP_PASSWORD:
                print("❌ Error: Gmail credentials missing in environment variables.")
                return

            msg = MIMEMultipart()
            msg['Subject'] = "Welcome to MediHabit! 💊"
            msg['From'] = f"MediHabit Team <{GMAIL_USER}>"
            msg['To'] = user_email
            
            body = f"Hi {user_name},\n\nWelcome to MediHabit! Your account is active. You can now set medicine reminders.\n\nBest,\nThe MediHabit Team"
            msg.attach(MIMEText(body, 'plain'))

            server = smtplib.SMTP('smtp.gmail.com', 587, timeout=30)
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
            server.quit()
            print(f"✅ SUCCESS: Welcome email sent to {user_email}")
        except Exception as e:
            print(f"❌ SMTP ERROR: {str(e)}")

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email').strip().lower()
        pw = request.form.get('password')
        
        if not name or not email or not pw:
            return jsonify({"error": "Fields missing"}), 400
        if User.query.filter_by(email=email).first():
            return jsonify({"error": "Email already registered"}), 400
        
        user = User(name=name, email=email)
        user.set_password(pw)
        db.session.add(user)
        db.session.commit()
        
        # Send email in background
        threading.Thread(target=send_welcome_email, args=(email, name)).start()
        return jsonify({"success": True})
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        pw = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(pw):
            session['user_id'], session['user_name'] = user.id, user.name
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
    logs = AlertLog.query.filter(AlertLog.user_id == uid, db.func.date(AlertLog.sent_at) == date.today()).all()
    return render_template('dashboard.html', meds=meds, logs=logs, now=datetime.now())

# ── Medication CRUD (Fixing 404 & 405 Errors) ────────────────────────────────

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
    flash(f'"{m.name}" added!', 'success')
    return redirect(url_for('dashboard'))

# FIX: Allow GET to view the edit form and POST to save changes
@app.route('/medication/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_medication(id):
    m = Medication.query.get_or_404(id)
    if m.user_id != session['user_id']:
        return "Unauthorized", 403
    
    if request.method == 'POST':
        m.name = request.form.get('name')
        m.dose = request.form.get('dose')
        m.frequency = request.form.get('frequency')
        m.time1 = request.form.get('time1')
        m.time2 = request.form.get('time2') or None
        m.recipient_email = request.form.get('recipient_email')
        m.notes = request.form.get('notes')
        m.email_enabled = 'email_enabled' in request.form
        
        db.session.commit()
        flash("Medication updated!", "success")
        return redirect(url_for('dashboard'))
    
    # If GET, show the edit page
    return render_template('edit_medication.html', med=m)

@app.route('/medication/delete/<int:id>', methods=['GET', 'POST'])
@login_required
def delete_medication(id):
    m = Medication.query.get_or_404(id)
    if m.user_id != session['user_id']:
        return "Unauthorized", 403
    db.session.delete(m)
    db.session.commit()
    flash("Medication deleted.", "info")
    return redirect(url_for('dashboard'))

# ── Reminder Engine ───────────────────────────────────────────────────────────

def send_email_reminder(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med or not med.email_enabled: return
        try:
            msg = MIMEMultipart()
            msg['Subject'] = f"💊 Reminder: {med.name}"
            msg['From'] = GMAIL_USER
            msg['To'] = med.recipient_email
            body = f"Hello,\n\nTime to take {med.name} ({med.dose}).\nNotes: {med.notes}"
            msg.attach(MIMEText(body, 'plain'))
            
            server = smtplib.SMTP('smtp.gmail.com', 587, timeout=30)
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
            server.quit()
            
            db.session.add(AlertLog(user_id=med.user_id, medication_name=med.name, recipient=med.recipient_email, status='sent'))
            db.session.commit()
        except Exception as e:
            db.session.add(AlertLog(user_id=med.user_id, medication_name=med.name, recipient=med.recipient_email, status='failed', error=str(e)))
            db.session.commit()

def check_and_send():
    with app.app_context():
        now = datetime.now().strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for m in meds:
            if m.time1 == now or m.time2 == now:
                threading.Thread(target=send_email_reminder, args=(m.id,), daemon=True).start()

# ── Scheduler Initialization ──────────────────────────────────────────────────

with app.app_context():
    db.create_all()

scheduler = BackgroundScheduler()
scheduler.add_job(check_and_send, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    # use_reloader=False is required to prevent APScheduler from running twice
    app.run(debug=True, use_reloader=False)
