#!/usr/bin/env python3
"""Serve the verified HUD backend on localhost:8010 (Phase 11).

A LIGHTWEIGHT way to run the exact routes the JARVIS HUD talks to —
``/api/stream/token``, ``/api/stream/{deviceId}`` (SSE), ``/api/command`` —
WITHOUT booting the full ~99K unified_supervisor (Docker/GCP/GUI). It
mounts the REAL ``unified_websocket`` router (the same code the supervisor
serves) on a minimal FastAPI app, so the HUD connects to a faithful
backend that speaks the exact contract we verified end-to-end.

DRY: reuses the production router + the governance SSE bridge — no
duplicated endpoint logic. This is a dev/test harness, not a second
product surface.

Run it with the hermetic venv:

    ~/.jarvis/venv/bin/python scripts/serve_hud_backend.py

Then run JARVISHUD in Xcode (with JARVIS_HUD_EXTERNAL_BACKEND=1 in the
scheme env). NEVER auto-runs on import.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap_path() -> Path:
    """Put the repo root AND backend/ on sys.path so the router's bare
    ``core.*`` imports resolve (the supervisor arranges the same)."""
    repo = Path(__file__).resolve().parents[1]
    for p in (str(repo), str(repo / "backend")):
        if p not in sys.path:
            sys.path.insert(0, p)
    return repo


def main() -> int:
    repo = _bootstrap_path()
    # Service-mode hints so nothing heavy/interactive spins up.
    os.environ.setdefault("JARVIS_SERVICE_MODE", "1")
    os.environ.setdefault("JARVIS_ENABLE_SLIM_MODE", "SLIM")
    os.environ.setdefault("OUROBOROS_BATTLE_HEADLESS", "1")

    host = os.environ.get("JARVIS_BACKEND_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("JARVIS_BACKEND_PORT", "8010") or "8010")
    except ValueError:
        port = 8010

    try:
        import uvicorn
        from fastapi import FastAPI
        from backend.api.unified_websocket import router
    except Exception as exc:  # noqa: BLE001
        print(f"[serve_hud_backend] import failed: {exc}\n"
              f"  → run with the hermetic venv: "
              f"~/.jarvis/venv/bin/python {Path(__file__).name}", flush=True)
        return 1

    app = FastAPI(title="JARVIS HUD Backend (verified routes)")
    app.include_router(router)

    print(f"[serve_hud_backend] serving the verified HUD routes on "
          f"http://{host}:{port}", flush=True)
    print(f"[serve_hud_backend]   POST /api/stream/token   → local token", flush=True)
    print(f"[serve_hud_backend]   GET  /api/stream/{{id}}    → SSE (O+V telemetry)", flush=True)
    print(f"[serve_hud_backend]   POST /api/command        → real command pipeline", flush=True)
    print(f"[serve_hud_backend] repo: {repo}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
