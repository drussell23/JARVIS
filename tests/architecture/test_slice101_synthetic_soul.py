"""Slice 101 Phase 6 — the Synthetic Soul: cross-session deep-memory verification.

Simulates three independent failing coding sessions, triggers the asynchronous
sleep-consolidation cascade, then simulates a fresh rebooted session and asserts
the organism inherently retrieves the consolidated memory and avoids the
previously-failing paradigm — with zero false positives on clean paths.

The persistent engram is the on-disk JSONL ledger set (belief-revision +
schelling/meta-prior); a "reboot" is a fresh read of those files (no in-memory
carry-over), which is exactly what the substrates do on next boot.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import sleep_daemon as SD
from backend.core.ouroboros.governance.belief_revision_ledger import (
    EvidenceKind,
    record_claim,
    record_evidence,
)
from backend.core.ouroboros.governance.cognitive_subscribers import (
    recent_avoidance_digest,
)

_FAILING_PARADIGM = "backend/core/ouroboros/governance/meta/ast_dynamic_imports.py"
_CLEAN_FILE = "backend/core/ouroboros/governance/serpent_animation.py"


@pytest.fixture(autouse=True)
def _soul_env(monkeypatch, tmp_path):
    """All masters ON; every ledger redirected to a tmp file so the test is
    hermetic and the 'reboot' is a real on-disk re-read."""
    monkeypatch.setenv("JARVIS_COGNITIVE_BUS_ENABLED", "1")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "1")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_PERSIST_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_BELIEF_REVISION_LEDGER_PATH", str(tmp_path / "belief.jsonl"),
    )
    monkeypatch.setenv("JARVIS_POSTMORTEM_FUSION_ENABLED", "1")
    monkeypatch.setenv("JARVIS_SLEEP_CONSOLIDATION_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_SLEEP_CONSOLIDATION_LEDGER_PATH",
        str(tmp_path / "consolidation.jsonl"),
    )
    monkeypatch.setenv("JARVIS_META_PRIOR_LEARNING_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_META_PRIOR_LEARNING_LEDGER_PATH", str(tmp_path / "meta.jsonl"),
    )
    monkeypatch.setenv("JARVIS_SCHELLING_PRIOR_ENABLED", "1")
    monkeypatch.setenv("JARVIS_SCHELLING_PRIOR_PERSIST_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_SCHELLING_PRIOR_LEDGER_PATH", str(tmp_path / "schelling.jsonl"),
    )
    monkeypatch.setenv("JARVIS_SLEEP_DAEMON_ENABLED", "1")
    yield


def _record_failing_session(session_idx: int) -> None:
    """One failing session: a falsified belief about the failing paradigm file
    (two falsifying records exceed the default falsify_threshold=2 → FALSIFIED)."""
    claim = record_claim(
        f"generation in [{_FAILING_PARADIGM}] succeeds",
        "generation/failure",
        target_files=[_FAILING_PARADIGM],
    )
    assert claim is not None
    for _ in range(2):
        record_evidence(
            claim.claim_id,
            EvidenceKind.FALSIFYING,
            source_op_id=f"op-s{session_idx}",
            source_session_id=f"session-{session_idx}",
        )


# === THE MARQUEE: 3 failing sessions → consolidate → reboot → retrieve ======

def test_three_failing_sessions_retrieved_after_reboot_zero_fp(tmp_path):
    # --- three independent failing sessions ---
    for s in range(3):
        _record_failing_session(s)

    # --- trigger the asynchronous sleep consolidation pass ---
    report = SD.run_sleep_cycle_once(idle_seconds=99999.0)
    assert report.master_enabled is True
    assert report.consolidation_verdict != "disabled"

    # --- persistence: the engram is on disk, surviving any reboot ---
    belief_ledger = tmp_path / "belief.jsonl"
    assert belief_ledger.exists()
    assert belief_ledger.stat().st_size > 0

    # --- simulate a fresh rebooted session: a brand-new read of the ledgers ---
    digest = recent_avoidance_digest()

    # The fresh session inherently KNOWS the failing paradigm.
    assert _FAILING_PARADIGM in digest
    # ...and actively avoids it (the block instructs caution / diagnosis).
    assert "Recently-Failing Areas" in digest
    # ZERO false positives: a clean, never-failed path is NOT flagged.
    assert _CLEAN_FILE not in digest


# === The sleep daemon: cascade + async off-hot-path behaviour ===============

def test_sleep_cycle_runs_cascade_without_raising():
    report = SD.run_sleep_cycle_once(idle_seconds=99999.0)
    assert report.master_enabled is True
    # The cascade executed (consolidation produced a real verdict, not disabled).
    assert report.consolidation_verdict in ("awake", "dreaming", "consolidated")


def test_sleep_cycle_inert_when_master_off(monkeypatch):
    monkeypatch.delenv("JARVIS_SLEEP_DAEMON_ENABLED", raising=False)
    report = SD.run_sleep_cycle_once()
    assert report.master_enabled is False
    assert report.consolidation_verdict == "disabled"


def test_sleep_daemon_loop_is_bounded_and_non_blocking():
    # The daemon runs off the hot path: a fake sleep proves we yield between
    # cycles (never a blocking compute), and max_cycles bounds it for the test.
    slept = []

    async def _fake_sleep(s):
        slept.append(s)

    cycles = asyncio.run(
        SD.run_sleep_daemon_loop(interval_s=10.0, max_cycles=3, sleep_fn=_fake_sleep)
    )
    assert cycles == 3
    # Yielded between cycles (2 inter-cycle sleeps for 3 cycles).
    assert len(slept) == 2


def test_sleep_daemon_loop_inert_when_master_off(monkeypatch):
    monkeypatch.delenv("JARVIS_SLEEP_DAEMON_ENABLED", raising=False)
    cycles = asyncio.run(SD.run_sleep_daemon_loop(max_cycles=5))
    assert cycles == 0


# === Consolidated meta-prior: retrieved at GENERATE across reboot ===========

def test_consolidated_meta_prior_retrieved_after_reboot():
    from backend.core.ouroboros.governance.schelling_consensus_prior import (
        record_prior_outcome,
    )
    from backend.core.ouroboros.governance.meta_prior_learning import (
        MetaPriorVerdict,
        compute_meta_distribution,
    )
    from backend.core.ouroboros.governance.strategic_direction import (
        StrategicDirectionService,
    )

    # Seed a consistently-winning strategic prior across many observations.
    for i in range(25):
        record_prior_outcome("factory_pattern_refactor", f"op-{i}", "sig-x", True)

    # The consolidated meta-distribution recognises it as DOMINANT.
    report = compute_meta_distribution()
    kinds_dominant = {
        p.prior_kind for p in report.per_prior
        if p.verdict is MetaPriorVerdict.DOMINANT
    }
    assert "factory_pattern_refactor" in kinds_dominant

    # Fresh rebooted session: the GENERATE injection surfaces it (reads the
    # persisted schelling/meta ledgers — survives reboot).
    svc = object.__new__(StrategicDirectionService)
    block = svc._render_consolidated_memory_section()
    assert "factory_pattern_refactor" in block
    assert "Consolidated Strategic Priors" in block


def test_consolidated_memory_inert_when_meta_off(monkeypatch):
    from backend.core.ouroboros.governance.strategic_direction import (
        StrategicDirectionService,
    )
    monkeypatch.delenv("JARVIS_META_PRIOR_LEARNING_ENABLED", raising=False)
    svc = object.__new__(StrategicDirectionService)
    assert svc._render_consolidated_memory_section() == ""
