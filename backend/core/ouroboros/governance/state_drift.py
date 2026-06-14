"""Slice 247 — State-Drift Reconciliation: zero-LLM target-file drift detection.

When a GOAL is preempted/resurrected (Slices 245/246), a human override may have
patched the exact file it was about to modify. Applying its pre-computed
candidate against the drifted disk state corrupts the file (AST + line-number
drift). This module is the deterministic, ~microsecond, NO-LLM validator: compare
the file hashes captured at GENERATE (``OperationContext.generate_file_hashes``,
preserved across the suspension) against the current disk, and — on mismatch —
produce a RE-ALIGNMENT instruction that forces the model to re-read the drifted
files before it regenerates.

Pure functions, env-driven, NEVER raise (they sit on the GENERATE/APPLY hot
path). The actual re-read + regenerate is the EXISTING GENERATE machinery; this
module only detects drift and renders the feedback that steers it.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

# Telemetry state tokens (Manifesto §7 — every autonomous decision is visible).
STATE_CONTEXT_DRIFTED = "CONTEXT_DRIFTED"
# Slice 248 — terminal_reason_code when the APPLY-time verification pass blocks a
# provably-stale candidate (drift the GENERATE-entry re-alignment did not resolve).
STATE_DRIFT_UNRECONCILED = "state_drift_unreconciled"

_ENV_ENABLED = "JARVIS_STATE_DRIFT_RECONCILE_ENABLED"
_ENV_VERIFY = "JARVIS_STATE_DRIFT_VERIFY_ENABLED"


def state_drift_reconcile_enabled() -> bool:
    """Master gate for the GENERATE-entry re-alignment (Slice 247, default-TRUE).
    When OFF, the legacy log-only / blind-apply behaviour stands (byte-identical).
    NEVER raises."""
    try:
        return os.getenv(_ENV_ENABLED, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def state_drift_verify_enabled() -> bool:
    """Slice 248 — master gate for the APPLY-time verification pass (default-TRUE).
    When OFF, drift is still detected + logged but the apply is NOT blocked
    (byte-identical to the pre-248 log-and-apply behaviour). NEVER raises."""
    try:
        return os.getenv(_ENV_VERIFY, "true").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001
        return False


def should_block_apply(
    prior_hashes: Optional[Sequence[Tuple[str, str]]],
    project_root: Optional[Any],
) -> Tuple[bool, List[str]]:
    """Slice 248 — the APPLY-time verification decision (pure, zero-LLM).

    Returns ``(block, drifted_files)``: ``drifted_files`` is the current set of
    targets whose on-disk content no longer matches the candidate's GENERATE
    baseline; ``block`` is True iff there IS drift AND the verify gate is on.
    A stale candidate applied to drifted disk corrupts the file (full-content
    overwrite = data loss; diff/anchor = line drift), so a True ``block`` means
    "refuse the apply, fail safe." NEVER raises — on any error returns
    ``(False, [])`` (degrade to the legacy apply rather than crash)."""
    drifted = detect_drift(prior_hashes, project_root)
    block = bool(drifted) and state_drift_verify_enabled()
    return block, drifted


def detect_drift(
    prior_hashes: Optional[Sequence[Tuple[str, str]]],
    project_root: Optional[Any],
) -> List[str]:
    """Return the relative paths whose on-disk sha256 differs from the baseline
    captured at GENERATE. Zero-LLM, deterministic.

    Skips (NOT drift):
      * empty baseline hash — the file did not exist at GENERATE (a new file);
      * a file now missing on disk — a deletion, a different failure class.
    NEVER raises — a hashing error on one file degrades to "not drifted" for
    that file so the validator can never crash the pipeline."""
    drifted: List[str] = []
    if not prior_hashes or project_root is None:
        return drifted
    try:
        root = Path(project_root)
    except (TypeError, ValueError):
        return drifted
    for entry in prior_hashes:
        try:
            rel, baseline = entry
        except (TypeError, ValueError):
            continue
        if not baseline:
            continue  # new file at GENERATE — nothing to compare
        try:
            cur = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        except (OSError, IOError):
            continue  # deleted / unreadable — not a drift
        except Exception:  # noqa: BLE001 — never crash the hot path
            continue
        if cur != baseline:
            drifted.append(rel)
    return drifted


def build_realignment_feedback(stale_files: Sequence[str]) -> str:
    """Render the RE-ALIGNMENT instruction injected into the GENERATE prompt when
    drift is detected. Forces the model to re-read the drifted files (via
    read_file) so it regenerates against the human's NEW state — never
    blind-patching a stale target. ASCII-only (Iron Gate strictness). Empty
    string when nothing drifted (no-op injection)."""
    files = [f for f in (stale_files or []) if f]
    if not files:
        return ""
    listing = "\n".join(f"  - {f}" for f in files)
    return (
        "## STATE=CONTEXT_DRIFTED -- mandatory re-alignment\n"
        "The following target file(s) were modified on disk AFTER your prior "
        "exploration (a concurrent human override or another operation patched "
        "them). Your earlier reading of them is STALE:\n"
        f"{listing}\n"
        "Before generating any patch you MUST call read_file on each drifted "
        "file above and re-derive your change against its CURRENT contents. "
        "Do NOT reuse line numbers, anchors, or content from your prior pass -- "
        "they have drifted and a blind patch will corrupt the file."
    )
