"""Every corpus row carries the output contract its prompt imposed.

2026-10-07: 1,044 of 1,215 training prompts asked for whole-file rewrites
(``full_content``) while O+V had moved to unified diffs (``2b.1-diff``) for
large files. GRPO re-generates from the prompt, so those prompts drove every
rollout into the 1,955-token completion window and the policy loss had no
tokens. Rows stored only the rendered prompt, so the contract could not be
recovered afterwards. Now the prompt builder records the contract it chose,
the recorder stamps it on the row, and the training handoff tells Reactor
which contracts are still current.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from backend.core.ouroboros.governance import providers as P
from backend.core.ouroboros.governance.observability import training_handoff as th
from backend.core.ouroboros.governance.observability import training_trigger as tt
from backend.core.ouroboros.governance.observability.trajectory_recorder import (
    reset_recorder_for_tests,
)
from backend.core.ouroboros.governance.op_context import OperationContext

ROOT = Path("/home/jarvis_svc/jarvis")


def _ctx(op_id, files=("backend/api/sse_contract.py",)):
    return OperationContext.create(target_files=tuple(files), description="d", op_id=op_id)


# ---------------------------------------------------------------------------
# The builder records the contract where it chooses it
# ---------------------------------------------------------------------------

def test_the_lean_builder_records_the_diff_contract(monkeypatch):
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "true")
    P._build_lean_codegen_prompt(_ctx("op-diff"), repo_root=ROOT, force_full_content=False)
    assert P.requested_output_schema("op-diff") == "2b.1-diff"


def test_the_lean_builder_records_full_content_when_forced(monkeypatch):
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "true")
    P._build_lean_codegen_prompt(_ctx("op-full"), repo_root=ROOT, force_full_content=True)
    assert P.requested_output_schema("op-full") == "2b.1"


def test_a_rebuilt_prompt_records_its_own_contract(monkeypatch):
    """The latest prompt of an op is the one the next generation answers."""
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "true")
    P._build_lean_codegen_prompt(_ctx("op-re"), repo_root=ROOT, force_full_content=False)
    P._build_lean_codegen_prompt(_ctx("op-re"), repo_root=ROOT, force_full_content=True)
    assert P.requested_output_schema("op-re") == "2b.1"


def test_an_unknown_op_has_no_contract():
    assert P.requested_output_schema("op-never-built") == ""


def test_the_record_is_bounded_by_the_pool(monkeypatch):
    monkeypatch.setenv("JARVIS_BG_POOL_SIZE", "1")
    bound = P._inflight_ops_max()
    for i in range(bound + 5):
        P._note_requested_schema(_ctx(f"op-b{i}"), "2b.1")
    assert P.requested_output_schema("op-b0") == ""                    # evicted
    assert P.requested_output_schema(f"op-b{bound + 4}") == "2b.1"     # newest kept
    assert len(P._REQUESTED_SCHEMA) <= bound


@pytest.mark.parametrize("armed, expected", [("true", ("2b.1-diff", "2b.1")), ("false", ("2b.1",))])
def test_current_single_file_contracts_follow_the_diff_switch(monkeypatch, armed, expected):
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", armed)
    assert P.single_file_output_schemas() == expected


# ---------------------------------------------------------------------------
# The recorder stamps it on every row
# ---------------------------------------------------------------------------

@dataclass
class _Result:
    candidates: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    provider_name: str = "local-prime"
    model_id: str = "qwen3-coder-ov:30b"
    is_noop: bool = False
    prompt_preloaded_files: Tuple[str, ...] = field(default_factory=tuple)
    total_input_tokens: int = 900
    total_output_tokens: int = 120
    cost_usd: float = 0.0


def _cand(cid):
    return {"candidate_id": cid, "candidate_hash": f"h-{cid}", "file_path": "a.py",
            "source_path": "a.py", "full_content": f"x = '{cid}'\n", "rationale": "r"}


@pytest.fixture()
def rec(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_ENABLED", "true")
    return reset_recorder_for_tests(path=tmp_path / "experience.jsonl")


def _rows(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_every_row_of_a_generation_carries_its_contract(rec):
    rec.record_generation(op_id="op-t", prompt="p", output_schema="2b.1-diff",
                          generation_result=_Result(candidates=(_cand("c1"), _cand("c2"))))
    rec.record_outcome(op_id="op-t", terminal_reason="applied")
    await rec.drain()
    rows = _rows(rec.path)
    assert len(rows) == 2 and {r["metadata"]["output_schema"] for r in rows} == {"2b.1-diff"}


@pytest.mark.asyncio
async def test_a_generation_with_no_recorded_contract_is_written_untagged(rec):
    rec.record_generation(op_id="op-u", prompt="p", generation_result=_Result(candidates=(_cand("c1"),)))
    rec.record_outcome(op_id="op-u", terminal_reason="applied")
    await rec.drain()
    assert _rows(rec.path)[0]["metadata"]["output_schema"] == ""


def test_both_recorder_seams_pass_the_requested_contract():
    """The local lane (PrimeProvider) and the paid lanes record through two
    separate seams; a seam without the tag writes untagged rows that the
    training filter then drops. Pin both."""
    import inspect

    src = inspect.getsource(P)
    seams = src.count("_traj_record_generation(")
    assert seams == 2
    assert src.count("output_schema=requested_output_schema(") == seams


# ---------------------------------------------------------------------------
# The handoff tells Reactor which contracts are current
# ---------------------------------------------------------------------------

@pytest.fixture
def cycle_env(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TRAINING_HANDOFF_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("JARVIS_TRAINING_HANDOFF_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("JARVIS_LOCAL_MODEL_NAME", "qwen3-coder-ov:30b")
    monkeypatch.setenv("TRINITY_GRPO_BASE_MODEL", "Qwen/Qwen3-Coder-30B-A3B-Instruct")
    monkeypatch.delenv("REACTOR_GRPO_OUTPUT_SCHEMAS", raising=False)
    monkeypatch.setattr(th, "_organism_live", lambda: None)
    monkeypatch.setattr(tt, "_preflight_cmd", lambda: ["preflight"])
    monkeypatch.setattr(tt, "_reactor_root", lambda: tmp_path / "reactor")

    async def met(model=None):
        return {"met": True, "unlearned": 20, "threshold": 15}
    monkeypatch.setattr(th, "training_yield", met)
    envs = {}

    async def run(cmd, *, timeout_s, cwd=None, env=None, log_path=None):
        name = "preflight" if cmd[0] == "preflight" else "train"
        envs[name] = dict(env or {})
        return (2, "{}") if name == "preflight" else (1, "")   # stop after preflight
    monkeypatch.setattr(tt, "_run", run)
    return envs


def test_preflight_is_told_the_current_contracts(cycle_env, monkeypatch):
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "true")
    out = asyncio.run(th.run_cycle(trigger="t"))
    assert out["state"] == "REFUSED"
    assert cycle_env["preflight"]["REACTOR_GRPO_OUTPUT_SCHEMAS"] == "2b.1-diff,2b.1"
    assert out["detail"]["preflight"]["output_schemas"] == "2b.1-diff,2b.1"


def test_an_operator_narrowing_wins(cycle_env, monkeypatch):
    monkeypatch.setenv("REACTOR_GRPO_OUTPUT_SCHEMAS", "2b.1-diff")
    asyncio.run(th.run_cycle(trigger="t"))
    assert cycle_env["preflight"]["REACTOR_GRPO_OUTPUT_SCHEMAS"] == "2b.1-diff"


def test_the_trainer_gets_the_same_contracts_as_the_gate():
    """Gate and run must count the same rows: one env, passed to both."""
    import inspect

    src = inspect.getsource(th._run)
    assert src.count("env=contracts") == 2


def test_the_policy_is_the_organisms_envelope_not_this_processs(cycle_env, monkeypatch):
    """The diff schema is armed by the production envelope every organism
    launcher hydrates -- not by .env. The handoff is not an organism, so
    reading its own environment would call diffs non-current and train on
    nothing but full content. Measured 2026-10-08: the exec .env has no
    JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED line at all."""
    import os
    monkeypatch.delenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", raising=False)
    asyncio.run(th.run_cycle(trigger="t"))
    assert cycle_env["preflight"]["REACTOR_GRPO_OUTPUT_SCHEMAS"] == "2b.1-diff,2b.1"
    assert "JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED" not in os.environ      # a copy was hydrated


def test_an_operator_disarming_the_diff_schema_is_respected(cycle_env, monkeypatch):
    monkeypatch.setenv("JARVIS_SINGLE_FILE_DIFF_SCHEMA_ENABLED", "false")
    asyncio.run(th.run_cycle(trigger="t"))
    assert cycle_env["preflight"]["REACTOR_GRPO_OUTPUT_SCHEMAS"] == "2b.1"
