"""Ouroboros VERIFY regression gate.

Enforces thresholds on PatchBenchmarker metrics and provides file rollback
when thresholds are violated. All thresholds are env-driven.

This is a deterministic gate — no LLM calls, pure threshold comparison.
"""
import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# TODO(rsi-trigger): verify gate scan — validate TodoScanner trigger-tag e2e (task #63)

_MIN_PASS_RATE = float(os.environ.get("JARVIS_VERIFY_MIN_PASS_RATE", "1.0"))
_COVERAGE_DROP_MAX = float(os.environ.get("JARVIS_VERIFY_COVERAGE_DROP_MAX", "5.0"))
_MAX_COMPLEXITY_DELTA = float(os.environ.get("JARVIS_VERIFY_MAX_COMPLEXITY_DELTA", "2.0"))
_MAX_LINT_VIOLATIONS = int(os.environ.get("JARVIS_VERIFY_MAX_LINT_VIOLATIONS", "5"))


def enforce_verify_thresholds(
    result: "BenchmarkResult",
    baseline_coverage: Optional[float] = None,
) -> Optional[str]:
    """Check BenchmarkResult against regression thresholds.

    Returns error reason string if any threshold violated, None if all pass.
    """
    if result.error is not None:
        return f"Benchmark error: {result.error}"

    if result.timed_out:
        return "Benchmark timed out — metrics unreliable"

    # Non-Python target sentinel: PatchBenchmarker intentionally skipped
    # pytest/ruff/radon because target_files contained zero .py files.
    # All metric fields (pass_rate, coverage, complexity, lint) carry no
    # signal in this case — verification of infra/config/docs changes is
    # delegated to InfraApplicator and the orchestrator scoped-verify path.
    # Skipping all threshold checks here is the deterministic counterpart
    # to the orchestrator's `_verify_test_total == 0 → _verify_test_passed
    # = True` guard. Ref: bt-2026-04-11-213801 / op-019d7e7d (requirements.txt)
    # blocked the first sustained APPLY before this guard existed.
    if getattr(result, "non_python_target", False):
        return None

    if result.pass_rate < _MIN_PASS_RATE:
        return (
            f"Test regression: pass_rate={result.pass_rate:.2f} "
            f"< threshold={_MIN_PASS_RATE:.2f}"
        )

    if baseline_coverage is not None:
        min_coverage = baseline_coverage - _COVERAGE_DROP_MAX
        if result.coverage_pct < min_coverage:
            return (
                f"Coverage regression: coverage={result.coverage_pct:.1f}% "
                f"< baseline={baseline_coverage:.1f}% - {_COVERAGE_DROP_MAX:.1f}% "
                f"(min={min_coverage:.1f}%)"
            )

    if result.complexity_delta > _MAX_COMPLEXITY_DELTA:
        return (
            f"Complexity spike: delta={result.complexity_delta:.1f} "
            f"> threshold={_MAX_COMPLEXITY_DELTA:.1f}"
        )

    if result.lint_violations > _MAX_LINT_VIOLATIONS:
        return (
            f"Lint violations: {result.lint_violations} "
            f"> threshold={_MAX_LINT_VIOLATIONS}"
        )

    return None


def rollback_files(
    pre_apply_snapshots: Dict[str, str],
    target_files: List[str],
    repo_root: Path,
) -> None:
    """Restore files from pre-apply snapshots and delete new files."""
    for rel_path, original_content in pre_apply_snapshots.items():
        if rel_path.startswith("_"):
            continue
        abs_path = repo_root / rel_path
        try:
            abs_path.write_text(original_content, encoding="utf-8")
            restored = abs_path.read_text(encoding="utf-8")
            restored_hash = hashlib.sha256(restored.encode()).hexdigest()
            expected_hash = hashlib.sha256(original_content.encode()).hexdigest()
            if restored_hash != expected_hash:
                logger.error(
                    "[VerifyGate] Rollback verification failed for %s: "
                    "expected %s, got %s",
                    rel_path, expected_hash[:12], restored_hash[:12],
                )
        except OSError as exc:
            logger.error("[VerifyGate] Failed to restore %s: %s", rel_path, exc)

    for rel_path in target_files:
        if rel_path not in pre_apply_snapshots and not rel_path.startswith("_"):
            abs_path = repo_root / rel_path
            if abs_path.exists():
                try:
                    abs_path.unlink()
                    logger.info("[VerifyGate] Deleted new file on rollback: %s", rel_path)
                except OSError as exc:
                    logger.error("[VerifyGate] Failed to delete %s: %s", rel_path, exc)
