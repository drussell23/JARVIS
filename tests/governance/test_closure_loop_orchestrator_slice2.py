"""Q4 Priority #2 Slice 2 — Store + Observer regression suite.

Pins the flock'd JSONL ring buffer (``closure_loop_store``) and the
async observer (``closure_loop_observer``).

Covers:

  §1   Store: env knobs (history dir / max records) clamps + defaults
  §2   Store: cold-start read returns empty tuple (file absent)
  §3   Store: append + read round-trip preserves record fidelity
  §4   Store: ring-buffer rotation truncates to max_records
  §5   Store: schema-mismatched lines silently dropped on read
  §6   Store: corrupt JSON lines tolerated on read
  §7   Store: master-flag-off → DISABLED (no I/O)
  §8   Store: REJECTED on non-record input
  §9   Store: PERSIST_ERROR when flock_append_line returns False
  §10  Store: read filters by since_ts
  §11  Store: read limit caps tail length
  §12  Observer: env knobs (interval / drift_multiplier /
       failure_backoff / liveness_pulse / dedup_ring_size)
  §13  Observer: shadow defaults — every advisory →
       SKIPPED_REPLAY_REJECTED (None replay verdict)
  §14  Observer: real validator injection — every advisory → PROPOSED
       when shadow replay is replaced with a stub returning
       DIVERGED_WORSE
  §15  Observer: dedup ring — second pass on same advisories doesn't
       double-emit
  §16  Observer: dedup ring evicts oldest fingerprint at capacity
  §17  Observer: master-flag-off short-circuit (no record persisted)
  §18  Observer: validator exception → SKIPPED_VALIDATION_FAILED
  §19  Observer: replay validator exception → SKIPPED_REPLAY_REJECTED
  §20  Observer: on_record_emitted callback fires once per OK persist
  §21  Observer: on_record_emitted exception swallowed
  §22  Observer: stats() shape includes outcome_histogram
  §23  Observer: failure backoff multiplies interval after exception
  §24  Observer: drift multiplier kicks in after a record is emitted
  §25  Observer: stop() is idempotent + safe on never-started observer
  §26  Observer: start() is idempotent
  §27  Observer: watermark advances per advisory ts
  §28  AST authority pin: store imports nothing from authority modules
  §29  AST authority pin: observer imports nothing from yaml_writer
       / orchestrator (governance) / iron_gate
  §30  Singleton get/reset
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
from pathlib import Path
from typing import List, Optional, Tuple
from unittest import mock

import pytest

from backend.core.ouroboros.governance.verification.closure_loop_orchestrator import (  # noqa: E501
    CLOSURE_LOOP_SCHEMA_VERSION,
    ClosureLoopRecord,
    ClosureOutcome,
)
from backend.core.ouroboros.governance.verification.closure_loop_observer import (  # noqa: E501
    CLOSURE_LOOP_OBSERVER_SCHEMA_VERSION,
    ClosureLoopObserver,
    closure_loop_observer_dedup_ring_size,
    closure_loop_observer_drift_multiplier,
    closure_loop_observer_failure_backoff_ceiling_s,
    closure_loop_observer_interval_default_s,
    closure_loop_observer_liveness_pulse_passes,
    get_default_observer,
    reset_default_observer,
    shadow_replay_validator,
    shadow_tightening_validator,
)
from backend.core.ouroboros.governance.verification.closure_loop_store import (  # noqa: E501
    RecordOutcome,
    closure_loop_history_dir,
    closure_loop_history_max_records,
    closure_loop_history_path,
    read_closure_history,
    record_closure_outcome,
    reset_for_tests,
)
from backend.core.ouroboros.governance.verification.coherence_action_bridge import (  # noqa: E501
    CoherenceAdvisory,
    CoherenceAdvisoryAction,
    TighteningIntent,
    TighteningProposalStatus,
)
from backend.core.ouroboros.governance.verification.coherence_auditor import (  # noqa: E501
    BehavioralDriftKind,
    DriftSeverity,
)
from backend.core.ouroboros.governance.verification.counterfactual_replay import (  # noqa: E501
    BranchSnapshot,
    BranchVerdict,
    DecisionOverrideKind,
    ReplayOutcome,
    ReplayTarget,
    ReplayVerdict,
)


# ---------------------------------------------------------------------------
# Builders + fixtures
# ---------------------------------------------------------------------------


def _intent(name: str = "budget_route_drift_pct") -> TighteningIntent:
    return TighteningIntent(
        parameter_name=name,
        current_value=0.25,
        proposed_value=0.20,
        direction="smaller_is_tighter",
    )


def _advisory(
    *,
    advisory_id: str = "adv-001",
    ts: float = 1000.0,
) -> CoherenceAdvisory:
    return CoherenceAdvisory(
        advisory_id=advisory_id,
        drift_signature=f"sig-{advisory_id}",
        drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
        action=CoherenceAdvisoryAction.TIGHTEN_RISK_BUDGET,
        severity=DriftSeverity.MEDIUM,
        detail="route distribution rotated",
        recorded_at_ts=ts,
        tightening_status=TighteningProposalStatus.PASSED,
        tightening_intent=_intent(),
    )


def _record(
    outcome: ClosureOutcome = ClosureOutcome.PROPOSED,
    *,
    advisory_id: str = "adv-001",
    fingerprint: str = "fp-001",
    decided_at_ts: float = 1000.0,
) -> ClosureLoopRecord:
    return ClosureLoopRecord(
        outcome=outcome,
        advisory_id=advisory_id,
        drift_kind=BehavioralDriftKind.BEHAVIORAL_ROUTE_DRIFT,
        parameter_name="budget_route_drift_pct",
        decided_at_ts=decided_at_ts,
        record_fingerprint=fingerprint,
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Each test gets its own history dir + clean env knobs."""
    monkeypatch.setenv(
        "JARVIS_CLOSURE_LOOP_HISTORY_DIR", str(tmp_path),
    )
    monkeypatch.setenv(
        "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", "true",
    )
    # Reset the closure_loop_observer singleton + the cigw's etc.
    reset_default_observer()
    reset_for_tests()
    yield
    reset_default_observer()
    reset_for_tests()


