"""
Weekly Planner - Web version (Flask)
------------------------------------
Same idea as the desktop Tkinter planner, but runs in the browser.

Run:
    pip install flask
    python app.py
Then open: http://127.0.0.1:5000

Password protection (single user):
    Set the PLANNER_PASSWORD environment variable to require a login.
    If it is NOT set, the site is open (handy for local use on your PC).
    On PythonAnywhere, set it at the top of your WSGI file:
        import os
        os.environ['PLANNER_PASSWORD'] = 'your-secret-password'
        os.environ['SECRET_KEY'] = 'long-random-string (see DEPLOY.md)'

Data is stored in planner_data.json next to this file (same format as
the desktop version, so you can copy that file here to migrate).
"""

import csv
import io
import json
import os
import shutil
from datetime import date, timedelta, datetime

from flask import Flask, render_template, request, jsonify, Response, \
    session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR lets hosting platforms store data on a persistent disk.
# Locally it just uses the project folder.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "planner_data.json")
BACKUP_FILE = DATA_FILE + ".bak"
TEMP_FILE = DATA_FILE + ".tmp"

DEFAULT_COLUMNS = ["Work", "Study", "Health", "Personal"]
MAX_NOTE_LENGTH = 2000

app = Flask(__name__)

# ------------------------------------------------------------------ #
# Simple password protection (single user, session cookie)
# ------------------------------------------------------------------ #
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-insecure-key")
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

_raw_password = os.environ.get("PLANNER_PASSWORD", "")
PASSWORD_HASH = generate_password_hash(_raw_password) if _raw_password else None
del _raw_password  # don't keep the plain password in memory
if PASSWORD_HASH is None:
    print("⚠️  WARNING: PLANNER_PASSWORD is not set — login is DISABLED (open site).")

AUTH_ENABLED = PASSWORD_HASH is not None


def safe_next(default="/"):
    """Return the ?next= redirect target, but only if it's a local path
    (prevents open-redirect attacks to external sites)."""
    nxt = request.args.get("next") or default
    if not nxt.startswith("/") or nxt.startswith("//"):
        return default
    return nxt


@app.before_request
def require_login():
    """Protect every page/API except /login and static files."""
    if not AUTH_ENABLED:
        return None  # open mode: no password configured
    if request.path == "/login" or request.path.startswith("/static/"):
        return None
    if not session.get("logged_in"):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Login required"}), 401
        return redirect(url_for("login", next=request.path))
    return None


# In-memory state, loaded once at startup, saved on every change.
columns: list = []
entries: dict = {}


# ------------------------------------------------------------------ #
# Data persistence (same safe pattern as the desktop app)
# ------------------------------------------------------------------ #
def load_data():
    global columns, entries
    for path in (DATA_FILE, BACKUP_FILE):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                columns = raw.get("columns", list(DEFAULT_COLUMNS))
                entries = raw.get("entries", {})
                if not columns:
                    columns = list(DEFAULT_COLUMNS)
                return
            except (json.JSONDecodeError, OSError):
                continue
    columns = list(DEFAULT_COLUMNS)
    entries = {}


def save_data():
    """Atomic save: temp file -> backup old -> replace. Crash-safe."""
    payload = {"columns": columns, "entries": entries}
    with open(TEMP_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    if os.path.exists(DATA_FILE):
        shutil.copy2(DATA_FILE, BACKUP_FILE)
    os.replace(TEMP_FILE, DATA_FILE)


def cleanup_empty_days():
    """Drop days where every cell is unchecked with a blank note."""
    for date_key in list(entries.keys()):
        day = entries[date_key]
        if all(not v.get("done") and not str(v.get("note", "")).strip()
               for v in day.values()):
            del entries[date_key]


def valid_date_key(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except (ValueError, TypeError):
        return False


def compute_streak(col_name: str) -> int:
    d = date.today()
    streak = 0
    while True:
        cell = entries.get(d.isoformat(), {}).get(col_name)
        if cell and cell.get("done"):
            streak += 1
            d -= timedelta(days=1)
        else:
            break
    return streak


load_data()


# ------------------------------------------------------------------ #
# Auth pages
# ------------------------------------------------------------------ #
@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))  # no password configured
    if session.get("logged_in"):
        return redirect(safe_next())
    error = None
    if request.method == "POST":
        if check_password_hash(PASSWORD_HASH, request.form.get("password", "")):
            session["logged_in"] = True
            session.permanent = True  # stay logged in for 30 days
            return redirect(safe_next())
        error = "Wrong password. Try again."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ------------------------------------------------------------------ #
# Pages
# ------------------------------------------------------------------ #
@app.route("/")
def index():
    return render_template("index.html", auth_enabled=AUTH_ENABLED)


# ------------------------------------------------------------------ #
# API (all protected by require_login when a password is set)
# ------------------------------------------------------------------ #
@app.route("/api/state")
def api_state():
    return jsonify({"columns": columns, "entries": entries})


