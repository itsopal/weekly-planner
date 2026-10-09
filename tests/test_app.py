import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from threading import Barrier

import pytest
from werkzeug.security import generate_password_hash

from web_planner import app as planner_module

TEST_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def planner(tmp_path, monkeypatch):
    data_file = tmp_path / "planner_data.json"
    monkeypatch.setattr(planner_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(planner_module, "DATA_FILE", str(data_file))
    monkeypatch.setattr(planner_module, "BACKUP_FILE", f"{data_file}.bak")
    monkeypatch.setattr(planner_module, "TEMP_FILE", f"{data_file}.tmp")
    monkeypatch.setattr(planner_module, "BACKUP_DIR", str(tmp_path / "planner_backups"))
    monkeypatch.setattr(planner_module, "columns", list(planner_module.DEFAULT_COLUMNS))
    monkeypatch.setattr(planner_module, "entries", {})
    monkeypatch.setattr(planner_module, "AUTH_ENABLED", False)
    monkeypatch.setattr(planner_module, "PASSWORD_HASH", None)
    planner_module.app.config.update(TESTING=True, SECRET_KEY="test-only-secret")
    return planner_module


@pytest.fixture
def client(planner):
    return planner.app.test_client()


@pytest.fixture
def protected_client(client, planner):
    planner.AUTH_ENABLED = True
    planner.PASSWORD_HASH = generate_password_hash(TEST_PASSWORD)
    return client


def test_index_renders_the_planner_in_open_mode(client):
    response = client.get("/")

    assert response.status_code == 200
    assert b"Weekly Planner" in response.data
    assert b'id="grid"' in response.data
    assert b'id="backupsBtn"' in response.data
    assert b'id="backupsModal"' in response.data
    assert b"Logout" not in response.data


def test_login_route_redirects_to_home_when_auth_is_disabled(client):
    response = client.get("/login")

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_state_returns_default_columns_and_empty_entries(client, planner):
    response = client.get("/api/state")

    assert response.status_code == 200
    assert response.json == {"columns": planner.DEFAULT_COLUMNS, "entries": {}}


def test_auth_protects_pages_and_api_but_allows_static_assets(protected_client):
    home = protected_client.get("/")
    state = protected_client.get("/api/state")
    stats = protected_client.get("/api/stats")
    export = protected_client.get("/api/export")
    backups = protected_client.get("/api/backups")
    css = protected_client.get("/static/style.css")
    service_worker = protected_client.get("/service-worker.js")

    assert home.status_code == 302
    assert home.headers["Location"].startswith("/login?next=/")
    assert (
        state.status_code
        == stats.status_code
        == export.status_code
        == backups.status_code
        == 401
    )
    assert state.json == {"ok": False, "error": "Login required"}
    assert css.status_code == 200
    assert css.mimetype == "text/css"
    assert service_worker.status_code == 200
    assert service_worker.headers["Service-Worker-Allowed"] == "/"


def test_login_rejects_bad_password_then_accepts_a_local_next(protected_client):
    login_page = protected_client.get("/login")
    wrong_password = protected_client.post(
        "/login", data={"password": "wrong-password"}
    )
    valid_password = protected_client.post(
        "/login?next=/api/state", data={"password": TEST_PASSWORD}
    )

    assert login_page.status_code == 200
    assert b"Enter your password" in login_page.data
    assert wrong_password.status_code == 200
    assert b"Wrong password" in wrong_password.data
    assert valid_password.status_code == 302
    assert valid_password.headers["Location"] == "/api/state"
    assert protected_client.get("/api/state").status_code == 200


def test_login_rejects_external_redirect_target(protected_client):
    response = protected_client.post(
        "/login?next=https://example.invalid/",
        data={"password": TEST_PASSWORD},
    )

    assert response.status_code == 302
    assert response.headers["Location"] == "/"


def test_login_redirects_logged_in_user_to_safe_next(protected_client):
    protected_client.post("/login", data={"password": TEST_PASSWORD})

    response = protected_client.get("/login?next=/api/state")

    assert response.status_code == 302
    assert response.headers["Location"] == "/api/state"


def test_logout_clears_the_session(protected_client):
    protected_client.post("/login", data={"password": TEST_PASSWORD})

    logout = protected_client.get("/logout")
    state = protected_client.get("/api/state")

    assert logout.status_code == 302
    assert logout.headers["Location"] == "/login"
    assert state.status_code == 401


def test_cell_is_saved_and_previous_value_is_backed_up(client, planner):
    date_key = "2026-10-12"
    first = client.post(
        "/api/cell",
        json={"date": date_key, "column": "Work", "done": True, "note": "Plan"},
    )
    second = client.post(
        "/api/cell",
        json={"date": date_key, "column": "Work", "done": False, "note": "Review"},
    )

    assert first.status_code == second.status_code == 200
    assert second.json == {"ok": True}
    assert json.loads(Path(planner.DATA_FILE).read_text(encoding="utf-8")) == {
        "columns": planner.DEFAULT_COLUMNS,
        "entries": {
            date_key: {"Work": {"done": False, "note": "Review"}},
        },
    }
    backup = json.loads(Path(planner.BACKUP_FILE).read_text(encoding="utf-8"))
    assert backup["entries"][date_key]["Work"]["note"] == "Plan"


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"date": "not-a-date", "column": "Work", "done": True}, "Bad date"),
        (
            {"date": "2026-10-12", "column": "Unknown", "done": True},
            "Unknown column",
        ),
    ],
)
def test_cell_rejects_invalid_date_or_column(client, payload, expected_error):
    response = client.post("/api/cell", json=payload)

    assert response.status_code == 400
    assert response.json == {"ok": False, "error": expected_error}