# ---------------------------------------------------------------------------
# §1 — Store env knobs
# ---------------------------------------------------------------------------


class TestStoreEnvKnobs:
    def test_history_dir_default_jarvis(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CLOSURE_LOOP_HISTORY_DIR", raising=False,
        )
        assert closure_loop_history_dir().name == ".jarvis"

    def test_max_records_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CLOSURE_LOOP_HISTORY_MAX_RECORDS", raising=False,
        )
        assert closure_loop_history_max_records() == 1024

    def test_max_records_clamps_floor(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_HISTORY_MAX_RECORDS", "0",
        )
        assert closure_loop_history_max_records() == 16

    def test_max_records_clamps_ceiling(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_HISTORY_MAX_RECORDS", "9999999",
        )
        assert closure_loop_history_max_records() == 65536

    def test_max_records_garbage_default(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_HISTORY_MAX_RECORDS", "abc",
        )
        assert closure_loop_history_max_records() == 1024


# ---------------------------------------------------------------------------
# §2–§11 — Store behavior
# ---------------------------------------------------------------------------


class TestStoreBehavior:
    def test_cold_start_read_returns_empty(self):
        assert read_closure_history() == ()

    def test_round_trip_preserves_fields(self):
        r = _record()
        assert record_closure_outcome(r) is RecordOutcome.OK
        history = read_closure_history()
        assert len(history) == 1
        assert history[0].outcome is r.outcome
        assert history[0].advisory_id == r.advisory_id
        assert history[0].record_fingerprint == r.record_fingerprint

    def test_ring_buffer_rotation_to_max(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_HISTORY_MAX_RECORDS", "16",
        )
        # Append 20 records — only the last 16 should survive.
        for i in range(20):
            r = _record(
                advisory_id=f"adv-{i:03d}",
                fingerprint=f"fp-{i:03d}",
                decided_at_ts=float(i),
            )
            assert record_closure_outcome(r) is RecordOutcome.OK
        history = read_closure_history()
        assert len(history) == 16
        # Oldest 4 dropped → first surviving is adv-004.
        assert history[0].advisory_id == "adv-004"
        assert history[-1].advisory_id == "adv-019"

    def test_schema_mismatched_lines_dropped(self):
        path = closure_loop_history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "schema_version": "wrong.v9",
                "outcome": "proposed",
            }) + "\n")
            fh.write(json.dumps({
                "schema_version": CLOSURE_LOOP_SCHEMA_VERSION,
                "outcome": "proposed",
                "advisory_id": "ok",
                "drift_kind": "behavioral_route_drift",
                "decided_at_ts": 1.0,
            }) + "\n")
        history = read_closure_history()
        assert len(history) == 1
        assert history[0].advisory_id == "ok"

    def test_corrupt_json_tolerated(self):
        path = closure_loop_history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps({
                "schema_version": CLOSURE_LOOP_SCHEMA_VERSION,
                "outcome": "proposed",
                "advisory_id": "ok",
                "drift_kind": "behavioral_route_drift",
                "decided_at_ts": 1.0,
            }) + "\n")
        history = read_closure_history()
        assert len(history) == 1

    def test_master_off_yields_disabled(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", "false",
        )
        assert (
            record_closure_outcome(_record())
            is RecordOutcome.DISABLED
        )
        assert read_closure_history() == ()

    def test_non_record_input_rejected(self):
        assert (
            record_closure_outcome("not a record")  # type: ignore[arg-type]
            is RecordOutcome.REJECTED
        )

    def test_persist_error_on_flock_failure(self):
        with mock.patch(
            "backend.core.ouroboros.governance.verification."
            "closure_loop_store.flock_append_line",
            return_value=False,
        ):
            assert (
                record_closure_outcome(_record())
                is RecordOutcome.PERSIST_ERROR
            )

    def test_read_filters_by_since_ts(self):
        for i, ts in enumerate([10.0, 20.0, 30.0]):
            record_closure_outcome(_record(
                advisory_id=f"a-{i}",
                fingerprint=f"f-{i}",
                decided_at_ts=ts,
            ))
        history = read_closure_history(since_ts=15.0)
        assert len(history) == 2
        assert history[0].advisory_id == "a-1"

    def test_read_limit_caps_tail(self):
        for i in range(5):
            record_closure_outcome(_record(
                advisory_id=f"a-{i}",
                fingerprint=f"f-{i}",
                decided_at_ts=float(i),
            ))
        history = read_closure_history(limit=2)
        assert len(history) == 2
        assert history[0].advisory_id == "a-3"
        assert history[1].advisory_id == "a-4"


