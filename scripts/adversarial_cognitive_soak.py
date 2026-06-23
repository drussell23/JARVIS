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

Two payloads ship (``--payload {merge_intervals,concurrency_lru}``); the default
is the Concurrency Gauntlet (a thread-safe TTL+LRU cache) because the round-1
merge-intervals payload was zero-shot solved -> the repair loop never fired. The
``--run`` path warms the model into VRAM first (``LocalPrimeClient.warmup``) so
the cold-start latency never trips a spurious timeout.

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
    """A coding sub-goal at (or past) the edge of a 7B's first-pass window.

    Two payloads ship (selectable via ``--payload``):

      * ``merge_intervals`` -- merge-overlapping-intervals WITH the half-open
        adjacency edge case (intervals that only TOUCH at an endpoint, e.g.
        [1,2] and [2,3], must merge into [1,3]). A 7B first draft typically uses
        ``s < last_end`` (strict) and silently botches adjacency. This proved
        *too easy* in round 1 (zero-shot solved -> the repair loop never fired).

      * ``concurrency_lru`` -- a thread-safe TTL+LRU cache with NON-BLOCKING
        background eviction. 7B first drafts almost universally (a) omit the
        ``threading.Lock`` around the OrderedDict mutations and/or (b) write a
        blocking ``while True: sleep`` eviction loop instead of a daemon thread.
        Both are caught deterministically by the test suite, so the epistemic
        repair -> pivot -> decompose loop is actually exercised UNDER FIRE.

    ``requirements`` is the per-payload requirements block (list of lines),
    keeping ``build_prompt`` payload-agnostic.
    """

    title: str
    description: str
    entry_symbol: str
    impl_filename: str
    test_filename: str
    tests: str
    system_prompt: str
    requirements: tuple
    decompose_focus: str = ""

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
            *list(self.requirements),
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


MERGE_INTERVALS_PAYLOAD = AdversarialPayload(
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
    requirements=(
        "- Define a top-level function named `merge_intervals`.",
        "- Handle the empty input case.",
        "- Sort the intervals first.",
        "- CRITICAL EDGE CASE: intervals that only TOUCH at an endpoint",
        "  (e.g. (1, 2) and (2, 3)) MUST merge into (1, 3) -- adjacency counts",
        "  as overlap. Use `<=`, not `<`.",
        "- Return a list of tuples.",
    ),
    decompose_focus=(
        " HYPER-ATOMIC FOCUS: get the adjacency edge case right first -- "
        "merge intervals that only touch at an endpoint using `<=`."
    ),
    system_prompt=(
        "You are a precise senior Python engineer. You write correct, minimal "
        "implementations and you reason carefully about edge cases before "
        "emitting code. Output a single Python code block only."
    ),
)


# ---------------------------------------------------------------------------
# Payload #2: the Concurrency Gauntlet (thread-safe TTL+LRU cache)
# ---------------------------------------------------------------------------
#
# Deliberately HARD for a 7B first pass. The test suite pins TWO bugs that 7B
# drafts almost universally hit:
#   (a) no threading.Lock around the OrderedDict mutations -> concurrent
#       get/put corrupts state (KeyError / changed-size-during-iteration);
#   (b) lazy-only TTL (expiry checked only on access) or a blocking
#       `while True: sleep` eviction loop -> the background-eviction +
#       non-blocking-constructor tests fail.
#
# Non-flaky by construction:
#   * thread-safety test uses a SHORT ttl so a (correct) background reaper is
#     ACTIVELY evicting while 8 threads hammer overlapping keys for ~1.5s; the
#     missing-lock multi-step read-modify-write (`if k in d` -> move_to_end ->
#     assign, racing the lazy/reaper pop) deterministically raises KeyError on
#     a buggy impl, while a locked impl is exception-free (verified 10/10);
#   * the invariant asserted is DETERMINISTIC (no exception AND len <= capacity),
#     never a timing-dependent exact value;
#   * TTL + non-blocking margins are generous (0.5s ttl, 1.5s wait, 5s ceiling)
#     so a correct impl is never flaky.

