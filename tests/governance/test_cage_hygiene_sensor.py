"""Regression spine for the cage hygiene sensor — aggregate, never flood.

`cage_calibration` refuses to widen a cage from observed demand, so denials
become FINDINGS about the synthesizer's rule. Giving those findings an
emission surface is the open loop this closes — and closing it naively opens
two holes: a denial-of-service against O+V's own intake, and alert fatigue
that buries the one finding that mattered.

Both are attacker-REACHABLE: anything that can make a worker ask for a
forbidden tool can make it ask a thousand times.

The load-bearing test is `test_runaway_50_denials_in_one_second_yields_one_signal`
— the mandate case. Aggregation collapses a burst of the SAME cluster; the
token bucket bounds a drip of DISTINCT ones. Either alone leaves the other
attack open, so both are asserted.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    cage_hygiene_sensor as chs,
)
from backend.core.ouroboros.governance.intake.sensors.cage_hygiene_sensor import (
    BENIGN,
    SOURCE,
    SUSPICIOUS,
    UNDETERMINED,
    CageHygieneSensor,
    DenialCluster,
    classify_cluster,
    note_denials,
)


class _Router:
    """A mock UnifiedIntakeRouter that records every envelope."""

    def __init__(self, result="enqueued"):
        self.seen = []
        self._result = result

    async def ingest(self, envelope):
        self.seen.append(envelope)
        return self._result


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("JARVIS_CAGE_HYGIENE_SENSOR_ENABLED", "1")
    monkeypatch.setenv("JARVIS_CAGE_HYGIENE_MIN_OCCURRENCES", "3")
    chs.reset_for_tests()
    yield
    chs.reset_for_tests()


def _ev(envelope, key, default=None):
    ev = getattr(envelope, "evidence", None)
    if ev is None and isinstance(envelope, dict):
        ev = envelope.get("evidence")
    return (ev or {}).get(key, default)


# ---------------------------------------------------------------------------
# The mandate case
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runaway_50_denials_in_one_second_yields_one_signal():
    """A runaway worker must not be able to flood the intake.

    Fifty identical denials inside one second is one CLUSTER carrying
    occurrences=50 — strictly more informative than fifty signals, and one
    envelope instead of fifty.
    """
    router = _Router()
    sensor = CageHygieneSensor("repo", router)

    start = time.monotonic()
    for _ in range(50):
        note_denials("python-source-mutator:rw", ("bash",))
    assert time.monotonic() - start < 1.0, "the burst itself must be cheap"

    await sensor.scan_once()

    assert len(router.seen) == 1, (
        f"50 denials produced {len(router.seen)} signals — intake floodable")
    assert _ev(router.seen[0], "occurrences") == 50, "the count must be kept"
    assert _ev(router.seen[0], "classification") == SUSPICIOUS


@pytest.mark.asyncio
async def test_repeating_the_burst_stays_quiet():
    """A persistent condition reports once, then stops — alert fatigue."""
    router = _Router()
    sensor = CageHygieneSensor("repo", router)
    for _ in range(10):
        note_denials("r:rw", ("read_file",))
    await sensor.scan_once()
    first = len(router.seen)
    await sensor.scan_once()
    await sensor.scan_once()
    assert len(router.seen) == first


@pytest.mark.asyncio
async def test_a_materially_worse_condition_reports_again():
    """"It got ten times worse" is new information, not a repeat."""
    router = _Router()
    sensor = CageHygieneSensor("repo", router)
    for _ in range(5):
        note_denials("r:rw", ("read_file",))
    await sensor.scan_once()
    assert len(router.seen) == 1
    for _ in range(50):
        note_denials("r:rw", ("read_file",))
    await sensor.scan_once()
    assert len(router.seen) == 2


@pytest.mark.asyncio
async def test_token_bucket_bounds_a_drip_of_distinct_clusters(monkeypatch):
    """Aggregation alone does not stop a slow sweep across many tools."""
    monkeypatch.setenv("JARVIS_CAGE_HYGIENE_BUCKET_CAPACITY", "2")
    monkeypatch.setenv("JARVIS_CAGE_HYGIENE_BUCKET_REFILL_PER_S", "0.0001")
    monkeypatch.setenv("JARVIS_CAGE_HYGIENE_MAX_EMIT", "50")
    chs.reset_for_tests()

    router = _Router()
    sensor = CageHygieneSensor("repo", router)
    for i in range(30):
        for _ in range(3):
            note_denials(f"role{i}:rw", (f"tool_{i}",))
    await sensor.scan_once()

    assert len(router.seen) <= 2, "token bucket did not bound distinct clusters"
    assert sensor.health()["throttled"] >= 1


@pytest.mark.asyncio
async def test_aggregator_memory_is_bounded(monkeypatch):
    """An attacker varying the tool name must not grow the aggregator."""
    monkeypatch.setenv("JARVIS_CAGE_HYGIENE_MAX_CLUSTERS", "16")
    chs.reset_for_tests()
    for i in range(500):
        note_denials("r:rw", (f"tool_{i}",))
    assert len(chs._aggregator.clusters) <= 16
    assert chs._aggregator.dropped > 0


@pytest.mark.asyncio
async def test_single_denial_is_below_the_reporting_floor():
    """One denial is a model guessing once; a pattern is a rule being wrong."""
    router = _Router()
    sensor = CageHygieneSensor("repo", router)
    note_denials("r:rw", ("read_file",))
    await sensor.scan_once()
    assert router.seen == []


# ---------------------------------------------------------------------------
# Classification — deterministic, no model, no attacker-supplied text
# ---------------------------------------------------------------------------


def _cluster(tool="read_file", occurrences=5, breadth=1):
    return DenialCluster(role="r", tool=tool, occurrences=occurrences,
                         first_seen=0.0, last_seen=1.0,
                         role_tool_breadth=breadth)


@pytest.mark.parametrize("tool", ["bash", "write_file", "web_fetch",
                                  "<mutation_budget>"])
def test_boundary_tools_are_suspicious(tool):
    """Absent from TOOL_CLASS_MAP == outside the read-only vocabulary.

    Reuses the taxonomy that already exists rather than a new list to keep
    correct.
    """
    assert classify_cluster(_cluster(tool=tool)) == SUSPICIOUS


def test_a_single_read_only_neighbour_is_benign():
    """A model guessing one plausible neighbour is an under-grant, not an
    attack — the synthesizer's rule is what should change."""
    assert classify_cluster(_cluster(tool="search_code")) == BENIGN