# ---------------------------------------------------------------------------
# §12 — Observer env knobs
# ---------------------------------------------------------------------------


class TestObserverEnvKnobs:
    def test_interval_default_600(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CLOSURE_LOOP_OBSERVER_INTERVAL_S", raising=False,
        )
        assert closure_loop_observer_interval_default_s() == 600.0

    def test_interval_floor_60(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_OBSERVER_INTERVAL_S", "1",
        )
        assert closure_loop_observer_interval_default_s() == 60.0

    def test_interval_ceiling_7200(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_OBSERVER_INTERVAL_S", "999999",
        )
        assert closure_loop_observer_interval_default_s() == 7200.0

    def test_drift_multiplier_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CLOSURE_LOOP_OBSERVER_DRIFT_MULTIPLIER",
            raising=False,
        )
        assert closure_loop_observer_drift_multiplier() == 0.5

    def test_failure_backoff_ceiling_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CLOSURE_LOOP_OBSERVER_FAILURE_BACKOFF_CEILING_S",
            raising=False,
        )
        assert (
            closure_loop_observer_failure_backoff_ceiling_s()
            == 3600.0
        )

    def test_liveness_pulse_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CLOSURE_LOOP_OBSERVER_LIVENESS_PULSE_PASSES",
            raising=False,
        )
        assert closure_loop_observer_liveness_pulse_passes() == 4

    def test_dedup_ring_size_default(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_CLOSURE_LOOP_OBSERVER_DEDUP_RING_SIZE",
            raising=False,
        )
        assert closure_loop_observer_dedup_ring_size() == 256


