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
import enum
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple


logger = logging.getLogger("Phase1.5.D.HardnessValidator")


# Repo-root resolution (matches scripts/candidate_generator_defect4_verdict.py)
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ===========================================================================
# Constants (§33.3 naming-cage)
# ===========================================================================


# Schema v2 — bumped from v1 to mark the addition of:
#   * Per-attempt ``status`` field (AttemptStatus taxonomy)
#   * Per-attempt ``retries_used`` field (parse-retry budget consumption)
#   * Per-problem ``attempt_status_counts`` aggregation
#   * Per-problem ``insufficient_samples`` flag
#   * Report-level ``hardness_set``, ``hardness_set_mean_fail_rate``,
#     ``meets_acceptance_gate``, ``acceptance_threshold``,
#     ``min_completed_per_problem``, ``parse_retry_budget``
# Legacy v1 fields (passes/fails/attempts_completed/attempts_errored/
# measured_first_try_fail_rate) are preserved alongside the new
# structured fields so v1 consumers keep parsing while v2-aware
# consumers branch on schema_version.
REPORT_SCHEMA_VERSION: str = "hardness_report.v2"
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


# ---- Stage 1 measurement-integrity knobs ----------------------------------
#
# Per the user-defined contract (Phase 1.5.D.2):
#
#   * Provider-parse errors are NOT failures.  They surface when the
#     model's response shape doesn't match either ``content`` or
#     ``reasoning_content`` (e.g., Qwen3.5 emitting only
#     ``reasoning_details``).  Counting these as "failed pytest"
#     would bias the measured fail-rate.  Instead: classify as
#     ``PROVIDER_PARSE_ERROR``, retry within budget, exclude from
#     the fail-rate numerator AND denominator.
#
#   * The fail-rate over a single problem is unreliable when N is
#     small.  ``insufficient_samples`` per-problem flag + a global
#     ``min_completed_per_problem`` knob make this honest.
#
#   * The acceptance gate is evaluated over a HARDNESS_SET subset
#     of the corpus (not necessarily every problem).  Empty default
#     means the gate is unevaluable until the operator sets the
#     set explicitly — protects against silent semantic change.

DEFAULT_PARSE_RETRY_BUDGET: int = 3
PARSE_RETRY_ENV_VAR: str = "JARVIS_VALIDATOR_PARSE_RETRY"

DEFAULT_MIN_COMPLETED_PER_PROBLEM: int = 3
MIN_COMPLETED_ENV_VAR: str = (
    "JARVIS_VALIDATOR_MIN_COMPLETED_PER_PROBLEM"
)

# Mean fail-rate threshold for HARDNESS_SET acceptance.  Bumped
# from 0.40 → 0.45 in Phase 1.5.D.2 Stage 3.5 after the first paid
# run (Stage 3) showed the mean landing exactly on the prior 0.40
# boundary with high per-problem variance (one fixture at 80%, the
# other at 0%).  0.45 gives operational margin without being so
# aggressive that legitimate borderline corpora get rejected.
DEFAULT_ACCEPTANCE_THRESHOLD: float = 0.45
ACCEPTANCE_THRESHOLD_ENV_VAR: str = (
    "JARVIS_VALIDATOR_ACCEPTANCE_THRESHOLD"
)

# Per-problem fail-rate FLOOR — the "no freeriders" rule.  Each
# HARDNESS_SET member must individually clear this floor; a fixture
# at 0% can no longer be carried by another fixture at 80% to
# squeak past the mean.  Added in Stage 3.5 after the empirical
# measurement showed problem_003 (v1) failing to trigger the
# multi-site trap while problem_002 carried the entire gate.
# Default 0.20 — every member contributes meaningful signal.
DEFAULT_PER_PROBLEM_FLOOR: float = 0.20
PER_PROBLEM_FLOOR_ENV_VAR: str = (
    "JARVIS_VALIDATOR_PER_PROBLEM_FLOOR"
)

