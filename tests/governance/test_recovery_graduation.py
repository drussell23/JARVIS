"""Graduation pins — Recovery Guidance + Voice Loop Closure arc.

Critical pins:
  * authority grep on all new modules
  * rule-coverage pin (every known failure_class has a dedicated rule)
  * no-LLM pin (advisor is deterministic, no model-generated text)
  * voice-opt-in pin (default is silent; narration requires two
    explicit env flags)
  * headless safety pin (construction never imports audio stack)
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest


_ARC_MODULES = [
    "backend/core/ouroboros/governance/recovery_advisor.py",
    "backend/core/ouroboros/governance/recovery_formatter.py",
    "backend/core/ouroboros/governance/recovery_announcer.py",
    "backend/core/ouroboros/governance/recovery_repl.py",
    "backend/core/ouroboros/governance/recovery_store.py",
]


_FORBIDDEN = (
    "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
    "semantic_guardian", "tool_executor", "candidate_generator",
    "change_engine",
)


@pytest.mark.parametrize("rel_path", _ARC_MODULES)
def test_module_has_no_authority_imports(rel_path: str):
    """Grep for ``from X.<forbidden>`` / ``import X.<forbidden>`` imports.

    Word-boundary anchors so names like ``unified_voice_orchestrator``
    don't falsely trigger on the substring ``orchestrator``.
    """
    src = Path(rel_path).read_text()
    violations: List[str] = []
    for mod in _FORBIDDEN:
        if re.search(
            rf"^\s*(from|import)\s+[^#\n]*\b{re.escape(mod)}\b",
            src, re.MULTILINE,
        ):
            violations.append(mod)
    assert violations == [], (
        f"{rel_path} imports forbidden: {violations}"
    )


# ===========================================================================
# No-LLM pin — advisor is deterministic
# ===========================================================================


@pytest.mark.parametrize("rel_path", [
    "backend/core/ouroboros/governance/recovery_advisor.py",
    "backend/core/ouroboros/governance/recovery_formatter.py",
    "backend/core/ouroboros/governance/recovery_store.py",
])
def test_advisor_modules_do_not_import_model_surface(rel_path: str):
    """The advisor + formatter + store must NEVER import a model
    surface — recovery is rule-based by design."""
    src = Path(rel_path).read_text()
    for forbidden in (
        "providers", "doubleword_provider", "candidate_generator",
        "plan_generator", "semantic_triage",
    ):
        assert not re.search(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(forbidden)}",
            src, re.MULTILINE,
        ), f"{rel_path} imports model surface {forbidden!r}"


# ===========================================================================
# Schema versions pinned
# ===========================================================================


def test_schema_versions_pinned():
    from backend.core.ouroboros.governance.recovery_advisor import (
        RECOVERY_PLAN_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.recovery_formatter import (
        RECOVERY_FORMATTER_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.recovery_announcer import (
        RECOVERY_ANNOUNCER_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.recovery_store import (
        RECOVERY_STORE_SCHEMA_VERSION,
    )
    assert RECOVERY_PLAN_SCHEMA_VERSION == "recovery_plan.v1"
    assert RECOVERY_FORMATTER_SCHEMA_VERSION == "recovery_formatter.v1"
    assert RECOVERY_ANNOUNCER_SCHEMA_VERSION == "recovery_announcer.v1"
    assert RECOVERY_STORE_SCHEMA_VERSION == "recovery_store.v1"


# ===========================================================================
# CRITICAL: rule coverage — every known stop_reason has a dedicated rule
# ===========================================================================


def test_every_known_stop_reason_has_dedicated_rule():
    from backend.core.ouroboros.governance.recovery_advisor import (
        FailureContext, advise, known_stop_reasons,
    )
    for stop_reason in known_stop_reasons():
        plan = advise(FailureContext(
            op_id="op-pin", stop_reason=stop_reason,
        ))
        assert plan.matched_rule != "generic", (
            f"stop_reason={stop_reason!r} fell through to generic"
        )
        assert plan.has_suggestions, (
            f"stop_reason={stop_reason!r} produced empty plan"
        )


def test_rule_count_at_least_14():
    from backend.core.ouroboros.governance.recovery_advisor import rule_count
    # 14 known stop reasons + exception fallback. Adding rules is
    # welcome; removing one below 14 would reduce coverage.
    assert rule_count() >= 14


def test_every_plan_has_bounded_suggestions():
    from backend.core.ouroboros.governance.recovery_advisor import (
        FailureContext, advise, known_stop_reasons,
    )
    for stop_reason in known_stop_reasons():
        plan = advise(FailureContext(
            op_id="op-pin", stop_reason=stop_reason,
        ))
        assert 1 <= len(plan.suggestions) <= 5


# ===========================================================================
# Voice opt-in pin
# ===========================================================================


def test_recovery_voice_default_off(monkeypatch):
    from backend.core.ouroboros.governance.recovery_announcer import (
        is_voice_live, recovery_voice_enabled,
    )
    for key in (
        "OUROBOROS_NARRATOR_ENABLED",
        "JARVIS_RECOVERY_VOICE_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    assert recovery_voice_enabled() is False
    # is_voice_live returns False unless BOTH are true
    assert is_voice_live() is False


def test_voice_requires_both_flags_true(monkeypatch):
    from backend.core.ouroboros.governance.recovery_announcer import (
        is_voice_live,
    )
    monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RECOVERY_VOICE_ENABLED", "true")
    assert is_voice_live() is True


# ===========================================================================
# Headless safety pin — no audio stack imported at construction
# ===========================================================================


def test_announcer_construction_does_not_import_audio():
    import sys
    from backend.core.ouroboros.governance.recovery_announcer import (
        RecoveryAnnouncer,
    )
    before = set(sys.modules)
    _ = RecoveryAnnouncer()
    after = set(sys.modules)
    forbidden = {
        "backend.core.supervisor.unified_voice_orchestrator",
    }
    leaked = [m for m in (after - before) if m in forbidden]
    assert leaked == [], f"announcer leaked audio imports: {leaked}"


# ===========================================================================
# REPL verb surface stable
# ===========================================================================


@pytest.mark.parametrize("verb", ["help", "session", "speak"])
def test_repl_help_mentions_verb(verb: str):
    from backend.core.ouroboros.governance.recovery_repl import (
        dispatch_recovery_command,
    )
    res = dispatch_recovery_command("/recover help")
    assert res.ok
    assert verb in res.text.lower()


def test_repl_speak_mentions_env_flags_when_voice_off():
    """Discoverability pin — when voice is off, the REPL tells the
    operator HOW to turn it on."""
    from backend.core.ouroboros.governance.recovery_advisor import (
        FailureContext, STOP_COST_CAP, advise,
    )
    from backend.core.ouroboros.governance.recovery_announcer import (
        RecoveryAnnouncer,
    )
    from backend.core.ouroboros.governance.recovery_repl import (
        dispatch_recovery_command,
    )
    from backend.core.ouroboros.governance.recovery_store import (
        RecoveryPlanStore,
    )
    store = RecoveryPlanStore()
    store.record(advise(FailureContext(
        op_id="op-1", stop_reason=STOP_COST_CAP,
    )))
    # No env flags set — voice off
    announcer = RecoveryAnnouncer(speaker=lambda *a, **k: None)
    res = dispatch_recovery_command(
        "/recover op-1 speak",
        plan_provider=store,
        announcer=announcer,
    )
    assert res.ok
    # Mentions at least one of the env flags needed
    lower = res.text.lower()
    assert (
        "oubarus" in lower  # not a real string, just a sentinel
        or "ouroboros_narrator" in lower
        or "jarvis_recovery_voice" in lower
    )


# ===========================================================================
# Docstring bit-rot
# ===========================================================================


def test_advisor_docstring_mentions_rule_based():
    import backend.core.ouroboros.governance.recovery_advisor as m
    doc = (m.__doc__ or "").lower()
    assert "rule-based" in doc or "rule based" in doc
    assert "no llm" in doc or "deterministic" in doc


def test_announcer_docstring_mentions_karen():
    import backend.core.ouroboros.governance.recovery_announcer as m
    doc = (m.__doc__ or "").lower()
    assert "karen" in doc


def test_repl_docstring_mentions_3_things():
    import backend.core.ouroboros.governance.recovery_repl as m
    doc = (m.__doc__ or "").lower()
    # The gap statement speaks of '3 things to try' — the operator-
    # facing REPL should name that contract.
    assert "3 things" in doc or "three things" in doc or "try" in doc


# ===========================================================================
# Determinism (re-pin across modules)
# ===========================================================================


def test_advisor_renders_deterministically():
    from backend.core.ouroboros.governance.recovery_advisor import (
        FailureContext, STOP_COST_CAP, advise,
    )
    from backend.core.ouroboros.governance.recovery_formatter import (
        render_text, render_voice,
    )
    ctx = FailureContext(
        op_id="op-det", stop_reason=STOP_COST_CAP,
        cost_spent_usd=0.80, cost_cap_usd=0.50,
    )
    plan_a = advise(ctx)
    plan_b = advise(ctx)
    assert render_text(plan_a) == render_text(plan_b)
    assert render_voice(plan_a) == render_voice(plan_b)
