"""Capability Firing Dimension — runtime self-perception spine (Gap 2 Step 2).

Proves the precise merkle detector: a capability that is statically REACHABLE
(ALIVE) but emitted NO runtime firing evidence in the adaptive window is
flagged DORMANT — the exact class Step 1's static half could not distinguish.
Markers are DERIVED from each capability's own source (log tags + ledger
stems); evidence is durable (ledger mtimes + session debug.log markers) over a
session-anchored window; and a SILENT verdict is high-confidence only when a
LEDGER evidence-of-work channel went quiet (log-only silence → observability
gap, not proven dormancy).
"""
from __future__ import annotations

import backend.core.ouroboros.governance.capability_firing as cf


# ---------------------------------------------------------------------------
# Marker derivation (from source) — no hardcoding
# ---------------------------------------------------------------------------


def test_derive_log_tag_and_ledger():
    src = (
        'logger = logging.getLogger("Ouroboros.MerkleCartographer")\n'
        'logger.info("[MerkleCartographer] update_full complete")\n'
        'path = ".jarvis/merkle_history.jsonl"\n'
    )
    m = cf.derive_markers(src)
    assert "MerkleCartographer" in m.tags
    assert "merkle_history" in m.ledgers
    assert not m.empty


def test_derive_ignores_type_hint_brackets():
    src = "def f(x: List[Any]) -> Dict[str, Path]:\n    return {}\n"
    m = cf.derive_markers(src)
    # List[Any]/Dict[...]/Path are type hints, NOT log tags → no markers.
    assert "Any" not in m.tags and "Path" not in m.tags
    assert m.empty


def test_derive_empty_source():
    assert cf.derive_markers("").empty


# ---------------------------------------------------------------------------
# Firing verdict — FIRING / SILENT / UNKNOWN + channels
# ---------------------------------------------------------------------------


def _evidence(markers=(), ledgers=(), window_start=0.0):
    return cf.FiringEvidence(
        present_markers=set(markers), active_ledgers=set(ledgers),
        window_start_unix=window_start,
    )


def test_firing_when_ledger_active():
    src = 'x = ".jarvis/merkle_history.jsonl"\n'
    verdict, hits, channels = cf.firing_verdict(src, _evidence(ledgers=["merkle_history"]))
    assert verdict == "FIRING"
    assert "ledger:merkle_history" in hits
    assert "ledger" in channels


def test_firing_when_log_tag_present():
    src = 'logger.info("[MerkleCartographer] swept")\n'
    verdict, hits, channels = cf.firing_verdict(src, _evidence(markers=["MerkleCartographer"]))
    assert verdict == "FIRING" and "tag:MerkleCartographer" in hits


def test_silent_when_ledger_quiet_is_the_merkle_signature():
    # Derives a ledger marker, but the ledger is NOT active in-window → SILENT
    # with a ledger channel = high-confidence dormancy (merkle update_full case).
    src = 'x = ".jarvis/merkle_history.jsonl"\n'
    verdict, hits, channels = cf.firing_verdict(src, _evidence(ledgers=["other_ledger"]))
    assert verdict == "SILENT" and hits == [] and channels == ["ledger"]


def test_silent_log_only_is_observability_gap_not_dormant():
    # Only a log tag, absent → SILENT but log-only (may be running silently).
    src = 'logger.info("[QuietSubsystem] tick")\n'
    verdict, hits, channels = cf.firing_verdict(src, _evidence(markers=["Other"]))
    assert verdict == "SILENT" and channels == ["log"]


def test_unknown_when_no_markers():
    verdict, hits, channels = cf.firing_verdict("def f():\n    return 1\n", _evidence())
    assert verdict == "UNKNOWN" and hits == [] and channels == []


# ---------------------------------------------------------------------------
# Evidence collection — adaptive window, injectable readers, never raises
# ---------------------------------------------------------------------------


def test_evidence_adaptive_window_from_sessions():
    # A session named bt-2026-07-16-000000 → epoch anchors the window; the log
    # text yields present markers; ledger mtimes ≥ window_start are active.
    sess_epoch = cf._session_epoch("bt-2026-07-16-000000")
    assert sess_epoch is not None
    sessions = [("bt-2026-07-16-000000", "[MerkleCartographer] boot\n[Router] go\n")]
    ledgers = [
        ("merkle_history", sess_epoch + 10),   # written after window start → active
        ("stale_ledger", sess_epoch - 100000),  # before window start → inactive
    ]
    ev = cf.collect_firing_evidence(
        __import__("pathlib").Path("/nonexistent"), now=sess_epoch + 100,
        sessions_reader=lambda: sessions, ledger_reader=lambda: ledgers,
    )
    assert "MerkleCartographer" in ev.present_markers
    assert "Router" in ev.present_markers
    assert "merkle_history" in ev.active_ledgers
    assert "stale_ledger" not in ev.active_ledgers
    assert ev.window_start_unix == sess_epoch


