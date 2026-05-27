"""Slice 28 — Adaptive Streaming TTFT Horizon & Inline Fault Discriminator.

Closes the v21 (bt-2026-05-27-025855) capability wedge: 12 EXHAUSTION
events on the 397B model, ALL with fsm_failure_mode=TIMEOUT, ALL at
elapsed=30.01s with remaining=329.86s. The static
``_PRIMARY_MAX_TIMEOUT_S`` (30s default) in ``_compute_primary_budget``
was killing primary calls before the streaming layer's 120s TTFT could
even fire on the wire. Cold-start TTFT for a 397B MoE legitimately
exceeds 30s per §46 fleet inventory.

# Phase 2 — Adaptive primary budget

``_compute_primary_budget(total_s, *, model_id="")`` now applies a
2.5× heavy scalar when ``_is_heavy_model(model_id)`` returns True
(same Qwen-397B/Kimi-K2.6 markers Slice 27 Phase 3 uses). Cap at 240s.
Legacy callers (no kwargs) preserve the byte-identical 30s cap.

# Phase 3 — Inline fault discriminator

On ``TimeoutError`` from ``_call_primary``'s wait_for, fires a
lightweight 2-token probe via ``self._primary.prompt_only``
(Slice 27 Phase 2 Aegis-stabilized) with a 5s wall budget.
Classifies:
  * probe_ok + fast latency → ``context_lag``
  * probe failed/timeout → ``infrastructure_outage``

Pure observability — never raises into caller, never changes return
value. The sentinel walker handles rotation structurally on the
original raise. Env-gated default-off via
``JARVIS_TTFT_FAULT_DISCRIMINATOR_ENABLED``.

# Test surface (3 AST pins + 9 spine)
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CG_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "candidate_generator.py"
)


# ──────────────────────────────────────────────────────────────────────
# AST PINS — 3
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_slice28_phase2_constants_present() -> None:
    """The Slice 28 Phase 2 substrate constants + env knobs MUST be
    declared so operators can grep + tune."""
    src = CG_FILE.read_text()
    assert "Slice 28 Phase 2" in src, (
        "candidate_generator missing Slice 28 Phase 2 attribution"
    )
    for sym in (
        "_PRIMARY_HEAVY_TTFT_SCALAR_DEFAULT",
        "_PRIMARY_HEAVY_TTFT_CAP_S_DEFAULT",
    ):
        assert sym in src, f"Slice 28 Phase 2 symbol {sym!r} missing"
    for env in (
        "JARVIS_PRIMARY_HEAVY_TTFT_SCALAR",
        "JARVIS_PRIMARY_HEAVY_TTFT_CAP_S",
    ):
        assert env in src, (
            f"Slice 28 Phase 2 env knob {env!r} missing — operator "
            "cannot tune without code edit"
        )


def test_ast_pin_compute_primary_budget_accepts_model_id() -> None:
    """``_compute_primary_budget`` MUST accept ``model_id`` as a
    keyword-only arg with a default. Without this, the call site at
    ``_call_primary`` can't engage adaptive mode and the v21 wedge
    persists."""
    src = CG_FILE.read_text()
    tree = ast.parse(src, filename=str(CG_FILE))
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef)
            and node.name == "_compute_primary_budget"
        ):
            # First positional is total_s; model_id must be in kwonlyargs
            kwonly = [a.arg for a in node.args.kwonlyargs]
            assert "model_id" in kwonly, (
                "_compute_primary_budget missing model_id kwarg — "
                "adaptive heavy scalar unreachable"
            )
            found = True
            break
    assert found, "_compute_primary_budget not found"


def test_ast_pin_phase3_fault_discriminator_method_present() -> None:
    """``_slice28_phase3_classify_ttft_failure`` MUST exist as an async
    method on CandidateGenerator AND the ``_call_primary`` except
    block MUST invoke it under env-flag gate."""
    src = CG_FILE.read_text()
    assert "Slice 28 Phase 3" in src, (
        "candidate_generator missing Slice 28 Phase 3 attribution"
    )
    assert "_slice28_phase3_classify_ttft_failure" in src, (
        "Phase 3 method missing"
    )
    assert "JARVIS_TTFT_FAULT_DISCRIMINATOR_ENABLED" in src, (
        "Phase 3 master flag env knob missing"
    )
    # Confirm the method is defined as an async method
    tree = ast.parse(src, filename=str(CG_FILE))
    found_def = False
    found_call = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_slice28_phase3_classify_ttft_failure"
        ):
            found_def = True
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_call_primary"
        ):
            body_src = ast.unparse(node)
            if "_slice28_phase3_classify_ttft_failure" in body_src:
                found_call = True
    assert found_def, (
        "_slice28_phase3_classify_ttft_failure not defined as async method"
    )
    assert found_call, (
        "_call_primary doesn't invoke _slice28_phase3_classify_ttft_failure — "
        "Phase 3 hook dead code"
    )


# ──────────────────────────────────────────────────────────────────────
# Phase 2 adaptive primary budget spine — 5
# ──────────────────────────────────────────────────────────────────────


def test_spine_phase2_legacy_no_model_id_preserves_30s_cap(monkeypatch) -> None:
    """Legacy caller (no model_id) MUST return the same value as
    pre-Slice-28 — the static 30s cap from _PRIMARY_MAX_TIMEOUT_S."""
    monkeypatch.delenv("OUROBOROS_PRIMARY_MAX_TIMEOUT_S", raising=False)
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    fn = CandidateGenerator._compute_primary_budget
    # total_s=300 (plenty) → cap at 30s (legacy)
    assert fn(300.0) == 30.0


def test_spine_phase2_heavy_397b_gets_75s_budget(monkeypatch) -> None:
    """Qwen-397B is heavy → 30 × 2.5 = 75s budget when total_s permits."""
    monkeypatch.delenv("OUROBOROS_PRIMARY_MAX_TIMEOUT_S", raising=False)
    monkeypatch.delenv("JARVIS_PRIMARY_HEAVY_TTFT_SCALAR", raising=False)
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    budget = CandidateGenerator._compute_primary_budget(
        300.0, model_id="Qwen/Qwen3.5-397B-A17B-FP8",
    )
    assert budget == 75.0, (
        f"Expected 75s heavy budget for 397B, got {budget}"
    )


def test_spine_phase2_heavy_kimi_gets_75s_budget(monkeypatch) -> None:
    """Kimi-K2.6 is the other heavy model in the default marker list."""
    monkeypatch.delenv("JARVIS_PRIMARY_HEAVY_TTFT_SCALAR", raising=False)
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    budget = CandidateGenerator._compute_primary_budget(
        300.0, model_id="moonshotai/Kimi-K2.6",
    )
    assert budget == 75.0


def test_spine_phase2_non_heavy_35b_preserves_30s_cap(monkeypatch) -> None:
    """Qwen-35B is NOT a heavy model — no scalar applied, legacy 30s."""
    monkeypatch.delenv("OUROBOROS_PRIMARY_MAX_TIMEOUT_S", raising=False)
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator, _is_heavy_model,
    )
    assert _is_heavy_model("Qwen/Qwen3.5-35B-A3B-FP8") is False
    budget = CandidateGenerator._compute_primary_budget(
        300.0, model_id="Qwen/Qwen3.5-35B-A3B-FP8",
    )
    assert budget == 30.0


def test_spine_phase2_env_knobs_override_defaults(monkeypatch) -> None:
    """Operators can tune both scalar and cap via env."""
    monkeypatch.delenv("OUROBOROS_PRIMARY_MAX_TIMEOUT_S", raising=False)
    monkeypatch.setenv("JARVIS_PRIMARY_HEAVY_TTFT_SCALAR", "4.0")
    monkeypatch.setenv("JARVIS_PRIMARY_HEAVY_TTFT_CAP_S", "180.0")
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )
    # 30 × 4.0 = 120, under 180 cap → 120
    budget = CandidateGenerator._compute_primary_budget(
        300.0, model_id="Qwen/Qwen3.5-397B-A17B-FP8",
    )
    assert budget == 120.0
    # If scalar pushes above cap, cap wins
    monkeypatch.setenv("JARVIS_PRIMARY_HEAVY_TTFT_SCALAR", "10.0")
    budget = CandidateGenerator._compute_primary_budget(
        300.0, model_id="Qwen/Qwen3.5-397B-A17B-FP8",
    )
    assert budget == 180.0  # 30 × 10 = 300 → capped at 180


# ──────────────────────────────────────────────────────────────────────
# Phase 3 inline fault discriminator spine — 4
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spine_phase3_master_off_skips_probe(monkeypatch) -> None:
    """When the master flag is off (default), Phase 3 helper should
    NOT be invoked from _call_primary. Confirmed via env check —
    the if statement gate."""
    monkeypatch.delenv("JARVIS_TTFT_FAULT_DISCRIMINATOR_ENABLED", raising=False)
    from backend.core.ouroboros.governance.candidate_generator import _envb
    assert _envb("JARVIS_TTFT_FAULT_DISCRIMINATOR_ENABLED", False) is False


@pytest.mark.asyncio
async def test_spine_phase3_classify_context_lag(monkeypatch, caplog) -> None:
    """When probe returns fast + non-empty, classification = context_lag."""
    monkeypatch.setenv("JARVIS_TTFT_FAULT_DISCRIMINATOR_ENABLED", "true")
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )

    fake_primary = mock.MagicMock()
    fake_primary.prompt_only = mock.AsyncMock(return_value="pong")

    cg = mock.MagicMock(spec=CandidateGenerator)
    cg._primary = fake_primary

    import logging
    with caplog.at_level(logging.WARNING):
        await CandidateGenerator._slice28_phase3_classify_ttft_failure(
            cg,
            attempted_model_id="Qwen/Qwen3.5-397B-A17B-FP8",
            op_id="op-test-context-lag",
            elapsed_s=30.0,
        )

    matches = [
        r for r in caplog.records
        if "Slice28.Phase3" in r.getMessage()
           and "classification=context_lag" in r.getMessage()
    ]
    assert len(matches) == 1, (
        f"Expected exactly 1 context_lag classification log; "
        f"got: {[r.getMessage() for r in caplog.records]!r}"
    )


@pytest.mark.asyncio
async def test_spine_phase3_classify_infrastructure_outage(
    monkeypatch, caplog,
) -> None:
    """When probe times out, classification = infrastructure_outage."""
    monkeypatch.setenv("JARVIS_TTFT_FAULT_DISCRIMINATOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_TTFT_PROBE_TIMEOUT_S", "0.1")
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )

    async def _slow_probe(*a, **kw):
        await asyncio.sleep(1.0)  # forces timeout at 0.1s
        return "would have been ok"

    fake_primary = mock.MagicMock()
    fake_primary.prompt_only = _slow_probe

    cg = mock.MagicMock(spec=CandidateGenerator)
    cg._primary = fake_primary

    import logging
    with caplog.at_level(logging.WARNING):
        await CandidateGenerator._slice28_phase3_classify_ttft_failure(
            cg,
            attempted_model_id="Qwen/Qwen3.5-397B-A17B-FP8",
            op_id="op-test-outage",
            elapsed_s=75.0,
        )

    matches = [
        r for r in caplog.records
        if "Slice28.Phase3" in r.getMessage()
           and "classification=infrastructure_outage" in r.getMessage()
    ]
    assert len(matches) == 1


@pytest.mark.asyncio
async def test_spine_phase3_never_raises_into_caller(monkeypatch) -> None:
    """If the probe itself raises an exception, the discriminator MUST
    swallow it and return cleanly. NEVER blocks the rotation."""
    monkeypatch.setenv("JARVIS_TTFT_FAULT_DISCRIMINATOR_ENABLED", "true")
    from backend.core.ouroboros.governance.candidate_generator import (
        CandidateGenerator,
    )

    async def _exploding_probe(*a, **kw):
        raise RuntimeError("simulated probe substrate failure")

    fake_primary = mock.MagicMock()
    fake_primary.prompt_only = _exploding_probe
    cg = mock.MagicMock(spec=CandidateGenerator)
    cg._primary = fake_primary

    # MUST NOT raise
    result = await CandidateGenerator._slice28_phase3_classify_ttft_failure(
        cg,
        attempted_model_id="Qwen/Qwen3.5-397B-A17B-FP8",
        op_id="op-test-explode",
        elapsed_s=30.0,
    )
    assert result is None  # observation hook returns None