def test_cell_truncates_notes_at_the_documented_limit(client, planner):
    note = "n" * (planner.MAX_NOTE_LENGTH + 25)

    response = client.post(
        "/api/cell",
        json={
            "date": "2026-10-12",
            "column": "Work",
            "done": True,
            "note": note,
        },
    )

    assert response.status_code == 200
    assert len(planner.entries["2026-10-12"]["Work"]["note"]) == planner.MAX_NOTE_LENGTH


def test_cell_drops_days_with_no_completion_or_note(client, planner):
    response = client.post(
        "/api/cell",
        json={"date": "2026-10-12", "column": "Work", "done": False, "note": "  "},
    )

    assert response.status_code == 200
    assert planner.entries == {}


def test_cell_save_failure_rolls_back_the_in_memory_edit(client, planner, monkeypatch):
    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(planner, "save_data", fail_save)
    response = client.post(
        "/api/cell",
        json={"date": "2026-10-12", "column": "Work", "done": True, "note": "Draft"},
    )

    assert response.status_code == 500
    assert response.json == {"ok": False, "error": "disk full"}
    assert planner.entries == {}
    assert not Path(planner.DATA_FILE).exists()


def test_clear_day_removes_saved_entries(client, planner):
    planner.entries["2026-10-12"] = {"Work": {"done": True, "note": "Finish report"}}

    response = client.post("/api/clear-day", json={"date": "2026-10-12"})

    assert response.status_code == 200
    assert response.json == {"ok": True}
    assert planner.entries == {}
    assert (
        json.loads(Path(planner.DATA_FILE).read_text(encoding="utf-8"))["entries"] == {}
    )


@pytest.mark.parametrize("date_value", ["", "not-a-date", None])
def test_clear_day_rejects_invalid_dates(client, date_value):
    response = client.post("/api/clear-day", json={"date": date_value})

    assert response.status_code == 400
    assert response.json["error"] == "Bad date"


def test_columns_can_be_added(client, planner):
    response = client.post("/api/columns/add", json={"name": "Projects"})

    assert response.status_code == 200
    assert response.json == {
        "ok": True,
        "columns": [*planner.DEFAULT_COLUMNS, "Projects"],
    }


@pytest.mark.parametrize(
    ("name", "expected_error"),
    [("  ", "Empty name"), ("Work", "Already exists")],
)
def test_add_column_rejects_empty_or_duplicate_names(client, name, expected_error):
    response = client.post("/api/columns/add", json={"name": name})

    assert response.status_code == 400
    assert response.json["error"] == expected_error


