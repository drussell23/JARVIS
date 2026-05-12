#!/usr/bin/env python3
"""Phase 1.5.D — Empirical L2 exercise corpus hardness validator.

Measures the production DW Tier 0 provider's first-try fail rate
against the L2 exercise corpus.  Closes the empirical-validation gap
before the Phase 1.5.E operator-paced acceptance soak: if the corpus
turns out trivially-solvable (DW one-shots the off-by-one in <20% of
attempts), the acceptance predicate ``.jarvis/ouroboros/repair_tree.
jsonl ≥ 1 row`` would never fire regardless of code correctness, and
we'd be debugging the wrong layer.  This validator surfaces that
condition BEFORE the soak runs.

Operator-paced contract
-----------------------

* Costs real money via ``DOUBLEWORD_API_KEY`` — refuses to run
  without ``--confirm-paid``.
* Bounded-cost: hard-stops at ``--max-cost-usd`` (default 0.50).
* Pure-stdlib + canonical substrate composition only.

Composition discipline (single-source-of-truth)
-----------------------------------------------

* :func:`backend.core.ouroboros.governance.l2_exercise_seed.list_corpus_problems`
  — canonical walker.  No parallel directory enumeration.
* :func:`backend.core.ouroboros.governance.l2_exercise_seed.load_exercise_problem`
  — canonical loader.  No parallel manifest parser.
* DW env vars (``DOUBLEWORD_API_KEY`` / ``DOUBLEWORD_BASE_URL`` /
  ``DOUBLEWORD_MODEL`` / ``DOUBLEWORD_INPUT_COST_PER_M`` /
  ``DOUBLEWORD_OUTPUT_COST_PER_M``) — SAME env vars
  ``DoublewordProvider.__init__`` reads.  No parallel config schema.
* Subprocess pytest invocation — SAME pattern
  :file:`tests/governance/test_fixture_l2_exercise_problem_001.py`
  uses for the buggy-code-fails / known-good-fix-passes invariants.

Honest measurement caveat
-------------------------

The prompt this validator sends is INTENTIONALLY simpler than the
production GENERATE prompt (which composes context expansion + plan
+ strategic direction + session lessons + tool-result history).  The
clean-room prompt produces an **upper bound** on production
difficulty:

* If the LLM one-shots the fixture here (with LESS context), the
  fixture is definitively too easy — production will one-shot too.
* If the LLM struggles here, the production path will struggle too
  or harder (additional context is help, not hindrance — but
  context can mislead).

A measured_first_try_fail_rate ≥ 0.40 under this clean-room prompt
gives the operator high confidence the fixture will trigger L2 in
the 1.5.E soak.

Report schema
-------------

Output written to ``<corpus_dir>/_hardness_report.json`` (the
underscore prefix means the canonical
:func:`list_corpus_problems` walker skips it).  Schema:

  {
    "schema_version": "hardness_report.v1",
    "timestamp": "<ISO 8601 UTC>",
    "provider_chain_used": "doubleword",
    "model": "<DW model>",
    "attempts_requested": <int>,
    "max_cost_usd": <float>,
    "total_cost_usd": <float>,
    "results": [ {<per-problem summary>}, ... ]
  }

Per-problem summary:

  {
    "problem_id": "...", "kind": "...",
    "attempts_completed": <int>, "attempts_errored": <int>,
    "passes": <int>, "fails": <int>,
    "measured_first_try_fail_rate": <float | null>,
    "per_attempt": [ {<attempt detail>}, ... ]
  }

Exit codes
----------

* 0 — report written successfully
* 2 — refusal: missing ``--confirm-paid`` OR missing
  ``DOUBLEWORD_API_KEY`` OR empty corpus
* 3 — runtime error during validation (rare; report still partially
  written if any results landed)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# Repo-root resolution (matches scripts/candidate_generator_defect4_verdict.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ===========================================================================
# Constants (§33.3 naming-cage)
# ===========================================================================


REPORT_SCHEMA_VERSION: str = "hardness_report.v1"
REPORT_FILENAME: str = "_hardness_report.json"

# Refusal sentinel argv flag.  Operator MUST pass this to confirm
# real-money execution; absent = script refuses to call the LLM.
CONFIRM_PAID_ARGV: str = "--confirm-paid"

DEFAULT_ATTEMPTS: int = 5
DEFAULT_MAX_COST_USD: float = 0.50
DEFAULT_TIMEOUT_S: float = 60.0
DEFAULT_PYTEST_TIMEOUT_S: int = 30

# Canonical provider label.  AST-pinned so the report schema stays
# stable across runs.
PROVIDER_DW: str = "doubleword"

# Canonical DW endpoint suffix.  Composes the same OpenAI-compat
# /v1/chat/completions surface DoublewordProvider's realtime path uses.
# Pinned by composition test so accidental drift to a different
# endpoint is caught.
DW_CHAT_COMPLETIONS_SUFFIX: str = "/chat/completions"


# ===========================================================================
# Prompt builder (stable format pinned by spine)
# ===========================================================================


def build_clean_room_fix_prompt(
    target_file_name: str,
    before_content: str,
    test_file_name: str,
    test_content: str,
) -> str:
    """Construct a clean-room fix prompt for the LLM.

    Intentionally simpler than the production GENERATE prompt — see
    the module docstring's "honest measurement caveat" section.

    Format is AST-pinned so hardness measurements across validator
    runs are comparable.
    """
    return (
        "You are an expert Python developer fixing a bug.\n\n"
        f"The file `{target_file_name}` contains buggy code:\n"
        "```python\n"
        f"{before_content}\n"
        "```\n\n"
        f"The following pytest assertions in `{test_file_name}` FAIL "
        "against this code:\n"
        "```python\n"
        f"{test_content}\n"
        "```\n\n"
        f"Produce the corrected `{target_file_name}` so all tests pass.\n"
        "Respond with ONLY the corrected Python code — no markdown "
        "fences, no commentary."
    )


def strip_markdown_fences(text: str) -> str:
    """Defensive: if the LLM ignores the 'no fences' instruction,
    strip a single leading/trailing fenced block.  Pure function;
    never raises."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.split("\n")
    lines = lines[1:]  # drop opening fence line
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)


