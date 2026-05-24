"""Aegis battle-test cap defaults (Slice 2B-iii.2).

Closes the gap surfaced by triumphant detonation soak
bt-2026-05-24-232345: the daemon's ``session_cap_usd()`` defaults to
``$0.00`` (intentional fail-closed safety per operator binding
"ceiling should remain strict"), but the battle-test runbook needs
explicit caps for forwarding to work. Relying on a bash script to
inject env vars is brittle + violates the "no hardcoding / highly
robust" directive.

This module is the structural fix: a single helper that the
battle-test harness invokes BEFORE the Aegis preflight step, which
sets the cap env vars **only if the operator hasn't already set
them**. Operator overrides take precedence — standard env-var
precedence semantics, the canonical pattern across the codebase.

# Operator bindings honored

  * **Daemon defaults stay strict**: ``aegis/flags.py`` is untouched;
    production Aegis use (non-battle-test) still defaults to $0.00
    (fail-closed). Only the battle-test harness composes this helper.
  * **Operator authority precedence**: any operator-set value (via
    env var) is preserved. The helper writes only into "unset" slots.
  * **Empty-string defensive coercion**: ``export VAR=`` (common bash
    typo) is treated as unset, not as an explicit "" value — protects
    operators from accidentally getting the $0.00 fail-closed path
    by mistake.
  * **Auditable**: returns a structured :class:`CapsResult` so the
    harness can log what happened (default vs operator vs already_set
    sources, per cap).
  * **Single seam**: the literal env-var names + default values live
    here. AST pin in the test suite forbids re-introducing the
    literals in ``scripts/ouroboros_battle_test.py``.

# Why $2.00?

Sized to comfortably accommodate the existing battle-test
``--cost-cap 1.00`` (SBA-level cap) + Aegis-side per-call lease
validation overhead (~$0.001 per heavy_probe, ~$0.0001 per
health_probe). Strict, but realistic for the wiring-validation soak
profile. Operator can override at any time via env.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from backend.core.ouroboros.aegis.flags import (
    ENV_AEGIS_HOURLY_BURN_CAP_USD,
    ENV_AEGIS_SESSION_CAP_USD,
)

logger = logging.getLogger(__name__)


# Canonical battle-test defaults. Single source of truth. AST pin
# forbids re-introducing these literals in the harness script.
DEFAULT_BATTLE_TEST_SESSION_CAP_USD: str = "2.00"
DEFAULT_BATTLE_TEST_HOURLY_BURN_CAP_USD: str = "2.00"


# Source-attribution literal type — auditability surface for the
# harness boot diagnostics. ``operator`` means env-precedence
# preserved; ``default`` means this helper wrote the value;
# ``already_set`` means a prior call already wrote it.
CapSource = Literal["operator", "default", "already_set"]


@dataclass(frozen=True)
class CapsResult:
    """Outcome of one ``default_battle_test_caps()`` invocation.

    Both ``*_source`` fields tell the operator EXACTLY where the
    effective value came from — critical for soak diagnostics where
    a misconfigured cap causes the lease-denied chain.
    """
    ok: bool
    session_cap_source: CapSource
    hourly_burn_cap_source: CapSource
    detail: str = ""


def _is_truly_unset(env_var: str) -> bool:
    """True iff the env var is absent OR set to an empty/whitespace
    string (the common bash typo case).

    Single seam — every "is this env var meaningfully set" check in
    this module routes through here so the policy is consistent.
    """
    raw = os.environ.get(env_var)
    return raw is None or not raw.strip()


# Module-level marker — tracks whether THIS process has written
# either cap, so a second invocation can report ``already_set`` rather
# than misattributing the write to ``operator``.
_HELPER_WROTE_SESSION_CAP: bool = False
_HELPER_WROTE_HOURLY_BURN_CAP: bool = False


def _reset_for_tests() -> None:
    """Test-only escape hatch — clears the module-level write tracker
    so monkeypatch-based tests can exercise the first-write path
    cleanly without leaking state across tests."""
    global _HELPER_WROTE_SESSION_CAP, _HELPER_WROTE_HOURLY_BURN_CAP
    _HELPER_WROTE_SESSION_CAP = False
    _HELPER_WROTE_HOURLY_BURN_CAP = False


def _resolve_source(env_var: str, prior_write_flag: bool) -> CapSource:
    """Classify where the EFFECTIVE env-var value came from.

    The decision tree:
      * Currently unset / empty → no source yet (caller will write
        default; this function's return only applies AFTER the
        meaningfulness check).
      * Set AND this helper wrote it earlier → ``already_set``
        (idempotent second call).
      * Set AND this helper did NOT write it → ``operator``
        (env-precedence preserved).

    Callers must check ``_is_truly_unset`` first; this only runs
    when the value is meaningfully present.
    """
    if prior_write_flag:
        return "already_set"
    return "operator"


def default_battle_test_caps() -> CapsResult:
    """Structurally install battle-test Aegis cap defaults.

    For each of ``JARVIS_AEGIS_SESSION_CAP_USD`` and
    ``JARVIS_AEGIS_HOURLY_BURN_CAP_USD``:

      1. If the env var is **unset** (or empty/whitespace), write the
         canonical battle-test default (``$2.00``).
      2. If the env var was **set by the operator** before this
         helper ran, preserve it (env-precedence).
      3. If the env var was **set by an earlier call to this helper
         in the same process**, report ``already_set`` (idempotent).

    Returns a :class:`CapsResult` so the caller (battle-test harness)
    can log exactly what happened per cap. NEVER raises — env-var
    writes that fail (e.g., sealed env in some test runners) fold
    into ``ok=False`` so the harness keeps booting.

    Operator binding: this function **never overrides an operator
    value**. The daemon-side defaults in ``aegis/flags.py`` remain
    $0.00 fail-closed for production.
    """
    global _HELPER_WROTE_SESSION_CAP, _HELPER_WROTE_HOURLY_BURN_CAP
    try:
        # ── session cap ──────────────────────────────────────────────
        if _is_truly_unset(ENV_AEGIS_SESSION_CAP_USD):
            os.environ[ENV_AEGIS_SESSION_CAP_USD] = DEFAULT_BATTLE_TEST_SESSION_CAP_USD
            _HELPER_WROTE_SESSION_CAP = True
            session_src: CapSource = "default"
            logger.info(
                "[BattleTestDefaults] %s unset → installing default "
                "$%s (operator can override via env)",
                ENV_AEGIS_SESSION_CAP_USD,
                DEFAULT_BATTLE_TEST_SESSION_CAP_USD,
            )
        else:
            session_src = _resolve_source(
                ENV_AEGIS_SESSION_CAP_USD, _HELPER_WROTE_SESSION_CAP,
            )
            if session_src == "operator":
                logger.info(
                    "[BattleTestDefaults] %s preset by operator → "
                    "preserving $%s",
                    ENV_AEGIS_SESSION_CAP_USD,
                    os.environ[ENV_AEGIS_SESSION_CAP_USD],
                )

        # ── hourly burn cap ──────────────────────────────────────────
        if _is_truly_unset(ENV_AEGIS_HOURLY_BURN_CAP_USD):
            os.environ[ENV_AEGIS_HOURLY_BURN_CAP_USD] = DEFAULT_BATTLE_TEST_HOURLY_BURN_CAP_USD
            _HELPER_WROTE_HOURLY_BURN_CAP = True
            burn_src: CapSource = "default"
            logger.info(
                "[BattleTestDefaults] %s unset → installing default "
                "$%s (operator can override via env)",
                ENV_AEGIS_HOURLY_BURN_CAP_USD,
                DEFAULT_BATTLE_TEST_HOURLY_BURN_CAP_USD,
            )
        else:
            burn_src = _resolve_source(
                ENV_AEGIS_HOURLY_BURN_CAP_USD, _HELPER_WROTE_HOURLY_BURN_CAP,
            )
            if burn_src == "operator":
                logger.info(
                    "[BattleTestDefaults] %s preset by operator → "
                    "preserving $%s",
                    ENV_AEGIS_HOURLY_BURN_CAP_USD,
                    os.environ[ENV_AEGIS_HOURLY_BURN_CAP_USD],
                )

        return CapsResult(
            ok=True,
            session_cap_source=session_src,
            hourly_burn_cap_source=burn_src,
            detail=(
                f"session={os.environ.get(ENV_AEGIS_SESSION_CAP_USD, '?')}"
                f" hourly_burn={os.environ.get(ENV_AEGIS_HOURLY_BURN_CAP_USD, '?')}"
            ),
        )
    except Exception as err:  # noqa: BLE001 — fail-closed surface
        logger.warning(
            "[BattleTestDefaults] env injection failed: %r — harness "
            "keeps booting; daemon will refuse leases until caps set",
            err,
        )
        return CapsResult(
            ok=False,
            session_cap_source="default",
            hourly_burn_cap_source="default",
            detail=f"{type(err).__name__}: {err!s}",
        )


__all__ = [
    "CapsResult",
    "default_battle_test_caps",
    "DEFAULT_BATTLE_TEST_SESSION_CAP_USD",
    "DEFAULT_BATTLE_TEST_HOURLY_BURN_CAP_USD",
]
