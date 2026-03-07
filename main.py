import os
import random
import string
import secrets
from pathlib import Path
import psycopg2
import requests
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, flash
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET")
if not app.secret_key:
    raise RuntimeError("SESSION_SECRET environment variable is required")

DATABASE_URL = os.environ.get("DATABASE_URL")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Diamond Dogs <noreply@mail.diamonddogs.ca>")

UPLOAD_DIR = Path(app.static_folder) / "uploads" / "profile-photos"
UPLOAD_URL_PREFIX = "uploads/profile-photos"
MAX_UPLOAD_SIZE_BYTES = 5 * 1024 * 1024
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

ALLOWED_EMAILS = [
    "luke.david.reimer@gmail.com",
    "forster.graham@gmail.com",
    "clockwerks77@gmail.com",
    "gavyn.mcleod@gmail.com"
]

EMAIL_TO_NAME = {
    "clockwerks77@gmail.com": "Adam",
    "luke.david.reimer@gmail.com": "Luke",
    "forster.graham@gmail.com": "Graham",
    "gavyn.mcleod@gmail.com": "Gavyn"
}


def get_name(email):
    if email:
        # First check if user has a custom display_name in database
        try:
            conn = get_db()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT display_name FROM users WHERE email = %s", (email.lower().strip(),))
            user = cursor.fetchone()
            conn.close()
            if user and user.get("display_name"):
                return user["display_name"]
        except:
            pass
        return EMAIL_TO_NAME.get(email.lower().strip(), email.split('@')[0])
    return ""


def get_user_profile(email):
    """Get full user profile including profile picture"""
    if not email:
        return None
    try:
        conn = get_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute("SELECT email, display_name, profile_picture FROM users WHERE email = %s", (email.lower().strip(),))
        user = cursor.fetchone()
        conn.close()
        return user
    except:
        return None


def is_allowed_email(email):
    return email.lower().strip() in [e.lower() for e in ALLOWED_EMAILS]


def generate_short_id(length=5):
    chars = string.ascii_lowercase + string.digits
    return ''.join(random.choice(chars) for _ in range(length))


def generate_token():
    return secrets.token_urlsafe(32)


def send_verification_email(to_email, token, request_url_root):
    if not RESEND_API_KEY:
        print("ERROR: Resend not configured - missing RESEND_API_KEY")
        return False, "Missing RESEND_API_KEY"
    
    verify_url = request_url_root.rstrip("/") + url_for("verify_email", token=token)

    email_payload = {
        "from": RESEND_FROM_EMAIL,
        "to": [to_email],
        "subject": "Verify your Diamond Dogs Scheduler account",
        "html": f"""
        <h2>Welcome to Diamond Dogs Scheduler!</h2>
        <p>Click the link below to verify your email and set up your password:</p>
        <p><a href="{verify_url}" style="background-color: #4F46E5; color: white; padding: 12px 24px; text-decoration: none; border-radius: 8px; display: inline-block;">Verify Email</a></p>
        <p>Or copy this link: {verify_url}</p>
        <p>This link expires in 24 hours.</p>
        """
    }
    
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type": "application/json"
            },
            json=email_payload,
            timeout=10
        )

        if response.status_code == 403 and "domain is not verified" in response.text:
            email_payload["from"] = "onboarding@resend.dev"
            response = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json"
                },
                json=email_payload,
                timeout=10
            )

        if response.status_code in (200, 201):
            return True, None

        error_summary = f"Resend rejected send (status={response.status_code}): {response.text[:300]}"
        print(f"ERROR: {error_summary}")
        return False, error_summary
    except Exception as e:
        print(f"ERROR: Failed to send email: {e}")
        return False, str(e)