# ---------------------------------------------------------------------------
# §13–§24 — Observer behavior
# ---------------------------------------------------------------------------


class TestObserverBehavior:
    def _stub_advisories(
        self, advisories: Tuple[CoherenceAdvisory, ...],
    ):
        """Patch ``read_coherence_advisories`` to return our fixture."""
        return mock.patch(
            "backend.core.ouroboros.governance.verification."
            "closure_loop_observer.read_coherence_advisories",
            return_value=advisories,
        )

    @pytest.mark.asyncio
    async def test_shadow_defaults_yield_replay_rejected(self):
        advs = tuple(
            _advisory(advisory_id=f"adv-{i}", ts=float(100 + i))
            for i in range(3)
        )
        with self._stub_advisories(advs):
            obs = ClosureLoopObserver()
            result = await obs.run_one_pass()
        assert result.advisories_seen == 3
        # All three persisted (shadow → SKIPPED_REPLAY_REJECTED).
        assert result.records_emitted == 3
        history = read_closure_history()
        for r in history:
            assert r.outcome is ClosureOutcome.SKIPPED_REPLAY_REJECTED

    @pytest.mark.asyncio
    async def test_real_replay_stub_yields_proposed(self):
        async def good_replay(adv):
            return ReplayVerdict(
                outcome=ReplayOutcome.SUCCESS,
                target=ReplayTarget(
                    session_id="s",
                    swap_at_phase="GATE",
                    swap_decision_kind=(
                        DecisionOverrideKind.GATE_DECISION
                    ),
                ),
                original_branch=BranchSnapshot(
                    branch_id="o", terminal_phase="C",
                    terminal_success=False,
                ),
                counterfactual_branch=BranchSnapshot(
                    branch_id="c", terminal_phase="C",
                    terminal_success=True,
                ),
                verdict=BranchVerdict.DIVERGED_WORSE,
            )

        advs = (_advisory(),)
        with self._stub_advisories(advs):
            obs = ClosureLoopObserver(replay_validator=good_replay)
            result = await obs.run_one_pass()
        assert result.records_emitted == 1
        history = read_closure_history()
        assert history[0].outcome is ClosureOutcome.PROPOSED

    @pytest.mark.asyncio
    async def test_dedup_prevents_double_emit_on_repeat_advisory(self):
        advs = (_advisory(advisory_id="adv-A", ts=100.0),)
        obs = ClosureLoopObserver()
        with self._stub_advisories(advs):
            r1 = await obs.run_one_pass()
            assert r1.records_emitted == 1
            r2 = await obs.run_one_pass()
            # Same advisory → same fingerprint → deduped on pass 2.
            assert r2.records_deduped == 1
            assert r2.records_emitted == 0
        history = read_closure_history()
        assert len(history) == 1

    @pytest.mark.asyncio
    async def test_dedup_ring_evicts_oldest(self, monkeypatch):
        # Tiny ring so we can demonstrate eviction directly.
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_OBSERVER_DEDUP_RING_SIZE", "16",
        )
        # Build 20 distinct advisories — appending the 17th evicts
        # advs[0]'s fingerprint, advs[1] on 18th, etc. After all 20,
        # the ring holds fingerprints for advs[4..19] only.
        advs = tuple(
            _advisory(advisory_id=f"adv-{i:03d}", ts=float(i))
            for i in range(20)
        )
        obs = ClosureLoopObserver()
        with self._stub_advisories(advs):
            await obs.run_one_pass()
        # Direct invariant assertion: ring should be at capacity (16)
        # and advs[0]'s fingerprint should NOT be in the dedup_set
        # (was evicted by advs[16]'s arrival).
        assert len(obs._dedup_ring) == 16
        # Compute advs[0]'s fingerprint by replaying compute_closure_outcome.
        from backend.core.ouroboros.governance.verification.closure_loop_orchestrator import (  # noqa: E501
            compute_closure_outcome,
        )
        rec_for_adv_0 = compute_closure_outcome(
            advisory=advs[0],
            validator_result=(True, "shadow_validator_stub"),
            replay_verdict=None,
            enabled=True,
        )
        fp_adv_0 = rec_for_adv_0.record_fingerprint
        assert fp_adv_0 not in obs._dedup_set, (
            "evicted fingerprint should not be in dedup_set"
        )
        # And the LAST 16 fingerprints (advs[4..19]) MUST be in set.
        for i in range(4, 20):
            rec = compute_closure_outcome(
                advisory=advs[i],
                validator_result=(True, "shadow_validator_stub"),
                replay_verdict=None,
                enabled=True,
            )
            assert rec.record_fingerprint in obs._dedup_set, (
                f"recent fingerprint advs[{i}] should be in set"
            )

    @pytest.mark.asyncio
    async def test_master_off_short_circuits(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_ORCHESTRATOR_ENABLED", "false",
        )
        advs = (_advisory(),)
        obs = ClosureLoopObserver()
        with self._stub_advisories(advs):
            result = await obs.run_one_pass()
        assert result.advisories_seen == 0
        assert result.records_emitted == 0

    @pytest.mark.asyncio
    async def test_validator_exception_yields_validation_failed(self):
        def boom(adv):
            raise RuntimeError("validator exploded")
        advs = (_advisory(),)
        obs = ClosureLoopObserver(tightening_validator=boom)
        with self._stub_advisories(advs):
            await obs.run_one_pass()
        history = read_closure_history()
        assert history[0].outcome is (
            ClosureOutcome.SKIPPED_VALIDATION_FAILED
        )
        assert "validator_raised" in history[0].validator_detail

    @pytest.mark.asyncio
    async def test_replay_exception_yields_replay_rejected(self):
        async def boom(adv):
            raise RuntimeError("replay exploded")
        advs = (_advisory(),)
        obs = ClosureLoopObserver(replay_validator=boom)
        with self._stub_advisories(advs):
            await obs.run_one_pass()
        history = read_closure_history()
        assert history[0].outcome is (
            ClosureOutcome.SKIPPED_REPLAY_REJECTED
        )

    @pytest.mark.asyncio
    async def test_on_record_emitted_callback_fires(self):
        captured: List[ClosureLoopRecord] = []

        async def cb(record: ClosureLoopRecord):
            captured.append(record)

        advs = (_advisory(),)
        obs = ClosureLoopObserver(on_record_emitted=cb)
        with self._stub_advisories(advs):
            await obs.run_one_pass()
        assert len(captured) == 1
        assert captured[0].advisory_id == "adv-001"

    @pytest.mark.asyncio
    async def test_on_record_emitted_exception_swallowed(self):
        async def cb(record):
            raise RuntimeError("downstream boom")

        advs = (_advisory(),)
        obs = ClosureLoopObserver(on_record_emitted=cb)
        with self._stub_advisories(advs):
            # Must not propagate the exception.
            await obs.run_one_pass()
        # Record still persisted despite callback failure.
        assert len(read_closure_history()) == 1

    @pytest.mark.asyncio
    async def test_stats_shape(self):
        obs = ClosureLoopObserver()
        s = obs.stats()
        assert s["schema_version"] == (
            CLOSURE_LOOP_OBSERVER_SCHEMA_VERSION
        )
        assert "outcome_histogram" in s
        # 6 ClosureOutcome values represented.
        assert len(s["outcome_histogram"]) == 6
        assert "consecutive_failures" in s
        assert "dedup_ring_size" in s

    def test_failure_backoff_multiplies_interval(self):
        obs = ClosureLoopObserver(interval_s=10.0)
        obs._consecutive_failures = 3
        # base 10 × 3 failures = 30.0, under ceiling 3600.
        assert obs._compute_next_interval() == 30.0

    def test_drift_multiplier_after_emit(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_OBSERVER_DRIFT_MULTIPLIER", "0.5",
        )
        obs = ClosureLoopObserver(interval_s=400.0)
        obs._signature_changed_last_pass = True
        # 400 × 0.5 = 200.0 — within the 60-floor.
        assert obs._compute_next_interval() == 200.0

    def test_drift_multiplier_floor_60(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CLOSURE_LOOP_OBSERVER_DRIFT_MULTIPLIER", "0.1",
        )
        obs = ClosureLoopObserver(interval_s=100.0)
        obs._signature_changed_last_pass = True
        # 100 × 0.1 = 10.0 — clamped UP to 60.
        assert obs._compute_next_interval() == 60.0