def test_breadth_across_read_only_tools_reads_as_probing(monkeypatch):
    monkeypatch.setenv("JARVIS_CAGE_HYGIENE_SUSPICIOUS_BREADTH", "3")
    assert classify_cluster(_cluster(tool="git_log", breadth=4)) == SUSPICIOUS


def test_classification_is_undetermined_when_the_taxonomy_is_unreachable(
    monkeypatch,
):
    """A classifier without an "I don't know" lies whenever it is unsure."""
    monkeypatch.setattr(chs, "_is_boundary_tool", lambda _n: None)
    assert classify_cluster(_cluster()) == UNDETERMINED


def test_classification_never_raises():
    assert classify_cluster(object()) in (BENIGN, SUSPICIOUS, UNDETERMINED)


# ---------------------------------------------------------------------------
# Routing + cost safety
# ---------------------------------------------------------------------------


def test_source_is_registered_in_all_three_places():
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        _VALID_SOURCES,
    )
    from backend.core.ouroboros.governance.intent.signals import SignalSource
    from backend.core.ouroboros.governance.urgency_router import (
        _BACKGROUND_SOURCES,
    )
    assert SOURCE in _VALID_SOURCES
    assert SignalSource(SOURCE) is SignalSource.CAGE_HYGIENE
    assert SOURCE in _BACKGROUND_SOURCES


@pytest.mark.asyncio
async def test_suspicious_findings_do_not_escalate_the_route():
    """The cost-amplification guard.

    Escalating urgency by severity would let an attacker who can trigger
    denials force IMMEDIATE routing and burn the Claude tier. Severity rides
    in evidence; the route stays cheap.
    """
    router = _Router()
    sensor = CageHygieneSensor("repo", router)
    for _ in range(20):
        note_denials("r:rw", ("bash",))
    await sensor.scan_once()
    env = router.seen[0]
    urgency = getattr(env, "urgency", None) or (
        env.get("urgency") if isinstance(env, dict) else None)
    assert urgency == "low"
    assert _ev(env, "classification") == SUSPICIOUS