def test_renaming_column_preserves_its_saved_cells(client, planner):
    planner.entries["2026-10-12"] = {"Work": {"done": True, "note": "Ship release"}}

    response = client.post(
        "/api/columns/rename", json={"old": "Work", "new": "Projects"}
    )

    assert response.status_code == 200
    assert response.json["columns"][0] == "Projects"
    assert "Work" not in planner.entries["2026-10-12"]
    assert planner.entries["2026-10-12"]["Projects"] == {
        "done": True,
        "note": "Ship release",
    }


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"old": "Missing", "new": "Projects"}, "Bad name"),
        ({"old": "Work", "new": "  "}, "Bad name"),
        ({"old": "Work", "new": "Study"}, "Already exists"),
    ],
)
def test_rename_column_rejects_bad_or_duplicate_names(client, payload, expected_error):
    response = client.post("/api/columns/rename", json=payload)

    assert response.status_code == 400
    assert response.json["error"] == expected_error


def test_rename_save_failure_rolls_back_columns_and_cell_data(
    client, planner, monkeypatch
):
    original_entries = {"2026-10-12": {"Work": {"done": True, "note": "Keep this"}}}
    planner.entries["2026-10-12"] = {"Work": {"done": True, "note": "Keep this"}}

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(planner, "save_data", fail_save)
    response = client.post(
        "/api/columns/rename", json={"old": "Work", "new": "Projects"}
    )

    assert response.status_code == 500
    assert planner.columns == planner.DEFAULT_COLUMNS
    assert planner.entries == original_entries


def test_deleting_column_removes_its_cells_and_cleans_empty_days(client, planner):
    planner.entries.update(
        {
            "2026-10-12": {"Work": {"done": True, "note": "Remove this"}},
            "2026-10-13": {
                "Work": {"done": True, "note": "Remove this too"},
                "Study": {"done": False, "note": "Keep this"},
            },
        }
    )

    response = client.post("/api/columns/delete", json={"name": "Work"})

    assert response.status_code == 200
    assert response.json["columns"] == ["Study", "Health", "Personal"]
    assert "2026-10-12" not in planner.entries
    assert planner.entries["2026-10-13"] == {
        "Study": {"done": False, "note": "Keep this"}
    }


def test_delete_column_rejects_unknown_name_and_last_column(client, planner):
    unknown = client.post("/api/columns/delete", json={"name": "Unknown"})
    planner.columns[:] = ["Only"]
    last_column = client.post("/api/columns/delete", json={"name": "Only"})

    assert unknown.status_code == 400
    assert unknown.json["error"] == "Unknown column"
    assert last_column.status_code == 400
    assert last_column.json["error"] == "Keep at least one column"


def test_copy_previous_week_copies_notes_skips_existing_days_and_unchecks_tasks(
    client, planner
):
    planner.entries.update(
        {
            "2026-10-05": {
                "Work": {"done": True, "note": "Prepare report"},
                "Study": {"done": True, "note": ""},
            },
            "2026-10-06": {"Health": {"done": True, "note": "Take a walk"}},
            "2026-10-12": {"Personal": {"done": False, "note": "Keep this"}},
        }
    )

    response = client.post("/api/copy-prev-week", json={"week_start": "2026-10-12"})

    assert response.status_code == 200
    assert response.json == {"ok": True, "copied": 1, "skipped": 1}
    assert planner.entries["2026-10-12"] == {
        "Personal": {"done": False, "note": "Keep this"}
    }
    assert planner.entries["2026-10-13"]["Health"] == {
        "done": False,
        "note": "Take a walk",
    }


@pytest.mark.parametrize("week_start", ["", "bad-date", None])
def test_copy_previous_week_rejects_invalid_start_date(client, week_start):
    response = client.post("/api/copy-prev-week", json={"week_start": week_start})

    assert response.status_code == 400
    assert response.json["error"] == "Bad week_start"


def test_stats_returns_empty_summary_for_new_planner(client):
    response = client.get("/api/stats")

    assert response.status_code == 200
    assert response.json == {
        "days_tracked": 0,
        "tasks_done": 0,
        "pct": 0,
        "best_day": None,
        "best_count": 0,
        "best_streak_col": None,
        "best_streak": 0,
    }


def test_stats_calculates_completion_and_streak(client, planner, monkeypatch):
    class FrozenDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 12)

    monkeypatch.setattr(planner, "date", FrozenDate)
    planner.entries.update(
        {
            "2026-10-12": {
                "Work": {"done": True, "note": ""},
                "Study": {"done": True, "note": ""},
            },
            "2026-10-11": {
                "Work": {"done": True, "note": ""},
                "Study": {"done": False, "note": ""},
            },
        }
    )

    response = client.get("/api/stats")

    assert response.status_code == 200
    assert response.json == {
        "days_tracked": 2,
        "tasks_done": 3,
        "pct": 37,
        "best_day": "2026-10-12",
        "best_count": 2,
        "best_streak_col": "Work",
        "best_streak": 2,
    }


