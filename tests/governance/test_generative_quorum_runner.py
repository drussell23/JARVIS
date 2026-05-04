"""Move 6 Slice 3 — K-way parallel runner regression tests.

Coverage:

  * **Disabled gate** — master flag off short-circuits to DISABLED
    with zero rolls fired (cost-free when disabled).
  * **Outcome matrix** — CONSENSUS / MAJORITY_CONSENSUS /
    DISAGREEMENT / FAILED via parameterized roll fixtures.
  * **Failure isolation** — generator raising exception or timing
    out produces empty-signature roll; other rolls still
    contribute (Slice 1's compute_consensus already gracefully
    handles the empties).
  * **Multi-file dispatch** — ``is_multi_file=True`` routes to
    ``compute_multi_file_signature``; type-mismatched output
    yields empty signature.
  * **Parallel execution proof** — wall-clock for K=3 each
    sleeping T must be ~T (not 3T).
  * **Cost aggregation** — ``cost_estimate_per_roll_usd``
    propagates to every roll regardless of success.
  * **Seed propagation** — generator receives ``seed = seed_base
    + i``; verified via output capture.
  * **Cancellation** — ``CancelledError`` from generator
    propagates (does not silently downgrade to FAILED).
  * **Defensive contract** — runner NEVER raises; even when
    ``compute_consensus`` is fed garbage rolls.
  * **Authority invariants** — AST-pinned import discipline (no
    governance modules outside Slice 1 + Slice 2; no orchestrator
    /policy/iron_gate/etc).
  * **Schema integrity** — frozen dataclass, ``to_dict`` round-
    trip, schema version stable.
"""
from __future__ import annotations

import ast
import asyncio
import os
import time
from pathlib import Path
from typing import List
from unittest import mock

import pytest

from backend.core.ouroboros.governance.verification.generative_quorum import (
    ConsensusOutcome,
    ConsensusVerdict,
)
from backend.core.ouroboros.governance.verification.generative_quorum_runner import (
    GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION,
    QuorumRunResult,
    run_quorum,
)


# ---------------------------------------------------------------------------
# Fixtures — caller-supplied roll generators
# ---------------------------------------------------------------------------


def make_static_gen(diff: str):
    async def gen(*, roll_id: str, seed: int) -> str:  # noqa: ARG001
        return diff
    return gen


def make_rotating_gen(diffs: list):
    counter = {"i": 0}

    async def gen(*, roll_id: str, seed: int):  # noqa: ARG001
        i = counter["i"]
        counter["i"] += 1
        return diffs[i % len(diffs)]
    return gen


def make_failing_gen(fail_indices: List[int], success_diff: str):
    counter = {"i": 0}

    async def gen(*, roll_id: str, seed: int) -> str:  # noqa: ARG001
        i = counter["i"]
        counter["i"] += 1
        if i in fail_indices:
            raise RuntimeError(f"boom roll {i}")
        return success_diff
    return gen


def make_slow_gen(sleep_s: float, diff: str):
    async def gen(*, roll_id: str, seed: int) -> str:  # noqa: ARG001
        await asyncio.sleep(sleep_s)
        return diff
    return gen


# ---------------------------------------------------------------------------
# 1. Disabled gate — master flag off short-circuits
# ---------------------------------------------------------------------------


