"""Dynamic Transport Router — per-op RT-vs-BATCH selection by payload size.

Replaces the all-or-nothing transport dichotomy (blanket force-batch OR the
force-RT workaround) with a heuristic: a localized fix streams (RT — fast TTFT,
proven ~0.78s, low latency), a large/multi-file refactor batches (the DW batch
API, viable now that the webhook-vs-poll retrieve-hang is fixed). The op's own
shape decides — no hardcode.

Consulted by ``doubleword_provider._slice36_should_force_batch`` ONLY for the
"Claude-unavailable + healthy stream" case; the rupture-driven slices (170 ledger
failover, 172 predictive, 179 warm-boot) still force batch on a DEGRADED stream
regardless (a broken wire must batch no matter the size).

Size signal (first available wins), reusing existing primitives — no new scan:
  1. ``providers._max_target_line_count`` over the op's target files (the SAME
     signal Slice-235 + the tool-loop op-weight read) when a repo root resolves.
  2. target-file COUNT (multi-file ⇒ architectural ⇒ batch).
  3. ``task_complexity`` tier (heavy/complex ⇒ batch).

Gated by ``JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED`` (default OFF → legacy static
decision, byte-identical). Pure + fail-soft: any error → False (prefer RT, the
low-latency default). NEVER raises.
"""
from __future__ import annotations

import os
from typing import Any

_ENABLED_ENV = "JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED"
_LINE_THRESHOLD_ENV = "JARVIS_DW_BATCH_PAYLOAD_LINE_THRESHOLD"
_FILE_COUNT_THRESHOLD_ENV = "JARVIS_DW_BATCH_PAYLOAD_FILE_THRESHOLD"

# A localized fix (version bump, one-liner, small function) is well under these.
# A large/multi-file refactor exceeds them → batch.
_DEFAULT_LINE_THRESHOLD = 400
_DEFAULT_FILE_THRESHOLD = 3
_HEAVY_COMPLEXITIES = frozenset({"heavy", "complex", "architectural", "heavy_code"})


def dynamic_transport_enabled() -> bool:
    """Master gate (default OFF → legacy static transport decision). NEVER raises."""
    try:
        return os.environ.get(_ENABLED_ENV, "").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip() or default)
        return v if v > 0 else default
    except (ValueError, TypeError):
        return default


def should_batch_by_payload(context: Any, model_id: str = "") -> bool:
    """True ⇒ route THIS op to BATCH (large/multi-file); False ⇒ RT stream
    (localized). Size-aware, reachability-independent, fail-soft → False (RT).

    Only meaningful when ``dynamic_transport_enabled()``; the caller checks that.
    NEVER raises."""
    try:
        targets = tuple(getattr(context, "target_files", ()) or ())

        # (2) multi-file ⇒ architectural ⇒ batch.
        if len(targets) >= _int_env(_FILE_COUNT_THRESHOLD_ENV, _DEFAULT_FILE_THRESHOLD):
            return True

        # (1) largest target file's line count (reuses the Slice-235 size primitive).
        line_threshold = _int_env(_LINE_THRESHOLD_ENV, _DEFAULT_LINE_THRESHOLD)
        repo_root = (
            getattr(context, "primary_repo_root", None)
            or getattr(context, "repo_root", None)
            or os.environ.get("JARVIS_PROJECT_ROOT")
            or "."
        )
        try:
            from backend.core.ouroboros.governance.providers import (
                _max_target_line_count as _max_lines,
            )
            lines = _max_lines(targets, repo_root)
            if isinstance(lines, int) and lines >= line_threshold:
                return True
        except Exception:  # noqa: BLE001 — size probe is best-effort
            pass

        # (3) complexity tier fallback (heavy/architectural ⇒ batch even if the
        # line probe missed, e.g. new-file creation with no on-disk target yet).
        complexity = str(getattr(context, "task_complexity", "") or "").strip().lower()
        if complexity in _HEAVY_COMPLEXITIES:
            return True

        # Localized op → RT stream (the fast, low-latency default).
        return False
    except Exception:  # noqa: BLE001 — fail-soft to RT
        return False


__all__ = [
    "dynamic_transport_enabled",
    "should_batch_by_payload",
]