# ===========================================================================
# Cost tracker (bounded; hard-stops at cap)
# ===========================================================================


@dataclass
class CostTracker:
    """Cumulative USD tracker for the validator run.

    Composes the same per-million pricing constants
    :class:`DoublewordProvider` reads from
    ``DOUBLEWORD_INPUT_COST_PER_M`` / ``DOUBLEWORD_OUTPUT_COST_PER_M``.
    Single source of truth: env var lookup happens once at construction;
    no parallel pricing table.
    """

    max_usd: float
    input_per_m: float
    output_per_m: float
    total_usd: float = 0.0
    last_usage: Dict[str, Any] = field(default_factory=dict)

    def record(self, usage: Dict[str, Any]) -> float:
        prompt_t = float(usage.get("prompt_tokens", 0) or 0)
        comp_t = float(usage.get("completion_tokens", 0) or 0)
        cost = (
            prompt_t * self.input_per_m
            + comp_t * self.output_per_m
        ) / 1_000_000.0
        self.total_usd += cost
        self.last_usage = dict(usage)
        return cost

    def exceeded(self) -> bool:
        return self.total_usd >= self.max_usd


# ===========================================================================
# Provider call (composes canonical DW env vars + endpoint suffix)
# ===========================================================================


async def call_doubleword_one_shot(
    api_key: str,
    base_url: str,
    model: str,
    prompt: str,
    timeout_s: float,
) -> Tuple[str, Dict[str, Any]]:
    """One-shot DW chat completion.

    Composes the OpenAI-compatible ``/chat/completions`` endpoint
    (same surface DoublewordProvider's realtime path uses).

    Returns
    -------
    (response_text, usage_dict)
        ``response_text`` is the LLM's completion content (raw — may
        contain markdown fences).  ``usage_dict`` is the OpenAI-compat
        usage payload (``prompt_tokens`` / ``completion_tokens`` /
        ``total_tokens``).

    Raises
    ------
    httpx.HTTPError, KeyError, ValueError, OSError
        Caller catches; one failed attempt is recorded as errored,
        run continues.
    """
    import httpx  # lazy: --help should not require httpx
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 4096,
        "stream": False,
    }
    url = f"{base_url.rstrip('/')}{DW_CHAT_COMPLETIONS_SUFFIX}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {}) or {}
    return text, usage


# ===========================================================================
# Pytest invocation (mirrors test_fixture_l2_exercise_problem_001.py)
# ===========================================================================


