# Deployment

Weekly Planner is a single-user Flask application that stores its state in a JSON file. It is suitable for a small private deployment, but the built-in Flask development server is for local development only.

## Before making it reachable

1. Set `PLANNER_PASSWORD` to a strong, unique password. If it is unset, the app has no login protection and every visitor can read and edit the planner.
2. Set `SECRET_KEY` to a long random value. Flask uses it to sign session cookies; never keep the development fallback key in a public deployment.
3. Serve the app over HTTPS and use the hosting provider's production WSGI server. Do not expose Flask's development server directly to the internet.
4. Configure `DATA_DIR` to a writable, persistent directory outside any ephemeral application/release directory. Restrict access to that directory and back it up.
5. Run a single WSGI worker process. The in-memory JSON store and lock serialize threaded requests within one process; multiple worker processes have separate memory and are not supported. Use a shared database before scaling to multiple processes.
6. Keep secrets and planner data out of Git. The repository `.gitignore` excludes the app's JSON data, rotated backups, temp saves, and `.env` files.

Generate a secret key with:

```bash
python -c 'import secrets; print(secrets.token_hex(32))'
```

Install the exact, resolved runtime dependency versions from the repository root with:

```bash
python -m pip install --require-hashes -r web_planner/requirements.lock
```

## PythonAnywhere (WSGI example)

In the PythonAnywhere web app configuration, point the source code to the repository's `web_planner` directory and use a WSGI file similar to the following. Replace the paths and placeholder values, and **do not commit a WSGI file containing real secrets**. If your account supports environment-variable configuration, use that instead of placing secrets directly in the WSGI file.

```python
import os
import sys

project_dir = "/home/yourusername/weekly-planner/web_planner"
data_dir = "/home/yourusername/.weekly-planner-data"

if project_dir not in sys.path:
    sys.path.insert(0, project_dir)

os.makedirs(data_dir, exist_ok=True)
os.environ["DATA_DIR"] = data_dir
os.environ["PLANNER_PASSWORD"] = "replace-with-a-strong-password"
os.environ["SECRET_KEY"] = "replace-with-a-long-random-secret"

from app import app as application
```

Set the virtualenv path in the PythonAnywhere web app settings and install the runtime dependencies there with `pip install --require-hashes -r /home/yourusername/weekly-planner/web_planner/requirements.lock`. Reload the WSGI app after changing environment settings.

## Persistent data and backups

The app writes `planner_data.json` and the compatibility file `planner_data.json.bak` inside `DATA_DIR` (which defaults to the `web_planner` directory). Up to 10 timestamped snapshots are stored in `DATA_DIR/planner_backups/`; the **Backup History** control can restore one, and restoring first snapshots the current file. `DATA_DIR` must be writable; the application creates it at startup. To migrate existing entries, stop the app and copy `planner_data.json` into the configured data directory before starting it again. Back up the whole data directory, and verify that backups can be restored.

The browser also keeps the last loaded state and unsynced cell edits in its local storage. That cache is device-local and unencrypted; clear the browser's site data to remove it. Offline shell caching requires an initial online visit over HTTPS or localhost.

## Local smoke test

For local testing only, you can start the development server from the repository root:

```bash
python -m pip install --require-hashes -r web_planner/requirements.lock
python web_planner/app.py
```

The development server binds to `0.0.0.0` for convenience; keep it on a trusted network and do not use this command as a production server.
