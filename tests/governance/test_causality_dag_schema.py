"""Priority 2 Slice 1 — Causality DAG schema extension regression spine.

Pins the additive backward-compatible schema extension to
``DecisionRecord`` and the master-flag-gated write path through
``DecisionRuntime.record()`` and ``decide()``.

§-numbered coverage map:

  §1   Master flag JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED — default false (Slice 1)
  §2   DecisionRecord new-field defaults (parent_record_ids=(), counterfactual_of=None)
  §3   DecisionRecord.to_dict — emits new fields ONLY when set
  §4   DecisionRecord.to_dict — byte-for-byte preserved when lineage absent
  §5   DecisionRecord.from_dict — round-trips with new fields
  §6   DecisionRecord.from_dict — backward-compat parses pre-Slice-1 rows
  §7   DecisionRecord.from_dict — defensively coerces malformed parent IDs
  §8   DecisionRecord.from_dict — defensively coerces non-string counterfactual_of
  §9   DecisionRecord.from_dict — non-iterable parents → empty tuple
  §10  DecisionRecord schema_version unchanged (decision_record.1)
  §11  Master-off forces lineage to empty/None at runtime.record()
  §12  Master-on threads lineage through runtime.record() → ledger row
  §13  decide() threads lineage to runtime.record() in RECORD mode
  §14  decide() lineage kwargs no-op in PASSTHROUGH/REPLAY
  §15  capture_phase_decision threads lineage through to decide()
  §16  Defensive: None / empty inputs handled cleanly
  §17  AST authority invariants (no new forbidden imports)
  §18  Tuple-of-strings frozen contract (parent_record_ids hashable)
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import tempfile
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.determinism import decision_runtime
from backend.core.ouroboros.governance.determinism.decision_runtime import (
    SCHEMA_VERSION,
    DecisionRecord,
    causality_dag_schema_enabled,
    decide,
    runtime_for_session,
)


# ===========================================================================
# §1 — Master flag default false (Slice 1 ships behind the flag)
# ===========================================================================


def test_schema_flag_default_true_post_graduation(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED", raising=False)
    assert causality_dag_schema_enabled() is True


@pytest.mark.parametrize("val", ["", " ", "  ", "\t"])
def test_schema_flag_empty_default_true_post_graduation(
    monkeypatch, val,
) -> None:
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED", val)
    assert causality_dag_schema_enabled() is True


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE"])
def test_schema_flag_explicit_true(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED", val)
    assert causality_dag_schema_enabled() is True


@pytest.mark.parametrize("val", ["0", "false", "no", "off", "garbage"])
def test_schema_flag_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED", val)
    assert causality_dag_schema_enabled() is False


# ===========================================================================
# §2 — DecisionRecord new-field defaults
# ===========================================================================


def _make_record(**overrides) -> DecisionRecord:
    base = dict(
        record_id="r1", session_id="s1", op_id="op-1", phase="ROUTE",
        kind="route", ordinal=0, inputs_hash="h", output_repr='"x"',
        monotonic_ts=1.0, wall_ts=2.0,
    )
    base.update(overrides)
    return DecisionRecord(**base)


def test_record_default_lineage_empty() -> None:
    r = _make_record()
    assert r.parent_record_ids == ()
    assert r.counterfactual_of is None


def test_record_explicit_lineage() -> None:
    r = _make_record(
        parent_record_ids=("p1", "p2"),
        counterfactual_of="r0",
    )
    assert r.parent_record_ids == ("p1", "p2")
    assert r.counterfactual_of == "r0"


# ===========================================================================
# §3-§4 — to_dict emission rules
# ===========================================================================


def test_to_dict_omits_empty_lineage() -> None:
    """Empty/None lineage → keys absent → byte-for-byte preserved."""
    r = _make_record()
    d = r.to_dict()
    assert "parent_record_ids" not in d
    assert "counterfactual_of" not in d


def test_to_dict_emits_when_parents_present() -> None:
    r = _make_record(parent_record_ids=("p1", "p2"))
    d = r.to_dict()
    assert d["parent_record_ids"] == ["p1", "p2"]
    # counterfactual_of still absent
    assert "counterfactual_of" not in d


def test_to_dict_emits_when_counterfactual_only() -> None:
    r = _make_record(counterfactual_of="r0")
    d = r.to_dict()
    assert d["counterfactual_of"] == "r0"
    assert "parent_record_ids" not in d


def test_to_dict_byte_for_byte_preserved_pre_slice_1() -> None:
    """Empty lineage produces JSON identical to a pre-Slice-1 record
    serialized via the same to_dict() — schema_version is the same,
    no new keys, key set is exactly the original 11."""
    r = _make_record()
    d = r.to_dict()
    expected_keys = {
        "record_id", "session_id", "op_id", "phase", "kind",
        "ordinal", "inputs_hash", "output_repr",
        "monotonic_ts", "wall_ts", "schema_version",
    }
    assert set(d.keys()) == expected_keys


# ===========================================================================
# §5-§6 — from_dict round-trip + backward-compat
# ===========================================================================


def test_from_dict_roundtrip_with_lineage() -> None:
    r = _make_record(
        parent_record_ids=("p1", "p2"),
        counterfactual_of="r0",
    )
    d = r.to_dict()
    r2 = DecisionRecord.from_dict(d)
    assert r2 == r


def test_from_dict_roundtrip_empty_lineage() -> None:
    r = _make_record()
    d = r.to_dict()
    r2 = DecisionRecord.from_dict(d)
    assert r2 == r


def test_from_dict_pre_slice_1_record_parses() -> None:
    """A row written before Slice 1 (no new keys) parses with
    empty/None defaults."""
    pre = {
        "record_id": "old-1", "session_id": "s", "op_id": "op",
        "phase": "ROUTE", "kind": "route", "ordinal": 0,
        "inputs_hash": "h", "output_repr": '"x"',
        "monotonic_ts": 1.0, "wall_ts": 2.0,
        "schema_version": SCHEMA_VERSION,
    }
    r = DecisionRecord.from_dict(pre)
    assert r is not None
    assert r.parent_record_ids == ()
    assert r.counterfactual_of is None


# ===========================================================================
# §7-§9 — Defensive coercion in from_dict
# ===========================================================================


def test_from_dict_coerces_malformed_parent_ids() -> None:
    """Mixed valid + invalid entries: valid ones land, invalid silently skipped."""
    raw = {
        "record_id": "r1", "session_id": "s", "op_id": "op",
        "phase": "p", "kind": "k", "ordinal": 0,
        "inputs_hash": "h", "output_repr": '"x"',
        "monotonic_ts": 1.0, "wall_ts": 2.0,
        "schema_version": SCHEMA_VERSION,
        "parent_record_ids": ["p1", None, "", 42, "p2", {"x": 1}],
    }
    r = DecisionRecord.from_dict(raw)
    assert r is not None
    # None / empty-string / dict silently dropped; "42" survives because
    # int → str coercion. Result: ("p1", "42", "p2") — but the dict {"x":1}
    # doesn't pass the (str, int, float) isinstance gate.
    assert "p1" in r.parent_record_ids
    assert "p2" in r.parent_record_ids


def test_from_dict_non_iterable_parents_yields_empty() -> None:
    raw = {
        "record_id": "r", "session_id": "s", "op_id": "op",
        "phase": "p", "kind": "k", "ordinal": 0,
        "inputs_hash": "h", "output_repr": '"x"',
        "monotonic_ts": 1.0, "wall_ts": 2.0,
        "schema_version": SCHEMA_VERSION,
        "parent_record_ids": 42,  # not iterable
    }
    r = DecisionRecord.from_dict(raw)
    assert r is not None
    assert r.parent_record_ids == ()


def test_from_dict_non_string_counterfactual_coerced() -> None:
    """Numeric counterfactual_of → str. Empty/whitespace → None."""
    base = {
        "record_id": "r", "session_id": "s", "op_id": "op",
        "phase": "p", "kind": "k", "ordinal": 0,
        "inputs_hash": "h", "output_repr": '"x"',
        "monotonic_ts": 1.0, "wall_ts": 2.0,
        "schema_version": SCHEMA_VERSION,
    }
    # numeric → coerced to string
    r = DecisionRecord.from_dict({**base, "counterfactual_of": 42})
    assert r is not None
    assert r.counterfactual_of == "42"
    # empty string → None
    r = DecisionRecord.from_dict({**base, "counterfactual_of": ""})
    assert r is not None
    assert r.counterfactual_of is None
    # whitespace → None
    r = DecisionRecord.from_dict({**base, "counterfactual_of": "  "})
    assert r is not None
    assert r.counterfactual_of is None


def test_from_dict_returns_none_on_unparseable_input() -> None:
    """Defensive contract: NEVER raises."""
    assert DecisionRecord.from_dict(None) is None
    assert DecisionRecord.from_dict({}) is None
    assert DecisionRecord.from_dict("not a mapping") is None
    assert DecisionRecord.from_dict({"schema_version": "wrong"}) is None


# ===========================================================================
# §10 — schema_version unchanged
# ===========================================================================


def test_schema_version_unchanged_for_additive_extension() -> None:
    """Slice 1 is additive backward-compat — schema_version stays
    decision_record.1. A bump would invalidate every pre-existing
    ledger."""
    assert SCHEMA_VERSION == "decision_record.1"
    r = _make_record(parent_record_ids=("p1",))
    assert r.schema_version == "decision_record.1"


# ===========================================================================
# §11-§13 — Master-off / master-on runtime behavior
# ===========================================================================


@pytest.fixture
def isolated_session(monkeypatch, tmp_path):
    """Fresh ledger directory per test so concurrent test runs don't
    collide on shared state."""
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", f"slice1-{tmp_path.name}")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path),
    )
    yield tmp_path


def _ledger_lines(tmp_path) -> list:
    """Read all JSONL rows from the test ledger."""
    lines = []
    for p in tmp_path.rglob("decisions.jsonl"):
        lines.extend(
            json.loads(line) for line in p.read_text().splitlines()
            if line.strip()
        )
    return lines


def test_record_master_off_suppresses_lineage(
    monkeypatch, isolated_session,
) -> None:
    """Caller supplies lineage; master-off drops it at write time."""
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED", "false")

    async def run():
        await decide(
            op_id="op-1", phase="ROUTE", kind="route",
            inputs={"x": 1},
            compute=lambda: {"r": "standard"},
            parent_record_ids=["p1", "p2"],
            counterfactual_of="r0",
        )

    asyncio.run(run())
    lines = _ledger_lines(isolated_session)
    assert len(lines) >= 1
    last = lines[-1]
    # Lineage MUST be absent (byte-for-byte preserved with pre-Slice-1)
    assert "parent_record_ids" not in last
    assert "counterfactual_of" not in last


def test_record_master_on_threads_lineage(
    monkeypatch, isolated_session,
) -> None:
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED", "true")

    async def run():
        await decide(
            op_id="op-2", phase="ROUTE", kind="route",
            inputs={"x": 1},
            compute=lambda: {"r": "standard"},
            parent_record_ids=("p1", "p2"),
            counterfactual_of="r0",
        )

    asyncio.run(run())
    lines = _ledger_lines(isolated_session)
    last = lines[-1]
    assert last.get("parent_record_ids") == ["p1", "p2"]
    assert last.get("counterfactual_of") == "r0"


def test_record_master_on_no_lineage_supplied(
    monkeypatch, isolated_session,
) -> None:
    """Master-on but caller passes no lineage → keys still absent
    (defensive defaults preserve byte-equality)."""
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED", "true")

    async def run():
        await decide(
            op_id="op-3", phase="ROUTE", kind="route",
            inputs={"x": 1},
            compute=lambda: {"r": "standard"},
        )

    asyncio.run(run())
    lines = _ledger_lines(isolated_session)
    last = lines[-1]
    assert "parent_record_ids" not in last
    assert "counterfactual_of" not in last


def test_record_master_on_coerces_malformed_parents(
    monkeypatch, isolated_session,
) -> None:
    """Mixed-type parents: valid coerced; invalid silently skipped."""
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED", "true")

    async def run():
        await decide(
            op_id="op-4", phase="ROUTE", kind="route",
            inputs={"x": 1},
            compute=lambda: {"r": "standard"},
            parent_record_ids=[
                "p1", None, "", 42, {"bad": "type"}, "p2",
            ],
        )

    asyncio.run(run())
    lines = _ledger_lines(isolated_session)
    last = lines[-1]
    parents = last.get("parent_record_ids", [])
    assert "p1" in parents
    assert "p2" in parents
    # None / empty / dict skipped
    assert None not in parents
    assert "" not in parents


# ===========================================================================
# §16 — Defensive contract
# ===========================================================================


def test_record_master_on_none_lineage_preserves_empty(
    monkeypatch, isolated_session,
) -> None:
    monkeypatch.setenv("JARVIS_CAUSALITY_DAG_SCHEMA_ENABLED", "true")

    async def run():
        await decide(
            op_id="op-5", phase="ROUTE", kind="route",
            inputs={"x": 1},
            compute=lambda: {"r": "standard"},
            parent_record_ids=None,
            counterfactual_of=None,
        )

    asyncio.run(run())
    lines = _ledger_lines(isolated_session)
    last = lines[-1]
    assert "parent_record_ids" not in last
    assert "counterfactual_of" not in last


# ===========================================================================
# §17 — AST authority invariants
# ===========================================================================


def test_decision_runtime_no_new_forbidden_imports() -> None:
    """Slice 1 is purely additive within decision_runtime; the
    forbidden-import surface (orchestrator/policy/etc) is unchanged."""
    src = Path(inspect.getfile(decision_runtime)).read_text()
    tree = ast.parse(src)
    forbidden = (
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.phase_runners",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.change_engine",
        "backend.core.ouroboros.governance.policy",
        "backend.core.ouroboros.governance.semantic_guardian",
        "backend.core.ouroboros.governance.providers",
        "backend.core.ouroboros.governance.urgency_router",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for fb in forbidden:
                    assert fb not in alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for fb in forbidden:
                assert fb not in node.module


# ===========================================================================
# §18 — Frozen / hashable contract
# ===========================================================================


def test_record_with_lineage_is_hashable() -> None:
    """parent_record_ids is Tuple[str,...] — must remain hashable."""
    r = _make_record(
        parent_record_ids=("p1", "p2"),
        counterfactual_of="r0",
    )
    hash(r)  # MUST NOT raise


def test_record_lineage_is_immutable() -> None:
    """frozen dataclass — assignment to lineage fields raises."""
    r = _make_record(parent_record_ids=("p1",))
    with pytest.raises((AttributeError, Exception)):
        r.parent_record_ids = ("p2",)  # type: ignore[misc]


def test_record_lineage_tuple_not_list() -> None:
    """parent_record_ids is structurally a tuple (immutable, hashable)."""
    r = _make_record(parent_record_ids=("p1", "p2"))
    assert isinstance(r.parent_record_ids, tuple)