_LRU_TESTS = textwrap.dedent(
    '''
    from impl import TTLLRUCache

    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor


    def test_basic_get_put():
        c = TTLLRUCache(capacity=2, ttl_seconds=100.0)
        try:
            c.put("a", 1)
            c.put("b", 2)
            assert c.get("a") == 1
            assert c.get("b") == 2
            assert c.get("missing") is None
        finally:
            c.stop()


    def test_lru_capacity_eviction_order():
        c = TTLLRUCache(capacity=2, ttl_seconds=100.0)
        try:
            c.put("a", 1)
            c.put("b", 2)
            assert c.get("a") == 1  # touch "a" -> "b" is the LRU
            c.put("c", 3)           # inserting "c" must evict the LRU ("b")
            assert c.get("b") is None
            assert c.get("a") == 1
            assert c.get("c") == 3
            assert len(c) <= 2
        finally:
            c.stop()


    def test_thread_safety_no_corruption():
        capacity = 16
        # Short ttl: a correct background reaper is ACTIVELY evicting while the
        # threads concurrently mutate. A missing-lock impl races on the
        # multi-step read-modify-write and raises (KeyError / changed-size);
        # a locked impl is exception-free and never exceeds capacity.
        c = TTLLRUCache(capacity=capacity, ttl_seconds=0.05)
        n_threads = 8
        duration_s = 1.5
        errors = []
        invariant_violations = []
        stop = threading.Event()

        def worker(tid):
            try:
                i = 0
                while not stop.is_set():
                    k = (i + tid) % 24  # overlapping keys across threads -> churn
                    if i % 2 == 0:
                        c.put(k, tid * 1000 + i)
                    else:
                        c.get(k)
                    if len(c) > capacity:
                        invariant_violations.append(len(c))
                    i += 1
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        try:
            with ThreadPoolExecutor(max_workers=n_threads) as ex:
                futs = [ex.submit(worker, t) for t in range(n_threads)]
                time.sleep(duration_s)
                stop.set()
                for f in futs:
                    f.result()
            assert not errors, \\
                "concurrent access raised (missing lock?): " + "; ".join(errors[:5])
            assert not invariant_violations, (
                "len exceeded capacity under concurrency (corruption): "
                + str(invariant_violations[:5])
            )
            assert len(c) <= capacity
        finally:
            c.stop()


    def test_ttl_background_eviction_not_lazy():
        c = TTLLRUCache(capacity=10, ttl_seconds=0.5)
        try:
            c.put("x", 42)
            assert c.get("x") == 42  # present immediately
            # Wait WITHOUT accessing "x". A lazy-only TTL leaves it resident;
            # a background mechanism must have evicted it.
            time.sleep(1.5)
            assert len(c) == 0, (
                "entry not evicted by background mechanism (lazy-only TTL?): len="
                + str(len(c))
            )
        finally:
            c.stop()


    def test_constructor_and_ops_do_not_block():
        start = time.monotonic()
        c = TTLLRUCache(capacity=4, ttl_seconds=10.0)
        try:
            c.put("a", 1)
            assert c.get("a") == 1
        finally:
            c.stop()
        elapsed = time.monotonic() - start
        # A blocking `while True: sleep` eviction loop in __init__/put would hang
        # far past this. Generous ceiling; a correct impl finishes in ms.
        assert elapsed < 5.0, \\
            "construction + ops blocked (blocking eviction loop?): " + str(elapsed)
    '''
).strip()


CONCURRENCY_LRU_PAYLOAD = AdversarialPayload(
    title="Thread-safe TTL+LRU cache with non-blocking background eviction",
    description=(
        "Implement `class TTLLRUCache` with `__init__(self, capacity, "
        "ttl_seconds)`, `get(self, key)`, and `put(self, key, value)`. It is a "
        "bounded LRU cache (least-recently-used eviction when over capacity) "
        "with per-entry time-to-live. It MUST be thread-safe: concurrent get/put "
        "from many threads must not corrupt state or lose updates. TTL eviction "
        "MUST be performed by a NON-BLOCKING background mechanism (a daemon "
        "thread or async task) -- entries expire about `ttl_seconds` after "
        "insertion and are removed even if never accessed again. Do NOT use a "
        "blocking `while True: sleep(...)` loop on the construction path; the "
        "constructor must return promptly. Also provide a `stop(self)` method "
        "that cleanly halts the background mechanism (so callers do not leak "
        "threads)."
    ),
    entry_symbol="TTLLRUCache",
    impl_filename="impl.py",
    test_filename="test_impl.py",
    tests=_LRU_TESTS,
    requirements=(
        "- Define a top-level class named `TTLLRUCache` with the exact methods:",
        "  `__init__(self, capacity, ttl_seconds)`, `get(self, key)`,",
        "  `put(self, key, value)`, and `stop(self)`. Implement `__len__` too.",
        "- THREAD SAFETY: guard ALL mutations of the internal store with a",
        "  `threading.Lock`/`RLock`. Concurrent get/put from 8+ threads must",
        "  never raise and must never let the store exceed `capacity`.",
        "- LRU: `get` and `put` mark a key most-recently-used; eviction when over",
        "  capacity removes the least-recently-used entry.",
        "- TTL: entries expire ~`ttl_seconds` after insertion. Eviction MUST be",
        "  driven by a NON-BLOCKING background mechanism (daemon thread / async",
        "  task) so expired entries are removed even if never accessed -- NOT",
        "  lazily-only on access, and NOT via a blocking `while True: sleep`",
        "  loop that stalls construction.",
        "- Provide `stop(self)` to halt the background mechanism cleanly.",
        "- Use `collections.OrderedDict` (or equivalent) for LRU ordering.",
    ),
    decompose_focus=(
        " HYPER-ATOMIC FOCUS: first make it thread-safe -- wrap EVERY OrderedDict "
        "mutation in a single threading.Lock -- and replace any blocking eviction "
        "loop with a daemon thread that wakes periodically and reaps expired keys "
        "under that same lock."
    ),
    system_prompt=(
        "You are a precise senior Python engineer specializing in concurrent "
        "data structures. You reason carefully about thread-safety (locks around "
        "every shared-state mutation) and about non-blocking background work "
        "before emitting code. Output a single Python code block only."
    ),
)


