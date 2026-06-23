"""tests/governance/test_dpo_synthesizer.py -- Phase 4a DPO Pair Synthesizer.

Covers the Epistemic Purity gate (cognitive-vs-infra), the golden-ratio
no-half-pairs gate, AST-symbol isolation + density cap, the reactor-core
DPOPair schema match, and the fire-and-forget / fail-soft / dedup / bounded
emit path. Uses fake trajectories (SimpleNamespace) -- NO real reactor-core,
GCS, or network.
"""
from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.dpo_synthesizer import (
    DPOPair,
    RepairTrajectory,
    classify_rejection,
    synthesize_pair,
    emit_dpo_pair,
    synthesize_and_emit,
    _INFLIGHT_TASKS,
)


# ---------------------------------------------------------------------------
# Source fixtures: a failed symbol and its repaired counterpart.
# ---------------------------------------------------------------------------

_FAILED_SRC = '''\
import os
import sys


UNRELATED = 1


def add(a, b):
    return a - b


def other_helper():
    return 99
'''

_REPAIRED_SRC = '''\
import os
import sys


UNRELATED = 1


def add(a, b):
    return a + b


def other_helper():
    return 99
'''


def _trajectory(
    *,
    resolved: bool = True,
    failed_src: str = _FAILED_SRC,
    repaired_src: str = _REPAIRED_SRC,
    stderr: str = "E   assert 1 == 2\nE   AssertionError",
    failure_source: str | None = None,
    fsm_state: str | None = None,
    prompt: str = "Fix the add function so it returns the sum",
    changed_symbol_hint: str = "add",
) -> RepairTrajectory:
    return RepairTrajectory(
        prompt=prompt,
        failed_candidate_src=failed_src,
        repaired_candidate_src=repaired_src if resolved else None,
        resolved=resolved,
        stderr=stderr,
        failure_source=failure_source,
        fsm_state=fsm_state,
        failure_signature_hash="sig-abc123",
        task_type="l2_repair",
        changed_symbol_hint=changed_symbol_hint,
        provider="doubleword",
    )


# ===========================================================================
# (a) classify_rejection: cognitive KEEP vs infra DROP, unknown -> infra
# ===========================================================================

@pytest.mark.parametrize("stderr", [
    "E   assert result == 3\nE   AssertionError",
    "  File 'x.py', line 2\n    def add(\n          ^\nSyntaxError: invalid syntax",
    "NameError: name 'foo' is not defined",
    "TypeError: unsupported operand type(s)",
    "ValueError: math domain error",
])
def test_classify_cognitive_keep(stderr):
    assert classify_rejection(stderr=stderr, failure_source=None, fsm_state=None) == "cognitive"


@pytest.mark.parametrize("failure_source", [
    "fsm_exhausted",
    "live_transport",
    "live_http_5xx",
    "generation_timeout",
    "local_egress_overweight",
])
def test_classify_infra_by_failure_source(failure_source):
    # Even with an assertion-shaped stderr, an infra failure_source must DROP.
    assert classify_rejection(
        stderr="E   AssertionError",
        failure_source=failure_source,
        fsm_state=None,
    ) == "infra"


@pytest.mark.parametrize("stderr", [
    "aiohttp.ClientConnectorError: connection timeout to upstream",
    "HTTP 503 Service Unavailable from provider",
    "Aegis forward timeout after 60s",
    "asyncio.TimeoutError",
    "all_providers_exhausted:fallback_skipped",
    "lane collapse: batch and realtime both timed out",
])
def test_classify_infra_by_stderr_shape(stderr):
    assert classify_rejection(stderr=stderr, failure_source=None, fsm_state=None) == "infra"


def test_classify_infra_by_fsm_state():
    assert classify_rejection(
        stderr="something ambiguous",
        failure_source=None,
        fsm_state="fsm_exhausted",
    ) == "infra"


def test_classify_unknown_drops_as_infra():
    # Cannot confidently classify as cognitive -> fail-safe to infra (DROP).
    assert classify_rejection(
        stderr="totally opaque noise blah blah",
        failure_source=None,
        fsm_state=None,
    ) == "infra"


def test_classify_never_raises_on_garbage():
    assert classify_rejection(stderr=None, failure_source=None, fsm_state=None) == "infra"  # type: ignore[arg-type]


# ===========================================================================
# (b) golden-ratio: resolved -> pair; yielded (no chosen) -> None
# ===========================================================================

def test_resolved_trajectory_yields_pair():
    pair = synthesize_pair(_trajectory(resolved=True))
    assert pair is not None
    assert isinstance(pair, DPOPair)


def test_yielded_trajectory_returns_none():
    # No repaired/chosen candidate (UNRESOLVABLE PATH / pivot) -> drop whole pair.
    assert synthesize_pair(_trajectory(resolved=False, repaired_src=None)) is None


