"""Pre-call session-budget hard authority (PRD §session-budget-preflight).

Closes the load-bearing gap surfaced by Step-1 verification soak
(2026-05-21 ``bt-2026-05-21-010600``): a single Claude call at
``route=immediate complexity=simple`` consumed ``$0.1281`` against a
``$0.10`` session cap because ``CostGovernor._derive_cap`` permits
per-op caps up to ``baseline_usd × route_factor × complexity_factor
× retry_headroom × readonly_factor`` ($1.20 in that example) — far
in excess of the session cap. ``CostTracker.record()`` fires its
``budget_event`` *after* the spend lands, which is too late to
prevent the overage.

This module is the hard wallet gate. Composes:

  * ``battle_test.cost_tracker.CostTracker`` via a duck-typed
    protocol (any object with a ``.remaining`` numeric property).
    The harness registers its tracker at construction; governance
    never imports ``battle_test``.

  * Env fallback chain when no provider is registered:
        Tier 1: ``JARVIS_S2_SESSION_BUDGET_USD`` (S2 wiring chain)
        Tier 2: ``OUROBOROS_BATTLE_COST_CAP`` (battle harness env)
        Tier 3: ``None`` — no authority active; preflight fail-OPEN

S2 remains advisory (operator constraint). This module is the hard
gate. S2 may inform forecasts but spend authority belongs here.

Architectural invariants:

  * No parallel cost ledger.
  * No hardcoded model prices.
  * No import of ``battle_test`` from this module (cycle-free).
  * Fail-CLOSED when authority is active; fail-OPEN when not (so
    environments without a session cap retain byte-identical
    pre-PR behavior).
  * NEVER raises any exception class OTHER than
    :class:`SessionBudgetPreflightRefused`.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional, Protocol, runtime_checkable

logger = logging.getLogger("Ouroboros.SessionBudgetAuthority")

SESSION_BUDGET_AUTHORITY_SCHEMA_VERSION: str = (
    "session_budget_authority.v1"
)

# Env fallback chain — reuses S2 + battle harness env knobs already
# in place. No new env knob for the budget value itself; this module
# only introduces the master clamp knob (consumed by CostGovernor).
_ENV_S2_SESSION_BUDGET = "JARVIS_S2_SESSION_BUDGET_USD"
_ENV_BATTLE_COST_CAP = "OUROBOROS_BATTLE_COST_CAP"


@runtime_checkable
class _SessionBudgetProvider(Protocol):
    """Duck-typed protocol: any object with a numeric ``.remaining``
    property satisfies it. ``CostTracker`` does today (verified at
    :file:`backend/core/ouroboros/battle_test/cost_tracker.py`).
    NO governance → battle_test import dependency."""

    @property
    def remaining(self) -> float: ...


# ---------------------------------------------------------------------------
# Structured refusal exception (orchestrator-introspectable)
# ---------------------------------------------------------------------------


class SessionBudgetPreflightRefused(Exception):
    """Raised by provider preflight when the estimated/authorized
    cost of a pending call exceeds remaining session budget.

    Carries structured fields so the orchestrator's cascade machinery
    can distinguish a hard-wallet refusal from a model/transport
    failure:

      * ``provider`` — short provider name (``"claude"`` / ``"doubleword"``)
      * ``estimated_cost_usd`` — the upper bound used by preflight
        (typically the caller's ``_max_cost_per_op``)
      * ``session_remaining_usd`` — authoritative remaining budget
        at the time of refusal
      * ``reason`` — short opaque token (``"session_budget_preflight_refused"``
        by default); orchestrator log filters / postmortem classifiers
        match on this verbatim
    """

    def __init__(
        self,
        *,
        provider: str,
        estimated_cost_usd: float,
        session_remaining_usd: float,
        reason: str = "session_budget_preflight_refused",
    ) -> None:
        super().__init__(
            f"{reason}: provider={provider} "
            f"est=${float(estimated_cost_usd):.4f} > "
            f"session_remaining=${float(session_remaining_usd):.4f}"
        )
        self.provider = str(provider)
        self.estimated_cost_usd = float(estimated_cost_usd)
        self.session_remaining_usd = float(session_remaining_usd)
        self.reason = str(reason)


# ---------------------------------------------------------------------------
# Process-wide registration (mirrors cost_governor singleton pattern)
# ---------------------------------------------------------------------------


_default_provider: Optional[_SessionBudgetProvider] = None
_lock = threading.Lock()


def set_session_budget_provider(
    provider: Optional[_SessionBudgetProvider],
) -> None:
    """Register the authoritative session-budget provider. Idempotent
    and last-write-wins (matches ``cost_governor`` singleton pattern).
    Passing ``None`` clears the registration (test helper / shutdown).

    Called from the battle harness immediately after the harness's
    ``CostTracker`` is constructed. Governance does NOT call this.

    NEVER raises."""
    global _default_provider
    try:
        with _lock:
            _default_provider = provider
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[SBA] set_session_budget_provider degraded: %s", exc,
        )


def get_session_budget_provider() -> Optional[_SessionBudgetProvider]:
    """Return the registered authoritative provider, or ``None`` when
    no provider has been registered in this process. NEVER raises."""
    try:
        with _lock:
            return _default_provider
    except Exception:  # noqa: BLE001 — defensive
        return None


def reset_for_tests() -> None:
    """Test helper — drops the registered provider. NEVER raises."""
    global _default_provider
    try:
        with _lock:
            _default_provider = None
    except Exception:  # noqa: BLE001 — defensive
        pass


# ---------------------------------------------------------------------------
# Authoritative remaining-budget read
# ---------------------------------------------------------------------------


def get_session_total_cap_usd() -> Optional[float]:
    """Slice 12Y Part 1 — return the total session budget cap
    (NOT the remaining). Composed as ``total_spent + remaining``
    from the registered provider so the value reflects the
    operator's original session budget irrespective of current
    spend.

    Returns ``None`` when no provider is registered (fail-OPEN).
    NEVER raises.

    Used by the background-spend ceiling: foreground ops always
    get a reserved runway of ``total_cap * (1 - limit_pct)``
    regardless of how much background ops have already burned.
    """
    try:
        provider = get_session_budget_provider()
        if provider is None:
            return None
        try:
            spent = float(getattr(provider, "total_spent", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            spent = 0.0
        try:
            remaining = float(getattr(provider, "remaining", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            remaining = 0.0
        return max(0.0, spent + remaining)
    except Exception:  # noqa: BLE001
        return None


# ── Slice 12Y Part 1 — Background Spend Limit ──
#
# Default fraction of total session cap that BACKGROUND-tier ops
# (TodoScanner, OpportunityMiner, DocStaleness, AI Miner, etc.,
# per urgency_router._BACKGROUND_SOURCES + _SPECULATIVE_SOURCES)
# may consume cumulatively. The complement (1 - limit_pct) is the
# RESERVED RUNWAY for foreground ops (complex / immediate /
# benchmark-source ops like SWE-Bench-Pro fixtures).
#
# bt-2026-05-23-211212 showed the fixture op refused at
# session_budget_preflight_refused: claude_est=$0.5000 >
# session_remaining=$0.4863 — sensor ops had consumed $0.514 of
# the $1.00 cap concurrently, leaving the fixture in a starvation
# window. With default 0.5 + cap=$1.00, sensors are capped at
# $0.50 cumulatively; the foreground fixture always sees
# remaining >= $0.50.
#
# Default 0.5 (sensors can use up to 50% of cap). Bounded to
# [0.0, 1.0] at read time; out-of-range falls back to default.
# Set to 1.0 to disable the reservation (pre-Slice-12Y behavior).
BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR: str = (
    "JARVIS_BACKGROUND_SPEND_LIMIT_PCT"
)


def get_background_spend_limit_pct() -> float:
    """Resolve the background-spend-limit fraction from env.
    Default 0.5. Bounded to [0.0, 1.0]. NEVER raises."""
    try:
        raw = os.environ.get(
            BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR, "",
        ).strip()
        v = float(raw) if raw else 0.5
        return max(0.0, min(1.0, v))
    except (TypeError, ValueError):
        return 0.5


# Closed set of signal_source values that are considered
# "background-tier" for the spend-ceiling check. Mirrors
# urgency_router._BACKGROUND_SOURCES + _SPECULATIVE_SOURCES (those
# are the cost-tier taxonomies). Duplicated here so the SBA never
# imports from urgency_router (avoid a layering loop — SBA is a
# lower-level primitive). The Slice 12Y test surface AST-pins
# these to stay in sync with urgency_router.
_BACKGROUND_TIER_SIGNAL_SOURCES: frozenset = frozenset({
    # _BACKGROUND_SOURCES mirror.
    "ai_miner",
    "exploration",
    "backlog",
    "architecture",
    "todo_scanner",
    "doc_staleness",
    # _SPECULATIVE_SOURCES mirror.
    "intent_discovery",
})


def is_background_tier_source(signal_source: Optional[str]) -> bool:
    """Return True iff the signal_source identifies a
    BACKGROUND or SPECULATIVE tier op (per the mirrored
    urgency_router taxonomy). NEVER raises."""
    if not isinstance(signal_source, str) or not signal_source:
        return False
    return signal_source.strip().lower() in _BACKGROUND_TIER_SIGNAL_SOURCES


def get_session_remaining_usd() -> Optional[float]:
    """Authoritative source of remaining session budget USD.

    Precedence (per design lock):

      1. Registered provider's ``.remaining`` (e.g. battle harness's
         ``CostTracker``).
      2. ``JARVIS_S2_SESSION_BUDGET_USD`` (S2 wiring chain Tier 1).
      3. ``OUROBOROS_BATTLE_COST_CAP`` (battle harness env).
      4. ``None`` — no authority active; downstream preflight
         fail-OPEN (preserves byte-identical pre-PR behavior in
         environments without a session cap).

    Returned value is non-negative. NEVER raises."""
    try:
        provider = get_session_budget_provider()
        if provider is not None:
            try:
                value = float(provider.remaining)
                return max(0.0, value)
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[SBA] provider.remaining read fault: %s", exc,
                )
        # Env fallback (no registered provider — tests, CLI-less paths,
        # operator-driven manual invocations).
        for env_name in (_ENV_S2_SESSION_BUDGET, _ENV_BATTLE_COST_CAP):
            try:
                raw = os.environ.get(env_name, "").strip()
            except Exception:  # noqa: BLE001
                continue
            if not raw:
                continue
            try:
                return max(0.0, float(raw))
            except (TypeError, ValueError):
                continue
        return None
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[SBA] get_session_remaining_usd fault: %s", exc,
        )
        return None


# ---------------------------------------------------------------------------
# Hard preflight gate (call BEFORE any provider dispatch)
# ---------------------------------------------------------------------------


def check_preflight(
    *,
    provider_name: str,
    estimated_cost_usd: float,
    signal_source: Optional[str] = None,
) -> None:
    """Hard wallet gate. Call BEFORE provider dispatch.

    Refuses (raises :class:`SessionBudgetPreflightRefused`) when the
    remaining session budget cannot accommodate ``estimated_cost_usd``.
    The caller's ``_max_cost_per_op`` is a reasonable conservative
    upper bound when no finer estimate is available.

    Slice 12Y Part 1 — Background Spend Ceiling
    --------------------------------------------
    When ``signal_source`` identifies a BACKGROUND or SPECULATIVE
    tier op (per :func:`is_background_tier_source`), an ADDITIONAL
    check applies: the op is refused if accepting it would push
    cumulative session spend above the reserved-foreground
    threshold:

        foreground_reserve_usd = total_cap * (1 - limit_pct)
        background_max_remaining = max(0, remaining - foreground_reserve_usd)
        refuse if est > background_max_remaining

    With default ``limit_pct=0.5`` and ``cap=$1.00``:
      * sensors can collectively burn up to $0.50 → at that point
        their per-call preflight refuses
      * foreground (complex / fixture / immediate / non-tier) ops
        always see the full ``remaining``, so the original $0.50
        reserve stays available for at least one Claude complex
        call ($0.50 estimate)

    Closes the bt-2026-05-23-211212 failure mode where concurrent
    sensor ops consumed budget and the fixture's $0.50 Claude
    estimate hit ``session_remaining=$0.4863``.

    When ``signal_source`` is None (default — all callers
    pre-Slice-12Y), behavior is byte-identical to the legacy gate.

    No-op when :func:`get_session_remaining_usd` returns ``None`` —
    preserves byte-identical pre-PR behavior in environments without
    an active session cap.

    NEVER raises any exception class OTHER than
    :class:`SessionBudgetPreflightRefused`."""
    try:
        remaining = get_session_remaining_usd()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[SBA] preflight: remaining read fault: %s", exc)
        return  # fail-OPEN on adapter fault
    if remaining is None:
        return  # no authority active
    try:
        est = float(max(0.0, estimated_cost_usd or 0.0))
    except (TypeError, ValueError):
        # Bad estimate input — fail-OPEN so a misformed caller doesn't
        # block ops. The post-hoc CostTracker.budget_event remains the
        # safety net.
        logger.debug(
            "[SBA] preflight: bad estimated_cost_usd %r — fail-open",
            estimated_cost_usd,
        )
        return

    # ── Slice 12Y Part 1 — Background spend ceiling ──
    # Computed BEFORE the legacy `est > remaining` check so the
    # background-specific refusal carries a distinct telemetry
    # tag ("background_spend_ceiling") instead of falling through
    # to the generic preflight refusal.
    if is_background_tier_source(signal_source):
        try:
            total_cap = get_session_total_cap_usd()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[SBA] preflight: total_cap read fault: %s", exc,
            )
            total_cap = None
        if total_cap is not None and total_cap > 0.0:
            limit_pct = get_background_spend_limit_pct()
            # Foreground reserve = total_cap * (1 - limit_pct).
            # Background ops only see the surplus above this
            # reserve as their available budget.
            foreground_reserve = total_cap * (1.0 - limit_pct)
            bg_remaining = max(0.0, remaining - foreground_reserve)
            if est > bg_remaining:
                logger.info(
                    "[SBA] preflight REFUSED (background_spend_ceiling): "
                    "provider=%s signal_source=%s est=$%.4f > "
                    "bg_remaining=$%.4f (total_cap=$%.4f "
                    "foreground_reserve=$%.4f limit_pct=%.2f)",
                    provider_name, signal_source, est, bg_remaining,
                    total_cap, foreground_reserve, limit_pct,
                )
                raise SessionBudgetPreflightRefused(
                    provider=str(provider_name),
                    estimated_cost_usd=est,
                    session_remaining_usd=bg_remaining,
                    reason=(
                        "session_budget_preflight_refused:"
                        "background_spend_ceiling"
                    ),
                )

    if est > remaining:
        logger.info(
            "[SBA] preflight REFUSED: provider=%s est=$%.4f > "
            "session_remaining=$%.4f",
            provider_name, est, remaining,
        )
        raise SessionBudgetPreflightRefused(
            provider=str(provider_name),
            estimated_cost_usd=est,
            session_remaining_usd=remaining,
        )


__all__ = [
    "BACKGROUND_SPEND_LIMIT_PCT_ENV_VAR",
    "SESSION_BUDGET_AUTHORITY_SCHEMA_VERSION",
    "SessionBudgetPreflightRefused",
    "_BACKGROUND_TIER_SIGNAL_SOURCES",
    "check_preflight",
    "get_background_spend_limit_pct",
    "get_session_budget_provider",
    "get_session_remaining_usd",
    "get_session_total_cap_usd",
    "is_background_tier_source",
    "reset_for_tests",
    "set_session_budget_provider",
]
