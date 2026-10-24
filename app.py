import os
import sqlite3
import csv
import io
import smtplib
import ssl
import re
from email.message import EmailMessage
from functools import wraps
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, Response, session
from dotenv import load_dotenv

load_dotenv()


app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "CHANGE_THIS_SECRET_BEFORE_DEPLOYMENT")
DB_NAME = os.path.join(os.path.dirname(__file__), "complaints.db")

# Configure a unique admin password using environment variables before deployment.
ADMIN_USERNAME = os.environ.get("CAMPUS_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("CAMPUS_ADMIN_PASSWORD", "")

# Set these environment variables to enable email notifications.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", SMTP_USERNAME)

# Replace each placeholder with the official inbox for that department.
DEPARTMENT_EMAILS = {
    "Maintenance": os.environ.get("EMAIL_MAINTENANCE", ""),
    "Electrical": os.environ.get("EMAIL_ELECTRICAL", ""),
    "IT": os.environ.get("EMAIL_IT", ""),
    "Sanitation": os.environ.get("EMAIL_SANITATION", ""),
    "Academic": os.environ.get("EMAIL_ACADEMIC", ""),
    "Hostel": os.environ.get("EMAIL_HOSTEL", ""),
    "Other": os.environ.get("EMAIL_GENERAL", "")
}

CATEGORY_DEPARTMENTS = {
    "Maintenance": "Maintenance Department",
    "Electrical": "Electrical Department",
    "IT": "IT Department",
    "Sanitation": "Sanitation Department",
    "Academic": "Academic Affairs",
    "Hostel": "Hostel Administration",
    "Other": "General Administration"
}
URGENT_WORDS = ["urgent", "emergency", "danger", "fire", "shock", "injury",
                "unsafe", "leak", "smoke", "short circuit"]

def get_db():
    connection = sqlite3.connect(DB_NAME)
    connection.row_factory = sqlite3.Row
    return connection

def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_name TEXT NOT NULL,
                student_id TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL,
                department TEXT NOT NULL,
                location TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TEXT NOT NULL
            )
        """)

def detect_priority(text):
    text = text.lower()
    return "High" if any(word in text for word in URGENT_WORDS) else "Normal"

def send_department_email(complaint):
    recipient = DEPARTMENT_EMAILS.get(complaint["category"], "")
    if not recipient:
        app.logger.warning("No department email configured for category %s", complaint["category"])
        return False, "Email not configured for this department."

    if not all([SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, MAIL_FROM]):
        app.logger.warning("SMTP environment variables are incomplete.")
        return False, "SMTP settings are incomplete."

    message = EmailMessage()
    message["Subject"] = f"Campus complaint #{complaint['id']}: {complaint['title']}"
    message["From"] = MAIL_FROM
    message["To"] = recipient
    message.set_content(
        f"""A new campus complaint has been submitted.

Complaint ID: {complaint['id']}
Category: {complaint['category']}
Department: {complaint['department']}
Priority: {complaint['priority']}
Student name: {complaint['student_name']}
Student ID: {complaint['student_id']}
Location: {complaint['location']}
Submitted at: {complaint['created_at']}

Title:
{complaint['title']}

Description:
{complaint['description']}

