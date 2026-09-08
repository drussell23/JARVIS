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

Why it covers PRODUCTION candidates too (2026-09-08)
----------------------------------------------------
The gate was scoped to test-authoring candidates on the argument that a
red test mapped to production code "may be exactly what the op must fix".
That case is what ``protected`` (``acceptance_names``: the declared target
symbols and every ``test_*`` token of the description) already guards — an
acceptance test is never excluded. Without the baseline, a production file
whose test module carries ambient reds can never be changed: the first
swarm candidate that edited ``candidate_generator.py`` correctly (soak
2026-09-08 00:54Z) failed VALIDATE on 17 tests that fail identically at
HEAD (they reach live provider paths). The verdict was, again, never about
the candidate.

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


def _test_name(node_id: str) -> str:
    """``tests/t.py::Cls::test_x[param]`` -> ``test_x``."""
    tail = str(node_id).rsplit("::", 1)[-1]
    return tail.split("[", 1)[0].strip()


def acceptance_names(description: str, target_symbols: Iterable[str] = ()) -> frozenset:
    """Test function names the op is explicitly asked to deliver or fix —
    declared target symbols plus every ``test_*`` token in the description.
    An ambient-red id carrying one of these names is the op's ACCEPTANCE
    criterion and is never excluded."""
    import re as _re
    names = {str(s).strip() for s in (target_symbols or ()) if str(s).strip()}
    names.update(_re.findall(r"\btest_[A-Za-z0-9_]+", description or ""))
    return frozenset(names)


def candidate_is_test_authoring(all_files: Iterable[Tuple[str, str]]) -> bool:
    """True when every candidate file is a test module. Kept for callers
    that classify a candidate; the differential gate no longer keys on it
    — a production candidate's acceptance tests are protected by name
    instead (see the module docstring, 2026-09-08)."""
    try:
        from backend.core.ouroboros.governance.ast_signature_anchor import is_test_path
    except Exception:  # noqa: BLE001
        return False
    files = [fp for fp, _ in all_files]
    return bool(files) and all(is_test_path(fp) for fp in files)


def ambient_red_ids(ctx: Any) -> Tuple[str, ...]:
    """The ambient-red ids the op carries: the explicit context field when
    stamped, else the ValidationResult riding ``ctx.validation`` (the
    validate core returns a result, not the context it stamped — the field
    was never populated and VERIFY deselected nothing, 2026-09-08)."""
    ids = tuple(str(t) for t in (getattr(ctx, "ambient_red_tests", ()) or ()) if str(t))
    if ids:
        return ids
    vr = getattr(ctx, "validation", None)
    return tuple(str(t) for t in (getattr(vr, "ambient_red_tests", ()) or ()) if str(t))


def apply_context_baseline(multi: Any, ctx: Any) -> Tuple[Any, Tuple[str, ...]]:
    """``apply_differential`` against the ambient verdict the op CARRIES
    (``ctx.ambient_red_tests``, stamped by VALIDATE) with the op's acceptance
    tests protected — the ONE way VERIFY reads the baseline, so VALIDATE
    and VERIFY can never disagree about which reds are the environment's.
    Identity when the op carries none."""
    baseline = frozenset(ambient_red_ids(ctx))
    if not baseline:
        return multi, ()
    return apply_differential(
        multi, baseline,
        protected=acceptance_names(
            getattr(ctx, "description", "") or "", getattr(ctx, "target_symbols", ()) or (),
        ),
    )


def apply_differential(
    multi: Any, baseline: frozenset, *, protected: Iterable[str] = (),
    test_authoring: bool = True,
) -> Tuple[Any, Tuple[str, ...]]:
    """Exclude failures that are entirely ambient (every failing id was red
    at baseline and none of them an acceptance test) from the verdict. A
    residual failure keeps the adapter result untouched. ``test_authoring``
    is the legacy opt-out (False disables the gate for a caller that must
    judge by every test); the gate itself no longer keys on it. Returns
    ``(multi, ignored_ids)``."""
    if baseline is None or not baseline or getattr(multi, "passed", False) or not test_authoring:
        return multi, ()
    protected_names = {str(p) for p in (protected or ())}
    new_results = []
    ignored = []
    for r in getattr(multi, "adapter_results", ()) or ():
        failed = _failed_ids(r)
        if getattr(r, "passed", False) or _timed_out(r) or not failed:
            new_results.append(r)
            continue
        residual = [t for t in failed if t not in baseline or _test_name(t) in protected_names]
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
