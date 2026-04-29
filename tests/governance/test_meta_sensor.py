"""Priority B Slice B1 — MetaSensor (degenerate-loop dormancy alarm).

The immune system policing itself. Watches for the structural
signature that nuked Phase 2 in soak #3: most postmortems have
total_claims=0 (PLAN-time claim capture silently disabled).

Pins:
  §1   Master flag default false (opt-in until graduation)
  §2   Master flag truthy/falsy contract
  §3   DormancyFinding is frozen + hashable
  §4   DormancyFinding.evidence_dict round-trip
  §5   DormancyDetector is frozen + hashable
  §6   register_dormancy_detector idempotent on identical
  §7   register_dormancy_detector rejects different-callable without
       overwrite
  §8   unregister_dormancy_detector returns True/False
  §9   list_dormancy_detectors alphabetical-stable
  §10  Seed detector empty_postmortem_rate registered at module load
  §11  reset_registry_for_tests clears + re-seeds
  §12  Threshold env override clamped to [0, 1]
  §13  Window env override floored at 10
  §14  Min-records env override floored at 1
  §15  Empty-postmortem detector returns None on insufficient sample
  §16  Empty-postmortem detector returns None below threshold
  §17  Empty-postmortem detector fires above threshold
  §18  Empty-postmortem finding has p1 severity + structured evidence
  §19  Empty-postmortem detector NEVER raises (verification missing
       OR ledger unreadable → returns None silently)
  §20  list_recent_postmortems integration: returns recent records
  §21  list_recent_postmortems master-off behavior (no records)
  §22  MetaSensor master-off start() is no-op (no task spawned)
  §23  MetaSensor master-on start() spawns task
  §24  MetaSensor.scan_once master-off returns []
  §25  MetaSensor.scan_once dedup — same finding fires only once
  §26  MetaSensor.scan_once emits envelope with required fields
  §27  Authority invariants — no orchestrator/phase_runner imports
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any, List

import pytest

from backend.core.ouroboros.governance.intake.sensors.meta_sensor import (
    DormancyDetector,
    DormancyFinding,
    META_SENSOR_SCHEMA_VERSION,
    MetaSensor,
    empty_postmortem_min_records,
    empty_postmortem_threshold,
    empty_postmortem_window,
    list_dormancy_detectors,
    meta_sensor_enabled,
    register_dormancy_detector,
    reset_registry_for_tests,
    unregister_dormancy_detector,
)


@pytest.fixture
def fresh_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# ===========================================================================
# §1-§2 — Master flag
# ===========================================================================


def test_master_flag_default_false(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_META_SENSOR_ENABLED", raising=False)
    assert meta_sensor_enabled() is False


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_master_flag_truthy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_META_SENSOR_ENABLED", val)
    assert meta_sensor_enabled() is True


@pytest.mark.parametrize(
    "val", ["", " ", "0", "false", "no", "off", "garbage"],
)
def test_master_flag_falsy(monkeypatch, val) -> None:
    monkeypatch.setenv("JARVIS_META_SENSOR_ENABLED", val)
    assert meta_sensor_enabled() is False


# ===========================================================================
# §3-§5 — Schema (frozen + hashable)
# ===========================================================================


def test_finding_is_frozen() -> None:
    f = DormancyFinding(
        detector_kind="x", severity="p1", summary="test",
    )
    with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
        f.detector_kind = "y"  # type: ignore[misc]


def test_finding_evidence_dict_round_trip() -> None:
    f = DormancyFinding(
        detector_kind="x", severity="p1", summary="test",
        evidence=(("k1", 1), ("k2", "v")),
    )
    d = f.evidence_dict()
    assert d == {"k1": 1, "k2": "v"}
    assert f.schema_version == META_SENSOR_SCHEMA_VERSION


def test_detector_is_frozen() -> None:
    d = DormancyDetector(
        detector_kind="x", severity="p1",
        description="test", evaluate=lambda: None,
    )
    with pytest.raises((AttributeError, FrozenInstanceError, TypeError)):
        d.detector_kind = "y"  # type: ignore[misc]


# ===========================================================================
# §6-§9 — Registry surface
# ===========================================================================


def test_register_idempotent_on_identical(fresh_registry) -> None:
    fn = lambda: None
    detector = DormancyDetector(
        detector_kind="custom", severity="p2",
        description="d", evaluate=fn,
    )
    register_dormancy_detector(detector)
    register_dormancy_detector(detector)  # silent no-op
    custom_count = sum(
        1 for d in list_dormancy_detectors()
        if d.detector_kind == "custom"
    )
    assert custom_count == 1


def test_register_rejects_different_without_overwrite(
    fresh_registry,
) -> None:
    fn1 = lambda: None
    fn2 = lambda: None
    d1 = DormancyDetector(
        detector_kind="custom", severity="p2",
        description="A", evaluate=fn1,
    )
    d2 = DormancyDetector(
        detector_kind="custom", severity="p3",
        description="B", evaluate=fn2,
    )
    register_dormancy_detector(d1)
    register_dormancy_detector(d2)  # logged but not replaced
    custom = [
        d for d in list_dormancy_detectors()
        if d.detector_kind == "custom"
    ]
    assert len(custom) == 1
    assert custom[0].description == "A"


def test_unregister_returns_correct_status(fresh_registry) -> None:
    register_dormancy_detector(
        DormancyDetector(
            detector_kind="ephemeral", severity="p3",
            description="d", evaluate=lambda: None,
        ),
    )
    assert unregister_dormancy_detector("ephemeral") is True
    assert unregister_dormancy_detector("ephemeral") is False
    assert unregister_dormancy_detector("never_registered") is False


def test_list_alphabetical_stable(fresh_registry) -> None:
    detectors = list_dormancy_detectors()
    kinds = [d.detector_kind for d in detectors]
    assert kinds == sorted(kinds)


# ===========================================================================
# §10-§11 — Seed detectors
# ===========================================================================


def test_empty_postmortem_seed_registered(fresh_registry) -> None:
    detectors = list_dormancy_detectors()
    kinds = [d.detector_kind for d in detectors]
    assert "empty_postmortem_rate" in kinds
    epr = next(
        d for d in detectors if d.detector_kind == "empty_postmortem_rate"
    )
    assert epr.severity == "p1"


def test_reset_clears_and_reseeds(fresh_registry) -> None:
    register_dormancy_detector(
        DormancyDetector(
            detector_kind="extra", severity="p3",
            description="d", evaluate=lambda: None,
        ),
    )
    kinds = [d.detector_kind for d in list_dormancy_detectors()]
    assert "extra" in kinds
    reset_registry_for_tests()
    kinds = [d.detector_kind for d in list_dormancy_detectors()]
    assert "extra" not in kinds
    assert "empty_postmortem_rate" in kinds


# ===========================================================================
# §12-§14 — Env-tunable thresholds
# ===========================================================================


def test_threshold_clamped_to_unit_interval(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_THRESHOLD", "5.0",
    )
    assert empty_postmortem_threshold() == 1.0
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_THRESHOLD", "-0.5",
    )
    assert empty_postmortem_threshold() == 0.0
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_THRESHOLD", "garbage",
    )
    assert empty_postmortem_threshold() == 0.7


def test_window_floored_at_10(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_META_EMPTY_POSTMORTEM_WINDOW", "5")
    assert empty_postmortem_window() == 10
    monkeypatch.setenv("JARVIS_META_EMPTY_POSTMORTEM_WINDOW", "500")
    assert empty_postmortem_window() == 500


def test_min_records_floored_at_1(monkeypatch) -> None:
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_MIN_RECORDS", "0",
    )
    assert empty_postmortem_min_records() == 1
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_MIN_RECORDS", "-5",
    )
    assert empty_postmortem_min_records() == 1


# ===========================================================================
# §15-§19 — Empty-postmortem detector evaluation
# ===========================================================================


@pytest.fixture
def isolated_ledger(tmp_path, monkeypatch):
    """Isolated determinism ledger for postmortem-population tests."""
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"),
    )
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_VERIFICATION_POSTMORTEM_ENABLED", "true",
    )
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "meta-sensor-test")
    from backend.core.ouroboros.governance.determinism.decision_runtime import (
        reset_all_for_tests,
    )
    reset_all_for_tests()
    yield tmp_path
    reset_all_for_tests()


async def _seed_postmortems(*, empty_count: int, total_count: int) -> None:
    """Helper — write `total_count` postmortems, of which `empty_count`
    have total_claims=0 and the rest have total_claims>=1."""
    from backend.core.ouroboros.governance.verification import (
        VerificationPostmortem, persist_postmortem,
    )
    for i in range(total_count):
        is_empty = i < empty_count
        pm = VerificationPostmortem(
            op_id=f"op-{i}",
            session_id="meta-sensor-test",
            started_unix=float(i),
            completed_unix=float(i + 1),
            total_claims=0 if is_empty else 3,
            must_hold_count=0 if is_empty else 3,
        )
        await persist_postmortem(
            pm=pm, op_id=pm.op_id,
            ctx=SimpleNamespace(op_id=pm.op_id),
        )


def test_detector_returns_none_on_insufficient_sample(
    isolated_ledger, fresh_registry, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_MIN_RECORDS", "20",
    )
    asyncio.run(_seed_postmortems(empty_count=5, total_count=10))
    epr = next(
        d for d in list_dormancy_detectors()
        if d.detector_kind == "empty_postmortem_rate"
    )
    finding = epr.evaluate()
    assert finding is None  # < min_records


def test_detector_returns_none_below_threshold(
    isolated_ledger, fresh_registry, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_THRESHOLD", "0.7",
    )
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_MIN_RECORDS", "5",
    )
    # 5/20 = 25% empty — below 70% threshold
    asyncio.run(_seed_postmortems(empty_count=5, total_count=20))
    epr = next(
        d for d in list_dormancy_detectors()
        if d.detector_kind == "empty_postmortem_rate"
    )
    finding = epr.evaluate()
    assert finding is None


def test_detector_fires_above_threshold(
    isolated_ledger, fresh_registry, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_THRESHOLD", "0.7",
    )
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_MIN_RECORDS", "5",
    )
    # 18/20 = 90% empty — above 70% threshold (matches soak #3 reality)
    asyncio.run(_seed_postmortems(empty_count=18, total_count=20))
    epr = next(
        d for d in list_dormancy_detectors()
        if d.detector_kind == "empty_postmortem_rate"
    )
    finding = epr.evaluate()
    assert finding is not None
    assert finding.detector_kind == "empty_postmortem_rate"


def test_finding_severity_and_evidence(
    isolated_ledger, fresh_registry, monkeypatch,
) -> None:
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_THRESHOLD", "0.7",
    )
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_MIN_RECORDS", "5",
    )
    asyncio.run(_seed_postmortems(empty_count=18, total_count=20))
    epr = next(
        d for d in list_dormancy_detectors()
        if d.detector_kind == "empty_postmortem_rate"
    )
    finding = epr.evaluate()
    assert finding is not None
    assert finding.severity == "p1"
    ev = finding.evidence_dict()
    assert ev["empty_count"] == 18
    assert ev["total_count"] == 20
    assert ev["rate"] == 0.9
    assert ev["threshold"] == 0.7
    assert "remediation" in ev
    assert "Priority A" in finding.summary or "VERIFICATION" in finding.summary


def test_detector_never_raises_on_missing_ledger(
    fresh_registry, tmp_path, monkeypatch,
) -> None:
    """No ledger directory → returns None silently."""
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "nonexistent"),
    )
    monkeypatch.setenv(
        "OUROBOROS_BATTLE_SESSION_ID", "meta-sensor-empty-test",
    )
    epr = next(
        d for d in list_dormancy_detectors()
        if d.detector_kind == "empty_postmortem_rate"
    )
    finding = epr.evaluate()
    assert finding is None  # no crash


# ===========================================================================
# §20-§21 — list_recent_postmortems integration
# ===========================================================================


def test_list_recent_postmortems_returns_records(
    isolated_ledger,
) -> None:
    from backend.core.ouroboros.governance.verification import (
        list_recent_postmortems,
    )
    asyncio.run(_seed_postmortems(empty_count=2, total_count=5))
    pms = list_recent_postmortems(limit=10)
    assert len(pms) == 5
    empty_seen = sum(1 for pm in pms if pm.total_claims == 0)
    assert empty_seen == 2


def test_list_recent_postmortems_master_off_returns_empty(
    fresh_registry, tmp_path, monkeypatch,
) -> None:
    """No ledger → empty tuple."""
    from backend.core.ouroboros.governance.verification import (
        list_recent_postmortems,
    )
    monkeypatch.setenv(
        "JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "nope"),
    )
    monkeypatch.setenv(
        "OUROBOROS_BATTLE_SESSION_ID", "meta-sensor-empty-test",
    )
    pms = list_recent_postmortems(limit=10)
    assert pms == ()


# ===========================================================================
# §22-§26 — MetaSensor (the sensor class itself)
# ===========================================================================


class _FakeRouter:
    def __init__(self):
        self.envelopes: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.envelopes.append(envelope)
        return "enqueued"


async def _run_scan(sensor: MetaSensor) -> List[Any]:
    return await sensor.scan_once()


def test_master_off_start_is_noop(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_META_SENSOR_ENABLED", "false")
    router = _FakeRouter()
    sensor = MetaSensor(repo="jarvis", router=router)
    asyncio.run(sensor.start())
    assert sensor._task is None  # type: ignore[attr-defined]
    sensor.stop()


def test_master_on_start_spawns_task(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_META_SENSOR_ENABLED", "true")
    router = _FakeRouter()
    sensor = MetaSensor(repo="jarvis", router=router, poll_interval_s=3600)

    async def _start_then_stop():
        await sensor.start()
        assert sensor._task is not None  # type: ignore[attr-defined]
        sensor.stop()

    asyncio.run(_start_then_stop())


def test_scan_once_master_off_returns_empty(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_META_SENSOR_ENABLED", "false")
    router = _FakeRouter()
    sensor = MetaSensor(repo="jarvis", router=router)
    findings = asyncio.run(_run_scan(sensor))
    assert findings == []


def test_scan_once_dedup(
    isolated_ledger, fresh_registry, monkeypatch,
) -> None:
    """Same finding fires only once across multiple scans."""
    monkeypatch.setenv("JARVIS_META_SENSOR_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_THRESHOLD", "0.7",
    )
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_MIN_RECORDS", "5",
    )
    asyncio.run(_seed_postmortems(empty_count=18, total_count=20))
    router = _FakeRouter()
    sensor = MetaSensor(repo="jarvis", router=router)
    findings_1 = asyncio.run(_run_scan(sensor))
    findings_2 = asyncio.run(_run_scan(sensor))
    # Both scans return the finding; only first emits envelope
    assert len(findings_1) == 1
    assert len(findings_2) == 1
    assert len(router.envelopes) == 1


def test_scan_once_emits_envelope_with_required_fields(
    isolated_ledger, fresh_registry, monkeypatch,
) -> None:
    monkeypatch.setenv("JARVIS_META_SENSOR_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_THRESHOLD", "0.7",
    )
    monkeypatch.setenv(
        "JARVIS_META_EMPTY_POSTMORTEM_MIN_RECORDS", "5",
    )
    asyncio.run(_seed_postmortems(empty_count=18, total_count=20))
    router = _FakeRouter()
    sensor = MetaSensor(repo="jarvis", router=router)
    asyncio.run(_run_scan(sensor))
    assert len(router.envelopes) == 1
    env = router.envelopes[0]
    # IntentEnvelope fields (per existing sensor pattern)
    assert env.source == "meta_dormancy_alarm"
    # P1 maps to canonical "critical" urgency (raw P1 retained in evidence)
    assert env.urgency == "critical"
    assert env.evidence["dormancy_severity"] == "p1"
    assert env.requires_human_ack is True
    # Evidence shape
    assert "empty_count" in env.evidence
    assert "rate" in env.evidence
    assert "remediation" in env.evidence


# ===========================================================================
# §27 — Authority invariants
# ===========================================================================


def test_no_orchestrator_imports() -> None:
    from backend.core.ouroboros.governance.intake.sensors import meta_sensor
    src = inspect.getsource(meta_sensor)
    forbidden = (
        "orchestrator", "phase_runner", "candidate_generator",
        "iron_gate", "change_engine", "policy",
    )
    for token in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{token}" not in src
        ), f"meta_sensor must not import {token}"
        assert (
            f"import backend.core.ouroboros.governance.{token}" not in src
        ), f"meta_sensor must not import {token}"