# Comma-separated list of problem_ids that compose the HARDNESS_SET
# (the subset over which the acceptance gate is computed).  Default
# empty → gate is unevaluable + meets_acceptance_gate=False (forces
# operator to make the set explicit).
HARDNESS_SET_ENV_VAR: str = "JARVIS_VALIDATOR_HARDNESS_SET"


class AttemptStatus(str, enum.Enum):
    """Four canonical outcomes for one logical validation attempt.

    Closed taxonomy.  AST-pinned: tests assert the value-set is
    exactly these four strings.

    PASSED / FAILED — pytest ran AND returned a verdict.  These
    are the ONLY two statuses that count toward
    ``measured_first_try_fail_rate``.

    PROVIDER_PARSE_ERROR — the provider call returned a response
    shape the parser could not extract content from (e.g., Qwen3.5
    emitting ``reasoning_details``-only).  Retried within budget;
    if budget exhausts, this attempt is excluded from the fail-rate
    numerator and denominator.

    ERRORED — any OTHER exception (httpx timeout, network failure,
    pytest subprocess timeout, fixture I/O error).  Not retried;
    excluded from the fail-rate numerator and denominator.
    """

    PASSED = "passed"
    FAILED = "failed"
    PROVIDER_PARSE_ERROR = "provider_parse_error"
    ERRORED = "errored"


# ===========================================================================
# Env readers (garbage-tolerant; NEVER raise; clamped where applicable)
# ===========================================================================


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int = 2**31 - 1,
) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(minimum, min(maximum, int(raw)))
    except (ValueError, TypeError):
        logger.warning(
            "[HardnessValidator] invalid %s=%r — using default %d",
            name, raw, default,
        )
        return default


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return max(minimum, min(maximum, float(raw)))
    except (ValueError, TypeError):
        logger.warning(
            "[HardnessValidator] invalid %s=%r — using default %.2f",
            name, raw, default,
        )
        return default


def parse_hardness_set(raw: Optional[str]) -> FrozenSet[str]:
    """Parse a comma-separated HARDNESS_SET env-var value into a
    deterministic frozenset of problem_ids.

    Tolerates surrounding whitespace + duplicate entries + empty
    segments.  Returns empty frozenset when the input is None or
    contains no non-empty tokens.

    NEVER raises.
    """
    if raw is None:
        return frozenset()
    tokens = [t.strip() for t in raw.split(",")]
    return frozenset(t for t in tokens if t)


def parse_retry_budget() -> int:
    """Read ``JARVIS_VALIDATOR_PARSE_RETRY`` (default 3, clamped
    [0, 10]).  NEVER raises."""
    return _env_int(
        PARSE_RETRY_ENV_VAR,
        DEFAULT_PARSE_RETRY_BUDGET,
        minimum=0, maximum=10,
    )


def min_completed_per_problem() -> int:
    """Read ``JARVIS_VALIDATOR_MIN_COMPLETED_PER_PROBLEM``
    (default 3, clamped [1, 100]).  NEVER raises."""
    return _env_int(
        MIN_COMPLETED_ENV_VAR,
        DEFAULT_MIN_COMPLETED_PER_PROBLEM,
        minimum=1, maximum=100,
    )


def acceptance_threshold() -> float:
    """Read ``JARVIS_VALIDATOR_ACCEPTANCE_THRESHOLD`` (default 0.45,
    clamped [0.0, 1.0]).  NEVER raises."""
    return _env_float(
        ACCEPTANCE_THRESHOLD_ENV_VAR,
        DEFAULT_ACCEPTANCE_THRESHOLD,
        minimum=0.0, maximum=1.0,
    )


def per_problem_floor() -> float:
    """Read ``JARVIS_VALIDATOR_PER_PROBLEM_FLOOR`` (default 0.20,
    clamped [0.0, 1.0]).  The "no freeriders" rule — every
    HARDNESS_SET member must clear this floor individually.
    NEVER raises."""
    return _env_float(
        PER_PROBLEM_FLOOR_ENV_VAR,
        DEFAULT_PER_PROBLEM_FLOOR,
        minimum=0.0, maximum=1.0,
    )


