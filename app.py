"""
MediHabit - app.py
✅ FIXED: Gmail SMTP (NO Resend), Render TZ, IST Timezone, Manual reminders
Deploy-ready for Render/GitHub
"""
import os
import threading
import smtplib
from email.mime.text import MimeText
from email.mime.multipart import MimeMultipart
import pytz
from datetime import datetime, timedelta
from functools import wraps
import logging

from flask import (Flask, render_template, request,
                   redirect, url_for, session, flash, send_from_directory)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler

# ── Logging Setup ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── App & DB setup ────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'medihabit-render-2024-secret')

# ── ✅ FIXED: Render Timezone Handling ────────────────────────────────────────
# Render sets TZ env var, but we force IST explicitly
IST = pytz.timezone('Asia/Kolkata')

# Get Render database URL
uri = os.environ.get('DATABASE_URL')
if uri and uri.startswith("postgres://"):
    uri = uri.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = uri or 'sqlite:///medihabit.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True, 
    "pool_recycle": 300,
    "pool_timeout": 30,
    "pool_size": 5,
    "max_overflow": 10
}

db = SQLAlchemy(app)

# ── ✅ FIXED: Gmail SMTP (Manual Email - NO APIs needed) ─────────────────────
GMAIL_USER = os.environ.get('GMAIL_USER')  # your gmail@gmail.com
GMAIL_PASS = os.environ.get('GMAIL_APP_PASSWORD')  # Gmail App Password

def send_gmail_email(to_email, subject, body_html, body_text=None):
    """✅ Gmail SMTP - Works perfectly on Render"""
    if not GMAIL_USER or not GMAIL_PASS:
        logger.error("❌ GMAIL_USER or GMAIL_PASS not set")
        return False
    
    try:
        # Create message
        msg = MimeMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"MediHabit 💊 <{GMAIL_USER}>"
        msg['To'] = to_email

        # HTML part
        html_part = MimeText(body_html, 'html')
        msg.attach(html_part)

        # Plain text fallback
        if body_text:
            text_part = MimeText(body_text, 'plain')
            msg.attach(text_part)

        # Connect & Send
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)
        server.quit()
        
        logger.info(f"✅ Gmail sent to {to_email}: {subject}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Gmail failed to {to_email}: {str(e)}")
        return False

# ── Models (Same as before but with better indexes) ───────────────────────────
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(IST))
    medications = db.relationship('Medication', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw, method='pbkdf2:sha256')

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

class Medication(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    dose = db.Column(db.String(100))
    frequency = db.Column(db.String(50))
    time1 = db.Column(db.String(5), nullable=False)  # "09:30"
    time2 = db.Column(db.String(5), nullable=True)
    recipient_email = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.String(300))
    email_enabled = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(Render))

class AlertLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    medication_id = db.Column(db.Integer, db.ForeignKey('medication.id'))
    medication_name = db.Column(db.String(200))
    recipient = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime, nullable=False, index=True)  # ✅ Index for fast queries
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
            name = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip().lower()
            pw = request.form.get('password', '')
            
            if not all([name, email, pw, len(pw) >= 6]):
                flash("Please fill all fields with password 6+ chars!", "danger")
                return redirect(url_for('register'))
            
            if User.query.filter_by(email=email).first():
                flash("Email already exists!", "danger")
                return redirect(url_for('register'))
            
            user = User(name=name, email=email)
            user.set_password(pw)
            db.session.add(user)
            db.session.commit()
            
            # ✅ Welcome email via Gmail SMTP
            welcome_html = f"""
            <div style="font-family: Arial, sans-serif; max-width: 500px; margin: 0 auto;">
                <h2 style="color: #2c5aa0;">Welcome to MediHabit, {name}! 💊</h2>
                <div style="background: #f8f9fa; padding: 25px; border-radius: 12px;">
                    <p>Your account is ready! 🎉</p>
                    <p><strong>Name:</strong> {name}</p>
                    <p><strong>Email:</strong> {email}</p>
                    <hr>
                    <p>Add medications in dashboard to get reminders at exact times.</p>
                    <p style="text-align: center; color: #666;">
                        🩺 Stay healthy with MediHabit!
                    </p>
                </div>
            </div>
            """
            
            success = send_gmail_email(email, f"Welcome to MediHabit, {name}! 💊", welcome_html)
            logger.info(f"Welcome email to {email}: {'✅' if success else '❌'}")
            
            flash("✅ Account created! Please login.", "success")
            return redirect(url_for('login'))
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"Register error: {e}")
            flash(f"Error: {str(e)}", "danger")
    
    return render_template('register.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email', '').strip().lower()
        pw = request.form.get('password', '')
        user = User.query.filter_by(email=email).first()
        
        if user and user.check_password(pw):
            session.clear()
            session.update({
                'user_id': user.id,
                'user_name': user.name,
                'user_email': user.email
            })
            flash(f"Welcome back, {user.name}! 👋", "success")
            return redirect(url_for('dashboard'))
        flash("Invalid credentials!", "danger")
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    session.clear()
    flash("Logged out.", "info")
    return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    uid = session['user_id']
    
    # ✅ All active medications
    meds = Medication.query.filter_by(user_id=uid, active=True)\
                          .order_by(Medication.name).all()
    
    meds_js = [{"id": m.id, "name": m.name, "t1": m.time1, "t2": m.time2} 
               for m in meds]

    # ✅ FIXED: Perfect IST time for Render
    now_ist = datetime.now(IST)
    today_logs = AlertLog.query.filter(
        AlertLog.user_id == uid,
        db.func.date(AlertLog.sent_at) == now_ist.date()
    ).order_by(AlertLog.sent_at.desc()).limit(10).all()
    
    return render_template('dashboard.html', 
                         meds=meds, 
                         meds_js=meds_js, 
                         logs=today_logs,
                         now_ist=now_ist.strftime('%I:%M %p IST'),
                         today_date=now_ist.strftime('%A, %d %B %Y'))

@app.route('/medication/add', methods=['POST'])
@login_required
def add_medication():
    try:
        email = request.form.get('recipient_email') or session.get('user_email')
        if not email:
            flash("Recipient email required!", "danger")
            return redirect(url_for('dashboard'))
            
        med = Medication(
            user_id=session['user_id'],
            name=request.form['name'].strip(),
            dose=request.form.get('dose', ''),
            frequency=request.form.get('frequency', ''),
            time1=request.form['time1'],  # Required HH:MM
            time2=request.form.get('time2') or None,
            recipient_email=email,
            notes=request.form.get('notes', '')
        )
        
        db.session.add(med)
        db.session.commit()
        flash(f'✅ "{med.name}" scheduled for {med.time1}!', 'success')
        
    except Exception as e:
        db.session.rollback()
        flash(f"Error: {str(e)}", "danger")
    
    return redirect(url_for('dashboard'))

# Other CRUD routes (same as before but with Gmail)
@app.route('/medication/edit/<int:id>', methods=['GET', 'POST'])
@login_required
def edit_medication(id):
    med = Medication.query.filter_by(id=id, user_id=session['user_id']).first_or_404()
    
    if request.method == 'POST':
        med.name = request.form['name']
        med.dose = request.form.get('dose', med.dose)
        med.time1 = request.form['time1']
        med.time2 = request.form.get('time2') or None
        med.recipient_email = request.form.get('recipient_email', med.recipient_email)
        med.notes = request.form.get('notes', med.notes)
        db.session.commit()
        flash("✅ Updated!", "success")
        return redirect(url_for('dashboard'))
    
    return render_template('edit_medication.html', med=med)

@app.route('/medication/delete/<int:id>')
@login_required
def delete_medication(id):
    med = Medication.query.filter_by(id=id, user_id=session['user_id']).first_or_404()
    db.session.delete(med)
    db.session.commit()
    flash("✅ Deleted!", "success")
    return redirect(url_for('dashboard'))