def test_missing_failed_candidate_returns_none():
    traj = _trajectory()
    traj = RepairTrajectory(
        prompt=traj.prompt,
        failed_candidate_src="",
        repaired_candidate_src=traj.repaired_candidate_src,
        resolved=True,
        stderr=traj.stderr,
        failure_source=None,
        fsm_state=None,
    )
    assert synthesize_pair(traj) is None


def test_identical_candidates_returns_none():
    # No actual change between rejected and chosen -> nothing to learn.
    assert synthesize_pair(_trajectory(repaired_src=_FAILED_SRC)) is None


# ===========================================================================
# (c) purity: infra-caused rejection -> None even with a chosen present
# ===========================================================================

def test_infra_rejection_dropped_despite_chosen():
    traj = _trajectory(resolved=True, failure_source="live_transport")
    assert synthesize_pair(traj) is None


def test_fsm_exhausted_rejection_dropped():
    traj = _trajectory(resolved=True, stderr="all_providers_exhausted:no_fallback_configured")
    assert synthesize_pair(traj) is None


# ===========================================================================
# (d) AST isolation: only the changed symbol, imports stripped, density cap
# ===========================================================================

def test_isolation_extracts_only_changed_symbol():
    pair = synthesize_pair(_trajectory())
    assert pair is not None
    # The changed symbol is `add`. Its bodies must be present...
    assert "def add" in pair.rejected
    assert "def add" in pair.chosen
    assert "return a - b" in pair.rejected
    assert "return a + b" in pair.chosen
    # ...and unrelated symbols / imports must be stripped.
    assert "import os" not in pair.rejected
    assert "import sys" not in pair.chosen
    assert "other_helper" not in pair.rejected
    assert "UNRELATED" not in pair.chosen


def test_density_cap_drops_irreducible_oversize():
    big_body = "    x = 'A' * 10\n" * 5000  # irreducibly huge symbol body
    failed = f"def add(a, b):\n{big_body}    return a - b\n"
    repaired = f"def add(a, b):\n{big_body}    return a + b\n"
    traj = _trajectory(failed_src=failed, repaired_src=repaired)
    os.environ["JARVIS_DPO_PAIR_MAX_CHARS"] = "2048"
    try:
        assert synthesize_pair(traj) is None
    finally:
        os.environ.pop("JARVIS_DPO_PAIR_MAX_CHARS", None)


def test_isolation_failure_returns_none():
    # Unparseable source -> cannot isolate -> drop (no raw dump).
    traj = _trajectory(failed_src="def add( <<<broken", repaired_src="def add( <<<broken2")
    assert synthesize_pair(traj) is None


# ===========================================================================
# (e) DPOPair shape matches reactor-core's schema
# ===========================================================================

def test_dpopair_shape_matches_reactor_schema():
    pair = synthesize_pair(_trajectory())
    assert pair is not None
    d = pair.to_dict()
    # reactor-core dpo_pair_generator.DPOPair.to_dict keys
    for key in (
        "prompt", "chosen", "rejected", "chosen_model", "rejected_model",
        "chosen_score", "rejected_score", "task_type", "generation_method",
        "metadata",
    ):
        assert key in d, f"missing key {key}"
    assert d["metadata"]["source"] == "ouroboros_epistemic_repair"
    assert d["metadata"]["signature"] == "sig-abc123"
    assert d["task_type"] == "l2_repair"


# ===========================================================================
# (f) emit: fire-and-forget non-blocking, fail-soft, dedup, bounded, OFF no-op
# ===========================================================================

def _ring_path(tmp_path):
    return str(tmp_path / "dpo_dataset.jsonl")


def test_emit_appends_to_ring(tmp_path, monkeypatch):
    path = _ring_path(tmp_path)
    monkeypatch.setenv("JARVIS_DPO_DATASET_PATH", path)
    pair = synthesize_pair(_trajectory())
    assert pair is not None

    async def _run():
        emit_dpo_pair(pair)
        await asyncio.gather(*list(_INFLIGHT_TASKS)) if _INFLIGHT_TASKS else None

    asyncio.run(_run())
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["metadata"]["source"] == "ouroboros_epistemic_repair"


def test_emit_dedup_content_hash(tmp_path, monkeypatch):
    path = _ring_path(tmp_path)
    monkeypatch.setenv("JARVIS_DPO_DATASET_PATH", path)
    pair = synthesize_pair(_trajectory())

    async def _run():
        for _ in range(3):
            emit_dpo_pair(pair)
            if _INFLIGHT_TASKS:
                await asyncio.gather(*list(_INFLIGHT_TASKS))

    asyncio.run(_run())
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip()]
    assert len(lines) == 1  # dedup -> only one


