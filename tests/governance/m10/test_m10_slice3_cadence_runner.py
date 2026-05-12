"""Regression spine for M10 Slice 3 — cadence runner.

Slice 3 ships:
  * Cadence-policy primitives (should_fire_at + env knobs)
  * run_cadence_step composing the producer-bridge
  * sweep_pending_for_merge polling PR status → phase transitions
  * expire_stale_pending transitioning timed-out proposals
  * SSE event EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED
  * /m10 sweep + /m10 expire REPL verbs
"""
from __future__ import annotations

import asyncio
import ast as _ast
import time
from pathlib import Path
from typing import Any, Iterator, List, Tuple
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.m10.cadence_runner import (
    CadenceStepResult,
    PhaseTransition,
    SweepResult,
    approval_timeout_s,
    cadence_enabled,
    cadence_n_ops,
    expire_stale_pending,
    expire_stale_pending_sync,
    gh_timeout_s,
    register_shipped_invariants,
    run_cadence_step,
    should_fire_at,
    sweep_pending_for_merge,
    sweep_pending_for_merge_sync,
)
from backend.core.ouroboros.governance.m10.proposal_store import (
    StoredProposal,
    append_proposal,
)


_M10_FLAG = "JARVIS_M10_ARCH_PROPOSER_ENABLED"
_CADENCE_FLAG = "JARVIS_M10_CADENCE_ENABLED"
_N_OPS_FLAG = "JARVIS_M10_CADENCE_N_OPS"
_TIMEOUT_FLAG = "JARVIS_M10_APPROVAL_TIMEOUT_S"


@pytest.fixture(autouse=True)
def _isolate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    monkeypatch.delenv(_M10_FLAG, raising=False)
    monkeypatch.delenv(_CADENCE_FLAG, raising=False)
    monkeypatch.delenv(_N_OPS_FLAG, raising=False)
    monkeypatch.delenv(_TIMEOUT_FLAG, raising=False)
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "proposals.jsonl"),
    )
    yield


def _enable_all(monkeypatch) -> None:
    monkeypatch.setenv(_M10_FLAG, "true")
    monkeypatch.setenv(_CADENCE_FLAG, "true")


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def test_cadence_enabled_default_false():
    assert cadence_enabled() is False


def test_cadence_requires_both_flags(monkeypatch):
    """Master alone or sub-flag alone → still disabled."""
    monkeypatch.setenv(_M10_FLAG, "true")
    assert cadence_enabled() is False
    monkeypatch.delenv(_M10_FLAG, raising=False)
    monkeypatch.setenv(_CADENCE_FLAG, "true")
    assert cadence_enabled() is False


def test_cadence_enabled_when_both_flags_on(monkeypatch):
    _enable_all(monkeypatch)
    assert cadence_enabled() is True


def test_cadence_n_ops_default_50():
    assert cadence_n_ops() == 50


def test_cadence_n_ops_env_override(monkeypatch):
    monkeypatch.setenv(_N_OPS_FLAG, "100")
    assert cadence_n_ops() == 100


def test_cadence_n_ops_clamped_to_max(monkeypatch):
    monkeypatch.setenv(_N_OPS_FLAG, "99999")
    assert cadence_n_ops() == 10_000


def test_cadence_n_ops_garbage_falls_to_default(monkeypatch):
    monkeypatch.setenv(_N_OPS_FLAG, "garbage")
    assert cadence_n_ops() == 50


def test_approval_timeout_default_24h():
    assert approval_timeout_s() == 86400.0


def test_gh_timeout_default_30s():
    assert gh_timeout_s() == 30.0


# ---------------------------------------------------------------------------
# Pure cadence policy
# ---------------------------------------------------------------------------


def test_should_fire_at_false_when_disabled():
    """Default-FALSE means should_fire_at always returns False."""
    assert should_fire_at(50) is False


