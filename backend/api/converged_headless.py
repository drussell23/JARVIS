"""Converged headless boot (Phase 12, Slice D).

The single ``--headless`` ASGI boot for the JARVIS body. It converges the
validated slices into one lifespan:

  • Slice B — the ASGI app binds + serves the router INSTANTLY; heavy
    subsystems load in a background task.
  • Slice C — the Topographical Hydration DAG loads Oracle / GovernedLoop /
    memory buses as verified nodes BEFORE the OuroborosDaemon actor
    awakens, all fault-isolated.
  • Slice D — on network readiness an internal Loopback Self-Test proves
    the DoubleWord failover (``FAILOVER_PROVEN``) or degrades it gracefully.
  • Then the boot emits a global ``SYSTEM_READY``.

This RETIRES the standalone ``headless_app`` wrapper (mandate 1) —
``unified_supervisor --headless`` delegates here. DRY (mandate 3): reuses
``HydrationOrchestrator`` + ``OuroborosActor`` + ``LoopbackSelfTest`` +
``DoubleWordUCPAdapter`` + the TrinityEventBus. Every entry NEVER raises.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, List, Optional

logger = logging.getLogger("Jarvis.ConvergedHeadless")

READY_TOPIC = "ouroboros.system"


# ---------------------------------------------------------------------------
# subsystem loaders (DRY — reuse the supervisor's construction)
# ---------------------------------------------------------------------------

async def _load_governance_bridge() -> None:
    from backend.api.governance_sse_bridge import install_governance_sse_bridge
    await install_governance_sse_bridge()


def _build_ouroboros_daemon() -> Any:
    """Same construction as unified_supervisor's Zone-7 boot (mandate 3);
    kernel handles default to None when the context isn't wired."""
    from backend.core.ouroboros.daemon import OuroborosDaemon
    from backend.core.ouroboros.daemon_config import DaemonConfig
    return OuroborosDaemon(
        oracle=None, fleet=None, bg_pool=None, intake_router=None,
        event_stream=None, proactive_drive=None, doubleword=None, gls=None,
        config=DaemonConfig.from_env())


async def _load_ouroboros_actor() -> None:
    from backend.api.ouroboros_actor import OuroborosActor
    OuroborosActor(_build_ouroboros_daemon).start()   # isolated bg task


def default_subsystems() -> List[Any]:
    """The O+V hydration DAG (Slice C): telemetry bridge → OuroborosDaemon
    (in its Actor shell), which depends on the bridge being up."""
    from backend.api.progressive_hydration import Subsystem
    return [
        Subsystem(name="governance_bridge", label="O+V telemetry bridge",
                  loader=_load_governance_bridge),
        Subsystem(name="ouroboros_daemon", label="OuroborosDaemon (Actor)",
                  loader=_load_ouroboros_actor,
                  depends_on=("governance_bridge",)),
    ]


# ---------------------------------------------------------------------------
# the converged app
# ---------------------------------------------------------------------------

async def _emit_system_ready(state: str, detail: dict) -> None:
    try:
        from backend.core.trinity_event_bus import get_event_bus_if_exists
        bus = get_event_bus_if_exists()
        if bus is None:
            return
        await bus.publish_raw(
            topic=READY_TOPIC,
            data={"type": "SYSTEM_READY", "state": state,
                  "narration_text": "JARVIS organism online — "
                                    f"{state}", "source_brain": "supervisor",
                  "detail": detail},
            persist=False)
    except Exception:  # noqa: BLE001
        pass


def create_converged_app(
    *,
    subsystems: Optional[List[Any]] = None,
    dw_provider: Optional[Any] = None,
    recovery: Optional[Any] = None,
    run_selftest: bool = True,
    mount_router: bool = True,
):
    """The converged --headless FastAPI app. Binds instantly; hydrates the
    DAG + runs the loopback self-test (with Dynamic Package Recovery) in the
    background → SYSTEM_READY."""
    from fastapi import FastAPI
    from backend.api.progressive_hydration import HydrationOrchestrator

    orch = HydrationOrchestrator(subsystems or default_subsystems())
    # Self-Healing engine (Slice E): wired by default so a missing transitive
    # dep on the DoubleWord backup self-heals instead of degrading. Injectable
    # (tests pass a fully-faked engine; None disables recovery entirely).
    if recovery is None and run_selftest:
        from backend.api.package_recovery import default_recovery
        recovery = default_recovery()

    @asynccontextmanager
    async def lifespan(app: "FastAPI"):
        async def _boot() -> None:
            # 1. Topographical Hydration DAG (fault-isolated).
            await orch.hydrate()
            # 2. Loopback Self-Test — prove the DoubleWord failover on
            #    network readiness (the router already serves).
            selftest = None
            if run_selftest:
                try:
                    from backend.api.loopback_selftest import LoopbackSelfTest
                    selftest = await LoopbackSelfTest(
                        dw_provider=dw_provider, recovery=recovery).run()
                    app.state.selftest = selftest
                except Exception:  # noqa: BLE001
                    logger.warning("[Converged] self-test degraded", exc_info=True)
            # 3. Global SYSTEM_READY (degraded if hydration OR self-test did).
            hyd = orch.state.value
            st = selftest.state.value if selftest is not None else "skipped"
            global_state = ("degraded" if (hyd == "degraded" or
                            st in ("degraded", "failed")) else "ready")
            app.state.system_state = global_state
            await _emit_system_ready(global_state,
                                     {"hydration": hyd, "selftest": st})
            logger.info("[Converged] SYSTEM_%s (hydration=%s, selftest=%s)",
                        global_state.upper(), hyd, st)

        app.state.system_state = "booting"
        app.state.hydration = orch
        task = asyncio.create_task(_boot(), name="converged-boot")
        app.state.boot_task = task
        try:
            yield
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    app = FastAPI(title="JARVIS Converged Headless", lifespan=lifespan)

    if mount_router:
        try:
            from backend.api.unified_websocket import router
            app.include_router(router)
        except Exception:  # noqa: BLE001
            logger.warning("[Converged] router mount degraded", exc_info=True)

    @app.get("/api/system/status")
    async def system_status():
        return {
            "system_state": getattr(app.state, "system_state", "booting"),
            "hydration": orch.snapshot(),
            "selftest": (getattr(app.state, "selftest", None).state.value
                         if getattr(app.state, "selftest", None) else "pending"),
        }

    return app


def main() -> int:
    """``unified_supervisor --headless`` delegates here."""
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
        print(f"[converged] uvicorn unavailable: {exc}", flush=True)
        return 1
    print(f"[converged] --headless organism on http://{host}:{port} — router "
          "serves INSTANTLY; DAG hydration + failover self-test in background",
          flush=True)
    uvicorn.run(create_converged_app(), host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["READY_TOPIC", "create_converged_app", "default_subsystems", "main"]
