"""
MediHabit - app.py
Full Flask backend: auth, CRUD, Gmail SMTP email alerts, APScheduler
"""

import os
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, date
from functools import wraps

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── App & DB setup ────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key          = os.environ.get('SECRET_KEY', 'change-this-in-production')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///medihabit.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

GMAIL_USER         = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASSWORD = os.environ.get('GMAIL_APP_PASSWORD', '')

db = SQLAlchemy(app)

# ── Models ────────────────────────────────────────────────────────────────────

class User(db.Model):
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(120), nullable=False)
    email         = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    medications   = db.relationship('Medication', backref='user',
                                    lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Medication(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name            = db.Column(db.String(200), nullable=False)
    dose            = db.Column(db.String(100))
    frequency       = db.Column(db.String(50))
    time1           = db.Column(db.String(5))   # "HH:MM"
    time2           = db.Column(db.String(5))
    recipient_email = db.Column(db.String(120))
    notes           = db.Column(db.String(300))
    email_enabled   = db.Column(db.Boolean, default=True)
    active          = db.Column(db.Boolean, default=True)


class AlertLog(db.Model):
    id              = db.Column(db.Integer, primary_key=True)
    user_id         = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medication_name = db.Column(db.String(200))
    alert_type      = db.Column(db.String(10))
    recipient       = db.Column(db.String(120))
    sent_at         = db.Column(db.DateTime, default=datetime.utcnow)
    status          = db.Column(db.String(20), default='sent')
    error           = db.Column(db.String(300))

# ── Auth decorator ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

# ── Routes: Auth ──────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return redirect(url_for('dashboard') if 'user_id' in session else url_for('login'))


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name     = request.form.get('name', '').strip()
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')

        if not name or not email or not password:
            flash('All fields are required.', 'error')
            return render_template('register.html')

        if User.query.filter_by(email=email).first():
            flash('An account with that email already exists.', 'error')
            return render_template('register.html')

        user = User(name=name, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        session['user_id']   = user.id
        session['user_name'] = user.name
        flash(f'Welcome, {name}!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email    = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        user     = User.query.filter_by(email=email).first()

        if user and user.check_password(password):
            session['user_id']   = user.id
            session['user_name'] = user.name
            return redirect(url_for('dashboard'))

        flash('Invalid email or password.', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Routes: Dashboard ─────────────────────────────────────────────────────────

@app.route('/dashboard')
@login_required
def dashboard():
    meds = Medication.query.filter_by(user_id=session['user_id'], active=True).all()
    today_logs = AlertLog.query.filter(
        AlertLog.user_id == session['user_id'],
        db.func.date(AlertLog.sent_at) == date.today()
    ).order_by(AlertLog.sent_at.desc()).all()
    return render_template('dashboard.html', meds=meds, logs=today_logs, now=datetime.now())

# ── Routes: Medication CRUD ───────────────────────────────────────────────────

@app.route('/medication/add', methods=['POST'])
@login_required
def add_medication():
    m = Medication(
        user_id         = session['user_id'],
        name            = request.form.get('name', '').strip(),
        dose            = request.form.get('dose', '').strip(),
        frequency       = request.form.get('frequency', ''),
        time1           = request.form.get('time1', ''),
        time2           = request.form.get('time2', '') or None,
        recipient_email = request.form.get('recipient_email', '').strip(),
        notes           = request.form.get('notes', '').strip(),
        email_enabled   = 'email_enabled' in request.form,
    )
    db.session.add(m)
    db.session.commit()
    flash(f'"{m.name}" added — reminders scheduled!', 'success')
    return redirect(url_for('dashboard'))


@app.route('/medication/<int:med_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_medication(med_id):
    m = Medication.query.filter_by(id=med_id, user_id=session['user_id']).first_or_404()
    if request.method == 'POST':
        m.name            = request.form.get('name', '').strip()
        m.dose            = request.form.get('dose', '').strip()
        m.frequency       = request.form.get('frequency', '')
        m.time1           = request.form.get('time1', '')
        m.time2           = request.form.get('time2', '') or None
        m.recipient_email = request.form.get('recipient_email', '').strip()
        m.notes           = request.form.get('notes', '').strip()
        m.email_enabled   = 'email_enabled' in request.form
        db.session.commit()
        flash('Medication updated!', 'success')
        return redirect(url_for('dashboard'))
    return render_template('edit_medication.html', med=m)


@app.route('/medication/<int:med_id>/delete', methods=['POST'])
@login_required
def delete_medication(med_id):
    m = Medication.query.filter_by(id=med_id, user_id=session['user_id']).first_or_404()
    name = m.name
    db.session.delete(m)
    db.session.commit()
    flash(f'"{name}" removed.', 'success')
    return redirect(url_for('dashboard'))


@app.route('/medication/<int:med_id>/test', methods=['POST'])
@login_required
def test_send(med_id):
    m = Medication.query.filter_by(id=med_id, user_id=session['user_id']).first_or_404()
    if m.email_enabled and m.recipient_email:
        ok, err = send_email_reminder(m)
        flash(f'Email {"sent to " + m.recipient_email if ok else "failed: " + err}',
              'success' if ok else 'error')
    else:
        flash('Email not enabled or no recipient set.', 'error')
    return redirect(url_for('dashboard'))

# ── Email sending ─────────────────────────────────────────────────────────────

def send_email_reminder(med):
    """Send styled HTML reminder email via Gmail SMTP."""
    try:
        msg            = MIMEMultipart('alternative')
        msg['Subject'] = f"💊 MediHabit Reminder: Time to take {med.name}"
        msg['From']    = GMAIL_USER
        msg['To']      = med.recipient_email
        now_str        = datetime.now().strftime('%A, %d %B %Y · %I:%M %p')

        plain = (
            f"Hello,\n\nThis is your reminder to take {med.name}.\n"
            + (f"Dosage: {med.dose}\n" if med.dose else "")
            + (f"Note: {med.notes}\n" if med.notes else "")
            + f"\nSent: {now_str}\n\nStay healthy!\n— MediHabit"
        )

        dose_row  = f"<p style='margin:4px 0;color:#555;font-size:14px;'>Dosage: {med.dose}</p>" if med.dose else ""
        notes_row = f"<p style='margin:4px 0;color:#555;font-size:14px;'>Note: {med.notes}</p>" if med.notes else ""

        html = f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f5f5f3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;">
  <div style="max-width:480px;margin:40px auto;background:#fff;border-radius:12px;border:1px solid #e0e0e0;overflow:hidden;">
    <div style="background:#1a1a1a;padding:20px 28px;">
      <span style="font-size:22px;">💊</span>
      <span style="color:#fff;font-size:18px;font-weight:500;margin-left:8px;">MediHabit</span>
    </div>
    <div style="padding:28px;">
      <h2 style="margin:0 0 6px;font-size:20px;color:#1a1a1a;">Time to take your medication</h2>
      <p style="margin:0 0 20px;color:#6b6b6b;font-size:13px;">{now_str}</p>
      <div style="background:#f5f5f3;border-radius:10px;padding:18px 20px;margin-bottom:20px;">
        <div style="font-size:18px;font-weight:500;color:#1a1a1a;margin-bottom:6px;">{med.name}</div>
        {dose_row}
        {notes_row}
      </div>
      <div style="background:#E6F1FB;border-radius:8px;padding:12px 16px;font-size:13px;color:#0C447C;">
        Take your medication as prescribed by your doctor.
      </div>
    </div>
    <div style="padding:14px 28px;border-top:1px solid #eee;font-size:12px;color:#aaa;text-align:center;">
      Sent by MediHabit — Your automated health reminder system
    </div>
  </div>
</body>
</html>"""

        msg.attach(MIMEText(plain, 'plain'))
        msg.attach(MIMEText(html, 'html'))

        with smtplib.SMTP('smtp.gmail.com', 587) as server:
            server.ehlo()
            server.starttls()
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, med.recipient_email, msg.as_string())

        _log(med, 'sent')
        return True, None

    except Exception as e:
        _log(med, 'failed', str(e))
        return False, str(e)


def _log(med, status, error=None):
    with app.app_context():
        db.session.add(AlertLog(
            user_id=med.user_id, medication_name=med.name,
            alert_type='email', recipient=med.recipient_email,
            status=status, error=error
        ))
        db.session.commit()

# ── Scheduler ─────────────────────────────────────────────────────────────────

def check_and_send():
    """Runs every minute — fires emails when clock matches reminder time."""
    with app.app_context():
        now  = datetime.now().strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for med in meds:
            if med.time1 == now or med.time2 == now:
                if med.recipient_email:
                    print(f"[{now}] Sending → {med.name} → {med.recipient_email}")
                    threading.Thread(target=send_email_reminder, args=(med,), daemon=True).start()

# ── Init ──────────────────────────────────────────────────────────────────────

with app.app_context():
    db.create_all()

scheduler = BackgroundScheduler()
scheduler.add_job(check_and_send, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    print("MediHabit → http://localhost:5000")
    app.run(debug=True, use_reloader=False)
