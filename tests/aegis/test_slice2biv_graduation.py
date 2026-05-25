"""Slice 2B-iv — Aegis Zero-Trust GRADUATION.

Closes the entire Aegis Zero-Trust Arc by graduating both master
switches to default-TRUE. The architecture has been proven
end-to-end across 6 sequential soaks:

  bt-2026-05-24-222008 → Slice 2B-ii.1 surfaced (Catch-22)
  bt-2026-05-24-225714 → Slice 2B-ii.2 + 2B-iii.1 surfaced
  bt-2026-05-24-232345 → Slice 2B-iii.2 surfaced (caps default $0)
  bt-2026-05-24-233640 → architecture fully proven (signals 1-4 green;
                          DW 403 = real upstream entitlement, not Aegis)
  bt-2026-05-24-235428 → Slice 2B-iii.3 surfaced (header collision 401)
  bt-2026-05-25-004146 → ULTIMATE: ZERO AuthenticationError, real
                          $0.10 Claude generation through Aegis with
                          stop_reason=end_turn output_tokens=5433.
                          Iron Gate then correctly refused the
                          undisciplined patch attempt — governance
                          working as designed.

# What graduates

Both ``JARVIS_AEGIS_ENABLED`` and ``JARVIS_AEGIS_FORWARDING_ENABLED``
flip from default-FALSE → default-TRUE. The graduation is **dual**
because the empirical evidence shows the architecture works ONLY when
both are on:

  * Master on / forwarding off → daemon spawns + scrubs creds, but
    no /v1/* routes registered → providers POST to {AEGIS}/v1/messages
    → 404 → all generation breaks by default. Would ship a regression.
  * Master off / forwarding on → forwarding routes never registered
    (build_app reads master flag at boot).
  * Master on / forwarding on → the configuration we tested across
    all 6 soaks; end-to-end operational. THIS is the default.

# Why this is the right time

  * Architecture proven (request_id from Anthropic in soaks 4, 5, 6
    — REAL upstream traffic, not mocked).
  * Strip-fix proven (zero AuthenticationError in 10-min soak after
    Slice 2B-iii.3 landed, vs 1 within 30s pre-fix).
  * Governance proven (Iron Gate correctly refused exploration-skipping
    Claude on the ansible op — fail-closed posture intact).
  * Soak hygiene proven (Slice 2B-iii.1 ledger rotation + 2B-iii.2
    structural cap defaults ensure clean per-session state).

# What this slice does NOT do

  * NO modification to `aegis/flags.py` defaults beyond the two
    booleans flipping — `session_cap_usd`, `hourly_burn_cap_usd`,
    `route_caps_usd` all STAY at $0.00 fail-closed (operator must
    explicitly authorize spend per session).
  * NO modification to operator-facing env-precedence — any operator
    setting `=false` for either flag preserves opt-out.
  * NO modification to provider behavior under disabled-Aegis paths
    — bridge factory still has the legacy direct-upstream branch
    intact (would fire if operator explicitly opts out).

# Test surface

AST pins (4): both flag defaults locked structurally at the
``is_enabled()`` / ``forwarding_enabled()`` function-body level
AND at the ``_seeds()`` FlagSpec registration level (the FlagRegistry
seed mirrors the function default — both must stay in sync).

Spine tests (4): both helpers return True when env unset; both
return False when env explicitly =false (operator-opt-out preserved);
both return True when env explicitly =true (post-graduation explicit
opt-in is a no-op).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FLAGS_FILE = REPO_ROOT / "backend" / "core" / "ouroboros" / "aegis" / "flags.py"


def _parse_flags() -> ast.Module:
    return ast.parse(FLAGS_FILE.read_text())


# ──────────────────────────────────────────────────────────────────────
# AST PIN #1 — is_enabled() function body has default=True
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_is_enabled_default_true() -> None:
    """``is_enabled()`` body must call ``get_bool(..., default=True)``.

    This is the load-bearing graduation. Reverting to default=False
    would silently demote the entire Aegis Zero-Trust posture across
    every JARVIS deployment on next pull.
    """
    tree = _parse_flags()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "is_enabled":
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if not isinstance(sub.func, ast.Attribute):
                continue
            if sub.func.attr != "get_bool":
                continue
            default_kw = next(
                (kw for kw in sub.keywords if kw.arg == "default"), None,
            )
            assert default_kw is not None, (
                "is_enabled() get_bool() call missing default= kwarg"
            )
            assert isinstance(default_kw.value, ast.Constant), (
                f"default= must be a literal, got {ast.dump(default_kw.value)}"
            )
            assert default_kw.value.value is True, (
                "is_enabled() default REVERTED from True → False. "
                "Slice 2B-iv graduated this flag based on 6-soak "
                "empirical evidence; reverting would silently demote "
                "the entire Aegis Zero-Trust posture by default."
            )
            return
    pytest.fail("is_enabled() function not found in flags.py")


# ──────────────────────────────────────────────────────────────────────
# AST PIN #2 — forwarding_enabled() function body has default=True
# ──────────────────────────────────────────────────────────────────────

def test_ast_pin_forwarding_enabled_default_true() -> None:
    """``forwarding_enabled()`` body must call ``get_bool(..., default=True)``.

    Dual-graduation requirement: master without forwarding = 404 on
    every /v1/* call = all generation broken by default. The two
    flags graduate together.
    """
    tree = _parse_flags()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != "forwarding_enabled":
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if not isinstance(sub.func, ast.Attribute):
                continue
            if sub.func.attr != "get_bool":
                continue
            default_kw = next(
                (kw for kw in sub.keywords if kw.arg == "default"), None,
            )
            assert default_kw is not None
            assert isinstance(default_kw.value, ast.Constant)
            assert default_kw.value.value is True, (
                "forwarding_enabled() default REVERTED from True → "
                "False. Slice 2B-iv graduated this with the master; "
                "demoting it (without also demoting master) would "
                "break all generation by default — providers route "
                "through Aegis but daemon would return 404."
            )
            return
    pytest.fail("forwarding_enabled() function not found in flags.py")


# ──────────────────────────────────────────────────────────────────────
# AST PIN #3 — FlagSpec seeds for both flags also default=True
# ──────────────────────────────────────────────────────────────────────

def _find_flagspec_default(tree: ast.Module, env_var_attr_name: str) -> bool:
    """Walk for ``FlagSpec(name=ENV_AEGIS_ENABLED, ..., default=...)``
    and return the literal default. The FlagRegistry seed mirrors the
    function default — both must stay in sync."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "FlagSpec":
            continue
        name_kw = next((kw for kw in node.keywords if kw.arg == "name"), None)
        if name_kw is None:
            continue
        # Match against the ENV constant name
        if not (
            isinstance(name_kw.value, ast.Name)
            and name_kw.value.id == env_var_attr_name
        ):
            continue
        default_kw = next(
            (kw for kw in node.keywords if kw.arg == "default"), None,
        )
        assert default_kw is not None, (
            f"FlagSpec(name={env_var_attr_name}) missing default= kwarg"
        )
        assert isinstance(default_kw.value, ast.Constant)
        return default_kw.value.value
    pytest.fail(f"FlagSpec(name={env_var_attr_name}) not found")


def test_ast_pin_flagspec_seed_aegis_enabled_default_true() -> None:
    """The FlagRegistry seed for ENV_AEGIS_ENABLED must also be
    default=True (mirrors the is_enabled() function default).

    De-sync between function-default + seed-default is a known
    foot-gun: docs/observability show one value, runtime reads
    another. This pin enforces parity.
    """
    tree = _parse_flags()
    assert _find_flagspec_default(tree, "ENV_AEGIS_ENABLED") is True, (
        "FlagSpec seed for ENV_AEGIS_ENABLED is NOT default=True. "
        "Mismatch between function default (graduated to True) and "
        "registry seed (still False) would mis-document the "
        "graduated state in /observability/flags and /help."
    )


def test_ast_pin_flagspec_seed_forwarding_enabled_default_true() -> None:
    """Same parity check for ENV_AEGIS_FORWARDING_ENABLED."""
    tree = _parse_flags()
    assert _find_flagspec_default(tree, "ENV_AEGIS_FORWARDING_ENABLED") is True, (
        "FlagSpec seed for ENV_AEGIS_FORWARDING_ENABLED is NOT "
        "default=True. See parallel test for ENV_AEGIS_ENABLED."
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — runtime behavior matches the AST pins
# ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def aegis_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip both Aegis master/forwarding env vars so the helpers
    fall through to their default. Per-fixture reset; monkeypatch
    restores after the test."""
    monkeypatch.delenv("JARVIS_AEGIS_ENABLED", raising=False)
    monkeypatch.delenv("JARVIS_AEGIS_FORWARDING_ENABLED", raising=False)


def test_spine_is_enabled_true_when_env_unset(aegis_env_unset: None) -> None:
    """With no env var, ``is_enabled()`` returns True (graduated)."""
    from backend.core.ouroboros.aegis.flags import is_enabled
    assert is_enabled() is True


def test_spine_forwarding_enabled_true_when_env_unset(
    aegis_env_unset: None,
) -> None:
    """With no env var, ``forwarding_enabled()`` returns True."""
    from backend.core.ouroboros.aegis.flags import forwarding_enabled
    assert forwarding_enabled() is True


def test_spine_operator_can_still_opt_out_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator explicit opt-out (env =false) STILL preserved
    post-graduation — graduation doesn't seal the flag, just flips
    the default. Critical for emergency rollback scenarios."""
    from backend.core.ouroboros.aegis.flags import (
        is_enabled, forwarding_enabled,
    )
    monkeypatch.setenv("JARVIS_AEGIS_ENABLED", "false")
    monkeypatch.setenv("JARVIS_AEGIS_FORWARDING_ENABLED", "false")
    assert is_enabled() is False, (
        "Operator opt-out (JARVIS_AEGIS_ENABLED=false) was overridden "
        "post-graduation — would prevent emergency rollback. "
        "Graduation must flip the DEFAULT, not seal the flag."
    )
    assert forwarding_enabled() is False, (
        "Same for forwarding flag."
    )


def test_spine_operator_explicit_true_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators with existing soak runbooks that set =true explicitly
    (per the 6 prior soaks) keep working — explicit-true matches the
    new default; behavior unchanged."""
    from backend.core.ouroboros.aegis.flags import (
        is_enabled, forwarding_enabled,
    )
    monkeypatch.setenv("JARVIS_AEGIS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_AEGIS_FORWARDING_ENABLED", "true")
    assert is_enabled() is True
    assert forwarding_enabled() is True
