"""Phase 12 Slice B — PromotionLedger regression spine.

Pins:
  §1 Empty/missing ledger boots clean
  §2 register_quarantine — idempotent, valid origins, malformed input rejected
  §3 record_success — ring buffer fills + clamps + resets failure_count
  §4 record_failure — counts up, doesn't demote unless promoted
  §5 is_eligible_for_promotion — strict gate (every latency, no failures, ring full)
  §6 promote — only when eligible; idempotent on already-promoted
  §7 demote — explicit + post-failure-while-promoted
  §8 disk persistence — atomic write, restart survival, schema mismatch
  §9 corrupt ledger boots empty (NEVER raises)
  §10 env-tunable thresholds (min_successes, max_latency, demotion_fail_threshold)
  §11 quarantined_models / promoted_models accessors
  §12 thread-safety via RLock (smoke test)
  §13 NEVER-raises contract on every public method
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any  # noqa: F401

import pytest

from backend.core.ouroboros.governance.dw_promotion_ledger import (
    LEDGER_SCHEMA_VERSION,
    QUARANTINE_AMBIGUOUS_METADATA,
    QUARANTINE_DEMOTED_FROM_BG,
    QUARANTINE_OPERATOR_DEMOTED,
    PromotionLedger,
    PromotionRecord,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_ledger_path(tmp_path: Path,
                         monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "dw_promotion_ledger.json"
    monkeypatch.setenv("JARVIS_DW_PROMOTION_LEDGER_PATH", str(p))
    return p


@pytest.fixture
def make_ledger(isolated_ledger_path: Path):
    def _factory(**kwargs) -> PromotionLedger:
        return PromotionLedger(**kwargs)
    return _factory


# ---------------------------------------------------------------------------
# §1 — Boot
# ---------------------------------------------------------------------------


def test_empty_ledger_boots_clean(make_ledger) -> None:
    led = make_ledger()
    led.load()
    assert led.quarantined_models() == ()
    assert led.promoted_models() == ()
    assert led.is_quarantined("any/model-1B") is False
    assert led.is_promoted("any/model-1B") is False


def test_missing_file_is_not_an_error(
    isolated_ledger_path: Path, make_ledger,
) -> None:
    assert not isolated_ledger_path.exists()
    led = make_ledger()
    led.load()  # should not raise
    assert led.all_snapshots() == ()


# ---------------------------------------------------------------------------
# §2 — register_quarantine
# ---------------------------------------------------------------------------


def test_register_quarantine_basic(make_ledger) -> None:
    led = make_ledger()
    led.register_quarantine("vendor/unknown-model")
    assert led.is_quarantined("vendor/unknown-model") is True
    assert led.is_promoted("vendor/unknown-model") is False
    snap = led.snapshot("vendor/unknown-model")
    assert snap is not None
    assert snap.quarantine_origin == QUARANTINE_AMBIGUOUS_METADATA


def test_register_quarantine_idempotent_preserves_progress(make_ledger) -> None:
    """Re-registering the same model on every catalog refresh must
    NOT wipe its accumulated success ring."""
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    led.record_success("x/m-1B", 110)
    led.register_quarantine("x/m-1B")  # second registration
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.success_latencies_ms == (100, 110)


def test_register_quarantine_rejects_invalid_origin(make_ledger) -> None:
    """Invalid origins normalize to ambiguous_metadata, no crash."""
    led = make_ledger()
    led.register_quarantine("x/m-1B", origin="not_a_valid_origin")
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.quarantine_origin == QUARANTINE_AMBIGUOUS_METADATA


def test_register_quarantine_rejects_empty_id(make_ledger) -> None:
    led = make_ledger()
    led.register_quarantine("")       # silent no-op
    led.register_quarantine("   ")    # silent no-op
    assert led.quarantined_models() == ()


# ---------------------------------------------------------------------------
# §3 — record_success
# ---------------------------------------------------------------------------


def test_record_success_appends_latency(make_ledger) -> None:
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 150)
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.success_latencies_ms == (150,)
    assert snap.failure_count == 0


def test_record_success_ring_buffer_clamps(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ring buffer must not grow past min_successes."""
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "3")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    for lat in (100, 110, 120, 130, 140):
        led.record_success("x/m-1B", lat)
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.success_latencies_ms == (120, 130, 140)


