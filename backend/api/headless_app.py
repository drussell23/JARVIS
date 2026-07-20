"""Progressive-hydration headless ASGI app (Phase 12, Slice B).

The productized ``--headless`` backend the Mac HUD connects to. It binds
port 8010 and exposes the validated router INSTANTLY (mandate 1), then
hydrates the heavy body (OuroborosDaemon, CoreML, PyTorch) in a background
``asyncio`` task scheduled from the FastAPI lifespan — so the Swift
client's TCP handshake never waits on ML init.

DRY (mandate 3): reuses the EXISTING ``unified_websocket`` router (the
same routes ``serve_hud_backend`` validated: token / SSE / command) and
the TrinityEventBus for hydration telemetry. No duplicated endpoints, no
second event plane.

Fail-soft (mandate 2): a subsystem that OOMs during hydration degrades
via ``SYSTEM_DEGRADED`` telemetry; the ASGI server stays up and the UCP
command endpoints (with the DoubleWord failover) keep serving.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, List, Optional

logger = logging.getLogger("Jarvis.HeadlessApp")


async def _load_ouroboros_daemon() -> None:
    """Best-effort O+V hydration loader. Attempts to arm the governance
    SSE bridge + (when a governed loop context exists) awaken the
    OuroborosDaemon. Any failure raises to the orchestrator's OOM guard —
    it is caught there, degrading O+V while the command loop stays live."""
    # Arm the O+V→HUD telemetry bridge (idempotent, self-arms on the bus).
    from backend.api.governance_sse_bridge import install_governance_sse_bridge
    await install_governance_sse_bridge()
    # NOTE: full OuroborosDaemon.awaken() needs the supervisor kernel
    # context (governed_loop/oracle). In the standalone headless app it is
    # armed lazily by the supervisor when present; here we ensure the
    # bridge is live so O+V telemetry reaches the HUD the moment O+V runs.


def _default_subsystems() -> List["Any"]:
    from backend.api.progressive_hydration import Subsystem
    return [
        Subsystem(name="governance_bridge", label="O+V telemetry bridge",
                  loader=_load_ouroboros_daemon),
    ]


def create_headless_app(*, subsystems: Optional[List[Any]] = None,
                        mount_router: bool = True):
    """Build the progressive-hydration FastAPI app. The router mounts
    instantly; hydration runs in the background from the lifespan.
    ``mount_router=False`` skips the heavy unified_websocket import (tests)."""
    from fastapi import FastAPI
    from backend.api.progressive_hydration import HydrationOrchestrator

    orchestrator = HydrationOrchestrator(subsystems or _default_subsystems())

    @asynccontextmanager
    async def lifespan(app: "FastAPI"):
        # Mandate 1: DO NOT await hydration here — schedule it so uvicorn
        # finishes startup + binds/serves instantly. The heavy load runs
        # off the request path.
        task = asyncio.create_task(orchestrator.hydrate(), name="hydration")
        app.state.hydration = orchestrator
        app.state.hydration_task = task
        try:
            yield
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    app = FastAPI(title="JARVIS Headless (progressive hydration)",
                  lifespan=lifespan)

    # DRY: the validated routes (token / SSE / command) from the real
    # unified_websocket router — the same ones serve_hud_backend mounted.
    if mount_router:
        try:
            from backend.api.unified_websocket import router
            app.include_router(router)
        except Exception:  # noqa: BLE001
            logger.warning("[HeadlessApp] router mount degraded", exc_info=True)

    @app.get("/api/hydration/status")
    async def hydration_status():
        """Live hydration state for the HUD (BOOTING/HYDRATING/READY/
        DEGRADED). Available the instant the app binds — before subsystems
        finish."""
        return orchestrator.snapshot()

    return app


def main() -> int:
    """Run the headless daemon on 8010 with progressive hydration."""
    import sys
    from pathlib import Path
    repo = Path(__file__).resolve().parents[2]
    for p in (str(repo), str(repo / "backend")):
        if p not in sys.path:
            sys.path.insert(0, p)
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
    except Exception as exc:  # noqa: BLE001
        print(f"[headless_app] uvicorn unavailable: {exc}", flush=True)
        return 1
    print(f"[headless_app] progressive-hydration daemon on http://{host}:{port} "
          "— router serves INSTANTLY, O+V hydrates in background", flush=True)
    uvicorn.run(create_headless_app(), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["create_headless_app", "main"]
