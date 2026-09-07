"""Differential VALIDATE gate — a candidate is judged by what it CHANGES.

Why this exists (2026-09-07)
----------------------------
Goal ``ov-tier1-multi-003`` looped for hours: every candidate for
``tests/governance/comms/karen_synth/test_speech_provider.py`` failed
VALIDATE on ``test_dw_provider_yields_completion_text`` — a test that already
existed and that FAILS WITHOUT ANY CHANGE inside the soak's sandbox
(``DWSpeechProvider.source`` routes through ``rt_gate``, whose local tier is
PRIMARY when ``JARVIS_LOCAL_PRIME_ENABLED`` is set in ``.env``; the live
local model answers before the test's ``_FakeDW`` stub is consulted). The
harness blamed the candidate, the lesson memory recorded a false lesson
(x23, escalated to a hard constraint), and no amount of generation could
converge, because the verdict was never about the candidate.

The gate runs the target test files that ALREADY EXIST in the candidate
tree BEFORE the candidate is materialized (same sandbox, same env, bounded
budget) and remembers which test ids are red at baseline. After the
candidate run, failures whose ids are all within that baseline set are
AMBIENT — excluded from the verdict, logged, and recorded as an
``ambient_red`` lesson so the organism (and the operator via LESSONS.md)
knows the environment, not the candidate, is red. Any residual failure —
a new test that fails, or a previously-green test that broke — still fails
VALIDATE exactly as before. A baseline that cannot be established (timeout,
runner fault, adapter crash with no test ids) ignores nothing: fail-safe
toward the stricter verdict.

Env: JARVIS_DIFFERENTIAL_VALIDATE_ENABLED (true),
JARVIS_DIFFERENTIAL_VALIDATE_BUDGET_FRACTION (0.3 of the remaining budget),
JARVIS_DIFFERENTIAL_VALIDATE_MIN_S (5.0).
"""
from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.Orchestrator")

_ENV_ENABLED = "JARVIS_DIFFERENTIAL_VALIDATE_ENABLED"
_ENV_FRACTION = "JARVIS_DIFFERENTIAL_VALIDATE_BUDGET_FRACTION"
_ENV_MIN_S = "JARVIS_DIFFERENTIAL_VALIDATE_MIN_S"

AMBIENT_ERROR_CLASS = "ambient_red"


def differential_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "true").strip().lower() not in ("0", "false", "no", "off")


def baseline_budget_s(remaining_s: float) -> float:
    """A bounded slice of the remaining VALIDATE budget for the baseline
    run: ``fraction * remaining`` floored at ``min_s`` but never more than
    the remaining budget itself."""
    try:
        frac = float(os.environ.get(_ENV_FRACTION, "").strip() or 0.3)
    except ValueError:
        frac = 0.3
    frac = min(0.9, max(0.05, frac))
    try:
        floor = float(os.environ.get(_ENV_MIN_S, "").strip() or 5.0)
    except ValueError:
        floor = 5.0
    rem = max(0.0, float(remaining_s))
    return max(0.0, min(rem, max(floor, frac * rem)))


def existing_runnable_targets(
    all_files: Iterable[Tuple[str, str]], tree_root: Path, runnable_suffixes: Iterable[str],
) -> Tuple[Path, ...]:
    """Candidate target files that ALREADY exist in the candidate tree and
    are runnable — the only files a baseline can be measured on."""
    out = []
    suffixes = set(runnable_suffixes)
    for fp, _content in all_files:
        rel = Path(str(fp))
        if rel.is_absolute():
            continue
        p = tree_root / rel
        try:
            if p.is_file() and p.suffix in suffixes:
                out.append(p)
        except OSError:
            continue
    return tuple(out)


async def baseline_failed_tests(
    runner: Any, files: Sequence[Path], *, sandbox_dir: Path, budget_s: float,
    op_id: str, original_paths: Optional[Dict[Path, Path]] = None,
) -> frozenset:
    """Test ids red BEFORE the candidate lands. ``frozenset()`` when the
    baseline is green or cannot be established (fail-safe: nothing is
    ignored). NEVER raises."""
    if not files or budget_s <= 0:
        return frozenset()
    try:
        multi = await runner.run(
            changed_files=tuple(files), sandbox_dir=sandbox_dir,
            timeout_budget_s=budget_s, op_id=op_id,
            original_paths=original_paths or {p: p for p in files},
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe toward the stricter verdict
        logger.debug("[Validation] differential baseline unavailable (%s)", exc)
        return frozenset()
    if getattr(multi, "passed", False):
        return frozenset()
    red = set()
    for r in getattr(multi, "adapter_results", ()) or ():
        if getattr(r, "passed", False) or _timed_out(r):
            continue
        red.update(t for t in _failed_ids(r) if t)
    return frozenset(red)


def _test_result(r: Any) -> Any:
    """The adapter's per-run test result (``AdapterResult.test_result``);
    the adapter itself when a flat shape is handed in."""
    return getattr(r, "test_result", None) or r


def _failed_ids(r: Any) -> Tuple[str, ...]:
    return tuple(getattr(_test_result(r), "failed_tests", ()) or ())


def _timed_out(r: Any) -> bool:
    return bool(getattr(_test_result(r), "timed_out", False))


def _mark_passed(r: Any) -> Any:
    """A copy of the adapter result with the ambient failures cleared —
    only fields the dataclass actually has are touched."""
    tr = getattr(r, "test_result", None)
    if tr is not None and dataclasses.is_dataclass(tr):
        names = {f.name for f in dataclasses.fields(tr)}
        kw = {k: v for k, v in {"passed": True, "failed": 0, "failed_tests": ()}.items() if k in names}
        r = dataclasses.replace(r, test_result=dataclasses.replace(tr, **kw))
    names = {f.name for f in dataclasses.fields(r)} if dataclasses.is_dataclass(r) else set()
    kw = {k: v for k, v in {"passed": True, "failure_class": "none", "failed": 0, "failed_tests": ()}.items() if k in names}
    return dataclasses.replace(r, **kw) if kw else r


def apply_differential(multi: Any, baseline: frozenset) -> Tuple[Any, Tuple[str, ...]]:
    """Exclude failures that are entirely ambient (every failing id was red
    at baseline) from the verdict. A residual failure keeps the adapter
    result untouched. Returns ``(multi, ignored_ids)``."""
    if baseline is None or not baseline or getattr(multi, "passed", False):
        return multi, ()
    new_results = []
    ignored = []
    for r in getattr(multi, "adapter_results", ()) or ():
        failed = _failed_ids(r)
        if getattr(r, "passed", False) or _timed_out(r) or not failed:
            new_results.append(r)
            continue
        residual = [t for t in failed if t not in baseline]
        if residual:
            new_results.append(r)
            continue
        ignored.extend(failed)
        new_results.append(_mark_passed(r))
    if not ignored:
        return multi, ()
    passed = all(getattr(r, "passed", False) for r in new_results)
    dominant = next((r for r in new_results if not getattr(r, "passed", False)), None)
    return (
        dataclasses.replace(multi, passed=passed, adapter_results=tuple(new_results), dominant_failure=dominant),
        tuple(ignored),
    )
