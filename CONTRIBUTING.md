# Contributing

Thanks for helping improve Weekly Planner. Bug reports, documentation fixes, and focused code changes are welcome.

## Development setup

Use Python 3.10 or newer. Node.js 22 is only needed to syntax-check JavaScript changes. From the repository root, create and activate a virtual environment, then install the locked development dependencies:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install --require-hashes -r web_planner/requirements-dev.lock
```

Run the same checks used by CI before opening a pull request:

```bash
python -m ruff check web_planner tests
python -m ruff format --check web_planner tests
python -m pytest
```

When changing frontend scripts, also run `node --check web_planner/static/app.js` and `node --check web_planner/static/service-worker.js` (Node.js 22).

To enable the optional local hooks, run `pre-commit install` once. You can run them against every tracked file with `pre-commit run --all-files`.

## Changes and pull requests

- Keep changes focused and add or update tests for behavior changes.
- Do not commit `planner_data.json`, `.bak`/temp files, the `planner_backups/` directory, credentials, or `.env` files. Use synthetic data in tests.
- Keep runtime dependencies in `web_planner/requirements.txt` and development/test tools in `web_planner/requirements-dev.txt`. Regenerate the corresponding lockfiles after changing dependency pins:

  ```bash
  cd web_planner
  python -m piptools compile --generate-hashes --strip-extras --allow-unsafe --output-file requirements.lock requirements.txt
  python -m piptools compile --generate-hashes --strip-extras --allow-unsafe --output-file requirements-dev.lock requirements-dev.txt
  cd ..
  ```

- Describe the motivation and user-visible impact in your pull request, and mention the checks you ran.

## Reporting security issues

Please do not publish passwords, planner data, or exploitable security details in public issues. Contact the repository maintainer privately instead.
