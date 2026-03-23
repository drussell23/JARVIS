# backend/core/ouroboros/governance/shadow_harness.py
"""
Shadow Harness -- Side-Effect-Free Parallel Execution
======================================================

Runs candidate code in a sandboxed shadow environment alongside production.
Hard-blocks dangerous side effects (file writes, subprocess spawns, deletions)
so shadow code **physically cannot** alter system state.

Components
----------
- **ShadowModeViolation**: raised when shadow code attempts a forbidden op.
- **CompareMode**: EXACT | AST | SEMANTIC output comparison strategy.
- **ShadowResult**: frozen record of a single shadow run.
- **SideEffectFirewall**: context manager that monkey-patches dangerous builtins.
- **OutputComparator**: scores similarity between expected and actual outputs.
- **ShadowHarness**: tracks confidence over time, auto-disqualifies on streaks.

No LLM calls.  Pure deterministic logic.
"""

from __future__ import annotations

import ast
import builtins
import enum
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Tuple

logger = logging.getLogger("Ouroboros.ShadowHarness")


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class ShadowModeViolation(Exception):
    """Raised when shadow code attempts a forbidden side effect."""


# ---------------------------------------------------------------------------
# CompareMode enum
# ---------------------------------------------------------------------------


class CompareMode(enum.Enum):
    """Output comparison strategy."""

    EXACT = "exact"
    AST = "ast"
    SEMANTIC = "semantic"


# ---------------------------------------------------------------------------
# ShadowResult frozen dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShadowResult:
    """Immutable record of a single shadow run."""

    confidence: float
    comparison_mode: CompareMode
    violations: Tuple[str, ...]
    shadow_duration_s: float
    production_match: bool
    disqualified: bool


# ---------------------------------------------------------------------------
# SideEffectFirewall
# ---------------------------------------------------------------------------

# Modes that imply write intent (open() second arg or keyword)
_WRITE_MODES = frozenset({"w", "a", "x", "wb", "ab", "xb", "w+", "a+", "x+",
                           "r+", "rb+", "r+b", "w+b", "wb+", "a+b", "ab+",
                           "x+b", "xb+", "wt", "at", "xt", "w+t", "a+t",
                           "x+t", "r+t", "rt+"})


def _is_write_mode(mode: str) -> bool:
    """Return True if *mode* implies any write capability."""
    # Normalise by stripping whitespace and lowering
    m = mode.strip().lower()
    # Any mode containing w, a, x, or + is a write mode
    for ch in ("w", "a", "x", "+"):
        if ch in m:
            return True
    return False