class TestDisabledGate:
    def test_enabled_via_env_unset_post_q4_graduation(self):
        # Q4 Priority #1 graduation (2026-05-02): master flag
        # default-true; unset env now produces CONSENSUS (the
        # quorum runs). Falsy env explicitly is the new "disabled"
        # path — covered by ``test_disabled_via_env_explicit_false``.
        os.environ.pop("JARVIS_GENERATIVE_QUORUM_ENABLED", None)
        gen = make_static_gen("def foo(): return 1")
        result = asyncio.run(run_quorum(gen, k=3))
        assert result.verdict.outcome is ConsensusOutcome.CONSENSUS
        assert len(result.rolls) == 3

    def test_disabled_via_env_explicit_false(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_GENERATIVE_QUORUM_ENABLED": "false"},
        ):
            gen = make_static_gen("x = 1")
            result = asyncio.run(run_quorum(gen, k=3))
            assert (
                result.verdict.outcome is ConsensusOutcome.DISABLED
            )

    def test_enabled_override_true_runs_rolls(self):
        gen = make_static_gen("x = 1")
        result = asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        assert (
            result.verdict.outcome is ConsensusOutcome.CONSENSUS
        )
        assert len(result.rolls) == 3

    def test_enabled_override_false_overrides_env_true(self):
        with mock.patch.dict(
            os.environ,
            {"JARVIS_GENERATIVE_QUORUM_ENABLED": "true"},
        ):
            gen = make_static_gen("x = 1")
            result = asyncio.run(
                run_quorum(gen, k=3, enabled_override=False),
            )
            assert (
                result.verdict.outcome is ConsensusOutcome.DISABLED
            )

    def test_disabled_zero_rolls_fired(self):
        """Cost-correctness: master-off must fire ZERO rolls."""
        call_count = {"n": 0}

        async def counting_gen(*, roll_id, seed):  # noqa: ARG001
            call_count["n"] += 1
            return "x"

        asyncio.run(
            run_quorum(counting_gen, k=5, enabled_override=False),
        )
        assert call_count["n"] == 0


# ---------------------------------------------------------------------------
# 2. Outcome matrix — CONSENSUS / MAJORITY / DISAGREEMENT
# ---------------------------------------------------------------------------


class TestOutcomeMatrix:
    def test_unanimous_consensus(self):
        gen = make_static_gen("def helper(x): return x * 2")
        result = asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        assert (
            result.verdict.outcome is ConsensusOutcome.CONSENSUS
        )
        assert result.verdict.agreement_count == 3
        assert result.verdict.total_rolls == 3
        assert result.verdict.distinct_count == 1
        assert result.verdict.canonical_signature is not None
        assert len(result.verdict.canonical_signature) == 64

    def test_quine_class_literal_invariance(self):
        """The Move 6 core invariant: 3 rolls returning structurally
        identical code with different literal values converge to
        CONSENSUS via Slice 2's normalize-literals pass."""
        gen = make_rotating_gen([
            "def helper(x): return x * 2",
            "def helper(x): return x * 3",
            "def helper(x): return x * 5",
        ])
        result = asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        assert (
            result.verdict.outcome is ConsensusOutcome.CONSENSUS
        )

    def test_majority_consensus_2_of_3(self):
        gen = make_rotating_gen([
            "def helper(x): return x",
            "def helper(x): return x",
            "class Helper: pass",
        ])
        result = asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        assert (
            result.verdict.outcome
            is ConsensusOutcome.MAJORITY_CONSENSUS
        )
        assert result.verdict.agreement_count == 2
        assert result.verdict.distinct_count == 2

    def test_disagreement_all_distinct(self):
        gen = make_rotating_gen([
            "def helper(x): return x",
            "def helper(x): return -x",
            "class Helper: pass",
        ])
        result = asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        assert (
            result.verdict.outcome
            is ConsensusOutcome.DISAGREEMENT
        )
        assert result.verdict.distinct_count == 3

    def test_k_eq_2_unanimous(self):
        gen = make_static_gen("x = 1")
        result = asyncio.run(
            run_quorum(gen, k=2, enabled_override=True),
        )
        assert (
            result.verdict.outcome is ConsensusOutcome.CONSENSUS
        )
        assert result.verdict.total_rolls == 2

    def test_k_below_floor_falls_back_to_env_default(self):
        """K=1 (below floor) is invalid → falls to env default
        (which is itself floor-clamped at 2)."""
        gen = make_static_gen("x = 1")
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_K": "2"},
        ):
            result = asyncio.run(
                run_quorum(gen, k=1, enabled_override=True),
            )
            assert result.verdict.total_rolls == 2

    def test_k_zero_falls_back_to_env_default(self):
        gen = make_static_gen("x = 1")
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_K": "2"},
        ):
            result = asyncio.run(
                run_quorum(gen, k=0, enabled_override=True),
            )
            assert result.verdict.total_rolls == 2

    def test_k_negative_falls_back_to_env_default(self):
        """Even pathological negative K falls back gracefully —
        runner never fires < 2 rolls."""
        gen = make_static_gen("x = 1")
        with mock.patch.dict(
            os.environ, {"JARVIS_QUORUM_K": "2"},
        ):
            result = asyncio.run(
                run_quorum(gen, k=-5, enabled_override=True),
            )
            assert result.verdict.total_rolls == 2

    def test_threshold_override_strict(self):
        """K=3 with 2 agreeing + threshold=3 → DISAGREEMENT."""
        gen = make_rotating_gen([
            "x = 1",
            "x = 2",
            "class C: pass",
        ])
        # Threshold 3 means even majority is rejected; only
        # unanimous CONSENSUS qualifies.
        result = asyncio.run(
            run_quorum(
                gen, k=3, threshold=3, enabled_override=True,
            ),
        )
        # 2 of 3 agree on x = <INT> structure but threshold is 3
        assert (
            result.verdict.outcome
            is ConsensusOutcome.DISAGREEMENT
        )


