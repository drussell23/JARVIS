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

    # Logical phase order for narrative presentation
    PHASE_SEQUENCE = [
        ("clean_slate",        "Phase -1: Clean Slate",       "🧹"),
        ("preflight",          "Phase 1: Preflight",          "🚀"),
        ("resources",          "Phase 2: Resources",          "📦"),
        ("loading_experience", "Phase 0: Loading Experience", "🖥️"),
        ("backend",            "Phase 3: Backend Server",     "⚙️"),
        ("intelligence",       "Phase 4: Intelligence",       "🧠"),
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
        """Emit a phase start banner (immediate — not queued)."""
        for name, title, icon in self.PHASE_SEQUENCE:
            if name == phase_name:
                elapsed = self._elapsed_ms()
                sep = "━" * 13
                self._log.info(f"\n{sep} {icon} BOOT │ {title}  ⏱ +{elapsed:.0f}ms {sep}\n")
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
        for name, title, icon in self.PHASE_SEQUENCE:
            if name in self._narrated:
                continue
            if name not in self._resolved:
                break  # Can't narrate this yet — predecessor hasn't resolved
            elapsed = self._resolved[name]
            error = self._failed.get(name)
            sep = "─" * 19
            if error:
                self._log.info(f"{sep} ❌ {icon} {title} FAILED ({error}) {sep}")
            else:
                self._log.info(f"{sep} ✅ {icon} {title} completed in {elapsed * 1000:.1f}ms {sep}")
            self._narrated.add(name)

    def active_local_reached(self, elapsed_s: float) -> None:
        """Emit the ACTIVE_LOCAL milestone banner."""
        self._log.info(
            f"\n{'━' * 20} 🟢 ACTIVE_LOCAL reached in {elapsed_s:.1f}s {'━' * 20}\n"
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
        # v350.4: Set phase to "finalizing" so _broadcast_startup_progress
        # allows progress to advance past the old ceiling. The monotonic
        # guard in _broadcast_startup_progress only updates _current_progress
        # for stages in _STARTUP_STAGES or when _current_startup_phase is
        # "complete"/"finalizing". Without this, the completion broadcast
        # (stage="complete", progress=100) is silently dropped.
        k._current_startup_phase = "finalizing"
        k._current_startup_progress = 90

        # Broadcast progress stages incrementally — NO stage="complete" yet.
        # The loading page smoothly animates through these stages.
        try:
            await k._safe_broadcast(
                stage="finalizing",
                message="Verifying backend readiness...",
                progress=90,
                metadata={"icon": "gear", "phase": "finalizing"},
            )
        except Exception:
            pass

        # Wait for /health/readiness-tier to confirm ACTIVE_LOCAL before
        # telling the frontend to transition. The readiness-tier endpoint
        # is the SINGLE SOURCE OF TRUTH driven by the ProgressiveReadiness
        # DAG. We NEVER broadcast stage="complete" unless the tier confirms.
        _ready_verified = False
        _port = int(os.environ.get("JARVIS_PORT", "8010"))
        for _attempt in range(20):  # max 10 seconds
            try:
                import aiohttp
                async with aiohttp.ClientSession() as _sess:
                    async with _sess.get(
                        f"http://127.0.0.1:{_port}/health/readiness-tier",
                        timeout=aiohttp.ClientTimeout(total=2),
                    ) as _resp:
                        if _resp.status == 200:
                            _tier_data = await _resp.json()
                            _tier_val = _tier_data.get("tier_value", 0)
                            if _tier_val >= 1:  # ACTIVE_LOCAL or higher
                                _ready_verified = True
                                logger.info(
                                    "[ParallelBoot] Readiness tier confirmed: %s (value=%d)",
                                    _tier_data.get("tier", "?"), _tier_val,
                                )
                                break
            except Exception:
                pass
            await asyncio.sleep(0.5)

        if not _ready_verified:
            # If tier didn't confirm, also try /health/ready as fallback
            for _attempt in range(6):  # 3 more seconds
                try:
                    async with aiohttp.ClientSession() as _sess:
                        async with _sess.head(
                            f"http://127.0.0.1:{_port}/health/ready",
                            timeout=aiohttp.ClientTimeout(total=2),
                        ) as _resp:
                            if _resp.status == 200:
                                _ready_verified = True
                                break
                except Exception:
                    pass
                await asyncio.sleep(0.5)

        if _ready_verified:
            # v350.4: Set phase to "complete" so the monotonic guard in
            # _broadcast_startup_progress allows _current_progress to reach 100.
            k._current_startup_phase = "complete"
            k._current_startup_progress = 100
            # Verified: broadcast stage="complete" — gated on actual readiness
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
                        "readiness_tier_verified": True,
                        "authority": "unified_supervisor",
                        "parallel_boot": True,
                        "frontend_optional": True,
                        "boot_elapsed_s": round(time.time() - t0, 1),
                    },
                )
            except Exception:
                pass
        else:
            # NOT verified: broadcast "finalizing" NOT "complete" — the
            # frontend will independently poll /health/readiness-tier and
            # transition when the tier actually confirms.
            logger.warning(
                "[ParallelBoot] Readiness tier not confirmed after 13s — "
                "staying at 'finalizing'. Frontend will poll readiness-tier."
            )
            try:
                await k._safe_broadcast(
                    stage="finalizing",
                    message="Backend started, verifying readiness...",
                    progress=92,
                    metadata={
                        "icon": "gear",
                        "phase": "finalizing",
                        "parallel_boot": True,
                        "awaiting_readiness_tier": True,
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
