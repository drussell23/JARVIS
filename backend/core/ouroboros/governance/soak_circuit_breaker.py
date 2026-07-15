"""SoakCircuitBreaker — Slice 19: fail-closed Budget & Compute circuit-breaker.

Purpose
-------
Let an *unattended* graduation soak run without runaway cloud spend. GCP
failover is default-ON (``JARVIS_FAILOVER_LIFECYCLE_ENABLED``), so a genuine
DW outage can auto-provision a real GCE J-Prime node — real money, no human at
the keyboard. This breaker is the absolute, fail-closed boundary around that.

Two INDEPENDENT trip triggers, each a dynamically-configured local threshold
(no shell timer, no hardcoded ceiling):

  1. CUMULATIVE COST  — total session API + GCE compute spend (USD).
  2. GCE COMPUTE RUNTIME — cumulative wall-time across all live GCE nodes.

Either crossing trips the breaker. A trip is STICKY for the process lifetime —
a soak that blew its budget must never silently resume paying. On trip it:

  * refuses ALL new resource acquisition — LLM dispatch (composed into
    :func:`session_budget_authority.check_preflight`) AND GCE spin-up
    (composed into ``gcp_vm_manager._enforce_budget_gate``),
  * cancels active batch / realtime queues (``BatchFutureRegistry`` +
    the durable ``BatchLedger`` open claims), and
  * emits a loud, durable trip alarm.

At 80% of the worst threshold it emits a highly-visible warning ONCE (SSE
event log + durable Aegis spend-WAL row) before the hard trip.

Composition (mandate 3 — DRY: no parallel accumulators, no new tracking table)
-----------------------------------------------------------------------------
  * LLM spend   → :meth:`CostGovernor.session_total_cumulative_usd`
  * GCE spend   → ``sum(vm.uptime_hours * vm.cost_per_hour)`` over the
                  registered manager's ``managed_vms``
  * GCE runtime → ``sum(vm.uptime_hours * 3600)`` over ``managed_vms``
  * durable LLM → :func:`aegis.spend_wal.replay_wal` (restart baseline)
  * cancel      → :meth:`BatchFutureRegistry.cancel_all` + ``BatchLedger``
  * telemetry   → ``ide_observability_stream`` SSE + Aegis spend-WAL RECONCILE
  * latch       → this module's own sticky flag (authoritative for the gates)

Restart durability (mandate 4)
------------------------------
:meth:`reconcile_on_boot` rebuilds the cost baseline from the durable Aegis
spend WAL and the live GCE runtime from a GCP ``instances.list`` query (via the
registered manager's ``_sync_managed_vms_with_gcp``) BEFORE the loop resumes —
a soak that already spent $8 of a $10 cap resumes at 80% used, not 0%. If the
reconstructed state is already over budget, boot trips immediately.

Purity (mandate 2)
------------------
Every threshold / window / fraction is resolved from env at *read* time — a
frozen :class:`SoakBreakerConfig` snapshot per assessment. Master flag
``JARVIS_SOAK_CIRCUIT_BREAKER_ENABLED`` default FALSE → prod is byte-identical;
the soak ``.env`` arms it. This module NEVER raises: the gates it feeds raise
their own refusal types; every method here is fail-soft.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("Ouroboros.SoakCircuitBreaker")

SOAK_CIRCUIT_BREAKER_SCHEMA_VERSION: str = "soak_circuit_breaker.v1"

# ── Env knobs (mandate 2 — all adaptive, resolved at read time) ──────────────
ENV_ENABLED = "JARVIS_SOAK_CIRCUIT_BREAKER_ENABLED"
ENV_MAX_COST_USD = "JARVIS_SOAK_MAX_COST_USD"
ENV_MAX_GCE_RUNTIME_S = "JARVIS_SOAK_MAX_GCE_RUNTIME_S"
ENV_WARN_PCT = "JARVIS_SOAK_BUDGET_WARN_PCT"

_TRUE = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool) -> bool:
    try:
        raw = os.environ.get(name)
        if raw is None:
            return default
        return raw.strip().lower() in _TRUE
    except Exception:  # noqa: BLE001 — never raise on config read
        return default


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SoakBreakerConfig:
    """Immutable per-assessment snapshot of the env-resolved thresholds.

    ``max_*`` of ``0`` (or negative) DISABLES that trigger — a soak can
    gate on cost alone, runtime alone, or both. ``warn_pct`` is clamped to
    ``(0, 1]``; anything out of range falls back to the 0.8 default."""

    enabled: bool
    max_cost_usd: float
    max_gce_runtime_s: float
    warn_pct: float

    @classmethod
    def from_env(cls) -> "SoakBreakerConfig":
        warn = _env_float(ENV_WARN_PCT, 0.8)
        if not (0.0 < warn <= 1.0):
            warn = 0.8
        return cls(
            enabled=_env_bool(ENV_ENABLED, False),
            max_cost_usd=max(0.0, _env_float(ENV_MAX_COST_USD, 0.0)),
            max_gce_runtime_s=max(0.0, _env_float(ENV_MAX_GCE_RUNTIME_S, 0.0)),
            warn_pct=warn,
        )

    @property
    def cost_trigger_active(self) -> bool:
        return self.max_cost_usd > 0.0

    @property
    def runtime_trigger_active(self) -> bool:
        return self.max_gce_runtime_s > 0.0


@dataclass(frozen=True)
class SoakAssessment:
    """Result of one :meth:`SoakCircuitBreaker.assess` read."""

    cost_used_usd: float
    cost_cap_usd: float
    cost_pct: float
    runtime_used_s: float
    runtime_cap_s: float
    runtime_pct: float
    worst_pct: float
    at_warn: bool
    over_budget: bool

    def to_detail(self) -> Dict[str, Any]:
        return {
            "schema_version": SOAK_CIRCUIT_BREAKER_SCHEMA_VERSION,
            "cost_used_usd": round(self.cost_used_usd, 6),
            "cost_cap_usd": round(self.cost_cap_usd, 6),
            "cost_pct": round(self.cost_pct, 4),
            "runtime_used_s": round(self.runtime_used_s, 2),
            "runtime_cap_s": round(self.runtime_cap_s, 2),
            "runtime_pct": round(self.runtime_pct, 4),
            "worst_pct": round(self.worst_pct, 4),
            "at_warn": self.at_warn,
            "over_budget": self.over_budget,
        }


_OK_ASSESSMENT = SoakAssessment(
    cost_used_usd=0.0, cost_cap_usd=0.0, cost_pct=0.0,
    runtime_used_s=0.0, runtime_cap_s=0.0, runtime_pct=0.0,
    worst_pct=0.0, at_warn=False, over_budget=False,
)


class SoakCircuitBreaker:
    """Process-wide fail-closed soak budget + compute breaker.

    Thread-safe (a ``threading.Lock`` guards the latch + one-shot warn
    flag). Assessments are lock-free reads of already-thread-safe
    collaborators (CostGovernor / managed_vms), so the hot gate path
    adds negligible contention."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tripped = False
        self._trip_reason = ""
        self._tripped_at: Optional[float] = None
        self._warned = False
        # Durable LLM-spend baseline reconstructed at boot (Aegis WAL replay).
        self._boot_cost_baseline_usd = 0.0
        # Weakrefs — the breaker OBSERVES these surfaces, it never owns them.
        self._vm_manager_ref: Optional[Callable[[], Any]] = None
        self._batch_registry_ref: Optional[Callable[[], Any]] = None

    # ── Collaborator registration (weakref — no ownership) ───────────────

    def register_vm_manager(self, manager: Any) -> None:
        """Register the live ``GCPVMManager`` so the breaker can read
        ``managed_vms`` runtime/cost and drive a boot GCP sync. NEVER raises."""
        try:
            self._vm_manager_ref = weakref.ref(manager)
        except Exception:  # noqa: BLE001 — some objects aren't weakref-able
            self._vm_manager_ref = lambda m=manager: m  # strong fallback

    def register_batch_registry(self, registry: Any) -> None:
        """Register the live ``BatchFutureRegistry`` so a trip can cancel
        active batch queues. NEVER raises."""
        try:
            self._batch_registry_ref = weakref.ref(registry)
        except Exception:  # noqa: BLE001
            self._batch_registry_ref = lambda r=registry: r

    def _vm_manager(self) -> Optional[Any]:
        try:
            return self._vm_manager_ref() if self._vm_manager_ref else None
        except Exception:  # noqa: BLE001
            return None

    def _batch_registry(self) -> Optional[Any]:
        try:
            return self._batch_registry_ref() if self._batch_registry_ref else None
        except Exception:  # noqa: BLE001
            return None

    # ── Cost + runtime reads (DRY — compose existing surfaces) ───────────

    def _llm_spend_usd(self) -> float:
        """In-process governance-ledger LLM spend + durable boot baseline."""
        live = 0.0
        try:
            from backend.core.ouroboros.governance.cost_governor import (
                get_default_cost_governor,
            )
            gov = get_default_cost_governor()
            if gov is not None:
                live = float(gov.session_total_cumulative_usd())
        except Exception:  # noqa: BLE001
            live = 0.0
        return max(0.0, live) + max(0.0, self._boot_cost_baseline_usd)

    def _gce_cost_and_runtime(self) -> tuple:
        """(gce_cost_usd, gce_runtime_s) summed over live managed VMs.

        ``uptime_hours`` on each ``VMInstance`` is computed from its
        ``created_at`` — which the boot GCP sync sets to the node's REAL
        creation timestamp, so this read is the durable-across-restart
        runtime once :meth:`reconcile_on_boot` has run."""
        mgr = self._vm_manager()
        if mgr is None:
            return 0.0, 0.0
        cost = 0.0
        runtime_s = 0.0
        try:
            vms = list(getattr(mgr, "managed_vms", {}).values())
        except Exception:  # noqa: BLE001
            return 0.0, 0.0
        for vm in vms:
            try:
                uptime_h = float(getattr(vm, "uptime_hours", 0.0) or 0.0)
                if uptime_h <= 0.0:
                    continue
                rate = float(getattr(vm, "cost_per_hour", 0.0) or 0.0)
                cost += uptime_h * rate
                runtime_s += uptime_h * 3600.0
            except Exception:  # noqa: BLE001 — one bad VM never blinds the sum
                continue
        return max(0.0, cost), max(0.0, runtime_s)

    def assess(self, cfg: Optional[SoakBreakerConfig] = None) -> SoakAssessment:
        """Read current spend + GCE runtime and compute utilization vs. the
        env thresholds. Pure read — no side effects. NEVER raises."""
        if cfg is None:
            cfg = SoakBreakerConfig.from_env()
        try:
            gce_cost, gce_runtime_s = self._gce_cost_and_runtime()
            cost_used = self._llm_spend_usd() + gce_cost

            cost_pct = (
                cost_used / cfg.max_cost_usd
                if cfg.cost_trigger_active else 0.0
            )
            runtime_pct = (
                gce_runtime_s / cfg.max_gce_runtime_s
                if cfg.runtime_trigger_active else 0.0
            )
            worst = max(cost_pct, runtime_pct)
            over = (
                (cfg.cost_trigger_active and cost_pct >= 1.0)
                or (cfg.runtime_trigger_active and runtime_pct >= 1.0)
            )
            at_warn = worst >= cfg.warn_pct and (
                cfg.cost_trigger_active or cfg.runtime_trigger_active
            )
            return SoakAssessment(
                cost_used_usd=cost_used,
                cost_cap_usd=cfg.max_cost_usd,
                cost_pct=cost_pct,
                runtime_used_s=gce_runtime_s,
                runtime_cap_s=cfg.max_gce_runtime_s,
                runtime_pct=runtime_pct,
                worst_pct=worst,
                at_warn=at_warn,
                over_budget=over,
            )
        except Exception:  # noqa: BLE001 — assessment must never break a gate
            logger.debug("[SoakCB] assess degraded", exc_info=True)
            return _OK_ASSESSMENT

    # ── The lazy gate hook (called at every resource-acquisition point) ──

    def assess_and_maybe_trip(self) -> SoakAssessment:
        """Assess, emit the one-shot 80% warning, and TRIP if over budget.

        Lazy (no background task): called from ``check_preflight`` (every LLM
        dispatch) and ``_enforce_budget_gate`` (every VM op) and the GCE
        monitor loop — so the breaker advances exactly at the moments new
        cost/compute could be committed, with zero event-loop overhead.
        Returns the assessment. NEVER raises."""
        cfg = SoakBreakerConfig.from_env()
        if not cfg.enabled:
            return _OK_ASSESSMENT
        assessment = self.assess(cfg)
        try:
            if assessment.at_warn and not assessment.over_budget:
                fire = False
                with self._lock:
                    if not self._warned and not self._tripped:
                        self._warned = True
                        fire = True
                if fire:
                    self._emit_warning(assessment)
            if assessment.over_budget:
                reason = self._reason_for(cfg, assessment)
                self.trip(reason=reason, assessment=assessment)
        except Exception:  # noqa: BLE001
            logger.debug("[SoakCB] assess_and_maybe_trip degraded", exc_info=True)
        return assessment

    @staticmethod
    def _reason_for(cfg: SoakBreakerConfig, a: SoakAssessment) -> str:
        if cfg.cost_trigger_active and a.cost_pct >= 1.0:
            return (
                f"soak_cost_cap_exceeded:${a.cost_used_usd:.4f}"
                f">=${a.cost_cap_usd:.4f}"
            )
        if cfg.runtime_trigger_active and a.runtime_pct >= 1.0:
            return (
                f"soak_gce_runtime_cap_exceeded:{a.runtime_used_s:.0f}s"
                f">={a.runtime_cap_s:.0f}s"
            )
        return "soak_budget_exceeded"

    # ── Trip + refusal ───────────────────────────────────────────────────

    def is_tripped(self) -> bool:
        try:
            with self._lock:
                return self._tripped
        except Exception:  # noqa: BLE001
            return False

    def refusal_reason(self) -> Optional[str]:
        """Sync hook for the dispatch/VM gates. Advances the breaker
        (assess+maybe-trip) and returns a refusal reason string when the
        breaker is tripped, else None (fail-OPEN when disabled/healthy).
        NEVER raises."""
        try:
            self.assess_and_maybe_trip()
        except Exception:  # noqa: BLE001
            pass
        try:
            with self._lock:
                if self._tripped:
                    return (
                        self._trip_reason
                        or "soak_circuit_tripped"
                    )
                return None
        except Exception:  # noqa: BLE001
            return None

    def trip(
        self,
        *,
        reason: str,
        assessment: Optional[SoakAssessment] = None,
    ) -> bool:
        """Set the STICKY trip latch (idempotent), emit the durable+live
        trip alarm, and schedule cancellation of active batch/RT queues.
        Returns True if this call performed the transition, False if the
        breaker was already tripped. NEVER raises."""
        with self._lock:
            if self._tripped:
                return False
            self._tripped = True
            self._trip_reason = str(reason)[:200]
            self._tripped_at = time.time()
        logger.critical(
            "🛑 [SoakCB] CIRCUIT TRIPPED — %s. Refusing all new LLM dispatch + "
            "GCE spin-up; cancelling active batch/RT queues.", self._trip_reason,
        )
        try:
            self._emit_trip(assessment)
        except Exception:  # noqa: BLE001
            logger.debug("[SoakCB] trip telemetry degraded", exc_info=True)
        self._schedule_cancel()
        return True

    def _schedule_cancel(self) -> None:
        """Schedule the async queue cancel on the running loop; if there is
        no loop (sync context / tests), fall back to a best-effort sync
        cancel of the batch registry. NEVER raises."""
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            task = loop.create_task(self.cancel_active_queues())
            # Consume the result so a failed cancel never warns.
            task.add_done_callback(
                lambda t: None if t.cancelled() else t.exception()
            )
            return
        except RuntimeError:
            pass  # no running loop
        except Exception:  # noqa: BLE001
            pass
        # Sync fallback — cancel the in-registry futures directly.
        try:
            reg = self._batch_registry()
            if reg is not None and hasattr(reg, "cancel_all"):
                reg.cancel_all("soak_circuit_tripped")
        except Exception:  # noqa: BLE001
            logger.debug("[SoakCB] sync cancel fallback degraded", exc_info=True)

    async def cancel_active_queues(self) -> Dict[str, int]:
        """Cancel active batch/realtime queues on a trip. Reuses the
        ``BatchFutureRegistry`` cancel primitive and settles the durable
        ``BatchLedger`` open claims terminal (returning provider queue
        slots). In-flight realtime provider calls are not force-killed —
        they drain, and every SUBSEQUENT dispatch is refused by the latch.
        NEVER raises. Returns a small counters dict."""
        out = {"batch_futures_cancelled": 0, "claims_settled": 0}
        # 1. In-flight batch futures (the batch queue).
        try:
            reg = self._batch_registry()
            if reg is not None and hasattr(reg, "cancel_all"):
                out["batch_futures_cancelled"] = int(
                    reg.cancel_all("soak_circuit_tripped")
                )
        except Exception:  # noqa: BLE001
            logger.debug("[SoakCB] batch registry cancel degraded", exc_info=True)
        # 2. Durable open claims with no live future (settle terminal so the
        #    reconcile sweep + admission gate see them closed).
        try:
            from backend.core.ouroboros.governance.dw_batch_ledger import (
                STATE_TERMINAL, get_batch_ledger,
            )
            ledger = get_batch_ledger()
            for claim in ledger.open_claims():
                try:
                    ledger.settle(
                        claim.batch_id, STATE_TERMINAL,
                        reason="soak_circuit_tripped",
                    )
                    out["claims_settled"] += 1
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            logger.debug("[SoakCB] claim settle degraded", exc_info=True)
        logger.warning(
            "[SoakCB] queue cancel complete: batch_futures=%d claims=%d",
            out["batch_futures_cancelled"], out["claims_settled"],
        )
        return out

    # ── Boot reconciliation (mandate 4 — restart durability) ─────────────

    async def reconcile_on_boot(self) -> Dict[str, Any]:
        """Reconstruct spend + active-node runtime from the durable ledger +
        live GCP API BEFORE the loop resumes, then assess (trips immediately
        if the reconstructed state is already over budget).

        Spend baseline ← Aegis spend WAL replay (durable across restart).
        GCE runtime ← the registered manager's ``_sync_managed_vms_with_gcp``
        (live ``instances.list``; sets each VM's real ``created_at`` so
        ``uptime_hours`` reflects the true node age, not this process's age).

        Fail-soft: any missing collaborator degrades to a partial baseline.
        NEVER raises. Returns a summary dict for the boot log / tests."""
        summary: Dict[str, Any] = {
            "enabled": SoakBreakerConfig.from_env().enabled,
            "cost_baseline_usd": 0.0,
            "gce_synced": False,
            "tripped_on_boot": False,
        }
        # 1. Durable LLM-spend baseline from the Aegis spend WAL.
        try:
            from backend.core.ouroboros.aegis import flags as _aegis_flags
            from backend.core.ouroboros.aegis.spend_wal import replay_wal
            entries = replay_wal(_aegis_flags.wal_path())
            baseline = 0.0
            for e in entries:
                # Prefer settled actuals; fall back to reserved/estimated so
                # an un-reconciled in-flight lease still counts against us
                # (fail-CLOSED: never under-count spend at boot).
                val = e.actual_cost_usd
                if val is None:
                    val = e.reserve_cost_usd
                if val is None:
                    val = e.estimated_cost_usd
                if val:
                    baseline += float(val)
            self._boot_cost_baseline_usd = max(0.0, baseline)
            summary["cost_baseline_usd"] = self._boot_cost_baseline_usd
        except Exception:  # noqa: BLE001
            logger.debug("[SoakCB] boot spend replay degraded", exc_info=True)

        # 2. Live GCE runtime reconstruction via the manager's GCP sync.
        try:
            mgr = self._vm_manager()
            if mgr is not None and hasattr(mgr, "_sync_managed_vms_with_gcp"):
                await mgr._sync_managed_vms_with_gcp()
                summary["gce_synced"] = True
        except Exception:  # noqa: BLE001
            logger.debug("[SoakCB] boot GCP sync degraded", exc_info=True)

        # 3. Assess against the reconstructed baseline — trip if already over.
        try:
            cfg = SoakBreakerConfig.from_env()
            if cfg.enabled:
                a = self.assess(cfg)
                logger.info(
                    "[SoakCB] boot reconcile: cost=$%.4f/%.4f (%.0f%%) "
                    "gce_runtime=%.0fs/%.0fs (%.0f%%) baseline=$%.4f",
                    a.cost_used_usd, a.cost_cap_usd, a.cost_pct * 100.0,
                    a.runtime_used_s, a.runtime_cap_s, a.runtime_pct * 100.0,
                    self._boot_cost_baseline_usd,
                )
                if a.over_budget:
                    self.trip(
                        reason=self._reason_for(cfg, a) + ":on_boot",
                        assessment=a,
                    )
                    summary["tripped_on_boot"] = True
                elif a.at_warn:
                    with self._lock:
                        already = self._warned
                        if not already:
                            self._warned = True
                    if not already:
                        self._emit_warning(a)
        except Exception:  # noqa: BLE001
            logger.debug("[SoakCB] boot assess degraded", exc_info=True)
        return summary

    # ── Telemetry (mandate 2 — event log + durable DB) ───────────────────

    def _emit_warning(self, assessment: SoakAssessment) -> None:
        detail = assessment.to_detail()
        detail["event"] = "soak_budget_warning"
        logger.warning(
            "⚠️  [SoakCB] budget at %.0f%% of cap (cost $%.4f/%.4f, "
            "gce_runtime %.0f/%.0fs) — approaching the fail-closed trip.",
            assessment.worst_pct * 100.0,
            assessment.cost_used_usd, assessment.cost_cap_usd,
            assessment.runtime_used_s, assessment.runtime_cap_s,
        )
        self._publish("warning", detail)
        self._durable_append("soak_warn", detail)

    def _emit_trip(self, assessment: Optional[SoakAssessment]) -> None:
        detail = (assessment.to_detail() if assessment is not None else {})
        detail["event"] = "soak_circuit_tripped"
        detail["reason"] = self._trip_reason
        detail["tripped_at"] = self._tripped_at
        self._publish("tripped", detail)
        self._durable_append("soak_trip", detail)

    @staticmethod
    def _publish(kind: str, detail: Dict[str, Any]) -> None:
        """Live event-log side — the SSE broker. Best-effort."""
        try:
            from backend.core.ouroboros.governance import (
                ide_observability_stream as S,
            )
            if kind == "warning":
                S.publish_soak_budget_warning(detail)
            else:
                S.publish_soak_circuit_tripped(detail)
        except Exception:  # noqa: BLE001
            logger.debug("[SoakCB] SSE publish degraded", exc_info=True)

    @staticmethod
    def _durable_append(tag: str, detail: Dict[str, Any]) -> None:
        """Durable-DB side — a RECONCILE row on the sealed Aegis spend WAL
        (mandate 3: integrate into the EXISTING logging schema, no new
        table). The row carries the assessment in ``detail`` and the current
        spend in ``actual_cost_usd`` so a post-hoc audit reconstructs the
        trip from the same ledger crash recovery already replays."""
        try:
            from backend.core.ouroboros.aegis import flags as _aegis_flags
            from backend.core.ouroboros.aegis.spend_wal import (
                SpendEntry, SpendEntryKind, append_entry_sync,
            )
            entry = SpendEntry(
                kind=SpendEntryKind.RECONCILE,
                ts=time.time(),
                op_id=f"soak_circuit_breaker:{tag}",
                route="soak_circuit_breaker",
                actual_cost_usd=float(detail.get("cost_used_usd", 0.0) or 0.0),
                detail=str(detail)[:500],
            )
            append_entry_sync(_aegis_flags.wal_path(), entry)
        except Exception:  # noqa: BLE001
            logger.debug("[SoakCB] durable WAL append degraded", exc_info=True)

    # ── Test / observability accessors ───────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        cfg = SoakBreakerConfig.from_env()
        a = self.assess(cfg)
        with self._lock:
            return {
                "enabled": cfg.enabled,
                "tripped": self._tripped,
                "trip_reason": self._trip_reason,
                "tripped_at": self._tripped_at,
                "warned": self._warned,
                "boot_cost_baseline_usd": self._boot_cost_baseline_usd,
                "assessment": a.to_detail(),
                "config": {
                    "max_cost_usd": cfg.max_cost_usd,
                    "max_gce_runtime_s": cfg.max_gce_runtime_s,
                    "warn_pct": cfg.warn_pct,
                },
            }


