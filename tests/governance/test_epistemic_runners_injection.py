"""Task #4b — the epistemic-budget probe runner, constructed + injected.

Root cause: providers.py:5690 and doubleword_provider.py:5125 called
attach_to_provider_run passing probe_runner/sbt_runner/orange_queue=None, and the
concrete runners were never constructed — so PROBE_TRIGGERED recorded
"no_probe_runner_injected" and the confidence-drop → probe loop was a no-op
observer. These tests prove the bolt is torqued: a real probe runner is
constructed (gated), it satisfies the Protocol, it runs the real pipeline, and
both provider call sites inject it.
"""
from __future__ import annotations

import asyncio

from backend.core.ouroboros.governance import epistemic_runners as er


def test_flag_defaults_off():
    import os
    os.environ.pop("JARVIS_EPISTEMIC_RUNNERS_ENABLED", None)
    assert er.epistemic_runners_enabled() is False


def test_off_yields_all_none_byte_identical(monkeypatch):
    """Flag off → (None, None, None): byte-identical to the pre-Task-4b path
    (attach_to_provider_run receives None runners exactly as before)."""
    monkeypatch.delenv("JARVIS_EPISTEMIC_RUNNERS_ENABLED", raising=False)
    assert er.build_epistemic_runners(op_id="op-1", claim="x") == (None, None, None)


def test_on_constructs_a_real_probe_runner(monkeypatch):
    monkeypatch.setenv("JARVIS_EPISTEMIC_RUNNERS_ENABLED", "true")
    probe, sbt, orange = er.build_epistemic_runners(
        op_id="op-1", target_file="a.py", claim="foo is int", posture="HARDEN",
    )
    assert probe is not None
    # Satisfies ProbeRunnerProtocol.run(*, payload) as an async method.
    assert hasattr(probe, "run") and asyncio.iscoroutinefunction(probe.run)
    # sbt/orange remain None (documented follow-up; hook handles gracefully).
    assert sbt is None and orange is None


def test_empty_op_id_short_circuits(monkeypatch):
    monkeypatch.setenv("JARVIS_EPISTEMIC_RUNNERS_ENABLED", "true")
    assert er.build_epistemic_runners(op_id="") == (None, None, None)


def test_probe_run_invokes_real_pipeline_and_returns_verdict(monkeypatch):
    """The adapter's run() must call the REAL execute_probe_environment with an
    AmbiguityContext synthesized from the captured op scalars, and return its
    verdict — not a no-op."""
    monkeypatch.setenv("JARVIS_EPISTEMIC_RUNNERS_ENABLED", "true")

    captured = {}

    async def _fake_execute(*, monitor, ambiguity_context, op_id, prior, resolver):
        captured["op_id"] = op_id
        captured["claim"] = ambiguity_context.claim
        captured["target_file"] = ambiguity_context.target_file
        captured["resolver"] = resolver
        return _FakeVerdict()

    class _FakeVerdict:
        action = type("A", (), {"value": "retry_with_feedback"})()

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.probe_environment_executor"
        ".execute_probe_environment",
        _fake_execute,
    )

    probe, _, _ = er.build_epistemic_runners(
        op_id="op-42", target_file="mod.py", claim="the claim", posture="EXPLORE",
    )
    verdict = asyncio.run(probe.run(payload={}))  # empty payload, enriched inside
    assert verdict is not None
    assert captured["op_id"] == "op-42"
    assert captured["claim"] == "the claim"
    assert captured["target_file"] == "mod.py"
    assert captured["resolver"] is not None  # real prober injected


def test_probe_run_never_raises(monkeypatch):
    """A probe failure must fail-soft to None, never raise into GENERATE."""
    monkeypatch.setenv("JARVIS_EPISTEMIC_RUNNERS_ENABLED", "true")

    async def _boom(**kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(
        "backend.core.ouroboros.governance.verification.probe_environment_executor"
        ".execute_probe_environment",
        _boom,
    )
    probe, _, _ = er.build_epistemic_runners(op_id="op-x", claim="c")
    assert asyncio.run(probe.run(payload={})) is None  # swallowed, not raised


def test_both_provider_sites_inject_the_runners():
    """Guard against re-severing: both provider call sites must build + inject
    the runners (not pass the bare op_id/route/risk_tier of the old code)."""
    import inspect
    from backend.core.ouroboros.governance import providers, doubleword_provider

    for mod in (providers, doubleword_provider):
        src = inspect.getsource(mod)
        assert "build_epistemic_runners" in src, (
            f"{mod.__name__} no longer builds the epistemic runners — re-severed"
        )
        assert "probe_runner=_eb_probe" in src, (
            f"{mod.__name__} no longer injects the probe runner"
        )