def test_should_fire_at_true_on_multiples(monkeypatch):
    _enable_all(monkeypatch)
    monkeypatch.setenv(_N_OPS_FLAG, "10")
    assert should_fire_at(10) is True
    assert should_fire_at(20) is True
    assert should_fire_at(100) is True


def test_should_fire_at_false_on_non_multiples(monkeypatch):
    _enable_all(monkeypatch)
    monkeypatch.setenv(_N_OPS_FLAG, "10")
    assert should_fire_at(1) is False
    assert should_fire_at(9) is False
    assert should_fire_at(11) is False


def test_should_fire_at_false_on_zero_and_negative(monkeypatch):
    _enable_all(monkeypatch)
    assert should_fire_at(0) is False
    assert should_fire_at(-5) is False


def test_should_fire_at_never_raises_on_garbage(monkeypatch):
    _enable_all(monkeypatch)
    assert should_fire_at("garbage") is False
    assert should_fire_at(None) is False
    assert should_fire_at([1, 2]) is False


# ---------------------------------------------------------------------------
# run_cadence_step
# ---------------------------------------------------------------------------


def test_run_cadence_step_no_fire_when_threshold_missed(monkeypatch):
    _enable_all(monkeypatch)

    async def _fake_bridge():
        raise AssertionError("must not be called")

    async def _run():
        return await run_cadence_step(
            7, bridge_callable=_fake_bridge,
        )

    result = asyncio.run(_run())
    assert isinstance(result, CadenceStepResult)
    assert result.fired is False


def test_run_cadence_step_fires_at_multiple(monkeypatch):
    _enable_all(monkeypatch)
    monkeypatch.setenv(_N_OPS_FLAG, "5")
    called = []

    async def _fake_bridge():
        called.append(True)

        class _R:
            outcome = "no_op"
            def to_dict(self): return {}
        return _R()

    async def _run():
        return await run_cadence_step(
            10, bridge_callable=_fake_bridge,
        )

    result = asyncio.run(_run())
    assert result.fired is True
    assert called == [True]


def test_run_cadence_step_never_raises_on_bridge_crash(monkeypatch):
    _enable_all(monkeypatch)
    monkeypatch.setenv(_N_OPS_FLAG, "1")

    async def _crash_bridge():
        raise RuntimeError("simulated bridge crash")

    async def _run():
        return await run_cadence_step(
            1, bridge_callable=_crash_bridge,
        )

    result = asyncio.run(_run())
    assert result.fired is False
    assert "raised" in result.diagnostic


def test_run_cadence_step_garbage_op_count_no_fire(monkeypatch):
    _enable_all(monkeypatch)

    async def _bridge():
        raise AssertionError("must not be called")

    async def _run():
        return await run_cadence_step(
            "garbage", bridge_callable=_bridge,
        )

    result = asyncio.run(_run())
    assert result.fired is False


# ---------------------------------------------------------------------------
# sweep_pending_for_merge
# ---------------------------------------------------------------------------


def test_sweep_no_op_when_disabled():
    async def _run():
        return await sweep_pending_for_merge()

    result = asyncio.run(_run())
    assert isinstance(result, SweepResult)
    assert "disabled" in result.diagnostic


def test_sweep_transitions_merged_to_graduated(monkeypatch):
    _enable_all(monkeypatch)
    # Seed an AWAITING_APPROVAL row with a PR URL.
    append_proposal(StoredProposal(
        proposal_id="m10-sweep-merge",
        kind="new_observer",
        phase="awaiting_approval",
        pr_url="https://github.com/x/y/pull/1",
        pr_branch="ouroboros/m10/test",
    ))

    async def _fake_status(pr_url: str):
        return ("merged", "ok")

    async def _run():
        return await sweep_pending_for_merge(
            pr_status_callable=_fake_status,
        )

    result = asyncio.run(_run())
    assert result.ok is True
    assert len(result.transitions) == 1
    t = result.transitions[0]
    assert t.proposal_id == "m10-sweep-merge"
    assert t.from_phase == "awaiting_approval"
    assert t.to_phase == "graduated"


