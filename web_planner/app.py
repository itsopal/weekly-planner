"""
Weekly Planner - Web version (Flask)
------------------------------------
Same idea as the desktop Tkinter planner, but runs in the browser.

Run from this directory:
    pip install -r requirements.txt
    python app.py
Then open: http://127.0.0.1:5000

Password protection (single user):
    Set the PLANNER_PASSWORD environment variable to require a login.
    If it is NOT set, the site is open (handy for local use on your PC).
    On PythonAnywhere, set it at the top of your WSGI file:
        import os
        os.environ['PLANNER_PASSWORD'] = 'your-secret-password'
        os.environ['SECRET_KEY'] = 'long-random-string (see ../DEPLOY.md)'

Data is stored in planner_data.json next to this file (same format as
the desktop version, so you can copy that file here to migrate).
"""

import copy
import csv
import io
import json
import os
import re
import shutil
import tempfile
import threading
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR lets hosting platforms store data on a persistent disk.
# Locally it just uses the project folder.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DATA_FILE = os.path.join(DATA_DIR, "planner_data.json")
BACKUP_FILE = DATA_FILE + ".bak"
TEMP_FILE = DATA_FILE + ".tmp"
BACKUP_DIR = os.path.join(DATA_DIR, "planner_backups")
MAX_BACKUPS = 10
LEGACY_BACKUP_ID = "legacy-bak"
BACKUP_FILENAME_RE = re.compile(
    r"^planner_data-(?P<timestamp>\d{8}T\d{6}\.\d{6}Z)\.json$"
)

DEFAULT_COLUMNS = ["Work", "Study", "Health", "Personal"]
MAX_NOTE_LENGTH = 2000

# All access to the process-wide planner state and its backing file is serialized.
# The re-entrant lock allows state_transaction() to call save_data() safely.
STATE_LOCK = threading.RLock()

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
    """Return the ?next= redirect target, but only if it's a local path."""
    nxt = request.args.get("next") or default
    if not nxt.startswith("/") or nxt.startswith("//"):
        return default
    return nxt


@app.before_request
def require_login():
    """Protect every page/API except login, static files, and the app shell worker."""
    if not AUTH_ENABLED:
        return None
    if request.path in {"/login", "/service-worker.js"} or request.path.startswith(
        "/static/"
    ):
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
# Data persistence and in-process concurrency
# ------------------------------------------------------------------ #
def _read_data_file(path):
    with open(path, "r", encoding="utf-8") as data_file:
        raw = json.load(data_file)
    if not isinstance(raw, dict):
        raise ValueError("Planner data must be a JSON object")

    loaded_columns = raw.get("columns", list(DEFAULT_COLUMNS))
    loaded_entries = raw.get("entries", {})
    if not isinstance(loaded_columns, list) or not all(
        isinstance(name, str) for name in loaded_columns
    ):
        raise ValueError("Planner columns are invalid")
    if not isinstance(loaded_entries, dict):
        raise ValueError("Planner entries are invalid")
    return loaded_columns or list(DEFAULT_COLUMNS), loaded_entries


def _backup_paths():
    """Return valid rotated snapshots, newest first."""
    try:
        names = os.listdir(BACKUP_DIR)
    except OSError:
        return []

    paths = []
    for name in names:
        if BACKUP_FILENAME_RE.fullmatch(name):
            path = os.path.join(BACKUP_DIR, name)
            if os.path.isfile(path):
                paths.append(path)
    return sorted(paths, reverse=True)


def load_data():
    """Load the primary file, then the newest good snapshot if it is damaged."""
    global columns, entries
    with STATE_LOCK:
        candidates = [DATA_FILE, *_backup_paths(), BACKUP_FILE]
        for path in candidates:
            if not os.path.exists(path):
                continue
            try:
                loaded_columns, loaded_entries = _read_data_file(path)
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                continue
            columns = loaded_columns
            entries = loaded_entries
            return path

        columns = list(DEFAULT_COLUMNS)
        entries = {}
        return None


