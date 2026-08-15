"""A floor that moved becomes work — and nothing else does.

Every test here EXERCISES the path. None asserts that a source file mentions
a flag, because the instrument this sensor adapts (`/evidence`) exists
precisely to find tests of that shape: three of them once certified that
`/narrate verbose` worked by confirming the handler's source named a flag no
code read. A test suite for the source-only detector that is itself
source-only would be the joke writing itself.

The load-bearing cases:

* ``test_steady_state_is_silent`` — the reason Phase 1 pinned the floors
  first. With the floors accepted this sensor emits nothing at all, which is
  what makes it safe to arm.
* ``test_a_wholesale_move_collapses_to_one_op`` and its proportional twin —
  one commit's damage is one op, and "wholesale" is measured against the
  population rather than against a number somebody picked.
* ``test_the_baseline_cannot_be_edited_by_the_work_this_sensor_creates`` —
  the sharpest failure mode in the whole design. The cheapest way to close a
  reachability finding is to re-accept the floor, and an autonomous loop
  offered that shortcut would take it.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import audit_ratchet as ar
from backend.core.ouroboros.governance.intake.sensors import (
    audit_drift_sensor as ads,
)
from backend.core.ouroboros.governance.intake.sensors.audit_drift_sensor import (
    SOURCE,
    AuditDriftSensor,
    cluster_drift,
    locate_finding,
)

# Real files in this checkout, in two different directories. Using real paths
# is the point: the locator resolves against the filesystem, and a fixture of
# invented paths would test the clustering while silently skipping the half
# that has to be true for an op to be actionable.
_REACH_A = "backend.core.ouroboros.battle_test.adaptive_window"
_REACH_B = "backend.core.ouroboros.battle_test.ambient_deck"
_GOV_A = "backend.core.ouroboros.governance.audit_ratchet"
_TEST_A = "tests/governance/test_audit_ratchet.py::test_a"
_TEST_B = "tests/governance/test_audit_ratchet.py::test_b"


class _Router:
    def __init__(self, result="enqueued"):
        self.seen = []
        self._result = result

    async def ingest(self, envelope):
        self.seen.append(envelope)
        return self._result


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIT_DRIFT_SENSOR_ENABLED", "1")
    for knob in ("JARVIS_AUDIT_DRIFT_MAX_EMIT", "JARVIS_AUDIT_DRIFT_STORM_MIN",
                 "JARVIS_AUDIT_DRIFT_STORM_FRACTION",
                 "JARVIS_AUDIT_DRIFT_BUCKET_CAPACITY",
                 "JARVIS_AUDIT_DRIFT_REGROWTH_FACTOR",
                 "JARVIS_AUDIT_DRIFT_SETTLE_S"):
        monkeypatch.delenv(knob, raising=False)
    ads.reset_for_tests()
    ar.reset_sweep_cache()
    yield
    ads.reset_for_tests()
    ar.reset_sweep_cache()


def _drift(new, *, scanned=163, baseline_exists=True, error=""):
    return ar.Drift(new={k: tuple(v) for k, v in new.items()},
                    baseline_exists=baseline_exists, scanned=scanned,
                    error=error)


def _sensor(monkeypatch, drifts, *, router=None, fresh=True):
    """A sensor over a fixed reading. The SWEEP is stubbed, never the sensor:
    everything from the reading onward is the real code path."""
    router = router or _Router()
    sweep = ar.Sweep(drifts=drifts, completed_at=1.0, fresh=fresh)

    async def _fake_sweep(*, force=False, max_age_s=None):
        return sweep

    monkeypatch.setattr(ar, "sweep", _fake_sweep)
    return AuditDriftSensor("jarvis", router), router


def _ev(envelope, key, default=None):
    return (getattr(envelope, "evidence", None) or {}).get(key, default)


# ---------------------------------------------------------------------------
# Silence — the property Phase 1 bought
# ---------------------------------------------------------------------------


async def test_steady_state_is_silent(monkeypatch):
    """With the floors accepted, an unchanged repository says nothing.

    This is why the floors were pinned before the adapter was built: the same
    reading on an unpinned checkout carries 1,042 standing findings.
    """
    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"asymmetric": (), "orphans": ()}),
        "evidence": _drift({"source_only": (), "flag_literal": ()},
                           scanned=59834),
    })
    assert await sensor.scan_once() == []
    assert router.seen == []


async def test_an_absent_baseline_is_not_a_regression(monkeypatch):
    """Not yet measured is not worse. A fresh checkout must not enqueue its
    whole standing list on first boot."""
    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"asymmetric": (_REACH_A, _REACH_B)},
                        baseline_exists=False),
    })
    assert await sensor.scan_once() == []
    assert router.seen == []


async def test_an_audit_that_could_not_run_says_nothing(monkeypatch):
    """A reading that failed is not a reading. Treating an error as an empty
    baseline would report every standing finding as newly regressed."""
    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"asymmetric": (_REACH_A,)}, error="OSError: boom"),
    })
    assert await sensor.scan_once() == []
    assert router.seen == []


# ---------------------------------------------------------------------------
# Clustering — one commit's damage is one op
# ---------------------------------------------------------------------------


def test_findings_in_one_directory_become_one_cluster():
    clusters = cluster_drift("reach", _drift({"asymmetric": (_REACH_A, _REACH_B)}))
    assert len(clusters) == 1
    assert clusters[0].locus == "backend/core/ouroboros/battle_test"
    assert clusters[0].size == 2
    assert set(clusters[0].paths) == {
        "backend/core/ouroboros/battle_test/adaptive_window.py",
        "backend/core/ouroboros/battle_test/ambient_deck.py",
    }


def test_findings_in_different_directories_stay_separate():
    """Clustering must not merge unrelated work — the locus is a fact about
    the repository, and two packages are two ops."""
    clusters = cluster_drift("reach", _drift({"asymmetric": (_REACH_A, _GOV_A)}))
    assert {c.locus for c in clusters} == {
        "backend/core/ouroboros/battle_test",
        "backend/core/ouroboros/governance",
    }


def test_a_wholesale_move_collapses_to_one_op():
    """Deleting a surface makes every module it reached asymmetric at once.
    Forty repairs is the wrong answer; one human decision is the right one."""
    keys = tuple(f"{_REACH_A}" for _ in range(0))  # explicit: start empty
    keys = tuple(sorted({_REACH_A, _REACH_B, _GOV_A}))
    many = keys + tuple(
        f"backend.core.ouroboros.battle_test.{n}" for n in
        ("canvas_mouse", "canvas_selection", "canvas_viewport", "clipboard_image",
         "clipboard_write", "cockpit_fsm", "cockpit_mount", "confirm_chord",
         "draft_stash", "focus_shield"))
    clusters = cluster_drift("reach", _drift({"asymmetric": many}, scanned=163))
    assert len(clusters) == 1
    assert clusters[0].storm is True
    assert clusters[0].locus == "*"
    # The count is the whole drift; the members are a sample of it.
    assert clusters[0].size == len(many)


def test_the_storm_threshold_is_proportional_to_what_was_scanned():
    """13 findings against 163 modules is a catastrophe. The same 13 against
    59,834 tests is a Tuesday. A fixed number cannot say both."""
    many = tuple(sorted({_REACH_A, _REACH_B, _GOV_A})) + tuple(
        f"backend.core.ouroboros.battle_test.{n}" for n in
        ("canvas_mouse", "canvas_selection", "canvas_viewport", "clipboard_image",
         "clipboard_write", "cockpit_fsm", "cockpit_mount", "confirm_chord",
         "draft_stash", "focus_shield"))
    small_population = cluster_drift("reach", _drift({"a": many}, scanned=163))
    large_population = cluster_drift("evidence", _drift({"a": many}, scanned=59834))
    assert small_population[0].storm is True
    assert all(c.storm is False for c in large_population)


def test_a_subset_bucket_claims_its_finding_first():
    """`flag_literal` is a strict subset of `source_only`. Reporting both
    would tell the operator twice, once under the label that says why it
    matters and once under the one that doesn't."""
    clusters = cluster_drift("evidence", _drift(
        {"source_only": (_TEST_A, _TEST_B), "flag_literal": (_TEST_A,)},
        scanned=59834))
    by_bucket = {c.bucket: c for c in clusters}
    assert set(by_bucket) == {"flag_literal", "source_only"}
    assert by_bucket["flag_literal"].members == (_TEST_A,)
    assert by_bucket["source_only"].members == (_TEST_B,)


