"""Features that were wired but could never run (2026-10-09 audit).

Each test pins the ROOT the feature was dead at, beside the guard that keeps
the revival from harming an unattended organism:

* AdversarialReviewer -- no provider was ever injected; reviewed nothing.
* DreamEngine -- refused a local-only host; and, alive, must yield the
  one-slot lane to live work (LaneIdleness + mid-call preemption).
* Self-critique / IntentDiscovery -- required a DoubleWord handle the RT gate
  never needed.
* ReviewBranch -- only the unreachable inline GATE called it; alive, an
  unattended process must skip, not expire every Yellow op into a reject.
* Envelope -- soak and cockpit differed in pipeline behaviour.
* Super agents -- read the operator's checkout instead of the session tree.
* Moltbook -- posts invisible in a headless soak.
* PerformanceRegression -- called a store method that never existed.
* Context budget -- the tool loop sized against a constant while the local
  transport fit to the negotiated window.
* subagent dispatch -- the round budget never reached the subagent.
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------- lane idleness


def test_stamped_op_set_records_every_mutation():
    from backend.core.ouroboros.governance.lane_idleness import StampedOpSet

    now = [100.0]
    ops = StampedOpSet(clock=lambda: now[0])
    for mutate in (
        lambda s: s.add("a"), lambda s: s.discard("a"), lambda s: s.update({"b"}),
        lambda s: s.__ior__({"c"}), lambda s: s.__isub__({"c"}), lambda s: s.clear(),
    ):
        now[0] += 1.0
        mutate(ops)
        assert ops.changed_at == now[0]


def test_lane_is_busy_with_an_op_or_another_generation_and_measures_quiet():
    from backend.core.ouroboros.governance.lane_idleness import LaneIdleness, StampedOpSet

    now = [0.0]
    ops = StampedOpSet(clock=lambda: now[0])
    inflight = [0]
    lane = LaneIdleness(ops, inflight=lambda: inflight[0], clock=lambda: now[0])
    ops.add("op-1")
    assert lane.busy() and lane.last_activity_s() == 0.0
    ops.discard("op-1")
    now[0] = 30.0
    assert not lane.busy() and lane.last_activity_s() == pytest.approx(30.0)
    inflight[0] = 1
    assert lane.busy()                          # someone else's generation
    assert not lane.busy(holding=1)             # ...or the caller's own


def test_unknowable_reads_as_busy():
    from backend.core.ouroboros.governance.lane_idleness import LaneIdleness

    def boom():
        raise RuntimeError("ledger gone")
    assert LaneIdleness(set(), inflight=boom).busy()


def test_run_unless_wanted_returns_the_result_when_left_alone(monkeypatch):
    from backend.core.ouroboros.governance import lane_idleness as li
    monkeypatch.setenv("JARVIS_LANE_YIELD_POLL_S", "0.01")

    class Gov:
        async def wanted_by_others(self, holding):
            return False

    async def work():
        await asyncio.sleep(0.05)
        return "done"
    assert asyncio.run(li.run_unless_wanted(work(), Gov())) == "done"


def test_run_unless_wanted_cancels_the_call_when_live_work_arrives(monkeypatch):
    from backend.core.ouroboros.governance import lane_idleness as li
    monkeypatch.setenv("JARVIS_LANE_YIELD_POLL_S", "0.01")
    seen = {}

    class Gov:
        async def wanted_by_others(self, holding):
            seen["holding"] = holding
            return True

    async def slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise

    async def scenario():
        with pytest.raises(li.Preempted):
            await li.run_unless_wanted(slow(), Gov(), holding=1)
    asyncio.run(scenario())
    assert seen == {"holding": 1, "cancelled": True}


def test_governed_loop_registers_its_op_set_as_the_default_lane():
    from backend.core.ouroboros.governance import governed_loop_service as gls
    src = inspect.getsource(gls)
    assert "self._active_ops: Set[str] = StampedOpSet()" in src
    assert "set_default_lane(LaneIdleness(self._active_ops))" in src


# ---------------------------------------------------------------- risk override


@pytest.mark.parametrize("override,start,expected", [
    ("APPROVAL_REQUIRED", "SAFE_AUTO", "APPROVAL_REQUIRED"),
    ("notify_apply", "SAFE_AUTO", "NOTIFY_APPLY"),
    ("SAFE_AUTO", "APPROVAL_REQUIRED", "APPROVAL_REQUIRED"),   # never weakens
    ("nonsense", "SAFE_AUTO", "SAFE_AUTO"),
    ("", "NOTIFY_APPLY", "NOTIFY_APPLY"),
])
def test_operator_risk_override(monkeypatch, override, start, expected):
    from backend.core.ouroboros.governance.risk_engine import RiskTier
    from backend.core.ouroboros.governance.risk_tier_floor import apply_operator_risk_override
    monkeypatch.setenv("JARVIS_RISK_CEILING", override)
    tier, _applied = apply_operator_risk_override(RiskTier[start])
    assert tier is RiskTier[expected]


def test_both_gate_paths_read_the_one_override():
    from backend.core.ouroboros.governance import orchestrator
    from backend.core.ouroboros.governance.phase_runners import gate_runner
    for mod in (orchestrator, gate_runner):
        src = inspect.getsource(mod)
        assert "apply_operator_risk_override" in src
        assert '"SAFE_AUTO": RiskTier.SAFE_AUTO,\n                "NOTIFY_APPLY"' not in src


# ---------------------------------------------------------- adversarial reviewer


class _AsyncProvider:
    def __init__(self, text):
        self.text, self.calls = text, 0

    async def review(self, prompt):
        from backend.core.ouroboros.governance.adversarial_reviewer_service import (
            ReviewProviderResult,
        )
        self.calls += 1
        return ReviewProviderResult(raw_response=self.text, cost_usd=0.0, model_used="local")


def _service(tmp_path):
    from backend.core.ouroboros.governance.adversarial_reviewer_service import (
        AdversarialReviewerService, _AdversarialAuditLedger,
    )
    try:
        ledger = _AdversarialAuditLedger(path=tmp_path / "adv.jsonl")
    except TypeError:
        ledger = _AdversarialAuditLedger()
    return AdversarialReviewerService(audit_ledger=ledger)


def test_reviewer_awaits_a_per_call_provider(tmp_path):
    svc = _service(tmp_path)
    prov = _AsyncProvider('{"findings": []}')
    review = asyncio.run(svc.areview_plan(
        op_id="op-1", plan_text="change foo", target_files=("a.py",),
        risk_tier_name="NOTIFY_APPLY", provider=prov,
    ))
    assert prov.calls == 1 and review.skip_reason != "no_provider"


def test_reviewer_without_any_provider_still_skips_cleanly(tmp_path):
    review = asyncio.run(_service(tmp_path).areview_plan(
        op_id="op-1", plan_text="x", risk_tier_name="NOTIFY_APPLY",
    ))
    assert review.skip_reason == "no_provider"


def test_hook_supplies_the_rt_gate_lane(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import adversarial_reviewer_provider as arp
    from backend.core.ouroboros.governance.adversarial_reviewer_hook import (
        areview_plan_for_generate_injection,
    )
    prov = _AsyncProvider('{"findings": []}')
    monkeypatch.setattr(arp, "default_review_provider", lambda: prov)
    asyncio.run(areview_plan_for_generate_injection(
        op_id="op-1", plan_text="plan", risk_tier_name="APPROVAL_REQUIRED",
        service=_service(tmp_path),
    ))
    assert prov.calls == 1


def test_rt_gate_provider_routes_through_the_gate(monkeypatch):
    from backend.core.ouroboros.governance import rt_gate
    from backend.core.ouroboros.governance.adversarial_reviewer_provider import RtGateReviewProvider
    seen = {}

    async def fake_gate(prompt, **kw):
        seen.update(kw)
        return "{}", "local"
    monkeypatch.setattr(rt_gate, "gate_completion_detailed", fake_gate)
    out = asyncio.run(RtGateReviewProvider().review("p"))
    assert seen["caller_id"] == "adversarial_reviewer"
    assert out.model_used == "local" and out.cost_usd == 0.0


def test_plan_runner_awaits_the_review_at_the_effective_tier():
    from backend.core.ouroboros.governance.phase_runners import plan_runner
    src = inspect.getsource(plan_runner)
    assert "await areview_plan_for_generate_injection(" in src
    assert "effective_risk_tier(" in src


def test_reviewer_service_stays_inside_its_io_cage():
    src = Path("backend/core/ouroboros/governance/adversarial_reviewer_service.py").read_text()
    assert "rt_gate" not in src and "providers import" not in src


# ----------------------------------------------------------------- dream engine


def test_dream_engine_counts_the_local_lane_as_a_backend(monkeypatch):
    from backend.core.ouroboros.consciousness import dream_engine as de
    from backend.core.ouroboros.governance import rt_gate
    monkeypatch.setattr(rt_gate, "local_tier_enabled", lambda: True)
    assert de._local_lane_enabled() is True


def test_dream_is_preempted_when_the_lane_is_wanted(monkeypatch):
    from backend.core.ouroboros.consciousness.dream_engine import DreamEngine
    monkeypatch.setenv("JARVIS_LANE_YIELD_POLL_S", "0.01")

    class Gov:
        async def wanted_by_others(self, holding):
            return True

    fake = SimpleNamespace(_resource_governor=Gov(), _preempted=asyncio.Event())

    async def slow():
        await asyncio.sleep(10)

    async def scenario():
        fake._preempted = asyncio.Event()
        out = await DreamEngine._unless_lane_wanted(fake, slow())
        return out, fake._preempted.is_set()
    assert asyncio.run(scenario()) == (None, True)


def test_harness_hands_the_dreamer_the_real_lane():
    from backend.core.ouroboros.battle_test import harness
    src = inspect.getsource(harness)
    assert "_ActivityStub" not in src and "_GovernorStub" not in src
    assert "activity_monitor=_lane" in src and "resource_governor=_lane" in src


# ------------------------------------------------- self-critique / intent / gate


def test_any_tier_available(monkeypatch):
    from backend.core.ouroboros.governance import paid_lanes, rt_gate
    monkeypatch.setattr(paid_lanes, "paid_lane_allowed", lambda n: False)
    monkeypatch.setattr(rt_gate, "local_tier_enabled", lambda: True)
    assert rt_gate.any_tier_available() and rt_gate.admitted_cloud_tiers() == ()
    monkeypatch.setattr(rt_gate, "local_tier_enabled", lambda: False)
    assert not rt_gate.any_tier_available()


def test_gate_critique_reports_the_answering_tier(monkeypatch):
    from backend.core.ouroboros.governance import rt_gate
    from backend.core.ouroboros.governance.self_critique import (
        GateCritiqueProvider, _critique_with_name,
    )

    async def fake_gate(prompt, **kw):
        return '{"rating": 4}', "local"
    monkeypatch.setattr(rt_gate, "gate_completion_detailed", fake_gate)
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.self_critique.build_critique_prompt",
        lambda req: "p",
    )
    req = SimpleNamespace(deadline_s=5.0)
    raw, name = asyncio.run(_critique_with_name(GateCritiqueProvider(), req))
    assert name == "local" and raw


def test_self_critique_wires_without_a_doubleword_handle():
    from backend.core.ouroboros.governance import governed_loop_service as gls
    src = inspect.getsource(gls)
    assert "if _dw_ref is not None or any_tier_available():" in src


def test_intent_discovery_has_no_model_name_and_yields_the_lane():
    from backend.core.ouroboros.governance.intake.sensors import intent_discovery_sensor as ids
    src = inspect.getsource(ids)
    assert "Qwen/" not in src
    assert "default_lane().busy()" in src and "run_unless_wanted(" in src
    assert "requires_human_ack=True" in src            # the safety invariant stays


# ---------------------------------------------------------------- review branch


def test_an_unattended_process_skips_review_instead_of_expiring(monkeypatch):
    from backend.core.ouroboros.governance import review_coordinator as rc
    from backend.core.ouroboros.governance.attention_ledger import reset_attention_ledger_for_tests
    reset_attention_ledger_for_tests()
    monkeypatch.setenv("JARVIS_REVIEW_BRANCH_ENABLED", "true")
    monkeypatch.setenv("JARVIS_REVIEW_TIMEOUT_S", "900")
    coord = rc.ReviewCoordinator()
    out = asyncio.run(coord.coordinate_review("op-1", [("a.py", "x=1\n")], risk_tier="notify_apply"))
    assert out.decision is rc.ReviewDecision.SKIPPED
    assert "unattended" in (out.error or "")


def test_notify_apply_review_maps_decisions(monkeypatch):
    from backend.core.ouroboros.governance import review_coordinator as rc
    monkeypatch.setenv("JARVIS_REVIEW_BRANCH_ENABLED", "true")

    class Coord:
        branch_manager = object()

        def __init__(self, decision):
            self.decision = decision

        async def coordinate_review(self, *a, **k):
            return SimpleNamespace(decision=self.decision, elapsed_s=1.0)

    change = SimpleNamespace(path="a.py", new_content="x", status="modified")
    run = lambda d: asyncio.run(rc.coordinate_notify_apply_review(  # noqa: E731
        Path("."), "op", [change], coordinator=Coord(d)))
    assert run(rc.ReviewDecision.ACCEPTED) == (False, rc.USER_REJECTED_REASON)
    assert run(rc.ReviewDecision.REJECTED) == (True, rc.USER_REJECTED_REASON)
    assert run(rc.ReviewDecision.EXPIRED) == (True, rc.REVIEW_EXPIRED_REASON)
    assert run(rc.ReviewDecision.SKIPPED) is None


def test_the_shipping_gate_path_calls_the_review():
    from backend.core.ouroboros.governance.phase_runners import gate_runner
    assert "coordinate_notify_apply_review(" in inspect.getsource(gate_runner)


# --------------------------------------------------------------------- envelope


def test_profiles_differ_only_in_presentation():
    """Given the SAME session budget, the two profiles differ only in
    presentation: what the organism does is identical. (Their DEFAULT session
    lengths differ, and every op budget is derived from the session length, so
    comparing defaults would compare two budgets, not two organisms.)"""
    from backend.core.ouroboros.governance.production_envelope import (
        PRESENTATION_KEYS, PROFILES, build,
    )
    wall, idle, cost = PROFILES["soak"]
    soak = build("soak", wall_s=wall, idle_timeout_s=idle, cost_cap_usd=cost).values
    cockpit = build("cockpit", wall_s=wall, idle_timeout_s=idle, cost_cap_usd=cost).values
    differing = {k for k in set(soak) | set(cockpit) if soak.get(k) != cockpit.get(k)}
    assert differing <= PRESENTATION_KEYS, differing - PRESENTATION_KEYS


@pytest.mark.parametrize("key", [
    "JARVIS_SWARM_ROUTING_ENABLED", "JARVIS_L2_SYMBOL_SCOPED_ENABLED",
    "JARVIS_REVIEW_BRANCH_ENABLED", "JARVIS_LAST_SESSION_SUMMARY_ENABLED",
])
def test_pipeline_flags_are_in_the_shared_core(key):
    from backend.core.ouroboros.governance.production_envelope import build
    assert key in build("soak").values and key in build("cockpit").values


# ------------------------------------------------------------------ super agents


def test_generator_reads_the_execution_root(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    from backend.core.ouroboros.governance.candidate_generator import CandidateGenerator
    worktree = tmp_path / "wt"
    worktree.mkdir()
    monkeypatch.setattr(aw, "effective_execution_root", lambda root: worktree)
    gen = CandidateGenerator.__new__(CandidateGenerator)
    gen._repo_root = tmp_path
    assert gen._execution_root() == str(worktree)


def test_an_invalid_workspace_declines_rather_than_reading_the_operator_tree(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance import autonomous_workspace as aw
    from backend.core.ouroboros.governance.candidate_generator import CandidateGenerator

    def invalid(root):
        raise aw.ExecutionRootInvalid("armed but broken")
    monkeypatch.setattr(aw, "effective_execution_root", invalid)
    gen = CandidateGenerator.__new__(CandidateGenerator)
    gen._repo_root = tmp_path
    (tmp_path / "a.py").write_text("x = 1\n")
    assert gen._read_source_for_swarm("a.py") is None
    assert gen._execution_root_or_none() is None


def test_governed_loop_gives_the_generator_its_root():
    from backend.core.ouroboros.governance import governed_loop_service as gls
    assert "repo_root=self._config.project_root," in inspect.getsource(gls)


# ---------------------------------------------------------------------- moltbook


def test_a_molt_post_reaches_the_session_log(tmp_path, monkeypatch, caplog):
    from backend.core.ouroboros.governance import moltbook as mb
    if not hasattr(mb, "post_molt"):
        pytest.skip("post API renamed")
    src = inspect.getsource(mb.post_molt)
    assert 'logger.info(\n            "[Moltbook] %s @%s/%s op=%s: %s"' in src


# ------------------------------------------------------------ perf regression


def test_perf_sensor_reads_through_the_store_api(monkeypatch):
    from backend.core.ouroboros.governance.intake.sensors import performance_regression_sensor as prs
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(prs.PerformanceRegressionSensor.scan_once)))
    # Calls, not text: the comment explaining the fix names `get_all()`.
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "get_all" not in called
    assert {"load_records", "get_records_by_task"} <= called


def test_record_epoch_orders_mixed_timestamps():
    from datetime import datetime, timezone
    from backend.core.ouroboros.governance.intake.sensors.performance_regression_sensor import (
        _record_epoch,
    )
    recs = [SimpleNamespace(timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc)),
            SimpleNamespace(timestamp=None), SimpleNamespace(timestamp=5.0)]
    assert sorted(recs, key=_record_epoch)[0].timestamp is None


# ------------------------------------------------------------- context budget


def test_context_budget_scales_with_the_window_and_keeps_ratios():
    from backend.core.ouroboros.governance import tool_executor as te
    default = te.context_budget_for(None)
    small = te.context_budget_for(default.max_chars // 2)
    assert small.max_chars == default.max_chars // 2
    assert small.compact_chars / small.max_chars == pytest.approx(
        default.compact_chars / default.max_chars, abs=1e-3)
    assert te.context_budget_for(default.max_chars * 4).max_chars == default.max_chars


def test_prompt_window_uses_the_transports_fitter():
    from backend.core.ouroboros.governance.local_inference_director import (
        PromptWindow, estimate_tokens, input_budget_tokens, prompt_window_for,
    )
    win = PromptWindow(input_tokens=input_budget_tokens(4096))
    big = "a" * (win.max_chars * 3)
    assert estimate_tokens(win.fit(big)) <= win.input_tokens
    client = SimpleNamespace(_cfg=SimpleNamespace(num_ctx=4096))
    assert prompt_window_for(client).input_tokens == win.input_tokens
    assert prompt_window_for(SimpleNamespace()) is None


def test_the_loop_fits_instead_of_aborting_when_a_window_is_known():
    from backend.core.ouroboros.governance import tool_executor as te
    src = inspect.getsource(te.ToolLoopCoordinator.run)
    assert "prompt_window.fit(current_prompt)" in src
    assert "context_budget_for(getattr(prompt_window" in src


# -------------------------------------------------------------- subagent deadline


def test_subagent_receives_the_round_budget_as_its_deadline():
    from backend.core.ouroboros.governance import tool_executor as te
    src = inspect.getsource(te)
    assert "pipeline_deadline=_dt.now(_tz.utc) + _td(seconds=max(0.0, float(timeout)))" in src


# -------------------------------------------------------------- epistemic planner


def test_planner_reports_what_the_bridge_did():
    from backend.core.ouroboros.governance import epistemic_planner as ep
    src = inspect.getsource(ep)
    assert "return enqueued" in src and "0 routed" in src