def _create_backup_snapshot():
    """Snapshot the current primary file before it is replaced."""
    if not os.path.isfile(DATA_FILE):
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    snapshot_time = datetime.now(timezone.utc)
    while True:
        timestamp = snapshot_time.strftime("%Y%m%dT%H%M%S.%fZ")
        backup_path = os.path.join(BACKUP_DIR, f"planner_data-{timestamp}.json")
        if not os.path.exists(backup_path):
            break
        snapshot_time += timedelta(microseconds=1)
    shutil.copy2(DATA_FILE, backup_path)

    # Keep the legacy .bak file current for compatibility with earlier releases.
    fd, legacy_temp = tempfile.mkstemp(
        prefix=os.path.basename(BACKUP_FILE) + ".",
        suffix=".tmp",
        dir=DATA_DIR,
    )
    os.close(fd)
    try:
        shutil.copy2(DATA_FILE, legacy_temp)
        os.replace(legacy_temp, BACKUP_FILE)
    finally:
        if os.path.exists(legacy_temp):
            os.unlink(legacy_temp)


def _rotate_backups():
    """Keep only the configured number of timestamped snapshots."""
    for path in _backup_paths()[MAX_BACKUPS:]:
        try:
            os.remove(path)
        except OSError as error:
            app.logger.warning(
                "Could not remove old planner backup %s: %s", path, error
            )


def save_data():
    """Write a consistent snapshot atomically and retain a rotating history."""
    with STATE_LOCK:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {"columns": copy.deepcopy(columns), "entries": copy.deepcopy(entries)}
        fd, temp_path = tempfile.mkstemp(
            prefix=os.path.basename(TEMP_FILE) + ".",
            suffix=".tmp",
            dir=DATA_DIR,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as data_file:
                json.dump(payload, data_file, indent=2, ensure_ascii=False)
                data_file.flush()
                os.fsync(data_file.fileno())

            _create_backup_snapshot()
            os.replace(temp_path, DATA_FILE)
            _rotate_backups()
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)


def restore_state(previous_columns, previous_entries):
    """Restore mutable globals in place after a failed transaction."""
    columns[:] = previous_columns
    entries.clear()
    entries.update(previous_entries)


@contextmanager
def state_transaction():
    """Serialize one in-memory change and roll it back if persistence fails."""
    with STATE_LOCK:
        previous_columns = copy.deepcopy(columns)
        previous_entries = copy.deepcopy(entries)
        try:
            yield
            save_data()
        except Exception:
            restore_state(previous_columns, previous_entries)
            raise


def cleanup_empty_days():
    """Drop days where every cell is unchecked with a blank note."""
    with STATE_LOCK:
        for date_key in list(entries.keys()):
            day = entries[date_key]
            if all(
                not value.get("done") and not str(value.get("note", "")).strip()
                for value in day.values()
            ):
                del entries[date_key]


def valid_date_key(value: str) -> bool:
    try:
        parsed = date.fromisoformat(value)
        return parsed.isoformat() == value
    except (ValueError, TypeError):
        return False


def compute_streak(col_name: str) -> int:
    with STATE_LOCK:
        current_day = date.today()
        streak = 0
        while True:
            cell = entries.get(current_day.isoformat(), {}).get(col_name)
            if cell and cell.get("done"):
                streak += 1
                current_day -= timedelta(days=1)
            else:
                break
        return streak


def list_backup_records():
    """Return API-safe metadata for rotated backups, newest first."""
    records = []
    for path in _backup_paths():
        name = os.path.basename(path)
        match = BACKUP_FILENAME_RE.fullmatch(name)
        if not match:
            continue
        created_at = datetime.strptime(
            match.group("timestamp"), "%Y%m%dT%H%M%S.%fZ"
        ).replace(tzinfo=timezone.utc)
        try:
            size_bytes = os.path.getsize(path)
        except OSError:
            continue
        records.append(
            {
                "id": name,
                "created_at": created_at.isoformat(),
                "size_bytes": size_bytes,
            }
        )

    # Older installations only have the original .bak file. Expose it until a
    # timestamped snapshot exists; after that it duplicates the latest snapshot.
    if not records and os.path.isfile(BACKUP_FILE):
        try:
            _read_data_file(BACKUP_FILE)
            records.append(
                {
                    "id": LEGACY_BACKUP_ID,
                    "created_at": datetime.fromtimestamp(
                        os.path.getmtime(BACKUP_FILE), timezone.utc
                    ).isoformat(),
                    "size_bytes": os.path.getsize(BACKUP_FILE),
                }
            )
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
    return records


load_data()


# ------------------------------------------------------------------ #
# Auth pages
# ------------------------------------------------------------------ #
@app.route("/login", methods=["GET", "POST"])
def login():
    if not AUTH_ENABLED:
        return redirect(url_for("index"))
    if session.get("logged_in"):
        return redirect(safe_next())
    error = None
    if request.method == "POST":
        if check_password_hash(PASSWORD_HASH, request.form.get("password", "")):
            session["logged_in"] = True
            session.permanent = True
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


