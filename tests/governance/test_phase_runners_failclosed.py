"""Anti-Venom fail-closed coverage for the LIVE phase-runners.

Task 7 hardened the orchestrator.py inline FSM (the ``JARVIS_PHASE_RUNNER_*
=false`` kill-switch FALLBACK path). The LIVE default path runs through the
extracted phase-runners (those flags default TRUE), so the immune fixes must
also live there. This module proves the ported locks on the runners:

* (A) ``gate_runner.py`` — SemanticGuardian crash FAILS CLOSED: risk_tier
  forced to APPROVAL_REQUIRED + a hard sentinel finding injected, never left
  at SAFE_AUTO (the historical fail-OPEN). Plus the git-HEAD baseline so an
  in-loop Venom write is compared original→candidate (not candidate→candidate).
* (B) ``slice4b_runner.py`` — the APPLY call(s) are ``asyncio.shield``-wrapped:
  the file-write + APPLIED ledger commit run to completion even if the op task
  is cancelled mid-write; CancelledError still propagates at the boundary.
* (C) ``generate_runner.py`` — a noop reported AFTER Venom landed in-loop
  writes CANCELS the op (``noop_inloop_write_guard``) instead of silently
  COMPLETE-ing unreviewed on-disk mutations.

Each test drives the REAL runner (constructed + ``run()``-ed) by reusing the
parity-test harness fakes. Legit (non-crash / non-cancel / plain-noop)
behavior is asserted unchanged.
"""
from __future__ import annotations

import asyncio
import dataclasses
import subprocess as _subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.op_context import OperationPhase
from backend.core.ouroboros.governance.phase_runners.gate_runner import GATERunner
from backend.core.ouroboros.governance.phase_runners.generate_runner import (
    GENERATERunner,
)
from backend.core.ouroboros.governance.phase_runners.slice4b_runner import (
    Slice4bRunner,
)
from backend.core.ouroboros.governance.risk_engine import RiskTier

# Reuse the established parity-test harnesses (real fakes that drive the
# runners through their full run() path). Importing the module does NOT
# trigger its autouse fixtures (those are module-scoped to that module).
from tests.governance.phase_runner import (  # noqa: E402
    test_gate_runner_parity as _gp,
    test_generate_runner_parity as _np,
    test_slice4b_runner_parity as _sp,
)


# ===========================================================================
# (A) gate_runner — Lock A fail-closed guardian + git-HEAD baseline
# ===========================================================================


def test_sentinel_guardian_crash_is_a_hard_finding():
    """The injected sentinel must be a *hard* finding so the tier-floor logic
    treats a guardian crash exactly like a fired hard pattern."""
    from backend.core.ouroboros.governance.orchestrator import (
        _SENTINEL_GUARDIAN_CRASH,
    )

    assert _SENTINEL_GUARDIAN_CRASH.severity == "hard"
    assert _SENTINEL_GUARDIAN_CRASH.pattern == "guardian_crashed"


@pytest.mark.asyncio
async def test_gate_guardian_crash_fails_closed(tmp_path, monkeypatch):
    """A SemanticGuardian crash on the LIVE gate-runner path must escalate a
    SAFE_AUTO candidate to APPROVAL_REQUIRED (fail-CLOSED), NOT leave it
    SAFE_AUTO with the semantic net silently down (the historical fail-OPEN)."""
    monkeypatch.setenv("JARVIS_NOTIFY_APPLY_DELAY_S", "0")
    monkeypatch.setenv("JARVIS_SAFE_AUTO_PREVIEW_DELAY_S", "0")

    ctx = _gp._gate_ctx(tmp_path)
    orch = _gp._orch(tmp_path)

    with patch(
        "backend.core.ouroboros.governance.semantic_guardian."
        "SemanticGuardian.inspect_batch",
        side_effect=RuntimeError("guardian boom"),
    ):
        result = await GATERunner(
            orch, None, _gp._candidate(), RiskTier.SAFE_AUTO,
        ).run(ctx)

    assert result.artifacts["risk_tier"] is RiskTier.APPROVAL_REQUIRED
    assert result.artifacts["risk_tier"] is not RiskTier.SAFE_AUTO


