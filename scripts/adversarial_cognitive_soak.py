#!/usr/bin/env python3
"""Adversarial Cognitive Soak harness.

Validates the RSI cognitive loop UNDER FIRE *before* the J-Prime failover is
flipped live. It drives a real ``qwen2.5-coder:7b`` (via the production
``LocalPrimeClient``) through a deliberately adversarial coding sub-goal and
observes the REAL epistemic-feedback -> repair -> pivot -> decompose loop produce
(or fail to produce) a test-verified candidate.

The GCP failover *infra* is proven separately; THIS proves the *cognitive
pipeline*: think -> fail -> read its own failure -> adapt (temperature decay +
epistemic diff) -> pivot -> decompose -> converge.

Design constraints (all enforced):
  * gated behind JARVIS_CHAOS_INJECTOR_ENABLED (default false),
  * ASCII only, ``from __future__ import annotations``, Python 3.9+
    (``asyncio.wait_for``, never ``asyncio.timeout``),
  * fail-soft, async,
  * REUSE the real primitives -- no reimplementation:
      - LocalPrimeClient (J-Prime failover generator),
      - epistemic_feedback.{build_failure_context, temperature_for_attempt,
        pivot_verdict},
      - goal_decomposition_planner.decompose_for_block,
      - failure_classifier.failure_signature_hash (logical failure signature),
  * REAL pytest subprocess execution for VALIDATE,
  * bounded -- no infinite loop.

This is NOT run automatically. ``--run`` requires JARVIS_CHAOS_INJECTOR_ENABLED
to be true and talks to a local Ollama at http://127.0.0.1:11434.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import types
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# --- Repo on path (standalone-script invocation) ---------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, ".."))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

# --- REAL primitives (no reimplementation) ---------------------------------
from backend.core.ouroboros.governance.epistemic_feedback import (  # noqa: E402
    build_failure_context,
    pivot_verdict,
    temperature_for_attempt,
)
from backend.core.ouroboros.governance.failure_classifier import (  # noqa: E402
    failure_signature_hash,
)
from backend.core.ouroboros.governance.goal_decomposition_planner import (  # noqa: E402
    decompose_for_block,
)
from backend.core.ouroboros.governance.local_inference_director import (  # noqa: E402
    LocalConfig,
    LocalPrimeClient,
)

_TRUE = {"1", "true", "yes", "on"}


def gate_enabled() -> bool:
    """Master kill-switch. Default OFF -> the harness refuses to run."""
    v = os.environ.get("JARVIS_CHAOS_INJECTOR_ENABLED")
    return bool(v) and v.strip().lower() in _TRUE


# ---------------------------------------------------------------------------
# The adversarial payload
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdversarialPayload:
    """A moderately complex coding sub-goal at the edge of a 7B's window.

    Chosen task: merge-overlapping-intervals WITH the half-open adjacency edge
    case (intervals that only TOUCH at an endpoint, e.g. [1,2] and [2,3], must
    merge into [1,3]). A first draft from a 7B typically uses ``s < last_end``
    (strict) and silently botches adjacency -- a subtle, deterministic edge case
    that the test suite pins.
    """

    title: str
    description: str
    entry_symbol: str
    impl_filename: str
    test_filename: str
    tests: str
    system_prompt: str

    def build_prompt(self, epistemic_feedback: str = "") -> str:
        """Compose the generation prompt; append the Hybrid Epistemic Diff (if any)."""
        parts = [
            "<task>",
            self.title,
            "</task>",
            "<description>",
            self.description,
            "</description>",
            "<requirements>",
            f"- Define a top-level function named `{self.entry_symbol}`.",
            "- Handle the empty input case.",
            "- Sort the intervals first.",
            "- CRITICAL EDGE CASE: intervals that only TOUCH at an endpoint",
            "  (e.g. (1, 2) and (2, 3)) MUST merge into (1, 3) -- adjacency counts",
            "  as overlap. Use `<=`, not `<`.",
            "- Return a list of tuples.",
            "</requirements>",
            "<output_format>",
            "Return ONLY a single Python code block (```python ... ```) with the",
            "full implementation. No prose, no tests.",
            "</output_format>",
        ]
        if epistemic_feedback:
            parts += [
                "",
                "<previous_attempt_feedback>",
                "Your previous attempt FAILED the test suite. Study this epistemic",
                "feedback (diff vs your prior attempt + the failing-test stderr) and",
                "FIX the root cause -- do not repeat the same mistake:",
                "",
                epistemic_feedback,
                "</previous_attempt_feedback>",
            ]
        return "\n".join(parts)


_TESTS = textwrap.dedent(
    '''
    from impl import merge_intervals


    def test_empty():
        assert merge_intervals([]) == []


    def test_no_overlap():
        assert merge_intervals([(1, 2), (4, 5)]) == [(1, 2), (4, 5)]


    def test_simple_overlap():
        assert merge_intervals([(1, 3), (2, 5)]) == [(1, 5)]


    def test_unsorted_input():
        assert merge_intervals([(4, 5), (1, 3), (2, 4)]) == [(1, 5), (4, 5)] or \\
            merge_intervals([(4, 5), (1, 3), (2, 4)]) == [(1, 5)]


    def test_adjacency_edge_case():
        # The subtle one: touching intervals must merge.
        assert merge_intervals([(1, 2), (2, 3)]) == [(1, 3)]


    def test_adjacency_chain():
        assert merge_intervals([(1, 2), (2, 3), (3, 4)]) == [(1, 4)]


    def test_nested():
        assert merge_intervals([(1, 10), (2, 3), (4, 5)]) == [(1, 10)]
    '''
).strip()


ADVERSARIAL_PAYLOAD = AdversarialPayload(
    title="Merge overlapping intervals (with adjacency edge case)",
    description=(
        "Implement `merge_intervals(intervals)` that merges a list of "
        "(start, end) integer tuples so that any overlapping OR ADJACENT "
        "intervals are combined into a single interval. Adjacency means two "
        "intervals that touch at an endpoint (e.g. (1, 2) and (2, 3)) must "
        "merge into (1, 3). Return a sorted list of merged (start, end) tuples."
    ),
    entry_symbol="merge_intervals",
    impl_filename="impl.py",
    test_filename="test_impl.py",
    tests=_TESTS,
    system_prompt=(
        "You are a precise senior Python engineer. You write correct, minimal "
        "implementations and you reason carefully about edge cases before "
        "emitting code. Output a single Python code block only."
    ),
)


# ---------------------------------------------------------------------------
# Code extraction + real pytest VALIDATE boundary
# ---------------------------------------------------------------------------

_CODE_BLOCK_RE = re.compile(r"```(?:python)?\s*\n(.*?)```", re.DOTALL)


def _extract_code_block(text: str) -> str:
    """Extract the first fenced python code block; fall back to raw text.

    Fail-soft: always returns a string.
    """
    if not text:
        return ""
    try:
        m = _CODE_BLOCK_RE.search(text)
        if m:
            return m.group(1).strip()
        # No fence -- best-effort: return the text verbatim (it may be raw code).
        return text.strip()
    except Exception:
        return str(text)


def _run_pytest_in_tempdir(impl_src: str, tests_src: str, *, timeout_s: int = 90) -> Dict[str, Any]:
    """Write impl + tests to a tempdir and run REAL pytest as a subprocess.

    Returns {passed: bool, stdout: str, stderr: str, returncode: int}. Fail-soft:
    a missing impl / timeout / crash is reported as a non-pass, never raises.
    """
    result: Dict[str, Any] = {"passed": False, "stdout": "", "stderr": "", "returncode": -1}
    if not impl_src:
        result["stderr"] = "empty implementation (model produced no code block)"
        return result
    try:
        with tempfile.TemporaryDirectory(prefix="adv_soak_") as d:
            impl_path = os.path.join(d, ADVERSARIAL_PAYLOAD.impl_filename)
            test_path = os.path.join(d, ADVERSARIAL_PAYLOAD.test_filename)
            with open(impl_path, "w", encoding="ascii", errors="replace") as f:
                f.write(impl_src)
            with open(test_path, "w", encoding="ascii", errors="replace") as f:
                f.write(tests_src)
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test_path],
                    cwd=d,
                    capture_output=True,
                    text=True,
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired as e:
                result["stderr"] = f"pytest TIMEOUT after {timeout_s}s: {e}"
                return result
            result["returncode"] = proc.returncode
            result["stdout"] = proc.stdout or ""
            result["stderr"] = proc.stderr or ""
            result["passed"] = proc.returncode == 0
            return result
    except Exception as e:  # noqa: BLE001
        result["stderr"] = f"harness error running pytest: {e}"
        return result


_FAIL_RE = re.compile(r"^(\S+\.py::\S+)\s+(?:FAILED|ERROR)", re.MULTILINE)


def _normalize_id(node_id: str) -> str:
    """Strip the (random tempdir) path prefix so the node id is path-independent.

    "/tmp/adv_soak_xyz/test_impl.py::test_foo" -> "test_impl.py::test_foo".
    This is what makes the failure SIGNATURE stable across attempts (the impl is
    rewritten into a fresh tempdir each VALIDATE, so the absolute path varies).
    """
    try:
        if "::" not in node_id:
            return node_id
        path, _, rest = node_id.partition("::")
        return os.path.basename(path) + "::" + rest
    except Exception:
        return node_id


def _failing_test_ids(out: Dict[str, Any]) -> List[str]:
    """Extract failing test node ids from pytest output (fail-soft, path-normalized)."""
    try:
        blob = (out.get("stdout") or "") + "\n" + (out.get("stderr") or "")
        ids = _FAIL_RE.findall(blob)
        if not ids:
            # Fallback: pytest -q "short test summary" style "FAILED ...::..."
            ids = re.findall(r"FAILED\s+(\S+::\S+)", blob)
        return sorted({_normalize_id(i) for i in ids}) if ids else []
    except Exception:
        return []


def _signature_for(out: Dict[str, Any]) -> str:
    """Logical failure signature -- reuse the production failure_signature_hash.

    Failure class is "test" here (a real assertion failure, not syntax/env).
    """
    try:
        ids = _failing_test_ids(out)
        return failure_signature_hash(ids, "test")
    except Exception:
        # Last-resort stable-ish fallback from the stderr tail.
        try:
            return failure_signature_hash([(out.get("stderr") or "")[-200:]], "test")
        except Exception:
            return "unknown"


# ---------------------------------------------------------------------------
# Goal stub for decompose_for_block (duck-typed: goal_id/title/description/files)
# ---------------------------------------------------------------------------


def _build_goal() -> Any:
    return types.SimpleNamespace(
        goal_id="adv-soak-merge-intervals",
        title=ADVERSARIAL_PAYLOAD.title,
        description=(
            ADVERSARIAL_PAYLOAD.description
            + " HYPER-ATOMIC FOCUS: get the adjacency edge case right first -- "
            "merge intervals that only touch at an endpoint using `<=`."
        ),
        target_files=(ADVERSARIAL_PAYLOAD.impl_filename,),
    )


# ---------------------------------------------------------------------------
# Soak narrative printing
# ---------------------------------------------------------------------------


def _say(line: str = "") -> None:
    print(line, flush=True)


# ---------------------------------------------------------------------------
# The cognitive loop driver
# ---------------------------------------------------------------------------


async def run_cognitive_soak(*, client: Any, max_repairs: int = 3) -> Dict[str, Any]:
    """Drive the adversarial payload through the REAL cognitive loop.

    Returns a result dict:
      {converged, attempts, temperature_trajectory, pivoted, decomposed,
       epistemic_diffs_injected, final_test_output, signatures}

    Bounded: at most (1 initial GENERATE + max_repairs repairs + 1 post-pivot
    GENERATE) attempts. Never loops forever. Fail-soft.
    """
    if not gate_enabled():
        raise RuntimeError(
            "adversarial_cognitive_soak refuses to run: set "
            "JARVIS_CHAOS_INJECTOR_ENABLED=true"
        )

    payload = ADVERSARIAL_PAYLOAD
    base_temp = float(os.environ.get("JARVIS_ADV_SOAK_BASE_TEMP", "0.7"))
    pytest_timeout_s = int(os.environ.get("JARVIS_ADV_SOAK_PYTEST_TIMEOUT_S", "90"))
    gen_timeout_s = float(os.environ.get("JARVIS_ADV_SOAK_GEN_TIMEOUT_S", "240"))

    iterations: List[Dict[str, Any]] = []
    temperature_trajectory: List[float] = []
    signatures: List[str] = []
    epistemic_diffs_injected = 0
    pivoted = False
    decomposed = False
    converged = False
    attempts = 0

    prev_impl = ""
    epistemic_feedback = ""
    repeated_signature_count = 0
    last_signature: Optional[str] = None
    current_payload_prompt_goal = payload  # may swap to decomposed sub-chunk text
    decomposed_description = ""

    async def _generate(temperature: float, feedback: str) -> str:
        prompt = current_payload_prompt_goal.build_prompt(feedback) \
            if hasattr(current_payload_prompt_goal, "build_prompt") \
            else payload.build_prompt(feedback)
        if decomposed_description:
            prompt = prompt + "\n\n<decomposed_sub_goal>\n" + decomposed_description + \
                "\n</decomposed_sub_goal>"
        try:
            resp = await asyncio.wait_for(
                client.generate(
                    prompt,
                    system_prompt=payload.system_prompt,
                    temperature=temperature,
                ),
                timeout=gen_timeout_s,
            )
        except asyncio.TimeoutError:
            _say(f"  [TIMEOUT] generate exceeded {gen_timeout_s}s -- treating as empty")
            return ""
        except Exception as e:  # noqa: BLE001
            _say(f"  [GEN-ERROR] {e} -- treating as empty")
            return ""
        return getattr(resp, "content", "") or ""

    _say("=" * 72)
    _say("ADVERSARIAL COGNITIVE SOAK -- driving the RSI loop UNDER FIRE")
    _say("=" * 72)
    _say(f"Payload: {payload.title}")
    _say(f"Entry symbol: {payload.entry_symbol}  | base_temp={base_temp}  | "
         f"max_repairs={max_repairs}")
    _say("-" * 72)

    # --- Bounded loop -------------------------------------------------------
    # Phase budget: initial GENERATE + up to max_repairs repairs, and a pivot
    # may grant ONE extra post-decompose GENERATE.
    total_budget = 1 + max_repairs + 1
    pivot_extra_used = False

    while attempts < total_budget:
        is_repair = attempts > 0
        temperature = temperature_for_attempt(base_temp, repeated_signature_count)
        temperature_trajectory.append(temperature)
        attempts += 1

        phase = "REPAIR" if is_repair else "GENERATE"
        _say(f"[attempt {attempts}] phase={phase}  temperature={temperature:.4f}  "
             f"repeated_sig_count={repeated_signature_count}  "
             f"diff_injected={bool(epistemic_feedback)}")

        raw = await _generate(temperature, epistemic_feedback)
        impl_src = _extract_code_block(raw)

        out = _run_pytest_in_tempdir(impl_src, payload.tests, timeout_s=pytest_timeout_s)
        passed = bool(out["passed"])

        iterations.append({
            "attempt": attempts,
            "temperature": round(temperature, 6),
            "signature": None,  # filled below on fail
            "diff_injected": bool(epistemic_feedback),
            "test_result": "PASS" if passed else "FAIL",
        })

        if passed:
            converged = True
            _say(f"  -> PASS (pytest green). Cognitive convergence reached on "
                 f"attempt {attempts}.")
            iterations[-1]["test_result"] = "PASS"
            final_out = out
            break

        # --- FAIL path ------------------------------------------------------
        sig = _signature_for(out)
        signatures.append(sig)
        iterations[-1]["signature"] = sig[:12]
        fail_ids = _failing_test_ids(out)
        _say(f"  -> FAIL  signature={sig[:12]}  failing={len(fail_ids)} "
             f"({', '.join(t.split('::')[-1] for t in fail_ids) or 'n/a'})")

        # Same-signature repeat tracking drives temperature decay + pivot.
        if last_signature is not None and sig == last_signature:
            repeated_signature_count += 1
        last_signature = sig

        # Build the Hybrid Epistemic Diff (REAL builder) and inject next turn.
        epistemic_feedback = build_failure_context(
            prior_src=prev_impl,
            failed_src=impl_src,
            stderr=(out.get("stdout") or "") + "\n" + (out.get("stderr") or ""),
            failing_tests=fail_ids,
            sub_goal_label=payload.title,
        )
        if epistemic_feedback:
            epistemic_diffs_injected += 1
            _say(f"  -> injected Hybrid Epistemic Diff "
                 f"({len(epistemic_feedback)} chars) into next prompt")
        prev_impl = impl_src

        # --- Pivot check (REAL pivot_verdict) -------------------------------
        # Floor is reached when one more decay no longer changes the temperature.
        next_temp = temperature_for_attempt(base_temp, repeated_signature_count + 1)
        temp_at_floor = abs(next_temp - temperature) < 1e-9

        if not pivoted and pivot_verdict(repeated_signature_count, temp_at_floor):
            pivoted = True
            _say("")
            _say("[SOVEREIGN YIELD: UNRESOLVABLE PATH] "
                 f"same signature x{repeated_signature_count}, temp at floor "
                 f"({temperature:.4f}). Pivoting -> decompose_for_block.")
            failure_hint = {
                "signature_hash": sig,
                "stderr_tail": (out.get("stdout") or "")[-1200:],
            }
            try:
                sub_goals = decompose_for_block(
                    _build_goal(),
                    zero_coverage=False,
                    failure_hint=failure_hint,
                )
                decomposed = bool(sub_goals)
                if sub_goals:
                    # Re-aim at the SMALLEST/most-atomic mutation sub-chunk.
                    chunk = sub_goals[-1]
                    decomposed_description = (
                        f"{getattr(chunk, 'title', '')}: "
                        f"{getattr(chunk, 'description', '')}"
                    )[:1500]
                    _say(f"  -> decompose emitted {len(sub_goals)} sub-goal(s); "
                         f"re-aiming at hyper-atomic chunk: "
                         f"{getattr(chunk, 'sub_goal_id', '?')}")
            except Exception as e:  # noqa: BLE001
                _say(f"  -> decompose_for_block error (fail-soft): {e}")
                decomposed = False

            # Reset the repeat counter so the post-pivot attempt gets a fair
            # (higher) temperature against the SMALLER chunk -- bounded by the
            # one pivot_extra grant.
            if not pivot_extra_used:
                pivot_extra_used = True
                total_budget += 1
                repeated_signature_count = 0
                last_signature = None
            _say("")

        if attempts >= total_budget:
            break

    final_out = locals().get("final_out", out if "out" in locals() else {})

    result = {
        "converged": converged,
        "attempts": attempts,
        "temperature_trajectory": temperature_trajectory,
        "pivoted": pivoted,
        "decomposed": decomposed,
        "epistemic_diffs_injected": epistemic_diffs_injected,
        "final_test_output": {
            "passed": bool(final_out.get("passed")) if isinstance(final_out, dict) else False,
            "stdout_tail": (final_out.get("stdout", "") if isinstance(final_out, dict) else "")[-800:],
            "stderr_tail": (final_out.get("stderr", "") if isinstance(final_out, dict) else "")[-800:],
        },
        "signatures": [s[:12] for s in signatures],
        "iterations": iterations,
    }
    return result


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


def print_report(result: Dict[str, Any]) -> None:
    _say("")
    _say("=" * 72)
    _say("SOAK NARRATIVE / VERDICT")
    _say("=" * 72)
    for it in result.get("iterations", []):
        _say(f"  attempt {it['attempt']:>2}  temp={it['temperature']:<8} "
             f"diff_injected={str(it['diff_injected']):<5} "
             f"sig={str(it['signature']):<14} {it['test_result']}")
    traj = ", ".join(f"{t:.4f}" for t in result.get("temperature_trajectory", []))
    _say("")
    _say(f"  temperature trajectory : [{traj}]")
    _say(f"  epistemic diffs injected: {result.get('epistemic_diffs_injected')}")
    _say(f"  pivoted                 : {result.get('pivoted')}")
    _say(f"  decomposed              : {result.get('decomposed')}")
    _say(f"  attempts                : {result.get('attempts')}")
    _say(f"  converged               : {result.get('converged')}")
    _say("-" * 72)
    if result.get("converged"):
        _say("VERDICT: CONVERGED. A test-verified candidate was produced UNDER FIRE.")
        _say("FLIP-GATE: SATISFIED -- the cognitive pipeline survives adversarial")
        _say("           load (think -> fail -> adapt -> [pivot/decompose] -> pass).")
    else:
        _say("VERDICT: NON-CONVERGENCE (honest, bounded). No infinite loop; the loop")
        _say("         yielded after exhausting its repair + pivot budget.")
        _say("FLIP-GATE: NOT satisfied -- do NOT flip the failover live yet.")
        st = result.get("final_test_output", {})
        if st.get("stdout_tail"):
            _say("  last pytest stdout tail:")
            for ln in st["stdout_tail"].splitlines()[-12:]:
                _say("    " + ln)
    _say("=" * 72)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def _build_real_client(model: str) -> LocalPrimeClient:
    cfg = LocalConfig.from_env()
    # Pin to the soak target regardless of env model default.
    cfg = LocalConfig(
        base_url=os.environ.get("JARVIS_LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434"),
        model_name=model,
        keep_alive_seconds=cfg.keep_alive_seconds,
        timeout_seed_ms=cfg.timeout_seed_ms,
        timeout_ceiling_ms=cfg.timeout_ceiling_ms,
        timeout_floor_ms=cfg.timeout_floor_ms,
        output_ratio=cfg.output_ratio,
        margin_sigma=cfg.margin_sigma,
        window_size=cfg.window_size,
        min_samples=cfg.min_samples,
        max_concurrency=cfg.max_concurrency,
        pool_limit=cfg.pool_limit,
    )
    return LocalPrimeClient(cfg)


async def _amain(args: argparse.Namespace) -> int:
    if not gate_enabled():
        _say("REFUSED: set JARVIS_CHAOS_INJECTOR_ENABLED=true to run the soak.")
        return 2
    if not args.run:
        _say("Dry mode: pass --run to drive the real local model. (Gate is ON.)")
        _say(f"Would target model={args.model} at "
             f"{os.environ.get('JARVIS_LOCAL_MODEL_BASE_URL', 'http://127.0.0.1:11434')}")
        return 0

    client = _build_real_client(args.model)
    try:
        result = await run_cognitive_soak(client=client, max_repairs=args.max_repairs)
        print_report(result)
        return 0 if result.get("converged") else 1
    finally:
        try:
            await client.aclose()
        except Exception:
            pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Adversarial Cognitive Soak -- drives qwen2.5-coder:7b through "
                    "the real epistemic-feedback -> repair -> pivot -> decompose loop."
    )
    parser.add_argument("--run", action="store_true",
                        help="Actually drive the real local Ollama model.")
    parser.add_argument("--model", default="qwen2.5-coder:7b",
                        help="Local model name (default: qwen2.5-coder:7b).")
    parser.add_argument("--max-repairs", type=int, default=3,
                        help="Max repair iterations before pivot (default: 3).")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        _say("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