def test_record_success_resets_failure_count(make_ledger) -> None:
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_failure("x/m-1B")
    led.record_failure("x/m-1B")
    snap_before = led.snapshot("x/m-1B")
    assert snap_before is not None and snap_before.failure_count == 2
    led.record_success("x/m-1B", 100)
    snap_after = led.snapshot("x/m-1B")
    assert snap_after is not None and snap_after.failure_count == 0


def test_record_success_auto_registers_unknown_model(make_ledger) -> None:
    led = make_ledger()
    led.record_success("brand/new-model", 100)
    assert led.is_quarantined("brand/new-model") is True
    snap = led.snapshot("brand/new-model")
    assert snap is not None
    assert snap.success_latencies_ms == (100,)


def test_record_success_rejects_negative_latency(make_ledger) -> None:
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", -5)
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.success_latencies_ms == ()


def test_record_success_rejects_non_int(make_ledger) -> None:
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", "not a number")  # type: ignore[arg-type]
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.success_latencies_ms == ()


# ---------------------------------------------------------------------------
# §4 — record_failure
# ---------------------------------------------------------------------------


def test_record_failure_counts_up(make_ledger) -> None:
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_failure("x/m-1B")
    led.record_failure("x/m-1B")
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.failure_count == 2


def test_record_failure_does_not_demote_unpromoted(make_ledger) -> None:
    """A model that's already quarantined doesn't get re-demoted."""
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    triggered = led.record_failure("x/m-1B")
    assert triggered is False
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.promoted is False


def test_record_failure_demotes_promoted_model(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Single failure (default threshold=1) demotes a promoted model."""
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "3")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MAX_LATENCY_MS", "500")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    for lat in (100, 110, 120):
        led.record_success("x/m-1B", lat)
    assert led.promote("x/m-1B") is True
    assert led.is_promoted("x/m-1B") is True
    triggered = led.record_failure("x/m-1B")
    assert triggered is True
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.promoted is False
    assert snap.quarantine_origin == QUARANTINE_DEMOTED_FROM_BG
    assert snap.success_latencies_ms == ()  # ring reset


# ---------------------------------------------------------------------------
# §5 — is_eligible_for_promotion (strict gate)
# ---------------------------------------------------------------------------


def test_eligible_when_all_strict_conditions_met(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "3")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MAX_LATENCY_MS", "200")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    led.record_success("x/m-1B", 150)
    led.record_success("x/m-1B", 199)
    assert led.is_eligible_for_promotion("x/m-1B") is True


def test_ineligible_when_ring_underfilled(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "5")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    led.record_success("x/m-1B", 100)
    assert led.is_eligible_for_promotion("x/m-1B") is False


def test_ineligible_when_any_latency_exceeds_threshold(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strict — ALL must be <= threshold, not P95."""
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "3")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MAX_LATENCY_MS", "200")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    led.record_success("x/m-1B", 150)
    led.record_success("x/m-1B", 250)  # one over threshold
    assert led.is_eligible_for_promotion("x/m-1B") is False


def test_ineligible_with_failures_in_window(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "3")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    led.record_success("x/m-1B", 100)
    led.record_success("x/m-1B", 100)
    led.record_failure("x/m-1B")
    assert led.is_eligible_for_promotion("x/m-1B") is False


def test_ineligible_when_already_promoted(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "2")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    led.record_success("x/m-1B", 100)
    assert led.promote("x/m-1B") is True
    assert led.is_eligible_for_promotion("x/m-1B") is False


def test_ineligible_for_unknown_model(make_ledger) -> None:
    led = make_ledger()
    assert led.is_eligible_for_promotion("never/seen-model") is False