@pytest.mark.asyncio
async def test_gate_guardian_clean_preserves_safe_auto(tmp_path, monkeypatch):
    """Legit behavior preserved: a guardian that does NOT crash leaves a clean
    SAFE_AUTO candidate untouched."""
    monkeypatch.setenv("JARVIS_NOTIFY_APPLY_DELAY_S", "0")
    monkeypatch.setenv("JARVIS_SAFE_AUTO_PREVIEW_DELAY_S", "0")

    ctx = _gp._gate_ctx(tmp_path)
    orch = _gp._orch(tmp_path)
    result = await GATERunner(
        orch, None, _gp._candidate(), RiskTier.SAFE_AUTO,
    ).run(ctx)

    assert result.status == "ok"
    assert result.artifacts["risk_tier"] is RiskTier.SAFE_AUTO


@pytest.mark.asyncio
async def test_gate_guardian_baseline_uses_git_head_for_venom_paths(
    tmp_path, monkeypatch,
):
    """For a path Venom wrote in-loop, the guardian's pre-image (_old) is
    baselined from ``git show HEAD:<path>`` so it compares original→candidate
    (not the candidate→candidate that an on-disk read would produce)."""
    monkeypatch.setenv("JARVIS_NOTIFY_APPLY_DELAY_S", "0")
    monkeypatch.setenv("JARVIS_SAFE_AUTO_PREVIEW_DELAY_S", "0")

    ctx = _gp._gate_ctx(tmp_path)
    # ctx.generation carries the venom edit history (in-loop write to a.py).
    gen = SimpleNamespace(
        venom_edit_history=({"path": "a.py", "action": "edit"},),
    )
    object.__setattr__(ctx, "generation", gen)

    orch = _gp._orch(tmp_path)
    cand = {"candidate_id": "c0", "file_path": "a.py", "full_content": "NEW\n"}

    captured: dict = {}

    def _fake_inspect(self, pairs):  # noqa: ANN001
        captured["pairs"] = list(pairs)
        return []

    _head = _subprocess.CompletedProcess(
        args=[], returncode=0, stdout="ORIGINAL\n", stderr="",
    )
    with patch(
        "backend.core.ouroboros.governance.semantic_guardian."
        "SemanticGuardian.inspect_batch",
        _fake_inspect,
    ), patch(
        "backend.core.ouroboros.governance.phase_runners.gate_runner."
        "subprocess.run",
        return_value=_head,
    ) as _run:
        await GATERunner(orch, None, cand, RiskTier.SAFE_AUTO).run(ctx)

    # git show HEAD:a.py was invoked for the venom-touched path.
    assert _run.called
    _argv = _run.call_args[0][0]
    assert _argv[:2] == ["git", "show"]
    assert _argv[2] == "HEAD:a.py"
    # Guardian saw the HEAD baseline as _old (original→candidate).
    assert captured["pairs"] == [("a.py", "ORIGINAL\n", "NEW\n")]


# ===========================================================================
# (B) slice4b_runner — C2 asyncio.shield the apply
# ===========================================================================


def test_slice4b_apply_calls_wrapped_in_shield():
    """Static guard: both apply sites (multi-file + single-file) are
    shield-wrapped."""
    import inspect

    from backend.core.ouroboros.governance.phase_runners import slice4b_runner

    src = inspect.getsource(slice4b_runner)
    assert src.count("asyncio.shield(") >= 2