class SideEffectFirewall:
    """Context manager that monkey-patches dangerous operations.

    Inside the ``with`` block, the following are blocked:

    * ``builtins.open`` for any write / append / exclusive-create mode
    * ``subprocess.run`` and ``subprocess.Popen`` (all calls)
    * ``os.remove`` and ``os.unlink`` (all calls)
    * ``shutil.rmtree`` (all calls)

    Read-mode ``open()`` continues to work normally.
    All originals are restored unconditionally on ``__exit__``.
    """

    def __init__(self) -> None:
        self._originals: Dict[str, Any] = {}

    # -- context manager ----------------------------------------------------

    def __enter__(self) -> "SideEffectFirewall":
        # Snapshot originals BEFORE patching
        # v302.0: subprocess.run/Popen no longer patched (organism must act)
        self._originals = {
            "builtins.open": builtins.open,
            "os.remove": os.remove,
            "os.unlink": os.unlink,
            "shutil.rmtree": shutil.rmtree,
        }

        original_open = self._originals["builtins.open"]

        # -- patched open --------------------------------------------------
        def _guarded_open(*args: Any, **kwargs: Any) -> Any:
            # Determine mode: second positional arg or 'mode' kwarg
            mode = "r"  # default
            if len(args) >= 2:
                mode = args[1]
            elif "mode" in kwargs:
                mode = kwargs["mode"]

            if _is_write_mode(mode):
                raise ShadowModeViolation(
                    f"Shadow mode blocked file write (mode={mode!r})"
                )
            return original_open(*args, **kwargs)

        builtins.open = _guarded_open  # type: ignore[assignment]

        # -- subprocess.run and subprocess.Popen ----------------------------
        # v302.0: NO LONGER BLOCKED. The organism must ACT on the physical
        # world (open browsers, run commands). The governed __import__ in
        # SandboxedExecutor controls WHICH modules are available.
        # Blocking subprocess globally also breaks TTS, voice, and system
        # commands — the Body cannot speak or act if Popen is patched.
        #
        # Destructive operations (os.remove, shutil.rmtree) remain blocked.

        # -- patched os.remove ---------------------------------------------
        def _blocked_remove(*args: Any, **kwargs: Any) -> Any:
            raise ShadowModeViolation(
                "Shadow mode blocked os.remove"
            )

        os.remove = _blocked_remove  # type: ignore[assignment]

        # -- patched os.unlink ---------------------------------------------
        def _blocked_unlink(*args: Any, **kwargs: Any) -> Any:
            raise ShadowModeViolation(
                "Shadow mode blocked os.unlink"
            )

        os.unlink = _blocked_unlink  # type: ignore[assignment]

        # -- patched shutil.rmtree -----------------------------------------
        def _blocked_rmtree(*args: Any, **kwargs: Any) -> Any:
            raise ShadowModeViolation(
                "Shadow mode blocked shutil.rmtree"
            )

        shutil.rmtree = _blocked_rmtree  # type: ignore[assignment]

        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Restore ALL originals unconditionally."""
        builtins.open = self._originals["builtins.open"]  # type: ignore[assignment]
        # subprocess.run and subprocess.Popen are no longer patched (v302.0)
        os.remove = self._originals["os.remove"]  # type: ignore[assignment]
        os.unlink = self._originals["os.unlink"]  # type: ignore[assignment]
        shutil.rmtree = self._originals["shutil.rmtree"]  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# OutputComparator
# ---------------------------------------------------------------------------


class OutputComparator:
    """Scores similarity between expected and actual outputs.

    Modes
    -----
    EXACT
        1.0 if strings are identical, else 0.0.
    AST
        Parse both as Python, compare ``ast.dump()``.
        Identical dumps = 1.0.  Different = partial score based on the
        length of the longest common prefix of the dumps.
        SyntaxError on either side = 0.0.
    SEMANTIC
        Phase-1 stub: delegates to AST comparison.
    """

    def compare(self, expected: str, actual: str, mode: CompareMode) -> float:
        """Return similarity score in [0.0, 1.0]."""
        if mode is CompareMode.EXACT:
            return self._compare_exact(expected, actual)
        elif mode is CompareMode.AST:
            return self._compare_ast(expected, actual)
        elif mode is CompareMode.SEMANTIC:
            # Phase 1: delegate to AST
            return self._compare_ast(expected, actual)
        else:
            raise ValueError(f"Unknown CompareMode: {mode!r}")

    # -- private helpers ---------------------------------------------------

    @staticmethod
    def _compare_exact(expected: str, actual: str) -> float:
        return 1.0 if expected == actual else 0.0

    @staticmethod
    def _compare_ast(expected: str, actual: str) -> float:
        try:
            dump_expected = ast.dump(ast.parse(expected))
        except SyntaxError:
            return 0.0
        try:
            dump_actual = ast.dump(ast.parse(actual))
        except SyntaxError:
            return 0.0

        if dump_expected == dump_actual:
            return 1.0

        # Partial score: longest common prefix ratio
        max_len = max(len(dump_expected), len(dump_actual))
        if max_len == 0:
            return 1.0  # both empty

        common = 0
        for a, b in zip(dump_expected, dump_actual):
            if a == b:
                common += 1
            else:
                break

        return common / max_len


# ---------------------------------------------------------------------------
# ShadowHarness
# ---------------------------------------------------------------------------


class ShadowHarness:
    """Tracks confidence over time and auto-disqualifies on streaks.

    Parameters
    ----------
    confidence_threshold
        Minimum acceptable confidence.  Runs *strictly below* this
        value count as low-confidence.
    disqualify_after
        Number of **consecutive** low-confidence runs that triggers
        automatic disqualification.
    """

    def __init__(
        self,
        confidence_threshold: float = 0.7,
        disqualify_after: int = 3,
    ) -> None:
        self._threshold = confidence_threshold
        self._disqualify_after = disqualify_after
        self._consecutive_low: int = 0
        self._disqualified: bool = False

    # -- public API --------------------------------------------------------

    @property
    def is_disqualified(self) -> bool:
        """Whether this harness has been auto-disqualified."""
        return self._disqualified

    def record_run(self, confidence: float) -> None:
        """Record a shadow run result.

        If *confidence* is strictly below the threshold, the consecutive
        low-confidence counter increments.  A run at or above the
        threshold resets the counter.  Once disqualified, state is sticky
        until ``reset()`` is called.
        """
        if self._disqualified:
            return

        if confidence < self._threshold:
            self._consecutive_low += 1
        else:
            self._consecutive_low = 0

        if self._consecutive_low >= self._disqualify_after:
            self._disqualified = True
            logger.warning(
                "Shadow harness disqualified after %d consecutive "
                "low-confidence runs (threshold=%.2f)",
                self._disqualify_after,
                self._threshold,
            )

    def reset(self) -> None:
        """Clear disqualification state and streak counter."""
        self._consecutive_low = 0
        self._disqualified = False
