"""Slice 5 graduation pins — Scheduled Wake-ups arc."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest


# ===========================================================================
# 1. Defaults — runner stays off until operator opts in
# ===========================================================================


def test_schedule_runner_default_off_by_design(monkeypatch):
    """Flipping this would start firing jobs in the background on every
    boot. Slice 5 ships the mechanism but keeps operator opt-in."""
    monkeypatch.delenv("JARVIS_SCHEDULE_RUNNER_ENABLED", raising=False)
    from backend.core.ouroboros.governance.schedule_runner import (
        schedule_runner_enabled,
    )
    assert schedule_runner_enabled() is False


# ===========================================================================
# 2. Revert matrix
# ===========================================================================


_REVERT = [
    "JARVIS_SCHEDULE_RUNNER_ENABLED",
]


@pytest.mark.parametrize("env", _REVERT)
def test_env_flag_roundtrip(env: str, monkeypatch):
    import os
    monkeypatch.setenv(env, "true")
    assert os.environ[env] == "true"
    monkeypatch.setenv(env, "false")
    assert os.environ[env] == "false"
    monkeypatch.setenv(env, "garbage")
    # Predicate reads garbage as false
    from backend.core.ouroboros.governance.schedule_runner import (
        schedule_runner_enabled,
    )
    assert schedule_runner_enabled() is False


# ===========================================================================
# 3. Authority invariants — arc modules import no gate/execution
# ===========================================================================


_ARC_MODULES = [
    "backend/core/ouroboros/governance/schedule_expression.py",
    "backend/core/ouroboros/governance/schedule_job.py",
    "backend/core/ouroboros/governance/schedule_wakeup.py",
    "backend/core/ouroboros/governance/schedule_runner.py",
]

_FORBIDDEN = (
    "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
    "semantic_guardian", "tool_executor", "candidate_generator",
    "change_engine",
)


@pytest.mark.parametrize("rel_path", _ARC_MODULES)
def test_arc_module_has_no_authority_imports(rel_path: str):
    src = Path(rel_path).read_text()
    violations: List[str] = []
    for mod in _FORBIDDEN:
        if re.search(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            src, re.MULTILINE,
        ):
            violations.append(mod)
    assert not violations, (
        f"{rel_path} imports forbidden modules: {violations}"
    )


# ===========================================================================
# 4. Schema version constants pinned
# ===========================================================================


def test_schema_versions_pinned():
    from backend.core.ouroboros.governance.schedule_expression import (
        SCHEDULE_EXPRESSION_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.schedule_job import (
        SCHEDULED_JOB_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.schedule_wakeup import (
        WAKEUP_CONTROLLER_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.schedule_runner import (
        SCHEDULE_RUNNER_SCHEMA_VERSION,
    )
    assert SCHEDULE_EXPRESSION_SCHEMA_VERSION == "schedule_expression.v1"
    assert SCHEDULED_JOB_SCHEMA_VERSION == "schedule_job.v1"
    assert WAKEUP_CONTROLLER_SCHEMA_VERSION == "schedule_wakeup.v1"
    assert SCHEDULE_RUNNER_SCHEMA_VERSION == "schedule_runner.v1"


# ===========================================================================
# 5. §1 authority: model source rejected everywhere
# ===========================================================================


def test_model_source_cannot_register_handlers():
    from backend.core.ouroboros.governance.schedule_job import (
        HandlerAuthorityError, JobRegistry,
    )

    class FakeSource(str):
        pass

    reg = JobRegistry()
    with pytest.raises(HandlerAuthorityError):
        reg.register_handler(
            "evil", lambda *a: None,
            source=FakeSource("model"),  # type: ignore[arg-type]
        )


# ===========================================================================
# 6. Docstring bit-rot guards
# ===========================================================================


def test_runner_switch_docstring_explains_default_off():
    from backend.core.ouroboros.governance.schedule_runner import (
        schedule_runner_enabled,
    )
    doc = schedule_runner_enabled.__doc__ or ""
    assert "false" in doc.lower()


# ===========================================================================
# 7. The gap-quote phrase actually parses cleanly
# ===========================================================================


def test_gap_quote_phrase_parses_end_to_end():
    """The user quote: "check this file every Monday morning." """
    from backend.core.ouroboros.governance.schedule_expression import (
        ScheduleExpression,
    )
    expr = ScheduleExpression.from_phrase("every monday at 9am")
    assert expr.canonical_cron == "0 9 * * 1"


# ===========================================================================
# 8. Gap-quote phrase round-trips through REPL
# ===========================================================================


def test_gap_quote_phrase_round_trips_through_repl():
    from backend.core.ouroboros.governance.schedule_job import (
        HandlerSource, JobRegistry,
    )
    from backend.core.ouroboros.governance.schedule_runner import (
        dispatch_schedule_command,
    )
    from backend.core.ouroboros.governance.schedule_wakeup import (
        WakeupController,
    )
    reg = JobRegistry()
    wak = WakeupController(min_delay_s=0.0, max_delay_s=100.0)

    async def _check(job, payload):
        return "checked"

    reg.register_handler("check", _check, source=HandlerSource.OPERATOR)
    result = dispatch_schedule_command(
        '/schedule add check "every monday at 9am" weekly file check',
        registry=reg, wakeup=wak,
    )
    assert result.ok is True
    jobs = reg.list_jobs()
    assert len(jobs) == 1
    assert jobs[0].expression.canonical_cron == "0 9 * * 1"


# ===========================================================================
# 9. Delay + capacity bounds still enforced post-arc
# ===========================================================================


def test_wakeup_delay_bounds_still_enforced():
    from backend.core.ouroboros.governance.schedule_wakeup import (
        WakeupController, WakeupDelayError,
    )
    ctl = WakeupController(min_delay_s=60.0, max_delay_s=3600.0)
    with pytest.raises(WakeupDelayError):
        ctl.schedule(handler_name="h", delay_seconds=0.0)
    with pytest.raises(WakeupDelayError):
        ctl.schedule(handler_name="h", delay_seconds=99999.0)


def test_wakeup_capacity_bound_still_enforced():
    from backend.core.ouroboros.governance.schedule_wakeup import (
        WakeupCapacityError, WakeupController,
    )
    ctl = WakeupController(
        min_delay_s=0.0, max_delay_s=100.0, max_pending=1,
    )
    ctl.schedule(handler_name="h", delay_seconds=50.0)
    with pytest.raises(WakeupCapacityError):
        ctl.schedule(handler_name="h", delay_seconds=50.0)