@pytest.mark.asyncio
async def test_slice4b_single_file_apply_is_shielded_against_outer_cancel(
    tmp_path,
):
    """Behavioral: the single-file apply runs to completion (file-write +
    APPLIED ledger commit, modeled by the change-engine reaching its tail)
    even when the op task is cancelled mid-write; CancelledError still
    propagates at the shield boundary."""
    release = asyncio.Event()
    entered = asyncio.Event()

    class _ShieldCE:
        def __init__(self):
            self.wrote = False
            self.executions: list = []

        async def execute(self, req):
            self.executions.append(req)
            entered.set()
            await release.wait()
            # Reached only if the shield protected the inner coroutine from
            # the outer cancel — this is the atomic write+ledger tail.
            self.wrote = True
            return _sp._FakeChangeResult(success=True)

    ce = _ShieldCE()
    orch = _sp._orch(tmp_path, change_engine=ce)
    ctx = _sp._approve_ctx(tmp_path)

    task = asyncio.create_task(
        Slice4bRunner(
            orch, None, _sp._candidate(tmp_path), RiskTier.SAFE_AUTO,
        ).run(ctx)
    )

    # Wait until the apply is in-flight (parked inside change_engine.execute).
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)  # deliver the cancel to the shield await boundary
    release.set()           # let the shielded inner apply finish

    # Stop is honored: CancelledError propagates out of run().
    with pytest.raises(asyncio.CancelledError):
        await task

    # ...but the shielded apply completed atomically despite the outer cancel.
    for _ in range(100):
        if ce.wrote:
            break
        await asyncio.sleep(0.01)
    assert ce.wrote is True


@pytest.mark.asyncio
async def test_slice4b_normal_apply_unchanged(tmp_path):
    """Legit behavior preserved: a normal (uncancelled) apply still advances
    to COMPLETE."""
    orch = _sp._orch(tmp_path)
    result = await Slice4bRunner(
        orch, None, _sp._candidate(tmp_path), RiskTier.SAFE_AUTO,
    ).run(ctx=_sp._approve_ctx(tmp_path))
    assert result.status == "ok"
    assert result.next_phase is OperationPhase.COMPLETE


# ===========================================================================
# (C) generate_runner — live noop + in-loop-write guard
# ===========================================================================


@pytest.mark.asyncio
async def test_noop_with_inloop_writes_cancels(tmp_path):
    """A noop reported after Venom landed in-loop writes is a guardian-bypass:
    unreviewed code on disk. The live runner must CANCEL (fail-closed), not
    silently COMPLETE."""
    gen = dataclasses.replace(
        _np._gen_result(has_candidates=False, is_noop=True),
        venom_edit_history=({"path": "a.py", "action": "edit"},),
    )
    generator = _np._FakeCandidateGenerator(result=gen)
    orch = _np._orch(tmp_path, generator=generator)
    ctx = _np._generate_ctx(tmp_path)

    result = await GENERATERunner(orch, None, None).run(ctx)

    assert result.status == "fail"
    assert result.next_ctx.phase is OperationPhase.CANCELLED
    assert result.next_ctx.terminal_reason_code == "noop_inloop_write_guard"
    assert result.reason == "noop_inloop_write_guard"
    # A FAILED ledger row was recorded for the guarded noop.
    assert any(
        extra.get("reason") == "noop_inloop_write_guard"
        for _phase, _state, extra in orch.ledger_records
    )


@pytest.mark.asyncio
async def test_plain_noop_without_inloop_writes_completes(tmp_path):
    """Legit behavior preserved: a plain noop (no in-loop Venom writes) still
    takes the legacy COMPLETE/noop terminal."""
    generator = _np._FakeCandidateGenerator(
        result=_np._gen_result(has_candidates=False, is_noop=True),
    )
    orch = _np._orch(tmp_path, generator=generator)
    ctx = _np._generate_ctx(tmp_path)

    result = await GENERATERunner(orch, None, None).run(ctx)

    assert result.next_ctx.phase is OperationPhase.COMPLETE
    assert result.next_ctx.terminal_reason_code == "noop"


__all__: list = []