def test_emit_bounded_ring(tmp_path, monkeypatch):
    path = _ring_path(tmp_path)
    monkeypatch.setenv("JARVIS_DPO_DATASET_PATH", path)
    monkeypatch.setenv("JARVIS_DPO_DATASET_MAX", "3")

    async def _run():
        for i in range(6):
            # Distinct prompts -> distinct content hashes (no dedup collapse).
            pair = synthesize_pair(_trajectory(prompt=f"fix add variant {i}"))
            assert pair is not None
            emit_dpo_pair(pair)
            if _INFLIGHT_TASKS:
                await asyncio.gather(*list(_INFLIGHT_TASKS))

    asyncio.run(_run())
    with open(path, encoding="utf-8") as fh:
        lines = [ln for ln in fh if ln.strip()]
    assert len(lines) == 3  # bounded


def test_emit_no_running_loop_is_noop(tmp_path, monkeypatch):
    path = _ring_path(tmp_path)
    monkeypatch.setenv("JARVIS_DPO_DATASET_PATH", path)
    pair = synthesize_pair(_trajectory())
    # No running loop -> no-op, never raises.
    emit_dpo_pair(pair)
    assert not os.path.exists(path)


def test_emit_failsoft_on_bad_path(monkeypatch):
    # An unwritable path must never raise into the caller.
    monkeypatch.setenv("JARVIS_DPO_DATASET_PATH", "/proc/cannot/write/here.jsonl")
    pair = synthesize_pair(_trajectory())

    async def _run():
        emit_dpo_pair(pair)  # must not raise
        if _INFLIGHT_TASKS:
            await asyncio.gather(*list(_INFLIGHT_TASKS))

    asyncio.run(_run())  # no exception escapes


def test_synthesis_disabled_off_noop(tmp_path, monkeypatch):
    path = _ring_path(tmp_path)
    monkeypatch.setenv("JARVIS_DPO_DATASET_PATH", path)
    monkeypatch.setenv("JARVIS_DPO_SYNTHESIS_ENABLED", "false")

    async def _run():
        emitted = synthesize_and_emit(_trajectory())
        if _INFLIGHT_TASKS:
            await asyncio.gather(*list(_INFLIGHT_TASKS))
        return emitted

    emitted = asyncio.run(_run())
    assert emitted is False
    assert not os.path.exists(path)


# ===========================================================================
# (g) synthesize_and_emit only on resolved repairs, fail-soft
# ===========================================================================

def test_synthesize_and_emit_resolved_true(tmp_path, monkeypatch):
    path = _ring_path(tmp_path)
    monkeypatch.setenv("JARVIS_DPO_DATASET_PATH", path)

    async def _run():
        emitted = synthesize_and_emit(_trajectory(resolved=True))
        if _INFLIGHT_TASKS:
            await asyncio.gather(*list(_INFLIGHT_TASKS))
        return emitted

    assert asyncio.run(_run()) is True
    assert os.path.exists(path)


def test_synthesize_and_emit_yielded_false(tmp_path, monkeypatch):
    path = _ring_path(tmp_path)
    monkeypatch.setenv("JARVIS_DPO_DATASET_PATH", path)

    async def _run():
        emitted = synthesize_and_emit(_trajectory(resolved=False, repaired_src=None))
        if _INFLIGHT_TASKS:
            await asyncio.gather(*list(_INFLIGHT_TASKS))
        return emitted

    assert asyncio.run(_run()) is False
    assert not os.path.exists(path)


def test_synthesize_and_emit_never_raises():
    # Pass garbage; must fail-soft to False, never raise.
    assert synthesize_and_emit(SimpleNamespace()) is False  # type: ignore[arg-type]


# ===========================================================================
# from_repair adapter: extracts trajectory from live ctx/result objects
# ===========================================================================

def test_from_repair_extracts_resolved():
    ctx = SimpleNamespace(
        op_id="op-1",
        generation=SimpleNamespace(candidates=[{"full_content": _FAILED_SRC, "file_path": "m.py"}]),
        signal=SimpleNamespace(description="Fix add"),
    )
    result = SimpleNamespace(
        terminal="L2_CONVERGED",
        candidate={"full_content": _REPAIRED_SRC, "file_path": "m.py"},
        iterations=(SimpleNamespace(failure_class="test", provider_name="doubleword"),),
        summary={},
        failure_signature_hash="sig-xyz",
        stderr_tail="E   AssertionError",
        stop_reason=None,
    )
    traj = RepairTrajectory.from_repair(ctx, result)
    assert traj is not None
    assert traj.resolved is True
    assert traj.failed_candidate_src == _FAILED_SRC
    assert traj.repaired_candidate_src == _REPAIRED_SRC
    pair = synthesize_pair(traj)
    assert pair is not None


def test_from_repair_non_converged_returns_none():
    ctx = SimpleNamespace(op_id="op-2", generation=None, signal=None)
    result = SimpleNamespace(terminal="L2_PIVOT", candidate=None, iterations=(), summary={})
    assert RepairTrajectory.from_repair(ctx, result) is None