def test_export_returns_bom_sorted_csv_with_quoted_notes(client, planner):
    planner.entries.update(
        {
            "2026-10-13": {"Work": {"done": False, "note": "Later"}},
            "2026-10-12": {"Work": {"done": True, "note": "Write, review\nnotes"}},
        }
    )

    response = client.get("/api/export")
    content = response.get_data(as_text=True).removeprefix("\ufeff")
    rows = list(csv.reader(io.StringIO(content)))

    assert response.status_code == 200
    assert response.mimetype == "text/csv"
    assert response.data.startswith(b"\xef\xbb\xbf")
    assert (
        "attachment; filename=weekly_planner_export.csv"
        in response.headers["Content-Disposition"]
    )
    assert rows[0] == [
        "Date",
        "Work Done",
        "Work Note",
        "Study Done",
        "Study Note",
        "Health Done",
        "Health Note",
        "Personal Done",
        "Personal Note",
    ]
    assert rows[1][:3] == ["2026-10-12", "Yes", "Write, review\nnotes"]
    assert rows[2][:3] == ["2026-10-13", "No", "Later"]


@pytest.mark.parametrize(
    ("endpoint", "payload"),
    [
        (
            "/api/cell",
            {"date": "2026-10-12", "column": "Work", "done": True, "note": "x"},
        ),
        ("/api/clear-day", {"date": "2026-10-12"}),
        ("/api/columns/add", {"name": "Projects"}),
        ("/api/columns/rename", {"old": "Work", "new": "Projects"}),
        ("/api/columns/delete", {"name": "Work"}),
        ("/api/copy-prev-week", {"week_start": "2026-10-12"}),
    ],
)
def test_mutation_endpoints_report_storage_errors(
    client, planner, monkeypatch, endpoint, payload
):
    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(planner, "save_data", fail_save)

    response = client.post(endpoint, json=payload)

    assert response.status_code == 500
    assert response.json == {"ok": False, "error": "disk full"}


def test_load_data_falls_back_to_backup_when_primary_file_is_corrupt(planner):
    Path(planner.DATA_FILE).write_text("not json", encoding="utf-8")
    Path(planner.BACKUP_FILE).write_text(
        json.dumps({"columns": ["Custom"], "entries": {"2026-10-12": {}}}),
        encoding="utf-8",
    )

    planner.load_data()

    assert planner.columns == ["Custom"]
    assert planner.entries == {"2026-10-12": {}}


def test_backup_history_rotates_and_keeps_the_legacy_backup(
    client, planner, monkeypatch
):
    monkeypatch.setattr(planner, "MAX_BACKUPS", 3)

    for version in range(6):
        planner.entries = {
            "2026-10-12": {"Work": {"done": True, "note": f"version-{version}"}}
        }
        planner.save_data()

    backups = client.get("/api/backups")
    current = json.loads(Path(planner.DATA_FILE).read_text(encoding="utf-8"))
    legacy_backup = json.loads(Path(planner.BACKUP_FILE).read_text(encoding="utf-8"))

    assert backups.status_code == 200
    assert len(backups.json["backups"]) == 3
    assert [item["created_at"] for item in backups.json["backups"]] == sorted(
        [item["created_at"] for item in backups.json["backups"]], reverse=True
    )
    assert current["entries"]["2026-10-12"]["Work"]["note"] == "version-5"
    assert legacy_backup["entries"]["2026-10-12"]["Work"]["note"] == "version-4"


def test_legacy_bak_is_listed_and_can_be_restored(client, planner):
    current = {"columns": ["Work"], "entries": {"2026-10-12": {}}}
    previous = {
        "columns": ["Focus"],
        "entries": {"2026-10-11": {"Focus": {"done": True, "note": "Old"}}},
    }
    Path(planner.DATA_FILE).write_text(json.dumps(current), encoding="utf-8")
    Path(planner.BACKUP_FILE).write_text(json.dumps(previous), encoding="utf-8")

    backups = client.get("/api/backups")
    response = client.post(
        "/api/backups/restore", json={"backup_id": planner.LEGACY_BACKUP_ID}
    )

    assert backups.status_code == 200
    assert [item["id"] for item in backups.json["backups"]] == [
        planner.LEGACY_BACKUP_ID
    ]
    assert response.status_code == 200
    assert planner.columns == ["Focus"]
    assert planner.entries == previous["entries"]


