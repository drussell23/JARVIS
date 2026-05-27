"""Slice 25 — Autonomous Pre-Flight Health & Entitlement Sentinel.

Closes the observability gap surfaced by v18 (bt-2026-05-26-233010):
when the upstream DW tier undergoes a network blackout or per-model
entitlement failure, the dispatch engine blindly churns wall-clock
time burning through every model in the fleet before finally
exhausting. The operator-binding contract: *we do not let external
dependency failures compromise system predictability*.

# Composition discipline (operator-mandated)

This module ORCHESTRATES existing substrate — it does NOT duplicate
any probe / classifier / ledger / sentinel logic:

* **HeavyProber** (``dw_heavy_probe.py``) — already-built async probe
  primitive with budget enforcement + adaptive TTFT + defensive
  error capture. We invoke ``HeavyProber.probe(model_id, ...)`` per
  trusted model in parallel (bounded by ``asyncio.gather``).
* **dw_entitlement_classifier** (``classify_4xx``) — pure-function
  classifier already distinguishes ``ENTITLEMENT_BLOCKED`` (per-model
  routing-rule rejection — permanent) from ``AUTH_FAILURE`` (global
  cred problem — transient) from ``OTHER_4XX``. We pass the probe's
  error body through this classifier verbatim.
* **PromotionLedger.demote(origin=QUARANTINE_ACCOUNT_NOT_ENTITLED)** —
  in-memory + on-disk demotion via the EXISTING demote API. The new
  origin constant (added in dw_promotion_ledger.py) is the only
  delta on the ledger side.
* **TopologySentinel.report_failure** — invoked for 5xx / timeout
  outcomes via the EXISTING signature; trips CLOSED→OPEN naturally
  via the weighted-streak threshold. Slice 24's structural fields
  (status_code / response_body / is_terminal) carry through.

# Injection / testability

The public ``run_preflight()`` takes an injectable ``probe_fn`` so
tests can mock outcomes WITHOUT requiring aiohttp + DW credentials.
Production wiring binds ``HeavyProber.probe`` (via the bound-session
factory at the call site). This decoupling lets the test surface
exercise every branch deterministically and keeps the module's
import surface acyclic.

# Closed outcome taxonomy

``PreflightVerdict`` is closed at 5 values:
* ``ACTIVE`` — probe succeeded with at least one token
* ``DEMOTED_ENTITLEMENT`` — 4xx + classifier returned ENTITLEMENT_BLOCKED
* ``DEGRADED_5XX`` — 5xx or transport error; sentinel reported_failure
* ``DEGRADED_TIMEOUT`` — probe timeout; sentinel reported_failure
* ``ERROR_OTHER`` — unclassified failure (probe substrate raised
  unexpectedly); recorded but no eviction (defensive — caller can
  still attempt the model)

# Fail-fast boundary

If ALL probed models end in non-ACTIVE verdicts AND ``halt_on_all_fail``
is True (default), ``run_preflight`` raises
``PreflightAllFailedError`` with a structured diagnostic. The caller
(harness boot OR candidate_generator first-activation) catches and
emits a clean shutdown rather than entering a deterministic
exhaustion loop. When ``halt_on_all_fail=False`` the function
returns the report for caller-side branching.

# Master flag

``JARVIS_PREFLIGHT_PROBE_ENABLED`` — default-FALSE pending v19
validation. Once a v19 detonation proves the probe yields actionable
demotions without false-positives, graduate to default-TRUE per
Slice 23's structural-condition pattern (auto-on when Claude
disabled + multi-model fleet present).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Awaitable, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Constants + closed taxonomy
# ──────────────────────────────────────────────────────────────────────


_ENV_MASTER = "JARVIS_PREFLIGHT_PROBE_ENABLED"
_ENV_TIMEOUT_PER_MODEL_S = "JARVIS_PREFLIGHT_TIMEOUT_PER_MODEL_S"
_ENV_TEST_PROMPT = "JARVIS_PREFLIGHT_TEST_PROMPT"
_ENV_HALT_ON_ALL_FAIL = "JARVIS_PREFLIGHT_HALT_ON_ALL_FAIL"

_DEFAULT_TIMEOUT_PER_MODEL_S = 10.0
_DEFAULT_TEST_PROMPT = "ping"
_DEFAULT_HALT_ON_ALL_FAIL = True


class PreflightVerdict(str, Enum):
    """Closed taxonomy — each probed model lands in exactly one bucket."""

    ACTIVE = "active"
    DEMOTED_ENTITLEMENT = "demoted_entitlement"
    DEGRADED_5XX = "degraded_5xx"
    DEGRADED_TIMEOUT = "degraded_timeout"
    ERROR_OTHER = "error_other"


@dataclass(frozen=True)
class ProbeOutcome:
    """Test-injectable contract — what ``probe_fn(model_id)`` returns.

    Production binds this to ``HeavyProbeResult`` via a thin adapter
    that extracts ``success`` / ``status_code`` / ``error_body``
    / ``latency_ms`` from the heavy-probe result.

    Tests construct directly.

    ``status_code`` semantics (matches DoublewordInfraError convention):
      * 200 — success (with optional tokens received)
      * 4xx — body fed to dw_entitlement_classifier for kind verdict
      * 5xx — degraded; sentinel report_failure(LIVE_HTTP_5XX)
      * 0   — non-HTTP (timeout/DNS/TLS); degraded_timeout if exception
              indicates timeout, else degraded_5xx
    """

    model_id: str
    success: bool
    status_code: int = 0
    error_body: str = ""
    latency_ms: int = 0
    timeout: bool = False
    error_message: str = ""


@dataclass(frozen=True)
class ModelPreflightResult:
    """Per-model outcome of the preflight probe. Frozen for audit."""

    model_id: str
    verdict: PreflightVerdict
    status_code: int = 0
    latency_ms: int = 0
    entitlement_marker: str = ""
    diagnostic: str = ""


@dataclass(frozen=True)
class PreflightReport:
    """Aggregate outcome across the trusted fleet."""

    started_at_unix: float
    finished_at_unix: float
    results: Tuple[ModelPreflightResult, ...]
    active_count: int
    demoted_entitlement_count: int
    degraded_5xx_count: int
    degraded_timeout_count: int
    error_other_count: int

    @property
    def all_failed(self) -> bool:
        return self.active_count == 0 and len(self.results) > 0

    @property
    def total_probed(self) -> int:
        return len(self.results)

    def summary_line(self) -> str:
        return (
            f"active={self.active_count} "
            f"demoted_entitlement={self.demoted_entitlement_count} "
            f"degraded_5xx={self.degraded_5xx_count} "
            f"degraded_timeout={self.degraded_timeout_count} "
            f"error_other={self.error_other_count} "
            f"total={self.total_probed} "
            f"duration_s={self.finished_at_unix - self.started_at_unix:.2f}"
        )


class PreflightAllFailedError(RuntimeError):
    """Raised when every probed model failed AND halt_on_all_fail=True.

    Carries the structured report so the caller (harness boot) can
    dump it before exiting cleanly. NEVER raised when at least one
    model probed ACTIVE — partial success is acceptable.
    """

    def __init__(self, report: PreflightReport) -> None:
        self.report = report
        super().__init__(
            f"preflight all models failed — {report.summary_line()}"
        )


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────


def _envb(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _envf(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _envs(name: str, default: str) -> str:
    raw = os.environ.get(name, "").strip()
    return raw if raw else default


def is_preflight_enabled() -> bool:
    """Master flag — read at every call so toggles take effect immediately."""
    return _envb(_ENV_MASTER, default=False)


def _classify_outcome(outcome: ProbeOutcome) -> Tuple[
    PreflightVerdict, str, str,
]:
    """Pure-function classifier — maps ProbeOutcome → (verdict, marker, diag).

    Composes ``dw_entitlement_classifier.classify_4xx`` for the 4xx
    branch. No I/O, no state, deterministic.
    """
    if outcome.success:
        return PreflightVerdict.ACTIVE, "", ""

    if outcome.timeout:
        return (
            PreflightVerdict.DEGRADED_TIMEOUT,
            "",
            f"timeout after {outcome.latency_ms}ms",
        )

    # 4xx → run through entitlement classifier
    if 400 <= outcome.status_code < 500:
        try:
            from backend.core.ouroboros.governance.dw_entitlement_classifier import (
                classify_4xx,
                KIND_ENTITLEMENT_BLOCKED,
            )
            result = classify_4xx(outcome.status_code, outcome.error_body)
            if result.kind == KIND_ENTITLEMENT_BLOCKED:
                return (
                    PreflightVerdict.DEMOTED_ENTITLEMENT,
                    result.matched_marker,
                    f"http_{outcome.status_code} entitlement_blocked "
                    f"marker={result.matched_marker!r}",
                )
            # AUTH_FAILURE / OTHER_4XX — treat as degraded (not entitlement)
            return (
                PreflightVerdict.DEGRADED_5XX,
                "",
                f"http_{outcome.status_code} kind={result.kind}",
            )
        except Exception as exc:  # noqa: BLE001 — defensive
            return (
                PreflightVerdict.ERROR_OTHER,
                "",
                f"classifier_raised:{type(exc).__name__}:{str(exc)[:120]}",
            )

    # 5xx → degraded
    if 500 <= outcome.status_code < 600:
        return (
            PreflightVerdict.DEGRADED_5XX,
            "",
            f"http_{outcome.status_code} {outcome.error_message[:120]}",
        )

    # 0 status_code with no timeout flag = transport-level non-timeout
    # (DNS/TLS/connection-refused). Classify as 5xx-style degradation
    # so the sentinel breaker sees the failure pressure.
    if outcome.status_code == 0:
        return (
            PreflightVerdict.DEGRADED_5XX,
            "",
            f"transport_error: {outcome.error_message[:120]}",
        )

    # Other status codes (1xx/2xx-non-success/3xx) — unexpected
    return (
        PreflightVerdict.ERROR_OTHER,
        "",
        f"unexpected_status={outcome.status_code} "
        f"msg={outcome.error_message[:120]}",
    )


# ──────────────────────────────────────────────────────────────────────
# Public entry point
# ──────────────────────────────────────────────────────────────────────


async def run_preflight(
    *,
    model_ids: Tuple[str, ...],
    probe_fn: Callable[[str], Awaitable[ProbeOutcome]],
    ledger: Optional["object"] = None,
    sentinel: Optional["object"] = None,
    timeout_per_model_s: Optional[float] = None,
    halt_on_all_fail: Optional[bool] = None,
) -> PreflightReport:
    """Probe each model in ``model_ids`` concurrently; route outcomes.

    For each model:

      * **ACTIVE** → no side effect (model is healthy)
      * **DEMOTED_ENTITLEMENT** → ``ledger.demote(model_id,
        origin=QUARANTINE_ACCOUNT_NOT_ENTITLED)`` if ledger provided
      * **DEGRADED_5XX** / **DEGRADED_TIMEOUT** →
        ``sentinel.report_failure(model_id, LIVE_HTTP_5XX/LIVE_TRANSPORT,
        status_code=..., response_body=..., is_terminal=False)`` if
        sentinel provided
      * **ERROR_OTHER** → recorded, no eviction (defensive)

    Concurrency: probes fire via ``asyncio.gather`` with per-probe
    ``asyncio.wait_for`` enforcing ``timeout_per_model_s`` (default 10s).
    Worst-case wall is ``timeout_per_model_s`` regardless of fleet size.

    Returns PreflightReport. Raises PreflightAllFailedError when every
    probe failed AND halt_on_all_fail is True (default).

    Callers MUST pass a ``probe_fn`` callable — tests inject a stub,
    production binds ``HeavyProber.probe`` adapted to ProbeOutcome.
    """
    if not model_ids:
        # Empty fleet — caller should not have called us, but be defensive
        empty = PreflightReport(
            started_at_unix=time.time(),
            finished_at_unix=time.time(),
            results=(),
            active_count=0,
            demoted_entitlement_count=0,
            degraded_5xx_count=0,
            degraded_timeout_count=0,
            error_other_count=0,
        )
        return empty

    effective_timeout = (
        timeout_per_model_s
        if timeout_per_model_s is not None
        else _envf(_ENV_TIMEOUT_PER_MODEL_S, _DEFAULT_TIMEOUT_PER_MODEL_S)
    )
    effective_halt = (
        halt_on_all_fail
        if halt_on_all_fail is not None
        else _envb(_ENV_HALT_ON_ALL_FAIL, _DEFAULT_HALT_ON_ALL_FAIL)
    )

    started = time.time()
    logger.info(
        "[Preflight] starting probes: models=%d timeout=%.1fs halt_on_all_fail=%s",
        len(model_ids), effective_timeout, effective_halt,
    )

    # Bounded per-probe timeout wrapper
    async def _probe_with_timeout(mid: str) -> ProbeOutcome:
        try:
            return await asyncio.wait_for(
                probe_fn(mid), timeout=effective_timeout,
            )
        except asyncio.TimeoutError:
            return ProbeOutcome(
                model_id=mid,
                success=False,
                status_code=0,
                latency_ms=int(effective_timeout * 1000),
                timeout=True,
                error_message=f"asyncio.wait_for hit {effective_timeout}s",
            )
        except Exception as exc:  # noqa: BLE001 — probe MUST NOT raise
            return ProbeOutcome(
                model_id=mid,
                success=False,
                status_code=0,
                error_message=f"probe_raised:{type(exc).__name__}:{str(exc)[:120]}",
            )

    outcomes = await asyncio.gather(
        *[_probe_with_timeout(m) for m in model_ids],
        return_exceptions=False,
    )

    # Classify + route side-effects
    results: List[ModelPreflightResult] = []
    active = 0
    dem_ent = 0
    deg_5xx = 0
    deg_timeout = 0
    err_other = 0

    for outcome in outcomes:
        verdict, marker, diag = _classify_outcome(outcome)
        results.append(ModelPreflightResult(
            model_id=outcome.model_id,
            verdict=verdict,
            status_code=outcome.status_code,
            latency_ms=outcome.latency_ms,
            entitlement_marker=marker,
            diagnostic=diag,
        ))

        if verdict is PreflightVerdict.ACTIVE:
            active += 1
            logger.info(
                "[Preflight] model=%s ACTIVE latency=%dms",
                outcome.model_id, outcome.latency_ms,
            )
            continue

        if verdict is PreflightVerdict.DEMOTED_ENTITLEMENT:
            dem_ent += 1
            logger.warning(
                "[Preflight] model=%s ENTITLEMENT_BLOCKED — evicting from "
                "PromotionLedger with origin=account_not_entitled "
                "(marker=%r)",
                outcome.model_id, marker,
            )
            _demote_in_ledger(ledger, outcome.model_id)
            continue

        if verdict is PreflightVerdict.DEGRADED_5XX:
            deg_5xx += 1
            logger.warning(
                "[Preflight] model=%s DEGRADED_5XX status=%d — sentinel "
                "report_failure (diag=%s)",
                outcome.model_id, outcome.status_code, diag,
            )
            _report_to_sentinel(
                sentinel, outcome, source="live_http_5xx", is_terminal=False,
            )
            continue

        if verdict is PreflightVerdict.DEGRADED_TIMEOUT:
            deg_timeout += 1
            logger.warning(
                "[Preflight] model=%s DEGRADED_TIMEOUT latency=%dms — "
                "sentinel report_failure",
                outcome.model_id, outcome.latency_ms,
            )
            _report_to_sentinel(
                sentinel, outcome, source="live_transport", is_terminal=False,
            )
            continue

        # ERROR_OTHER — recorded only, no eviction
        err_other += 1
        logger.warning(
            "[Preflight] model=%s ERROR_OTHER (diag=%s) — "
            "no eviction (defensive)",
            outcome.model_id, diag,
        )

    finished = time.time()
    report = PreflightReport(
        started_at_unix=started,
        finished_at_unix=finished,
        results=tuple(results),
        active_count=active,
        demoted_entitlement_count=dem_ent,
        degraded_5xx_count=deg_5xx,
        degraded_timeout_count=deg_timeout,
        error_other_count=err_other,
    )

    logger.info("[Preflight] complete: %s", report.summary_line())

    if report.all_failed and effective_halt:
        logger.error(
            "[Preflight] FAIL-FAST — every probed model failed; halting "
            "initialization. report=%s",
            report.summary_line(),
        )
        raise PreflightAllFailedError(report)

    return report


# ──────────────────────────────────────────────────────────────────────
# Side-effect helpers (kept private — never raise into caller)
# ──────────────────────────────────────────────────────────────────────


def _demote_in_ledger(ledger: Optional[object], model_id: str) -> None:
    """Best-effort demote with QUARANTINE_ACCOUNT_NOT_ENTITLED origin.

    Lazy import keeps this module's substrate independent of the
    ledger module (so a circular-import collapse can't take down
    preflight). NEVER raises into the caller — eviction is the
    enhancement; correctness of the report is the primary contract.
    """
    if ledger is None:
        return
    try:
        from backend.core.ouroboros.governance.dw_promotion_ledger import (
            QUARANTINE_ACCOUNT_NOT_ENTITLED,
        )
        ledger.demote(model_id, origin=QUARANTINE_ACCOUNT_NOT_ENTITLED)
        # ledger.demote() already writes to disk via _ensure_loaded /
        # save semantics; no additional persistence call needed.
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[Preflight] ledger demote failed for %s: %r",
            model_id, exc,
        )


def _report_to_sentinel(
    sentinel: Optional[object],
    outcome: ProbeOutcome,
    *,
    source: str,
    is_terminal: bool,
) -> None:
    """Best-effort sentinel report_failure call. Slice 24's structural
    fields carry through (status_code / response_body / is_terminal).
    """
    if sentinel is None:
        return
    try:
        from backend.core.ouroboros.governance.topology_sentinel import (
            FailureSource,
        )
        # Map source string to enum
        fs = (
            FailureSource.LIVE_TRANSPORT
            if source == "live_transport"
            else FailureSource.LIVE_HTTP_5XX
        )
        sentinel.report_failure(
            outcome.model_id,
            fs,
            detail=f"preflight:{outcome.error_message[:200]}",
            status_code=(
                outcome.status_code if outcome.status_code > 0 else None
            ),
            response_body=outcome.error_body[:512],
            is_terminal=is_terminal,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[Preflight] sentinel report_failure failed for %s: %r",
            outcome.model_id, exc,
        )