def hardness_set_from_env() -> FrozenSet[str]:
    """Read ``JARVIS_VALIDATOR_HARDNESS_SET`` and parse via
    :func:`parse_hardness_set`.  NEVER raises."""
    return parse_hardness_set(os.environ.get(HARDNESS_SET_ENV_VAR))


# ===========================================================================
# Acceptance gate computation (pure function; NEVER raises)
# ===========================================================================


def compute_acceptance_gate(
    hardness_set: FrozenSet[str],
    results: List[Dict[str, Any]],
    *,
    threshold: float,
    min_completed: int,
    per_problem_floor_value: float = 0.0,
) -> Tuple[Optional[float], bool, Dict[str, Any]]:
    """Evaluate the Phase 1.5.D acceptance gate.

    Returns
    -------
    (mean_fail_rate, meets_gate, diagnostic)
        ``mean_fail_rate`` is ``None`` whenever the gate is
        unevaluable (empty set / missing member / insufficient
        samples on any member / null rate on any member).
        ``meets_gate`` is ``True`` iff ALL of the following hold:
          * The set is non-empty.
          * Every member is in the results AND fully sampled.
          * Every member has a non-null fail-rate ≥
            ``per_problem_floor_value`` (the "no freeriders" rule).
          * The mean fail-rate across the set is ≥ ``threshold``.

        ``diagnostic`` carries the operator-visible reason —
        one of: ``empty_set`` / ``missing_members`` /
        ``insufficient_samples`` / ``null_rate`` / ``below_floor``
        / ``ok``.

    Pure function over the inputs.  NEVER raises.

    The floor parameter defaults to 0.0 for backward compatibility
    with v2 callers; new code should pass the env-resolved
    :func:`per_problem_floor` value.
    """
    if not hardness_set:
        return None, False, {"reason": "empty_set"}
    by_id = {r.get("problem_id"): r for r in results}
    missing = [pid for pid in hardness_set if pid not in by_id]
    if missing:
        return None, False, {
            "reason": "missing_members",
            "missing": sorted(missing),
        }
    insufficient = [
        pid for pid in hardness_set
        if int(by_id[pid].get("attempts_completed", 0)) < min_completed
    ]
    if insufficient:
        return None, False, {
            "reason": "insufficient_samples",
            "under_sampled": sorted(insufficient),
            "min_required": min_completed,
        }
    rates: List[float] = []
    for pid in hardness_set:
        rate = by_id[pid].get("measured_first_try_fail_rate")
        if rate is None:
            return None, False, {
                "reason": "null_rate",
                "problem_id": pid,
            }
        rates.append(float(rate))
    # Per-problem floor — every member must clear individually
    # ("no freeriders").  Stage 3.5 contract; without this, one
    # fixture at 80% can carry a fixture at 0% past the mean.
    under_floor = sorted(
        pid for pid, rate in zip(sorted(hardness_set), [
            float(by_id[p]["measured_first_try_fail_rate"])
            for p in sorted(hardness_set)
        ])
        if rate < per_problem_floor_value
    )
    if under_floor:
        return None, False, {
            "reason": "below_floor",
            "under_floor": under_floor,
            "floor": per_problem_floor_value,
            "per_problem_rates": {
                pid: float(by_id[pid]["measured_first_try_fail_rate"])
                for pid in sorted(hardness_set)
            },
        }
    mean = sum(rates) / len(rates)
    return (
        mean,
        mean >= threshold,
        {"reason": "ok", "n_problems": len(rates)},
    )


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
    text = _extract_dw_message_content(data)
    usage = data.get("usage", {}) or {}
    return text, usage


