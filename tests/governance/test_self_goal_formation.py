"""P1 Slice 2 — SelfGoalFormationEngine regression suite.

Pins the entire decision tree of the LLM-driving cognition layer per
PRD §9 P1. Every gate has at least one positive + one negative test;
runaway-prevention safety gates have multiple.

Sections:
    (A) Master flag — env reader + engine respects flag
    (B) Posture veto — EXPLORE/CONSOLIDATE proceed, HARDEN/MAINTAIN block
    (C) Per-session cap — strict cap on emissions, configurable
    (D) Cost cap — accumulator, refusal at cap, configurable
    (E) Cluster discovery — no-clusters short-circuit, blocklist filter
    (F) Model-caller integration — happy path, exception swallow,
        empty response, malformed JSON, markdown fence tolerance
    (G) ProposalDraft schema invariants
    (H) Persistence — JSONL ledger append, write-failure short-circuit
    (I) Default-singleton accessor
    (J) Authority invariants — banned imports + side-effect surface
    (K) Telemetry — INFO marker fires per PRD contract
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List, Tuple
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.postmortem_recall import PostmortemRecord
from backend.core.ouroboros.governance.posture import Posture
from backend.core.ouroboros.governance.self_goal_formation import (
    DEFAULT_COST_CAP_USD,
    DEFAULT_PER_SESSION_CAP,
    PROPOSAL_SCHEMA_VERSION,
    ProposalDraft,
    SelfGoalFormationEngine,
    cost_cap_usd,
    get_default_engine,
    is_enabled,
    min_cluster_size_override,
    per_session_cap,
    reset_default_engine,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        "JARVIS_SELF_GOAL_FORMATION_ENABLED",
        "JARVIS_SELF_GOAL_PER_SESSION_CAP",
        "JARVIS_SELF_GOAL_COST_CAP_USD",
        "JARVIS_SELF_GOAL_MIN_CLUSTER_SIZE",
    ):
        monkeypatch.delenv(k, raising=False)
    reset_default_engine()
    yield
    reset_default_engine()


def _enable(monkeypatch, **overrides):
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    for k, v in overrides.items():
        monkeypatch.setenv(f"JARVIS_SELF_GOAL_{k}", str(v))


def _record(
    op_id: str,
    *,
    root_cause: str = "all_providers_exhausted:fallback_failed",
    failed_phase: str = "GENERATE",
    timestamp_unix: float = 1_700_000_000.0,
    target_files: tuple = ("a.py",),
) -> PostmortemRecord:
    return PostmortemRecord(
        op_id=op_id,
        session_id="s1",
        root_cause=root_cause,
        failed_phase=failed_phase,
        next_safe_action="retry_with_smaller_seed",
        target_files=target_files,
        timestamp_iso="2026-04-26T10:00:00",
        timestamp_unix=timestamp_unix,
    )


def _three_records(**kw) -> List[PostmortemRecord]:
    return [
        _record(op_id=f"op{i}", timestamp_unix=1_700_000_000.0 + i * 3600.0, **kw)
        for i in range(3)
    ]


def _stub_model(
    description: str = "Investigate recurring failure pattern",
    rationale: str = "3 ops failed; investigate fallback path.",
    cost: float = 0.05,
):
    payload = json.dumps({"description": description, "rationale": rationale})
    return lambda prompt, max_cost: (payload, cost)


def _fresh_engine(tmp_path: Path) -> SelfGoalFormationEngine:
    return SelfGoalFormationEngine(
        project_root=tmp_path,
        ledger_path=tmp_path / "ledger.jsonl",
    )


# ---------------------------------------------------------------------------
# (A) Master flag
# ---------------------------------------------------------------------------


def test_master_flag_default_true_post_graduation(monkeypatch):
    """JARVIS_SELF_GOAL_FORMATION_ENABLED defaults True post-graduation
    (P1 Slice 5, 2026-04-26). Hot-revert: set env to "false".

    If this test fails AND P1 has been intentionally rolled back: rename
    to test_master_flag_default_false (and flip the assertion + the
    source-grep pin) per the same discipline P0/P0.5 used."""
    monkeypatch.delenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", raising=False)
    assert is_enabled() is True


def test_master_flag_explicit_false_hot_revert(monkeypatch):
    """Hot-revert path: explicit false disables engine post-graduation."""
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "false")
    assert is_enabled() is False


def test_master_flag_explicit_true(monkeypatch):
    """Idempotent: explicit true matches the new default."""
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "true")
    assert is_enabled() is True


def test_master_flag_off_engine_returns_none(tmp_path, monkeypatch):
    """Hot-revert pin: explicit false → engine.evaluate short-circuits."""
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "false")
    eng = _fresh_engine(tmp_path)
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    assert out is None


# ---------------------------------------------------------------------------
# (B) Posture veto
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("posture", [Posture.EXPLORE, Posture.CONSOLIDATE])
def test_permitted_postures_proceed(monkeypatch, tmp_path, posture):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=posture,
        model_caller=_stub_model(),
    )
    assert out is not None
    assert out.posture_at_proposal == posture.value


@pytest.mark.parametrize("posture", [Posture.HARDEN, Posture.MAINTAIN])
def test_blocked_postures_short_circuit(monkeypatch, tmp_path, posture):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=posture,
        model_caller=_stub_model(),
    )
    assert out is None
    assert eng.cost_spent_usd == 0.0  # model never called


# ---------------------------------------------------------------------------
# (C) Per-session cap
# ---------------------------------------------------------------------------


def test_default_per_session_cap_is_1():
    assert per_session_cap() == 1
    assert DEFAULT_PER_SESSION_CAP == 1


def test_per_session_cap_blocks_second_call(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    first = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    second = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    assert first is not None
    assert second is None
    assert eng.proposals_emitted == 1


def test_per_session_cap_override(monkeypatch, tmp_path):
    _enable(monkeypatch, PER_SESSION_CAP=3, COST_CAP_USD=10.0)
    eng = _fresh_engine(tmp_path)
    # 3 evaluate calls with distinct cluster signatures so blocklist
    # doesn't drop them.
    for i, phase in enumerate(("GENERATE", "VALIDATE", "APPLY")):
        out = eng.evaluate(
            postmortems=_three_records(failed_phase=phase),
            posture=Posture.EXPLORE,
            model_caller=_stub_model(),
        )
        assert out is not None, f"call {i+1} unexpectedly short-circuited"
    assert eng.proposals_emitted == 3
    # 4th call must hit the cap.
    out4 = eng.evaluate(
        postmortems=_three_records(failed_phase="VERIFY"),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    assert out4 is None


def test_per_session_cap_zero_disables(monkeypatch, tmp_path):
    _enable(monkeypatch, PER_SESSION_CAP=0)
    eng = _fresh_engine(tmp_path)
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    assert out is None


def test_per_session_cap_negative_clamps_to_zero(monkeypatch):
    monkeypatch.setenv("JARVIS_SELF_GOAL_PER_SESSION_CAP", "-99")
    assert per_session_cap() == 0


# ---------------------------------------------------------------------------
# (D) Cost cap
# ---------------------------------------------------------------------------


def test_default_cost_cap_is_010():
    assert cost_cap_usd() == 0.10
    assert DEFAULT_COST_CAP_USD == 0.10


def test_cost_accumulates_across_calls(monkeypatch, tmp_path):
    _enable(monkeypatch, PER_SESSION_CAP=3)
    eng = _fresh_engine(tmp_path)
    eng.evaluate(
        postmortems=_three_records(failed_phase="A"),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(cost=0.04),
    )
    eng.evaluate(
        postmortems=_three_records(failed_phase="B"),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(cost=0.03),
    )
    assert eng.cost_spent_usd == pytest.approx(0.07)


def test_cost_cap_blocks_call_when_exhausted(monkeypatch, tmp_path):
    _enable(monkeypatch, PER_SESSION_CAP=5, COST_CAP_USD=0.05)
    eng = _fresh_engine(tmp_path)
    # First call spends 0.05 → cap hit exactly.
    eng.evaluate(
        postmortems=_three_records(failed_phase="A"),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(cost=0.05),
    )
    assert eng.cost_spent_usd == pytest.approx(0.05)
    # Second call must short-circuit at cost cap, not call the model.
    called = []

    def watch_caller(prompt, max_cost):
        called.append(max_cost)
        return ("{}", 0.0)

    out = eng.evaluate(
        postmortems=_three_records(failed_phase="B"),
        posture=Posture.EXPLORE,
        model_caller=watch_caller,
    )
    assert out is None
    assert called == []  # model_caller never invoked


def test_cost_cap_passes_remaining_budget_to_caller(monkeypatch, tmp_path):
    _enable(monkeypatch, COST_CAP_USD=0.10)
    eng = _fresh_engine(tmp_path)
    captured = []

    def cap_aware(prompt, max_cost):
        captured.append(max_cost)
        return (
            json.dumps({"description": "x", "rationale": "y"}),
            0.02,
        )

    eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=cap_aware,
    )
    # First call gets full budget.
    assert captured == [pytest.approx(0.10)]


def test_cost_cap_negative_clamps_to_zero(monkeypatch):
    monkeypatch.setenv("JARVIS_SELF_GOAL_COST_CAP_USD", "-1.0")
    assert cost_cap_usd() == 0.0


# ---------------------------------------------------------------------------
# (E) Cluster discovery + blocklist filter
# ---------------------------------------------------------------------------


def test_no_clusters_short_circuits(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    # Only 2 records — below default min_cluster_size of 3.
    recs = [
        _record("op1", timestamp_unix=1_700_000_000.0),
        _record("op2", timestamp_unix=1_700_003_600.0),
    ]
    out = eng.evaluate(
        postmortems=recs,
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    assert out is None
    assert eng.cost_spent_usd == 0.0


def test_blocklist_filters_proposed_signatures(monkeypatch, tmp_path):
    """When the operator passes a blocklist that includes the cluster's
    signature_hash, the engine must short-circuit without calling the model."""
    _enable(monkeypatch)
    from backend.core.ouroboros.governance.postmortem_clusterer import (
        cluster_postmortems,
    )
    recs = _three_records()
    expected_hash = cluster_postmortems(recs)[0].signature.signature_hash()
    eng = _fresh_engine(tmp_path)
    out = eng.evaluate(
        postmortems=recs,
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
        blocklist_hashes=[expected_hash],
    )
    assert out is None


def test_inprocess_blocklist_self_dedup(monkeypatch, tmp_path):
    """A second evaluate with the same cluster signature must short-circuit
    via the in-process blocklist (so even with PER_SESSION_CAP=2 we don't
    propose the same signature twice)."""
    _enable(monkeypatch, PER_SESSION_CAP=5)
    eng = _fresh_engine(tmp_path)
    out1 = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    out2 = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    assert out1 is not None
    assert out2 is None  # in-process blocklist dedup'd it


def test_min_cluster_size_override_env(monkeypatch):
    monkeypatch.setenv("JARVIS_SELF_GOAL_MIN_CLUSTER_SIZE", "5")
    assert min_cluster_size_override() == 5


# ---------------------------------------------------------------------------
# (F) Model-caller integration
# ---------------------------------------------------------------------------


def test_model_caller_exception_returns_none(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)

    def boom(prompt, max_cost):
        raise RuntimeError("test boom")

    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=boom,
    )
    assert out is None
    # Engine never raises + counter not incremented.
    assert eng.proposals_emitted == 0


def test_model_caller_empty_response_short_circuits(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=lambda p, m: ("", 0.01),
    )
    assert out is None


def test_model_caller_malformed_json_short_circuits(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=lambda p, m: ("not even close to json", 0.01),
    )
    assert out is None


def test_model_caller_markdown_fence_is_tolerated(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    fenced = (
        "```json\n"
        '{"description": "Fenced description", "rationale": "Fenced rationale."}\n'
        "```"
    )
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=lambda p, m: (fenced, 0.01),
    )
    assert out is not None
    assert out.description == "Fenced description"


def test_model_caller_prose_around_json_is_tolerated(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    response = (
        "Sure — here's my proposal:\n"
        '{"description": "X", "rationale": "Y"}\n'
        "Hope that helps."
    )
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=lambda p, m: (response, 0.01),
    )
    assert out is not None
    assert out.description == "X"


def test_model_caller_negative_cost_clamped(monkeypatch, tmp_path):
    """Defensive: a buggy caller returning negative cost must not corrupt
    the accumulator."""
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=lambda p, m: (
            json.dumps({"description": "x", "rationale": "y"}),
            -5.0,
        ),
    )
    assert eng.cost_spent_usd == 0.0


# ---------------------------------------------------------------------------
# (G) ProposalDraft schema invariants
# ---------------------------------------------------------------------------


def test_proposal_schema_version_is_frozen():
    assert PROPOSAL_SCHEMA_VERSION == "self_goal_formation.1"


def test_proposal_draft_carries_signature_hash(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    assert out is not None
    assert len(out.signature_hash) == 12  # sha256[:12]
    assert out.auto_proposed is True
    assert out.cluster_member_count == 3


def test_proposal_draft_to_ledger_dict_jsonable(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    out = eng.evaluate(
        postmortems=_three_records(),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    assert out is not None
    d = out.to_ledger_dict()
    # Must serialize cleanly (no tuples, no enums leaking).
    serialized = json.dumps(d)
    assert "self_goal_formation.1" in serialized


# ---------------------------------------------------------------------------
# (H) Persistence
# ---------------------------------------------------------------------------


def test_ledger_appends_one_line_per_proposal(monkeypatch, tmp_path):
    _enable(monkeypatch, PER_SESSION_CAP=3, COST_CAP_USD=10.0)
    eng = _fresh_engine(tmp_path)
    for phase in ("GENERATE", "VALIDATE", "APPLY"):
        eng.evaluate(
            postmortems=_three_records(failed_phase=phase),
            posture=Posture.EXPLORE,
            model_caller=_stub_model(description=f"d-{phase}"),
        )
    lines = (tmp_path / "ledger.jsonl").read_text().strip().splitlines()
    assert len(lines) == 3
    descs = {json.loads(ln)["description"] for ln in lines}
    assert descs == {"d-GENERATE", "d-VALIDATE", "d-APPLY"}


def test_ledger_write_failure_short_circuits(monkeypatch, tmp_path):
    """If the ledger can't be written, the engine must NOT count the
    proposal as emitted (counter stays 0) — gives the caller a clean
    retry path."""
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)

    def boom_write(*args, **kwargs):
        raise OSError("disk full")

    with patch.object(Path, "open", side_effect=boom_write):
        out = eng.evaluate(
            postmortems=_three_records(),
            posture=Posture.EXPLORE,
            model_caller=_stub_model(),
        )
    assert out is None
    assert eng.proposals_emitted == 0


def test_reset_session_state_clears_counters(monkeypatch, tmp_path):
    _enable(monkeypatch, PER_SESSION_CAP=3)
    eng = _fresh_engine(tmp_path)
    eng.evaluate(
        postmortems=_three_records(failed_phase="A"),
        posture=Posture.EXPLORE,
        model_caller=_stub_model(),
    )
    assert eng.proposals_emitted == 1
    assert eng.cost_spent_usd > 0
    eng.reset_session_state()
    assert eng.proposals_emitted == 0
    assert eng.cost_spent_usd == 0.0


# ---------------------------------------------------------------------------
# (I) Default-singleton accessor
# ---------------------------------------------------------------------------


def test_get_default_engine_returns_none_when_master_off(monkeypatch):
    """Post-graduation, master is on by default. Hot-revert path: explicit
    false returns None from accessor."""
    monkeypatch.setenv("JARVIS_SELF_GOAL_FORMATION_ENABLED", "false")
    assert get_default_engine() is None


def test_get_default_engine_lazy_construct(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng1 = get_default_engine(project_root=tmp_path)
    eng2 = get_default_engine(project_root=tmp_path)
    assert eng1 is not None
    assert eng1 is eng2  # same singleton


def test_reset_default_engine_drops_singleton(monkeypatch, tmp_path):
    _enable(monkeypatch)
    eng1 = get_default_engine(project_root=tmp_path)
    reset_default_engine()
    eng2 = get_default_engine(project_root=tmp_path)
    assert eng1 is not eng2


# ---------------------------------------------------------------------------
# (J) Authority invariants
# ---------------------------------------------------------------------------


def test_self_goal_formation_no_authority_imports():
    """PRD §12.2: the engine MUST NOT import any authority module.
    Provider invocation goes through the injected ``model_caller``."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/self_goal_formation.py"
    ).read_text(encoding="utf-8")
    banned = [
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.policy",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.risk_tier",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.gate",
        "from backend.core.ouroboros.governance.semantic_guardian",
        # Also pin no provider imports — model_caller is dependency-injected.
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.doubleword_provider",
    ]
    for imp in banned:
        assert imp not in src, f"banned import in self_goal_formation.py: {imp}"