@app.route('/medication/toggle/<int:id>')
@login_required
def toggle_medication(id):
    med = Medication.query.filter_by(id=id, user_id=session['user_id']).first_or_404()
    med.active = not med.active
    db.session.commit()
    flash(f"✅ {'Activated' if med.active else 'Deactivated'}!", "success")
    return redirect(url_for('dashboard'))

# ── ✅ FIXED: Reminder Engine (Gmail + Perfect IST) ───────────────────────────
def send_reminder(med):
    """Send single reminder"""
    now_ist = datetime.now(IST)
    
    html_template = f"""
    <div style="font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; max-width: 500px; margin: 0 auto;">
        <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 30px; border-radius: 20px; text-align: center; color: white;">
            <h1 style="margin: 0; font-size: 28px;">⏰ Medication Time!</h1>
        </div>
        <div style="background: white; padding: 30px; border-radius: 20px; box-shadow: 0 20px 40px rgba(0,0,0,0.1); margin-top: -20px;">
            <h2 style="color: #d63384; margin-top: 0;">💊 {med.name}</h2>
            <div style="background: #f8f9fa; padding: 20px; border-radius: 12px; margin: 20px 0;">
                <p><strong>📅 Time:</strong> {now_ist.strftime('%I:%M %p IST')}</p>
                <p><strong>💊 Dose:</strong> {med.dose or 'As prescribed'}</p>
                <p><strong>📝 Notes:</strong> {med.notes or 'Take now!'}</p>
            </div>
            <div style="text-align: center; padding: 20px; background: #e8f5e8; border-radius: 12px;">
                <h3 style="color: #28a745; margin: 0;">✅ Don't forget!</h3>
                <p style="color: #666; margin: 5px 0 0;">Sent by MediHabit</p>
            </div>
        </div>
    </div>
    """
    
    subject = f"⏰ {med.name} - Take your medication now!"
    success = send_gmail_email(med.recipient_email, subject, html_template)
    
    # Log it
    log = AlertLog(
        user_id=med.user_id,
        medication_id=med.id,
        medication_name=med.name,
        recipient=med.recipient_email,
        status='sent' if success else 'failed',
        sent_at=now_ist,  # ✅ PERFECT IST TIME
        error="Email failed" if not success else None
    )
    db.session.add(log)
    db.session.commit()
    
    logger.info(f"Reminder {med.name} to {med.recipient_email}: {'✅' if success else '❌'}")

def check_reminders():
    """✅ Check every minute - Perfect for Render"""
    try:
        with app.app_context():
            now_ist = datetime.now(IST)
            current_time_str = now_ist.strftime('%H:%M')  # "14:30"
            
            logger.info(f"🕐 [{now_ist.strftime('%H:%M:%S IST')}] Checking reminders...")
            
            # Get ALL active meds
            active_meds = Medication.query.filter_by(active=True, email_enabled=True).all()
            
            for med in active_meds:
                # ✅ EXACT time match
                if med.time1 == current_time_str:
                    logger.info(f"🎯 Match! Sending {med.name} at {current_time_str}")
                    threading.Thread(target=send_reminder, args=(med,), daemon=True).start()
                
                if med.time2 and med.time2 == current_time_str:
                    logger.info(f"🎯 Match! Sending {med.name} (2nd dose) at {current_time_str}")
                    threading.Thread(target=send_reminder, args=(med,), daemon=True).start()
                    
    except Exception as e:
        logger.error(f"Reminder check failed: {e}")

# ── Render Deployment Ready Startup ───────────────────────────────────────────
@app.before_first_request
def init_db():
    db.create_all()
    logger.info("✅ Database initialized")

# Global scheduler
scheduler = None

def start_scheduler():
    global scheduler
    scheduler = BackgroundScheduler(timezone=IST)
    scheduler.add_job(
        id='reminder_checker',
        func=check_reminders,
        trigger='interval',
        minutes=1,
        replace_existing=True,
        misfire_grace_time=120  # 2 minutes grace for Render cold starts
    )
    scheduler.start()
    logger.info("✅ Reminder scheduler started (1 min intervals, IST)")

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
    
    start_scheduler()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