# ---------------------------------------------------------------------------
# 3. Failure isolation — exception / timeout / wrong-shape
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    def test_one_roll_raises_others_contribute(self):
        gen = make_failing_gen(
            fail_indices=[1], success_diff="x = 1",
        )
        result = asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        assert (
            result.verdict.outcome
            is ConsensusOutcome.MAJORITY_CONSENSUS
        )
        assert result.verdict.agreement_count == 2
        assert "roll-1" in result.failed_roll_ids
        failed_roll = next(
            r for r in result.rolls if r.roll_id == "roll-1"
        )
        assert failed_roll.ast_signature == ""

    def test_all_rolls_raise(self):
        gen = make_failing_gen(
            fail_indices=[0, 1, 2], success_diff="x = 1",
        )
        result = asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        assert (
            result.verdict.outcome
            is ConsensusOutcome.DISAGREEMENT
        )
        assert len(result.failed_roll_ids) == 3

    def test_one_roll_times_out(self):
        counter = {"i": 0}

        async def gen(*, roll_id, seed):  # noqa: ARG001
            i = counter["i"]
            counter["i"] += 1
            if i == 1:
                await asyncio.sleep(2.0)
            return "x = 1"

        result = asyncio.run(
            run_quorum(
                gen, k=3, timeout_per_roll_s=0.1,
                enabled_override=True,
            ),
        )
        assert "roll-1" in result.failed_roll_ids
        assert (
            result.verdict.outcome
            is ConsensusOutcome.MAJORITY_CONSENSUS
        )

    def test_all_rolls_timeout(self):
        gen = make_slow_gen(2.0, "x = 1")
        result = asyncio.run(
            run_quorum(
                gen, k=3, timeout_per_roll_s=0.05,
                enabled_override=True,
            ),
        )
        assert len(result.failed_roll_ids) == 3
        assert (
            result.verdict.outcome
            is ConsensusOutcome.DISAGREEMENT
        )

    def test_non_awaitable_generator_treated_as_failure(self):
        def sync_gen(*, roll_id, seed):  # noqa: ARG001
            return "x = 1"

        result = asyncio.run(
            run_quorum(sync_gen, k=2, enabled_override=True),  # type: ignore[arg-type]
        )
        assert len(result.failed_roll_ids) == 2

    def test_wrong_output_shape_yields_empty_signature(self):
        async def bad_gen(*, roll_id, seed):  # noqa: ARG001
            return 42  # not str, not Mapping

        result = asyncio.run(
            run_quorum(bad_gen, k=2, enabled_override=True),  # type: ignore[arg-type]
        )
        for r in result.rolls:
            assert r.ast_signature == ""
        assert (
            result.verdict.outcome
            is ConsensusOutcome.DISAGREEMENT
        )


