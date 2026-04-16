import os
import threading
import resend  # Use Resend instead of smtplib for Render compatibility
import pytz
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
# Secret key for session signing
app.secret_key = os.environ.get('SECRET_KEY', 'medihabit-super-secret-key-123')
IST = pytz.timezone('Asia/Kolkata')

# Database configuration (PostgreSQL for Render, SQLite for local)
uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {"pool_pre_ping": True, "pool_recycle": 280}

db = SQLAlchemy(app)

# ── Resend API Email Logic ────────────────────────────────────────────────────
resend.api_key = os.environ.get('RESEND_API_KEY')

def send_smtp_email(to_email, subject, body):
    """
    Using Resend API to bypass SMTP restrictions.
    """
    if not resend.api_key:
        print("❌ Error: RESEND_API_KEY not set in Environment Variables")
        return False

    try:
        params = {
            "from": "MediHabit <onboarding@resend.dev>",
            "to": [to_email],
            "subject": subject,
            "text": body,
        }
        resend.Emails.send(params)
        print(f"✅ Email sent successfully to {to_email}")
        return True
    except Exception as e:
        print(f"❌ Resend API Error: {str(e)}") 
        return False

# ── Models ────────────────────────────────────────────────────────────────────
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    
    # Updated to explicitly fetch IST on every creation
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Kolkata')))
    
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
    
    # Updated to explicitly fetch IST on every log entry
    sent_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Kolkata')))
    
    status = db.Column(db.String(20), default='sent')
    error = db.Column(db.String(300))

# ── Helpers ───────────────────────────────────────────────────────────────────
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

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
                flash("Email already registered! Please log in.", "danger")
                return redirect(url_for('register'))
            
            user = User(name=name, email=email)
            user.set_password(pw)
            db.session.add(user)
            db.session.commit()
            
            welcome_body = f"Hi {name},\n\nWelcome to MediHabit! Your account is active."
            threading.Thread(target=send_smtp_email, args=(email, "Welcome to MediHabit! 💊", welcome_body)).start()
            
            flash("Account created successfully! Please login.", "success")
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            flash(f"Error: {str(e)}", "danger")
            return redirect(url_for('register'))
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        pw = request.form.get('password')
        user = User.query.filter_by(email=email).first()
        if user and user.check_password(pw):
            session.update({'user_id': user.id, 'user_name': user.name})
            flash(f"Welcome back, {user.name}!", "success")
            return redirect(url_for('dashboard'))
        flash("Invalid email or password.", "danger")
        return redirect(url_for('login'))
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ── Updated Dashboard Route ───────────────────────────────────────────────────
@app.route('/dashboard')
@login_required
def dashboard():
    uid = session.get('user_id')
    meds = Medication.query.filter_by(user_id=uid).all() 
    
    # 1. Force IST timezone for the "Today" calculation
    tz = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.now(tz)
    
    # Create a 24-hour window for 'Today' in IST
    start_of_day = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)
    
    # 2. Query logs within this specific IST window to bypass UTC server confusion
    logs = AlertLog.query.filter(
        AlertLog.user_id == uid, 
        AlertLog.sent_at >= start_of_day,
        AlertLog.sent_at < end_of_day
    ).order_by(AlertLog.sent_at.desc()).all()
    
    today_display = now_ist.strftime('%A, %d %B %Y')
    
    return render_template('dashboard.html', meds=meds, logs=logs, today_date=today_display)

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

@app.route('/medication/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id != session['user_id']:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        med.name = request.form.get('name')
        med.dose = request.form.get('dose')
        med.frequency = request.form.get('frequency')
        med.time1 = request.form.get('time1')
        med.time2 = request.form.get('time2') or None
        med.notes = request.form.get('notes')
        med.recipient_email = request.form.get('recipient_email')
        med.email_enabled = 'email_enabled' in request.form
        
        db.session.commit()
        flash("Medication updated!", "success")
        return redirect(url_for('dashboard'))
    return render_template('edit_medication.html', med=med)

@app.route('/medication/delete/<int:id>')
@login_required
def delete_medication(id):
    med = Medication.query.get_or_404(id)
    if med.user_id == session['user_id']:
        db.session.delete(med)
        db.session.commit()
        flash("Medication removed.", "success")
    return redirect(url_for('dashboard'))

# ── Updated Profile Edit Logic ────────────────────────────────────────────────
@app.route('/profile', methods=['GET', 'POST'])
@login_required
def profile():
    # Fix: Fetching fresh user from DB to ensure session is active
    user = User.query.filter_by(id=session['user_id']).first()
    
    if not user:
        flash("User not found.", "danger")
        return redirect(url_for('login'))

    if request.method == 'POST':
        try:
            # 1. Update Name
            new_name = request.form.get('name')
            if new_name:
                user.name = new_name
                session['user_name'] = new_name 
            
            # 2. Update Password (only if user provided a new one)
            new_pw = request.form.get('password')
            if new_pw and len(new_pw.strip()) > 0:
                user.set_password(new_pw)
            
            db.session.commit()
            flash("Profile updated successfully!", "success")
            return redirect(url_for('dashboard'))
            
        except Exception as e:
            db.session.rollback()
            flash("An error occurred while updating your profile.", "danger")
            return redirect(url_for('profile'))
    
    return render_template('edit_profile.html', user=user)

# ── Reminder Engine ───────────────────────────────────────────────────────────
def send_reminder_task(med_id):
    with app.app_context():
        med = Medication.query.get(med_id)
        if not med or not med.email_enabled: return
        subject = f"💊 Time for {med.name}"
        body = f"Hello,\n\nIt is time for your medication: {med.name}\nNotes: {med.notes}"
        success = send_smtp_email(med.recipient_email, subject, body)
        
        log = AlertLog(
            user_id=med.user_id, 
            medication_name=med.name, 
            status='sent' if success else 'failed', 
            recipient=med.recipient_email,
            sent_at=datetime.now(pytz.timezone('Asia/Kolkata')) # Explicit IST log
        )
        db.session.add(log)
        db.session.commit()

def check_and_send():
    with app.app_context():
        # Force the scheduler to check against current IST time
        now_str = datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%H:%M')
        meds = Medication.query.filter_by(active=True, email_enabled=True).all()
        for m in meds:
            if m.time1 == now_str or m.time2 == now_str:
                threading.Thread(target=send_reminder_task, args=(m.id,), daemon=True).start()

# ── Startup ───────────────────────────────────────────────────────────────────
with app.app_context():
    db.create_all()

scheduler = BackgroundScheduler(timezone=IST)
scheduler.add_job(check_and_send, 'interval', minutes=1)
scheduler.start()

if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)
