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

# Universal mode (2026-07-21 — soak bt-2026-07-21-230753): the benchmark-only
# gating left host self-development ops unguarded, so a candidate carrying a
# write-root-DOUBLED path sailed to APPLY and hard-ENOENT'd inside the
# ChangeEngine. Universal mode extends the gate to ALL ops with one semantic
# difference: host self-dev legitimately CREATES new files, so a missing
# target is only a steering error when its parent directory ALSO fails to
# exist inside the write root (a genuinely new file lands in an existing
# package; the doubled-path garbage class never does).
_UNIVERSAL_ENABLED_ENV = "JARVIS_TARGET_EXISTENCE_GATE_UNIVERSAL"


def guard_enabled() -> bool:
    """Master flag — default TRUE (graduates on for the scored soak).

    Single-knob hot-revert: a falsey value restores the pre-Slice-72 behavior
    (the candidate flows straight to APPLY and ENOENTs as before).
    """
    raw = os.environ.get(_ENABLED_ENV, "true").strip().lower()
    return raw not in ("0", "false", "no", "off")


def universal_guard_enabled() -> bool:
    """Universal-mode master — default TRUE. Falsey restores benchmark-only
    gating (non-benchmark candidates flow ungated to APPLY as before)."""
    raw = os.environ.get(_UNIVERSAL_ENABLED_ENV, "true").strip().lower()
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


def _new_file_lane_allows(rel_path: str, write_root: Path) -> bool:
    """The universal-mode new-file predicate: ``rel_path`` resolves inside
    ``write_root`` AND its ANCHOR (first path component) exists there.

    Anchor semantics, not immediate-parent (2026-07-22 refinement): the
    ChangeEngine's Sandboxed Ephemeral Instantiation legitimately scaffolds
    nested NEW package dirs (``backend/new_pkg/sub/module.py`` under an
    existing ``backend/``), so demanding an existing immediate parent would
    contradict the engine and block real module scaffolding. The garbage
    classes this lane must reject — write-root echoes (``Documents/...``),
    absolute-prefix remnants, hallucinated top-level trees — always fail at
    the FIRST component. A single-component path (``newfile.py``) anchors
    on the root itself, which exists by definition. Escapes are rejected
    (mirrors ``_resolves_inside_and_exists``). Never raises.
    """
    try:
        root = write_root.resolve()
        resolved = (root / rel_path).resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    try:
        rel = resolved.relative_to(root)
    except ValueError:
        return False  # escaped the worktree
    parts = rel.parts
    if len(parts) <= 1:
        return True  # anchors on the root itself
    try:
        return (root / parts[0]).is_dir()
    except OSError:
        return False


def find_missing_targets(
    candidates: Sequence[Any],
    write_root: Optional[Path],
    *,
    allow_new_files: bool = False,
) -> List[str]:
    """Return the sorted unique target paths that don't exist under write_root.

    ``write_root`` is the benchmark worktree (from ``_swe_bench_write_root``)
    or, in universal mode, the effective execution root. When it is ``None``
    (no per-op write root resolved) the guard is INERT — we cannot know the
    repo layout, so we never block. Pure; never raises.

    ``allow_new_files`` (universal mode, keyword-only; default ``False`` =
    benchmark semantics byte-identical): when TRUE a nonexistent target is
    acceptable IF its anchor component exists inside the write root — the
    legitimate host-self-dev new-file/new-package lane (the ChangeEngine
    scaffolds the nested parents). A missing target whose FIRST component
    is also missing (write-root echoes, absolute remnants, hallucinated
    trees) is flagged either way.
    """
    if write_root is None:
        return []
    missing: set = set()
    for cand in candidates or ():
        for rel in _candidate_target_paths(cand):
            if _resolves_inside_and_exists(rel, write_root):
                continue
            if allow_new_files and _new_file_lane_allows(rel, write_root):
                continue
            missing.add(rel)
    return sorted(missing)


def build_retry_feedback(
    missing: Sequence[str], *, benchmark: bool = True,
) -> str:
    """The self-correcting GENERATE_RETRY payload shown back to the model.

    ``benchmark`` (keyword-only; default ``True`` = the original Slice 72
    wording, byte-identical for existing callers) selects the lane-correct
    narrative: benchmark ops get the third-party-repo steering text;
    universal-mode host ops get path-hygiene steering (the doubled-prefix /
    phantom-parent class) that does NOT wrongly tell the model it is outside
    the host framework.
    """
    paths = ", ".join(f"'{p}'" for p in missing) or "'<unknown>'"
    if benchmark:
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
    return (
        "## PREVIOUS GENERATION REJECTED — TARGET PATH DOES NOT RESOLVE\n\n"
        f"The proposed target file(s) {paths} neither exist in the repository "
        "nor land inside an existing package directory. This usually means the "
        "path was malformed — e.g. it repeats the repository root's own path "
        "prefix, embeds an absolute filesystem prefix, or points into a "
        "directory tree that does not exist.\n\n"
        "INSTRUCTIONS FOR RETRY:\n"
        "- Emit CLEAN repo-relative paths only (e.g. 'backend/pkg/module.py') —\n"
        "  never absolute paths and never paths containing the repository's own\n"
        "  directory name as a prefix.\n"
        "- To modify existing code, call read_file / search_code / list_dir to\n"
        "  confirm the exact repo-relative path BEFORE emitting the patch.\n"
        "- To create a NEW file, place it inside a directory that already\n"
        "  exists in the repository.\n"
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