def _extract_dw_message_content(data: Dict[str, Any]) -> str:
    """Extract the assistant message content from a DW chat-completions
    response.

    Mirrors the canonical parsing path in
    :meth:`DoublewordProvider._call_realtime_chat_completions`
    (``doubleword_provider.py`` lines 2058-2074): for reasoning models
    like Qwen3.5-397B, the actual answer may appear in ``content`` OR
    in ``reasoning_content`` (when the model emits a long reasoning
    trace and folds the final answer into the reasoning section).
    Composes the SAME fallback ladder — no parallel parsing logic.

    Raises
    ------
    KeyError
        If neither ``content`` nor ``reasoning_content`` is present.
        Includes the available keys in the error message so empirical
        diagnosis is one-step.
    """
    choices = data.get("choices", []) or []
    if not choices:
        raise KeyError(
            f"DW response missing 'choices' (or empty). Top-level "
            f"keys: {sorted(data.keys())}"
        )
    message = choices[0].get("message", {}) or {}
    content = message.get("content", "") or ""
    if not content:
        content = message.get("reasoning_content", "") or ""
    if not content:
        raise KeyError(
            f"DW response 'message' has neither 'content' nor "
            f"'reasoning_content' (or both were empty). Message "
            f"keys: {sorted(message.keys())}"
        )
    return content


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


