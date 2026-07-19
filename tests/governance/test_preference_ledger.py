"""Preference Ledger + Multi-Variable Attenuation spine.

Mandate 4 verbatim (2026-07-19): the QoS sensor registers a
frustration trigger, but a subsequent terminal execution returns exit
code 0 → the attenuation engine catches the downstream success,
adjusts the in-memory score, and prevents the path from being falsely
categorized as a failure.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.comms.duplex import (
    preference_ledger as pl,
)
from backend.core.ouroboros.governance.comms.duplex.preference_ledger import (
    PreferenceLedger,
)


class _Envelope:
    def __init__(self, context):
        self.evidence = {"ux_degradation": True, "cause": "rapid_repeat",
                         "dialogue_context": context}


@pytest.fixture(autouse=True)
def _reset():
    pl.reset_default_ledger()
    yield
    pl.reset_default_ledger()


class TestMandate4Attenuation:
    def test_frustration_then_exit0_not_a_false_failure(self):
        """MANDATE 4 VERBATIM."""
        clock = [1000.0]
        ledger = PreferenceLedger(clock=lambda: clock[0])
        ctx = ["user: refactor the auth module", "daniel: here's the diff"]
        # QoS fires a frustration (pending, NOT yet a negative):
        ledger.record_frustration(_Envelope(ctx))
        assert ledger.snapshot()["pending"] == 1
        assert ledger.stats["definitive_negatives"] == 0
        # Downstream terminal command returns exit 0 — it actually
        # WORKED. Attenuate the frustration + book implicit success:
        clock[0] += 3.0
        ledger.record_exit_code(0, context=ctx)
        snap = ledger.snapshot()
        assert snap["pending"] == 0                  # frustration cleared
        assert ledger.stats["attenuated"] == 1       # noise caught
        assert ledger.stats["successes"] == 1
        assert ledger.stats["definitive_negatives"] == 0  # NOT a failure
        # The path's net score is POSITIVE (a success), not negative:
        top = ledger.top_strategies(min_score=0.0)
        assert top                                   # the path is rated up

    def test_frustration_then_override_is_definitive_negative(self):
        ledger = PreferenceLedger()
        ctx = ["user: fix the deadlock", "daniel: try this"]
        ledger.record_frustration(_Envelope(ctx))
        # The user OVERRODE immediately (SIGINT) → confirmed failure:
        ledger.confirm_override()
        assert ledger.stats["definitive_negatives"] == 1
        assert ledger.stats["attenuated"] == 0

    def test_idle_beyond_threshold_attenuates(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LEDGER_IDLE_SUCCESS_S", "45")
        ledger = PreferenceLedger()
        ctx = ["user: explain the module", "daniel: it does X"]
        ledger.record_frustration(_Envelope(ctx))
        ledger.note_idle(60.0, context=ctx)          # moved on satisfied
        assert ledger.stats["attenuated"] == 1
        assert ledger.stats["successes"] == 1
        assert ledger.snapshot()["pending"] == 0

    def test_brief_idle_does_not_attenuate(self, monkeypatch):
        monkeypatch.setenv("JARVIS_LEDGER_IDLE_SUCCESS_S", "45")
        ledger = PreferenceLedger()
        ctx = ["user: x", "daniel: y"]
        ledger.record_frustration(_Envelope(ctx))
        ledger.note_idle(10.0, context=ctx)          # still working
        assert ledger.stats["attenuated"] == 0
        assert ledger.snapshot()["pending"] == 1     # frustration stands

    def test_nonzero_exit_does_not_attenuate(self):
        ledger = PreferenceLedger()
        ctx = ["user: build it", "daniel: done"]
        ledger.record_frustration(_Envelope(ctx))
        ledger.record_exit_code(1, context=ctx)       # the command FAILED
        assert ledger.stats["attenuated"] == 0
        assert ledger.snapshot()["pending"] == 1


class TestScaffoldingBias:
    def test_top_strategies_ranks_verified_patterns(self):
        ledger = PreferenceLedger()
        good = ["user: add caching", "daniel: use functools.lru_cache"]
        for _ in range(3):
            ledger.record_frustration(_Envelope(good))
            ledger.record_exit_code(0, context=good)  # repeatedly worked
        top = ledger.top_strategies()
        assert top and "lru_cache" in top[0]

    def test_format_for_prompt_empty_until_proven(self):
        ledger = PreferenceLedger()
        assert ledger.format_for_prompt() == ""       # no proof yet

    def test_format_for_prompt_injects_verified(self):
        ledger = PreferenceLedger()
        ctx = ["user: parse the config", "daniel: use tomllib"]
        for _ in range(2):
            ledger.record_frustration(_Envelope(ctx))
            ledger.record_exit_code(0, context=ctx)
        block = ledger.format_for_prompt()
        assert "Verified Operational Patterns" in block
        assert "tomllib" in block


class TestNoiseGuard:
    def test_lone_frustration_never_scores_negative(self):
        """A single frustration with NO override and NO downstream
        signal is noise — it must NOT sink the path's score."""
        ledger = PreferenceLedger()
        ctx = ["user: typo command", "daniel: response"]
        ledger.record_frustration(_Envelope(ctx))
        # No override, no exit code — just a pending frustration.
        # The path's SCORE (successes vs definitive negatives) is 0,
        # not negative — a typo doesn't poison the ledger.
        rec = ledger._paths[list(ledger._paths)[0]]
        assert rec.score == 0.0
        assert rec.definitive_negatives == 0


class TestIntegrationWiring:
    def test_qos_feeds_ledger_pin(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] /
               "backend/core/ouroboros/governance/comms/duplex/qos_sensor.py").read_text()
        assert "get_default_ledger" in src and "record_frustration" in src

    def test_strategic_direction_reads_ledger_pin(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] /
               "backend/core/ouroboros/governance/strategic_direction.py").read_text()
        assert "get_default_ledger" in src and "format_for_prompt" in src

    def test_peer_consensus_mounted_in_chat_pin(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] /
               "backend/core/ouroboros/governance/chat_text_bridge.py").read_text()
        body = src[src.index("async def _run"):][:900]
        assert "should_yield_to_karen" in body
        assert "daniel_yields_to_karen" in body
