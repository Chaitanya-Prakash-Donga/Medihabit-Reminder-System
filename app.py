"""
=============================================================
  MediHabit Reminder System - Full Backend
  Stack : Python 3.10+ | SQLite | smtplib | Flask | APScheduler
=============================================================
"""

import sqlite3
import hashlib
import secrets
import smtplib
import re
import json
import threading
import os
import pytz
from flask import Flask, jsonify, request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from typing import Optional

# APScheduler for background scheduling
try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    SCHEDULER_AVAILABLE = True
except ImportError:
    SCHEDULER_AVAILABLE = False

# ── SMTP Configuration (Pulled from Render Env Vars) ──
SMTP_CONFIG = {
    "host": "smtp.gmail.com",
    "port": 587,
    "username": os.getenv("EMAIL_USER"), 
    "password": os.getenv("EMAIL_PASS"), # Use 16-char App Password
    "from_name": "MediHabit",
}

DB_PATH = "medihabit.db"

# ─────────────────────────────────────────────────────────────
# 1.  WEB FRAMEWORK INITIALIZATION (Fixes Gunicorn Error)
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return jsonify({
        "status": "MediHabit Backend Running",
        "time_ist": now_str()
    })

# ─────────────────────────────────────────────────────────────
# 2.  DATABASE & UTILITIES (Updated for IST)
# ─────────────────────────────────────────────────────────────
def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db() -> None:
    ddl = """
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        phone TEXT,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        timezone TEXT DEFAULT 'Asia/Kolkata',
        is_active INTEGER DEFAULT 1,
        created_at TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS medicines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        dosage TEXT NOT NULL,
        form TEXT DEFAULT 'Tablet',
        frequency TEXT NOT NULL,
        times_of_day TEXT NOT NULL,
        start_date TEXT NOT NULL,
        end_date TEXT,
        stock_count INTEGER DEFAULT 0,
        low_stock_alert INTEGER DEFAULT 5,
        notes TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TEXT,
        updated_at TEXT
    );

    CREATE TABLE IF NOT EXISTS reminder_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
        scheduled_at TEXT NOT NULL,
        channel TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        sent_at TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS adherence (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        medicine_id INTEGER NOT NULL REFERENCES medicines(id) ON DELETE CASCADE,
        scheduled_date TEXT NOT NULL,
        scheduled_time TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        marked_at TEXT
    );
    """
    with get_connection() as conn:
        conn.executescript(ddl)

def now_str() -> str:
    # Forces Asia/Kolkata regardless of server location
    ist = pytz.timezone('Asia/Kolkata')
    return datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")

def today_str() -> str:
    ist = pytz.timezone('Asia/Kolkata')
    return datetime.now(ist).strftime("%Y-%m-%d")

def current_hhmm() -> str:
    ist = pytz.timezone('Asia/Kolkata')
    return datetime.now(ist).strftime("%H:%M")

# ─────────────────────────────────────────────────────────────
# 3.  EMAIL SERVICE
# ─────────────────────────────────────────────────────────────
class EmailService:
    @staticmethod
    def _send(to_email: str, subject: str, html_body: str) -> bool:
        if not SMTP_CONFIG["username"] or not SMTP_CONFIG["password"]:
            print("[EMAIL ERROR] Credentials missing in Env Vars.")
            return False
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = f"{SMTP_CONFIG['from_name']} <{SMTP_CONFIG['username']}>"
            msg["To"] = to_email
            msg.attach(MIMEText(html_body, "html"))

            with smtplib.SMTP(SMTP_CONFIG["host"], SMTP_CONFIG["port"]) as server:
                server.starttls()
                server.login(SMTP_CONFIG["username"], SMTP_CONFIG["password"])
                server.sendmail(SMTP_CONFIG["username"], to_email, msg.as_string())
            return True
        except Exception as exc:
            print(f"[EMAIL ERROR] {exc}")
            return False

    @staticmethod
    def send_welcome(user_name: str, to_email: str):
        subject = "Welcome to MediHabit 💊"
        html = f"<h2>Welcome {user_name}!</h2><p>Your medicine reminder account is active.</p>"
        return EmailService._send(to_email, subject, html)

    @staticmethod
    def send_reminder(user_name: str, to_email: str, med_name: str, dosage: str, time: str):
        subject = f"⏰ Medicine Reminder: {med_name}"
        html = f"<h3>Hi {user_name},</h3><p>It's {time}. Please take <b>{med_name} ({dosage})</b>.</p>"
        return EmailService._send(to_email, subject, html)

# ─────────────────────────────────────────────────────────────
# 4.  SCHEDULER & SERVICES
# ─────────────────────────────────────────────────────────────
class ReminderService:
    @staticmethod
    def fire_reminder(medicine_id: int):
        with get_connection() as conn:
            med = conn.execute("SELECT * FROM medicines WHERE id=?", (medicine_id,)).fetchone()
            user = conn.execute("SELECT * FROM users WHERE id=?", (med["user_id"],)).fetchone()

        # Send Mail
        email_sent = EmailService.send_reminder(
            user["name"], user["email"], med["name"], med["dosage"], current_hhmm()
        )

        # Log Result
        status = "sent" if email_sent else "failed"
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO reminder_logs (user_id, medicine_id, scheduled_at, channel, status, sent_at, created_at) VALUES (?,?,'IST','email',?,?,?)",
                (user["id"], medicine_id, status, now_str(), now_str())
            )

class SchedulerService:
    def __init__(self):
        self.scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    def _check_reminders(self):
        now_time = current_hhmm()
        today = today_str()
        with get_connection() as conn:
            meds = conn.execute("SELECT * FROM medicines WHERE is_active=1").fetchall()
        
        for med in meds:
            times = json.loads(med["times_of_day"])
            if now_time in times:
                ReminderService.fire_reminder(med["id"])

    def start(self):
        if not self.scheduler.running:
            self.scheduler.add_job(self._check_reminders, 'interval', minutes=1)
            self.scheduler.start()

# ─────────────────────────────────────────────────────────────
# 5.  STARTUP (Runs on Render)
# ─────────────────────────────────────────────────────────────
# Initialize database and scheduler automatically
init_db()
rem_scheduler = SchedulerService()
rem_scheduler.start()

if __name__ == "__main__":
    # Local development run
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
