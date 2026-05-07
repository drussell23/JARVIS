"""§3.6.2 vector #6 — Phase 9 graduation orchestrator.

Pins per operator binding 2026-05-07 (verbatim — load-bearing):

  "Solve the root problem directly—without workarounds, brute force,
   or shortcut solutions. Significantly strengthen the system into
   something advanced, asynchronous, dynamic, adaptive, intelligent,
   and highly robust, with no hardcoding. Fully leverage existing
   files and architecture so we avoid duplication and build cleanly
   on what already exists."

Coverage (~38 tests):
  Slice 1 — Phase9Orchestrator substrate
    * Closed 4-value taxonomy (READY/PENDING/BLOCKED/GRADUATED)
    * Master flag default-FALSE per §33.1
    * Empty queue when master off (zero filesystem touch)
    * Phase9QueueEntry frozen + schema_version + to_dict
    * get_full_queue composes CADENCE_POLICY (24 flags)
    * Status derivation: graduated > blocked > ready > pending
    * Readiness score: clean / required clamped [0, 1]
    * rank_by_readiness: GRADUATED last; rest by score desc
    * next_recommended_flag returns highest-score non-blocked
    * Interaction matrix: append-only JSONL recording
    * Pair counts derive from sessions deterministically
    * total_session_count
    * Defensive: malformed JSONL lines skipped
    * Defensive: NEVER raises on broken ledger / missing files
    * 5 AST pins clean + each fires on synthetic regression

  Slice 2 — /phase9 REPL
    * Auto-discovery (matches=False on unrelated lines)
    * help / bare overview / next / flag / interactions /
      partners / unknown subcommand
    * Disabled message when master flag off
    * /phase9 flag rejects missing arg + unknown name
    * /phase9 partners empty when no co-soaks
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/"
        "phase9_orchestrator.py"
    )


@pytest.fixture
def isolated_orchestrator(tmp_path, monkeypatch):
    """Fresh orchestrator with isolated matrix path per test."""
    monkeypatch.setenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", "true",
    )
    matrix_path = tmp_path / "matrix.jsonl"
    monkeypatch.setenv(
        "JARVIS_PHASE9_INTERACTION_MATRIX_PATH",
        str(matrix_path),
    )
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        Phase9Orchestrator,
        reset_default_orchestrator_for_tests,
    )
    reset_default_orchestrator_for_tests()
    yield Phase9Orchestrator(
        interaction_matrix_path_override=matrix_path,
    )
    reset_default_orchestrator_for_tests()


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


def test_status_taxonomy_4_values():
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        Phase9QueueStatus,
    )
    assert {s.name for s in Phase9QueueStatus} == {
        "READY", "PENDING", "BLOCKED", "GRADUATED",
    }


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def test_master_default_false(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        master_enabled,
    )
    assert master_enabled() is False


def test_master_truthy(monkeypatch):
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        master_enabled,
    )
    for v in ("1", "true", "yes", "on"):
        monkeypatch.setenv(
            "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", v,
        )
        assert master_enabled() is True


def test_empty_queue_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        Phase9Orchestrator,
    )
    orch = Phase9Orchestrator()
    assert orch.get_full_queue() == ()
    assert orch.next_recommended_flag() is None
    assert orch.get_interaction_matrix() == {}
    assert orch.total_session_count() == 0


# ---------------------------------------------------------------------------
# Phase9QueueEntry artifact
# ---------------------------------------------------------------------------


def test_queue_entry_frozen():
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        Phase9QueueEntry, Phase9QueueStatus,
    )
    e = Phase9QueueEntry(
        flag_name="X", cadence_class="pass_b",
        clean_count=0, runner_count=0, infra_count=0,
        required=3, last_outcome="none",
        description="...", status=Phase9QueueStatus.PENDING,
        readiness_score=0.0, interaction_partner_count=0,
    )
    with pytest.raises(Exception):
        e.flag_name = "Y"  # type: ignore[misc]


def test_queue_entry_to_dict():
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        Phase9QueueEntry, Phase9QueueStatus,
    )
    e = Phase9QueueEntry(
        flag_name="X", cadence_class="pass_c",
        clean_count=2, runner_count=0, infra_count=1,
        required=5, last_outcome="clean",
        description="test", status=Phase9QueueStatus.PENDING,
        readiness_score=0.4, interaction_partner_count=3,
    )
    d = e.to_dict()
    assert d["flag_name"] == "X"
    assert d["status"] == "pending"
    assert d["readiness_score"] == pytest.approx(0.4)
    assert d["interaction_partner_count"] == 3
    assert "schema_version" in d


# ---------------------------------------------------------------------------
# get_full_queue composes CADENCE_POLICY
# ---------------------------------------------------------------------------


def test_full_queue_size_matches_policy(isolated_orchestrator):
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    queue = isolated_orchestrator.get_full_queue()
    # Queue size MUST equal CADENCE_POLICY length (composes
    # canonical table).
    assert len(queue) == len(CADENCE_POLICY)


def test_full_queue_preserves_policy_order(
    isolated_orchestrator,
):
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    queue = isolated_orchestrator.get_full_queue()
    queue_names = [e.flag_name for e in queue]
    policy_names = [p.flag_name for p in CADENCE_POLICY]
    assert queue_names == policy_names


def test_full_queue_carries_descriptions(
    isolated_orchestrator,
):
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    queue = isolated_orchestrator.get_full_queue()
    expected = {p.flag_name: p.description for p in CADENCE_POLICY}
    for e in queue:
        assert e.description == expected[e.flag_name]


# ---------------------------------------------------------------------------
# Status derivation
# ---------------------------------------------------------------------------


def test_status_pending_when_clean_below_required(
    isolated_orchestrator,
):
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        Phase9QueueStatus,
    )
    # No ledger data → all entries pending (zero clean,
    # required > 0).
    queue = isolated_orchestrator.get_full_queue()
    for e in queue:
        # Could be pending, ready (if env-flipped), or blocked.
        # In test isolation with no ledger writes, all should
        # be pending OR graduated (env var might override).
        assert e.status in (
            Phase9QueueStatus.PENDING,
            Phase9QueueStatus.GRADUATED,
        )


def test_status_graduated_when_env_flag_on(
    isolated_orchestrator, monkeypatch,
):
    """If a CADENCE_POLICY flag is env-flipped to true, it
    surfaces as GRADUATED."""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        Phase9QueueStatus,
    )
    if not CADENCE_POLICY:
        pytest.skip("CADENCE_POLICY empty")
    target = CADENCE_POLICY[0].flag_name
    monkeypatch.setenv(target, "true")
    queue = isolated_orchestrator.get_full_queue()
    entry = next(
        e for e in queue if e.flag_name == target
    )
    assert entry.status == Phase9QueueStatus.GRADUATED
    assert entry.readiness_score == pytest.approx(1.0)


def test_readiness_score_in_range(isolated_orchestrator):
    queue = isolated_orchestrator.get_full_queue()
    for e in queue:
        assert 0.0 <= e.readiness_score <= 1.0


# ---------------------------------------------------------------------------
# rank_by_readiness ordering
# ---------------------------------------------------------------------------


def test_rank_by_readiness_graduated_last(
    isolated_orchestrator, monkeypatch,
):
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        Phase9QueueStatus,
    )
    if len(CADENCE_POLICY) < 2:
        pytest.skip("Need ≥2 flags")
    # Mark one flag as graduated.
    target = CADENCE_POLICY[0].flag_name
    monkeypatch.setenv(target, "true")
    ranked = isolated_orchestrator.rank_by_readiness()
    # Graduated must be at the END.
    last = ranked[-1]
    assert last.flag_name == target
    assert last.status == Phase9QueueStatus.GRADUATED


def test_next_recommended_flag_skips_graduated(
    isolated_orchestrator, monkeypatch,
):
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    if len(CADENCE_POLICY) < 2:
        pytest.skip("Need ≥2 flags")
    # Mark first as graduated.
    monkeypatch.setenv(CADENCE_POLICY[0].flag_name, "true")
    nxt = isolated_orchestrator.next_recommended_flag()
    assert nxt is not None
    assert nxt != CADENCE_POLICY[0].flag_name


def test_next_recommended_flag_none_when_all_graduated(
    isolated_orchestrator, monkeypatch,
):
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    for p in CADENCE_POLICY:
        monkeypatch.setenv(p.flag_name, "true")
    assert (
        isolated_orchestrator.next_recommended_flag() is None
    )


# ---------------------------------------------------------------------------
# Interaction matrix (append-only JSONL)
# ---------------------------------------------------------------------------


def test_record_session_flags_basic(isolated_orchestrator):
    ok = isolated_orchestrator.record_session_flags(
        session_id="bt-1",
        flags_enabled=("FLAG_A", "FLAG_B"),
    )
    assert ok is True
    matrix = isolated_orchestrator.get_interaction_matrix()
    pair = frozenset({"FLAG_A", "FLAG_B"})
    assert matrix.get(pair) == 1


def test_record_session_flags_noop_master_off(
    isolated_orchestrator, monkeypatch,
):
    monkeypatch.delenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", raising=False,
    )
    ok = isolated_orchestrator.record_session_flags(
        session_id="bt-1",
        flags_enabled=("FLAG_A",),
    )
    assert ok is False


def test_record_session_flags_rejects_empty(
    isolated_orchestrator,
):
    assert isolated_orchestrator.record_session_flags(
        session_id="", flags_enabled=("FLAG_A",),
    ) is False
    assert isolated_orchestrator.record_session_flags(
        session_id="bt-1", flags_enabled=(),
    ) is False


def test_pair_count_increments_across_sessions(
    isolated_orchestrator,
):
    isolated_orchestrator.record_session_flags(
        session_id="bt-1",
        flags_enabled=("FLAG_A", "FLAG_B"),
    )
    isolated_orchestrator.record_session_flags(
        session_id="bt-2",
        flags_enabled=("FLAG_A", "FLAG_B", "FLAG_C"),
    )
    matrix = isolated_orchestrator.get_interaction_matrix()
    assert matrix[frozenset({"FLAG_A", "FLAG_B"})] == 2
    assert matrix[frozenset({"FLAG_A", "FLAG_C"})] == 1
    assert matrix[frozenset({"FLAG_B", "FLAG_C"})] == 1


def test_total_session_count_tracks_writes(
    isolated_orchestrator,
):
    assert isolated_orchestrator.total_session_count() == 0
    for i in range(5):
        isolated_orchestrator.record_session_flags(
            session_id=f"bt-{i}",
            flags_enabled=("FLAG_A",),
        )
    assert isolated_orchestrator.total_session_count() == 5


def test_partner_count_in_queue_entry(
    isolated_orchestrator, monkeypatch,
):
    """When a CADENCE_POLICY flag has been recorded alongside
    other flags, the queue entry's interaction_partner_count
    reflects the distinct partner count."""
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    if not CADENCE_POLICY:
        pytest.skip("Empty policy")
    target = CADENCE_POLICY[0].flag_name
    isolated_orchestrator.record_session_flags(
        session_id="bt-1",
        flags_enabled=(target, "FLAG_X", "FLAG_Y"),
    )
    queue = isolated_orchestrator.get_full_queue()
    entry = next(e for e in queue if e.flag_name == target)
    assert entry.interaction_partner_count == 2


# ---------------------------------------------------------------------------
# Defensive — malformed input
# ---------------------------------------------------------------------------


def test_skips_corrupt_jsonl_lines(
    isolated_orchestrator, tmp_path,
):
    # Write deliberately malformed JSONL.
    matrix_path = isolated_orchestrator.matrix_path
    matrix_path.parent.mkdir(parents=True, exist_ok=True)
    matrix_path.write_text(
        "garbage\n"
        + json.dumps({
            "session_id": "good", "flags": ["A", "B"],
            "ts": 1.0,
        }) + "\n"
        + "{not json\n"
        + "\n",
        encoding="utf-8",
    )
    # NEVER raises; valid line ingested.
    matrix = isolated_orchestrator.get_interaction_matrix()
    assert matrix[frozenset({"A", "B"})] == 1
    assert isolated_orchestrator.total_session_count() == 1


def test_never_raises_on_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PHASE9_INTERACTION_MATRIX_PATH",
        str(tmp_path / "nonexistent.jsonl"),
    )
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        Phase9Orchestrator,
    )
    orch = Phase9Orchestrator()
    # Must NOT raise.
    assert orch.get_interaction_matrix() == {}
    assert orch.total_session_count() == 0


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pin_name", [
        "phase9_orchestrator_status_taxonomy_4_values",
        "phase9_orchestrator_master_flag_default_false",
        "phase9_orchestrator_authority_asymmetry",
        "phase9_orchestrator_no_archived_orchestrator_import",
        "phase9_orchestrator_composes_canonical_ledger",
    ],
)
def test_ast_pin_validates_clean(pin_name):
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == pin_name
    )
    violations = pin.validate(tree, src)
    assert violations == ()


def test_no_archived_import_pin_fires_synthetic():
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = (
        "from archive.legacy.graduation_orchestrator_2026_04_06 "
        "import GraduationOrchestrator"
    )
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "phase9_orchestrator_no_archived_orchestrator_import"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations
    assert any("archived" in v for v in violations)


def test_taxonomy_pin_fires_on_drift():
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = '''
class Phase9QueueStatus:
    READY = "ready"
    DONE = "done"
    SUPER_BLOCKED = "super_blocked"
'''
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "phase9_orchestrator_status_taxonomy_4_values"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


def test_authority_pin_fires_on_orchestrator_import():
    from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
        register_shipped_invariants,
    )
    bad = "from backend.core.ouroboros.governance.orchestrator import x"
    tree = ast.parse(bad)
    pin = next(
        i for i in register_shipped_invariants()
        if i.invariant_name == (
            "phase9_orchestrator_authority_asymmetry"
        )
    )
    violations = pin.validate(tree, bad)
    assert violations


# ---------------------------------------------------------------------------
# /phase9 REPL
# ---------------------------------------------------------------------------


def test_repl_unmatched_line():
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/something_else")
    assert out.matched is False


def test_repl_help():
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 help")
    assert out.ok is True
    assert "/phase9 next" in out.text
    assert "/phase9 interactions" in out.text


def test_repl_disabled_when_master_off(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9")
    assert out.ok is True
    assert "disabled" in out.text


def test_repl_overview_shows_counts(
    isolated_orchestrator, monkeypatch,
):
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9")
    assert out.ok is True
    assert "queue" in out.text


def test_repl_next_shows_recommendation(
    isolated_orchestrator, monkeypatch,
):
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 next")
    assert out.ok is True
    # Either has a recommendation or "(none)" message.
    assert "JARVIS_" in out.text or "no soakable flag" in out.text


def test_repl_flag_detail(
    isolated_orchestrator, monkeypatch,
):
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        CADENCE_POLICY,
    )
    if not CADENCE_POLICY:
        pytest.skip("Empty policy")
    target = CADENCE_POLICY[0].flag_name
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command(f"/phase9 flag {target}")
    assert out.ok is True
    assert target in out.text
    assert "cadence_class" in out.text
    assert "readiness_score" in out.text


def test_repl_flag_unknown(
    isolated_orchestrator, monkeypatch,
):
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command(
        "/phase9 flag JARVIS_NEVER_REGISTERED_FLAG",
    )
    assert out.ok is False
    assert "no policy entry" in out.text


def test_repl_flag_missing_arg():
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 flag")
    assert out.ok is False
    assert "missing flag name" in out.text


def test_repl_interactions_empty(
    isolated_orchestrator, monkeypatch,
):
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 interactions")
    assert out.ok is True
    assert "no recorded sessions" in out.text


def test_repl_interactions_with_data(
    isolated_orchestrator, monkeypatch,
):
    isolated_orchestrator.record_session_flags(
        session_id="bt-1",
        flags_enabled=("FLAG_A", "FLAG_B"),
    )
    isolated_orchestrator.record_session_flags(
        session_id="bt-2",
        flags_enabled=("FLAG_A", "FLAG_B", "FLAG_C"),
    )
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 interactions")
    assert out.ok is True
    assert "FLAG_A" in out.text
    assert "FLAG_B" in out.text


def test_repl_partners_with_data(
    isolated_orchestrator, monkeypatch,
):
    isolated_orchestrator.record_session_flags(
        session_id="bt-1",
        flags_enabled=("FLAG_A", "FLAG_B"),
    )
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 partners FLAG_A")
    assert out.ok is True
    assert "FLAG_B" in out.text


def test_repl_partners_empty(
    isolated_orchestrator, monkeypatch,
):
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command(
        "/phase9 partners FLAG_LONELY",
    )
    assert out.ok is True
    assert "no recorded co-soak partners" in out.text


def test_repl_partners_missing_arg():
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 partners")
    assert out.ok is False
    assert "missing flag name" in out.text


def test_repl_unknown_subcommand():
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 bogus")
    assert out.ok is False
    assert "unknown subcommand" in out.text


def test_repl_diagnose_disabled_when_master_off(monkeypatch):
    """`/phase9 diagnose` shares the orchestrator master flag —
    when off, returns the disabled message."""
    monkeypatch.delenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 diagnose")
    assert out.ok is True
    assert "disabled" in out.text


def test_repl_diagnose_when_ledger_off(monkeypatch):
    """When orchestrator master is on but graduation_ledger
    master is off, diagnose surfaces the structured
    diagnostic (operator binding 2026-05-07: do not silently
    no-op)."""
    monkeypatch.setenv(
        "JARVIS_PHASE9_ORCHESTRATOR_ENABLED", "true",
    )
    monkeypatch.delenv(
        "JARVIS_GRADUATION_LEDGER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 diagnose")
    assert out.ok is True
    assert "JARVIS_GRADUATION_LEDGER_ENABLED" in out.text


def test_repl_diagnose_full_path(
    isolated_orchestrator, monkeypatch,
):
    """End-to-end: master flags on, diagnose surfaces the
    canonical sections (totals / blocked / next-soakable /
    cadence guidance)."""
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 diagnose")
    assert out.ok is True
    assert "totals" in out.text
    assert "next-soakable" in out.text
    assert "cadence" in out.text


def test_repl_help_documents_diagnose():
    """Help text MUST mention the diagnose subcommand so
    operators discover it without grep."""
    from backend.core.ouroboros.governance.phase9_repl import (
        dispatch_phase9_command,
    )
    out = dispatch_phase9_command("/phase9 help")
    assert out.ok is True
    assert "/phase9 diagnose" in out.text


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance import (
        phase9_orchestrator as mod,
    )
    expected = {
        "PHASE9_ORCHESTRATOR_SCHEMA_VERSION",
        "Phase9Orchestrator",
        "Phase9QueueEntry",
        "Phase9QueueStatus",
        "get_default_orchestrator",
        "interaction_matrix_path",
        "master_enabled",
        "register_flags",
        "register_shipped_invariants",
        "reset_default_orchestrator_for_tests",
    }
    assert set(mod.__all__) == expected