def test_restore_endpoint_restores_a_snapshot_and_backs_up_current_data(
    client, planner
):
    planner.columns[:] = ["Focus"]
    planner.entries["2026-10-12"] = {"Focus": {"done": True, "note": "Original"}}
    planner.save_data()
    planner.entries["2026-10-12"]["Focus"]["note"] = "Current"
    planner.save_data()
    backup_id = client.get("/api/backups").json["backups"][0]["id"]

    response = client.post("/api/backups/restore", json={"backup_id": backup_id})

    assert response.status_code == 200
    assert response.json == {"ok": True, "restored": backup_id, "columns": ["Focus"]}
    assert planner.entries["2026-10-12"]["Focus"]["note"] == "Original"
    history = client.get("/api/backups").json["backups"]
    assert len(history) == 2
    current_snapshot = json.loads(
        Path(planner.BACKUP_DIR, history[0]["id"]).read_text(encoding="utf-8")
    )
    assert current_snapshot["entries"]["2026-10-12"]["Focus"]["note"] == "Current"


@pytest.mark.parametrize(
    ("backup_id", "expected_status", "expected_error"),
    [
        ("../../planner_data.json", 400, "Invalid backup ID"),
        ("planner_data-20200101T000000.000000Z.json", 404, "Backup not found"),
    ],
)
def test_restore_endpoint_rejects_unsafe_or_missing_backup_ids(
    client, backup_id, expected_status, expected_error
):
    response = client.post("/api/backups/restore", json={"backup_id": backup_id})

    assert response.status_code == expected_status
    assert response.json["error"] == expected_error


def test_restore_endpoint_rejects_a_corrupt_snapshot(client, planner):
    Path(planner.BACKUP_DIR).mkdir(parents=True)
    backup_id = "planner_data-20261009T120000.000000Z.json"
    Path(planner.BACKUP_DIR, backup_id).write_text("not json", encoding="utf-8")

    response = client.post("/api/backups/restore", json={"backup_id": backup_id})

    assert response.status_code == 400
    assert response.json["error"] == "Backup is invalid or unreadable"


def test_failed_restore_leaves_the_current_in_memory_state_unchanged(
    client, planner, monkeypatch
):
    planner.entries["2026-10-12"] = {"Work": {"done": True, "note": "Original"}}
    planner.save_data()
    planner.entries["2026-10-12"]["Work"]["note"] = "Current"
    planner.save_data()
    backup_id = client.get("/api/backups").json["backups"][0]["id"]

    def fail_save():
        raise OSError("disk full")

    monkeypatch.setattr(planner, "save_data", fail_save)
    response = client.post("/api/backups/restore", json={"backup_id": backup_id})

    assert response.status_code == 500
    assert planner.entries["2026-10-12"]["Work"]["note"] == "Current"


def test_concurrent_cell_saves_are_serialized_without_lost_updates(planner):
    save_count = 16
    start_together = Barrier(save_count)

    def save_cell(index):
        date_key = (date(2026, 10, 1) + timedelta(days=index)).isoformat()
        with planner.app.test_client() as worker_client:
            start_together.wait(timeout=10)
            response = worker_client.post(
                "/api/cell",
                json={
                    "date": date_key,
                    "column": "Work",
                    "done": True,
                    "note": f"concurrent-{index}",
                },
            )
            return response.status_code

    with ThreadPoolExecutor(max_workers=save_count) as executor:
        statuses = list(executor.map(save_cell, range(save_count)))

    persisted = json.loads(Path(planner.DATA_FILE).read_text(encoding="utf-8"))
    assert statuses == [200] * save_count
    assert len(persisted["entries"]) == save_count
    assert {cell["Work"]["note"] for cell in persisted["entries"].values()} == {
        f"concurrent-{index}" for index in range(save_count)
    }
    assert not list(Path(planner.DATA_DIR).glob("planner_data.json.tmp.*"))
