"""Severance becomes a signal, not a script somebody has to remember to run.

`capability_liveness.snapshot()` reported 190 severance candidates today —
107 with telemetry that has never fired, including `repair_engine`, which
CLAUDE.md documents as enabled by default and closing the Ouroboros cycle.
That answer existed all day and nobody had it, because getting it required
invoking a script. A self-perception layer that must be asked is not
self-perception.

The load-bearing test is `test_a_component_with_zero_reachability_emits_high_severity`
— the mandate case. The second is `test_severity_requires_BOTH_criticality_and_silence`:
either signal alone over-reports, and an alarm that cries wolf on
dynamically-dispatched helpers is one nobody reads by the third week.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.intake.sensors import (
    liveness_sensor as ls,
)
from backend.core.ouroboros.governance.intake.sensors.liveness_sensor import (
    SOURCE,
    LivenessFinding,
    LivenessSensor,
    critical_categories,
    severity_for,
)


class _Router:
    def __init__(self, result="enqueued"):
        self.seen = []
        self._result = result

    async def ingest(self, envelope):
        self.seen.append(envelope)
        return self._result


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVENESS_SENSOR_ENABLED", "1")
    monkeypatch.delenv("JARVIS_LIVENESS_CRITICAL_CATEGORIES", raising=False)
    yield


def _ev(envelope, key, default=None):
    ev = getattr(envelope, "evidence", None)
    if ev is None and isinstance(envelope, dict):
        ev = envelope.get("evidence")
    return (ev or {}).get(key, default)


def _candidate(**kw):
    row = {
        "source_file": "governance/repair_engine.py",
        "category": "safety",
        "flag": "JARVIS_L2_ENABLED",
        "firing": "SILENT",
        # LEDGER-BACKED, because the real row now is. `repair_engine` declares
        # `__firing_ledgers__ = ("repair_tree",)` — the durable evidence L2
        # writes two hops away, through `repair_tree._archive_result` — so its
        # silence is PROVABLE rather than merely unobserved. A fixture still
        # carrying log-only silence would model a row that no longer exists
        # and would quietly stop exercising the escalation path.
        "ledger_backed": True,
        "fraction_severed": 1.0,
        "severed_symbols": ["repair_once", "run_repair_loop"],
    }
    row.update(kw)
    return row


def _stub_snapshot(sensor, rows):
    async def _collect():
        findings = []
        for r in rows:
            findings.append(LivenessFinding(
                source_file=r["source_file"].split("/")[-1],
                category=r["category"], flag=r["flag"], firing=r["firing"],
                fraction_severed=r["fraction_severed"],
                severed_symbols=tuple(r["severed_symbols"]),
                # Mirrors the production `scan()` seam, which forwards
                # `ledger_backed` from the verdict. A stub that dropped it
                # would score every fixture `low` and silently stop testing
                # severity at all.
                severity=severity_for(r["category"], r["firing"],
                                      r["fraction_severed"],
                                      ledger_backed=r.get("ledger_backed")),
            ))
        findings.sort(key=lambda f: f.rank)
        return findings
    sensor.collect_findings = _collect
    return sensor


# ---------------------------------------------------------------------------
# The mandate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_component_with_zero_reachability_emits_high_severity():
    """THE mandate case: a safety component drops to 0 inbound edges.

    Modelled on the real finding — `repair_engine`, category `safety`,
    telemetry SILENT, fully severed.
    """
    router = _Router()
    sensor = _stub_snapshot(LivenessSensor("repo", router), [_candidate()])
    await sensor.scan_once()

    assert len(router.seen) == 1, "a fully severed safety component did not emit"
    env = router.seen[0]
    assert _ev(env, "severity") == "high"
    assert _ev(env, "firing") == "SILENT"
    assert _ev(env, "fraction_severed") == 1.0
    assert "repair_once" in _ev(env, "severed_symbols")
    desc = getattr(env, "description", "") or ""
    assert "repair_engine.py" in desc


@pytest.mark.asyncio
async def test_severity_requires_criticality_and_PROVEN_silence():
    """Either signal alone over-reports, and SILENT is two different claims.

    A safety capability whose telemetry is FIRING is alive whatever the AST
    says — dynamic dispatch leaves no static edge. A SILENT experimental
    helper is not worth waking anyone for.

    SUPERSEDED 2026-07-31: was ``severity_for("safety", "SILENT", 1.0) ==
    "high"`` with no third state.

    `capability_firing.firing_verdict` distinguishes them and says so plainly:
    a ``ledger`` channel is reliable evidence-of-work, a ``log``-only channel
    "is ambiguous (an absent log tag may mean 'ran silently' — an
    observability gap — not proven dormancy)". Measured against the live
    snapshot, 11 of the 12 rows this function scored HIGH did so on log-only
    silence. An auditor wrong 11 times in 12 is ignored on the twelfth.
    """
    assert severity_for("safety", "SILENT", 1.0, ledger_backed=True) == "high"
    assert severity_for("safety", "FIRING", 1.0, ledger_backed=True) == "low"
    assert severity_for("experimental", "SILENT", 1.0, ledger_backed=True) == "low"
    assert severity_for("safety", "UNKNOWN", 1.0, ledger_backed=True) == "low"


@pytest.mark.asyncio
async def test_unprovable_silence_does_not_raise_an_alarm():
    """A log-only SILENT is an observability gap, not a severance.

    Still EMITTED — at ``low``. Nothing is hidden; it stops being an alarm.
    """
    assert severity_for("safety", "SILENT", 1.0, ledger_backed=False) == "low"


@pytest.mark.asyncio
async def test_absent_provenance_is_not_treated_as_proof():
    """``None`` — the row carried no ``ledger_backed`` — is not evidence.

    Same discipline `firing_verdict` applies when it refuses to turn "no
    derivable markers" into SILENT: absence of evidence is not evidence of
    absence, in either direction.
    """
    assert severity_for("safety", "SILENT", 1.0) == "low"
    assert severity_for("safety", "SILENT", 1.0, ledger_backed=None) == "low"


@pytest.mark.asyncio
async def test_registered_never_invoked_still_escalates():
    """Positive evidence OF severance, and unaffected by the ledger question.

    The module declared itself reachable by dispatch and still never ran —
    which rules out the innocent explanation a SILENT log channel leaves open.
    """
    assert severity_for("safety", "REGISTERED_NEVER_INVOKED", 1.0) == "high"


def test_criticality_is_derived_from_the_existing_taxonomy():
    """Not a hardcoded module list.

    A hardcoded {"repair_engine", "aegis", "cage"} is wrong within a month:
    a new safety capability is not in it and nothing says so. Every verdict
    already carries a FlagRegistry `category`, and 129 of today's 190
    candidates are `safety`.
    """
    assert "safety" in critical_categories()


def test_critical_categories_are_widenable_without_code(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVENESS_CRITICAL_CATEGORIES", "safety,routing")
    assert critical_categories() == frozenset({"safety", "routing"})
    assert severity_for("routing", "SILENT", 0.9, ledger_backed=True) == "high"


# ---------------------------------------------------------------------------
# Bounds — 190 findings must not become 190 ops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emission_is_capped_per_scan(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVENESS_MAX_EMIT", "2")
    monkeypatch.setenv("JARVIS_LIVENESS_BUCKET_CAPACITY", "50")
    router = _Router()
    rows = [_candidate(source_file=f"m{i}.py", severed_symbols=[f"s{i}"])
            for i in range(40)]
    sensor = _stub_snapshot(LivenessSensor("repo", router), rows)
    found = await sensor.scan_once()
    assert len(found) == 40
    assert len(router.seen) == 2, "the 190-finding flood is not bounded"


@pytest.mark.asyncio
async def test_high_severity_is_emitted_before_low(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVENESS_MAX_EMIT", "1")
    monkeypatch.setenv("JARVIS_LIVENESS_BUCKET_CAPACITY", "50")
    router = _Router()
    rows = [
        _candidate(source_file="cosmetic.py", category="experimental",
                   severed_symbols=["x"]),
        _candidate(source_file="repair_engine.py", severed_symbols=["y"]),
    ]
    sensor = _stub_snapshot(LivenessSensor("repo", router), rows)
    await sensor.scan_once()
    assert "repair_engine.py" in (getattr(router.seen[0], "description", "") or "")


@pytest.mark.asyncio
async def test_a_persistent_condition_reports_once():
    """190 findings do not resolve between scans; without dedup this would
    re-emit the same backlog every cycle."""
    router = _Router()
    sensor = _stub_snapshot(LivenessSensor("repo", router), [_candidate()])
    await sensor.scan_once()
    await sensor.scan_once()
    await sensor.scan_once()
    assert len(router.seen) == 1


@pytest.mark.asyncio
async def test_a_changed_severed_set_is_a_new_finding():
    """Dedup keys on the SYMBOLS, not the path — a file that merely got
    edited is not news, a different dead set is."""
    router = _Router()
    sensor = _stub_snapshot(LivenessSensor("repo", router), [_candidate()])
    await sensor.scan_once()
    _stub_snapshot(sensor, [_candidate(severed_symbols=["repair_once", "NEW"])])
    await sensor.scan_once()
    assert len(router.seen) == 2


@pytest.mark.asyncio
async def test_below_the_floor_is_not_reported(monkeypatch):
    """One unreferenced helper is normal; most of a surface is not."""
    monkeypatch.setenv("JARVIS_LIVENESS_SEVERED_FLOOR", "0.5")
    from backend.core.ouroboros.governance.intake.sensors.liveness_sensor import (
        _severed_floor,
    )
    assert _severed_floor() == 0.5
    assert severity_for("safety", "SILENT", 0.2, ledger_backed=True) == "low"


@pytest.mark.asyncio
async def test_token_bucket_bounds_a_sustained_drip(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVENESS_MAX_EMIT", "25")
    monkeypatch.setenv("JARVIS_LIVENESS_BUCKET_CAPACITY", "2")
    monkeypatch.setenv("JARVIS_LIVENESS_BUCKET_REFILL_PER_S", "0.0000012")
    router = _Router()
    rows = [_candidate(source_file=f"m{i}.py", severed_symbols=[f"s{i}"])
            for i in range(20)]
    sensor = _stub_snapshot(LivenessSensor("repo", router), rows)
    await sensor.scan_once()
    assert len(router.seen) <= 2
    assert sensor.health()["throttled"] >= 1


# ---------------------------------------------------------------------------
# Routing, refusals, wiring
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
    assert SignalSource(SOURCE) is SignalSource.CAPABILITY_SEVERANCE
    assert SOURCE in _BACKGROUND_SOURCES


@pytest.mark.asyncio
async def test_high_severity_does_not_escalate_the_route():
    """Severance is archaeology, not an incident. Escalating would put
    repo-wide static analysis on the Claude tier."""
    router = _Router()
    sensor = _stub_snapshot(LivenessSensor("repo", router), [_candidate()])
    await sensor.scan_once()
    env = router.seen[0]
    urgency = getattr(env, "urgency", None) or (
        env.get("urgency") if isinstance(env, dict) else None)
    assert urgency == "low"
    assert _ev(env, "severity") == "high"


@pytest.mark.asyncio
async def test_disabled_sensor_emits_nothing(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVENESS_SENSOR_ENABLED", "0")
    router = _Router()
    sensor = _stub_snapshot(LivenessSensor("repo", router), [_candidate()])
    assert await sensor.scan_once() == []
    assert router.seen == []


def test_default_is_off(monkeypatch):
    monkeypatch.delenv("JARVIS_LIVENESS_SENSOR_ENABLED", raising=False)
    assert ls.sensor_enabled() is False


@pytest.mark.asyncio
async def test_a_rejected_envelope_is_not_marked_seen():
    router = _Router(result="rejected")
    sensor = _stub_snapshot(LivenessSensor("repo", router), [_candidate()])
    await sensor.scan_once()
    assert sensor._seen == {}


def test_health_is_bounded_and_never_raises():
    h = LivenessSensor("repo", _Router()).health()
    assert set(h) >= {"enabled", "running", "scans", "emitted", "throttled",
                      "firing_counts", "bucket_tokens"}
