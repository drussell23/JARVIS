"""A dead-letter queue holds work, and a cockpit that knows must say.

Two defects, both found by reading production state rather than code:

  1. `.jarvis/intake_dlq.jsonl` held 156 rows of which 152 were DIAGNOSTICS.
     `append_dlq(envelope: Any, ...)` typed its parameter `Any`, so the
     contract was unenforced and callers filed error telemetry into a replay
     queue. Not merely untidy: `replay_dlq` dedups by goal_id, every
     diagnostic has an empty one, so a replay would collapse them to a single
     entry and hand an error record to the intake router AS WORK.
  2. The cockpit's economic-death banner filtered on
     `consecutive_economic_failures` — a key `economic_view()` does not
     return. The branch never rendered for any provider, ever, while
     doubleword sat at `state='economic', hard_open=True, status 402`.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.cli.ov import _economic_evidence, _liquidity_lines
from backend.core.ouroboros.governance import intake_dlq as dlq


class TestTheQueueHoldsWork:
    def test_a_pure_diagnostic_is_not_replayable(self):
        """The shape that made up 55 of the 156 production rows: informative
        to a human, meaningless to an intake router."""
        assert not dlq.is_replayable(
            {"action": "heartbeat_frozen", "error_class": "X",
             "surface": "auth", "note": "n", "detail": "d", "event": "e"})

    def test_node_coldstart_telemetry_is_not_replayable(self):
        """91 of the 156."""
        assert not dlq.is_replayable(
            {"awakened_model": "m", "awakened_tier_heavy": True,
             "classification": "c", "instance": "i", "k": 1})

    def test_a_real_envelope_is_replayable(self):
        assert dlq.is_replayable(
            {"dedup_key": "abc", "description": "Fix the thing",
             "confidence": 0.9})

    def test_a_minimal_envelope_stays_replayable(self):
        """Deliberately permissive. An earlier draft required actionable
        content and would have DIVERTED REAL WORK carrying only an id — worse
        than the misfiling being fixed, because misfiling never lost work."""
        assert dlq.is_replayable({"goal_id": "g1"})

    def test_dedup_key_counts_as_identity(self):
        """Its absence was a live defect: real envelopes identify themselves
        with `dedup_key` and often carry no goal_id, and `replay_dlq` dedups
        first-wins — so every such envelope collapsed into ONE survivor."""
        assert dlq._goal_id({"dedup_key": "k1"}) == "k1"
        assert dlq._goal_id({"causal_id": "c1"}) == "c1"
        assert dlq._goal_id({"goal_id": "g", "dedup_key": "k"}) == "g"

    def test_a_diagnostic_is_rerouted_not_dropped(self, tmp_path):
        """Enforcing the contract must not cost data — otherwise callers
        would simply drop what the queue refuses."""
        p = str(tmp_path / "dlq.jsonl")
        dlq.append_dlq({"action": "x", "error_class": "y"}, reason="r", path=p)
        assert dlq.read_dlq(p) == []
        diag = dlq._diagnostics_path(p)
        import os
        assert os.path.exists(diag)
        assert "action" in open(diag, encoding="utf-8").read()

    def test_real_work_still_reaches_the_queue(self, tmp_path):
        p = str(tmp_path / "dlq.jsonl")
        dlq.append_dlq({"goal_id": "g9", "description": "real"},
                       reason="no_router", path=p)
        assert len(dlq.read_dlq(p)) == 1

    @pytest.mark.asyncio
    async def test_legacy_rows_are_skipped_on_replay(self, tmp_path):
        """152 such rows exist today. Guarding only new writes would leave
        them armed."""
        import json
        p = tmp_path / "dlq.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in [
            {"ts": 1, "reason": "x", "goal_id": "",
             "envelope": {"action": "a", "error_class": "b"}},
            {"ts": 2, "reason": "y", "goal_id": "g1",
             "envelope": {"goal_id": "g1", "description": "real work"}},
        ]) + "\n", encoding="utf-8")
        seen = []

        async def _ingest(env):
            seen.append(env)
        drained = await dlq.replay_dlq(str(p), _ingest)
        assert drained == 1
        assert seen and seen[0].get("description") == "real work"


class TestTheBannerSaysWhoIsOutOfMoney:
    def _rows(self, econ):
        return _liquidity_lines(
            {"anthropic": {"tokens_remaining": 5_000_000},
             "doubleword": {"tokens_remaining": None}},
            any_exhausted=True, economic=econ)

    def test_the_state_shape_is_read_not_a_nonexistent_counter(self):
        """`economic_view()` returns {state, hard_open, reason, …}. The old
        filter read `consecutive_economic_failures`, which is never present,
        so the branch never fired for any provider."""
        out = " ".join(self._rows({
            "doubleword": {"state": "economic", "hard_open": True,
                           "reason": "status 402"}}))
        assert "doubleword: OUT OF CREDIT" in out
        assert "status 402" in out

    def test_the_legacy_counter_shape_still_works(self):
        out = " ".join(self._rows({
            "doubleword": {"consecutive_economic_failures": 3}}))
        assert "OUT OF CREDIT" in out

    def test_a_lapsed_verdict_is_reported_not_silenced(self):
        """`economic_view` degrades a stale verdict to state='unknown' — it
        must not assert knowledge it lacks. But dropping the row reports
        "nothing known" when what IS known is "last time we looked this lane
        was out of money"."""
        out = " ".join(self._rows({
            "anthropic": {"state": "unknown", "stale_clock": True,
                          "unverified_since": 0.0,
                          "reason": "Your credit balance is too low"}}))
        assert "last known OUT OF CREDIT" in out
        assert "lapsed on a timer" in out

    def test_a_healthy_provider_produces_no_warning(self):
        """Restraint: healthy is invisible."""
        out = " ".join(self._rows({"claude": {"state": "healthy"}}))
        assert "OUT OF CREDIT" not in out

    def test_the_human_sentence_survives_truncation(self):
        """Vendors wrap the one useful sentence in transport noise;
        truncating the envelope yields JSON scaffolding and drops the remedy."""
        raw = ("Error code: 400 - {'type': 'error', 'error': {'type': "
               "'invalid_request_error', 'message': 'Your credit balance is "
               "too low to access the Anthropic API. Please go to Plans'}}")
        got = _economic_evidence(raw)
        assert got.startswith("Your credit balance is too low")
        assert "invalid_request_error" not in got

    def test_evidence_extraction_never_raises(self):
        for bad in ("", None, 12345, "{'message': "):
            assert isinstance(_economic_evidence(bad), str)  # type: ignore[arg-type]
