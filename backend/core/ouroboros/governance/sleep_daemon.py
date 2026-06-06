"""Sleep Consolidation Daemon — Slice 101 Phase 6 (the Synthetic Soul).

A background async daemon that runs the cross-session memory consolidation
cascade OFF the hot path, so the organism's long-horizon "deep learning" never
blocks the FSM. Each cycle composes the EXISTING (previously dormant) substrates
— it adds orchestration + cadence, not new memory logic:

    belief_revision_ledger (FALSIFIED beliefs)  ─┐
    postmortem_fusion (root-cause clusters)      ─┤→ sleep_consolidation_pass
                                                    (stable patterns, persisted)
                                                  → meta_prior_learning
                                                    (prior calibration, persisted)

VERIFY-FIRST (2026-06-06): the cascade is ~95% self-composing already —
``run_consolidation_pass`` internally pulls belief + fusion; ``fuse_recent_
postmortems`` already extracts ROOT CAUSES (``root_cause_class`` /
``representative_root_cause`` / ``suggested_next_action``), not symptoms. The
daemon's job is to RUN the passes on an idle-gated cadence and surface a unified
report. Both passes persist to JSONL, so the consolidated memory **survives
reboots**; it is retrieved at GENERATE by
``strategic_direction._render_consolidated_memory_section``.

HONEST SCOPE NOTE: ``meta_prior_learning`` is prior-STRATEGY calibration (win
rates from the Schelling consensus stream), a *complementary* signal — NOT the
failed-paradigm store. The persistent "avoid-this-paradigm" engram is the belief
+ postmortem-fusion ledger. The daemon refreshes both; the GENERATE retrieval
reads the failure root-causes.

Master ``JARVIS_SLEEP_DAEMON_ENABLED`` — §33.1 default-FALSE. The daemon NEVER
raises into the loop; every substrate call is independently guarded.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger("ouroboros.sleep_daemon")

_ENV_ENABLED = "JARVIS_SLEEP_DAEMON_ENABLED"
_ENV_INTERVAL_S = "JARVIS_SLEEP_DAEMON_INTERVAL_S"
_TRUTHY = ("1", "true", "yes", "on")

# Default cadence — 30 min, matching sleep_consolidation's idle threshold so a
# cycle's idle_seconds proxy clears the pass's own idle gate.
_DEFAULT_INTERVAL_S = 1800.0
_MIN_INTERVAL_S = 5.0

SLEEP_CYCLE_SCHEMA_VERSION = "sleep_cycle.1"


def sleep_daemon_enabled() -> bool:
    """§33.1 master — default FALSE. Never raises."""
    try:
        raw = os.environ.get(_ENV_ENABLED)
        if raw is None:
            return False
        return raw.strip().lower() in _TRUTHY
    except Exception:  # noqa: BLE001
        return False


def daemon_interval_s() -> float:
    """Idle-gated cadence for the background loop. Clamped to a sane floor."""
    try:
        raw = float(os.environ.get(_ENV_INTERVAL_S, str(_DEFAULT_INTERVAL_S)))
        return max(_MIN_INTERVAL_S, raw)
    except Exception:  # noqa: BLE001
        return _DEFAULT_INTERVAL_S


@dataclass(frozen=True)
class SleepCycleReport:
    """Unified telemetry for one consolidation cycle. Frozen artifact."""

    master_enabled: bool
    consolidation_verdict: str
    consolidation_candidates: int
    fused_cluster_count: int
    meta_dominant_count: int
    meta_declining_count: int
    autobiography_finding: str
    autobiography_escape_count: int
    diagnostic: str
    elapsed_s: float
    schema_version: str = SLEEP_CYCLE_SCHEMA_VERSION


def _disabled_report(started: float) -> SleepCycleReport:
    return SleepCycleReport(
        master_enabled=False,
        consolidation_verdict="disabled",
        consolidation_candidates=0,
        fused_cluster_count=0,
        meta_dominant_count=0,
        meta_declining_count=0,
        autobiography_finding="corpus_disabled",
        autobiography_escape_count=0,
        diagnostic=f"sleep daemon disabled via {_ENV_ENABLED}=false",
        elapsed_s=0.0,
    )


def run_sleep_cycle_once(
    *,
    idle_seconds: Optional[float] = None,
    now_unix: Optional[float] = None,
) -> SleepCycleReport:
    """Run ONE consolidation cycle synchronously. Composes the existing passes;
    NEVER raises. Returns a DISABLED report when the master flag is off.

    This is the unit the async loop drives; it is also the deterministic seam
    the cross-session test exercises directly.
    """
    started = time.time() if now_unix is None else float(now_unix)
    if not sleep_daemon_enabled():
        return _disabled_report(started)

    idle = daemon_interval_s() if idle_seconds is None else float(idle_seconds)

    # 1) Sleep-consolidation pass — auto-composes belief (FALSIFIED) + fusion.
    consolidation_verdict = "skipped"
    consolidation_candidates = 0
    try:
        from backend.core.ouroboros.governance.sleep_consolidation_pass import (
            run_consolidation_pass,
        )
        report = run_consolidation_pass(idle)
        consolidation_verdict = str(getattr(report.verdict, "value", report.verdict))
        consolidation_candidates = len(getattr(report, "candidates", ()) or ())
    except Exception as exc:  # noqa: BLE001 — one pass failing never aborts the cycle
        logger.debug("[SleepDaemon] consolidation pass failed: %s", exc)

    # 2) Root-cause fusion count (for telemetry; the pass already consumed it).
    fused_cluster_count = 0
    try:
        from backend.core.ouroboros.governance.postmortem_fusion import (
            fuse_recent_postmortems,
        )
        fusion = fuse_recent_postmortems(now_unix=started)
        fused_cluster_count = len(getattr(fusion, "meta_postmortems", ()) or ())
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SleepDaemon] fusion count failed: %s", exc)

    # 2b) Adversarial autobiography self-audit — Slice 101 Phase 7. The organism
    # reads its OWN recent commit history (git log + git show, scoped to O+V-
    # signed commits) and probes each diff against the canonical adversarial
    # corpus: did a shipped autonomous commit smuggle a cage-bypass pattern? This
    # is the right home for an expensive git self-audit — off the hot path, on
    # the idle sleep cadence. The audit self-publishes SSE + ledger. A
    # CORPUS_ESCAPE is a loud SAFETY alarm (we surface it, we do NOT auto-
    # remediate — un-gameable observability is the correct posture for a self-
    # integrity detector). Self-gates on JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED
    # (returns CORPUS_DISABLED cheaply, before any git, when off).
    autobiography_finding = "corpus_disabled"
    autobiography_escape_count = 0
    try:
        from backend.core.ouroboros.governance.adversarial_autobiography import (
            audit_autobiography,
        )
        audit = audit_autobiography(force_refresh=True)
        autobiography_finding = str(
            getattr(audit.finding, "value", audit.finding)
        )
        autobiography_escape_count = int(getattr(audit, "escape_count", 0) or 0)
        if autobiography_finding == "corpus_escape":
            logger.warning(
                "[SleepDaemon] SELF-AUDIT CAGE ESCAPE — %d O+V commit(s) "
                "shipped an adversarial-corpus pattern (cage_health=%.3f). "
                "Operator review required.",
                autobiography_escape_count,
                float(getattr(audit, "cage_health_ratio", 0.0) or 0.0),
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SleepDaemon] autobiography audit failed: %s", exc)

    # 3) Meta-prior calibration refresh — complementary stream, also persists.
    meta_dominant = 0
    meta_declining = 0
    try:
        from backend.core.ouroboros.governance.meta_prior_learning import (
            compute_meta_distribution,
        )
        meta = compute_meta_distribution(now_unix=started)
        meta_dominant = int(getattr(meta, "dominant_count", 0) or 0)
        meta_declining = int(getattr(meta, "declining_count", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[SleepDaemon] meta-prior refresh failed: %s", exc)

    elapsed = max(0.0, time.time() - started)
    diagnostic = (
        f"consolidation={consolidation_verdict} "
        f"candidates={consolidation_candidates} "
        f"fused={fused_cluster_count} "
        f"meta_dom={meta_dominant} meta_decl={meta_declining} "
        f"autobiography={autobiography_finding}"
    )
    if (
        consolidation_verdict in ("consolidated", "dreaming")
        or fused_cluster_count > 0
        or autobiography_escape_count > 0
    ):
        logger.info("[SleepDaemon] cycle complete — %s", diagnostic)
    return SleepCycleReport(
        master_enabled=True,
        consolidation_verdict=consolidation_verdict,
        consolidation_candidates=consolidation_candidates,
        fused_cluster_count=fused_cluster_count,
        meta_dominant_count=meta_dominant,
        meta_declining_count=meta_declining,
        autobiography_finding=autobiography_finding,
        autobiography_escape_count=autobiography_escape_count,
        diagnostic=diagnostic,
        elapsed_s=elapsed,
    )


async def run_sleep_daemon_loop(
    *,
    interval_s: Optional[float] = None,
    max_cycles: Optional[int] = None,
    sleep_fn: Any = None,
) -> int:
    """Background loop: run a consolidation cycle every ``interval_s`` (idle-gated
    cadence). Returns the number of cycles run. Bounded by ``max_cycles`` (None =
    until cancelled). Inert (returns 0 immediately) when the master flag is off.
    NEVER raises except ``asyncio.CancelledError`` (propagated for clean stop).

    ``sleep_fn`` defaults to ``asyncio.sleep`` (injectable for tests).
    """
    if not sleep_daemon_enabled():
        return 0
    _sleep = sleep_fn if sleep_fn is not None else asyncio.sleep
    _interval = daemon_interval_s() if interval_s is None else max(_MIN_INTERVAL_S, float(interval_s))
    cycles = 0
    try:
        while max_cycles is None or cycles < max_cycles:
            try:
                run_sleep_cycle_once(idle_seconds=_interval)
            except Exception as exc:  # noqa: BLE001 — a bad cycle never kills the daemon
                logger.debug("[SleepDaemon] cycle swallowed: %s", exc)
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            await _sleep(_interval)
    except asyncio.CancelledError:
        logger.debug("[SleepDaemon] loop cancelled after %d cycle(s)", cycles)
        raise
    return cycles
