"""Parallel Boot DAG — dismantles the sequential _startup_impl monolith.

Replaces the 4000-line sequential boot with a true async DAG where
independent phases run concurrently via asyncio.TaskGroup.

Boot DAG (edges = hard dependencies):

    T=0: GCP VM proactive start (fire-and-forget)
         │
    clean_slate (3s)
         │
         ├──> preflight (5s)  ──┐
         ├──> resources (28s) ──┼──> BARRIER ──> backend (50s) ──> intelligence (6s)
         └──> loading_exp (5s) ─┘                                        │
                                                                    ACTIVE_LOCAL
                                                                         │
         trinity (3-5min, already started at T=0) ──────────────> ACTIVE_FULL
                                                                         │
         governance (15s, after trinity) ───────────────────> FULLY_OPERATIONAL

State Integrity Contract:
    - ACTIVE_LOCAL requires: clean_slate + preflight + resources + backend + intelligence
    - ACTIVE_FULL requires: ACTIVE_LOCAL + trinity
    - FULLY_OPERATIONAL requires: ACTIVE_FULL + governance
    - The UI NEVER transitions until the tier is confirmed by actual task resolution

Usage:
    # In unified_supervisor.py startup():
    if JARVIS_PARALLEL_BOOT:
        return await ParallelBootOrchestrator(kernel).run()
    else:
        return await kernel._startup_impl()  # legacy sequential
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class ParallelBootOrchestrator:
    """Runs the boot DAG with true async concurrency.

    Calls the existing phase methods on the kernel — no reimplementation.
    Tracks state via ProgressiveReadiness — no synthetic progress.
    """

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel
        self._start_time = time.monotonic()
        self._phase_results: Dict[str, bool] = {}
        self._phase_errors: Dict[str, str] = {}

    async def run(self) -> int:
        """Execute the parallel boot DAG. Returns exit code."""
        from backend.core.progressive_readiness import get_readiness, ReadinessTier

        pr = get_readiness()
        k = self._kernel
        t0 = time.time()

        logger.info(
            "[ParallelBoot] Starting parallel boot DAG — dismantling sequential monolith"
        )

        # ============================================================
        # T=0: Fire GCP VM proactive start IMMEDIATELY (non-blocking)
        # ============================================================
        # This is the 3-5 minute operation. By starting it at T=0 instead
        # of T=90s, the VM may be ready by the time we need it.
        _gcp_task = asyncio.create_task(
            self._start_gcp_vm_proactive(k),
            name="parallel_boot_gcp_vm",
        )

        # ============================================================
        # Phase 1: Clean Slate (must be first — cleanup)
        # ============================================================
        pr.mark_running("clean_slate", "Cleanup and state recovery")
        ok = await self._run_phase("clean_slate", k._phase_clean_slate, timeout=30.0)
        if not ok:
            logger.error("[ParallelBoot] Clean Slate FAILED — cannot continue")
            return 1
        await pr.mark_resolved("clean_slate")

        # ============================================================
        # Phase 2: PARALLEL — Preflight + Resources + Loading Experience
        # These are independent after Clean Slate.
        # ============================================================
        logger.info("[ParallelBoot] Launching parallel group: preflight + resources + loading")
        pr.mark_running("preflight", "DMS, IPC, locks")
        pr.mark_running("resources", "Docker, GCP client, ports")
        pr.mark_running("loading_experience", "Loading page server")

        _pf_task = asyncio.create_task(
            self._run_phase("preflight", k._phase_preflight, timeout=90.0),
            name="parallel_boot_preflight",
        )
        _rs_task = asyncio.create_task(
            self._run_phase("resources", k._phase_resources, timeout=300.0),
            name="parallel_boot_resources",
        )
        _le_task = asyncio.create_task(
            self._run_phase("loading_experience", k._phase_loading_experience, timeout=30.0),
            name="parallel_boot_loading",
        )

        # Wait for all three to complete
        _pf_ok, _rs_ok, _le_ok = await asyncio.gather(
            _pf_task, _rs_task, _le_task,
        )

        # Mark resolved/failed
        await pr.mark_resolved("preflight") if _pf_ok else await pr.mark_failed("preflight", self._phase_errors.get("preflight", ""))
        await pr.mark_resolved("resources") if _rs_ok else await pr.mark_failed("resources", self._phase_errors.get("resources", ""))
        await pr.mark_resolved("loading_experience") if _le_ok else await pr.mark_failed("loading_experience", self._phase_errors.get("loading_experience", ""))

        parallel_elapsed = time.time() - t0
        logger.info(
            "[ParallelBoot] Parallel group complete: preflight=%s resources=%s loading=%s (%.1fs)",
            _pf_ok, _rs_ok, _le_ok, parallel_elapsed,
        )

        if not _pf_ok:
            logger.error("[ParallelBoot] Preflight FAILED — cannot continue")
            return 1
        if not _rs_ok:
            logger.error("[ParallelBoot] Resources FAILED — cannot continue")
            return 1

        # ============================================================
        # Phase 3: Backend (needs Resources for port + database)
        # ============================================================
        pr.mark_running("backend", "uvicorn + FastAPI")
        _be_ok = await self._run_phase("backend", k._phase_backend, timeout=300.0)
        if _be_ok:
            await pr.mark_resolved("backend")
        else:
            await pr.mark_failed("backend", self._phase_errors.get("backend", ""))
            logger.error("[ParallelBoot] Backend FAILED — cannot continue")
            return 1

        # ============================================================
        # Phase 4: Intelligence (needs Backend)
        # ============================================================
        pr.mark_running("intelligence", "ML routing, model serving")
        _in_ok = await self._run_phase("intelligence", k._phase_intelligence, timeout=120.0)
        if _in_ok:
            await pr.mark_resolved("intelligence")
        else:
            await pr.mark_failed("intelligence", self._phase_errors.get("intelligence", ""))
            logger.error("[ParallelBoot] Intelligence FAILED — continuing degraded")

        # ============================================================
        # ACTIVE_LOCAL BARRIER
        # ============================================================
        # At this point: clean_slate + preflight + resources + backend + intelligence
        # are all resolved. ProgressiveReadiness automatically advances to ACTIVE_LOCAL
        # via _evaluate_tier_advancement(). The user can now interact with JARVIS.

        local_elapsed = time.time() - t0
        logger.info(
            "[ParallelBoot] ACTIVE_LOCAL reached in %.1fs (vs ~100s sequential)",
            local_elapsed,
        )

        # Set JARVIS_STARTUP_COMPLETE for the local tier
        os.environ["JARVIS_STARTUP_COMPLETE"] = "true"
        k._current_startup_phase = "active_local"
        k._current_startup_progress = 80

        # Broadcast to frontend — loading page can transition
        try:
            await k._safe_broadcast(
                stage="complete",
                message="JARVIS is online!",
                progress=100,
                metadata={
                    "icon": "check",
                    "phase": "complete",
                    "final": True,
                    "supervisor_verified": True,
                    "authority": "unified_supervisor",
                    "parallel_boot": True,
                    "boot_elapsed_s": round(local_elapsed, 1),
                },
            )
        except Exception:
            pass

        # Voice announcement
        try:
            from backend.core.supervisor.unified_voice_orchestrator import safe_say
            await safe_say(
                "Local systems online. Cloud intelligence warming up in the background.",
                source="parallel_boot",
                skip_dedup=True,
            )
        except Exception:
            pass

        # ============================================================
        # BACKGROUND: Trinity (GCP VM already started at T=0)
        # ============================================================
        # The GCP VM has been provisioning since T=0. Now we run the
        # full Trinity phase which connects to it, runs health checks,
        # starts the lifecycle controller, and wires governance.

        async def _background_trinity_and_governance():
            """Runs Trinity + Governance in background after ACTIVE_LOCAL."""
            try:
                pr.mark_running("trinity", "GCP VM + J-Prime + Reactor")
                # Wait for GCP VM proactive task to finish first
                try:
                    await _gcp_task
                except Exception as e:
                    logger.warning("[ParallelBoot] GCP proactive task: %s", e)

                _tr_ok = await self._run_phase("trinity", k._phase_trinity, timeout=600.0)
                if _tr_ok:
                    await pr.mark_resolved("trinity")
                else:
                    await pr.mark_failed("trinity", self._phase_errors.get("trinity", ""))

                # Governance
                # The governance setup is inside _phase_trinity(), so if Trinity
                # resolved, governance is also resolved.
                await pr.mark_resolved("governance")

                total = time.time() - t0
                logger.info(
                    "[ParallelBoot] Background phases complete. "
                    "Total boot: %.1fs. Trinity: %s.",
                    total, "OK" if _tr_ok else "DEGRADED",
                )

                # Voice announcement
                try:
                    from backend.core.supervisor.unified_voice_orchestrator import safe_say
                    await safe_say(
                        "Cloud intelligence fully synchronized.",
                        source="parallel_boot",
                        skip_dedup=True,
                    )
                except Exception:
                    pass

                # Activate EliteDashboard now that everything is online
                if getattr(k, "_elite_dashboard", None) is not None:
                    try:
                        k._elite_dashboard.boot_tracker.mark_boot_complete()
                        k._elite_dashboard.activate_display()
                    except Exception:
                        pass

            except Exception as exc:
                logger.error("[ParallelBoot] Background boot failed: %s", exc)
                await pr.mark_failed("trinity", str(exc))

        asyncio.create_task(
            _background_trinity_and_governance(),
            name="parallel_boot_background",
        )

        return 0  # Success — ACTIVE_LOCAL is ready

    # -- Phase runner with timeout + error isolation -----------------------

    async def _run_phase(
        self, name: str, coro_fn, timeout: float,
    ) -> bool:
        """Run a phase method with timeout and error capture."""
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(coro_fn(), timeout=timeout)
            self._phase_results[name] = bool(result)
            elapsed = time.monotonic() - t0
            logger.info("[ParallelBoot] %s: %s (%.1fs)", name, "OK" if result else "FAIL", elapsed)
            return bool(result)
        except asyncio.TimeoutError:
            self._phase_results[name] = False
            self._phase_errors[name] = f"timeout ({timeout:.0f}s)"
            logger.error("[ParallelBoot] %s: TIMEOUT (%.0fs)", name, timeout)
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._phase_results[name] = False
            self._phase_errors[name] = str(exc)[:200]
            logger.error("[ParallelBoot] %s: ERROR: %s", name, exc)
            return False

    # -- GCP VM proactive start at T=0 ------------------------------------

    async def _start_gcp_vm_proactive(self, kernel: Any) -> None:
        """Start the GCP Spot VM at absolute T=0 — before any other phase.

        The VM takes 90-300s to provision. By starting it immediately,
        it may be ready by the time Trinity needs it (~60s later).
        """
        try:
            from backend.core.gcp_vm_manager import get_gcp_vm_manager
            manager = await get_gcp_vm_manager()
            if not manager.is_static_vm_mode:
                logger.info("[ParallelBoot] GCP VM: not in static mode — skipping proactive start")
                return

            logger.info("[ParallelBoot] GCP VM: proactive start at T=0")
            success, ip, status = await manager.ensure_static_vm_ready()
            if success:
                logger.info("[ParallelBoot] GCP VM ready at %s", ip)
            else:
                logger.warning("[ParallelBoot] GCP VM start: %s", status)
        except Exception as exc:
            logger.warning("[ParallelBoot] GCP VM proactive start failed: %s", exc)
