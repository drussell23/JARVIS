"""The self-immunization audit has a caller, and its verdict reaches health.

``audit_self_immunization`` had no production caller -- its flag did nothing
(2026-10-08). AntivenomObserver runs it at boot and on an interval, off the
event loop, and settles each verdict into the capability-health ledger so a
guardian that misses a mutated pattern raises the same alarm as any failing
capability.
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

from backend.core.ouroboros.governance import antivenom_observer as ao


class _Ledger:
    def __init__(self):
        self.settled = []

    async def settle(self, capability, *, ok, op_id="", detail=""):
        self.settled.append((capability, ok, detail))


def _report(gaps, finding=None):
    return SimpleNamespace(finding=SimpleNamespace(value=finding or ("gap" if gaps else "immunized")),
                           gap_count=gaps, diagnostic=f"gaps={gaps}")


def test_an_immunized_guardian_settles_healthy():
    ledger = _Ledger()
    obs = ao.AntivenomObserver(audit=lambda: _report(0), ledger=ledger)
    asyncio.run(obs.run_once())
    assert ledger.settled == [(ao.CAPABILITY, True, "gaps=0")]


def test_a_missed_mutation_settles_as_a_failure():
    ledger = _Ledger()
    obs = ao.AntivenomObserver(audit=lambda: _report(3), ledger=ledger)
    asyncio.run(obs.run_once())
    assert ledger.settled == [(ao.CAPABILITY, False, "gaps=3")]


def test_a_disabled_audit_settles_nothing():
    ledger = _Ledger()
    obs = ao.AntivenomObserver(audit=lambda: _report(0, "disabled"), ledger=ledger)
    asyncio.run(obs.run_once())
    assert ledger.settled == []


def test_the_audit_runs_off_the_event_loop():
    seen = {}

    def audit():
        seen["thread"] = threading.current_thread() is threading.main_thread()
        return _report(0)
    asyncio.run(ao.AntivenomObserver(audit=audit, ledger=_Ledger()).run_once())
    assert seen["thread"] is False


def test_a_failing_audit_never_takes_the_loop_down():
    def boom():
        raise RuntimeError("guardian import failed")
    assert asyncio.run(ao.AntivenomObserver(audit=boom, ledger=_Ledger()).run_once()) is None


def test_it_runs_at_boot_then_waits_its_interval_and_stops_cleanly():
    ledger = _Ledger()

    async def scenario():
        obs = ao.AntivenomObserver(audit=lambda: _report(0), ledger=ledger, interval=lambda: 3600.0)
        obs.start()
        await asyncio.sleep(0.2)
        await obs.stop()
    asyncio.run(scenario())
    assert len(ledger.settled) == 1                  # boot run; the next waits an interval


def test_the_real_audit_runs_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_ANTIVENOM_IMMUNIZATION_ENABLED", "true")
    monkeypatch.setenv("JARVIS_ANTIVENOM_IMMUNIZATION_LEDGER_PATH", str(tmp_path / "av.jsonl"))
    ledger = _Ledger()
    report = asyncio.run(ao.AntivenomObserver(ledger=ledger).run_once())
    assert report is not None and report.probes_run > 0
    assert ledger.settled and ledger.settled[0][0] == ao.CAPABILITY


def test_the_loop_service_starts_and_stops_it():
    import inspect
    from backend.core.ouroboros.governance import governed_loop_service as gls
    src = inspect.getsource(gls)
    assert "AntivenomObserver()" in src and '"_antivenom_observer",' in src
