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
RESEND_FROM_EMAIL = os.environ.get("RESEND_FROM_EMAIL", "Templo <noreply@templobooker.com>")

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

FREE_POLL_LIMIT = 1
FREE_DATE_LIMIT = 15
VALID_ROLES = {"user", "admin"}
VALID_TIERS = {"free", "paid"}
ADMIN_EMAILS = {
    email.strip().lower()
    for email in os.environ.get("ADMIN_EMAILS", "luke.david.reimer@gmail.com").split(",")
    if email.strip()
}


def normalize_email(email):
    return (email or "").strip().lower()


def parse_invite_emails(raw_emails):
    if not raw_emails:
        return []

    invites = set()
    for entry in raw_emails.replace(",", "\n").splitlines():
        email = normalize_email(entry)
        if email and "@" in email:
            invites.add(email)
    return sorted(invites)


def serialize_invite_emails(emails):
    normalized = {
        normalize_email(email)
        for email in emails
        if normalize_email(email) and "@" in normalize_email(email)
    }
    return "\n".join(sorted(normalized))


def default_role_for_email(email):
    return "admin" if normalize_email(email) in ADMIN_EMAILS else "user"


def default_tier_for_email(email):
    return "paid" if default_role_for_email(email) == "admin" else "free"


def is_valid_date_string(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except (TypeError, ValueError):
        return False


def get_name(email):
    if email:
        normalized_email = normalize_email(email)
        # First check if user has a custom display_name in database
        try:
            conn = get_db()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT display_name FROM users WHERE email = %s", (normalized_email,))
            user = cursor.fetchone()
            conn.close()
            if user and user.get("display_name"):
                return user["display_name"]
        except:
            pass
        return EMAIL_TO_NAME.get(normalized_email, normalized_email.split('@')[0])
    return ""


def get_user_profile(email):
    """Get full user profile including profile picture"""
    if not email:
        return None
    try:
        normalized_email = normalize_email(email)
        conn = get_db()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        cursor.execute(
            "SELECT email, display_name, profile_picture, role, tier, is_verified FROM users WHERE email = %s",
            (normalized_email,)
        )
        user = cursor.fetchone()
        conn.close()
        return user
    except:
        return None


def is_allowed_email(email):
    return normalize_email(email) in [normalize_email(e) for e in ALLOWED_EMAILS]


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
        "subject": "Verify your Templo account",
        "html": f"""
        <h2>Welcome to Templo!</h2>
        <p>Click the link below to verify your email and set up your password:</p>
        <p><a href="{verify_url}" style="background-color: #4F46E5; color: white; padding: 12px 24px; text-decoration: none; border-radius: 8px; display: inline-block;">Verify Email</a></p>
        <p>Or copy this link: {verify_url}</p>
        <p>Templo: templobooker.com</p>
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


def get_current_user():
    user_email = normalize_email(session.get("user_email"))
    if not user_email:
        return None

    user = get_user_profile(user_email)
    if not user:
        return {
            "email": user_email,
            "display_name": None,
            "profile_picture": None,
            "role": default_role_for_email(user_email),
            "tier": default_tier_for_email(user_email),
            "is_verified": False
        }

    user["email"] = normalize_email(user["email"])
    user["role"] = (user.get("role") or default_role_for_email(user["email"])).lower()
    user["tier"] = (user.get("tier") or default_tier_for_email(user["email"])).lower()
    return user


def is_admin_user(user):
    return bool(user and user.get("role") == "admin")


def user_can_manage_poll(user, poll):
    if not user or not poll:
        return False
    if is_admin_user(user):
        return True
    return normalize_email(poll.get("admin_email")) == normalize_email(user.get("email"))


def user_can_access_poll(user, poll):
    if not user or not poll:
        return False
    if user_can_manage_poll(user, poll):
        return True

    invited_emails = parse_invite_emails(poll.get("invite_emails"))
    return normalize_email(user.get("email")) in invited_emails


def get_owned_poll_count(email):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM polls WHERE LOWER(admin_email) = %s", (normalize_email(email),))
    count = cursor.fetchone()[0]
    conn.close()
    return count


def delete_poll_records(cursor, poll_id):
    cursor.execute("SELECT id FROM dates WHERE poll_id = %s", (poll_id,))
    date_rows = cursor.fetchall()
    date_ids = [
        row["id"] if isinstance(row, dict) else row[0]
        for row in date_rows
    ]

    for date_id in date_ids:
        cursor.execute("DELETE FROM votes WHERE date_id = %s", (date_id,))

    cursor.execute("DELETE FROM dates WHERE poll_id = %s", (poll_id,))
    cursor.execute("DELETE FROM polls WHERE id = %s", (poll_id,))


def sync_admin_account(conn, email):
    normalized_email = normalize_email(email)
    if normalized_email not in ADMIN_EMAILS:
        return
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET role = 'admin', tier = 'paid' WHERE LOWER(email) = %s",
        (normalized_email,)
    )


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
            role TEXT DEFAULT 'user',
            tier TEXT DEFAULT 'free',
            display_name TEXT,
            profile_picture TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Add columns if they don't exist (for existing tables)
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name TEXT")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_picture TEXT")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT DEFAULT 'user'")
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT 'free'")
        cursor.execute("UPDATE users SET role = 'user' WHERE role IS NULL OR role = ''")
        cursor.execute("UPDATE users SET tier = 'free' WHERE tier IS NULL OR tier = ''")
        for admin_email in ADMIN_EMAILS:
            cursor.execute(
                "UPDATE users SET role = 'admin', tier = 'paid' WHERE LOWER(email) = %s",
                (admin_email,)
            )
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
    current_user = get_current_user()
    return dict(
        get_name=get_name,
        current_user=current_user,
        is_admin=is_admin_user(current_user)
    )


@app.route("/")
def home():
    if not session.get("user_email"):
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
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

        if user and email in ADMIN_EMAILS and (user.get("role") != "admin" or user.get("tier") != "paid"):
            conn = get_db()
            sync_admin_account(conn, email)
            conn.commit()
            conn.close()

            conn = get_db()
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            cursor.execute("SELECT * FROM users WHERE email = %s", (email,))
            user = cursor.fetchone()
            conn.close()
        
        if not user:
            conn = get_db()
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO users (email, role, tier) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (email, default_role_for_email(email), default_tier_for_email(email))
            )
            sync_admin_account(conn, email)
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
    cursor.execute("SELECT password_hash FROM users WHERE email = %s", (email,))
    user = cursor.fetchone()

    # If the account already has a password (e.g., email update flow), verification can complete immediately.
    if user and user.get("password_hash"):
        cursor.execute("UPDATE users SET is_verified = TRUE WHERE email = %s", (email,))
        cursor.execute("DELETE FROM verification_tokens WHERE email = %s", (email,))
        conn.commit()
        conn.close()
        flash("Email verified successfully. Please sign in.", "success")
        return redirect(url_for("login"))

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
    current_user = get_current_user()

    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("login"))

    owned_poll_count = get_owned_poll_count(current_user["email"])
    free_limit_reached = current_user["tier"] == "free" and owned_poll_count >= FREE_POLL_LIMIT

    return render_template(
        "home.html",
        user_email=current_user["email"],
        owned_poll_count=owned_poll_count,
        free_poll_limit=FREE_POLL_LIMIT,
        free_date_limit=FREE_DATE_LIMIT,
        can_create_poll=not free_limit_reached
    )


@app.route("/create", methods=["POST"])
def create_poll_step1():
    current_user = get_current_user()
    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("login"))

    poll_name = request.form.get("poll_name", "").strip()
    
    if not poll_name:
        flash("Please fill in all fields", "error")
        return redirect(url_for("create_poll"))
    if len(poll_name) > 120:
        flash("Poll name is too long (max 120 characters).", "error")
        return redirect(url_for("create_poll"))

    if current_user["tier"] == "free":
        owned_poll_count = get_owned_poll_count(current_user["email"])
        if owned_poll_count >= FREE_POLL_LIMIT:
            flash("Free tier allows 1 active poll at a time. Delete your existing poll or upgrade to Pro.", "error")
            return redirect(url_for("dashboard"))

    session["poll_name"] = poll_name
    session["poll_creator_email"] = current_user["email"]
    
    return redirect(url_for("calendar_view"))


@app.route("/calendar")
def calendar_view():
    current_user = get_current_user()
    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("login"))

    if "poll_name" not in session:
        flash("Please start by creating a poll", "error")
        return redirect(url_for("create_poll"))

    if normalize_email(session.get("poll_creator_email")) != normalize_email(current_user["email"]):
        flash("Poll creation session expired. Please start again.", "error")
        session.pop("poll_name", None)
        session.pop("poll_creator_email", None)
        return redirect(url_for("create_poll"))

    max_dates = FREE_DATE_LIMIT if current_user["tier"] == "free" else None
    return render_template(
        "calendar.html",
        poll_name=session["poll_name"],
        max_dates=max_dates
    )


@app.route("/finalize", methods=["POST"])
def finalize_poll():
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Please log in first"}), 401

    if "poll_name" not in session:
        return jsonify({"error": "Session expired"}), 400

    if normalize_email(session.get("poll_creator_email")) != normalize_email(current_user["email"]):
        return jsonify({"error": "Session expired"}), 400
    
    data = request.get_json(silent=True) or {}
    selected_dates = sorted(set(data.get("dates", [])))
    
    if not selected_dates:
        return jsonify({"error": "Please select at least one date"}), 400

    if any(not is_valid_date_string(date_str) for date_str in selected_dates):
        return jsonify({"error": "One or more selected dates are invalid."}), 400

    today_iso = datetime.utcnow().strftime("%Y-%m-%d")
    if any(date_str < today_iso for date_str in selected_dates):
        return jsonify({"error": "Past dates are not allowed."}), 400

    if current_user["tier"] == "free":
        if len(selected_dates) > FREE_DATE_LIMIT:
            return jsonify({"error": f"Free tier allows up to {FREE_DATE_LIMIT} dates per poll."}), 400
        owned_poll_count = get_owned_poll_count(current_user["email"])
        if owned_poll_count >= FREE_POLL_LIMIT:
            return jsonify({"error": "Free tier allows 1 active poll at a time. Delete your existing poll or upgrade."}), 400
    
    conn = get_db()
    cursor = conn.cursor()

    poll_id = generate_short_id()
    cursor.execute("SELECT 1 FROM polls WHERE id = %s", (poll_id,))
    while cursor.fetchone():
        poll_id = generate_short_id()
        cursor.execute("SELECT 1 FROM polls WHERE id = %s", (poll_id,))
    
    cursor.execute(
        "INSERT INTO polls (id, name, admin_email) VALUES (%s, %s, %s)",
        (poll_id, session["poll_name"], current_user["email"])
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
            (date_id, current_user["email"])
        )
    
    conn.commit()
    conn.close()
    
    session.pop("poll_name", None)
    session.pop("poll_creator_email", None)
    
    return jsonify({"poll_id": poll_id})


@app.route("/share/<poll_id>")
def share_poll(poll_id):
    current_user = get_current_user()
    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    
    if not poll:
        flash("Poll not found", "error")
        conn.close()
        return redirect(url_for("home"))

    if not user_can_manage_poll(current_user, poll):
        flash("Only the poll creator or an admin can manage sharing settings.", "error")
        conn.close()
        return redirect(url_for("view_poll", poll_id=poll_id))

    poll["invite_emails"] = serialize_invite_emails(parse_invite_emails(poll.get("invite_emails")))
    conn.close()
    
    poll_url = request.url_root.rstrip("/") + url_for("view_poll", poll_id=poll_id)
    
    return render_template("share.html", poll=poll, poll_url=poll_url)


@app.route("/share/<poll_id>/update-emails", methods=["POST"])
def update_invite_emails(poll_id):
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Please log in first"}), 401

    data = request.get_json(silent=True) or {}
    emails = data.get("emails", "")
    poll_name = (data.get("name") or "").strip()
    if poll_name and len(poll_name) > 120:
        return jsonify({"error": "Poll name is too long (max 120 characters)."}), 400
    
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()

    if not poll:
        conn.close()
        return jsonify({"error": "Poll not found"}), 404

    if not user_can_manage_poll(current_user, poll):
        conn.close()
        return jsonify({"error": "You do not have permission to edit this poll"}), 403

    invite_text = serialize_invite_emails(parse_invite_emails(emails))
    if poll_name:
        cursor.execute(
            "UPDATE polls SET name = %s, invite_emails = %s WHERE id = %s",
            (poll_name, invite_text, poll_id)
        )
    else:
        cursor.execute(
            "UPDATE polls SET invite_emails = %s WHERE id = %s",
            (invite_text, poll_id)
        )

    conn.commit()
    conn.close()
    
    return jsonify({"success": True, "invite_count": len(parse_invite_emails(invite_text))})


@app.route("/poll/<poll_id>")
def view_poll(poll_id):
    current_user = get_current_user()
    if not current_user:
        flash("Please log in first to view this poll.", "error")
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    
    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    
    if not poll:
        conn.close()
        flash("Poll not found", "error")
        return redirect(url_for("home"))

    if not user_can_access_poll(current_user, poll):
        conn.close()
        flash("You are not invited to this poll.", "error")
        return redirect(url_for("dashboard"))
    
    cursor.execute("SELECT * FROM dates WHERE poll_id = %s ORDER BY date", (poll_id,))
    dates = cursor.fetchall()
    
    votes_dict = {}
    participants = set()
    yes_counts = {}
    
    for date in dates:
        cursor.execute(
            "SELECT user_email, status FROM votes WHERE date_id = %s",
            (date["id"],)
        )
        date_votes = cursor.fetchall()
            
        votes_dict[date["id"]] = {v["user_email"]: v["status"] for v in date_votes}
        yes_counts[date["id"]] = sum(1 for v in date_votes if v["status"] == "yes")
        for v in date_votes:
            participants.add(v["user_email"])
    
    conn.close()
    
    max_yes = max(yes_counts.values()) if yes_counts else 0
    best_dates = [d_id for d_id, count in yes_counts.items() if count == max_yes and max_yes > 0]
    
    poll["can_manage"] = user_can_manage_poll(current_user, poll)
    
    return render_template(
        "vote.html",
        poll=poll,
        dates=dates,
        votes_dict=votes_dict,
        participants=sorted(participants),
        user_email=current_user["email"],
        best_dates=best_dates
    )


@app.route("/poll/<poll_id>/delete", methods=["POST"])
def delete_poll(poll_id):
    current_user = get_current_user()
    
    if not current_user:
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
    
    if not user_can_manage_poll(current_user, poll):
        conn.close()
        flash("Only the poll creator or an admin can delete this poll", "error")
        return redirect(url_for("view_poll", poll_id=poll_id))
    
    delete_poll_records(cursor, poll_id)
    
    conn.commit()
    conn.close()
    
    flash("Poll deleted successfully", "success")
    return redirect(url_for("dashboard"))




@app.route("/poll/<poll_id>/vote", methods=["POST"])
def submit_vote(poll_id):
    current_user = get_current_user()
    if not current_user:
        return jsonify({"error": "Please log in first"}), 401
    
    data = request.get_json(silent=True) or {}
    date_id = data.get("date_id")
    status = data.get("status")
    
    if not date_id or status not in ["yes", "no", "maybe"]:
        return jsonify({"error": "Invalid vote data"}), 400

    try:
        date_id = int(date_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid date ID"}), 400
    
    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute("SELECT * FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()
    if not poll:
        conn.close()
        return jsonify({"error": "Poll not found"}), 404

    if not user_can_access_poll(current_user, poll):
        conn.close()
        return jsonify({"error": "You are not invited to this poll"}), 403

    cursor.execute("SELECT id FROM dates WHERE id = %s AND poll_id = %s", (date_id, poll_id))
    if not cursor.fetchone():
        conn.close()
        return jsonify({"error": "Invalid date for this poll"}), 400
    
    cursor.execute(
        '''INSERT INTO votes (date_id, user_email, status)
           VALUES (%s, %s, %s)
           ON CONFLICT(date_id, user_email)
           DO UPDATE SET status = EXCLUDED.status''',
        (date_id, current_user["email"], status)
    )
    
    conn.commit()
    conn.close()
    
    return jsonify({"success": True, "date_removed": False})


@app.route("/dashboard")
def dashboard():
    current_user = get_current_user()

    if not current_user:
        return redirect(url_for("login"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute('''
        SELECT p.*, COUNT(d.id) AS date_count
        FROM polls p
        LEFT JOIN dates d ON d.poll_id = p.id
        GROUP BY p.id
        ORDER BY p.created_at DESC
    ''')
    all_polls = cursor.fetchall()
    conn.close()

    visible_polls = []
    for poll in all_polls:
        if not user_can_access_poll(current_user, poll):
            continue

        is_owner = normalize_email(poll["admin_email"]) == normalize_email(current_user["email"])
        invited_set = set(parse_invite_emails(poll.get("invite_emails")))
        poll["is_owner"] = is_owner
        poll["is_invited"] = normalize_email(current_user["email"]) in invited_set and not is_owner
        poll["can_manage"] = user_can_manage_poll(current_user, poll)
        poll["invite_count"] = len(invited_set)
        visible_polls.append(poll)

    owned_poll_count = get_owned_poll_count(current_user["email"])
    can_create_poll = not (current_user["tier"] == "free" and owned_poll_count >= FREE_POLL_LIMIT)

    return render_template(
        "dashboard.html",
        polls=visible_polls,
        user_email=current_user["email"],
        owned_poll_count=owned_poll_count,
        free_poll_limit=FREE_POLL_LIMIT,
        free_date_limit=FREE_DATE_LIMIT,
        can_create_poll=can_create_poll
    )


@app.route("/upgrade")
def upgrade():
    current_user = get_current_user()
    if not current_user:
        flash("Please log in first", "error")
        return redirect(url_for("login"))

    if current_user["tier"] == "paid":
        flash("You're already on the Pro tier.", "success")
        return redirect(url_for("dashboard"))

    return render_template("upgrade.html")


@app.route("/admin")
def admin_panel():
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    cursor.execute(
        '''
        SELECT
            u.email,
            u.display_name,
            u.role,
            u.tier,
            u.is_verified,
            u.created_at,
            COUNT(p.id) AS owned_poll_count
        FROM users u
        LEFT JOIN polls p ON LOWER(p.admin_email) = LOWER(u.email)
        GROUP BY u.email, u.display_name, u.role, u.tier, u.is_verified, u.created_at
        ORDER BY u.created_at ASC
        '''
    )
    users = cursor.fetchall()

    cursor.execute(
        '''
        SELECT p.*, COUNT(d.id) AS date_count
        FROM polls p
        LEFT JOIN dates d ON d.poll_id = p.id
        GROUP BY p.id
        ORDER BY p.created_at DESC
        '''
    )
    polls = cursor.fetchall()
    conn.close()

    for poll in polls:
        poll["invite_count"] = len(parse_invite_emails(poll.get("invite_emails")))

    return render_template("admin.html", users=users, polls=polls)


@app.route("/admin/users/<path:target_email>/update", methods=["POST"])
def admin_update_user(target_email):
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    target_email = normalize_email(target_email)
    role = (request.form.get("role") or "").strip().lower()
    tier = (request.form.get("tier") or "").strip().lower()

    if role not in VALID_ROLES or tier not in VALID_TIERS:
        flash("Invalid role or tier selection.", "error")
        return redirect(url_for("admin_panel"))

    if role == "admin":
        tier = "paid"

    if target_email in ADMIN_EMAILS:
        role = "admin"
        tier = "paid"

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)

    if role != "admin":
        cursor.execute("SELECT COUNT(*) AS admin_count FROM users WHERE role = 'admin'")
        admin_count = cursor.fetchone()["admin_count"]
        cursor.execute("SELECT role FROM users WHERE LOWER(email) = %s", (target_email,))
        current_target = cursor.fetchone()
        if current_target and current_target["role"] == "admin" and admin_count <= 1:
            conn.close()
            flash("At least one admin must remain in the system.", "error")
            return redirect(url_for("admin_panel"))

    cursor.execute(
        "UPDATE users SET role = %s, tier = %s WHERE LOWER(email) = %s",
        (role, tier, target_email)
    )
    if cursor.rowcount == 0:
        conn.close()
        flash("User not found.", "error")
        return redirect(url_for("admin_panel"))

    conn.commit()
    conn.close()

    flash("User updated successfully.", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/polls/<poll_id>/delete", methods=["POST"])
def admin_delete_poll(poll_id):
    current_user = get_current_user()
    if not is_admin_user(current_user):
        flash("Admin access required.", "error")
        return redirect(url_for("dashboard"))

    conn = get_db()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id FROM polls WHERE id = %s", (poll_id,))
    poll = cursor.fetchone()

    if not poll:
        conn.close()
        flash("Poll not found.", "error")
        return redirect(url_for("admin_panel"))

    delete_poll_records(cursor, poll_id)
    conn.commit()
    conn.close()

    flash("Poll deleted.", "success")
    return redirect(url_for("admin_panel"))




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
        new_email = normalize_email(request.form.get("email", user_email))
        profile_picture = request.form.get("profile_picture", "").strip()
        current_email = normalize_email(user_email)

        if not new_email:
            conn.close()
            flash("Email is required", "error")
            return redirect(url_for("profile"))

        if not is_allowed_email(new_email):
            conn.close()
            flash("Sorry, this email is not authorized to use this app", "error")
            return redirect(url_for("profile"))

        if new_email != current_email:
            cursor.execute(
                "SELECT 1 FROM users WHERE LOWER(email) = %s AND LOWER(email) != %s",
                (new_email, current_email)
            )
            if cursor.fetchone():
                conn.close()
                flash("That email is already in use.", "error")
                return redirect(url_for("profile"))

            token = generate_token()

            cursor.execute(
                '''
                UPDATE users
                SET email = %s, display_name = %s, profile_picture = %s, is_verified = FALSE
                WHERE LOWER(email) = %s
                ''',
                (
                    new_email,
                    display_name if display_name else None,
                    profile_picture if profile_picture else None,
                    current_email
                )
            )
            cursor.execute(
                "UPDATE polls SET admin_email = %s WHERE LOWER(admin_email) = %s",
                (new_email, current_email)
            )
            cursor.execute(
                "UPDATE votes SET user_email = %s WHERE LOWER(user_email) = %s",
                (new_email, current_email)
            )
            cursor.execute(
                "DELETE FROM verification_tokens WHERE email = %s",
                (new_email,)
            )
            cursor.execute(
                "INSERT INTO verification_tokens (email, token, expires_at) VALUES (%s, %s, NOW() + INTERVAL '24 hours')",
                (new_email, token)
            )
            conn.commit()
            conn.close()

            sent, error = send_verification_email(new_email, token, request.url_root)
            session.pop("user_email", None)
            session.pop("poll_name", None)
            session.pop("poll_creator_email", None)

            if sent:
                flash("Email updated. Please verify your new email before signing in again.", "success")
            else:
                flash("Email updated, but we could not send verification right now. Try signing in to resend.", "error")
                if error:
                    print(f"ERROR: Verification email send failed for {new_email}: {error}")
            return redirect(url_for("login"))

        cursor.execute(
            "UPDATE users SET display_name = %s, profile_picture = %s WHERE LOWER(email) = %s",
            (
                display_name if display_name else None,
                profile_picture if profile_picture else None,
                current_email
            )
        )
        conn.commit()
        flash("Profile updated successfully!", "success")
    
    cursor.execute("SELECT email, display_name, profile_picture FROM users WHERE email = %s", (normalize_email(user_email),))
    user = cursor.fetchone()
    conn.close()
    
    normalized_email = normalize_email(user_email)
    default_name = EMAIL_TO_NAME.get(normalized_email, normalized_email.split('@')[0])
    
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
    session.pop("poll_name", None)
    session.pop("poll_creator_email", None)
    session.pop("pending_verification_email", None)
    return redirect(url_for("login"))


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8080")),
        debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes")
    )
