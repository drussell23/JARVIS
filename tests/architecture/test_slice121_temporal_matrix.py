"""Slice 121 — Adversarial Volume & Concurrency-Hardening Matrix.

Marquee proofs:
  1. The hash chain stays MATHEMATICALLY UNBROKEN under heavy concurrent writes
     (many threads → one lock-guarded ledger → verify_ledger True).
  2. The HONESTY invariant: the report is labelled a volume/concurrency
     statistic and carries NO wall-clock-equivalence / "months simulated"
     claim — parallelism is not time-compression.
"""

from __future__ import annotations

import itertools

from backend.core.ouroboros.governance.red_blue_matrix import BlueEvidenceLedger, verify_ledger
from backend.core.ouroboros.governance.temporal_matrix import (
    AttackResult,
    MatrixReport,
    ThreadSafeLedger,
    drive_concurrent_siege,
    matrix_concurrency,
    temporal_matrix_enabled,
)


def test_master_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_TEMPORAL_MATRIX_ENABLED", raising=False)
    assert temporal_matrix_enabled() is False
    monkeypatch.setenv("JARVIS_TEMPORAL_MATRIX_ENABLED", "1")
    assert temporal_matrix_enabled() is True


def test_concurrency_clamped(monkeypatch):
    monkeypatch.setenv("JARVIS_TEMPORAL_MATRIX_CONCURRENCY", "99999")
    assert matrix_concurrency() == 256  # clamped to _MAX_CONCURRENCY
    monkeypatch.setenv("JARVIS_TEMPORAL_MATRIX_CONCURRENCY", "0")
    assert matrix_concurrency() == 1
    monkeypatch.setenv("JARVIS_TEMPORAL_MATRIX_CONCURRENCY", "garbage")
    assert matrix_concurrency() == 8


class TestConcurrencyCorrectness:
    def test_chain_unbroken_under_heavy_concurrent_writes(self, tmp_path):
        # 16 producer threads, each emitting 100 attacks → 1600 receipts, all
        # linked through one lock-guarded ledger. The chain MUST stay intact.
        ledger = ThreadSafeLedger(BlueEvidenceLedger(path=tmp_path / "evidence.jsonl"))
        _ctr = itertools.count()

        def producer():
            return [
                AttackResult(
                    attack_class="ast_bypass",
                    payload=f"payload-{next(_ctr)}",
                    verdict="POLICY_DENIED",
                    blocked=True,
                    blocked_by="iron_gate",
                )
                for _ in range(100)
            ]

        rep = drive_concurrent_siege(
            attack_results_producer=producer, concurrency=16, ledger=ledger,
        )
        assert rep.total_attacks == 1600
        assert rep.receipts_written == 1600
        # THE marquee assertion: tamper-evident chain holds under contention.
        assert rep.chain_intact is True, rep.chain_detail
        intact, detail = verify_ledger(ledger.path)
        assert intact is True, detail

    def test_seq_numbers_are_contiguous_no_dupes(self, tmp_path):
        ledger = ThreadSafeLedger(BlueEvidenceLedger(path=tmp_path / "e.jsonl"))
        _ctr = itertools.count()

        def producer():
            return [AttackResult("c", f"p{next(_ctr)}", "v", True) for _ in range(50)]

        drive_concurrent_siege(attack_results_producer=producer, concurrency=8, ledger=ledger)
        seqs = []
        import json

        for line in ledger.path.read_text().splitlines():
            seqs.append(json.loads(line)["seq"])
        # No lost/duplicated sequence numbers despite 8 concurrent writers.
        assert sorted(seqs) == list(range(400))

    def test_escape_rate_computed(self, tmp_path):
        ledger = ThreadSafeLedger(BlueEvidenceLedger(path=tmp_path / "e.jsonl"))
        _ctr = itertools.count()

        def producer():
            # 1 in 10 escapes.
            out = []
            for _ in range(10):
                i = next(_ctr)
                out.append(AttackResult("c", f"p{i}", "v", blocked=(i % 10 != 0)))
            return out

        rep = drive_concurrent_siege(attack_results_producer=producer, concurrency=4, ledger=ledger)
        assert rep.total_attacks == 40
        assert rep.escaped == 4
        assert abs(rep.escape_rate - 0.1) < 1e-9


class TestHonestyInvariant:
    """Parallelism is throughput, not duration — the report must say so."""

    def test_report_kind_is_volume_not_duration(self):
        rep = MatrixReport(concurrency=50, total_attacks=5000)
        d = rep.to_dict()
        assert d["evidence_kind"] == "adversarial_volume_concurrency"
        assert "wall_clock_seconds" in d  # it reports REAL elapsed time...
        # ...and NEVER a calendar-equivalence claim.
        blob = str(d).lower()
        for forbidden in ("months_simulated", "years_simulated", "calendar_equivalent",
                          "equivalent_months", "equivalent_years", "simulated_duration"):
            assert forbidden not in blob

    def test_disclaimer_states_not_a_substitute(self):
        d = MatrixReport().to_dict()
        low = d["disclaimer"].lower()
        assert "not" in low and ("substitute" in low or "duration" in low)
        assert "t5" in low or "wall-clock" in low