def test_sweep_transitions_closed_to_rejected(monkeypatch):
    _enable_all(monkeypatch)
    append_proposal(StoredProposal(
        proposal_id="m10-sweep-closed",
        kind="new_observer",
        phase="awaiting_approval",
        pr_url="https://github.com/x/y/pull/2",
    ))

    async def _fake_status(pr_url: str):
        return ("closed", "ok")

    async def _run():
        return await sweep_pending_for_merge(
            pr_status_callable=_fake_status,
        )

    result = asyncio.run(_run())
    assert len(result.transitions) == 1
    assert result.transitions[0].to_phase == "rejected"


def test_sweep_no_transition_on_open(monkeypatch):
    _enable_all(monkeypatch)
    append_proposal(StoredProposal(
        proposal_id="m10-sweep-open",
        kind="new_observer",
        phase="awaiting_approval",
        pr_url="https://github.com/x/y/pull/3",
    ))

    async def _fake_status(pr_url: str):
        return ("open", "ok")

    async def _run():
        return await sweep_pending_for_merge(
            pr_status_callable=_fake_status,
        )

    result = asyncio.run(_run())
    assert result.transitions == ()
    assert result.inspected_count >= 1


def test_sweep_skips_rows_without_pr_url(monkeypatch):
    _enable_all(monkeypatch)
    append_proposal(StoredProposal(
        proposal_id="m10-no-pr",
        kind="new_observer",
        phase="awaiting_approval",
        pr_url="",  # no PR
    ))
    called = []

    async def _fake_status(pr_url: str):
        called.append(pr_url)
        return ("merged", "ok")

    async def _run():
        return await sweep_pending_for_merge(
            pr_status_callable=_fake_status,
        )

    asyncio.run(_run())
    assert called == []  # no PR → no poll


def test_sweep_never_raises_on_status_crash(monkeypatch):
    _enable_all(monkeypatch)
    append_proposal(StoredProposal(
        proposal_id="m10-poll-crash",
        kind="new_observer",
        phase="awaiting_approval",
        pr_url="https://github.com/x/y/pull/4",
    ))

    async def _crash_status(pr_url: str):
        raise RuntimeError("simulated gh crash")

    async def _run():
        return await sweep_pending_for_merge(
            pr_status_callable=_crash_status,
        )

    # Doesn't raise — but the transition entry records the failure
    result = asyncio.run(_run())
    assert result.ok is True
    # Defensive transition row appended with poll-raised reason
    assert any(
        "raised" in t.reason for t in result.transitions
    )


# ---------------------------------------------------------------------------
# expire_stale_pending
# ---------------------------------------------------------------------------


def test_expire_no_op_when_disabled():
    async def _run():
        return await expire_stale_pending()

    result = asyncio.run(_run())
    assert "disabled" in result.diagnostic


def test_expire_transitions_stale_to_expired(monkeypatch):
    _enable_all(monkeypatch)
    # Stale: last_updated > 24h ago (default timeout).
    stale_ts = time.time() - (86400.0 + 10.0)
    append_proposal(StoredProposal(
        proposal_id="m10-stale",
        kind="new_observer",
        phase="awaiting_approval",
        pr_url="https://github.com/x/y/pull/5",
        last_updated_at_unix=stale_ts,
    ))

    async def _run():
        return await expire_stale_pending()

    result = asyncio.run(_run())
    assert len(result.transitions) == 1
    t = result.transitions[0]
    assert t.proposal_id == "m10-stale"
    assert t.to_phase == "expired"


def test_expire_skips_recent_rows(monkeypatch):
    _enable_all(monkeypatch)
    # Recent: last_updated 1 minute ago.
    recent_ts = time.time() - 60.0
    append_proposal(StoredProposal(
        proposal_id="m10-recent",
        kind="new_observer",
        phase="awaiting_approval",
        pr_url="https://github.com/x/y/pull/6",
        last_updated_at_unix=recent_ts,
    ))

    async def _run():
        return await expire_stale_pending()

    result = asyncio.run(_run())
    assert result.transitions == ()