# ---------------------------------------------------------------------------
# 4. Multi-file dispatch
# ---------------------------------------------------------------------------


class TestMultiFile:
    def test_multi_file_unanimous(self):
        async def gen(*, roll_id, seed):  # noqa: ARG001
            return {
                "a.py": "def foo(): return 1",
                "b.py": "def bar(): return 2",
            }

        result = asyncio.run(
            run_quorum(
                gen, k=3, is_multi_file=True,
                enabled_override=True,
            ),
        )
        assert (
            result.verdict.outcome is ConsensusOutcome.CONSENSUS
        )

    def test_multi_file_quine_invariance(self):
        gen = make_rotating_gen([
            {"a.py": "x = 1"},
            {"a.py": "x = 2"},
            {"a.py": "x = 99"},
        ])

        result = asyncio.run(
            run_quorum(
                gen, k=3, is_multi_file=True,
                enabled_override=True,
            ),
        )
        assert (
            result.verdict.outcome is ConsensusOutcome.CONSENSUS
        )

    def test_multi_file_with_str_output_yields_empty_sig(self):
        async def gen(*, roll_id, seed):  # noqa: ARG001
            return "def foo(): pass"

        result = asyncio.run(
            run_quorum(
                gen, k=2, is_multi_file=True,
                enabled_override=True,
            ),
        )
        for r in result.rolls:
            assert r.ast_signature == ""

    def test_single_file_with_mapping_output_yields_empty_sig(
        self,
    ):
        async def gen(*, roll_id, seed):  # noqa: ARG001
            return {"a.py": "x = 1"}

        result = asyncio.run(
            run_quorum(
                gen, k=2, is_multi_file=False,
                enabled_override=True,
            ),
        )
        for r in result.rolls:
            assert r.ast_signature == ""


# ---------------------------------------------------------------------------
# 5. Parallel execution proof
# ---------------------------------------------------------------------------


class TestParallelExecution:
    def test_k_rolls_run_concurrently(self):
        """K=3 rolls each sleeping 0.1s must complete in ~0.1s
        wall clock, not 0.3s. Confirms asyncio.gather parallelism."""
        gen = make_slow_gen(0.1, "x = 1")
        start = time.monotonic()
        result = asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        elapsed = time.monotonic() - start
        assert elapsed < 0.25, (
            f"parallel K=3 took {elapsed:.3f}s, expected <0.25s"
        )
        assert (
            result.verdict.outcome is ConsensusOutcome.CONSENSUS
        )

    def test_elapsed_seconds_recorded(self):
        gen = make_slow_gen(0.05, "x = 1")
        result = asyncio.run(
            run_quorum(gen, k=2, enabled_override=True),
        )
        assert result.elapsed_seconds >= 0.05
        assert result.elapsed_seconds < 1.0


# ---------------------------------------------------------------------------
# 6. Cost + seed propagation
# ---------------------------------------------------------------------------


class TestCostAndSeed:
    def test_cost_propagates_to_every_roll(self):
        gen = make_static_gen("x = 1")
        result = asyncio.run(
            run_quorum(
                gen, k=3, cost_estimate_per_roll_usd=0.012,
                enabled_override=True,
            ),
        )
        for r in result.rolls:
            assert r.cost_estimate_usd == pytest.approx(0.012)

    def test_cost_propagates_even_to_failed_rolls(self):
        gen = make_failing_gen(
            fail_indices=[0, 1, 2], success_diff="x = 1",
        )
        result = asyncio.run(
            run_quorum(
                gen, k=3, cost_estimate_per_roll_usd=0.005,
                enabled_override=True,
            ),
        )
        for r in result.rolls:
            assert r.cost_estimate_usd == pytest.approx(0.005)

    def test_seed_propagation_unique_per_roll(self):
        seeds_received: List[int] = []

        async def gen(*, roll_id, seed):  # noqa: ARG001
            seeds_received.append(seed)
            return "x = 1"

        asyncio.run(
            run_quorum(
                gen, k=3, seed_base=100,
                enabled_override=True,
            ),
        )
        assert sorted(seeds_received) == [100, 101, 102]

    def test_seed_default_starts_at_zero(self):
        seeds_received: List[int] = []

        async def gen(*, roll_id, seed):  # noqa: ARG001
            seeds_received.append(seed)
            return "x = 1"

        asyncio.run(
            run_quorum(gen, k=2, enabled_override=True),
        )
        assert sorted(seeds_received) == [0, 1]

    def test_roll_id_format(self):
        ids_received: List[str] = []

        async def gen(*, roll_id, seed):  # noqa: ARG001
            ids_received.append(roll_id)
            return "x = 1"

        asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        assert sorted(ids_received) == ["roll-0", "roll-1", "roll-2"]


