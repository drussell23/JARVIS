"""A failed probe is a fault, not a verdict.

An Ollama node reloading the 30B answers ``/api/tags`` with the model absent.
The served-model probe returned ``None``, the resolver accepted the NOMINAL
slot declaration — ``full_content_only`` — and the diff schema was never
requested for the entire session, silently, because nothing distinguished
"measured as whole-file-only" from "not measured at all". The reload took
seconds. The soak took an hour and produced whole-file rewrites.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.core.ouroboros.governance import governed_loop_service as GLS


class _Verdict:
    def __init__(self, served_model, declared, capability, changed=False, reason="r"):
        self.served_model = served_model
        self.declared = declared
        self.capability = capability
        self.changed = changed
        self.reason = reason


class _Selector:
    """The real contract: a served model resolves the capability; ``None``
    leaves the declaration standing — which is exactly the silent path."""

    def effective_schema_capability(self, *, declared, served_model):
        if served_model and "qwen3-coder" in served_model:
            return _Verdict(served_model, declared, "full_content_and_diff", changed=True)
        return _Verdict(served_model or "", declared, declared)


def _gls():
    return types.SimpleNamespace(_brain_selector=_Selector())


def _brain(declared="full_content_only"):
    return types.SimpleNamespace(schema_capability=declared, brain_id="local")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    GLS._LAST_OBSERVED_CAPABILITY.clear()
    GLS._PROBE_FAULT_UNTIL.clear()
    GLS._SERVED_CAP_ANNOUNCED.clear()
    monkeypatch.setattr(GLS, "_local_lane_endpoint", lambda: "http://node:11434")
    # The retry ladder sleeps; no test should pay for it.
    monkeypatch.setenv("JARVIS_CAPABILITY_PROBE_RETRIES", "0")
    yield
    GLS._LAST_OBSERVED_CAPABILITY.clear()
    GLS._PROBE_FAULT_UNTIL.clear()


def _probe(*results):
    """A probe that returns *results* in order, then repeats the last."""
    seq = list(results)
    calls = []

    async def _fn(endpoint):
        calls.append(endpoint)
        return seq[min(len(calls) - 1, len(seq) - 1)]

    _fn.calls = calls
    return _fn


def _resolve(monkeypatch, probe):
    monkeypatch.setattr(GLS, "_probe_served_model", probe)
    return asyncio.run(GLS._resolve_served_capability(_gls(), _brain()))


def test_a_live_probe_resolves_the_capability(monkeypatch):
    served, cap = _resolve(monkeypatch, _probe("qwen3-coder-ov:30b"))
    assert cap == "full_content_and_diff"
    assert served == "qwen3-coder-ov:30b"


def test_a_probe_fault_reuses_what_was_OBSERVED_not_what_was_declared(monkeypatch):
    """THE defect. A reload is not a downgrade: the lane served a diff-capable
    model a minute ago and nothing has said otherwise."""
    p = _probe("qwen3-coder-ov:30b", None)
    monkeypatch.setattr(GLS, "_probe_served_model", p)
    assert asyncio.run(GLS._resolve_served_capability(_gls(), _brain()))[1] == "full_content_and_diff"
    assert asyncio.run(GLS._resolve_served_capability(_gls(), _brain()))[1] == "full_content_and_diff"


def test_a_probe_fault_is_announced_at_WARNING(monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        _resolve(monkeypatch, _probe(None))
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "CapabilityProbeFault" in msgs
    assert "UNMEASURED" in msgs


def test_with_nothing_ever_observed_it_says_the_diff_schema_will_be_withheld(
    monkeypatch, caplog,
):
    """Degradation still happens here — there is nothing to reuse. What must
    never happen is that it happens QUIETLY."""
    with caplog.at_level("WARNING"):
        served, cap = _resolve(monkeypatch, _probe(None))
    assert cap == "full_content_only"
    assert "will NOT be offered the diff schema" in " ".join(
        r.getMessage() for r in caplog.records
    )


def test_the_fallback_is_never_self_confirming(monkeypatch):
    """A fault path that recorded the declaration it just fell back to would
    manufacture an 'observation' that was never observed, and every later
    fault would cite it as evidence."""
    _resolve(monkeypatch, _probe(None))
    assert GLS._LAST_OBSERVED_CAPABILITY == {}


def test_the_retry_ladder_runs_then_recovers(monkeypatch):
    monkeypatch.setenv("JARVIS_CAPABILITY_PROBE_RETRIES", "2")
    monkeypatch.setenv("JARVIS_CAPABILITY_PROBE_BACKOFF_S", "0")
    seq = [None, "qwen3-coder-ov:30b"]
    calls = {"n": 0}

    async def _resolve_served_model(endpoint):
        calls["n"] += 1
        return seq[min(calls["n"] - 1, len(seq) - 1)]

    import backend.core.ouroboros.governance.candidate_generator as CG
    monkeypatch.setattr(CG, "_resolve_served_model", _resolve_served_model)
    assert asyncio.run(GLS._probe_served_model("http://node:11434")) == "qwen3-coder-ov:30b"
    assert calls["n"] == 2


def test_an_announced_fault_suppresses_the_ladder_for_a_window(monkeypatch):
    """Without this the ladder is paid on EVERY submit, so a genuinely dead
    endpoint adds its full latency to every op in the soak."""
    monkeypatch.setenv("JARVIS_CAPABILITY_PROBE_RETRIES", "3")
    monkeypatch.setenv("JARVIS_CAPABILITY_PROBE_BACKOFF_S", "0")
    calls = {"n": 0}

    async def _dead(endpoint):
        calls["n"] += 1
        return None

    import backend.core.ouroboros.governance.candidate_generator as CG
    monkeypatch.setattr(CG, "_resolve_served_model", _dead)
    asyncio.run(GLS._probe_served_model("http://node:11434"))
    first = calls["n"]
    assert first == 4                      # one probe + three retries
    asyncio.run(GLS._probe_served_model("http://node:11434"))
    assert calls["n"] == first + 1         # inside the window: no ladder


def test_a_recovery_clears_the_window(monkeypatch):
    import backend.core.ouroboros.governance.candidate_generator as CG
    monkeypatch.setenv("JARVIS_CAPABILITY_PROBE_BACKOFF_S", "0")

    async def _dead(endpoint):
        return None

    async def _live(endpoint):
        return "qwen3-coder-ov:30b"

    monkeypatch.setattr(CG, "_resolve_served_model", _dead)
    asyncio.run(GLS._probe_served_model("http://node:11434"))
    assert "http://node:11434" in GLS._PROBE_FAULT_UNTIL
    monkeypatch.setattr(CG, "_resolve_served_model", _live)
    asyncio.run(GLS._probe_served_model("http://node:11434"))
    assert "http://node:11434" not in GLS._PROBE_FAULT_UNTIL


def test_resolution_never_blocks_submit(monkeypatch):
    async def _boom(endpoint):
        raise RuntimeError("node on fire")

    monkeypatch.setattr(GLS, "_probe_served_model", _boom)
    served, cap = asyncio.run(GLS._resolve_served_capability(_gls(), _brain()))
    assert cap == "full_content_only"
