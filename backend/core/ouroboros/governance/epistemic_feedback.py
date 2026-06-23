"""
epistemic_feedback.py — Pure stdlib leaf for the Adaptive Epistemic Feedback Matrix.

Spec: docs/superpowers/specs/2026-06-22-epistemic-feedback-and-lane-escalation.md §1.2
Task T1 (pure foundation): Hybrid Epistemic Diff + Parametric Degeneration + Pivot Verdict.

Constraints:
  - Pure stdlib only: ast, difflib, os  (no model calls, no I/O, no heavy imports)
  - from __future__ import annotations  (Python 3.9+ forward refs)
  - ASCII only
  - Fail-soft everywhere — never raises; returns best partial string or ""
  - Never exec/eval
"""
from __future__ import annotations

import ast
import difflib
import os


# ---------------------------------------------------------------------------
# Env helpers (fail-soft)
# ---------------------------------------------------------------------------

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (ValueError, TypeError):
        return default


def _env_float_strict(key: str, default: float) -> tuple[float, bool]:
    """Return (value, ok). ok=False means the env var was present but unparseable."""
    raw = os.environ.get(key)
    if raw is None:
        return default, True
    try:
        return float(raw), True
    except (ValueError, TypeError):
        return default, False


def _env_bool(key: str, default: bool) -> bool:
    val = os.environ.get(key, "").strip().lower()
    if val in ("1", "true", "yes", "on"):
        return True
    if val in ("0", "false", "no", "off"):
        return False
    return default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_failure_context(
    *,
    prior_src: object,
    failed_src: object,
    stderr: object,
    failing_tests: object,
    sub_goal_label: str = "",
) -> str:
    """Hybrid Epistemic Diff.

    Assembled in order:
      1. Safe AST probe of failed_src — prepends [SOVEREIGN SYNTAX FATAL] on SyntaxError.
      2. Labeled unified diff (prior_src vs failed_src), middle-truncated when large.
      3. Stderr trace tail + failing test ids.

    Fail-soft: never raises. On any error returns best partial str or "".
    """
    try:
        parts: list[str] = []

        # Coerce inputs to str safely
        prior_str: str = str(prior_src) if prior_src is not None else ""
        failed_str: str = str(failed_src) if failed_src is not None else ""
        stderr_str: str = str(stderr) if stderr is not None else ""
        sub_goal_str: str = str(sub_goal_label) if sub_goal_label else ""

        # Coerce failing_tests to a list of strings, fail-soft
        try:
            if failing_tests is None:
                tests_list: list[str] = []
            elif isinstance(failing_tests, (list, tuple)):
                tests_list = [str(t) for t in failing_tests]
            elif isinstance(failing_tests, str):
                tests_list = [failing_tests]
            else:
                tests_list = [str(t) for t in failing_tests]
        except Exception:
            tests_list = []

        # ------------------------------------------------------------------
        # Header line if label given
        # ------------------------------------------------------------------
        if sub_goal_str:
            parts.append(f"--- Epistemic Feedback: {sub_goal_str} ---")

        # ------------------------------------------------------------------
        # 1. Safe AST probe (fail-soft)
        # ------------------------------------------------------------------
        try:
            ast.parse(failed_str or "")
        except SyntaxError as e:
            try:
                lineno = e.lineno if e.lineno is not None else "?"
                msg = e.msg if e.msg is not None else str(e)
                parts.append(f"[SOVEREIGN SYNTAX FATAL] line={lineno} msg={msg}")
            except Exception:
                parts.append("[SOVEREIGN SYNTAX FATAL] line=? msg=unknown")
        except Exception:
            # Any other exception from ast.parse -> skip header
            pass

        # ------------------------------------------------------------------
        # 2. Labeled unified diff, middle-truncated
        # ------------------------------------------------------------------
        try:
            diff_max = _env_int("JARVIS_EPISTEMIC_DIFF_MAX_CHARS", 4000)
            prior_lines = (prior_str or "").splitlines(keepends=True)
            failed_lines = (failed_str or "").splitlines(keepends=True)
            raw_diff = "".join(
                difflib.unified_diff(
                    prior_lines,
                    failed_lines,
                    fromfile="Previous Stable Sub-Goal",
                    tofile="Current Failing Iteration",
                )
            )
            diff_block = _truncate_middle(raw_diff, diff_max)
            parts.append(diff_block)
        except Exception:
            parts.append("")

        # ------------------------------------------------------------------
        # 3. Stderr trace tail + failing test ids
        # ------------------------------------------------------------------
        try:
            trace_max = _env_int("JARVIS_EPISTEMIC_TRACE_MAX_CHARS", 2500)
            stderr_tail = stderr_str[-trace_max:] if len(stderr_str) > trace_max else stderr_str
            parts.append("--- FAILING TEST STDERR (tail) ---")
            parts.append(stderr_tail)
        except Exception:
            pass

        try:
            parts.append("--- FAILING TESTS ---")
            parts.append(", ".join(tests_list))
        except Exception:
            pass

        return "\n".join(parts)

    except Exception:
        return ""


def temperature_for_attempt(base_temp: float, repeated_signature_count: int) -> float:
    """Parametric Degeneration.

    Returns max(floor, base_temp * (decay ** max(0, repeated_signature_count))).
    repeated_signature_count=0 -> returns base_temp unchanged.
    Fail-soft -> returns base_temp on any env parse error or computation error.
    """
    try:
        decay, decay_ok = _env_float_strict("JARVIS_EPISTEMIC_TEMP_DECAY", 0.5)
        floor, floor_ok = _env_float_strict("JARVIS_EPISTEMIC_TEMP_FLOOR", 0.0)
        # If any env var is explicitly set but unparseable -> fail-soft: return base_temp
        if not decay_ok or not floor_ok:
            return float(base_temp)
        count = max(0, int(repeated_signature_count))
        result = float(base_temp) * (decay ** count)
        return max(floor, result)
    except Exception:
        try:
            return float(base_temp)
        except Exception:
            return 0.0


def pivot_verdict(repeated_signature_count: int, temp_at_floor: bool) -> bool:
    """Unresolvable-path detection.

    True iff temp_at_floor AND repeated_signature_count >= stall_passes.
    Fail-soft -> False.
    """
    try:
        stall_passes = _env_int("JARVIS_EPISTEMIC_PIVOT_PASSES", 2)
        return bool(temp_at_floor) and int(repeated_signature_count) >= stall_passes
    except Exception:
        return False


def epistemic_feedback_enabled() -> bool:
    """Returns True if JARVIS_EPISTEMIC_FEEDBACK_ENABLED is set to a truthy value (default true)."""
    return _env_bool("JARVIS_EPISTEMIC_FEEDBACK_ENABLED", True)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _truncate_middle(text: str, max_chars: int) -> str:
    """Truncate the MIDDLE of text so both ends survive.

    The start and end are each preserved up to half the budget; if the text
    fits within max_chars it is returned unchanged.
    """
    if len(text) <= max_chars:
        return text
    # Reserve budget: half for head, half for tail
    half = max_chars // 2
    head = text[:half]
    tail = text[-half:]
    elided = len(text) - 2 * half
    return f"{head}\n... <{elided} chars elided> ...\n{tail}"