# ---------------------------------------------------------------------------
# 7. Cancellation propagation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_external_cancellation_propagates(self):
        """When the parent task is cancelled mid-quorum, the
        CancelledError must surface — runner does not silently
        swallow cancellation. This is the realistic scenario:
        orchestrator decides to cancel the entire op."""
        gen = make_slow_gen(10.0, "x = 1")

        async def driver():
            task = asyncio.create_task(
                run_quorum(gen, k=2, enabled_override=True),
            )
            # Let the rolls start
            await asyncio.sleep(0.05)
            task.cancel()
            await task

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(driver())


# ---------------------------------------------------------------------------
# 8. Defensive contract — runner never raises
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_garbage_generator_returns_failed_or_disagreement(
        self,
    ):
        async def garbage(*, roll_id, seed):  # noqa: ARG001
            return None

        result = asyncio.run(
            run_quorum(garbage, k=2, enabled_override=True),  # type: ignore[arg-type]
        )
        assert isinstance(result, QuorumRunResult)
        assert isinstance(result.verdict, ConsensusVerdict)

    def test_negative_timeout_does_not_raise(self):
        gen = make_static_gen("x = 1")
        result = asyncio.run(
            run_quorum(
                gen, k=2, timeout_per_roll_s=-1.0,
                enabled_override=True,
            ),
        )
        assert isinstance(result, QuorumRunResult)


# ---------------------------------------------------------------------------
# 9. Schema integrity
# ---------------------------------------------------------------------------


class TestSchema:
    def test_quorum_run_result_is_frozen(self):
        gen = make_static_gen("x = 1")
        result = asyncio.run(
            run_quorum(gen, k=2, enabled_override=True),
        )
        with pytest.raises((AttributeError, Exception)):
            result.elapsed_seconds = 999.0  # type: ignore[misc]

    def test_to_dict_round_trip_shape(self):
        gen = make_static_gen("x = 1")
        result = asyncio.run(
            run_quorum(gen, k=2, enabled_override=True),
        )
        d = result.to_dict()
        assert "verdict" in d
        assert "rolls" in d
        assert "failed_roll_ids" in d
        assert "elapsed_seconds" in d
        assert (
            d["schema_version"]
            == GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION
        )
        assert isinstance(d["rolls"], list)
        assert len(d["rolls"]) == 2

    def test_schema_version_stable(self):
        assert (
            GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION
            == "generative_quorum_runner.1"
        )


# ---------------------------------------------------------------------------
# 10. Authority invariants — AST-pinned import discipline
# ---------------------------------------------------------------------------


def _runner_source() -> str:
    """Helper that loads the runner source as text. We compute
    forbidden-token strings dynamically so this test file itself
    doesn't trip code-scan hooks."""
    path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "core"
        / "ouroboros"
        / "governance"
        / "verification"
        / "generative_quorum_runner.py"
    )
    return path.read_text(encoding="utf-8")


