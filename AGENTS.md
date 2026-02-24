# AGENTS.md

## Cursor Cloud specific instructions

### Architecture overview

JARVIS AI Agent is a Python/FastAPI backend (port 8010) + React frontend (port 3000). Designed for macOS M1/M2 but the core backend and frontend run on Linux. See `wiki/Setup-&-Installation.md` for full setup docs.

### Running services

- **Backend**: `source /workspace/venv/bin/activate && cd /workspace/backend && PYTHONPATH=/workspace:/workspace/backend JARVIS_ENV=development VOICE_ENABLED=false WAKE_WORD_ENABLED=false NEURAL_MESH_ENABLED=false GCP_VM_ENABLED=false USE_CLOUD_SQL=false JARVIS_DB_TYPE=sqlite python run_server.py --port 8010`
- **Frontend**: `cd /workspace/frontend && BROWSER=none PORT=3000 npx react-scripts start`
- Health check: `curl http://localhost:8010/health/ping` (returns `{"status":"ok","pong":true}`)
- API docs: `http://localhost:8010/docs` (Swagger UI)

### Key gotchas

- **macOS-only packages**: `pyobjc-*`, `coremltools`, `pvporcupine`, `pyaudio` are macOS-only. They're skipped on Linux. Some test files that import voice/vision modules directly will fail collection due to these missing macOS deps.
- **NumPy version**: torch 2.2.x requires `numpy<2`. Install `numpy<2` to avoid the `_ARRAY_API not found` error.
- **Frontend stuck at 50%**: The JARVIS frontend loading screen polls `startup-progress` to discover the backend. It uses `REACT_APP_BACKEND_PORT` (default 8010). Set `REACT_APP_BACKEND_PORT=8010` in `frontend/.env`. The loading gate may still show "Backend starting" if the connection service discovery cycle hasn't completed.
- **`/api/command` endpoint**: Requires a valid `ANTHROPIC_API_KEY` to process commands. Without it, requests will hang until timeout.
- **`tests/conftest.py`** imports `tests.conftest_gmd_ferrari` as a pytest plugin. This file must exist.

### Testing

- Run from repo root: `source /workspace/venv/bin/activate && python -m pytest tests/unit/ -p no:xdist --timeout=30`
- Core tests that work on Linux: `python -m pytest tests/unit/core/ tests/unit/supervisor/ tests/unit/config/ tests/unit/backend/core/ tests/unit/backend/neural_mesh/ -p no:xdist --timeout=30`
- Frontend tests: `cd frontend && npx react-scripts test --watchAll=false --ci --testPathPattern='JarvisConnectionService'`
- Pre-commit uses bandit: `bandit -r backend/ --severity-level medium --confidence-level medium -q`

### Linting

- Python: `bandit` (security linter), `black` (formatter, line-length 100), `isort` (import sorter). Config in `pyproject.toml` and `.pre-commit-config.yaml`.
- Frontend: ESLint is configured via `react-app` preset in `frontend/package.json`.
