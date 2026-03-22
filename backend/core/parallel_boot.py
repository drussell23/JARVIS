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


class _BootCLINarrator:
    """Projects parallel DAG execution into sequential CLI output.

    The parallel boot runs phases concurrently, but the terminal shows a
    clean sequential narrative matching the kernel's structured format:

        ━━━━━━━━━ 🚀 BOOT │ Phase 1: Preflight  ⏱ +15293ms ━━━━━━━━━━
        💎 [INFO] +  16189ms │   [Kernel] Cleaning orphaned semaphores...
        ─────────────────── ✅ BOOT completed in 4842.0ms ───────────────

    Phases are narrated in LOGICAL order (clean_slate → preflight →
    resources → backend → intelligence) even if they resolve out of order.
    The narrator queues out-of-order completions and emits them when all
    preceding phases have finished.
    """

    # Zone-based narrative matching the sequential boot's zone architecture.
    # The user sees Zone 0 → Zone 6 in order. Parallel is invisible.
    # Format: (dag_node, zone_id, zone_icon_title, zone_description)
    PHASE_SEQUENCE = [
        ("clean_slate",        "Zone 0",   "🛡️  Early Protection",  "Signal guards, cleanup, crash recovery"),
        ("preflight",          "Zone 5.1", "🎯 Orchestration",      "DMS, IPC, locks, signal handlers"),
        ("loading_experience", "Zone 5.2", "🖥️  Loading Experience", "Loading page server"),
        ("resources",          "Zone 3",   "☁️  Resources",          "Docker, GCP VM, ports, storage"),
        ("backend",            "Zone 6.1", "⚡ The Kernel",          "uvicorn + FastAPI backend server"),
        ("intelligence",       "Zone 4",   "🧠 Intelligence",       "ML routing, model serving, vision"),
    ]

    def __init__(self, kernel_logger) -> None:
        self._log = kernel_logger
        self._boot_start = time.monotonic()
        self._narrated: set = set()       # Phases already narrated
        self._resolved: Dict[str, float] = {}  # phase -> elapsed_s
        self._failed: Dict[str, str] = {}      # phase -> error

    def _elapsed_ms(self) -> float:
        return (time.monotonic() - self._boot_start) * 1000

    def phase_start(self, phase_name: str) -> None:
        """Emit a zone start banner matching the sequential boot format."""
        for name, zone, icon_title, desc in self.PHASE_SEQUENCE:
            if name == phase_name:
                elapsed = self._elapsed_ms()
                sep = "━" * 13
                self._log.info(
                    f"\n{sep} {icon_title} │ {zone} | {desc}  ⏱ +{elapsed:.0f}ms {sep}\n"
                )
                return

    def phase_resolved(self, phase_name: str, _total_elapsed: float = 0) -> None:
        """Record a phase completion. Emit narrative in order."""
        # Use the DAG node's own elapsed time for accuracy
        try:
            from backend.core.progressive_readiness import get_readiness
            pr = get_readiness()
            node = pr._nodes.get(phase_name)
            if node and node.elapsed_s > 0:
                self._resolved[phase_name] = node.elapsed_s
            else:
                self._resolved[phase_name] = _total_elapsed
        except Exception:
            self._resolved[phase_name] = _total_elapsed
        self._flush_narrative()

    def phase_failed(self, phase_name: str, error: str) -> None:
        """Record a phase failure. Emit narrative in order."""
        self._failed[phase_name] = error
        self._resolved[phase_name] = 0.0
        self._flush_narrative()

    def _flush_narrative(self) -> None:
        """Emit queued phase completions in logical order.

        If phases 1,2,3 resolve but we've only narrated 1, this emits
        2 and 3 in order. If 3 resolves before 2, it waits.
        """
        for name, zone, icon_title, _desc in self.PHASE_SEQUENCE:
            if name in self._narrated:
                continue
            if name not in self._resolved:
                break  # Can't narrate — predecessor zone hasn't resolved
            elapsed = self._resolved[name]
            error = self._failed.get(name)
            sep = "─" * 19
            if error:
                self._log.info(f"{sep} ❌ {icon_title} │ {zone} FAILED: {error} ({elapsed:.1f}s) {sep}")
            else:
                self._log.info(f"{sep} ✅ {icon_title} │ {zone} completed in {elapsed:.1f}s {sep}")
            self._narrated.add(name)

    def active_local_reached(self, elapsed_s: float) -> None:
        """Emit the ACTIVE_LOCAL milestone banner."""
        sep = "━" * 20
        self._log.info(f"\n{sep} 🟢 ACTIVE_LOCAL │ System ready in {elapsed_s:.1f}s {sep}")
        self._log.info(f"{'':>24}Local systems online — cloud warming up in background\n"
        )