@app.route("/service-worker.js")
def service_worker():
    response = app.send_static_file("service-worker.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


# ------------------------------------------------------------------ #
# API (all protected by require_login when a password is set)
# ------------------------------------------------------------------ #
@app.route("/api/state")
def api_state():
    with STATE_LOCK:
        return jsonify(
            {"columns": copy.deepcopy(columns), "entries": copy.deepcopy(entries)}
        )


@app.route("/api/cell", methods=["POST"])
def api_cell():
    """Save one cell: {date, column, done, note}."""
    data = request.get_json(force=True, silent=True) or {}
    date_key = data.get("date", "")
    col = data.get("column", "")
    if not valid_date_key(date_key):
        return jsonify({"ok": False, "error": "Bad date"}), 400

    with STATE_LOCK:
        if col not in columns:
            return jsonify({"ok": False, "error": "Unknown column"}), 400
        note = str(data.get("note", ""))[:MAX_NOTE_LENGTH]
        try:
            with state_transaction():
                entries.setdefault(date_key, {})[col] = {
                    "done": bool(data.get("done")),
                    "note": note,
                }
                cleanup_empty_days()
        except OSError as error:
            return jsonify({"ok": False, "error": str(error)}), 500
    return jsonify({"ok": True})


@app.route("/api/clear-day", methods=["POST"])
def api_clear_day():
    data = request.get_json(force=True, silent=True) or {}
    date_key = data.get("date", "")
    if not valid_date_key(date_key):
        return jsonify({"ok": False, "error": "Bad date"}), 400

    try:
        with state_transaction():
            entries.pop(date_key, None)
    except OSError as error:
        return jsonify({"ok": False, "error": str(error)}), 500
    return jsonify({"ok": True})


@app.route("/api/columns/add", methods=["POST"])
def api_add_column():
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "Empty name"}), 400

    with STATE_LOCK:
        if name in columns:
            return jsonify({"ok": False, "error": "Already exists"}), 400
        try:
            with state_transaction():
                columns.append(name)
        except OSError as error:
            return jsonify({"ok": False, "error": str(error)}), 500
        return jsonify({"ok": True, "columns": copy.deepcopy(columns)})


@app.route("/api/columns/rename", methods=["POST"])
def api_rename_column():
    data = request.get_json(force=True, silent=True) or {}
    old = str(data.get("old", ""))
    new = str(data.get("new", "")).strip()

    with STATE_LOCK:
        if old not in columns or not new:
            return jsonify({"ok": False, "error": "Bad name"}), 400
        if new in columns:
            return jsonify({"ok": False, "error": "Already exists"}), 400
        try:
            with state_transaction():
                columns[columns.index(old)] = new
                for day_data in entries.values():
                    if old in day_data:
                        day_data[new] = day_data.pop(old)
        except OSError as error:
            return jsonify({"ok": False, "error": str(error)}), 500
        return jsonify({"ok": True, "columns": copy.deepcopy(columns)})


@app.route("/api/columns/delete", methods=["POST"])
def api_delete_column():
    data = request.get_json(force=True, silent=True) or {}
    name = str(data.get("name", ""))

    with STATE_LOCK:
        if name not in columns:
            return jsonify({"ok": False, "error": "Unknown column"}), 400
        if len(columns) == 1:
            return jsonify({"ok": False, "error": "Keep at least one column"}), 400
        try:
            with state_transaction():
                columns.remove(name)
                for day_data in entries.values():
                    day_data.pop(name, None)
                cleanup_empty_days()
        except OSError as error:
            return jsonify({"ok": False, "error": str(error)}), 500
        return jsonify({"ok": True, "columns": copy.deepcopy(columns)})


