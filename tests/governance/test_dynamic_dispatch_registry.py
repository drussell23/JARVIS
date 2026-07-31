"""AST cannot see dispatch — and REGISTERED is not FIRING.

`capability_liveness` under-reports by construction: a pub/sub handler, an
`importlib` load and a `getattr` route leave no static edge, so a live
capability reads as dead. Its own verdict text admits it ("may be dynamically
dispatched — a review candidate, not proven dead"). This registry is the
other half of that sentence.

The load-bearing test is `test_registration_alone_does_NOT_clear_a_finding`.
The tempting design treats anything in the registry as alive — but
subscribing proves a handler ASKED to be called, never that it WAS. A handler
that registers at boot and never fires is exactly the failure being hunted,
and "registered ⇒ alive" would let the audit confidently clear the one case
it exists to catch.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import dynamic_dispatch_registry as ddr
from backend.core.ouroboros.governance.dynamic_dispatch_registry import (
    FIRING_DYNAMICALLY,
    REGISTERED_NEVER_INVOKED,
    UNSEEN,
    dynamic_verdict,
    dynamically_dispatched,
    note_invocation,
    register,
    snapshot,
)
from backend.core.ouroboros.governance.intake.sensors.liveness_sensor import (
    LivenessFinding,
    LivenessSensor,
    effective_firing,
    severity_for,
)


class _Router:
    def __init__(self):
        self.seen = []

    async def ingest(self, envelope):
        self.seen.append(envelope)
        return "enqueued"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("JARVIS_LIVENESS_SENSOR_ENABLED", "1")
    monkeypatch.delenv("JARVIS_DYNAMIC_DISPATCH_REGISTRY_ENABLED", raising=False)
    ddr.reset_for_tests()
    yield
    ddr.reset_for_tests()


ISOLATED = "phantom_handler_module"


def _severed_row(**kw):
    """A capability AST says is 100% unreachable."""
    row = {
        "source_file": f"governance/{ISOLATED}.py",
        "category": "safety",
        "flag": "JARVIS_PHANTOM_ENABLED",
        "firing": "SILENT",
        "fraction_severed": 1.0,
        "severed_symbols": ["handle_event", "on_signal"],
    }
    row.update(kw)
    return row


def _stub(sensor, rows):
    async def _collect():
        from backend.core.ouroboros.governance.intake.sensors import (
            liveness_sensor as ls,
        )
        out = []
        for r in rows:
            sf = r["source_file"].split("/")[-1]
            firing = ls.effective_firing(sf, r["firing"])
            if firing == FIRING_DYNAMICALLY:
                continue
            out.append(LivenessFinding(
                source_file=sf, category=r["category"], flag=r["flag"],
                firing=firing, fraction_severed=r["fraction_severed"],
                severed_symbols=tuple(r["severed_symbols"]),
                severity=ls.severity_for(r["category"], firing,
                                         r["fraction_severed"], sf)))
        out.sort(key=lambda f: f.rank)
        return out
    sensor.collect_findings = _collect
    return sensor


# ---------------------------------------------------------------------------
# The mandate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_isolated_module_is_flagged_dead_before_the_breadcrumb():
    """Baseline: zero static edges -> high-severity severance."""
    router = _Router()
    sensor = _stub(LivenessSensor("repo", router), [_severed_row()])
    found = await sensor.scan_once()
    assert len(found) == 1
    assert found[0].severity == "high"
    assert found[0].firing == "SILENT"
    assert len(router.seen) == 1, "a fully severed safety module did not emit"


@pytest.mark.asyncio
async def test_the_breadcrumb_reclassifies_it_and_prevents_a_false_positive():
    """THE mandate case: register + INVOKE, and the alert must disappear."""
    register(f"{ISOLATED}.py", channel="fs.changed")
    note_invocation(f"{ISOLATED}.py", channel="fs.changed")

    assert dynamic_verdict(f"{ISOLATED}.py") == FIRING_DYNAMICALLY
    assert effective_firing(f"{ISOLATED}.py", "SILENT") == FIRING_DYNAMICALLY

    router = _Router()
    sensor = _stub(LivenessSensor("repo", router), [_severed_row()])
    found = await sensor.scan_once()

    assert found == [], "a demonstrably-running module was still reported dead"
    assert router.seen == [], "false-positive dead-code alert was emitted"


@pytest.mark.asyncio
async def test_registration_alone_does_NOT_clear_a_finding():
    """The load-bearing refusal.

    Subscribing proves the handler asked to be called, never that it was.
    "Registered ⇒ alive" would flip the exact bug being hunted from
    "investigate" to "ignore".
    """
    register(f"{ISOLATED}.py", channel="fs.changed")   # no invocation
    assert dynamic_verdict(f"{ISOLATED}.py") == REGISTERED_NEVER_INVOKED

    router = _Router()
    sensor = _stub(LivenessSensor("repo", router), [_severed_row()])
    found = await sensor.scan_once()

    assert len(found) == 1, "registration silently cleared a severed module"
    assert found[0].firing == REGISTERED_NEVER_INVOKED
    assert found[0].severity == "high", (
        "registered-but-never-invoked is evidence OF severance, not against it")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_unseen_when_nothing_was_recorded():
    assert dynamic_verdict("never_heard_of_it") == UNSEEN


def test_a_disabled_registry_vouches_for_nothing(monkeypatch):
    note_invocation("m.py")
    monkeypatch.setenv("JARVIS_DYNAMIC_DISPATCH_REGISTRY_ENABLED", "0")
    assert dynamic_verdict("m.py") == UNSEEN
    assert effective_firing("m.py", "SILENT") == "SILENT"


def test_module_keys_normalise_to_the_basename_liveness_reports():
    """Keying on anything else makes the intersection match nothing, which
    looks identical to "no dynamic evidence"."""
    note_invocation("backend.core.ouroboros.governance.repair_engine")
    assert dynamic_verdict("repair_engine") == FIRING_DYNAMICALLY
    assert dynamic_verdict("governance/repair_engine.py") == FIRING_DYNAMICALLY


def test_registry_is_bounded(monkeypatch):
    monkeypatch.setenv("JARVIS_DYNAMIC_DISPATCH_MAX_MODULES", "16")
    ddr.reset_for_tests()
    for i in range(200):
        register(f"m{i}.py")
    snap = snapshot()
    assert snap["tracked"] <= 16
    assert snap["dropped"] > 0


def test_breadcrumbs_never_raise_on_junk():
    for bad in (None, "", 123, object()):
        register(bad)
        note_invocation(bad)
        assert isinstance(dynamic_verdict(bad), str)


@pytest.mark.asyncio
async def test_the_decorator_records_both_facts():
    calls = []

    @dynamically_dispatched(channel="test.topic")
    async def _handler(evt):
        calls.append(evt)
        return "ok"

    # Decoration alone is registration, not invocation.
    assert dynamic_verdict(_handler.__module__) == REGISTERED_NEVER_INVOKED
    assert await _handler("e") == "ok"
    assert calls == ["e"]
    assert dynamic_verdict(_handler.__module__) == FIRING_DYNAMICALLY


def test_the_decorator_is_transparent_for_sync_handlers():
    @dynamically_dispatched
    def _sync(a, b=2):
        return a + b

    assert _sync(1) == 3
    assert _sync.__name__ == "_sync"


# ---------------------------------------------------------------------------
# Wiring — the EXISTING seams, not a parallel tracker
# ---------------------------------------------------------------------------


def test_the_event_bus_records_registration_and_invocation_separately():
    """DRY: attached to TrinityEventBus's own subscribe/deliver seams.

    They must be DIFFERENT seams — recording invocation at subscribe time
    would make every subscriber look alive forever.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[2]
           / "backend/core/trinity_event_bus.py").read_text(encoding="utf-8")
    assert "dynamic_dispatch_registry" in src
    assert "register as _dd_register" in src
    assert "note_invocation as _dd_invoked" in src
    # Registration in subscribe, invocation in delivery — not the same place.
    assert src.index("register as _dd_register") < src.index(
        "note_invocation as _dd_invoked")


def test_severity_demotes_only_on_real_invocation():
    assert severity_for("safety", "SILENT", 1.0, "ghost.py") == "high"
    note_invocation("ghost.py")
    assert severity_for("safety", "SILENT", 1.0, "ghost.py") == "low"