# ---------------------------------------------------------------------------
# Locating — an op with no file is not an op
# ---------------------------------------------------------------------------


def test_a_dotted_module_resolves_to_its_file():
    assert locate_finding(_GOV_A) == (
        "backend/core/ouroboros/governance/audit_ratchet.py")


def test_a_package_resolves_to_its_init():
    assert locate_finding("backend.core.ouroboros.governance.intake.sensors") == (
        "backend/core/ouroboros/governance/intake/sensors/__init__.py")


def test_a_node_id_resolves_to_its_test_file():
    assert locate_finding(_TEST_A) == "tests/governance/test_audit_ratchet.py"


def test_a_key_naming_nothing_resolves_to_nothing():
    """Guessing would be worse than refusing: an op pointed at a file that
    does not exist burns a generation to discover it."""
    assert locate_finding("no.such.module.anywhere") == ""
    assert locate_finding("") == ""
    assert locate_finding("tests/does_not_exist.py::test_x") == ""


async def test_unlocatable_findings_never_become_ops(monkeypatch):
    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"asymmetric": ("no.such.module", "also.not.real")}),
    })
    assert await sensor.scan_once() == []
    assert router.seen == []


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


async def test_the_envelope_is_a_real_validated_intent_envelope(monkeypatch):
    """Built through `make_envelope`, so the 15-field contract is exercised
    rather than described. A builder called with the wrong fields raises on
    its first statement and the sensor would be dead on arrival."""
    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"asymmetric": (_REACH_A, _REACH_B)}),
    })
    await sensor.scan_once()
    assert len(router.seen) == 1
    env = router.seen[0]
    assert env.source == SOURCE
    assert env.target_files
    assert all(p.endswith(".py") for p in env.target_files)
    assert env.dedup_key and env.idempotency_key and env.signal_id
    assert 0.0 <= env.confidence <= 1.0


