"""FSM phase SSE emission (Part C of the final A1 audit fix).

`fsm_classify_to_applied` is SSE-only: the auditor learns CLASSIFY/APPLY phases
ONLY from `fsm_phase_changed` events. But `publish_fsm_phase` had ZERO call sites
-> the criterion was structurally always False in the live path. `publish_fsm_phase_for_ctx`
is the ctx-aware wrapper the orchestrator seams call to emit those events.
"""
from __future__ import annotations

import backend.core.ouroboros.governance.ide_observability_stream as ios


class _Ctx:
    def __init__(self, op_id="op-abc123", route="standard", risk_tier="notify_apply"):
        self.op_id = op_id
        self.route = route
        self.risk_tier = risk_tier


def test_emits_phase_with_ctx_fields(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ios, "publish_fsm_phase",
        lambda op_id, phase, route, risk_tier: calls.append((op_id, phase, route, risk_tier)),
    )
    monkeypatch.setenv("JARVIS_FSM_PHASE_SSE_ENABLED", "true")

    ios.publish_fsm_phase_for_ctx(_Ctx(), "CLASSIFY")

    assert calls == [("op-abc123", "CLASSIFY", "standard", "notify_apply")]


def test_gated_off_emits_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(ios, "publish_fsm_phase", lambda *a, **k: calls.append(a))
    monkeypatch.setenv("JARVIS_FSM_PHASE_SSE_ENABLED", "false")

    ios.publish_fsm_phase_for_ctx(_Ctx(), "APPLY")

    assert calls == []


def test_fail_soft_on_bad_ctx(monkeypatch):
    # Missing attributes / publish raising -> NEVER raises into the pipeline.
    monkeypatch.setenv("JARVIS_FSM_PHASE_SSE_ENABLED", "true")

    def _boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(ios, "publish_fsm_phase", _boom)
    ios.publish_fsm_phase_for_ctx(object(), "CLASSIFY")  # no op_id, publish raises -> no exception


def test_missing_optional_fields_default_to_empty(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ios, "publish_fsm_phase",
        lambda op_id, phase, route, risk_tier: calls.append((op_id, phase, route, risk_tier)),
    )
    monkeypatch.setenv("JARVIS_FSM_PHASE_SSE_ENABLED", "true")

    class _Bare:
        op_id = "op-bare"

    ios.publish_fsm_phase_for_ctx(_Bare(), "APPLY")

    assert calls == [("op-bare", "APPLY", "", "")]