def get_db():
    conn = psycopg2.connect(DATABASE_URL)
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS polls (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            admin_email TEXT NOT NULL,
            invite_emails TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS dates (
            id SERIAL PRIMARY KEY,
            poll_id TEXT NOT NULL REFERENCES polls(id),
            date TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS votes (
            id SERIAL PRIMARY KEY,
            date_id INTEGER NOT NULL REFERENCES dates(id),
            user_email TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('yes', 'no', 'maybe')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(date_id, user_email)
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            is_verified BOOLEAN DEFAULT FALSE,
            display_name TEXT,
            profile_picture TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Add columns if they don't exist (for existing tables)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_picture TEXT")
    except:
        pass
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS verification_tokens (
            id SERIAL PRIMARY KEY,
            email TEXT NOT NULL,
            token TEXT UNIQUE NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP NOT NULL
        )
    ''')
    
    conn.commit()
    conn.close()


init_db()


@app.context_processor
def utility_processor():
    return dict(get_name=get_name)


@app.route("/")
def home():
    if not session.get("user_email"):
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        
        if not email:
            flash("Please enter your email", "error")
            return redirect(url_for("login"))
        
        if not is_allowed_email(email):
            flash("Sorry, this email is not authorized to use this app", "error")
            return redirect(url_for("login"))
        
        conn = get_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()
        conn.close()
        
        if not user:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (email) VALUES (%s) ON CONFLICT DO NOTHING", (email,))
            conn.commit()
            conn.close()
            
            token = generate_token()
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO verification_tokens (email, token, expires_at) VALUES (%s, %s, NOW() + INTERVAL '24 hours')",
                (email, token)
            )
            conn.commit()
            conn.close()
            
            sent, error = send_verification_email(email, token, request.url_root)
            if sent:
                flash("Welcome! We've sent you a verification email. Please check your inbox to set up your password.", "success")
            else:
                flash("We could not send your verification email right now. Please try again in a minute.", "error")
                if error:
                    print(f"ERROR: Verification email send failed for {email}: {error}")
            return redirect(url_for("login"))
        
        if not user["is_verified"]:
            token = generate_token()
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM verification_tokens WHERE email = %s", (email,))
            cursor.execute(
                "INSERT INTO verification_tokens (email, token, expires_at) VALUES (%s, %s, NOW() + INTERVAL '24 hours')",
                (email, token)
            )
            conn.commit()
            conn.close()
            
            sent, error = send_verification_email(email, token, request.url_root)
            if sent:
                flash("Your account is not verified. We've sent a new verification email.", "error")
            else:
                flash("Your account is not verified, but we could not send a new email yet. Please try again shortly.", "error")
                if error:
                    print(f"ERROR: Verification email resend failed for {email}: {error}")
            return redirect(url_for("login"))
        
        if not password:
            flash("Please enter your password", "error")
            return redirect(url_for("login"))
        
        if not check_password_hash(user["password_hash"], password):
            flash("Incorrect password", "error")
            return redirect(url_for("login"))
        
        session["user_email"] = email
        flash(f"Welcome back, {get_name(email)}!", "success")
        return redirect(url_for("home"))
    
    return render_template("login.html")


@app.route("/verify/<token>")
def verify_email(token):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute(
        "SELECT * FROM verification_tokens WHERE token = %s AND expires_at > NOW()",
        (token,)
    )
    token_record = cursor.fetchone()
    
    if not token_record:
        conn.close()
        flash("Invalid or expired verification link", "error")
        return redirect(url_for("login"))
    
    email = token_record["email"]
    session["pending_verification_email"] = email
    conn.close()
    
    return redirect(url_for("set_password"))


@app.route("/set-password", methods=["GET", "POST"])
def set_password():
    email = session.get("pending_verification_email")
    
    if not email:
        flash("Please verify your email first", "error")
        return redirect(url_for("login"))
    
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        
        if len(password) < 6:
            flash("Password must be at least 6 characters", "error")
            return redirect(url_for("set_password"))
        
        if password != confirm_password:
            flash("Passwords do not match", "error")
            return redirect(url_for("set_password"))
        
        conn = get_db()
        cursor = conn.cursor()
        
        cursor.execute(
            "UPDATE users SET password_hash = %s, is_verified = TRUE WHERE email = %s",
            (generate_password_hash(password), email)
        )
        cursor.execute("DELETE FROM verification_tokens WHERE email = %s", (email,))
        conn.commit()
        conn.close()
        
        session.pop("pending_verification_email", None)
        session["user_email"] = email
        
        flash("Password set successfully! You are now logged in.", "success")
        return redirect(url_for("home"))
    
    return render_template("set_password.html", email=email)


@app.route("/create")
def create_poll():
    if not session.get("user_email"):
        flash("Please log in first", "error")
        return redirect(url_for("login"))
    return render_template("home.html", user_email=session.get("user_email"))


@app.route("/create", methods=["POST"])
def create_poll_step1():
    admin_email = request.form.get("admin_email", "").strip()
    poll_name = request.form.get("poll_name", "").strip()
    
    if not admin_email or not poll_name:
        flash("Please fill in all fields", "error")
        return redirect(url_for("home"))
    
    if not is_allowed_email(admin_email):
        flash("Sorry, this email is not authorized to use this app", "error")
        return redirect(url_for("home"))
    
    session["admin_email"] = admin_email
    session["poll_name"] = poll_name
    session["user_email"] = admin_email
    
    return redirect(url_for("calendar_view"))


@app.route("/calendar")
def calendar_view():
    if "admin_email" not in session or "poll_name" not in session:
        flash("Please start by creating a poll", "error")
        return redirect(url_for("home"))
    
    return render_template("calendar.html", poll_name=session["poll_name"])


@app.route("/finalize", methods=["POST"])
def finalize_poll():
    if "admin_email" not in session or "poll_name" not in session:
        return jsonify({"error": "Session expired"}), 400
    
    data = request.get_json()
    selected_dates = data.get("dates", [])
    
    if not selected_dates:
        return jsonify({"error": "Please select at least one date"}), 400
    
    poll_id = generate_short_id()
    
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute(
        "INSERT INTO polls (id, name, admin_email) VALUES (%s, %s, %s)",
        (poll_id, session["poll_name"], session["admin_email"])
    )
    
    date_ids = []
    for date_str in selected_dates:
        cursor.execute(
            "INSERT INTO dates (poll_id, date) VALUES (%s, %s) RETURNING id",
            (poll_id, date_str)
        )
        date_ids.append(cursor.fetchone()[0])
    
    for date_id in date_ids:
        cursor.execute(
            "INSERT INTO votes (date_id, user_email, status) VALUES (%s, %s, 'yes')",
            (date_id, session["admin_email"])
        )
    
    conn.commit()
    conn.close()
    
    session.pop("poll_name", None)
    
    return jsonify({"poll_id": poll_id})


@app.route("/share/<poll_id>")
def share_poll(poll_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    conn.close()
    
    if not poll:
        flash("Poll not found", "error")
        return redirect(url_for("home"))
    
    poll_url = request.url_root.rstrip("/") + url_for("view_poll", poll_id=poll_id)
    
    return render_template("share.html", poll=poll, poll_url=poll_url)


@app.route("/share/<poll_id>/update-emails", methods=["POST"])
def update_invite_emails(poll_id):
    data = request.get_json()
    emails = data.get("emails", "")
    
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE polls SET invite_emails = %s WHERE id = %s", (emails, poll_id))
    conn.commit()
    conn.close()
    
    return jsonify({"success": True})


@app.route("/poll/<poll_id>")
def view_poll(poll_id):
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    
    if not poll:
        conn.close()
        flash("Poll not found", "error")
        return redirect(url_for("home"))
    
    cursor.execute("SELECT * FROM dates WHERE poll_id = %s ORDER BY date", (poll_id,))
    dates = cursor.fetchall()
    
    votes_dict = {}
    participants = set()
    yes_counts = {}
    dates_to_remove = []
    
    for date in dates:
        cursor.execute(
            "SELECT user_email, status FROM votes WHERE date_id = %s",
            (date["id"],)
        )
        date_votes = cursor.fetchall()
        
        if date_votes and all(v["status"] == "no" for v in date_votes):
            dates_to_remove.append(date["id"])
            continue
            
        votes_dict[date["id"]] = {v["user_email"]: v["status"] for v in date_votes}
        yes_counts[date["id"]] = sum(1 for v in date_votes if v["status"] == "yes")
        for v in date_votes:
            participants.add(v["user_email"])
    
    for date_id in dates_to_remove:
        cursor.execute("DELETE FROM votes WHERE date_id = %s", (date_id,))
        cursor.execute("DELETE FROM dates WHERE id = %s", (date_id,))
    
    if dates_to_remove:
        conn.commit()
        dates = [d for d in dates if d["id"] not in dates_to_remove]
    
    conn.close()
    
    max_yes = max(yes_counts.values()) if yes_counts else 0
    best_dates = [d_id for d_id, count in yes_counts.items() if count == max_yes and max_yes > 0]
    
    user_email = session.get("user_email")
    
    return render_template(
        "vote.html",
        poll=poll,
        dates=dates,
        votes_dict=votes_dict,
        participants=sorted(participants),
        user_email=user_email,
        best_dates=best_dates
    )


@app.route("/poll/<poll_id>/delete", methods=["POST"])
def delete_poll(poll_id):
    user_email = session.get("user_email")
    
    if not user_email:
        flash("Please log in first", "error")
        return redirect(url_for("view_poll", poll_id=poll_id))
    
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute("SELECT admin_email FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    
    if not poll:
        conn.close()
        flash("Poll not found", "error")
        return redirect(url_for("home"))
    
    if poll["admin_email"].lower() != user_email.lower():
        conn.close()
        flash("Only the poll creator can delete this poll", "error")
        return redirect(url_for("view_poll", poll_id=poll_id))
    
    cursor.execute("SELECT id FROM dates WHERE poll_id = %s", (poll_id,))
    date_ids = [row["id"] for row in cursor.fetchall()]
    
    for date_id in date_ids:
        cursor.execute("DELETE FROM votes WHERE date_id = %s", (date_id,))
    
    cursor.execute("DELETE FROM dates WHERE poll_id = %s", (poll_id,))
    cursor.execute("DELETE FROM polls WHERE id = %s", (poll_id,))
    
    conn.commit()
    conn.close()
    
    flash("Poll deleted successfully", "success")
    return redirect(url_for("dashboard"))




@app.route("/poll/<poll_id>/vote", methods=["POST"])
def submit_vote(poll_id):
    if "user_email" not in session:
        return jsonify({"error": "Please log in first"}), 401
    
    data = request.get_json()
    date_id = data.get("date_id")
    status = data.get("status")
    
    if not date_id or status not in ["yes", "no", "maybe"]:
        return jsonify({"error": "Invalid vote data"}), 400
    
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute(
        '''INSERT INTO votes (date_id, user_email, status)
           VALUES (%s, %s, %s)
           ON CONFLICT(date_id, user_email)
           DO UPDATE SET status = EXCLUDED.status''',
        (date_id, session["user_email"], status)
    )
    
    date_removed = False
    if status == "no":
        cursor.execute("SELECT status FROM votes WHERE date_id = %s", (date_id,))
        all_votes = cursor.fetchall()
        if all_votes and all(v["status"] == "no" for v in all_votes):
            cursor.execute("DELETE FROM votes WHERE date_id = %s", (date_id,))
            cursor.execute("DELETE FROM dates WHERE id = %s", (date_id,))
            date_removed = True
    
    conn.commit()
    conn.close()
    
    return jsonify({"success": True, "date_removed": date_removed})


@app.route("/dashboard")
def dashboard():
    user_email = session.get("user_email")
    
    if not user_email:
        return redirect(url_for("login"))
    
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    # Show ALL polls, not just user's polls
    cursor.execute('''
        SELECT * FROM polls
        ORDER BY created_at DESC
    ''')
    
    polls = cursor.fetchall()
    conn.close()
    
    return render_template("dashboard.html", polls=polls, user_email=user_email)




@app.route("/profile", methods=["GET", "POST"])
def profile():
    user_email = session.get("user_email")
    
    if not user_email:
        flash("Please log in first", "error")
        return redirect(url_for("login"))
    
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    if request.method == "POST":
        display_name = request.form.get("display_name", "").strip()
        profile_picture = request.form.get("profile_picture", "").strip()
        
        cursor.execute(
            "UPDATE users SET display_name = %s, profile_picture = %s WHERE email = %s",
            (display_name if display_name else None, profile_picture if profile_picture else None, user_email)
        )
        conn.commit()
        flash("Profile updated successfully!", "success")
    
    cursor.execute("SELECT email, display_name, profile_picture FROM users WHERE email = %s", (user_email,))
    user = cursor.fetchone()
    conn.close()
    
    default_name = EMAIL_TO_NAME.get(user_email.lower().strip(), user_email.split('@')[0])
    
    return render_template("profile.html", user=user, default_name=default_name)


@app.route("/profile/upload-photo", methods=["POST"])
def upload_profile_photo():
    user_email = session.get("user_email")
    
    if not user_email:
        return jsonify({"error": "Not logged in"}), 401
    
    if "photo" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    
    file = request.files["photo"]
    if file.filename == "":
        return jsonify({"error": "No file selected"}), 400

    original_filename = secure_filename(file.filename)
    if not original_filename:
        return jsonify({"error": "Invalid file name"}), 400
    
    allowed_extensions = {"png", "jpg", "jpeg", "gif", "webp"}
    ext = original_filename.rsplit(".", 1)[-1].lower() if "." in original_filename else ""
    if ext not in allowed_extensions:
        return jsonify({"error": "Invalid file type. Please upload an image."}), 400
    
    try:
        file_data = file.read()
        if len(file_data) > MAX_UPLOAD_SIZE_BYTES:
            return jsonify({"error": "File is too large. Max size is 5MB."}), 400

        user_slug = secure_filename(user_email.replace("@", "_"))
        saved_filename = f"{user_slug}_{secrets.token_hex(8)}.{ext}"
        output_path = UPLOAD_DIR / saved_filename

        with open(output_path, "wb") as out_file:
            out_file.write(file_data)

        photo_url = url_for("static", filename=f"{UPLOAD_URL_PREFIX}/{saved_filename}", _external=True)
        
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET profile_picture = %s WHERE email = %s",
            (photo_url, user_email)
        )
        conn.commit()
        conn.close()
        
        return jsonify({"success": True, "url": photo_url})
    except Exception as e:
        print(f"ERROR uploading photo: {e}")
        return jsonify({"error": "Failed to upload photo. Please try again."}), 500


@app.route("/logout")
def logout():
    session.pop("user_email", None)
    session.pop("admin_email", None)
    session.pop("poll_name", None)
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    )
