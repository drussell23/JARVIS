"""Slice 2B-iii.2 — Structural battle-test Aegis caps.

Closes the gap surfaced by the triumphant detonation soak
bt-2026-05-24-232345: the daemon's ``session_cap_usd()`` defaults to
``$0.00`` (intentional fail-closed safety per operator binding
"ceiling should remain strict"). The runbook needs explicit caps,
but relying on a bash script to inject env vars is brittle + violates
the "no hardcoding / highly robust" directive.

# Fix

New module ``backend/core/ouroboros/aegis/battle_test_defaults.py``
exposes ``default_battle_test_caps()`` — a structural helper that
sets the cap env vars **only if the operator hasn't already set
them**. Operator overrides take precedence (env-var precedence,
the canonical pattern across the codebase).

# Operator binding (verbatim)

  * "Do NOT touch the $0.00 default in aegis/flags.py. The
    production daemon must remain default fail-closed."
  * "This helper must inject a strict battle-test budget ... into
    the environment only if those variables are not already set
    by the operator."
  * "This injection must occur before the Aegis preflight boot
    sequence."

# Why $2.00?

Defaults sized to comfortably accommodate the existing battle-test
``--cost-cap 1.00`` (the SBA-level cap) + headroom for the Aegis
daemon's per-call lease validation (~$0.001 per heavy_probe +
~$0.0001 per health_probe). Strict, but realistic for the soak
profile. Operator can override via env at any time.

# Test surface

  * Spine: unset env + helper invocation → both vars set to defaults
  * Spine: operator-set env → helper preserves operator values (no
    override)
  * Spine: partial operator override (one set, one unset) → operator
    value preserved AND default applied to the other (independent)
  * Spine: helper returns structured CapsResult — auditable
  * AST pin: no hardcoded ``JARVIS_AEGIS_SESSION_CAP_USD`` /
    ``JARVIS_AEGIS_HOURLY_BURN_CAP_USD`` string literals in
    ``scripts/ouroboros_battle_test.py`` (anti-regression — would
    indicate someone re-introduced the bash-script env injection)
  * AST pin: helper invoked BEFORE Aegis preflight in the harness
    (source-order)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.aegis import battle_test_defaults


@pytest.fixture(autouse=True)
def _reset_helper_write_tracker() -> None:
    """Each test starts with fresh module-level write trackers so the
    ``operator`` vs ``already_set`` classification is correct in
    isolation. Without this, the first test to call the helper would
    set the module-level flags True and every subsequent test would
    see operator-set values misclassified as ``already_set``."""
    battle_test_defaults._reset_for_tests()


# ──────────────────────────────────────────────────────────────────────
# Spine — env defaults applied when unset
# ──────────────────────────────────────────────────────────────────────

def test_caps_applied_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When neither env var is set, the helper installs the canonical
    battle-test defaults so the Aegis daemon boots with non-zero
    ceilings (otherwise every lease is denied with session_cap_exceeded)."""
    monkeypatch.delenv("JARVIS_AEGIS_SESSION_CAP_USD", raising=False)
    monkeypatch.delenv("JARVIS_AEGIS_HOURLY_BURN_CAP_USD", raising=False)

    result = battle_test_defaults.default_battle_test_caps()

    assert result.ok is True
    assert result.session_cap_source == "default"
    assert result.hourly_burn_cap_source == "default"
    # The canonical default — sized to accommodate the soak's
    # --cost-cap 1.00 + Aegis lease overhead.
    import os
    assert os.environ["JARVIS_AEGIS_SESSION_CAP_USD"] == "2.00"
    assert os.environ["JARVIS_AEGIS_HOURLY_BURN_CAP_USD"] == "2.00"


# ──────────────────────────────────────────────────────────────────────
# Spine — operator override preserved (env precedence)
# ──────────────────────────────────────────────────────────────────────

def test_operator_session_cap_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the operator pre-set ``JARVIS_AEGIS_SESSION_CAP_USD``, the
    helper must NOT overwrite it — operator authority precedence."""
    monkeypatch.setenv("JARVIS_AEGIS_SESSION_CAP_USD", "0.50")
    monkeypatch.delenv("JARVIS_AEGIS_HOURLY_BURN_CAP_USD", raising=False)

    result = battle_test_defaults.default_battle_test_caps()

    assert result.ok is True
    assert result.session_cap_source == "operator"
    assert result.hourly_burn_cap_source == "default"
    import os
    assert os.environ["JARVIS_AEGIS_SESSION_CAP_USD"] == "0.50"
    assert os.environ["JARVIS_AEGIS_HOURLY_BURN_CAP_USD"] == "2.00"


def test_operator_hourly_burn_cap_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent override of the hourly burn cap — symmetric."""
    monkeypatch.delenv("JARVIS_AEGIS_SESSION_CAP_USD", raising=False)
    monkeypatch.setenv("JARVIS_AEGIS_HOURLY_BURN_CAP_USD", "5.00")

    result = battle_test_defaults.default_battle_test_caps()

    assert result.session_cap_source == "default"
    assert result.hourly_burn_cap_source == "operator"
    import os
    assert os.environ["JARVIS_AEGIS_SESSION_CAP_USD"] == "2.00"
    assert os.environ["JARVIS_AEGIS_HOURLY_BURN_CAP_USD"] == "5.00"


