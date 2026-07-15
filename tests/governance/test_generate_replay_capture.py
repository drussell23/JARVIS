"""Task #11 — close GENERATE replay-blindness.

GENERATE was the one phase blind to replay: the determinism substrate
captured only a provider-selection DIGEST (a hash), so REPLAY re-invoked the
model live — the most expensive, least reproducible phase couldn't be
replayed. This wraps the FULL generation acquisition in
capture_phase_decision with a GenerationResult adapter, so REPLAY returns the
recorded candidates and SKIPS the provider call entirely.

Proof obligations:
  * The GenerationResult adapter round-trips faithfully.
  * RECORD then REPLAY of the same op returns the recorded result WITHOUT
    invoking the (model) compute — the actual replay-blindness fix.
  * PASSTHROUGH (default) runs compute live — bit-for-bit legacy.
  * The wire is present + digest-only capture is gone (guard re-severing).
  * A park (BaseException) propagates through the capture, never swallowed.
"""
from __future__ import annotations

import inspect

import pytest

from backend.core.ouroboros.governance.op_context import GenerationResult
# Importing the runner registers the GENERATE/generate adapter at module load.
import backend.core.ouroboros.governance.phase_runners.generate_runner as gr  # noqa: F401,E501
from backend.core.ouroboros.governance.determinism.phase_capture import (
    capture_phase_decision,
    get_adapter,
    iter_registered,
)
from backend.core.ouroboros.governance.determinism.decision_runtime import (
    reset_all_for_tests,
)
from backend.core.ouroboros.governance.park_signal import ParkRequested


def _gen(n: int = 2) -> GenerationResult:
    return GenerationResult(
        candidates=tuple(
            {"file_path": f"f{i}.py", "full_content": f"x = {i}\n"}
            for i in range(n)
        ),
        provider_name="dw",
        generation_duration_s=1.25,
        model_id="model-x",
        is_noop=False,
        venom_edit_history=({"tool": "edit_file", "path": "f0.py"},),
        prompt_preloaded_files=("f0.py",),
        total_input_tokens=100,
        total_output_tokens=42,
        cost_usd=0.01,
    )


class _Ctx:
    op_id = "op-gen-1"
    provider_route = "STANDARD"
    signal_urgency = ""
    signal_source = ""
    task_complexity = ""
    target_files = ()
    cross_repo = False
    is_read_only = False


@pytest.fixture
def det_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path / "det"))
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DETERMINISM_PHASE_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("OUROBOROS_BATTLE_SESSION_ID", "gen-replay-test")
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MODE", raising=False)
    reset_all_for_tests()
    yield
    reset_all_for_tests()


# ── adapter round-trip ───────────────────────────────────────────────

def test_generate_adapter_is_registered():
    assert ("GENERATE", "generate") in iter_registered()
    assert get_adapter(phase="GENERATE", kind="generate").name == (
        "generation_result_adapter"
    )


def test_adapter_roundtrips_generation_result():
    a = get_adapter(phase="GENERATE", kind="generate")
    g = _gen(3)
    back = a.deserialize(a.serialize(g))
    assert isinstance(back, GenerationResult)
    assert back.candidates == g.candidates
    assert back.provider_name == g.provider_name
    assert back.model_id == g.model_id
    assert back.total_output_tokens == g.total_output_tokens
    assert back.cost_usd == g.cost_usd
    assert back.venom_edit_history == g.venom_edit_history
    # tool_execution_records are live-execution audit → empty on the
    # reconstructed (replay) result, by design.
    assert back.tool_execution_records == ()


def test_adapter_handles_none():
    a = get_adapter(phase="GENERATE", kind="generate")
    assert a.deserialize(a.serialize(None)) is None


# ── THE fix: RECORD then REPLAY skips the model ──────────────────────

@pytest.mark.asyncio
async def test_record_then_replay_skips_the_model(det_env, monkeypatch):
    calls = {"n": 0}

    async def compute_live():
        calls["n"] += 1
        return _gen(2)

    # RECORD — compute runs once, result serialized to the ledger.
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    reset_all_for_tests()
    out1 = await capture_phase_decision(
        op_id="op-gen-1", phase="GENERATE", kind="generate",
        ctx=_Ctx(), compute=compute_live,
    )
    assert calls["n"] == 1
    assert isinstance(out1, GenerationResult)
    assert out1.candidates == _gen(2).candidates

    # REPLAY — the model MUST NOT be called. A fresh runtime reads the
    # recorded result from disk and returns it; compute would raise.
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "replay")
    reset_all_for_tests()

    async def compute_boom():
        calls["n"] += 1
        raise AssertionError("the model was called during REPLAY!")

    out2 = await capture_phase_decision(
        op_id="op-gen-1", phase="GENERATE", kind="generate",
        ctx=_Ctx(), compute=compute_boom,
    )
    assert calls["n"] == 1  # compute_boom NEVER ran → blindness closed
    assert isinstance(out2, GenerationResult)
    assert out2.candidates == _gen(2).candidates
    assert out2.provider_name == "dw"
    assert out2.model_id == "model-x"


@pytest.mark.asyncio
async def test_passthrough_default_runs_compute_live(det_env, monkeypatch):
    """Default (no LEDGER_MODE = PASSTHROUGH): compute runs, nothing is
    recorded — bit-for-bit legacy."""
    monkeypatch.delenv("JARVIS_DETERMINISM_LEDGER_MODE", raising=False)
    reset_all_for_tests()
    calls = {"n": 0}

    async def compute_live():
        calls["n"] += 1
        return _gen(1)

    out = await capture_phase_decision(
        op_id="op-pt-1", phase="GENERATE", kind="generate",
        ctx=_Ctx(), compute=compute_live,
    )
    assert calls["n"] == 1
    assert isinstance(out, GenerationResult)


@pytest.mark.asyncio
async def test_park_propagates_through_capture(det_env, monkeypatch):
    """A PARK-EMIT (ParkRequested, a BaseException) raised by the generator
    must fly THROUGH the capture wrapper untouched — never swallowed, never
    recorded as a generation."""
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_MODE", "record")
    reset_all_for_tests()

    from types import SimpleNamespace
    _sig = SimpleNamespace(
        op_id="op-park-1", token="tok", attempt_seq=0,
        descriptor=SimpleNamespace(kind="generate"),
    )

    async def compute_park():
        raise ParkRequested(_sig)

    with pytest.raises(ParkRequested):
        await capture_phase_decision(
            op_id="op-park-1", phase="GENERATE", kind="generate",
            ctx=_Ctx(), compute=compute_park,
        )


# ── reachability: the wire is present, digest-only capture is gone ───

def test_generate_runner_wraps_acquisition_not_digest():
    src = inspect.getsource(gr.GENERATERunner.run)
    # The full acquisition is captured under kind="generate"...
    assert 'kind="generate"' in src
    assert "_acquire_generation" in src
    assert "compute=_acquire_generation" in src
    # ...and the old digest-only provider_selection capture is gone.
    assert "provider_selection" not in src
    assert "_digest_compute" not in src