# Registry for the --payload selector. Default is the Concurrency Gauntlet for
# this round (the merge-intervals payload was zero-shot solved last round).
PAYLOADS: Dict[str, AdversarialPayload] = {
    "merge_intervals": MERGE_INTERVALS_PAYLOAD,
    "concurrency_lru": CONCURRENCY_LRU_PAYLOAD,
}
DEFAULT_PAYLOAD = "concurrency_lru"

# Back-compat alias: existing callers/tests referenced ADVERSARIAL_PAYLOAD as
# the merge-intervals payload. Keep it pointing there so existing assertions
# stay valid; the --run default is the Concurrency Gauntlet via DEFAULT_PAYLOAD.
ADVERSARIAL_PAYLOAD = MERGE_INTERVALS_PAYLOAD


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


def _run_pytest_in_tempdir(
    impl_src: str,
    tests_src: str,
    *,
    timeout_s: int = 90,
    payload: "Optional[AdversarialPayload]" = None,
) -> Dict[str, Any]:
    """Write impl + tests to a tempdir and run REAL pytest as a subprocess.

    ``payload`` supplies the impl/test filenames (defaults to the back-compat
    ADVERSARIAL_PAYLOAD when not given).

    Returns {passed: bool, stdout: str, stderr: str, returncode: int}. Fail-soft:
    a missing impl / timeout / crash is reported as a non-pass, never raises.
    """
    payload = payload or ADVERSARIAL_PAYLOAD
    result: Dict[str, Any] = {"passed": False, "stdout": "", "stderr": "", "returncode": -1}
    if not impl_src:
        result["stderr"] = "empty implementation (model produced no code block)"
        return result
    try:
        with tempfile.TemporaryDirectory(prefix="adv_soak_") as d:
            impl_path = os.path.join(d, payload.impl_filename)
            test_path = os.path.join(d, payload.test_filename)
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


def _build_goal(payload: "Optional[AdversarialPayload]" = None) -> Any:
    payload = payload or ADVERSARIAL_PAYLOAD
    return types.SimpleNamespace(
        goal_id="adv-soak-" + payload.entry_symbol.lower().replace("_", "-"),
        title=payload.title,
        description=(payload.description + (payload.decompose_focus or "")),
        target_files=(payload.impl_filename,),
    )


# ---------------------------------------------------------------------------
# Soak narrative printing
# ---------------------------------------------------------------------------


def _say(line: str = "") -> None:
    print(line, flush=True)


# ---------------------------------------------------------------------------
# The cognitive loop driver
# ---------------------------------------------------------------------------