def test_expire_skips_non_awaiting_phases(monkeypatch):
    _enable_all(monkeypatch)
    append_proposal(StoredProposal(
        proposal_id="m10-merged",
        kind="new_observer",
        phase="awaiting_merge",  # not AWAITING_APPROVAL
        pr_url="https://github.com/x/y/pull/7",
        last_updated_at_unix=time.time() - 100_000.0,
    ))

    async def _run():
        return await expire_stale_pending()

    result = asyncio.run(_run())
    assert result.transitions == ()


# ---------------------------------------------------------------------------
# Sync wrappers
# ---------------------------------------------------------------------------


def test_sweep_sync_no_loop():
    """Default-disabled → returns disabled diagnostic."""
    r = sweep_pending_for_merge_sync()
    assert isinstance(r, SweepResult)


def test_sweep_sync_inside_loop():
    async def _outer():
        return sweep_pending_for_merge_sync()

    r = asyncio.run(_outer())
    assert isinstance(r, SweepResult)


def test_expire_sync_no_loop():
    r = expire_stale_pending_sync()
    assert isinstance(r, SweepResult)


# ---------------------------------------------------------------------------
# SSE event registration
# ---------------------------------------------------------------------------


def test_phase_changed_event_constant_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED,
    )
    assert EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED == (
        "m10_proposal_phase_changed"
    )


def test_phase_changed_event_in_valid_frozenset():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED,
        _VALID_EVENT_TYPES,
    )
    assert (
        EVENT_TYPE_M10_PROPOSAL_PHASE_CHANGED
        in _VALID_EVENT_TYPES
    )


def test_sweep_publishes_phase_changed_event(monkeypatch):
    """End-to-end SSE publish on transition."""
    _enable_all(monkeypatch)
    append_proposal(StoredProposal(
        proposal_id="m10-sse-test",
        kind="new_observer",
        phase="awaiting_approval",
        pr_url="https://github.com/x/y/pull/99",
    ))

    captured = []

    def _fake_publish(event_type, op_id, payload=None):
        captured.append((event_type, op_id, payload))
        return f"evt-{len(captured)}"

    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    monkeypatch.setattr(ios, "publish_task_event", _fake_publish)

    async def _fake_status(pr_url: str):
        return ("merged", "ok")

    async def _run():
        return await sweep_pending_for_merge(
            pr_status_callable=_fake_status,
        )

    asyncio.run(_run())
    assert any(
        c[0] == "m10_proposal_phase_changed" for c in captured
    )


# ---------------------------------------------------------------------------
# REPL /m10 sweep + /m10 expire
# ---------------------------------------------------------------------------


def test_repl_sweep_dispatched(monkeypatch):
    monkeypatch.setenv(_M10_FLAG, "true")
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    r = dispatch_m10_command("/m10 sweep")
    assert r.matched is True
    assert "unknown subcommand" not in r.text


def test_repl_expire_dispatched(monkeypatch):
    monkeypatch.setenv(_M10_FLAG, "true")
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    r = dispatch_m10_command("/m10 expire")
    assert r.matched is True
    assert "unknown subcommand" not in r.text


def test_repl_help_lists_sweep_and_expire():
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    r = dispatch_m10_command("/m10 help")
    assert "sweep" in r.text
    assert "expire" in r.text


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_pins():
    pins = register_shipped_invariants()
    names = {p.invariant_name for p in pins}
    assert "m10_cadence_runner_entry_points" in names
    assert "m10_cadence_runner_composes_canonical" in names


def test_ast_pins_pass_on_current_source():
    pins = register_shipped_invariants()
    src_path = Path(
        "backend/core/ouroboros/governance/m10/cadence_runner.py"
    )
    source = src_path.read_text(encoding="utf-8")
    tree = _ast.parse(source)
    for pin in pins:
        violations = pin.validate(tree, source)
        assert violations == (), (
            f"{pin.invariant_name} drift: {violations}"
        )
