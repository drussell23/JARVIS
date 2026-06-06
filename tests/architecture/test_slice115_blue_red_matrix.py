"""Slice 115 — Blue/Red Adversarial Falsification Matrix.

Proves: (1) the Blue ledger writes tamper-evident hash-chained receipts and
DETECTS post-hoc tampering; (2) the recursion-depth Red surface drives the gate
past MAX_RECURSION_DEPTH and the gate HALTS it, with a receipt logged; (3) the
containment surface composes adversarial_sweep and records receipts; (4) every
blocked attack produces a verifiable receipt (the honest version of "Blue logs
it 100 % of the time" — NOT a faked 100 % block rate).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.governance import red_blue_matrix as RB
from backend.core.ouroboros.governance.red_blue_matrix import (
    ATTACK_CONTAINMENT,
    ATTACK_RECURSION,
    BlueEvidenceLedger,
    matrix_enabled,
    run_recursion_siege,
    run_siege,
    verify_ledger,
)


# ===========================================================================
# Masters
# ===========================================================================


def test_masters_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_RED_BLUE_MATRIX_ENABLED", raising=False)
    assert matrix_enabled() is False
    monkeypatch.setenv("JARVIS_RED_BLUE_MATRIX_ENABLED", "1")
    assert matrix_enabled() is True


# ===========================================================================
# Blue ledger — tamper-evident hash-chained receipts
# ===========================================================================


class TestBlueLedger:
    def test_records_chained_receipts(self, tmp_path):
        led = BlueEvidenceLedger(tmp_path / "evidence.jsonl")
        r0 = led.record(attack_class=ATTACK_RECURSION, payload="p0", verdict="halt",
                        blocked=True, blocked_by="recursion_depth_gate")
        r1 = led.record(attack_class=ATTACK_CONTAINMENT, payload="p1", verdict="blocked_ast",
                        blocked=True, blocked_by="ast")
        assert r0.seq == 0 and r1.seq == 1
        assert r1.prev_hash == r0.record_hash          # chain links
        assert r0.payload_sha256 != r1.payload_sha256  # distinct payload hashes
        ok, reason = verify_ledger(led.path)
        assert ok, reason

    def test_resume_continues_chain(self, tmp_path):
        p = tmp_path / "evidence.jsonl"
        BlueEvidenceLedger(p).record(attack_class=ATTACK_RECURSION, payload="a", verdict="halt", blocked=True)
        # New ledger instance over the same file resumes seq + chain.
        led2 = BlueEvidenceLedger(p)
        r = led2.record(attack_class=ATTACK_RECURSION, payload="b", verdict="halt", blocked=True)
        assert r.seq == 1
        ok, _ = verify_ledger(p)
        assert ok

    def test_tampering_is_detected(self, tmp_path):
        p = tmp_path / "evidence.jsonl"
        led = BlueEvidenceLedger(p)
        led.record(attack_class=ATTACK_RECURSION, payload="x", verdict="halt", blocked=True)
        led.record(attack_class=ATTACK_CONTAINMENT, payload="y", verdict="blocked_ast", blocked=True)
        # Forge the evidence: flip the first receipt's verdict from blocked → escaped.
        lines = p.read_text().splitlines()
        rec0 = json.loads(lines[0]); rec0["blocked"] = False; rec0["verdict"] = "passed_through"
        lines[0] = json.dumps(rec0, separators=(",", ":"))
        p.write_text("\n".join(lines) + "\n")
        ok, reason = verify_ledger(p)
        assert ok is False                  # tamper-evident
        assert "tampered" in reason or "chain" in reason


# ===========================================================================
# Red surface 2 (NEW) — recursion-depth bound siege
# ===========================================================================


class TestRecursionSiege:
    def test_gate_halts_chain_beyond_bound_and_logs_receipt(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_RECURSION_DEPTH_GATE_ENABLED", "1")  # default-TRUE anyway
        led = BlueEvidenceLedger(tmp_path / "evidence.jsonl")
        attacks, blocked = run_recursion_siege(led)
        assert attacks >= 1
        assert blocked == attacks, "the recursion gate MUST halt every over-bound chain"
        # Every block produced a verifiable receipt attributing the recursion gate.
        recs = [json.loads(l) for l in led.path.read_text().splitlines() if l.strip()]
        assert recs and all(r["attack_class"] == ATTACK_RECURSION for r in recs)
        assert all(r["blocked"] and r["blocked_by"] == "recursion_depth_gate" for r in recs)
        ok, _ = verify_ledger(led.path)
        assert ok


# ===========================================================================
# Containment surface — composes the EXISTING adversarial_sweep (Red engine)
# ===========================================================================


class TestContainmentSiege:
    @pytest.mark.asyncio
    async def test_composes_sweep_and_records(self, tmp_path, monkeypatch):
        # Mock the (heavy, real-cage) sweep for a deterministic unit test.
        class _FakeSweep:
            total_variants = 40
            adversarial_escape_count_with_mutations = 2
            adversarial_escape_count_raw = 1
            mutation_induced_escapes = ({"seed": "s1", "strategy": "alias"},
                                        {"seed": "s2", "strategy": "synonym"})
        import backend.core.ouroboros.governance.graduation.adversarial_sweep as SW
        async def _fake_run_sweep(**kw):
            return _FakeSweep()
        monkeypatch.setattr(SW, "run_sweep", _fake_run_sweep)
        led = BlueEvidenceLedger(tmp_path / "evidence.jsonl")
        attacks, blocked = await RB.run_containment_siege(led)
        assert attacks == 40 and blocked == 38
        # 1 summary receipt + 2 escape receipts.
        recs = [json.loads(l) for l in led.path.read_text().splitlines() if l.strip()]
        assert len(recs) == 3  # 1 summary + 2 per-escape receipts
        # The 2 individual mutation-induced escapes are recorded honestly as
        # passed_through (the summary receipt also reflects non-full containment).
        assert sum(1 for r in recs if r["verdict"] == "passed_through") == 2
        ok, _ = verify_ledger(led.path)
        assert ok


# ===========================================================================
# Full siege — honest aggregate + master gating
# ===========================================================================


class TestRunSiege:
    @pytest.mark.asyncio
    async def test_inert_when_master_off(self, tmp_path, monkeypatch):
        monkeypatch.delenv("JARVIS_RED_BLUE_MATRIX_ENABLED", raising=False)
        rep = await run_siege(ledger=BlueEvidenceLedger(tmp_path / "e.jsonl"))
        assert rep.attacks == 0 and rep.receipts_written == 0

    @pytest.mark.asyncio
    async def test_siege_records_and_reports(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_RED_BLUE_MATRIX_ENABLED", "1")
        monkeypatch.setenv("JARVIS_RECURSION_DEPTH_GATE_ENABLED", "1")
        import backend.core.ouroboros.governance.graduation.adversarial_sweep as SW
        class _FakeSweep:
            total_variants = 10
            adversarial_escape_count_with_mutations = 0
            adversarial_escape_count_raw = 0
            mutation_induced_escapes = ()
        async def _fake(**kw):
            return _FakeSweep()
        monkeypatch.setattr(SW, "run_sweep", _fake)
        led = BlueEvidenceLedger(tmp_path / "e.jsonl")
        rep = await run_siege(ledger=led)
        assert rep.attacks > 0
        assert rep.receipts_written > 0
        assert ATTACK_RECURSION in rep.per_surface and ATTACK_CONTAINMENT in rep.per_surface
        ok, _ = verify_ledger(led.path)
        assert ok