async def test_findings_never_escalate_the_route(monkeypatch):
    """Repo-wide static analysis on the Claude tier is how a diagnostic
    becomes a budget incident. Even the storm case stays cheap."""
    from backend.core.ouroboros.governance.intent.signals import SignalSource
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        _VALID_SOURCES,
    )
    from backend.core.ouroboros.governance.urgency_router import (
        _BACKGROUND_SOURCES,
    )
    assert SOURCE in _VALID_SOURCES
    assert SignalSource(SOURCE) is SignalSource.AUDIT_REGRESSION
    assert SOURCE in _BACKGROUND_SOURCES

    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"asymmetric": tuple(
            f"backend.core.ouroboros.battle_test.{n}" for n in
            ("canvas_mouse", "canvas_selection", "canvas_viewport",
             "clipboard_image", "clipboard_write", "cockpit_fsm",
             "cockpit_mount", "confirm_chord", "draft_stash", "focus_shield",
             "adaptive_window", "ambient_deck", "epistemic_filter"))},
            scanned=163)},
    )
    await sensor.scan_once()
    assert router.seen[0].urgency == "low"
    assert _ev(router.seen[0], "storm") is True


async def test_the_op_is_told_that_moving_the_floor_is_not_the_fix(monkeypatch):
    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"orphans": (_REACH_A,)}),
    })
    await sensor.scan_once()
    description = router.seen[0].description
    assert ".jarvis/" in description
    assert "accept" in description
    assert _ev(router.seen[0], "baseline_is_not_the_fix") is True


async def test_the_evidence_carries_the_dedup_axis_the_router_hashes(monkeypatch):
    """`intent_envelope._dedup_key` hashes `evidence["signature"]`. Without
    it the router's own dedup window would key on target files alone and this
    sensor would need a second, disagreeing opinion about identity."""
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        _dedup_key,
    )
    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"orphans": (_REACH_A,)}),
    })
    await sensor.scan_once()
    env = router.seen[0]
    assert _ev(env, "signature") == "reach|orphans|backend/core/ouroboros/battle_test"
    assert env.dedup_key == _dedup_key(env.source, env.target_files, env.evidence)


# ---------------------------------------------------------------------------
# Bounds — the flood this door could otherwise become
# ---------------------------------------------------------------------------


