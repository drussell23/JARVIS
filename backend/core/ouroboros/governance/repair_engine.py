"""L2 Iterative Self-Repair Loop Engine

Provides configuration, runtime budget tracking, and FSM-driven repair orchestration
for Ouroboros governance operations that fail validation.

The repair loop implements:
- Multi-iteration classification and fix generation
- Test-driven repair with failure class tracking
- Adaptive timeout and cost budgeting
- Flaky test detection and confirmation
- Progress tracking and early termination

This module is structured as:
1. **RepairBudget** - Immutable configuration loaded from environment
2. **RepairEngine** - FSM executor and repair orchestration (stub, Task 5)
3. **Repair context & telemetry** - Ledger and audit trail
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from typing import ClassVar, Dict, Optional

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepairBudget:
    """Immutable repair loop resource and iteration budget.

    Loaded from environment variables at system startup. All fields are frozen
    and cannot be mutated after construction.

    Parameters
    ----------
    enabled : bool
        Whether L2 iterative repair is enabled. Set via ``JARVIS_L2_ENABLED``
        (default: ``False``).
    max_iterations : int
        Maximum repair iterations before hard stop. Set via ``JARVIS_L2_MAX_ITERS``
        (default: ``5``).
    timebox_s : float
        Total wall-clock time budget for entire repair loop in seconds.
        Set via ``JARVIS_L2_TIMEBOX_S`` (default: ``120.0``).
    min_deadline_remaining_s : float
        Minimum remaining deadline before stopping repair. If operation deadline
        is less than this value, repair halts. Set via ``JARVIS_L2_MIN_DEADLINE_S``
        (default: ``10.0``).
    per_iteration_test_timeout_s : float
        Test execution timeout per iteration in seconds. Set via ``JARVIS_L2_ITER_TEST_TIMEOUT_S``
        (default: ``60.0``).
    max_diff_lines : int
        Maximum diff lines per candidate. Set via ``JARVIS_L2_MAX_DIFF_LINES``
        (default: ``150``).
    max_files_changed : int
        Maximum files changed per candidate. Set via ``JARVIS_L2_MAX_FILES_CHANGED``
        (default: ``3``).
    max_total_validation_runs : int
        Maximum total validation/test runs across all iterations.
        Set via ``JARVIS_L2_MAX_VALIDATION_RUNS`` (default: ``8``).
    no_progress_streak_kill : int
        Kill repair after N consecutive failures with no progress.
        Set via ``JARVIS_L2_NO_PROGRESS_KILL`` (default: ``2``).
    max_class_retries : Dict[str, int]
        Max retries per failure class. Keys: ``"syntax"``, ``"test"``, ``"flake"``, ``"env"``.
        Set via ``JARVIS_L2_CLASS_RETRIES_JSON`` (default: ``{"syntax":2,"test":3,"flake":2,"env":1}``).
    flake_confirm_reruns : int
        How many times to rerun a passing test to confirm it's not flaky.
        Set via ``JARVIS_L2_FLAKE_RERUNS`` (default: ``1``).
    """

    enabled: bool = False
    max_iterations: int = 5
    timebox_s: float = 120.0
    min_deadline_remaining_s: float = 10.0
    per_iteration_test_timeout_s: float = 60.0
    max_diff_lines: int = 150
    max_files_changed: int = 3
    max_total_validation_runs: int = 8
    no_progress_streak_kill: int = 2
    max_class_retries: Dict[str, int] = dataclasses.field(
        default_factory=lambda: {"syntax": 2, "test": 3, "flake": 2, "env": 1}
    )
    flake_confirm_reruns: int = 1

    @classmethod
    def from_env(cls) -> RepairBudget:
        """Load RepairBudget configuration from environment variables.

        All environment variables are optional. Missing variables fall back to
        defaults. For ``JARVIS_L2_CLASS_RETRIES_JSON``, parse errors log a
        warning and use the default dict.

        Returns
        -------
        RepairBudget
            Frozen budget instance with values read from environment.
        """
        # Boolean parsing: accept "true" (case-insensitive)
        enabled_str = os.environ.get("JARVIS_L2_ENABLED", "false").lower()
        enabled = enabled_str == "true"

        # Integer parsing
        max_iterations = int(
            os.environ.get("JARVIS_L2_MAX_ITERS", cls.__dataclass_fields__["max_iterations"].default)
        )
        max_diff_lines = int(
            os.environ.get("JARVIS_L2_MAX_DIFF_LINES", cls.__dataclass_fields__["max_diff_lines"].default)
        )
        max_files_changed = int(
            os.environ.get("JARVIS_L2_MAX_FILES_CHANGED", cls.__dataclass_fields__["max_files_changed"].default)
        )
        max_total_validation_runs = int(
            os.environ.get("JARVIS_L2_MAX_VALIDATION_RUNS", cls.__dataclass_fields__["max_total_validation_runs"].default)
        )
        no_progress_streak_kill = int(
            os.environ.get("JARVIS_L2_NO_PROGRESS_KILL", cls.__dataclass_fields__["no_progress_streak_kill"].default)
        )
        flake_confirm_reruns = int(
            os.environ.get("JARVIS_L2_FLAKE_RERUNS", cls.__dataclass_fields__["flake_confirm_reruns"].default)
        )

        # Float parsing
        timebox_s = float(
            os.environ.get("JARVIS_L2_TIMEBOX_S", cls.__dataclass_fields__["timebox_s"].default)
        )
        min_deadline_remaining_s = float(
            os.environ.get("JARVIS_L2_MIN_DEADLINE_S", cls.__dataclass_fields__["min_deadline_remaining_s"].default)
        )
        per_iteration_test_timeout_s = float(
            os.environ.get("JARVIS_L2_ITER_TEST_TIMEOUT_S", cls.__dataclass_fields__["per_iteration_test_timeout_s"].default)
        )

        # JSON parsing with fallback to default
        max_class_retries_json = os.environ.get("JARVIS_L2_CLASS_RETRIES_JSON")
        if max_class_retries_json:
            try:
                max_class_retries = json.loads(max_class_retries_json)
            except (json.JSONDecodeError, ValueError) as e:
                _logger.warning(
                    "Failed to parse JARVIS_L2_CLASS_RETRIES_JSON: %s, using defaults",
                    e,
                )
                max_class_retries = cls.__dataclass_fields__["max_class_retries"].default_factory()
        else:
            max_class_retries = cls.__dataclass_fields__["max_class_retries"].default_factory()

        return cls(
            enabled=enabled,
            max_iterations=max_iterations,
            timebox_s=timebox_s,
            min_deadline_remaining_s=min_deadline_remaining_s,
            per_iteration_test_timeout_s=per_iteration_test_timeout_s,
            max_diff_lines=max_diff_lines,
            max_files_changed=max_files_changed,
            max_total_validation_runs=max_total_validation_runs,
            no_progress_streak_kill=no_progress_streak_kill,
            max_class_retries=max_class_retries,
            flake_confirm_reruns=flake_confirm_reruns,
        )
