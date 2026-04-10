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
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///medihabit.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Email credentials
GMAIL_USER = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')

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
    time1 = db.Column(db.String(5))   # "HH:MM"
    time2 = db.Column(db.String(5), nullable=True)
    recipient_email = db.Column(db.String(120))
    notes = db.Column(db.String(300))
    email_enabled = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)


class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medication_name = db.Column(db.String(200))
    alert_type = db.Column(db.String(10), default='email')
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
    """Sends a thank you email after successful registration."""
    with app.app_context():
        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = "Welcome to MediHabit! 💊"
            msg['From'] = GMAIL_USER
            msg['To'] = user_email
            
            body = f"Hi {user_name},\n\nThank you for registering with MediHabit! We're here to help you stay on track with your medications.\n\nYou can now log in and start adding your medication reminders.\n\nBest regards,\nThe MediHabit Team"
            msg.attach(MIMEText(body, 'plain'))

            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.send_message(msg)
        except Exception as e:
            print(f"Welcome email failed: {e}")

# ── Routes: Auth ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not name or not email or not password:
            return jsonify({"error": "Missing Fields"}), 400

        if User.query.filter_by(email=email).first():
            return jsonify({"error": "Email already exists"}), 400

        user = User(name=name, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        # Trigger welcome email in background thread
        threading.Thread(target=send_welcome_email, args=(email, name), daemon=True).start()
        
        return jsonify({"success": True})

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            session['user_id'] = user.id
            session['user_name'] = user.name
            return jsonify({"success": True})
        
        return jsonify({"error": "Invalid email or password"}), 401

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Routes: Dashboard ─────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    user_id = session['user_id']
    meds = Medication.query.filter_by(user_id=user_id, active=True).all()
    today = date.today()
    today_logs = AlertLog.query.filter(
        AlertLog.user_id == user_id,
        db.func.date(AlertLog.sent_at) == today
    ).order_by(AlertLog.sent_at.desc()).all()
    
    return render_template('dashboard.html', meds=meds, logs=today_logs, now=datetime.now())

# ── Routes: Medication CRUD ───────────────────────────────────────────────────

@app.route('/medication/add', methods=['POST'])
@login_required
def add_medication():
    m = Medication(
        user_id = session['user_id'],
        name = request.form.get('name', '').strip(),
        dose = request.form.get('dose', '').strip(),
        frequency = request.form.get('frequency', ''),
        time1 = request.form.get('time1', ''),
        time2 = request.form.get('time2', '') or None,
        recipient_email = request.form.get('recipient_email', '').strip(),
        notes = request.form.get('notes', '').strip(),
        email_enabled = 'email_enabled' in request.form,
    )
    db.session.add(m)
    db.session.commit()
    flash(f'"{m.name}" added successfully!', 'success')
    return redirect(url_for('dashboard'))

@app.route('/medication/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_medication(id):
    med = Medication.query.get_or_404(id)
    # Security: Ensure user owns this medication
    if med.user_id != session['user_id']:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        med.name = request.form.get('name', '').strip()
        med.dose = request.form.get('dose', '').strip()
        med.frequency = request.form.get('frequency', '')
        med.time1 = request.form.get('time1', '')
        med.time2 = request.form.get('time2', '') or None
        med.recipient_email = request.form.get('recipient_email', '').strip()
        med.notes = request.form.get('notes', '').strip()
        med.email_enabled = 'email_enabled' in request.form
        
        db.session.commit()
        flash('Medication updated!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('edit_medication.html', med=med)

@app.route('/medication/delete/<int:id>', methods=['POST'])
@login_required
def delete_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id == session['user_id']:
        db.session.delete(med)
        db.session.commit()
        flash('Medication removed.', 'info')
    return redirect(url_for('dashboard'))

# ── Email Engine & Scheduler ──────────────────────────────────────────────────

def send_email_reminder(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med or not med.email_enabled or not med.recipient_email:
            return

        try:
            msg = MIMEMultipart('alternative')
            msg['Subject'] = f"💊 Reminder: {med.name}"
            msg['From'] = GMAIL_USER
            msg['To'] = med.recipient_email
            
            body = f"It's time to take {med.name} ({med.dose}).\nNotes: {med.notes}"
            msg.attach(MIMEText(body, 'plain'))

            with smtplib.SMTP('smtp.gmail.com', 587) as server:
                server.starttls()
                server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
                server.send_message(msg)

            _log(med, 'sent')
        except Exception as e:
            _log(med, 'failed', str(e))

def _log(med, status, error=None):
    db.session.add(AlertLog(
        user_id=med.user_id, medication_name=med.name,
        recipient=med.recipient_email, status=status, error=error
    ))
    db.session.commit()

def check_and_send():
    with app.app_context():
        now = datetime.now().strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for med in meds:
            if med.time1 == now or med.time2 == now:
                threading.Thread(target=send_email_reminder, args=(med.id,), daemon=True).start()

# ── Initialization ────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

scheduler = BackgroundScheduler()
scheduler.add_job(check_and_send, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