Please review this complaint and update its status in the Campus Complaint Management System.
"""
    )
    context = ssl.create_default_context()
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(message)
    return True, "Email notification sent."

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            flash("Administrator login is required to access reports.", "error")
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped

@app.route("/")
def index():
    return render_template("index.html", categories=CATEGORY_DEPARTMENTS)

@app.route("/submit", methods=["POST"])
def submit():
    fields = {
        "student_name": request.form.get("student_name", "").strip(),
        "student_id": request.form.get("student_id", "").strip(),
        "title": request.form.get("title", "").strip(),
        "description": request.form.get("description", "").strip(),
        "category": request.form.get("category", "").strip(),
        "location": request.form.get("location", "").strip()
    }
    if not all(fields.values()):
        flash("Please complete all fields and select a category.", "error")
        return redirect(url_for("index"))

    if fields["category"] not in CATEGORY_DEPARTMENTS:
        flash("Please select a valid complaint category.", "error")
        return redirect(url_for("index"))

    department = CATEGORY_DEPARTMENTS[fields["category"]]
    priority = detect_priority(fields["title"] + " " + fields["description"])
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    with get_db() as db:
        cursor = db.execute("""
            INSERT INTO complaints
            (student_name, student_id, title, description, category, department,
             location, priority, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending', ?)
        """, (fields["student_name"], fields["student_id"], fields["title"],
              fields["description"], fields["category"], department, fields["location"],
              priority, created_at))
        complaint_id = cursor.lastrowid
        row = db.execute("SELECT * FROM complaints WHERE id=?", (complaint_id,)).fetchone()
        complaint = dict(row)

    # Email errors do not discard the saved complaint.
    try:
        email_sent, email_message = send_department_email(complaint)
    except Exception:
        app.logger.exception("Department email notification failed for complaint #%s", complaint_id)
        email_sent, email_message = False, "Complaint saved, but the email could not be sent. Check SMTP and department email settings."

    return render_template("submitted.html", complaint_id=complaint_id,
                           category=fields["category"], department=department,
                           priority=priority, email_sent=email_sent,
                           email_message=email_message)

@app.route("/track", methods=["GET", "POST"])
def track():
    complaint = None
    if request.method == "POST":
        complaint_id = request.form.get("complaint_id", "").strip()
        student_id = request.form.get("student_id", "").strip()
        with get_db() as db:
            complaint = db.execute(
                "SELECT id, student_id, title, category, department, location, priority, status, created_at "
                "FROM complaints WHERE id=? AND student_id=?",
                (complaint_id, student_id)
            ).fetchone()
        if not complaint:
            flash("Complaint not found. Check the complaint ID and student ID.", "error")
    return render_template("track.html", complaint=complaint)

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if not ADMIN_PASSWORD:
            flash("Admin password is not configured. Set CAMPUS_ADMIN_PASSWORD in the terminal, then restart the app.", "error")
        elif username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session.clear()
            session["is_admin"] = True
            session["admin_username"] = username
            flash("Administrator login successful.", "success")
            destination = request.args.get("next", "")
            if not destination.startswith("/") or destination.startswith("//"):
                destination = url_for("admin")
            return redirect(destination)
        else:
            flash("Incorrect administrator username or password.", "error")
    return render_template("admin_login.html")

@app.route("/admin/logout")
def admin_logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))

@app.route("/admin")
@admin_required
def admin():
    status = request.args.get("status", "All")
    category = request.args.get("category", "All")
    query = "SELECT * FROM complaints WHERE 1=1"
    params = []
    if status != "All":
        query += " AND status=?"
        params.append(status)
    if category != "All":
        query += " AND category=?"
        params.append(category)
    query += " ORDER BY CASE priority WHEN 'High' THEN 0 ELSE 1 END, id DESC"
    with get_db() as db:
        complaints = db.execute(query, params).fetchall()
        counts = {
            "total": db.execute("SELECT COUNT(*) FROM complaints").fetchone()[0],
            "pending": db.execute("SELECT COUNT(*) FROM complaints WHERE status='Pending'").fetchone()[0],
            "progress": db.execute("SELECT COUNT(*) FROM complaints WHERE status='In Progress'").fetchone()[0],
            "resolved": db.execute("SELECT COUNT(*) FROM complaints WHERE status='Resolved'").fetchone()[0]
        }
    return render_template("admin.html", complaints=complaints, status=status,
                           category=category, categories=list(CATEGORY_DEPARTMENTS.keys()),
                           counts=counts)

@app.route("/update/<int:complaint_id>", methods=["POST"])
@admin_required
def update(complaint_id):
    new_status = request.form.get("status", "")
    if new_status not in {"Pending", "In Progress", "Resolved", "Rejected"}:
        flash("Invalid status.", "error")
    else:
        with get_db() as db:
            db.execute("UPDATE complaints SET status=? WHERE id=?", (new_status, complaint_id))
        flash(f"Complaint #{complaint_id} updated to {new_status}.", "success")
    return redirect(url_for("admin"))

@app.route("/export")
@admin_required
def export():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Complaint ID", "Student Name", "Student ID", "Title", "Description",
                     "Category", "Department", "Location", "Priority", "Status", "Created At"])
    with get_db() as db:
        rows = db.execute("SELECT * FROM complaints ORDER BY id DESC").fetchall()
    for row in rows:
        writer.writerow([row["id"], row["student_name"], row["student_id"], row["title"],
                         row["description"], row["category"], row["department"], row["location"],
                         row["priority"], row["status"], row["created_at"]])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=campus_complaint_report.csv"})

init_db()

if __name__ == "__main__":
    app.run(debug=True)
