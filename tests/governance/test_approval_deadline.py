"""The Iron Gate must not wait for a human forever.

``_race_gate_answer`` used to pass ``timeout=None`` whenever a local [Y/n]
prompt was alive — the docstring said so deliberately: "No deadline declared...
This gate has none while a local prompt is alive." With the cockpit now running
production budgets and a background pool executing roadmap ops, an operator who
walks away from a live prompt holds a worker for the life of the process, and
with a pool of six that is the whole pool, one op at a time.

The gate is now bounded, fails CLOSED, and records why.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.inline_approval import (
    DEFAULT_APPROVAL_DEADLINE_S,
    approval_deadline_s,
    record_approval_timeout,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("JARVIS_APPROVAL_DEADLINE_S", raising=False)


# --------------------------------------------------------------------------
# The policy
# --------------------------------------------------------------------------

def test_unset_is_bounded_not_unbounded():
    """The failure being prevented is precisely nobody having chosen, so the
    default must be a bound — not 'wait forever until someone configures it'."""
    assert approval_deadline_s() == DEFAULT_APPROVAL_DEADLINE_S


def test_zero_is_the_explicit_opt_out(monkeypatch):
    monkeypatch.setenv("JARVIS_APPROVAL_DEADLINE_S", "0")
    assert approval_deadline_s() is None


@pytest.mark.parametrize("raw,expected", [("60", 60.0), ("900", 900.0), ("1", 5.0)])
def test_an_explicit_value_is_honoured_with_a_floor(monkeypatch, raw, expected):
    monkeypatch.setenv("JARVIS_APPROVAL_DEADLINE_S", raw)
    assert approval_deadline_s() == expected


@pytest.mark.parametrize("raw", ["banana", "", "   "])
def test_an_unparseable_value_falls_back_to_the_bound(monkeypatch, raw):
    monkeypatch.setenv("JARVIS_APPROVAL_DEADLINE_S", raw)
    assert approval_deadline_s() == DEFAULT_APPROVAL_DEADLINE_S


def test_the_envelope_keeps_the_deadline_under_the_pipeline_budget():
    """If the gate outlived the pipeline clock, the op would be killed from
    underneath it and the timeout row would never be written."""
    from backend.core.ouroboros.governance.production_envelope import build

    for profile in ("soak", "cockpit"):
        e = build(profile)
        assert e.approval_deadline_s < e.pipeline_timeout_s


# --------------------------------------------------------------------------
# The ledger row
# --------------------------------------------------------------------------

def test_a_timeout_is_recorded_to_the_op_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_LEDGER_DIR", str(tmp_path))
    assert record_approval_timeout(
        "op-test-approval-1", waited_s=1800.0, target_files=("a.py",),
    ) is True
    rows = []
    for path in tmp_path.rglob("*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    assert rows, "no ledger row was written"
    row = rows[-1]
    assert row["op_id"] == "op-test-approval-1"
    assert row["state"] == "blocked"
    assert row["data"]["reason"] == "approval_timeout"
    assert row["data"]["waited_s"] == 1800.0
    assert row["data"]["target_files"] == ["a.py"]


def test_recording_never_raises_even_on_a_hostile_dir(monkeypatch):
    """A gate that cannot record its refusal must still refuse."""
    monkeypatch.setenv("OUROBOROS_LEDGER_DIR", "/proc/definitely/not/writable")
    assert record_approval_timeout("op-x", waited_s=1.0) in (True, False)


# --------------------------------------------------------------------------
# The gate itself — it must TERMINATE, and it must refuse
# --------------------------------------------------------------------------

class _Flow:
    """The gate method under test, lifted onto a minimal host.

    Bound off the real class so the test exercises the SHIPPING code path,
    not a re-implementation of it.
    """

    def __init__(self):
        from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow

        self._race = SerpentFlow._race_gate_answer.__get__(self, _Flow)
        self._gate_answered_via_cockpit = False
        self._gate_timed_out = False


def test_the_gate_gives_up_and_refuses_when_nobody_answers(monkeypatch):
    """A never-resolving bridge future with no local prompt: before this
    change, with a live local surface, the wait was unbounded. The assertion
    that matters is simply that this call RETURNS."""
    monkeypatch.setenv("JARVIS_APPROVAL_DEADLINE_S", "5")

    async def _run():
        flow = _Flow()
        never = asyncio.get_running_loop().create_future()
        started = time.monotonic()
        decision = await asyncio.wait_for(flow._race(never), timeout=30)
        return decision, time.monotonic() - started, flow

    decision, elapsed, flow = asyncio.run(_run())
    assert decision is not None, "the gate returned None — it did not refuse"
    assert decision.choice.name == "REJECT", "a timeout must fail CLOSED"
    assert flow._gate_timed_out is True
    assert elapsed < 25, "the gate did not honour its deadline"


def test_the_timeout_decision_can_never_be_replayed_as_operator_intent(monkeypatch):
    """SYNTHETIC provenance is what stops 'nobody answered' being stored and
    quoted back to the model as something the human wanted."""
    monkeypatch.setenv("JARVIS_APPROVAL_DEADLINE_S", "5")

    async def _run():
        flow = _Flow()
        never = asyncio.get_running_loop().create_future()
        return await asyncio.wait_for(flow._race(never), timeout=30)

    decision = asyncio.run(_run())
    assert getattr(decision, "is_stated", False) is False
    prov = getattr(decision, "provenance", None)
    assert "synthetic" in str(getattr(prov, "value", prov)).lower()


def test_the_deadline_is_absolute_not_restarted_per_surface(monkeypatch):
    """The loop re-enters asyncio.wait every time a surface dies. A relative
    timeout would restart the clock each time and the gate would still hang,
    just less obviously."""
    import inspect

    from backend.core.ouroboros.battle_test import serpent_flow

    src = inspect.getsource(serpent_flow.SerpentFlow._race_gate_answer)
    assert "_gate_expiry" in src
    assert "time.monotonic() +" in src, "deadline is not an absolute instant"
    # every in-loop reassignment must clamp to what remains of the budget
    assert "deadline = _bridge_only_wait_s()" not in src, (
        "an in-loop reassignment bypasses the gate budget"
    )
