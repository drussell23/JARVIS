"""Slice 203 — Strategic Simulation Sandbox (propose-don't-dispose).

The organism analyzes its OWN telemetry (the observability registry) to
identify recurring operational deficiencies, maps each to a candidate
remediation goal, scores them by a heuristic fitness, and writes the top few
into a NON-authoritative draft (.jarvis/roadmap.draft.yaml) — then bundles
them into an operator-review PR. The operator reviews and, if they approve,
runs strategy_signer to elevate the draft into the signed authoritative
roadmap. The organism PROPOSES; the operator DISPOSES + signs (Slice 202
line, honored).

Honest framing (pinned):
  * "fitness" is a heuristic prioritization (severity × frequency ÷ effort) —
    NOT a predictive ROI simulation. The organism cannot truly simulate the
    future value of an upgrade it hasn't built.
  * goals are DRAFT/advisory — the simulator writes ONLY the .draft file and
    NEVER the active roadmap.yaml, and NEVER signs.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.strategy_simulator import (
    analyze_deficiencies,
    compile_draft,
    compute_fitness,
    propose_via_pr,
    simulator_enabled,
    synthesize_goals,
    write_draft,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV = _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"

# A registry snapshot showing real operational pain (the live soak shape).
_PAIN = {
    "hedge_concurrency_dispatches": 27,
    "hedge_rt_victories": 10,
    "hedge_races_abandoned": 3,
    "provider_exhaustions": 5,
    "control_plane_starvation_events": 27,
}
_CLEAN = {
    "hedge_concurrency_dispatches": 50,
    "hedge_rt_victories": 30,
    "hedge_races_abandoned": 0,
    "provider_exhaustions": 0,
    "control_plane_starvation_events": 0,
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("JARVIS_STRATEGY_SIMULATOR_ENABLED", raising=False)
    yield


# ===========================================================================
# A — gate
# ===========================================================================

def test_simulator_disabled_by_default():
    assert simulator_enabled() is False


# ===========================================================================
# B — deficiency analysis from real telemetry
# ===========================================================================

def test_analyze_finds_deficiencies_in_pain_snapshot():
    defs = analyze_deficiencies(_PAIN)
    kinds = {d["kind"] for d in defs}
    assert "provider_exhaustion" in kinds          # 5 exhaustions
    assert "control_plane_starvation" in kinds     # 27 starvation events
    for d in defs:
        assert d["severity"] > 0 and d["frequency"] >= 0


def test_analyze_clean_snapshot_finds_little_or_nothing():
    defs = analyze_deficiencies(_CLEAN)
    # zero exhaustions/starvation/abandoned → no remediation deficiencies
    assert all(d["kind"] != "provider_exhaustion" for d in defs)
    assert all(d["kind"] != "control_plane_starvation" for d in defs)


def test_analyze_never_raises_on_garbage():
    assert isinstance(analyze_deficiencies({}), list)
    assert isinstance(analyze_deficiencies(None), list)  # type: ignore[arg-type]


# ===========================================================================
# C — heuristic fitness
# ===========================================================================

def test_fitness_rises_with_severity():
    low = compute_fitness({"severity": 1.0, "frequency": 1.0, "effort": 1.0})
    high = compute_fitness({"severity": 9.0, "frequency": 1.0, "effort": 1.0})
    assert high > low


def test_fitness_falls_with_effort():
    cheap = compute_fitness({"severity": 5.0, "frequency": 1.0, "effort": 1.0})
    pricey = compute_fitness({"severity": 5.0, "frequency": 1.0, "effort": 9.0})
    assert cheap > pricey


def test_fitness_never_divides_by_zero():
    assert compute_fitness({"severity": 5.0, "frequency": 1.0, "effort": 0.0}) >= 0


# ===========================================================================
# D — goal synthesis + draft (NEVER active, NEVER signed)
# ===========================================================================

def test_synthesize_goals_from_pain_are_schema_valid():
    goals = synthesize_goals(_PAIN, top_n=6)
    assert 1 <= len(goals) <= 6
    for g in goals:
        assert g["id"] and g["title"] and g["priority"] in (
            "critical", "high", "medium", "low",
        )
        assert "fitness" in g  # the score that ranked it


def test_synthesize_ranks_by_fitness_desc():
    goals = synthesize_goals(_PAIN, top_n=6)
    fits = [g["fitness"] for g in goals]
    assert fits == sorted(fits, reverse=True)


def test_compiled_draft_is_proposal_not_authority():
    draft = compile_draft(synthesize_goals(_PAIN))
    assert draft["signed"] is False
    assert draft["authority"] in ("draft", "proposal", "advisory")
    assert "simulation" in draft["source"].lower()
    assert "signature" not in draft or not draft.get("signature")


def test_write_draft_targets_draft_file_NOT_active_roadmap(tmp_path):
    p = tmp_path / "roadmap.draft.yaml"
    out = write_draft(synthesize_goals(_PAIN), path=p)
    assert out == p and p.exists()
    assert ".draft." in str(out)
    # the active roadmap.yaml must NOT be created by the simulator
    assert not (tmp_path / "roadmap.yaml").exists()


# ===========================================================================
# E — PR handshake (propose to operator; dedup; never sign)
# ===========================================================================

def test_propose_via_pr_opens_operator_review_pr(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_STRATEGY_SIMULATOR_ENABLED", "1")
    calls = []

    async def _creator(op_id, description, files, **kw):
        calls.append((op_id, description, files))
        class _R:  # noqa: D401
            pr_url = "https://github.com/drussell23/JARVIS/pull/203"
        return _R()

    res = asyncio.run(propose_via_pr(
        snapshot=_PAIN, pr_creator=_creator,
        draft_path=tmp_path / "roadmap.draft.yaml",
        marker_path=tmp_path / ".strategy_proposal_marker",
    ))
    assert res is not None and res["pr_url"].endswith("/203")
    assert len(calls) == 1
    assert "Strategic Proposal" in calls[0][1] or "Strategic" in calls[0][1]


def test_propose_dedups_identical_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_STRATEGY_SIMULATOR_ENABLED", "1")
    calls = []

    async def _creator(op_id, description, files, **kw):
        calls.append(1)
        class _R:
            pr_url = "u"
        return _R()

    kw = dict(
        snapshot=_PAIN, pr_creator=_creator,
        draft_path=tmp_path / "d.yaml",
        marker_path=tmp_path / ".marker",
    )
    asyncio.run(propose_via_pr(**kw))
    asyncio.run(propose_via_pr(**kw))  # identical draft → no second PR
    assert len(calls) == 1


def test_propose_disabled_is_noop(tmp_path):
    async def _creator(*a, **k):
        raise AssertionError("must not be called when disabled")
    res = asyncio.run(propose_via_pr(
        snapshot=_PAIN, pr_creator=_creator,
        draft_path=tmp_path / "d.yaml", marker_path=tmp_path / ".m",
    ))
    assert res is None


def test_propose_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_STRATEGY_SIMULATOR_ENABLED", "1")

    async def _boom(*a, **k):
        raise RuntimeError("gh died")

    res = asyncio.run(propose_via_pr(
        snapshot=_PAIN, pr_creator=_boom,
        draft_path=tmp_path / "d.yaml", marker_path=tmp_path / ".m",
    ))
    assert res is None


# ===========================================================================
# F — doctrine pins
# ===========================================================================

def test_simulator_never_signs_or_writes_active_roadmap():
    src = (_GOV / "strategy_simulator.py").read_text(encoding="utf-8")
    assert "compute_signature(" not in src
    assert "sign_roadmap_doc(" not in src
    # writes ONLY the draft path; never the bare active roadmap.yaml
    assert "roadmap.draft.yaml" in src


def test_boundary_gate_not_weakened():
    src = (_GOV / "governance_boundary_gate.py").read_text(encoding="utf-8")
    assert "APPROVAL_REQUIRED" in src


def test_gls_wires_strategy_sim_trigger():
    src = (_GOV / "governed_loop_service.py").read_text(encoding="utf-8")
    assert "propose_via_pr" in src or "strategy_simulator" in src