async def test_a_repeat_is_silent_until_it_gets_materially_worse(monkeypatch):
    """A persistent condition reports once. The finding is still in the
    floor's delta, so nothing is forgotten by staying quiet."""
    router = _Router()
    sensor, _ = _sensor(monkeypatch, {
        "reach": _drift({"orphans": (_REACH_A,)}),
    }, router=router)
    await sensor.scan_once()
    await sensor.scan_once()
    assert len(router.seen) == 1
    assert sensor.health()["suppressed"] == 1

    worse, _ = _sensor(monkeypatch, {
        "reach": _drift({"orphans": (_REACH_A, _REACH_B)}),
    }, router=router)
    await worse.scan_once()
    assert len(router.seen) == 2, "twice as bad is new information"


async def test_the_per_scan_cap_bounds_a_burst(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIT_DRIFT_MAX_EMIT", "1")
    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"asymmetric": (_REACH_A, _GOV_A)}),
    })
    found = await sensor.scan_once()
    assert len(found) == 2, "both clusters are FOUND"
    assert len(router.seen) == 1, "only one is said"


async def test_the_token_bucket_bounds_a_drip_of_distinct_clusters(monkeypatch):
    """The per-scan cap bounds a burst; the bucket bounds a sustained drip of
    DISTINCT findings. Either alone leaves the other open."""
    monkeypatch.setenv("JARVIS_AUDIT_DRIFT_BUCKET_CAPACITY", "1")
    monkeypatch.setenv("JARVIS_AUDIT_DRIFT_BUCKET_REFILL_PER_S", "0.0000001")
    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"asymmetric": (_REACH_A, _GOV_A)}),
    })
    await sensor.scan_once()
    assert len(router.seen) == 1
    assert sensor.health()["throttled"] == 1


async def test_a_rejected_envelope_is_not_remembered_as_said(monkeypatch):
    """Marking an unsent finding as seen would silence it forever."""
    router = _Router(result="backpressure")
    sensor, _ = _sensor(monkeypatch, {
        "reach": _drift({"orphans": (_REACH_A,)}),
    }, router=router)
    await sensor.scan_once()
    assert sensor.health()["known_clusters"] == 0


# ---------------------------------------------------------------------------
# Refusals + lifecycle
# ---------------------------------------------------------------------------


def test_default_is_off(monkeypatch):
    monkeypatch.delenv("JARVIS_AUDIT_DRIFT_SENSOR_ENABLED", raising=False)
    assert ads.sensor_enabled() is False