@app.route("/api/copy-prev-week", methods=["POST"])
def api_copy_prev_week():
    """Copy last week's notes into the given week (unchecked)."""
    data = request.get_json(force=True, silent=True) or {}
    week_start = data.get("week_start", "")
    if not valid_date_key(week_start):
        return jsonify({"ok": False, "error": "Bad week_start"}), 400

    monday = date.fromisoformat(week_start)
    prev_monday = monday - timedelta(days=7)
    copied = 0
    skipped = 0
    try:
        with state_transaction():
            for index in range(7):
                previous_key = (prev_monday + timedelta(days=index)).isoformat()
                current_key = (monday + timedelta(days=index)).isoformat()
                previous_data = entries.get(previous_key, {})
                if not previous_data:
                    continue
                if current_key in entries and entries[current_key]:
                    skipped += 1
                    continue
                new_day = {}
                for col in columns:
                    note = previous_data.get(col, {}).get("note", "")
                    if note:
                        new_day[col] = {"done": False, "note": note}
                        copied += 1
                if new_day:
                    entries[current_key] = new_day
    except OSError as error:
        return jsonify({"ok": False, "error": str(error)}), 500
    return jsonify({"ok": True, "copied": copied, "skipped": skipped})


@app.route("/api/stats")
def api_stats():
    with STATE_LOCK:
        total_done = 0
        total_cells = 0
        best_day, best_count = None, 0
        for date_key, day_data in entries.items():
            day_done = sum(1 for col in columns if day_data.get(col, {}).get("done"))
            total_done += day_done
            total_cells += len(columns)
            if day_done > best_count:
                best_count, best_day = day_done, date_key
        best_col, best_streak = None, 0
        for col in columns:
            streak = compute_streak(col)
            if streak > best_streak:
                best_streak, best_col = streak, col
        pct = (total_done * 100 // total_cells) if total_cells else 0
        return jsonify(
            {
                "days_tracked": len(entries),
                "tasks_done": total_done,
                "pct": pct,
                "best_day": best_day,
                "best_count": best_count,
                "best_streak_col": best_col,
                "best_streak": best_streak,
            }
        )


@app.route("/api/export")
def api_export():
    with STATE_LOCK:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        header = ["Date"]
        for col in columns:
            header.extend((f"{col} Done", f"{col} Note"))
        writer.writerow(header)
        for date_key in sorted(entries):
            day_data = entries[date_key]
            row = [date_key]
            for col in columns:
                cell = day_data.get(col, {})
                row.extend(("Yes" if cell.get("done") else "No", cell.get("note", "")))
            writer.writerow(row)
        return Response(
            "\ufeff" + buffer.getvalue(),
            mimetype="text/csv",
            headers={
                "Content-Disposition": "attachment; filename=weekly_planner_export.csv"
            },
        )


@app.route("/api/backups")
def api_backups():
    with STATE_LOCK:
        return jsonify({"backups": list_backup_records()})


@app.route("/api/backups/restore", methods=["POST"])
def api_restore_backup():
    data = request.get_json(force=True, silent=True) or {}
    backup_id = data.get("backup_id")
    if not isinstance(backup_id, str) or (
        backup_id != LEGACY_BACKUP_ID and not BACKUP_FILENAME_RE.fullmatch(backup_id)
    ):
        return jsonify({"ok": False, "error": "Invalid backup ID"}), 400

    backup_path = (
        BACKUP_FILE
        if backup_id == LEGACY_BACKUP_ID
        else os.path.join(BACKUP_DIR, backup_id)
    )
    with STATE_LOCK:
        if not os.path.isfile(backup_path):
            return jsonify({"ok": False, "error": "Backup not found"}), 404
        try:
            backup_columns, backup_entries = _read_data_file(backup_path)
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return jsonify(
                {"ok": False, "error": "Backup is invalid or unreadable"}
            ), 400
        try:
            with state_transaction():
                columns[:] = backup_columns
                entries.clear()
                entries.update(backup_entries)
        except OSError as error:
            return jsonify({"ok": False, "error": str(error)}), 500
        return jsonify(
            {
                "ok": True,
                "restored": backup_id,
                "columns": copy.deepcopy(columns),
            }
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("=" * 55)
    print("  📅 Weekly Planner is starting...")
    print(f"  👉 Open in your browser:  http://127.0.0.1:{port}")
    print("  (Keep this window open. Press Ctrl+C to stop.)")
    print("=" * 55, flush=True)
    try:
        app.run(host="0.0.0.0", port=port, threaded=True)
    except OSError as error:
        if "Address already in use" in str(error) or "10048" in str(error):
            fallback_port = 8000
            print(f"\n⚠️  Port {port} is busy, trying {fallback_port} instead...")
            print(
                f"  👉 Open in your browser:  http://127.0.0.1:{fallback_port}\n",
                flush=True,
            )
            app.run(host="0.0.0.0", port=fallback_port, threaded=True)
        else:
            raise