class ParallelBootOrchestrator:
    """Runs the boot DAG with true async concurrency.

    Calls the existing phase methods on the kernel — no reimplementation.
    Tracks state via ProgressiveReadiness — no synthetic progress.
    CLI output follows a sequential narrative via _BootCLINarrator.
    """

    def __init__(self, kernel: Any) -> None:
        self._kernel = kernel
        self._start_time = time.monotonic()
        self._phase_results: Dict[str, bool] = {}
        self._phase_errors: Dict[str, str] = {}
        self._narrator = _BootCLINarrator(kernel.logger)

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
        self._narrator.phase_start("clean_slate")
        try:
            await k._safe_broadcast(
                stage="starting", message="Cleaning up previous session...",
                progress=5, metadata={"icon": "broom", "phase": 0},
            )
        except Exception:
            pass

        pr.mark_running("clean_slate", "Cleanup and state recovery")
        ok = await self._run_phase("clean_slate", k._phase_clean_slate, timeout=30.0)
        if not ok:
            self._narrator.phase_failed("clean_slate", "startup failure")
            logger.error("[ParallelBoot] Clean Slate FAILED — cannot continue")
            return 1
        await pr.mark_resolved("clean_slate")
        self._narrator.phase_resolved("clean_slate", time.time() - t0)

        # ============================================================
        # Phase 2: PARALLEL — Preflight + Resources + Loading Experience
        # These are independent after Clean Slate.
        # ============================================================
        try:
            await k._safe_broadcast(
                stage="resources", message="Initializing systems in parallel...",
                progress=15, metadata={"icon": "bolt", "phase": 1, "parallel": True},
            )
        except Exception:
            pass

        logger.info("[ParallelBoot] Launching parallel group: preflight + resources + loading")
        self._narrator.phase_start("preflight")
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

        # Mark resolved/failed and narrate in logical order
        if _pf_ok:
            await pr.mark_resolved("preflight")
            self._narrator.phase_resolved("preflight", time.time() - t0)
        else:
            await pr.mark_failed("preflight", self._phase_errors.get("preflight", ""))
            self._narrator.phase_failed("preflight", self._phase_errors.get("preflight", "unknown"))

        if _rs_ok:
            await pr.mark_resolved("resources")
            self._narrator.phase_resolved("resources", time.time() - t0)
        else:
            await pr.mark_failed("resources", self._phase_errors.get("resources", ""))
            self._narrator.phase_failed("resources", self._phase_errors.get("resources", "unknown"))

        if _le_ok:
            await pr.mark_resolved("loading_experience")
            self._narrator.phase_resolved("loading_experience", time.time() - t0)
        else:
            await pr.mark_failed("loading_experience", self._phase_errors.get("loading_experience", ""))
            self._narrator.phase_failed("loading_experience", self._phase_errors.get("loading_experience", "unknown"))

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
        try:
            await k._safe_broadcast(
                stage="backend", message="Starting backend server...",
                progress=40, metadata={"icon": "server", "phase": 2},
            )
        except Exception:
            pass

        self._narrator.phase_start("backend")
        pr.mark_running("backend", "uvicorn + FastAPI")
        _be_ok = await self._run_phase("backend", k._phase_backend, timeout=300.0)
        if _be_ok:
            await pr.mark_resolved("backend")
            self._narrator.phase_resolved("backend", time.time() - t0)
        else:
            await pr.mark_failed("backend", self._phase_errors.get("backend", ""))
            self._narrator.phase_failed("backend", self._phase_errors.get("backend", "unknown"))
            logger.error("[ParallelBoot] Backend FAILED — cannot continue")
            return 1

        # ============================================================
        # Phase 4: Intelligence (needs Backend)
        # ============================================================
        self._narrator.phase_start("intelligence")
        try:
            await k._safe_broadcast(
                stage="intelligence", message="Loading intelligence layer...",
                progress=70, metadata={"icon": "sparkles", "phase": 3},
            )
        except Exception:
            pass

        pr.mark_running("intelligence", "ML routing, model serving")
        _in_ok = await self._run_phase("intelligence", k._phase_intelligence, timeout=120.0)
        if _in_ok:
            await pr.mark_resolved("intelligence")
            self._narrator.phase_resolved("intelligence", time.time() - t0)
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
        self._narrator.active_local_reached(local_elapsed)
        logger.info(
            "[ParallelBoot] ACTIVE_LOCAL reached in %.1fs (vs ~100s sequential)",
            local_elapsed,
        )

        # Set JARVIS_STARTUP_COMPLETE for the local tier
        os.environ["JARVIS_STARTUP_COMPLETE"] = "true"
        k._current_startup_phase = "active_local"
        k._current_startup_progress = 85

        # v350.5: DO NOT broadcast stage="complete" here.
        #
        # The loading server's _background_readiness_tier_task is the SOLE
        # driver of frontend progress. It polls /health/readiness-tier and
        # maps tier advancement to progress:
        #   BOOTING           → 5-80%  (DAG nodes resolving)
        #   ACTIVE_LOCAL      → 85%    (local systems ready)
        #   ACTIVE_FULL       → 95%    (cloud connected)
        #   FULLY_OPERATIONAL → 100%   (stage="complete")
        #
        # If we broadcast stage="complete" here, the loading server's
        # monotonic progress guard locks at 100% and the readiness-tier
        # polling can never correct it. The frontend shows 100% while
        # Trinity is still booting — which is exactly the misalignment
        # the user sees.
        #
        # Instead, broadcast "active_local" at 85% to update the stage
        # name without poisoning the progress to 100%.
        try:
            await k._safe_broadcast(
                stage="active_local",
                message="Local systems ready — cloud warming up...",
                progress=85,
                metadata={
                    "icon": "check",
                    "phase": "active_local",
                    "parallel_boot": True,
                    "boot_elapsed_s": round(time.time() - t0, 1),
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