# ─────────────────────────────────────────────────────────────────────────────
# Process-wide singleton (mirrors cost_governor / session_budget_authority)
# ─────────────────────────────────────────────────────────────────────────────

_default_breaker: Optional[SoakCircuitBreaker] = None
_singleton_lock = threading.Lock()


def get_soak_breaker() -> SoakCircuitBreaker:
    """Return the process-wide breaker, creating it on first use. NEVER raises."""
    global _default_breaker
    with _singleton_lock:
        if _default_breaker is None:
            _default_breaker = SoakCircuitBreaker()
        return _default_breaker


def set_soak_breaker(breaker: Optional[SoakCircuitBreaker]) -> None:
    """Last-write-wins registration (test / custom-wiring). NEVER raises."""
    global _default_breaker
    with _singleton_lock:
        _default_breaker = breaker


def reset_for_tests() -> None:
    """Drop the singleton so the next accessor builds a fresh breaker."""
    global _default_breaker
    with _singleton_lock:
        _default_breaker = None


def soak_breaker_enabled() -> bool:
    """True iff the master flag arms the breaker. NEVER raises."""
    return SoakBreakerConfig.from_env().enabled


def soak_dispatch_refusal_reason() -> Optional[str]:
    """The always-on gate hook for the LLM-dispatch + GCE-spin chokepoints.

    Returns a refusal-reason string when the breaker is armed AND tripped
    (or trips on this very assessment), else ``None`` — so a caller does:

        reason = soak_dispatch_refusal_reason()
        if reason is not None:
            <refuse with `reason`>

    Fail-OPEN (returns ``None``) when the master flag is off or on any
    fault, preserving byte-identical behavior in unarmed environments.
    NEVER raises."""
    try:
        if not soak_breaker_enabled():
            return None
        return get_soak_breaker().refusal_reason()
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "ENV_ENABLED",
    "ENV_MAX_COST_USD",
    "ENV_MAX_GCE_RUNTIME_S",
    "ENV_WARN_PCT",
    "SOAK_CIRCUIT_BREAKER_SCHEMA_VERSION",
    "SoakAssessment",
    "SoakBreakerConfig",
    "SoakCircuitBreaker",
    "get_soak_breaker",
    "reset_for_tests",
    "set_soak_breaker",
    "soak_breaker_enabled",
    "soak_dispatch_refusal_reason",
]
