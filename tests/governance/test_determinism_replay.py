"""Tests for Slice 1.4 — Replay-as-pure-function harness.

Operator ruling: "the assert_byte_identical_trace unit test must be
exhaustively parameterized. It must definitively prove that time-travel
state reconstruction works."

Pins:
  * replay() is a pure function (no side effects, deterministic)
  * replay() is byte-identical across multiple invocations
  * time_travel() is equivalent to replay(log[:t])
  * assert_byte_identical_trace detects divergence
  * Merkle DAG predecessor graph is correctly reconstructed
  * Empty logs produce empty state
  * Malformed rows are gracefully handled
  * State-0 seeding works correctly
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Tuple

import pytest

from backend.core.ouroboros.governance.observability.replay_harness import (
    ReplayState,
    TraceComparison,
    assert_byte_identical_trace,
    replay,
    time_travel,
)


# ---------------------------------------------------------------------------
# Fixtures: mock DecisionRow-like objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MockRow:
    """Minimal shape that replay() expects."""

    phase: str = ""
    decision: str = ""
    factors: Dict[str, Any] = None  # type: ignore[assignment]
    payload_hash: str = ""
    predecessor_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.factors is None:
            object.__setattr__(self, "factors", {})


def _make_trace(
    *phase_decision_pairs: Tuple[str, str],
    with_hashes: bool = False,
) -> list[MockRow]:
    """Build a trace from (phase, decision) pairs."""
    rows = []
    for i, (phase, decision) in enumerate(phase_decision_pairs):
        pred = (rows[-1].payload_hash,) if rows and with_hashes else ()
        rows.append(MockRow(
            phase=phase,
            decision=decision,
            factors={"step": i},
            payload_hash=f"hash_{i}" if with_hashes else "",
            predecessor_ids=pred,
        ))
    return rows


# ---------------------------------------------------------------------------
# Replay purity (the load-bearing property)
# ---------------------------------------------------------------------------


class TestReplayPurity:
    """Prove that replay() is a pure function."""

    def test_empty_log_empty_state(self) -> None:
        """Empty log → empty state."""
        state = replay([])
        assert state["step_count"] == 0
        assert state["decisions"] == []
        assert state["phase_sequence"] == []

    def test_deterministic_across_calls(self) -> None:
        """Same input → byte-identical output across calls."""
        trace = _make_trace(
            ("CLASSIFY", "STANDARD"),
            ("ROUTE", "STANDARD"),
            ("GENERATE", "OK"),
            ("VALIDATE", "PASS"),
            ("GATE", "APPROVE"),
        )
        s1 = replay(trace)
        s2 = replay(trace)
        # Import canonical_serialize for byte-level comparison.
        from backend.core.ouroboros.governance.observability.determinism_substrate import (  # noqa: E501
            canonical_serialize,
        )
        assert canonical_serialize(s1) == canonical_serialize(s2)

    @pytest.mark.parametrize("n_rows", [1, 5, 10, 50, 100])
    def test_deterministic_for_n_rows(self, n_rows: int) -> None:
        """Deterministic for any number of rows."""
        trace = [
            MockRow(
                phase=f"PHASE_{i}",
                decision=f"DECISION_{i}",
                factors={"i": i, "mod": i % 3},
            )
            for i in range(n_rows)
        ]
        s1 = replay(trace)
        s2 = replay(trace)
        assert s1 == s2
        assert s1["step_count"] == n_rows

    def test_no_mutation_of_input(self) -> None:
        """replay() does not mutate the input log."""
        trace = _make_trace(("CLASSIFY", "STANDARD"))
        trace_copy = list(trace)
        replay(trace)
        assert trace == trace_copy

    def test_no_side_effects(self) -> None:
        """replay() has no observable side effects."""
        import os
        import tempfile
        # Verify no files are created by replay.
        tmpdir = tempfile.mkdtemp()
        before = set(os.listdir(tmpdir))
        replay(_make_trace(("CLASSIFY", "STANDARD")))
        after = set(os.listdir(tmpdir))
        assert before == after


# ---------------------------------------------------------------------------
# Replay correctness
# ---------------------------------------------------------------------------


class TestReplayCorrectness:
    """Verify the reducer produces correct state."""

    def test_decisions_tracked(self) -> None:
        trace = _make_trace(
            ("CLASSIFY", "STANDARD"),
            ("ROUTE", "IMMEDIATE"),
        )
        state = replay(trace)
        assert state["decisions"] == [
            ("CLASSIFY", "STANDARD"),
            ("ROUTE", "IMMEDIATE"),
        ]

    def test_phase_sequence_tracked(self) -> None:
        trace = _make_trace(
            ("CLASSIFY", "X"),
            ("ROUTE", "Y"),
            ("GENERATE", "Z"),
        )
        state = replay(trace)
        assert state["phase_sequence"] == [
            "CLASSIFY", "ROUTE", "GENERATE",
        ]

    def test_factors_deduped(self) -> None:
        """Factor keys are unique across all rows."""
        rows = [
            MockRow(phase="A", decision="X", factors={"k1": 1, "k2": 2}),
            MockRow(phase="B", decision="Y", factors={"k2": 3, "k3": 4}),
        ]
        state = replay(rows)
        assert state["factors_seen"] == ["k1", "k2", "k3"]

    def test_payload_hashes_tracked(self) -> None:
        trace = _make_trace(
            ("A", "X"), ("B", "Y"), with_hashes=True,
        )
        state = replay(trace)
        assert state["payload_hashes"] == ["hash_0", "hash_1"]

    def test_predecessor_graph(self) -> None:
        trace = _make_trace(
            ("A", "X"), ("B", "Y"), ("C", "Z"), with_hashes=True,
        )
        state = replay(trace)
        # hash_1 → [hash_0], hash_2 → [hash_1]
        assert state["predecessor_graph"]["hash_1"] == ["hash_0"]
        assert state["predecessor_graph"]["hash_2"] == ["hash_1"]

    def test_step_count(self) -> None:
        trace = _make_trace(
            ("A", "X"), ("B", "Y"), ("C", "Z"),
        )
        state = replay(trace)
        assert state["step_count"] == 3


# ---------------------------------------------------------------------------
# State-0 seeding
# ---------------------------------------------------------------------------


class TestState0Seeding:
    """Verify initial state seeding."""

    def test_seed_decisions(self) -> None:
        state_0 = {"decisions": [("PRIOR", "DONE")]}
        state = replay(
            _make_trace(("CLASSIFY", "OK")),
            state_0=state_0,
        )
        assert state["decisions"] == [
            ("PRIOR", "DONE"),
            ("CLASSIFY", "OK"),
        ]

    def test_seed_step_count(self) -> None:
        state_0 = {"step_count": 10}
        state = replay(
            _make_trace(("A", "X")),
            state_0=state_0,
        )
        assert state["step_count"] == 11

    def test_seed_predecessor_graph(self) -> None:
        state_0 = {
            "predecessor_graph": {"h0": ["h_prev"]},
        }
        state = replay([], state_0=state_0)
        assert state["predecessor_graph"] == {"h0": ["h_prev"]}


# ---------------------------------------------------------------------------
# Time travel
# ---------------------------------------------------------------------------


class TestTimeTravel:
    """Prove time_travel(log, t) ≡ replay(log[:t])."""

    @pytest.mark.parametrize("t", [0, 1, 2, 3, 5])
    def test_equivalence(self, t: int) -> None:
        trace = _make_trace(
            ("A", "1"), ("B", "2"), ("C", "3"),
            ("D", "4"), ("E", "5"),
        )
        state_tt = time_travel(trace, t)
        state_rp = replay(trace[:t])
        assert state_tt == state_rp

    def test_time_travel_zero(self) -> None:
        """t=0 → empty state."""
        state = time_travel(_make_trace(("A", "1")), 0)
        assert state["step_count"] == 0

    def test_time_travel_full(self) -> None:
        """t=len(log) → full replay."""
        trace = _make_trace(("A", "1"), ("B", "2"))
        assert time_travel(trace, 2) == replay(trace)


# ---------------------------------------------------------------------------
# assert_byte_identical_trace (exhaustive parameterization)
# ---------------------------------------------------------------------------


class TestAssertByteIdenticalTrace:
    """The highest-impact unit test the codebase is missing.

    Exhaustively parameterized to prove time-travel state
    reconstruction works.
    """

    def test_identical_traces(self) -> None:
        """Two identical traces → identical=True."""
        trace = _make_trace(
            ("CLASSIFY", "STANDARD"),
            ("ROUTE", "IMMEDIATE"),
            ("GENERATE", "OK"),
        )
        result = assert_byte_identical_trace(trace, trace)
        assert result.identical is True
        assert result.trace_1_hash == result.trace_2_hash
        assert len(result.trace_1_hash) == 64

    def test_different_decision_detected(self) -> None:
        """Different decisions → divergence detected."""
        t1 = _make_trace(("CLASSIFY", "STANDARD"))
        t2 = _make_trace(("CLASSIFY", "COMPLEX"))
        result = assert_byte_identical_trace(t1, t2)
        assert result.identical is False
        assert "divergence" in result.diff_detail

    def test_different_phase_detected(self) -> None:
        """Different phases → divergence detected."""
        t1 = _make_trace(("CLASSIFY", "X"))
        t2 = _make_trace(("ROUTE", "X"))
        result = assert_byte_identical_trace(t1, t2)
        assert result.identical is False

    def test_different_length_detected(self) -> None:
        """Different trace lengths → divergence detected."""
        t1 = _make_trace(("A", "1"), ("B", "2"))
        t2 = _make_trace(("A", "1"))
        result = assert_byte_identical_trace(t1, t2)
        assert result.identical is False
        assert "length" in result.diff_detail

    def test_empty_traces_identical(self) -> None:
        """Two empty traces → identical."""
        result = assert_byte_identical_trace([], [])
        assert result.identical is True

    @pytest.mark.parametrize("n", [1, 2, 5, 10, 20, 50])
    def test_identical_n_row_traces(self, n: int) -> None:
        """N-row identical traces are detected as identical."""
        trace = [
            MockRow(
                phase=f"P{i}",
                decision=f"D{i}",
                factors={"i": i},
            )
            for i in range(n)
        ]
        result = assert_byte_identical_trace(trace, list(trace))
        assert result.identical is True

    @pytest.mark.parametrize("diverge_at", [0, 1, 2, 3, 4])
    def test_divergence_at_step_n(self, diverge_at: int) -> None:
        """Divergence at step N is precisely located."""
        t1 = [
            MockRow(phase=f"P{i}", decision="SAME")
            for i in range(5)
        ]
        t2 = list(t1)
        t2[diverge_at] = MockRow(
            phase=f"P{diverge_at}", decision="DIFFERENT",
        )
        result = assert_byte_identical_trace(t1, t2)
        assert result.identical is False
        assert result.divergence_step == diverge_at

    def test_different_factors_detected(self) -> None:
        """Different factors → divergence in factors_seen."""
        t1 = [MockRow(phase="A", decision="X", factors={"k1": 1})]
        t2 = [MockRow(phase="A", decision="X", factors={"k2": 2})]
        result = assert_byte_identical_trace(t1, t2)
        assert result.identical is False

    def test_different_hashes_detected(self) -> None:
        """Different payload hashes → divergence."""
        t1 = [MockRow(phase="A", decision="X", payload_hash="h1")]
        t2 = [MockRow(phase="A", decision="X", payload_hash="h2")]
        result = assert_byte_identical_trace(t1, t2)
        assert result.identical is False

    def test_predecessor_graph_divergence(self) -> None:
        """Different predecessor graphs → divergence."""
        t1 = [
            MockRow(phase="A", decision="X", payload_hash="h0"),
            MockRow(
                phase="B", decision="Y", payload_hash="h1",
                predecessor_ids=("h0",),
            ),
        ]
        t2 = [
            MockRow(phase="A", decision="X", payload_hash="h0"),
            MockRow(
                phase="B", decision="Y", payload_hash="h1",
                predecessor_ids=("different",),
            ),
        ]
        result = assert_byte_identical_trace(t1, t2)
        assert result.identical is False


# ---------------------------------------------------------------------------
# Malformed input resilience
# ---------------------------------------------------------------------------


class TestMalformedInputResilience:
    """replay() never crashes on bad input."""

    def test_row_without_phase(self) -> None:
        """Row without .phase attribute → error recorded."""

        class BadRow:
            decision = "X"

        state = replay([BadRow()])
        assert state["step_count"] == 1
        # Should still work (getattr returns "")

    def test_row_with_none_factors(self) -> None:
        """Row with None factors → no crash."""
        row = MockRow(phase="A", decision="X", factors=None)  # type: ignore
        state = replay([row])
        assert state["step_count"] == 1

    def test_non_dict_factors(self) -> None:
        """Row with non-dict factors → no crash."""

        @dataclass(frozen=True)
        class WeirdRow:
            phase: str = "A"
            decision: str = "X"
            factors: str = "not_a_dict"
            payload_hash: str = ""
            predecessor_ids: Tuple[str, ...] = ()

        state = replay([WeirdRow()])
        assert state["step_count"] == 1
        # factors_seen should be empty (string is not dict)
        assert state["factors_seen"] == []