def test_evidence_never_raises_on_bad_readers():
    def _boom():
        raise RuntimeError("reader down")

    ev = cf.collect_firing_evidence(
        __import__("pathlib").Path("/x"), now=1000.0,
        sessions_reader=_boom, ledger_reader=_boom,
    )
    assert isinstance(ev, cf.FiringEvidence)  # degrades, no raise


def test_build_firing_probe_returns_verdicts():
    sess_epoch = cf._session_epoch("bt-2026-07-16-000000")
    probe, ev = cf.build_firing_probe(
        __import__("pathlib").Path("/x"), now=sess_epoch + 50,
        sessions_reader=lambda: [("bt-2026-07-16-000000", "[Alpha] hi\n")],
        ledger_reader=lambda: [("beta_log", sess_epoch + 5)],
    )
    assert probe('logger.info("[Alpha] x")\n')[0] == "FIRING"
    assert probe('p = "beta_log.jsonl"\n')[0] == "FIRING"
    assert probe('p = "gamma.jsonl"\n')[0] == "SILENT"
    assert probe("def f(): pass\n")[0] == "UNKNOWN"


def test_probe_never_raises():
    probe, _ = cf.build_firing_probe(
        __import__("pathlib").Path("/x"), now=1.0,
        sessions_reader=lambda: [], ledger_reader=lambda: [],
    )
    assert probe(None)[0] == "UNKNOWN"  # type: ignore[arg-type]


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_CAPABILITY_FIRING_ENABLED", raising=False)
    assert cf.master_enabled() is True
    monkeypatch.setenv("JARVIS_CAPABILITY_FIRING_ENABLED", "0")
    assert cf.master_enabled() is False


# ---------------------------------------------------------------------------
# End-to-end integration with capability_liveness — DORMANT detection
# ---------------------------------------------------------------------------


def test_liveness_dormant_end_to_end(tmp_path, monkeypatch):
    """A reachable capability that writes a ledger, but the ledger went quiet →
    DORMANT in the combined liveness snapshot (the merkle class)."""
    import backend.core.ouroboros.governance.capability_liveness as cl
    import backend.core.ouroboros.governance.flag_registry as fr
    from tests.governance.test_capability_liveness import (
        _FakeRegistry, _FakeSpec, _repo, _write,
    )

    repo = _repo(tmp_path)
    # A capability module: a called function (→ ALIVE) that writes a ledger.
    mod = _write(repo, "backend/gov/sweeper.py",
                 'def sweep():\n    open(".jarvis/sweep_history.jsonl", "a")\n    return 1\n')
    _write(repo, "backend/gov/driver.py",
           "from backend.gov.sweeper import sweep\n\ndef boot():\n    return sweep()\n")
    monkeypatch.setattr(fr, "ensure_seeded",
                        lambda: _FakeRegistry([_FakeSpec("JARVIS_SWEEP_ENABLED", mod)]))
    # Inject a firing probe whose evidence has NO active ledgers (sweep_history
    # never written in-window) → the reachable capability is DORMANT.
    probe, _ev = cf.build_firing_probe(
        repo, now=1000.0,
        sessions_reader=lambda: [], ledger_reader=lambda: [],
    )
    cl.reset_cache_for_tests()
    snap = cl.aggregate_capability_liveness(
        repo_root=repo, now=1000.0, firing_probe=probe,
    )
    assert snap.counts.get("ALIVE") == 1              # statically reachable
    assert len(snap.dormant) == 1                     # but ledger went quiet
    assert snap.dormant[0]["flag"] == "JARVIS_SWEEP_ENABLED"
    assert snap.dormant[0]["firing"] == "SILENT"
    assert "ledger" in snap.dormant[0]["firing_channels"]


def test_liveness_firing_marks_not_dormant(tmp_path, monkeypatch):
    """Same capability, but its ledger IS active in-window → FIRING, not
    dormant (the merkle post-Slice-31 case)."""
    import backend.core.ouroboros.governance.capability_liveness as cl
    import backend.core.ouroboros.governance.flag_registry as fr
    from tests.governance.test_capability_liveness import (
        _FakeRegistry, _FakeSpec, _repo, _write,
    )

    repo = _repo(tmp_path)
    mod = _write(repo, "backend/gov/sweeper2.py",
                 'def sweep2():\n    open(".jarvis/sweep2_history.jsonl", "a")\n    return 1\n')
    _write(repo, "backend/gov/driver2.py",
           "from backend.gov.sweeper2 import sweep2\n\ndef boot():\n    return sweep2()\n")
    monkeypatch.setattr(fr, "ensure_seeded",
                        lambda: _FakeRegistry([_FakeSpec("JARVIS_SWEEP2_ENABLED", mod)]))
    probe, _ev = cf.build_firing_probe(
        repo, now=1000.0,
        sessions_reader=lambda: [],
        ledger_reader=lambda: [("sweep2_history", 999.0)],  # active in-window
    )
    cl.reset_cache_for_tests()
    snap = cl.aggregate_capability_liveness(
        repo_root=repo, now=1000.0, firing_probe=probe,
    )
    assert snap.counts.get("ALIVE") == 1
    assert len(snap.dormant) == 0
    assert snap.firing_evidence.get("counts", {}).get("FIRING") == 1
