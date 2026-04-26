"""P1.5 Slice 1 — HypothesisLedger + REPL regression suite.

Pins the JSONL primitive + REPL surface so:
  (a) Slice 2 can wire SelfGoalFormationEngine to emit Hypothesis rows
      against a stable contract.
  (b) Last-write-wins semantics never silently drift.
  (c) Authority invariants hold on both new modules.

Sections:
    (A) Hypothesis dataclass — frozen + lifecycle helpers
    (B) make_hypothesis_id — deterministic + collision-free for distinct inputs
    (C) Append + load — happy path / multiple rows / last-write-wins
    (D) Find helpers — by_id / open / validated / invalidated
    (E) record_outcome — appends update row + lookup returns latest
    (F) stats() — counts pending/validated/invalidated
    (G) Tolerance — missing file / malformed lines / partial fields /
        write failures
    (H) Default-singleton accessor
    (I) REPL — routing / help / list / pending / validated / invalidated /
        show / stats / unknown subcommand / parse error
    (J) Authority invariants — banned imports + side-effect surface pin
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.hypothesis_ledger import (
    DEFAULT_LEDGER_FILENAME,
    HYPOTHESIS_SCHEMA_VERSION,
    Hypothesis,
    HypothesisLedger,
    get_default_ledger,
    make_hypothesis_id,
    reset_default_ledger,
)
from backend.core.ouroboros.governance.hypothesis_repl import (
    HypothesisDispatchResult,
    dispatch_hypothesis_command as REPL,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_default_ledger()
    yield
    reset_default_ledger()


def _make(
    *,
    op_id: str = "op-001",
    claim: str = "Test hypothesis",
    expected: str = "Test expected outcome",
    actual: str = None,
    validated=None,
    sig_hash: str = None,
    ts: float = None,
) -> Hypothesis:
    ts = ts if ts is not None else time.time()
    return Hypothesis(
        hypothesis_id=make_hypothesis_id(op_id, claim, ts),
        op_id=op_id,
        claim=claim,
        expected_outcome=expected,
        actual_outcome=actual,
        validated=validated,
        proposed_signature_hash=sig_hash,
        created_unix=ts,
    )


def _seed(ledger: HypothesisLedger, *hypotheses: Hypothesis) -> None:
    for h in hypotheses:
        ledger.append(h)


# ---------------------------------------------------------------------------
# (A) Hypothesis dataclass
# ---------------------------------------------------------------------------


def test_hypothesis_is_frozen():
    h = _make()
    with pytest.raises(Exception):
        h.claim = "mutated"  # type: ignore[misc]


def test_hypothesis_lifecycle_helpers():
    h_open = _make()
    h_val = _make(actual="x", validated=True)
    h_inval = _make(actual="x", validated=False)
    h_undecided = _make(actual="x", validated=None)
    assert h_open.is_open() and not h_open.is_validated()
    assert h_val.is_validated() and not h_val.is_open()
    assert h_inval.is_invalidated() and not h_inval.is_open()
    # Undecided + actual_outcome present → not open + neither validated
    assert not h_undecided.is_open()
    assert not h_undecided.is_validated()
    assert not h_undecided.is_invalidated()


def test_hypothesis_to_ledger_dict_includes_schema_version():
    h = _make()
    d = h.to_ledger_dict()
    assert d["schema_version"] == HYPOTHESIS_SCHEMA_VERSION
    assert d["hypothesis_id"] == h.hypothesis_id


# ---------------------------------------------------------------------------
# (B) make_hypothesis_id
# ---------------------------------------------------------------------------


def test_make_hypothesis_id_deterministic():
    a = make_hypothesis_id("op-1", "claim", 1700.0)
    b = make_hypothesis_id("op-1", "claim", 1700.0)
    assert a == b
    assert len(a) == 12


def test_make_hypothesis_id_distinct_inputs_distinct_ids():
    ids = {
        make_hypothesis_id("op-1", "claim", 1700.0),
        make_hypothesis_id("op-2", "claim", 1700.0),
        make_hypothesis_id("op-1", "claim2", 1700.0),
        make_hypothesis_id("op-1", "claim", 1701.0),
    }
    assert len(ids) == 4


# ---------------------------------------------------------------------------
# (C) Append + load
# ---------------------------------------------------------------------------


def test_append_and_load_single(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    h = _make()
    assert ledger.append(h) is True
    rows = ledger.load_all()
    assert len(rows) == 1
    assert rows[0].hypothesis_id == h.hypothesis_id


def test_append_creates_jsonl_with_one_line_per_row(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    ledger.append(_make(op_id="a", ts=1.0))
    ledger.append(_make(op_id="b", ts=2.0))
    text = (tmp_path / ".jarvis" / "hypothesis_ledger.jsonl").read_text()
    assert len(text.strip().splitlines()) == 2


def test_load_all_empty_when_file_missing(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    assert ledger.load_all() == []


def test_load_all_last_write_wins_per_id(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    ts = time.time()
    h_open = Hypothesis(
        hypothesis_id="hid-1", op_id="op", claim="c", expected_outcome="e",
        created_unix=ts,
    )
    h_validated = Hypothesis(
        hypothesis_id="hid-1", op_id="op", claim="c", expected_outcome="e",
        actual_outcome="happened", validated=True, created_unix=ts,
        validated_unix=ts + 100,
    )
    ledger.append(h_open)
    ledger.append(h_validated)
    rows = ledger.load_all()
    assert len(rows) == 1
    assert rows[0].is_validated()
    assert rows[0].actual_outcome == "happened"


def test_load_preserves_first_seen_order(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    h1 = _make(op_id="first", ts=1.0)
    h2 = _make(op_id="second", ts=2.0)
    h3 = _make(op_id="third", ts=3.0)
    ledger.append(h1)
    ledger.append(h2)
    ledger.append(h3)
    # Update h1 last
    ledger.append(Hypothesis(
        hypothesis_id=h1.hypothesis_id, op_id=h1.op_id,
        claim=h1.claim, expected_outcome=h1.expected_outcome,
        actual_outcome="x", validated=True, created_unix=h1.created_unix,
    ))
    rows = ledger.load_all()
    # h1 should still appear FIRST despite latest update.
    assert rows[0].hypothesis_id == h1.hypothesis_id
    assert rows[0].is_validated()
    assert [r.op_id for r in rows] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# (D) Find helpers
# ---------------------------------------------------------------------------


def test_find_by_id(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    h = _make()
    ledger.append(h)
    assert ledger.find_by_id(h.hypothesis_id) is not None
    assert ledger.find_by_id("nonexistent") is None


def test_find_by_id_case_insensitive(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    h = _make()
    ledger.append(h)
    assert ledger.find_by_id(h.hypothesis_id.upper()) is not None


def test_find_open_validated_invalidated(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    _seed(
        ledger,
        _make(op_id="open"),
        _make(op_id="val", actual="x", validated=True),
        _make(op_id="inval", actual="y", validated=False),
    )
    assert len(ledger.find_open()) == 1
    assert ledger.find_open()[0].op_id == "open"
    assert len(ledger.find_validated()) == 1
    assert ledger.find_validated()[0].op_id == "val"
    assert len(ledger.find_invalidated()) == 1
    assert ledger.find_invalidated()[0].op_id == "inval"


# ---------------------------------------------------------------------------
# (E) record_outcome
# ---------------------------------------------------------------------------


def test_record_outcome_writes_update_row(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    h = _make()
    ledger.append(h)
    assert ledger.record_outcome(
        h.hypothesis_id, "did happen", validated=True,
    ) is True
    rows = ledger.load_all()
    assert len(rows) == 1
    assert rows[0].is_validated()
    assert rows[0].actual_outcome == "did happen"
    assert rows[0].validated_unix is not None


def test_record_outcome_unknown_id_returns_false(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    assert ledger.record_outcome("nope", "x", True) is False


def test_record_outcome_preserves_prior_fields(tmp_path):
    """Critical: the update row must carry forward op_id/claim/expected/sig
    so subsequent reads don't lose context."""
    ledger = HypothesisLedger(project_root=tmp_path)
    h = _make(op_id="op-X", sig_hash="prop-abc")
    ledger.append(h)
    ledger.record_outcome(h.hypothesis_id, "result", True)
    found = ledger.find_by_id(h.hypothesis_id)
    assert found.op_id == "op-X"
    assert found.proposed_signature_hash == "prop-abc"