def test_both_operator_overrides_preserved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When operator sets both, helper is a no-op for env state but
    still returns a structured result (auditable)."""
    monkeypatch.setenv("JARVIS_AEGIS_SESSION_CAP_USD", "10.00")
    monkeypatch.setenv("JARVIS_AEGIS_HOURLY_BURN_CAP_USD", "10.00")

    result = battle_test_defaults.default_battle_test_caps()

    assert result.ok is True
    assert result.session_cap_source == "operator"
    assert result.hourly_burn_cap_source == "operator"
    import os
    assert os.environ["JARVIS_AEGIS_SESSION_CAP_USD"] == "10.00"
    assert os.environ["JARVIS_AEGIS_HOURLY_BURN_CAP_USD"] == "10.00"


# ──────────────────────────────────────────────────────────────────────
# Spine — empty-string env var treated as unset (defensive)
# ──────────────────────────────────────────────────────────────────────

def test_empty_string_env_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the operator sets the env var to an empty string (common
    bash typo — ``export JARVIS_AEGIS_SESSION_CAP_USD=``), the
    helper applies the default rather than leaving an invalid value."""
    monkeypatch.setenv("JARVIS_AEGIS_SESSION_CAP_USD", "")
    monkeypatch.setenv("JARVIS_AEGIS_HOURLY_BURN_CAP_USD", "   ")  # whitespace

    result = battle_test_defaults.default_battle_test_caps()

    assert result.session_cap_source == "default"
    assert result.hourly_burn_cap_source == "default"
    import os
    assert os.environ["JARVIS_AEGIS_SESSION_CAP_USD"] == "2.00"
    assert os.environ["JARVIS_AEGIS_HOURLY_BURN_CAP_USD"] == "2.00"


# ──────────────────────────────────────────────────────────────────────
# Spine — idempotent (second call with same state is a no-op)
# ──────────────────────────────────────────────────────────────────────

def test_idempotent_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling twice produces consistent state — second call sees
    the first call's writes as 'already set' (sources reflect the
    earlier write, not 'operator')."""
    monkeypatch.delenv("JARVIS_AEGIS_SESSION_CAP_USD", raising=False)
    monkeypatch.delenv("JARVIS_AEGIS_HOURLY_BURN_CAP_USD", raising=False)

    r1 = battle_test_defaults.default_battle_test_caps()
    assert r1.session_cap_source == "default"

    r2 = battle_test_defaults.default_battle_test_caps()
    # On second call the values are now in env (set by us), so they
    # appear "preset" — we report ``already_set`` to distinguish from
    # genuine operator overrides.
    assert r2.ok is True
    assert r2.session_cap_source == "already_set"
    assert r2.hourly_burn_cap_source == "already_set"


# ──────────────────────────────────────────────────────────────────────
# AST PIN #1 — no hardcoded cap-env literals in the harness script
# ──────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_SCRIPT = REPO_ROOT / "scripts" / "ouroboros_battle_test.py"


def test_ast_pin_no_hardcoded_cap_env_literals_in_harness() -> None:
    """``scripts/ouroboros_battle_test.py`` must NOT contain the
    string literals ``"JARVIS_AEGIS_SESSION_CAP_USD"`` or
    ``"JARVIS_AEGIS_HOURLY_BURN_CAP_USD"`` — those names belong in
    ``aegis/flags.py`` (canonical declaration) and
    ``aegis/battle_test_defaults.py`` (the helper that reads/writes
    them). Re-introducing them in the harness would signal someone
    bypassed the helper with an inline ``os.environ.setdefault(...)``
    or equivalent — exactly the bash-script-style brittleness this
    slice was built to eliminate.

    The single sanctioned mention is the helper invocation:
    ``default_battle_test_caps()``.
    """
    src = HARNESS_SCRIPT.read_text()
    offenders = []
    for needle in (
        "JARVIS_AEGIS_SESSION_CAP_USD",
        "JARVIS_AEGIS_HOURLY_BURN_CAP_USD",
    ):
        if needle in src:
            # Find line numbers for forensics
            for lineno, line in enumerate(src.splitlines(), 1):
                if needle in line:
                    offenders.append(f"scripts/ouroboros_battle_test.py:{lineno}: {line.strip()[:80]}")
    assert not offenders, (
        "Hardcoded Aegis cap env-var literal(s) in the harness script. "
        "Must route through aegis.battle_test_defaults.default_battle_test_caps().\n"
        "Offenders:\n  " + "\n  ".join(offenders)
    )


# ──────────────────────────────────────────────────────────────────────
# AST PIN #2 — helper invoked BEFORE aegis_preflight() in harness
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_default_battle_test_caps_invoked_before_preflight() -> None:
    """The harness must call ``default_battle_test_caps()`` BEFORE
    the Aegis preflight step spawns the daemon — otherwise the
    daemon boots with the $0.00 defaults and refuses every lease.
    Source-order pin (same shape as the ledger hygiene wiring pin
    from Slice 2B-iii.1)."""
    src = HARNESS_SCRIPT.read_text()
    caps_idx = src.find("default_battle_test_caps")
    preflight_idx = src.find("aegis_preflight()")
    assert caps_idx > 0, (
        "scripts/ouroboros_battle_test.py does not invoke "
        "default_battle_test_caps"
    )
    assert preflight_idx > 0, (
        "aegis_preflight() invocation not found in harness"
    )
    assert caps_idx < preflight_idx, (
        f"default_battle_test_caps invoked AFTER aegis_preflight() — "
        f"the daemon boots with $0.00 defaults before the helper sets "
        f"the env vars. Source-order must be: caps → preflight. "
        f"caps at char {caps_idx}, preflight at char {preflight_idx}"
    )