async def test_a_disabled_sensor_neither_reads_nor_emits(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIT_DRIFT_SENSOR_ENABLED", "0")
    swept = {"n": 0}

    async def _counting_sweep(*, force=False, max_age_s=None):
        swept["n"] += 1
        return ar.Sweep()

    monkeypatch.setattr(ar, "sweep", _counting_sweep)
    router = _Router()
    sensor = AuditDriftSensor("jarvis", router)
    assert await sensor.scan_once() == []
    await sensor.start()
    assert swept["n"] == 0, "a disabled sensor must not even pay for a reading"
    assert router.seen == []


async def test_stop_is_safe_before_start_and_twice():
    sensor = AuditDriftSensor("jarvis", _Router())
    sensor.stop()
    sensor.stop()
    assert sensor.health()["running"] is False


async def test_a_source_change_arms_the_settle_timer_rather_than_scanning(
        monkeypatch):
    """A 41-second audit per keystroke is how event-primary becomes a
    liability. The timer coalesces an editing session into ONE measurement."""
    monkeypatch.setenv("JARVIS_AUDIT_DRIFT_SETTLE_S", "5")
    sensor, router = _sensor(monkeypatch, {
        "reach": _drift({"orphans": (_REACH_A,)}),
    })

    class _Event:
        topic = "fs.changed.modified"
        payload = {"extension": ".py", "path": "backend/x.py"}

    await sensor._on_fs_event(_Event())
    assert sensor.health()["fs_events_handled"] == 1
    assert router.seen == [], "arming is not scanning"
    assert sensor._settle_task is not None
    sensor.stop()


async def test_a_non_python_change_is_ignored(monkeypatch):
    sensor, _ = _sensor(monkeypatch, {"reach": _drift({})})

    class _Event:
        topic = "fs.changed.modified"
        payload = {"extension": ".md", "path": "README.md"}

    await sensor._on_fs_event(_Event())
    assert sensor.health()["fs_events_ignored"] == 1
    assert sensor._settle_task is None


async def test_a_failed_bus_subscription_leaves_the_poll_intact(monkeypatch):
    class _Bus:
        async def subscribe(self, *_a, **_k):
            raise RuntimeError("no bus")

    sensor, _ = _sensor(monkeypatch, {"reach": _drift({})})
    await sensor.subscribe_to_bus(_Bus())  # must not raise
    assert sensor.health()["poll_interval_s"] > 0


# ---------------------------------------------------------------------------
# The instrument must outlive the work it creates
# ---------------------------------------------------------------------------


def test_the_baseline_cannot_be_edited_by_the_work_this_sensor_creates():
    """The sharpest failure mode: the cheapest way to close a reachability
    finding is to re-accept the floor, and the only test of a measurement is
    the measurement — so weakening it would PASS."""
    from backend.core.ouroboros.governance.tool_executor import (
        _is_protected_path,
    )
    for path in (".jarvis/surface_reachability_baseline.json",
                 ".jarvis/source_assertion_baseline.json",
                 "backend/core/ouroboros/governance/audit_ratchet.py",
                 "backend/core/ouroboros/battle_test/surface_reachability.py",
                 "backend/core/ouroboros/battle_test/source_assertion_audit.py",
                 "backend/core/ouroboros/governance/intake/sensors/"
                 "audit_drift_sensor.py"):
        assert _is_protected_path(path), path


async def test_the_sensor_never_runs_an_audit_of_its_own(monkeypatch):
    """Two consumers scanning independently could DISAGREE about the same
    repository, and the operator would have no way to tell which was stale."""
    runs = {"n": 0}

    async def _run():
        runs["n"] += 1
        return object()

    instrument = ar.AuditRatchet(ar.Instrument(
        name="probe", run=_run, findings=lambda _r: {}))
    monkeypatch.setattr(ar, "registered_ratchets", lambda: [instrument])
    ar.reset_sweep_cache()

    router = _Router()
    sensors = [AuditDriftSensor("jarvis", router) for _ in range(3)]
    await asyncio.gather(*(s.scan_once() for s in sensors))
    assert runs["n"] == 1, "three consumers, one scan"


async def test_production_actually_builds_this_sensor(tmp_path, monkeypatch):
    """The wiring proof, RUN rather than grepped.

    `test_sensor_construction_liveness` asserts a production file contains
    the text ``AuditDriftSensor(`` — which is a test of spelling, and would
    pass just as happily if the block containing it never executed. This
    calls the real `_build_components` and looks at the list the service
    will actually drive.
    """
    from backend.core.ouroboros.governance.intake.intake_layer_service import (
        IntakeLayerConfig,
        IntakeLayerService,
    )
    # Subsystems built along the way resolve their ledgers from `.jarvis/`
    # directly rather than from the config, and a test that appends to the
    # operator's real state has changed the system it is measuring. Both
    # expose the override their own docstrings name for exactly this.
    monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("JARVIS_MERKLE_STATE_DIR", str(tmp_path / ".jarvis"))
    monkeypatch.setenv("JARVIS_INVARIANT_DRIFT_BASE_DIR", str(tmp_path / ".jarvis"))
    service = IntakeLayerService(
        gls=None, config=IntakeLayerConfig(project_root=tmp_path), say_fn=None)
    try:
        await asyncio.wait_for(service._build_components(), timeout=90)
        built = {type(s).__name__ for s in service._sensors}
        assert "AuditDriftSensor" in built
    finally:
        await service._teardown()


async def test_a_cached_reading_never_claims_to_be_fresh(monkeypatch):
    """A consumer that thinks it forced a scan and did not would report the
    state of a repository it never measured."""
    async def _run():
        return object()

    monkeypatch.setattr(ar, "registered_ratchets", lambda: [
        ar.AuditRatchet(ar.Instrument(name="probe", run=_run,
                                      findings=lambda _r: {}))])
    ar.reset_sweep_cache()
    first = await ar.sweep()
    second = await ar.sweep()
    assert first.fresh is True
    assert second.fresh is False
    assert ar.last_sweep() is not None