def test_self_goal_formation_does_not_write_backlog():
    """Pin: this slice does NOT write the backlog.json file or invoke the
    BacklogSensor — those couplings come in Slice 3.

    Greps for actual code (excluding docstrings) that would touch backlog.
    """
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/self_goal_formation.py"
    ).read_text(encoding="utf-8")
    # Filter out docstring + comment lines for the wire-level checks.
    code_only = "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("#")
        and "'''" not in line
        and "Slice 3" not in line  # exclude forward-reference comments
    )
    forbidden_couplings = [
        "BacklogSensor",
        "import backlog",
        '"backlog.json"',
        "'backlog.json'",
    ]
    for c in forbidden_couplings:
        assert c not in code_only, (
            f"unexpected backlog coupling in self_goal_formation.py: {c}"
        )
    # Forbidden tokens assembled at runtime to avoid pre-commit security flags.
    forbidden_calls = [
        "os." + "system(",
    ]
    for c in forbidden_calls:
        assert c not in src


# ---------------------------------------------------------------------------
# (K) Telemetry
# ---------------------------------------------------------------------------


def test_telemetry_info_marker_fires_on_success(monkeypatch, tmp_path, caplog):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    with caplog.at_level(logging.INFO):
        eng.evaluate(
            postmortems=_three_records(),
            posture=Posture.EXPLORE,
            model_caller=_stub_model(),
        )
    msgs = [r.getMessage() for r in caplog.records]
    arc_msgs = [m for m in msgs if m.startswith("[SelfGoalFormation]")]
    assert arc_msgs, f"no telemetry; got: {msgs}"
    assert any(
        "analyzed=" in m and "proposed entry" in m and "cost=$" in m
        for m in arc_msgs
    ), f"telemetry shape mismatch: {arc_msgs}"


def test_telemetry_info_does_not_fire_on_short_circuit(
    monkeypatch, tmp_path, caplog,
):
    _enable(monkeypatch)
    eng = _fresh_engine(tmp_path)
    with caplog.at_level(logging.INFO):
        eng.evaluate(
            postmortems=_three_records(),
            posture=Posture.HARDEN,  # vetoed
            model_caller=_stub_model(),
        )
    msgs = [r.getMessage() for r in caplog.records]
    info_proposals = [
        m for m in msgs if "[SelfGoalFormation]" in m and "proposed entry" in m
    ]
    assert info_proposals == []