async def _execute_one_attempt(
    problem: Any,
    attempt_idx: int,
    dw_config: Dict[str, Any],
    cost_tracker: CostTracker,
    parse_retry_budget_value: int,
) -> Dict[str, Any]:
    """Run one logical attempt with up to ``parse_retry_budget_value``
    retries on PROVIDER_PARSE_ERROR.

    Returns a per-attempt record with a ``status`` field set to one
    of :class:`AttemptStatus` values + a ``retries_used`` count.

    Each retry consumes the cost budget; the loop short-circuits to
    ``PROVIDER_PARSE_ERROR`` (with reason ``cost_cap_reached_mid_retry``)
    if the tracker exceeds mid-retry.

    PROVIDER_PARSE_ERROR is detected by catching ``KeyError`` from
    :func:`_extract_dw_message_content` specifically.  Other exception
    types route to ``ERRORED`` (no retry).  ``asyncio.CancelledError``
    propagates per the §7 fail-closed contract.
    """
    retries_used = 0
    last_parse_diag: Optional[str] = None
    last_usage: Dict[str, Any] = {}
    last_cost: float = 0.0
    prompt = build_clean_room_fix_prompt(
        problem.target_file_name,
        problem.before_content,
        problem.test_file_name,
        problem.test_content,
    )
    while True:
        if cost_tracker.exceeded():
            # Out of budget either before first try or mid-retry
            if retries_used == 0:
                # Caller will translate "no attempts started + cap
                # already hit" as a skip — but defensively classify
                # here as PROVIDER_PARSE_ERROR with cost reason so
                # the report is still self-describing.
                return {
                    "attempt": attempt_idx,
                    "status": AttemptStatus.PROVIDER_PARSE_ERROR.value,
                    "retries_used": retries_used,
                    "reason": "cost_cap_before_start",
                    "error": last_parse_diag,
                }
            return {
                "attempt": attempt_idx,
                "status": AttemptStatus.PROVIDER_PARSE_ERROR.value,
                "retries_used": retries_used,
                "reason": "cost_cap_reached_mid_retry",
                "error": last_parse_diag,
            }
        try:
            text, usage = await call_doubleword_one_shot(
                api_key=dw_config["api_key"],
                base_url=dw_config["base_url"],
                model=dw_config["model"],
                prompt=prompt,
                timeout_s=dw_config["timeout_s"],
            )
            last_usage = usage
            last_cost = cost_tracker.record(usage)
        except KeyError as exc:
            # PROVIDER_PARSE_ERROR — response shape didn't match
            # content/reasoning_content.  Cost is NOT recorded here
            # because call_doubleword_one_shot raises before usage
            # extraction returns; the model still consumed tokens
            # server-side but we won't know the exact spend.  This
            # is the same accounting gap the canonical
            # DoublewordProvider has — composing the same trade-off.
            retries_used += 1
            last_parse_diag = f"{type(exc).__name__}: {exc}"
            if retries_used > parse_retry_budget_value:
                return {
                    "attempt": attempt_idx,
                    "status": AttemptStatus.PROVIDER_PARSE_ERROR.value,
                    "retries_used": retries_used,
                    "reason": "retry_budget_exhausted",
                    "error": last_parse_diag,
                }
            logger.info(
                "[HardnessValidator] parse-retry %d/%d for attempt %d "
                "(problem=%s): %s",
                retries_used, parse_retry_budget_value, attempt_idx,
                problem.problem_id, last_parse_diag,
            )
            continue
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — operator-visible
            return {
                "attempt": attempt_idx,
                "status": AttemptStatus.ERRORED.value,
                "retries_used": retries_used,
                "error": f"{type(exc).__name__}: {exc}",
            }
        # Provider call succeeded → run pytest verdict
        try:
            candidate = strip_markdown_fences(text)
            with tempfile.TemporaryDirectory() as raw_tmp:
                tmpdir = Path(raw_tmp)
                passed = run_pytest_against_candidate(
                    tmpdir, candidate, problem.test_content,
                    problem.target_file_name, problem.test_file_name,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return {
                "attempt": attempt_idx,
                "status": AttemptStatus.ERRORED.value,
                "retries_used": retries_used,
                "error": f"pytest_setup: {type(exc).__name__}: {exc}",
                "usage": last_usage,
                "cost_usd": round(last_cost, 6),
            }
        return {
            "attempt": attempt_idx,
            "status": (
                AttemptStatus.PASSED.value if passed
                else AttemptStatus.FAILED.value
            ),
            "retries_used": retries_used,
            "passed": passed,  # legacy v1 field, preserved
            "usage": last_usage,
            "cost_usd": round(last_cost, 6),
        }


async def validate_one_problem(
    problem: Any,
    attempts: int,
    dw_config: Dict[str, Any],
    cost_tracker: CostTracker,
    *,
    parse_retry_budget_value: Optional[int] = None,
    min_completed_value: Optional[int] = None,
) -> Dict[str, Any]:
    """Run N attempts against one canonical
    :class:`ExerciseProblem`.  Returns a per-problem result dict for
    the report (schema v2 with v1 legacy fields preserved).

    ``parse_retry_budget_value`` / ``min_completed_value`` default to
    the env-resolved values (:func:`parse_retry_budget` /
    :func:`min_completed_per_problem`) so call sites needn't thread
    them explicitly — but tests can override.

    Stops early if the cost tracker exceeds the cap (a skip entry is
    appended for the un-attempted indices).
    """
    if parse_retry_budget_value is None:
        parse_retry_budget_value = parse_retry_budget()
    if min_completed_value is None:
        min_completed_value = min_completed_per_problem()
    counts = {
        AttemptStatus.PASSED.value: 0,
        AttemptStatus.FAILED.value: 0,
        AttemptStatus.PROVIDER_PARSE_ERROR.value: 0,
        AttemptStatus.ERRORED.value: 0,
    }
    per_attempt: List[Dict[str, Any]] = []
    for attempt_idx in range(attempts):
        if cost_tracker.exceeded():
            per_attempt.append({
                "attempt": attempt_idx,
                "skipped_reason": "cost_cap_reached",
            })
            break
        record = await _execute_one_attempt(
            problem, attempt_idx, dw_config, cost_tracker,
            parse_retry_budget_value,
        )
        per_attempt.append(record)
        status = record.get("status")
        if status in counts:
            counts[status] += 1
    completed = (
        counts[AttemptStatus.PASSED.value]
        + counts[AttemptStatus.FAILED.value]
    )
    fail_rate: Optional[float] = (
        (counts[AttemptStatus.FAILED.value] / completed)
        if completed > 0 else None
    )
    # Legacy v1 fields preserved alongside v2 structured fields.
    return {
        "problem_id": problem.problem_id,
        "kind": problem.kind.value,
        "attempts_requested": attempts,
        # ---- v1 legacy ----
        "attempts_completed": completed,
        "attempts_errored": (
            counts[AttemptStatus.PROVIDER_PARSE_ERROR.value]
            + counts[AttemptStatus.ERRORED.value]
        ),
        "passes": counts[AttemptStatus.PASSED.value],
        "fails": counts[AttemptStatus.FAILED.value],
        "measured_first_try_fail_rate": fail_rate,
        # ---- v2 structured ----
        "attempt_status_counts": dict(counts),
        "insufficient_samples": completed < min_completed_value,
        "min_completed_per_problem": min_completed_value,
        "parse_retry_budget": parse_retry_budget_value,
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

    # Stage 1 measurement-integrity knobs (resolved once + threaded
    # through every per-problem run so the report can record them
    # verbatim).
    parse_retry_budget_value = parse_retry_budget()
    min_completed_value = min_completed_per_problem()
    acceptance_threshold_value = acceptance_threshold()
    per_problem_floor_value = per_problem_floor()
    hardness_set = hardness_set_from_env()

    print("== L2 corpus hardness validator (Phase 1.5.D / schema v2) ==")
    print(f"  corpus:           {corpus}")
    print(f"  problems:         {[d.name for d in problem_dirs]}")
    print(f"  attempts/ea:      {args.attempts}")
    print(f"  max cost:         ${args.max_cost_usd:.2f}")
    print(f"  model:            {dw_config['model']}")
    print(f"  parse_retry:      {parse_retry_budget_value}")
    print(f"  min_completed:    {min_completed_value}")
    print(f"  accept_threshold: {acceptance_threshold_value:.2f}")
    print(f"  per_problem_floor:{per_problem_floor_value:.2f}")
    print(
        f"  hardness_set:     "
        f"{sorted(hardness_set) if hardness_set else '<empty>'}",
    )
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
            in_set = problem.problem_id in hardness_set
            tag = " [HARDNESS_SET]" if in_set else ""
            print(
                f"-> {problem.problem_id} "
                f"(kind={problem.kind.value}){tag}",
            )
            result = await validate_one_problem(
                problem, args.attempts, dw_config, cost_tracker,
                parse_retry_budget_value=parse_retry_budget_value,
                min_completed_value=min_completed_value,
            )
            results.append(result)
            rate = result["measured_first_try_fail_rate"]
            rate_s = f"{rate:.0%}" if rate is not None else "n/a"
            sc = result["attempt_status_counts"]
            print(
                f"   passed={sc['passed']} failed={sc['failed']} "
                f"parse_err={sc['provider_parse_error']} "
                f"errored={sc['errored']} "
                f"(fail_rate={rate_s}, "
                f"insufficient={result['insufficient_samples']})",
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

    # Compose acceptance gate over HARDNESS_SET (pure function;
    # NEVER raises) — operator-honest reason field surfaces in
    # the report when the gate is unevaluable.
    gate_mean, meets_gate, gate_diag = compute_acceptance_gate(
        hardness_set,
        results,
        threshold=acceptance_threshold_value,
        min_completed=min_completed_value,
        per_problem_floor_value=per_problem_floor_value,
    )

    report: Dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "provider_chain_used": PROVIDER_DW,
        "model": dw_config["model"],
        "attempts_requested": args.attempts,
        "max_cost_usd": args.max_cost_usd,
        "total_cost_usd": round(cost_tracker.total_usd, 6),
        # ---- v2 measurement-integrity knobs ----
        "parse_retry_budget": parse_retry_budget_value,
        "min_completed_per_problem": min_completed_value,
        "acceptance_threshold": acceptance_threshold_value,
        "per_problem_floor": per_problem_floor_value,
        "hardness_set": sorted(hardness_set),
        "hardness_set_mean_fail_rate": (
            round(gate_mean, 6) if gate_mean is not None else None
        ),
        "meets_acceptance_gate": meets_gate,
        "gate_diagnostic": gate_diag,
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
    # Operator-visible gate summary (also persisted in the report)
    if gate_mean is not None:
        print(
            f"HARDNESS_SET gate: mean_fail_rate={gate_mean:.2%} "
            f"vs threshold {acceptance_threshold_value:.0%} → "
            f"{'PASS' if meets_gate else 'FAIL'}",
        )
    else:
        print(
            f"HARDNESS_SET gate: unevaluable "
            f"({gate_diag.get('reason', '?')})",
        )
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
