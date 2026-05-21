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
) -> None:
    """Hard wallet gate. Call BEFORE provider dispatch.

    Refuses (raises :class:`SessionBudgetPreflightRefused`) when the
    remaining session budget cannot accommodate ``estimated_cost_usd``.
    The caller's ``_max_cost_per_op`` is a reasonable conservative
    upper bound when no finer estimate is available.

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
    "SESSION_BUDGET_AUTHORITY_SCHEMA_VERSION",
    "SessionBudgetPreflightRefused",
    "set_session_budget_provider",
    "get_session_budget_provider",
    "reset_for_tests",
    "get_session_remaining_usd",
    "check_preflight",
]