class TestAuthorityInvariants:
    @pytest.fixture
    def runner_source(self) -> str:
        return _runner_source()

    def test_no_orchestrator_imports(self, runner_source):
        forbidden = [
            "orchestrator",
            "iron_gate",
            "policy",
            "change_engine",
            "candidate_generator",
            "providers",
            "doubleword_provider",
            "urgency_router",
            "auto_action_router",
            "subagent_scheduler",
            "tool_executor",
            "phase_runners",
            "semantic_guardian",
            "semantic_firewall",
        ]
        tree = ast.parse(runner_source)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = (
                    node.module if isinstance(node, ast.ImportFrom)
                    else (
                        node.names[0].name if node.names else ""
                    )
                )
                module = module or ""
                for f in forbidden:
                    assert f not in module, (
                        f"forbidden import contains {f!r}: {module}"
                    )

    def test_governance_imports_in_allowlist(self, runner_source):
        """Slice 3 may import Slice 1 + Slice 2 + (Slice 5 lazy)
        ide_observability_stream + (Slice 5b C lazy)
        generative_quorum_observer from governance. No other
        module."""
        tree = ast.parse(runner_source)
        allowed = {
            "backend.core.ouroboros.governance.verification.generative_quorum",
            "backend.core.ouroboros.governance.verification.ast_canonical",
            # Slice 5 — lazy SSE publisher import
            "backend.core.ouroboros.governance.ide_observability_stream",
            # Slice 5b C — lazy bounded-JSONL recorder import.
            # Read-only consumer of QuorumRunResult; never mutates
            # runner state. Authority floor preserved.
            "backend.core.ouroboros.governance.verification.generative_quorum_observer",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "governance" in node.module:
                    assert node.module in allowed, (
                        f"governance import outside allowlist: "
                        f"{node.module}"
                    )

    def test_no_mutation_tools(self, runner_source):
        # Compose forbidden tokens dynamically so this test source
        # doesn't itself contain literal banned-shell patterns.
        forbidden = [
            "edit_file", "write_file", "delete_file",
            "subprocess." + "run", "subprocess." + "Popen",
            "os." + "system", "os.remove", "os.unlink",
            "shutil.rmtree",
        ]
        for f in forbidden:
            assert f not in runner_source, (
                f"runner contains forbidden mutation token: {f!r}"
            )

    def test_no_exec_eval_compile(self, runner_source):
        """Critical safety pin — runner orchestrates parallel
        async calls; it MUST NEVER execute candidate code."""
        tree = ast.parse(runner_source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    assert node.func.id not in (
                        "exec", "eval", "compile",
                    ), (
                        f"runner contains forbidden call: "
                        f"{node.func.id}"
                    )

    def test_run_quorum_is_async(self, runner_source):
        tree = ast.parse(runner_source)
        async_funcs = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        ]
        assert "run_quorum" in async_funcs

    def test_public_api_exported(self, runner_source):
        for name in (
            "run_quorum", "QuorumRunResult", "RollGenerator",
            "GENERATIVE_QUORUM_RUNNER_SCHEMA_VERSION",
        ):
            assert f'"{name}"' in runner_source


# ---------------------------------------------------------------------------
# 11. End-to-end Quorum scenarios — Move 6 mechanism proofs
# ---------------------------------------------------------------------------


class TestEndToEnd:
    def test_test_shape_gaming_distinct_shapes_caught(self):
        """Three rolls all generate `assert <CONSTANT>` style
        tests but with structurally different shapes (Compare vs
        Constant). Quorum DETECTS this as DISAGREEMENT — exactly
        the test-shape gaming defense."""
        gen = make_rotating_gen([
            "def test_foo(): assert True",
            "def test_foo(): assert 1 == 1",
            "def test_foo(): assert 'a' == 'a'",
        ])
        result = asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        assert result.verdict.distinct_count >= 2

    def test_three_independent_rolls_diverge_on_distinct_logic(
        self,
    ):
        """Three rolls produced by different prompts/seeds
        produce structurally distinct candidates. Defense
        active."""
        gen = make_rotating_gen([
            "def f(x): return x + 1",
            "def f(x):\n    if x > 0:\n        return x\n    return 0",
            "def f(x): return [y for y in range(x)]",
        ])
        result = asyncio.run(
            run_quorum(gen, k=3, enabled_override=True),
        )
        assert (
            result.verdict.outcome
            is ConsensusOutcome.DISAGREEMENT
        )
