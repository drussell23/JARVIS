"""Slice 253 — Active Shadow Endorsement & HITL Gateway (decoupled).

Proves the bidirectional return channel: a trapped shadow action can be
RE-HYDRATED and executed for one specific ``action_id`` — bypassing the shadow
block for that single run — WITHOUT dropping the global
``JARVIS_RESILIENCE_SHADOW_MODE`` shield. These tests are decoupled from the
102K-line kernel (they exercise ``cybernetic_reanimation`` + the stream broker
with fakes); the kernel-chain proof lives in
``test_reanimation_kernel_wiring.py`` (sandbox-off).
"""
from __future__ import annotations

import asyncio
import os

import pytest

import backend.core.cybernetic_reanimation as cr
from backend.core.ouroboros.governance import ide_observability_stream as ios


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Fresh broker + empty pending-action registry + shadow ON for every test."""
    monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
    monkeypatch.setenv("JARVIS_RESILIENCE_SHADOW_MODE", "true")
    monkeypatch.delenv("JARVIS_SHADOW_PENDING_MAX", raising=False)
    monkeypatch.delenv("JARVIS_SHADOW_PENDING_TTL_S", raising=False)
    ios.reset_default_broker()
    cr.reset_pending_shadow_actions()
    yield
    cr.reset_pending_shadow_actions()
    ios.reset_default_broker()


def _events(event_type):
    return [
        e for e in ios.get_default_broker().recent_history(limit=100)
        if e.event_type == event_type
    ]


# ---------------------------------------------------------------------------
# Phase 1 — the endorsement payload (event type + publish wrapper)
# ---------------------------------------------------------------------------

class TestEndorsementEventType:
    def test_event_type_value(self):
        assert ios.EVENT_TYPE_ENDORSE_SHADOW_ACTION == "endorse_shadow_action"

    def test_event_type_registered(self):
        assert ios.EVENT_TYPE_ENDORSE_SHADOW_ACTION in ios._VALID_EVENT_TYPES

    def test_trapped_payload_carries_action_id(self):
        # The trap telemetry must carry the action_id so the Host can reference
        # the specific pending action when endorsing it.
        ios.publish_shadow_action_trapped(
            organ_name="SelfHealingOrchestrator",
            intended_action="execute remediation",
            action_id="shadow-000042",
        )
        evs = _events(ios.EVENT_TYPE_SHADOW_ACTION_TRAPPED)
        assert evs and evs[-1].payload["action_id"] == "shadow-000042"


class TestPublishEndorse:
    def test_publishes_structured_audit(self):
        eid = ios.publish_endorse_shadow_action(
            action_id="shadow-000007", organ_name="LoadSheddingController",
            outcome="executed",
        )
        assert eid is not None
        evs = _events(ios.EVENT_TYPE_ENDORSE_SHADOW_ACTION)
        assert len(evs) == 1
        p = evs[-1].payload
        assert p["action_id"] == "shadow-000007"
        assert p["organ_name"] == "LoadSheddingController"
        assert p["outcome"] == "executed"

    def test_returns_none_when_stream_disabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        assert ios.publish_endorse_shadow_action(action_id="x") is None

    def test_never_raises(self):
        # Garbage args must not raise (telemetry is best-effort).
        ios.publish_endorse_shadow_action(action_id=None, organ_name=object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Phase 2 — execution re-hydration (the heart)
# ---------------------------------------------------------------------------

class TestRehydration:
    def test_shadow_guard_stashes_pending_on_trap(self):
        ran = []
        rv = cr.shadow_guard("kill proc-9", lambda: ran.append("X"), organ="SHO")
        assert rv is cr.SHADOW_TRAPPED
        assert ran == []                                # NOT executed
        assert cr.pending_shadow_action_count() == 1    # but stashed for endorsement
        ids = cr.pending_shadow_action_ids()
        assert len(ids) == 1 and ids[0].startswith("shadow-")

    def test_trap_publishes_action_id_into_telemetry(self):
        cr.shadow_guard("kill proc-9", lambda: None, organ="SHO")
        aid = cr.pending_shadow_action_ids()[0]
        evs = _events(ios.EVENT_TYPE_SHADOW_ACTION_TRAPPED)
        assert evs and evs[-1].payload["action_id"] == aid

    def test_endorse_executes_the_trapped_action(self):
        ran = []
        cr.shadow_guard("kill proc-9", lambda: ran.append("KILLED") or "done", organ="SHO")
        aid = cr.pending_shadow_action_ids()[0]

        res = asyncio.run(cr.endorse_shadow_action(aid))

        assert res.status == "executed"
        assert res.result == "done"
        assert ran == ["KILLED"]                         # re-hydrated + executed
        assert cr.pending_shadow_action_count() == 0     # one-shot — consumed

    def test_endorse_does_not_drop_global_shadow_shield(self):
        # The crux: a single action runs, but the global shield stays UP.
        ran = []
        cr.shadow_guard("kill proc-9", lambda: ran.append("X"), organ="SHO")
        aid = cr.pending_shadow_action_ids()[0]
        assert cr.resilience_shadow_mode_enabled() is True

        asyncio.run(cr.endorse_shadow_action(aid))

        assert ran == ["X"]                              # endorsed action ran
        assert cr.resilience_shadow_mode_enabled() is True  # shield NEVER dropped
        # A NEW action is still trapped (shadow still gating everything else).
        ran2 = []
        cr.shadow_guard("kill proc-2", lambda: ran2.append("Y"), organ="SHO")
        assert ran2 == []

    def test_endorse_publishes_audit_event(self):
        cr.shadow_guard("kill proc-9", lambda: "ok", organ="SHO")
        aid = cr.pending_shadow_action_ids()[0]
        asyncio.run(cr.endorse_shadow_action(aid))
        evs = _events(ios.EVENT_TYPE_ENDORSE_SHADOW_ACTION)
        assert evs and evs[-1].payload["action_id"] == aid
        assert evs[-1].payload["outcome"] == "executed"

    def test_endorse_async_action(self):
        ran = []

        async def _kill():
            ran.append("ASYNC-KILLED")
            return "async-done"

        asyncio.run(cr.shadow_guard_async("kill proc-9", _kill, organ="SHO"))
        aid = cr.pending_shadow_action_ids()[0]

        res = asyncio.run(cr.endorse_shadow_action(aid))
        assert res.status == "executed"
        assert res.result == "async-done"
        assert ran == ["ASYNC-KILLED"]

    def test_endorse_unknown_id_is_not_found(self):
        res = asyncio.run(cr.endorse_shadow_action("shadow-999999"))
        assert res.status == "not_found"

    def test_endorse_is_one_shot(self):
        cr.shadow_guard("kill proc-9", lambda: "ok", organ="SHO")
        aid = cr.pending_shadow_action_ids()[0]
        asyncio.run(cr.endorse_shadow_action(aid))
        # second endorse of the same id cannot re-fire the action
        res2 = asyncio.run(cr.endorse_shadow_action(aid))
        assert res2.status == "not_found"

    def test_endorse_expired_does_not_execute(self):
        ran = []
        cr.shadow_guard("kill proc-9", lambda: ran.append("X"), organ="SHO")
        aid = cr.pending_shadow_action_ids()[0]
        # age the entry past any TTL
        cr._PENDING.entries[aid].created_monotonic = 0.0  # type: ignore[attr-defined]
        res = asyncio.run(cr.endorse_shadow_action(aid))
        assert res.status == "expired"
        assert ran == []                                 # stale kill must NOT fire

    def test_endorse_swallows_execute_error(self):
        def _boom():
            raise RuntimeError("kill failed")

        cr.shadow_guard("kill proc-9", _boom, organ="SHO")
        aid = cr.pending_shadow_action_ids()[0]
        res = asyncio.run(cr.endorse_shadow_action(aid))   # must NOT raise
        assert res.status == "error"

    def test_registry_is_bounded(self, monkeypatch):
        monkeypatch.setenv("JARVIS_SHADOW_PENDING_MAX", "2")
        for i in range(3):
            cr.shadow_guard(f"kill proc-{i}", lambda: None, organ="SHO")
        assert cr.pending_shadow_action_count() == 2     # oldest evicted, bounded


# ---------------------------------------------------------------------------
# Phase 3 — the CLI interceptor decision logic (transport-agnostic, TTY-free)
# ---------------------------------------------------------------------------

class TestCliInterceptorLogic:
    def test_prompt_describes_the_trapped_action(self):
        prompt = cr.endorsement_prompt_for({
            "organ_name": "SelfHealingOrchestrator",
            "intended_action": "execute remediation 'restart' on 'worker-7'",
            "action_id": "shadow-000003",
        })
        assert "SelfHealingOrchestrator" in prompt
        assert "execute remediation" in prompt
        assert "Y/N" in prompt

    def test_choice_yes_endorses_and_executes(self):
        ran = []
        cr.shadow_guard("kill proc-9", lambda: ran.append("X"), organ="SHO")
        aid = cr.pending_shadow_action_ids()[0]
        res = asyncio.run(cr.handle_endorsement_choice(aid, "y"))
        assert res.status == "executed"
        assert ran == ["X"]

    def test_choice_no_declines_without_executing(self):
        ran = []
        cr.shadow_guard("kill proc-9", lambda: ran.append("X"), organ="SHO")
        aid = cr.pending_shadow_action_ids()[0]
        res = asyncio.run(cr.handle_endorsement_choice(aid, "n"))
        assert res.status == "declined"
        assert ran == []                                 # decline never executes
        assert cr.pending_shadow_action_count() == 1     # left pending (until TTL)