@pytest.mark.asyncio
async def test_emission_points_at_the_rule_not_the_cage():
    """The finding must name the ROOT cause — the synthesizer's rule."""
    router = _Router()
    sensor = CageHygieneSensor("repo", router)
    for _ in range(5):
        note_denials("r:rw", ("run_tests",))
    await sensor.scan_once()
    env = router.seen[0]
    desc = getattr(env, "description", "") or ""
    assert "worker_synthesizer" in desc
    assert "NOT widened" in desc


# ---------------------------------------------------------------------------
# Refusals + lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_sensor_neither_aggregates_nor_emits(monkeypatch):
    monkeypatch.setenv("JARVIS_CAGE_HYGIENE_SENSOR_ENABLED", "0")
    chs.reset_for_tests()
    router = _Router()
    for _ in range(20):
        note_denials("r:rw", ("bash",))
    assert chs._aggregator.clusters == {}
    assert await CageHygieneSensor("repo", router).scan_once() == []
    assert router.seen == []


def test_default_is_off(monkeypatch):
    monkeypatch.delenv("JARVIS_CAGE_HYGIENE_SENSOR_ENABLED", raising=False)
    assert chs.sensor_enabled() is False


@pytest.mark.asyncio
async def test_rejected_envelope_is_not_marked_seen():
    router = _Router(result="rejected")
    sensor = CageHygieneSensor("repo", router)
    for _ in range(5):
        note_denials("r:rw", ("bash",))
    await sensor.scan_once()
    assert sensor._seen == {}


@pytest.mark.asyncio
async def test_stale_clusters_expire_out_of_the_window(monkeypatch):
    monkeypatch.setenv("JARVIS_CAGE_HYGIENE_WINDOW_S", "1")
    chs.reset_for_tests()
    for _ in range(5):
        note_denials("r:rw", ("bash",))
    assert chs._aggregator.clusters
    chs._aggregator.expire(time.time() + 10.0)
    assert chs._aggregator.clusters == {}


@pytest.mark.asyncio
async def test_budget_exhaustion_clusters_as_its_own_pseudo_tool():
    """Budget exhaustion denies the MUTATION capability, not a named tool."""
    router = _Router()
    sensor = CageHygieneSensor("repo", router)
    for _ in range(5):
        note_denials("r:rw", (), count_denied=1)
    await sensor.scan_once()
    assert _ev(router.seen[0], "tool") == "<mutation_budget>"


def test_note_denials_never_raises_on_junk():
    note_denials(None, None)  # type: ignore[arg-type]
    note_denials("", ("",))
    assert chs._aggregator.clusters == {}


def test_health_is_bounded_and_never_raises():
    health = CageHygieneSensor("repo", _Router()).health()
    assert set(health) >= {"enabled", "running", "scans", "emitted",
                           "throttled", "clusters", "bucket_tokens"}


@pytest.mark.asyncio
async def test_calibration_seam_feeds_the_aggregator(monkeypatch, tmp_path):
    """No second instrumentation point — the existing seam feeds this."""
    from backend.core.ouroboros.governance.autonomy import cage_calibration as cc
    from backend.core.ouroboros.governance.autonomy.worker_synthesizer import (
        WorkerShape,
    )

    monkeypatch.setenv("JARVIS_CAGE_CALIBRATION_ENABLED", "1")
    cc.reset_for_tests(tmp_path)

    class _Cage:
        max_mutations = 2
        mutations_count = 2
        call_records = (("bash", "c1", "type_denied", time.time()),)

    class _Result:
        status = "completed"

    shape = WorkerShape(role="python-source mutator",
                        allowed_tools=("read_file",), mutation_budget=2,
                        context_budget_tokens=8000, read_only=False)
    for _ in range(4):
        cc.observe_unit(shape, _Cage(), _Result())
    cc.reset_for_tests(None)

    assert any(c.tool == "bash" for c in chs._aggregator.clusters.values())