# ---------------------------------------------------------------------------
# §6 — promote
# ---------------------------------------------------------------------------


def test_promote_only_when_eligible(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "3")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    # Not yet eligible
    assert led.promote("x/m-1B") is False
    led.record_success("x/m-1B", 100)
    led.record_success("x/m-1B", 100)
    led.record_success("x/m-1B", 100)
    # Now eligible
    assert led.promote("x/m-1B") is True


def test_promote_records_promoted_at_unix(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    t0 = time.time()
    assert led.promote("x/m-1B") is True
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.promoted_at_unix is not None
    assert snap.promoted_at_unix >= t0


def test_promote_idempotent_returns_false(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    assert led.promote("x/m-1B") is True
    # Already promoted → second call returns False
    assert led.promote("x/m-1B") is False


# ---------------------------------------------------------------------------
# §7 — demote
# ---------------------------------------------------------------------------


def test_demote_resets_state(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    led.promote("x/m-1B")
    assert led.demote("x/m-1B") is True
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    assert snap.promoted is False
    assert snap.success_latencies_ms == ()
    assert snap.failure_count == 0
    assert snap.quarantine_origin == QUARANTINE_OPERATOR_DEMOTED


def test_demote_unknown_returns_false(make_ledger) -> None:
    led = make_ledger()
    assert led.demote("never/seen") is False


# ---------------------------------------------------------------------------
# §8 — disk persistence
# ---------------------------------------------------------------------------


def test_save_and_reload_roundtrip(
    isolated_ledger_path: Path, make_ledger,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "2")
    led1 = make_ledger()
    led1.register_quarantine("x/m-7B")
    led1.record_success("x/m-7B", 120)
    led1.record_success("x/m-7B", 130)
    led1.promote("x/m-7B")
    led1.register_quarantine("y/m-3B")
    led1.record_failure("y/m-3B")
    # Reload from disk
    led2 = make_ledger()
    led2.load()
    assert led2.is_promoted("x/m-7B") is True
    assert led2.is_quarantined("y/m-3B") is True
    s = led2.snapshot("x/m-7B")
    assert s is not None
    assert s.success_latencies_ms == (120, 130)


def test_autosave_off_does_not_persist(
    isolated_ledger_path: Path,
) -> None:
    led = PromotionLedger(autosave=False)
    led.register_quarantine("x/m-1B")
    # Disk should NOT have been written
    assert not isolated_ledger_path.exists()
    led.save()
    assert isolated_ledger_path.exists()


def test_schema_mismatch_starts_empty(
    isolated_ledger_path: Path, make_ledger,
) -> None:
    """Future-version cache → start empty (forces re-quarantine on
    next catalog discovery cycle)."""
    isolated_ledger_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_ledger_path.write_text(json.dumps({
        "schema_version": "dw_promotion.99",
        "records": [{"model_id": "should-be-ignored"}],
    }), encoding="utf-8")
    led = make_ledger()
    led.load()
    assert led.all_snapshots() == ()


# ---------------------------------------------------------------------------
# §9 — corrupt ledger boots empty (NEVER raises)
# ---------------------------------------------------------------------------


def test_corrupt_json_does_not_raise(
    isolated_ledger_path: Path, make_ledger,
) -> None:
    isolated_ledger_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_ledger_path.write_text("{not valid json", encoding="utf-8")
    led = make_ledger()
    led.load()  # should not raise
    assert led.all_snapshots() == ()


def test_malformed_record_skipped(
    isolated_ledger_path: Path, make_ledger,
) -> None:
    """One bad record doesn't blow up the whole ledger."""
    isolated_ledger_path.parent.mkdir(parents=True, exist_ok=True)
    isolated_ledger_path.write_text(json.dumps({
        "schema_version": LEDGER_SCHEMA_VERSION,
        "records": [
            {"model_id": "good/m-1B"},
            "this is not a dict",
            {"model_id": "", "extra": "garbage"},  # empty id, filtered
        ],
    }), encoding="utf-8")
    led = make_ledger()
    led.load()
    snaps = led.all_snapshots()
    assert len(snaps) == 1
    assert snaps[0].model_id == "good/m-1B"


# ---------------------------------------------------------------------------
# §10 — env-tunable thresholds
# ---------------------------------------------------------------------------


def test_min_successes_env_override(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    assert led.is_eligible_for_promotion("x/m-1B") is True


def test_max_latency_env_override(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MAX_LATENCY_MS", "50")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    assert led.is_eligible_for_promotion("x/m-1B") is False


def test_demotion_threshold_env_override(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    monkeypatch.setenv("JARVIS_DW_PROMOTION_DEMOTION_FAIL_THRESHOLD", "3")
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    led.record_success("x/m-1B", 100)
    led.promote("x/m-1B")
    assert led.record_failure("x/m-1B") is False  # 1 fail
    assert led.is_promoted("x/m-1B") is True
    assert led.record_failure("x/m-1B") is False  # 2 fails
    assert led.is_promoted("x/m-1B") is True
    assert led.record_failure("x/m-1B") is True   # 3 fails → demotion
    assert led.is_promoted("x/m-1B") is False


# ---------------------------------------------------------------------------
# §11 — Accessors
# ---------------------------------------------------------------------------


def test_quarantined_and_promoted_partition(
    make_ledger, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_DW_PROMOTION_MIN_SUCCESSES", "1")
    led = make_ledger()
    led.register_quarantine("a/m-1B")
    led.register_quarantine("b/m-1B")
    led.register_quarantine("c/m-1B")
    led.record_success("a/m-1B", 100)
    led.promote("a/m-1B")
    assert led.quarantined_models() == ("b/m-1B", "c/m-1B")
    assert led.promoted_models() == ("a/m-1B",)


# ---------------------------------------------------------------------------
# §12 — Thread-safety smoke test
# ---------------------------------------------------------------------------


def test_concurrent_record_success_does_not_corrupt(make_ledger) -> None:
    led = make_ledger()
    led.register_quarantine("x/m-1B")
    workers = []
    n_per_thread = 20
    n_threads = 4
    def _worker():
        for _ in range(n_per_thread):
            led.record_success("x/m-1B", 100)
    for _ in range(n_threads):
        t = threading.Thread(target=_worker)
        workers.append(t)
        t.start()
    for t in workers:
        t.join()
    snap = led.snapshot("x/m-1B")
    assert snap is not None
    # Ring buffer clamped at default min_successes (10)
    assert len(snap.success_latencies_ms) == 10


# ---------------------------------------------------------------------------
# §13 — NEVER-raises contract
# ---------------------------------------------------------------------------


def test_all_public_methods_tolerate_garbage(make_ledger) -> None:
    led = make_ledger()
    # None/empty/whitespace ids
    for bad in (None, "", "   ", "\t\n"):
        led.register_quarantine(bad)  # type: ignore[arg-type]
        led.record_success(bad, 100)  # type: ignore[arg-type]
        led.record_failure(bad)  # type: ignore[arg-type]
        assert led.is_quarantined(bad) is False  # type: ignore[arg-type]
        assert led.is_promoted(bad) is False  # type: ignore[arg-type]
        assert led.is_eligible_for_promotion(bad) is False  # type: ignore[arg-type]
        assert led.promote(bad) is False  # type: ignore[arg-type]
        assert led.demote(bad) is False  # type: ignore[arg-type]
        assert led.snapshot(bad) is None  # type: ignore[arg-type]


def test_promotion_record_from_json_dict_handles_garbage() -> None:
    # Empty/malformed
    assert PromotionRecord.from_json_dict({}) is None
    assert PromotionRecord.from_json_dict({"model_id": ""}) is None
    # Sane minimal
    rec = PromotionRecord.from_json_dict({"model_id": "x/m-1B"})
    assert rec is not None
    assert rec.model_id == "x/m-1B"
    assert rec.success_latencies_ms == []