async def run_cognitive_soak(
    *,
    client: Any,
    max_repairs: int = 3,
    payload: "Optional[AdversarialPayload]" = None,
) -> Dict[str, Any]:
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

    payload = payload or ADVERSARIAL_PAYLOAD
    base_temp = float(os.environ.get("JARVIS_ADV_SOAK_BASE_TEMP", "0.7"))
    # Generous: the LRU payload's thread-safety test runs ~1.5s of real
    # concurrency on top of process startup, so keep a comfortable margin.
    pytest_timeout_s = int(os.environ.get("JARVIS_ADV_SOAK_PYTEST_TIMEOUT_S", "120"))
    # Generous per-generate budget so a slow-but-valid CPU generation (a cold
    # local 7B can take well over a minute on the first real tokens) is not cut
    # off. The harness warms the model first, but keep the ceiling high anyway.
    gen_timeout_s = float(os.environ.get("JARVIS_ADV_SOAK_GEN_TIMEOUT_S", "360"))

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

        out = _run_pytest_in_tempdir(
            impl_src, payload.tests, timeout_s=pytest_timeout_s, payload=payload
        )
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
                    _build_goal(payload),
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
    # Pin to the soak target regardless of env model default, and raise the
    # adaptive-timeout ceiling generously so a slow-but-valid CPU generation
    # from a cold/large 7B is not severed by the client's own timeout (the
    # harness also warms the model first). Reuses the existing LocalConfig env
    # knob (JARVIS_LOCAL_INFERENCE_TIMEOUT_MS) -- defaults to 360s here.
    ceiling_ms = int(os.environ.get("JARVIS_LOCAL_INFERENCE_TIMEOUT_MS", "360000"))
    cfg = LocalConfig(
        base_url=os.environ.get("JARVIS_LOCAL_MODEL_BASE_URL", "http://127.0.0.1:11434"),
        model_name=model,
        keep_alive_seconds=cfg.keep_alive_seconds,
        timeout_seed_ms=max(cfg.timeout_seed_ms, ceiling_ms),
        timeout_ceiling_ms=max(cfg.timeout_ceiling_ms, ceiling_ms),
        timeout_floor_ms=cfg.timeout_floor_ms,
        output_ratio=cfg.output_ratio,
        margin_sigma=cfg.margin_sigma,
        window_size=cfg.window_size,
        min_samples=cfg.min_samples,
        max_concurrency=cfg.max_concurrency,
        pool_limit=cfg.pool_limit,
    )
    return LocalPrimeClient(cfg)


async def _warmup_client(client: Any, *, timeout_s: float) -> bool:
    """Force the model into VRAM before the cognitive clock starts.

    Reuses the production ``LocalPrimeClient.warmup``. Logs the confirmed
    warm-load time or a fail-soft warning. Never raises.
    """
    import time as _time
    _say("-" * 72)
    _say(f"[soak] warming up model (forcing weights into VRAM, timeout={timeout_s:.0f}s) ...")
    t0 = _time.monotonic()
    try:
        ok = await client.warmup(timeout_s=timeout_s)
    except Exception as e:  # noqa: BLE001 -- fail-soft, never block the soak
        _say(f"[soak] warmup raised (fail-soft, proceeding cold): {e}")
        return False
    elapsed = _time.monotonic() - t0
    if ok:
        _say(f"[soak] warmup confirmed in {elapsed:.1f}s (model in memory)")
    else:
        _say(f"[soak] WARNING: warmup did not confirm in {elapsed:.1f}s "
             f"(timeout/unreachable) -- proceeding cold (fail-soft)")
    return bool(ok)


async def _amain(args: argparse.Namespace) -> int:
    if not gate_enabled():
        _say("REFUSED: set JARVIS_CHAOS_INJECTOR_ENABLED=true to run the soak.")
        return 2

    payload = PAYLOADS.get(args.payload, PAYLOADS[DEFAULT_PAYLOAD])

    if not args.run:
        _say("Dry mode: pass --run to drive the real local model. (Gate is ON.)")
        _say(f"Would target model={args.model} at "
             f"{os.environ.get('JARVIS_LOCAL_MODEL_BASE_URL', 'http://127.0.0.1:11434')}")
        _say(f"Selected payload: {args.payload} -- {payload.title}")
        return 0

    client = _build_real_client(args.model)
    try:
        # Warmup-first: eliminate the cold-start spurious timeout the first
        # round hit, so the cognitive loop starts against a warm model.
        warmup_timeout_s = float(os.environ.get("JARVIS_ADV_SOAK_WARMUP_TIMEOUT_S",
                                                str(args.warmup_timeout)))
        await _warmup_client(client, timeout_s=warmup_timeout_s)
        result = await run_cognitive_soak(
            client=client, max_repairs=args.max_repairs, payload=payload
        )
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
    parser.add_argument("--payload", choices=sorted(PAYLOADS.keys()),
                        default=DEFAULT_PAYLOAD,
                        help=f"Adversarial payload to drive (default: {DEFAULT_PAYLOAD}).")
    parser.add_argument("--warmup-timeout", type=float, default=180.0,
                        help="Seconds to wait for the VRAM warmup before the loop "
                             "(default: 180; env JARVIS_ADV_SOAK_WARMUP_TIMEOUT_S).")
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        _say("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