@app.route("/api/cell", methods=["POST"])
def api_cell():
    """Save one cell: {date, column, done, note}"""
    data = request.get_json(force=True, silent=True) or {}
    date_key = data.get("date", "")
    col = data.get("column", "")
    if not valid_date_key(date_key):
        return jsonify({"ok": False, "error": "Bad date"}), 400
    if col not in columns:
        return jsonify({"ok": False, "error": "Unknown column"}), 400
    note = str(data.get("note", ""))[:MAX_NOTE_LENGTH]
    entries.setdefault(date_key, {})[col] = {
        "done": bool(data.get("done")),
        "note": note,
    }
    cleanup_empty_days()
    try:
        save_data()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/clear-day", methods=["POST"])
def api_clear_day():
    data = request.get_json(force=True, silent=True) or {}
    date_key = data.get("date", "")
    if not valid_date_key(date_key):
        return jsonify({"ok": False, "error": "Bad date"}), 400
    entries.pop(date_key, None)
    try:
        save_data()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/columns/add", methods=["POST"])
def api_add_column():
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "Empty name"}), 400
    if name in columns:
        return jsonify({"ok": False, "error": "Already exists"}), 400
    columns.append(name)
    try:
        save_data()
    except OSError as e:
        columns.pop()
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "columns": columns})


@app.route("/api/columns/rename", methods=["POST"])
def api_rename_column():
    data = request.get_json(force=True, silent=True) or {}
    old = str(data.get("old", ""))
    new = str(data.get("new", "")).strip()
    if old not in columns or not new:
        return jsonify({"ok": False, "error": "Bad name"}), 400
    if new in columns:
        return jsonify({"ok": False, "error": "Already exists"}), 400
    columns[columns.index(old)] = new
    for day_data in entries.values():
        if old in day_data:
            day_data[new] = day_data.pop(old)
    try:
        save_data()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "columns": columns})


@app.route("/api/columns/delete", methods=["POST"])
def api_delete_column():
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name", ""))
    if name not in columns:
        return jsonify({"ok": False, "error": "Unknown column"}), 400
    if len(columns) == 1:
        return jsonify({"ok": False, "error": "Keep at least one column"}), 400
    columns.remove(name)
    for day_data in entries.values():
        day_data.pop(name, None)
    cleanup_empty_days()
    try:
        save_data()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "columns": columns})


@app.route("/api/copy-prev-week", methods=["POST"])
def api_copy_prev_week():
    """Copy last week's notes into the given week (unchecked)."""
    data = request.get_json(force=True, silent=True) or {}
    week_start = data.get("week_start", "")
    if not valid_date_key(week_start):
        return jsonify({"ok": False, "error": "Bad week_start"}), 400
    monday = date.fromisoformat(week_start)
    prev_monday = monday - timedelta(days=7)
    copied, skipped = 0, 0
    for i in range(7):
        prev_key = (prev_monday + timedelta(days=i)).isoformat()
        curr_key = (monday + timedelta(days=i)).isoformat()
        prev_data = entries.get(prev_key, {})
        if not prev_data:
            continue
        if curr_key in entries and entries[curr_key]:
            skipped += 1
            continue
        new_day = {}
        for col in columns:
            note = prev_data.get(col, {}).get("note", "")
            if note:
                new_day[col] = {"done": False, "note": note}
                copied += 1
        if new_day:
            entries[curr_key] = new_day
    try:
        save_data()
    except OSError as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "copied": copied, "skipped": skipped})


@app.route("/api/stats")
def api_stats():
    total_done = 0
    total_cells = 0
    best_day, best_count = None, 0
    for date_key, day_data in entries.items():
        day_done = sum(1 for c in columns if day_data.get(c, {}).get("done"))
        total_done += day_done
        total_cells += len(columns)
        if day_done > best_count:
            best_count, best_day = day_done, date_key
    best_col, best_streak = None, 0
    for c in columns:
        s = compute_streak(c)
        if s > best_streak:
            best_streak, best_col = s, c
    pct = (total_done * 100 // total_cells) if total_cells else 0
    return jsonify({
        "days_tracked": len(entries),
        "tasks_done": total_done,
        "pct": pct,
        "best_day": best_day,
        "best_count": best_count,
        "best_streak_col": best_col,
        "best_streak": best_streak,
    })


@app.route("/api/export")
def api_export():
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["Date"]
    for col in columns:
        header.append(f"{col} Done")
        header.append(f"{col} Note")
    writer.writerow(header)
    for date_key in sorted(entries.keys()):
        day_data = entries[date_key]
        row = [date_key]
        for col in columns:
            cell = day_data.get(col, {})
            row.append("Yes" if cell.get("done") else "No")
            row.append(cell.get("note", ""))
        writer.writerow(row)
    return Response(
        "\ufeff" + buf.getvalue(),  # BOM so Excel shows UTF-8 correctly
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=weekly_planner_export.csv"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("  📅 Weekly Planner is starting...")
    print(f"  👉 Open in your browser:  http://127.0.0.1:{port}")
    print("  (Keep this window open. Press Ctrl+C to stop.)")
    print("=" * 55, flush=True)
    try:
        app.run(host="0.0.0.0", port=port)
    except OSError as e:
        # Port busy? (WinError 10048 on Windows) Try a fallback port.
        if "Address already in use" in str(e) or "10048" in str(e):
            alt = 8000
            print(f"\n⚠️  Port {port} is busy, trying {alt} instead...")
            print(f"  👉 Open in your browser:  http://127.0.0.1:{alt}\n", flush=True)
            app.run(host="0.0.0.0", port=alt)
        else:
            raise
