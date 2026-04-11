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

# FIX: Ensures PostgreSQL works on Render (handles 'postgres://' vs 'postgresql://')
database_url = os.environ.get('DATABASE_URL', 'sqlite:///medihabit.db')
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Credentials from Environment Variables
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')
ADMIN_EMAIL = os.environ.get('ADMIN_EMAIL', GMAIL_USER) 

db = SQLAlchemy(app)

# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    medications = db.relationship('Medication', backref='user',
                                    lazy=True, cascade='all, delete-orphan')

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
    time1 = db.Column(db.String(5))   # Format "HH:MM"
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
            msg = MIMEMultipart()
            msg['Subject'] = "Welcome to MediHabit! 💊"
            msg['From'] = GMAIL_USER
            msg['To'] = user_email
            
            body = f"Hi {user_name},\n\nWelcome to MediHabit! Your account is active. Log in to start adding reminders.\n\nBest,\nThe MediHabit Team"
            msg.attach(MIMEText(body, 'plain'))

            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.send_message(msg)
        except Exception as e:
            print(f"Welcome email error: {e}")

def send_admin_report():
    """Admin Feature: Sends a list of all users and their meds to the admin email."""
    with app.app_context():
        users = User.query.all()
        if not users:
            return

        report = f"📋 MEDIHABIT SYSTEM REPORT - {date.today()}\n" + "="*40 + "\n\n"
        for u in users:
            report += f"USER: {u.name} ({u.email})\n"
            meds = Medication.query.filter_by(user_id=u.id).all()
            if meds:
                for m in meds:
                    report += f"  - [Med] {m.name} | [Time] {m.time1} | [To] {m.recipient_email}\n"
            else:
                report += "  - No medications added yet.\n"
            report += "-"*40 + "\n"

        try:
            msg = MIMEMultipart()
            msg['Subject'] = f"MediHabit Admin Activity Report: {date.today()}"
            msg['From'] = GMAIL_USER
            msg['To'] = ADMIN_EMAIL
            msg.attach(MIMEText(report, 'plain'))

            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.send_message(msg)
            print("✅ Admin report sent successfully.")
        except Exception as e:
            print(f"❌ Admin report failed: {e}")

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name, email, pw = request.form.get('name'), request.form.get('email').strip().lower(), request.form.get('password')
        if not name or not email or not pw:
            return jsonify({"error": "Fields missing"}), 400
        if User.query.filter_by(email=email).first():
            return jsonify({"error": "Email exists"}), 400
        
        user = User(name=name, email=email)
        user.set_password(pw)
        db.session.add(user)
        db.session.commit()
        
        threading.Thread(target=send_welcome_email, args=(email, name), daemon=True).start()
        return jsonify({"success": True})
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email, pw = request.form.get('email').strip().lower(), request.form.get('password')
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

# ── Medication CRUD ───────────────────────────────────────────────────────────

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

# ... (Include existing edit/delete routes here) ...

# ── Engine & Scheduler ────────────────────────────────────────────────────────

def send_email_reminder(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med or not med.email_enabled: return
        try:
            msg = MIMEMultipart()
            msg['Subject'] = f"💊 Reminder: {med.name}"
            msg['From'] = GMAIL_USER
            msg['To'] = med.recipient_email
            body = f"Time to take {med.name} ({med.dose}).\nNotes: {med.notes}"
            msg.attach(MIMEText(body, 'plain'))
            with smtplib.SMTP('smtp.gmail.com', 587) as s:
                s.starttls()
                s.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                s.send_message(msg)
            _log(med, 'sent')
        except Exception as e:
            _log(med, 'failed', str(e))

def _log(med, status, error=None):
    db.session.add(AlertLog(user_id=med.user_id, medication_name=med.name, recipient=med.recipient_email, status=status, error=error))
    db.session.commit()

def check_and_send():
    with app.app_context():
        now = datetime.now().strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for m in meds:
            if m.time1 == now or m.time2 == now:
                threading.Thread(target=send_email_reminder, args=(m.id,), daemon=True).start()

# ── Initialization ────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

scheduler = BackgroundScheduler()
scheduler.add_job(check_and_send, 'interval', minutes=1)
# Schedule the Admin Report for 10:00 PM (22:00)
scheduler.add_job(send_admin_report, 'cron', hour=22, minute=0)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
