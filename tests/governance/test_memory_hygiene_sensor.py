"""Regression spine for proactive memory (MemoryHygieneSensor).

The arc's memory was pull-only: it answered at CONTEXT_EXPANSION and never
initiated. This is the half that makes it a SIGNAL SOURCE — in this codebase
the precise meaning of "proactive", not a background thread recomputing a
score.

Three groups, and the last two are where the risk lives:

* **Detection** — memory can see defects in itself, using evidence the rest
  of the arc produced (drift, the admission ledger, the utility store).
* **Registration** — the source token must be in THREE places; missing any
  one fails silently and differently. This test is the only thing standing
  between "works" and "every envelope is dropped".
* **Bounds** — this corpus measured 54 drifted topics. A sensor that emits
  one op per finding floods the intake on first boot.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake.sensors.memory_hygiene_sensor import (
    SOURCE,
    HygieneFinding,
    MemoryHygieneSensor,
)


class _Router:
    """Captures envelopes instead of enqueueing them."""

    def __init__(self, result="enqueued"):
        self.seen = []
        self._result = result

    async def ingest(self, envelope):
        self.seen.append(envelope)
        return self._result


@pytest.fixture(autouse=True)
def _on(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_MEMORY_HYGIENE_SENSOR_ENABLED", "1")
    monkeypatch.setenv("JARVIS_MEMORY_HYGIENE_COOLDOWN_S", "0")
    from backend.core.ouroboros.governance.memory_admission import (
        reset_default_registry,
    )
    from backend.core.ouroboros.governance import memory_utility as mu
    reset_default_registry()
    # Bind the utility store to a tmp root. `reset_for_tests(None)` restores
    # the REAL `.jarvis/memory_utility.jsonl`, so a suite that writes
    # observations would accumulate across tests AND across runs — the mass
    # would grow until confidence-gated assertions silently inverted.
    mu.reset_for_tests(tmp_path)
    yield
    reset_default_registry()
    mu.reset_for_tests(None)


def _finding(kind="drifted", uri="t.md", h="h1"):
    return HygieneFinding(kind=kind, uri=uri, content_hash=h,
                          summary="s", target_files=(uri,), severity="low",
                          evidence={})


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        coro)


# ---------------------------------------------------------------------------
# Registration — the silent-failure surface
# ---------------------------------------------------------------------------


def test_source_is_registered_in_all_three_places():
    """Missing any one fails silently and differently.

    * absent from ``_VALID_SOURCES``      -> every envelope dropped
    * absent from ``SignalSource``        -> typed consumers cannot classify
    * absent from ``_BACKGROUND_SOURCES`` -> chores route to Claude at 15x

    The whitelist's own comment records the last time a sensor got this
    wrong: $0.53 of Claude budget burned on doc scans.
    """
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        _VALID_SOURCES,
    )
    from backend.core.ouroboros.governance.intent.signals import SignalSource
    from backend.core.ouroboros.governance.urgency_router import (
        _BACKGROUND_SOURCES,
    )
    assert SOURCE in _VALID_SOURCES
    assert SignalSource(SOURCE) is SignalSource.MEMORY_HYGIENE
    assert SOURCE in _BACKGROUND_SOURCES


def test_emitted_envelopes_carry_low_urgency():
    """Urgency is the other half of staying on the BACKGROUND route."""
    router = _Router()
    sensor = MemoryHygieneSensor("repo", router)
    assert _run(sensor._emit(_finding())) is True
    env = router.seen[0]
    urgency = getattr(env, "urgency", None) or (
        env.get("urgency") if isinstance(env, dict) else None)
    assert urgency == "low"


def test_evidence_is_schema_versioned():
    router = _Router()
    sensor = MemoryHygieneSensor("repo", router)
    _run(sensor._emit(_finding()))
    env = router.seen[0]
    ev = getattr(env, "evidence", None) or (
        env.get("evidence") if isinstance(env, dict) else {})
    assert ev.get("schema_version", "").startswith("memory_hygiene.")
    assert ev.get("kind") == "drifted"


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_drifted_and_orphaned_topics_become_findings(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_ROUTING_ENABLED", "1")
    from backend.core.ouroboros.governance import module_routing as mr

    class _Frag:
        def __init__(self, uri, drift, h):
            self.uri, self.drift, self.content_hash = uri, drift, h
            self.title, self.modules = uri, ("a.py",)
            self.summary = "s"

    async def _fake_load(_dir, _root):
        return ([_Frag("d.md", "drifted", "h1"),
                 _Frag("o.md", "orphaned", "h2"),
                 _Frag("f.md", "fresh", "h3")], None)

    monkeypatch.setattr(mr, "_load_topic_fragments", _fake_load)
    sensor = MemoryHygieneSensor("repo", _Router(), project_root=tmp_path)
    kinds = {f.kind for f in _run(sensor.collect_findings())}
    assert kinds == {"drifted", "orphaned"}  # fresh topics are not defects


def test_unreachable_needs_evidence_mass(monkeypatch):
    """A topic is not "unreachable" after one lost pass."""
    monkeypatch.setenv("JARVIS_MEMORY_HYGIENE_MIN_PASSES", "5")
    from backend.core.ouroboros.governance.memory_admission import (
        AdmissionDecision, AdmissionReason, AdmissionRecord, AdmissionRow,
        MemoryConsumer, record_admission,
    )

    class _T:
        uri, title, modules = "u.md", "U", ("a.py",)
        content_hash = "loser"

    def _lose(n):
        for i in range(n):
            record_admission(AdmissionRecord.of(
                op_id=f"op{i}", consumer=MemoryConsumer.MAIN,
                rows=[AdmissionRow(
                    source_id="s", uri="u.md", content_hash="loser",
                    decision=AdmissionDecision.WITHHELD,
                    reason=AdmissionReason.RANK_BELOW_CUTOFF,
                    score=0.1, chars=10)],
                corpus_size=1, corpus_provenance="git_tracked",
                corpus_excluded=0, char_budget=100))

    sensor = MemoryHygieneSensor("repo", _Router())
    _lose(3)
    assert sensor._admission_findings({"loser": _T()}) == []
    _lose(4)
    found = sensor._admission_findings({"loser": _T()})
    assert [f.kind for f in found] == ["unreachable"]


def test_a_topic_that_ever_won_is_never_unreachable(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_HYGIENE_MIN_PASSES", "2")
    from backend.core.ouroboros.governance.memory_admission import (
        AdmissionDecision, AdmissionReason, AdmissionRecord, AdmissionRow,
        MemoryConsumer, record_admission,
    )

    class _T:
        uri, title, modules = "u.md", "U", ()
        content_hash = "h"

    def _row(admitted):
        return AdmissionRow(
            source_id="s", uri="u.md", content_hash="h",
            decision=(AdmissionDecision.ADMITTED if admitted
                      else AdmissionDecision.WITHHELD),
            reason=(AdmissionReason.SEMANTIC if admitted
                    else AdmissionReason.RANK_BELOW_CUTOFF),
            score=0.5, chars=10)

    for i, admitted in enumerate((False, False, False, True)):
        record_admission(AdmissionRecord.of(
            op_id=f"op{i}", consumer=MemoryConsumer.MAIN, rows=[_row(admitted)],
            corpus_size=1, corpus_provenance="git_tracked",
            corpus_excluded=0, char_budget=100))
    assert MemoryHygieneSensor("repo", _Router())._admission_findings(
        {"h": _T()}) == []


def test_suspect_requires_confidence_not_just_a_low_multiplier(monkeypatch):
    """One unlucky op must never accuse a topic of being wrong."""
    from backend.core.ouroboros.governance import memory_utility as mu

    class _T:
        uri, title, modules = "s.md", "S", ()
        content_hash = "bad"

    import time as _t

    store = mu.get_store()
    # Enough failing mass to clear the MULTIPLIER gate but not a confident
    # amount — so the only thing left deciding is the confidence floor.
    # Isolating one gate at a time matters: with a single observation the
    # multiplier alone excludes it, and the test would pass while proving
    # nothing about confidence.
    store.add([mu.Observation("bad", 0.0, _t.time()) for _ in range(2)]
              + [mu.Observation("good", 1.0, _t.time()) for _ in range(6)])
    sensor = MemoryHygieneSensor("repo", _Router())
    assert store.reading("bad").multiplier < 0.85, "multiplier gate not isolated"
    monkeypatch.setenv("JARVIS_MEMORY_HYGIENE_SUSPECT_MIN_CONF", "0.9")
    assert sensor._utility_findings({"bad": _T()}) == []
    monkeypatch.setenv("JARVIS_MEMORY_HYGIENE_SUSPECT_MIN_CONF", "0.0")
    assert [f.kind for f in sensor._utility_findings({"bad": _T()})] == [
        "suspect"]


def test_suspect_summary_admits_correlation_is_not_proof():
    """The organism must not tell itself a correlation is a verdict."""
    from backend.core.ouroboros.governance import memory_utility as mu
    import time as _t

    class _T:
        uri, title, modules = "s.md", "S", ()
        content_hash = "bad"

    store = mu.get_store()
    store.add([mu.Observation("bad", 0.0, _t.time()) for _ in range(20)]
              + [mu.Observation("good", 1.0, _t.time()) for _ in range(20)])
    found = MemoryHygieneSensor("repo", _Router())._utility_findings(
        {"bad": _T()})
    assert found and "correlation is not proof" in found[0].summary


def test_cold_utility_store_produces_nothing():
    class _T:
        uri, title, modules = "s.md", "S", ()
        content_hash = "never"
    assert MemoryHygieneSensor("repo", _Router())._utility_findings(
        {"never": _T()}) == []


# ---------------------------------------------------------------------------
# Bounds — the flood the corpus would otherwise cause
# ---------------------------------------------------------------------------


def test_emission_is_capped_per_scan(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_HYGIENE_MAX_EMIT", "2")
    router = _Router()
    sensor = MemoryHygieneSensor("repo", router)
    sensor.collect_findings = lambda: _async(
        [_finding(uri=f"t{i}.md", h=f"h{i}") for i in range(30)])
    found = _run(sensor.scan_once())
    assert len(found) == 30
    assert len(router.seen) == 2, "cold-start flood not bounded"


async def _async(value):
    return value


def test_severity_ordering_puts_orphaned_and_suspect_first(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_HYGIENE_MAX_EMIT", "2")
    router = _Router()
    sensor = MemoryHygieneSensor("repo", router)
    sensor.collect_findings = lambda: _async([
        _finding("uncovered", "u.md", "h1"),
        _finding("drifted", "d.md", "h2"),
        _finding("orphaned", "o.md", "h3"),
        _finding("suspect", "s.md", "h4"),
    ])
    _run(sensor.scan_once())
    kinds = [e.evidence["kind"] if hasattr(e, "evidence") else
             e["evidence"]["kind"] for e in router.seen]
    assert kinds == ["orphaned", "suspect"]


def test_dedup_is_keyed_on_payload_so_a_repair_clears_it():
    """A path-keyed dedup would suppress the finding forever after one
    failed repair; a payload key re-examines the new text on its merits."""
    router = _Router()
    sensor = MemoryHygieneSensor("repo", router)

    sensor.collect_findings = lambda: _async([_finding(h="v1")])
    _run(sensor.scan_once())
    assert len(router.seen) == 1

    _run(sensor.scan_once())
    assert len(router.seen) == 1, "same payload re-emitted"

    # Topic repaired -> new payload -> new hash -> eligible again if still bad.
    sensor.collect_findings = lambda: _async([_finding(h="v2")])
    _run(sensor.scan_once())
    assert len(router.seen) == 2


def test_a_rejected_envelope_is_not_marked_seen():
    """If the router refused it, the finding must remain emittable."""
    router = _Router(result="rejected")
    sensor = MemoryHygieneSensor("repo", router)
    sensor.collect_findings = lambda: _async([_finding()])
    _run(sensor.scan_once())
    assert sensor._seen == {}


def test_cooldown_suppresses_a_burst(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_HYGIENE_COOLDOWN_S", "3600")
    router = _Router()
    sensor = MemoryHygieneSensor("repo", router)
    sensor.collect_findings = lambda: _async([_finding()])
    _run(sensor.scan_once())
    before = len(router.seen)
    _run(sensor.scan_once())
    assert len(router.seen) == before


def test_disabled_sensor_emits_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_MEMORY_HYGIENE_SENSOR_ENABLED", "0")
    router = _Router()
    sensor = MemoryHygieneSensor("repo", router)
    sensor.collect_findings = lambda: _async([_finding()])
    assert _run(sensor.scan_once()) == []
    assert router.seen == []


def test_default_is_off():
    """A sensor that enqueues autonomous work earns default-on via a soak."""
    import os
    from backend.core.ouroboros.governance.intake.sensors import (
        memory_hygiene_sensor as mhs,
    )
    os.environ.pop("JARVIS_MEMORY_HYGIENE_SENSOR_ENABLED", None)
    assert mhs.sensor_enabled() is False


# ---------------------------------------------------------------------------
# Event-primary wiring
# ---------------------------------------------------------------------------


def test_registry_listener_fires_on_every_routing_pass():
    """The seam that makes this event-primary rather than polled.

    Per-op ledgers are created lazily, so a subscriber wanting "every pass"
    had nothing to attach to until the registry grew its own listener.
    """
    from backend.core.ouroboros.governance.memory_admission import (
        AdmissionRecord, MemoryConsumer, get_default_registry,
        record_admission,
    )
    seen = []
    get_default_registry().add_listener(seen.append)
    record_admission(AdmissionRecord.of(
        op_id="op-x", consumer=MemoryConsumer.MAIN, rows=[],
        corpus_size=1, corpus_provenance="git_tracked",
        corpus_excluded=0, char_budget=10))
    assert len(seen) == 1 and seen[0]["op_id"] == "op-x"


def test_registry_listener_is_idempotent_and_isolated():
    from backend.core.ouroboros.governance.memory_admission import (
        AdmissionRecord, MemoryConsumer, get_default_registry,
        record_admission,
    )
    seen = []

    def boom(_):
        raise RuntimeError("listener exploded")

    registry = get_default_registry()
    registry.add_listener(boom)
    registry.add_listener(seen.append)
    registry.add_listener(seen.append)  # idempotent by identity
    record_admission(AdmissionRecord.of(
        op_id="op-y", consumer=MemoryConsumer.MAIN, rows=[],
        corpus_size=1, corpus_provenance="git_tracked",
        corpus_excluded=0, char_budget=10))
    assert len(seen) == 1, "a raising listener starved the next one"


def test_fs_events_outside_memory_topics_are_ignored():
    sensor = MemoryHygieneSensor("repo", _Router())
    _run(sensor._on_fs_event({"path": "backend/core/orchestrator.py"}))
    assert sensor._debounce_task is None


def test_health_projection_is_bounded_and_never_raises():
    health = MemoryHygieneSensor("repo", _Router()).health()
    assert set(health) >= {"enabled", "running", "scans", "emitted",
                           "suppressed", "max_emit_per_scan"}
    assert health["schema_version"].startswith("memory_hygiene.")