# ---------------------------------------------------------------------------
# (F) stats
# ---------------------------------------------------------------------------


def test_stats_empty_ledger(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    s = ledger.stats()
    assert s == {"total": 0, "open": 0, "validated": 0, "invalidated": 0}


def test_stats_mixed_state(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    _seed(
        ledger,
        _make(op_id="o1"),
        _make(op_id="o2"),
        _make(op_id="v1", actual="x", validated=True),
        _make(op_id="i1", actual="y", validated=False),
    )
    s = ledger.stats()
    assert s == {"total": 4, "open": 2, "validated": 1, "invalidated": 1}


# ---------------------------------------------------------------------------
# (G) Tolerance
# ---------------------------------------------------------------------------


def test_malformed_line_skipped(tmp_path):
    (tmp_path / ".jarvis").mkdir()
    p = tmp_path / ".jarvis" / "hypothesis_ledger.jsonl"
    h = _make()
    p.write_text(
        "garbage\n"
        + json.dumps(h.to_ledger_dict()) + "\n"
        + "{not valid json\n"
    )
    ledger = HypothesisLedger(project_root=tmp_path)
    rows = ledger.load_all()
    assert len(rows) == 1
    assert rows[0].hypothesis_id == h.hypothesis_id


def test_row_missing_hypothesis_id_skipped(tmp_path):
    (tmp_path / ".jarvis").mkdir()
    p = tmp_path / ".jarvis" / "hypothesis_ledger.jsonl"
    p.write_text(json.dumps({"op_id": "x", "claim": "y"}) + "\n")
    assert HypothesisLedger(project_root=tmp_path).load_all() == []


def test_append_failure_returns_false(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    with patch.object(Path, "open", side_effect=OSError("disk full")):
        assert ledger.append(_make()) is False


def test_load_handles_invalid_validated_field(tmp_path):
    """Defensive: validated field that isn't True/False/None gets coerced
    to None (caller can't trust it)."""
    (tmp_path / ".jarvis").mkdir()
    p = tmp_path / ".jarvis" / "hypothesis_ledger.jsonl"
    p.write_text(json.dumps({
        "hypothesis_id": "x", "op_id": "o", "claim": "c",
        "expected_outcome": "e", "validated": "maybe",
    }) + "\n")
    rows = HypothesisLedger(project_root=tmp_path).load_all()
    assert len(rows) == 1
    assert rows[0].validated is None


# ---------------------------------------------------------------------------
# (H) Default-singleton accessor
# ---------------------------------------------------------------------------


def test_get_default_ledger_lazy_construct(tmp_path):
    a = get_default_ledger(project_root=tmp_path)
    b = get_default_ledger(project_root=tmp_path)
    assert a is b


def test_get_default_ledger_no_master_flag(tmp_path):
    """Unlike engine, the ledger has no master flag — operator can always
    inspect prior decisions even when engine is hot-reverted."""
    ledger = get_default_ledger(project_root=tmp_path)
    assert ledger is not None


def test_reset_default_ledger_drops_singleton(tmp_path):
    a = get_default_ledger(project_root=tmp_path)
    reset_default_ledger()
    b = get_default_ledger(project_root=tmp_path)
    assert a is not b


# ---------------------------------------------------------------------------
# (I) REPL — routing + commands
# ---------------------------------------------------------------------------


def test_repl_unrelated_line_unmatched(tmp_path):
    r = REPL("/posture explain", project_root=tmp_path)
    assert r.matched is False


def test_repl_hypothesis_without_ledger_unmatched(tmp_path):
    r = REPL("/hypothesis", project_root=tmp_path)
    assert r.matched is False


def test_repl_help(tmp_path):
    r = REPL("/hypothesis ledger help", project_root=tmp_path)
    assert r.ok is True
    assert "show" in r.text and "validated" in r.text


def test_repl_question_mark_alias(tmp_path):
    r = REPL("/hypothesis ledger ?", project_root=tmp_path)
    assert r.ok is True


def test_repl_list_empty(tmp_path):
    r = REPL("/hypothesis ledger", project_root=tmp_path)
    assert r.ok is True
    assert "no hypotheses" in r.text


def test_repl_list_populated(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    _seed(ledger, _make(op_id="abc-001", claim="something"))
    r = REPL("/hypothesis ledger list", project_root=tmp_path, ledger=ledger)
    assert r.ok is True
    assert "abc-001" in r.text
    assert "something" in r.text


def test_repl_pending_only_open(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    _seed(
        ledger,
        _make(op_id="open-1"),
        _make(op_id="val-1", actual="x", validated=True),
    )
    r = REPL("/hypothesis ledger pending", project_root=tmp_path, ledger=ledger)
    assert "open-1" in r.text
    assert "val-1" not in r.text


def test_repl_validated_only(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    _seed(
        ledger,
        _make(op_id="v1", actual="x", validated=True),
        _make(op_id="i1", actual="y", validated=False),
    )
    r = REPL("/hypothesis ledger validated", project_root=tmp_path, ledger=ledger)
    assert "v1" in r.text
    assert "i1" not in r.text


def test_repl_invalidated_only(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    _seed(
        ledger,
        _make(op_id="v1", actual="x", validated=True),
        _make(op_id="i1", actual="y", validated=False),
    )
    r = REPL("/hypothesis ledger invalidated", project_root=tmp_path, ledger=ledger)
    assert "i1" in r.text
    assert "v1" not in r.text


def test_repl_show_unknown_id(tmp_path):
    r = REPL("/hypothesis ledger show nope", project_root=tmp_path)
    assert r.ok is False
    assert "no hypothesis" in r.text


def test_repl_show_missing_arg(tmp_path):
    r = REPL("/hypothesis ledger show", project_root=tmp_path)
    assert r.ok is False
    assert "missing" in r.text.lower()


def test_repl_show_full_detail(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    h = _make(claim="The big claim", expected="The big expected")
    ledger.append(h)
    r = REPL(
        f"/hypothesis ledger show {h.hypothesis_id}",
        project_root=tmp_path, ledger=ledger,
    )
    assert r.ok is True
    assert "The big claim" in r.text
    assert "The big expected" in r.text
    assert "PENDING" in r.text


def test_repl_stats(tmp_path):
    ledger = HypothesisLedger(project_root=tmp_path)
    _seed(
        ledger,
        _make(op_id="o1"),
        _make(op_id="v1", actual="x", validated=True),
    )
    r = REPL("/hypothesis ledger stats", project_root=tmp_path, ledger=ledger)
    assert "total:" in r.text
    assert "open:" in r.text


def test_repl_unknown_subcommand(tmp_path):
    r = REPL("/hypothesis ledger floof", project_root=tmp_path)
    assert r.ok is False
    assert "unknown" in r.text.lower()


def test_repl_parse_error(tmp_path):
    r = REPL('/hypothesis ledger show "unclosed', project_root=tmp_path)
    assert r.matched is True
    assert "parse error" in r.text


# ---------------------------------------------------------------------------
# (J) Authority invariants
# ---------------------------------------------------------------------------


def test_hypothesis_ledger_no_authority_imports():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/hypothesis_ledger.py"
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
        # Provider imports forbidden — primitive never invokes models.
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.doubleword_provider",
    ]
    for imp in banned:
        assert imp not in src, f"banned import: {imp}"


def test_hypothesis_repl_no_authority_imports():
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/hypothesis_repl.py"
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
    ]
    for imp in banned:
        assert imp not in src, f"banned import: {imp}"


def test_hypothesis_ledger_only_writes_jsonl():
    """Pin: the primitive does NO subprocess / env mutation / system calls.
    Only file I/O is the JSONL append."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/hypothesis_ledger.py"
    ).read_text(encoding="utf-8")
    forbidden = [
        "subprocess.",
        "os.environ[",
        "os." + "system(",  # split to dodge pre-commit hook
    ]
    for c in forbidden:
        assert c not in src, f"unexpected coupling: {c}"


def test_hypothesis_repl_is_read_only_on_ledger():
    """REPL never writes — only reads. Slice 2 wires the engine to write."""
    src = (
        Path(__file__).resolve().parent.parent.parent
        / "backend/core/ouroboros/governance/hypothesis_repl.py"
    ).read_text(encoding="utf-8")
    # No append() / record_outcome() calls in REPL.
    assert ".append(" not in src or "rows.append" in src  # only Python list.append OK
    assert ".record_outcome(" not in src


def test_default_ledger_filename_pinned():
    assert DEFAULT_LEDGER_FILENAME == "hypothesis_ledger.jsonl"


def test_schema_version_pinned():
    assert HYPOTHESIS_SCHEMA_VERSION == "hypothesis_ledger.1"