def run_pytest_against_candidate(
    tmpdir: Path,
    candidate_code: str,
    test_content: str,
    target_name: str,
    test_name: str,
    timeout_s: int = DEFAULT_PYTEST_TIMEOUT_S,
) -> bool:
    """Write the candidate + test files into tmpdir + run pytest.

    SAME subprocess pattern :file:`tests/governance/
    test_fixture_l2_exercise_problem_001.py::_run_pytest_against_files`
    uses.  Returns True iff the subprocess exit code is 0.
    """
    (tmpdir / target_name).write_text(candidate_code, encoding="utf-8")
    (tmpdir / test_name).write_text(test_content, encoding="utf-8")
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest", str(tmpdir / test_name),
                "-q", "--no-header", "--tb=no",
            ],
            cwd=str(tmpdir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


# ===========================================================================
# Per-problem driver
# ===========================================================================


async def validate_one_problem(
    problem: Any,
    attempts: int,
    dw_config: Dict[str, Any],
    cost_tracker: CostTracker,
) -> Dict[str, Any]:
    """Run N attempts against one canonical
    :class:`ExerciseProblem`.  Returns a per-problem result dict for
    the report.

    Stops early if the cost tracker exceeds the cap."""
    passes = 0
    fails = 0
    errors = 0
    per_attempt: List[Dict[str, Any]] = []
    for attempt_idx in range(attempts):
        if cost_tracker.exceeded():
            per_attempt.append({
                "attempt": attempt_idx,
                "skipped_reason": "cost_cap_reached",
            })
            break
        try:
            prompt = build_clean_room_fix_prompt(
                problem.target_file_name,
                problem.before_content,
                problem.test_file_name,
                problem.test_content,
            )
            text, usage = await call_doubleword_one_shot(
                api_key=dw_config["api_key"],
                base_url=dw_config["base_url"],
                model=dw_config["model"],
                prompt=prompt,
                timeout_s=dw_config["timeout_s"],
            )
            attempt_cost = cost_tracker.record(usage)
            candidate = strip_markdown_fences(text)
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmpdir = Path(raw_tmp)
                passed = run_pytest_against_candidate(
                    tmpdir, candidate, problem.test_content,
                    problem.target_file_name, problem.test_file_name,
                )
            if passed:
                passes += 1
            else:
                fails += 1
            per_attempt.append({
                "attempt": attempt_idx,
                "passed": passed,
                "usage": usage,
                "cost_usd": round(attempt_cost, 6),
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — operator-visible
            errors += 1
            per_attempt.append({
                "attempt": attempt_idx,
                "error": f"{type(exc).__name__}: {exc}",
            })
    completed = passes + fails
    fail_rate: Optional[float] = (
        (fails / completed) if completed > 0 else None
    )
    return {
        "problem_id": problem.problem_id,
        "kind": problem.kind.value,
        "attempts_requested": attempts,
        "attempts_completed": completed,
        "attempts_errored": errors,
        "passes": passes,
        "fails": fails,
        "measured_first_try_fail_rate": fail_rate,
        "per_attempt": per_attempt,
    }


# ===========================================================================
# Async main
# ===========================================================================


async def _amain(args: argparse.Namespace) -> int:
    # Lazy import canonical substrate — composition pin.
    from backend.core.ouroboros.governance.l2_exercise_seed import (
        list_corpus_problems,
        load_exercise_problem,
        corpus_path as substrate_corpus_path,
    )

    corpus = (
        Path(args.corpus) if args.corpus else substrate_corpus_path()
    )
    problem_dirs = list_corpus_problems(corpus)
    if not problem_dirs:
        print(
            f"ERROR: no problem directories found in {corpus}",
            file=sys.stderr,
        )
        return 2

    dw_config: Dict[str, Any] = {
        "api_key": os.environ.get("DOUBLEWORD_API_KEY", "").strip(),
        "base_url": os.environ.get(
            "DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1",
        ).strip(),
        "model": os.environ.get(
            "DOUBLEWORD_MODEL", "Qwen/Qwen3.5-397B-A17B-FP8",
        ).strip(),
        "timeout_s": args.timeout,
    }
    if not dw_config["api_key"]:
        print(
            "ERROR: DOUBLEWORD_API_KEY env var is required.",
            file=sys.stderr,
        )
        return 2

    input_per_m = float(
        os.environ.get("DOUBLEWORD_INPUT_COST_PER_M", "0.10"),
    )
    output_per_m = float(
        os.environ.get("DOUBLEWORD_OUTPUT_COST_PER_M", "0.40"),
    )
    cost_tracker = CostTracker(
        max_usd=args.max_cost_usd,
        input_per_m=input_per_m,
        output_per_m=output_per_m,
    )

    print("== L2 corpus hardness validator (Phase 1.5.D) ==")
    print(f"  corpus:       {corpus}")
    print(f"  problems:     {[d.name for d in problem_dirs]}")
    print(f"  attempts/ea:  {args.attempts}")
    print(f"  max cost:     ${args.max_cost_usd:.2f}")
    print(f"  model:        {dw_config['model']}")
    print()

    results: List[Dict[str, Any]] = []
    runtime_error: Optional[str] = None
    try:
        for problem_dir in problem_dirs:
            problem = load_exercise_problem(problem_dir)
            if problem is None:
                print(
                    f"SKIP {problem_dir.name}: "
                    f"load_exercise_problem returned None",
                )
                continue
            print(f"-> {problem.problem_id} (kind={problem.kind.value})")
            result = await validate_one_problem(
                problem, args.attempts, dw_config, cost_tracker,
            )
            results.append(result)
            rate = result["measured_first_try_fail_rate"]
            rate_s = f"{rate:.0%}" if rate is not None else "n/a"
            print(
                f"   fails={result['fails']}/"
                f"{result['attempts_completed']} "
                f"(rate={rate_s}) errors={result['attempts_errored']}",
            )
            if cost_tracker.exceeded():
                print(
                    f"   COST CAP REACHED "
                    f"(${cost_tracker.total_usd:.4f} >= "
                    f"${args.max_cost_usd:.2f})",
                )
                break
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — write partial report
        runtime_error = f"{type(exc).__name__}: {exc}"
        print(
            f"RUNTIME ERROR (writing partial report): {runtime_error}",
            file=sys.stderr,
        )

    report: Dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider_chain_used": PROVIDER_DW,
        "model": dw_config["model"],
        "attempts_requested": args.attempts,
        "max_cost_usd": args.max_cost_usd,
        "total_cost_usd": round(cost_tracker.total_usd, 6),
        "results": results,
    }
    if runtime_error is not None:
        report["runtime_error"] = runtime_error

    report_path = corpus / REPORT_FILENAME
    try:
        report_path.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8",
        )
    except OSError as exc:
        print(
            f"ERROR: could not write report to {report_path}: {exc}",
            file=sys.stderr,
        )
        return 3
    print()
    print(f"Wrote {report_path}")
    print(f"Total cost: ${cost_tracker.total_usd:.4f}")
    return 0 if runtime_error is None else 3


# ===========================================================================
# CLI
# ===========================================================================


def build_argparser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description=(
            "Phase 1.5.D empirical hardness validator.  Measures the "
            "first-try fail rate of the DW Tier 0 provider against "
            "the L2 exercise corpus.  Operator-paced, bounded-cost; "
            f"refuses to run without {CONFIRM_PAID_ARGV}."
        ),
    )
    ap.add_argument(
        "--corpus", default=None,
        help=(
            "Corpus directory.  Default: from "
            "JARVIS_L2_EXERCISE_CORPUS_PATH (or in-repo fixtures)."
        ),
    )
    ap.add_argument(
        "--attempts", type=int, default=DEFAULT_ATTEMPTS,
        help=f"Attempts per problem (default {DEFAULT_ATTEMPTS}).",
    )
    ap.add_argument(
        "--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD,
        help=(
            f"Hard cost cap in USD (default ${DEFAULT_MAX_COST_USD:.2f})."
        ),
    )
    ap.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_S,
        help=(
            f"Per-request timeout in seconds (default "
            f"{DEFAULT_TIMEOUT_S:.0f})."
        ),
    )
    ap.add_argument(
        CONFIRM_PAID_ARGV, dest="confirm_paid", action="store_true",
        help=(
            "REQUIRED.  Acknowledges this script costs real money "
            "via DOUBLEWORD_API_KEY."
        ),
    )
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_argparser().parse_args(argv)
    if not args.confirm_paid:
        print(
            "ERROR: this script costs real money via "
            "DOUBLEWORD_API_KEY.",
            file=sys.stderr,
        )
        print(
            f"Re-run with {CONFIRM_PAID_ARGV} to confirm.",
            file=sys.stderr,
        )
        return 2
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