# ---------------------------------------------------------------------------
# §25–§27 — Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_stop_idempotent_on_unstarted(self):
        obs = ClosureLoopObserver()
        # Must not raise.
        await obs.stop()
        await obs.stop()

    @pytest.mark.asyncio
    async def test_start_idempotent(self):
        obs = ClosureLoopObserver(interval_s=3600.0)
        await obs.start()
        first_task = obs._task
        await obs.start()
        # Same task — re-start was a no-op.
        assert obs._task is first_task
        await obs.stop()

    @pytest.mark.asyncio
    async def test_watermark_advances_per_advisory_ts(self):
        advs = tuple(
            _advisory(advisory_id=f"adv-{i}", ts=float(100 + i))
            for i in range(3)
        )
        obs = ClosureLoopObserver()
        with mock.patch(
            "backend.core.ouroboros.governance.verification."
            "closure_loop_observer.read_coherence_advisories",
            return_value=advs,
        ):
            await obs.run_one_pass()
        # Watermark advanced to the latest advisory's ts.
        assert obs._last_seen_advisory_ts == 102.0


# ---------------------------------------------------------------------------
# §28–§29 — AST authority pins
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    @staticmethod
    def _module_imports(mod) -> List[str]:
        src = inspect.getsource(mod)
        tree = ast.parse(src)
        out = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                out.append(node.module)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    out.append(alias.name)
        return out

    def test_store_imports_no_authority_modules(self):
        from backend.core.ouroboros.governance.verification import (
            closure_loop_store,
        )
        imports = self._module_imports(closure_loop_store)
        forbidden = {
            "yaml_writer", "meta_governor", "iron_gate",
            "risk_tier", "change_engine", "candidate_generator",
            "gate",
        }
        for imp in imports:
            for f in forbidden:
                # "orchestrator" appears in
                # closure_loop_orchestrator (whitelist that one)
                if imp.endswith("closure_loop_orchestrator"):
                    continue
                assert f not in imp.split("."), (
                    f"forbidden import: {imp}"
                )

    def test_observer_imports_no_authority_modules(self):
        from backend.core.ouroboros.governance.verification import (
            closure_loop_observer,
        )
        imports = self._module_imports(closure_loop_observer)
        forbidden = {
            "yaml_writer", "meta_governor", "iron_gate",
            "risk_tier", "change_engine", "candidate_generator",
            "gate",
        }
        for imp in imports:
            for f in forbidden:
                if imp.endswith("closure_loop_orchestrator"):
                    continue
                assert f not in imp.split("."), (
                    f"forbidden import: {imp}"
                )

    def test_observer_module_does_not_call_approve(self):
        from backend.core.ouroboros.governance.verification import (
            closure_loop_observer,
        )
        src = inspect.getsource(closure_loop_observer)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr != "approve", (
                        f"forbidden .approve call at line "
                        f"{node.lineno}"
                    )


# ---------------------------------------------------------------------------
# §30 — Singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_get_default_observer_returns_same_instance(self):
        a = get_default_observer()
        b = get_default_observer()
        assert a is b

    def test_reset_default_observer_clears(self):
        a = get_default_observer()
        reset_default_observer()
        b = get_default_observer()
        assert a is not b


# ---------------------------------------------------------------------------
# Schema version pin
# ---------------------------------------------------------------------------


def test_observer_schema_version_pin():
    assert (
        CLOSURE_LOOP_OBSERVER_SCHEMA_VERSION
        == "closure_loop_observer.v1"
    )


# ---------------------------------------------------------------------------
# Shadow validators contract
# ---------------------------------------------------------------------------


def test_shadow_tightening_validator_returns_ok():
    ok, detail = shadow_tightening_validator(_advisory())
    assert ok is True
    assert "shadow" in detail


@pytest.mark.asyncio
async def test_shadow_replay_validator_returns_none():
    assert await shadow_replay_validator(_advisory()) is None
