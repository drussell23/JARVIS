"""Slice 72 — Generative Target-Existence Guard.

Root cause (verify-first, bt-2026-06-03-035359 debug.log): for a SWE-Bench-Pro
op the tool loop WAS correctly confined to the prepared worktree (Slice 3G
override fired, chroot + allowlist all working), and the model DID explore it —
but after the DW primary timed out (148s), the Claude fallback did a single
rushed exploration round and emitted a target path in the HOST framework's
namespace (``backend/core/process_manager.py``) instead of the benchmark
repo's (``qutebrowser/utils/guiprocess.py``). APPLY rebased that JARVIS path
onto the worktree → hard ``ENOENT`` → postmortem, no patch, no container score.

This module is the deterministic gate: for a benchmark op, every candidate's
target file MUST already exist inside the write root (the worktree) — the
benchmark fixes EXISTING code, never creates host-framework files. A miss is a
generation-steering error, surfaced back to the model as self-correcting
GENERATE_RETRY feedback rather than crashing APPLY.

Pure functions; NEVER raise. Gated by the orchestrator on
``signal_source == "swe_bench_pro"`` so host self-development (which legitimately
creates new files) is completely untouched.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence, Tuple


_ENABLED_ENV = "JARVIS_SWE_BENCH_TARGET_EXISTENCE_GUARD_ENABLED"


def guard_enabled() -> bool:
    """Master flag — default TRUE (graduates on for the scored soak).

    Single-knob hot-revert: a falsey value restores the pre-Slice-72 behavior
    (the candidate flows straight to APPLY and ENOENTs as before).
    """
    raw = os.environ.get(_ENABLED_ENV, "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _candidate_target_paths(candidate: Any) -> Tuple[str, ...]:
    """Extract every repo-relative target path a candidate would write.

    Handles both the legacy single ``file_path`` shape and the multi-file
    ``files: [{file_path, ...}]`` shape. Pure; never raises; returns () for
    anything unparseable (a non-dict candidate, missing keys, etc.).
    """
    if not isinstance(candidate, Mapping):
        return ()
    out: List[str] = []
    seen: set = set()

    def _add(p: Any) -> None:
        if isinstance(p, str) and p.strip():
            s = p.strip()
            if s not in seen:
                seen.add(s)
                out.append(s)

    files = candidate.get("files")
    if isinstance(files, Sequence) and not isinstance(files, (str, bytes)):
        for entry in files:
            if isinstance(entry, Mapping):
                _add(entry.get("file_path"))
    _add(candidate.get("file_path"))
    return tuple(out)


def _resolves_inside_and_exists(rel_path: str, write_root: Path) -> bool:
    """True iff ``rel_path`` resolves INSIDE ``write_root`` AND exists on disk.

    A path that escapes the write root (``../`` climb, absolute host path) is
    treated as missing — the existing ChangeEngine chroot would reject it too;
    here we surface it as a retry-able steering error instead. Never raises.
    """
    try:
        root = write_root.resolve()
        candidate = (root / rel_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        candidate.relative_to(root)
    except ValueError:
        return False  # escaped the worktree
    try:
        return candidate.is_file()
    except OSError:
        return False


def find_missing_targets(
    candidates: Sequence[Any], write_root: Optional[Path],
) -> List[str]:
    """Return the sorted unique target paths that don't exist under write_root.

    ``write_root`` is the benchmark worktree (from ``_swe_bench_write_root``).
    When it is ``None`` (no per-op write root resolved) the guard is INERT —
    we cannot know the repo layout, so we never block. Pure; never raises.
    """
    if write_root is None:
        return []
    missing: set = set()
    for cand in candidates or ():
        for rel in _candidate_target_paths(cand):
            if not _resolves_inside_and_exists(rel, write_root):
                missing.add(rel)
    return sorted(missing)


def build_retry_feedback(missing: Sequence[str]) -> str:
    """The self-correcting GENERATE_RETRY payload shown back to the model."""
    paths = ", ".join(f"'{p}'" for p in missing) or "'<unknown>'"
    return (
        "## PREVIOUS GENERATION REJECTED — TARGET FILE DOES NOT EXIST\n\n"
        f"The proposed target file(s) {paths} do not exist within the current "
        "isolated problem repository. You are working on a THIRD-PARTY project, "
        "NOT the host framework — paths like 'backend/core/...' belong to the "
        "host system and are out of bounds.\n\n"
        "INSTRUCTIONS FOR RETRY:\n"
        "- Your modifications MUST target files that already exist in THIS repo.\n"
        "- Call search_code / glob_files / list_dir to locate the real file that\n"
        "  implements the behavior described in the problem statement BEFORE\n"
        "  emitting any patch.\n"
        "- Emit the patch against the exact repo-relative path you confirmed\n"
        "  exists via exploration — do not guess a host-framework path.\n"
    )


# Sentinel prefix the orchestrator's retry-feedback dispatcher keys on (mirrors
# the ``ascii_gate_failed:`` convention).
TARGET_MISSING_PREFIX = "target_file_missing:"


def missing_target_error_message(missing: Sequence[str]) -> str:
    """Compose the RuntimeError message the generation-loop gate raises."""
    return f"{TARGET_MISSING_PREFIX} {', '.join(missing)}"


# ---------------------------------------------------------------------------
# Phase 3 — Contextual Prompt Insulation
# ---------------------------------------------------------------------------
# The generation-steering root cause was amplified by the GENERATE prompt being
# saturated with HOST-framework context (the JARVIS Manifesto, architecture
# docs, recent-commit momentum, active goals) — which biased the model toward
# host paths like ``backend/core/...``. For a benchmark op that host context is
# pure noise: the model must focus entirely on the third-party problem repo.
# This flag strips those host-specific strategic injections for swe_bench ops.
_PROMPT_INSULATION_ENV = "JARVIS_BENCHMARK_PROMPT_INSULATION_ENABLED"


def prompt_insulation_enabled() -> bool:
    """Master flag — default TRUE. Falsey restores host-context injection."""
    raw = os.environ.get(_PROMPT_INSULATION_ENV, "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def should_insulate_prompt(signal_source: Optional[str]) -> bool:
    """True iff host-framework strategic context should be withheld from the
    GENERATE prompt — i.e. this is a benchmark op AND insulation is enabled.

    Pure; never raises. Non-benchmark ops are always False (host self-dev keeps
    its full strategic context).
    """
    return prompt_insulation_enabled() and (signal_source or "") == "swe_bench_pro"
