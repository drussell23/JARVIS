"""Git-history momentum signal — extracted from StrategicDirectionService.

Pure-data computation of recent commit momentum. Conventional-commit parsing
of the last N commit subjects → structured ``MomentumSnapshot`` (scope and
type histograms, latest subject lines, wall-clock span). Zero model
inference, zero authority surface, zero side effects beyond a single
``git log`` subprocess.

Why a separate module (P0.5 Slice 1, PRD §9 Phase 1):
    The same momentum signal is consumed by two governance subsystems:
    ``StrategicDirectionService`` (prompt injection at session boot,
    Manifesto §4 — synthetic soul) and — under P0.5 — by
    ``DirectionInferrer`` as an arc-context input to posture evaluation.
    Hosting the parser in ``strategic_direction.py`` made it inaccessible
    to the inferrer without a circular import. Extracting to its own
    module keeps both consumers thin and lets the regression suite pin
    the parser independently of either consumer.

Authority invariants (PRD §12.2 / Manifesto §1 Boundary):
    - Read-only: the only side effect is a bounded subprocess call to
      ``git log``. Never mutates code, env, repo state, or governance state.
    - No banned imports: this module MUST NOT import ``orchestrator``,
      ``policy``, ``iron_gate``, ``risk_tier``, ``change_engine``,
      ``candidate_generator``, ``gate``, or ``semantic_guardian``.
      Pinned by ``test_git_momentum_no_authority_imports``.
    - Best-effort: any failure (no git, shallow clone, subprocess timeout,
      malformed output) returns ``None``. Callers default to a "no signal"
      branch. Never raises.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Conventional Commits: ``type(scope)?: subject``. Scope is optional.
# This regex is the single source of truth for both consumers
# (``StrategicDirectionService`` and the P0.5 ``DirectionInferrer``
# arc-context branch).
_CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]+)\))?:\s*(?P<subject>.+)$"
)

_DEFAULT_MAX_COMMITS = 100
_DEFAULT_TIMEOUT_S = 5.0
_SUBJECT_TRUNCATION = 60  # chars; matches StrategicDirection legacy behavior


@dataclass(frozen=True)
class MomentumSnapshot:
    """Structured snapshot of recent git momentum.

    All histograms are sorted descending by count (ties broken by name asc),
    and capped via ``top()`` rather than at construction so consumers can
    pick their own visibility budget.

    Attributes
    ----------
    commit_count:
        Number of commits actually parsed (≤ ``max_commits``; lower if
        the repo has fewer commits or some lines were dropped).
    scope_counts:
        ``{scope_name: count}``. Only populated for conventional-commit
        lines that carried a scope. Sorted descending by count.
    type_counts:
        ``{type_name: count}``. Only populated for conventional-commit
        lines that carried a type. Sorted descending by count.
    latest_subjects:
        Up to the ``max_commits`` most recent subject lines, truncated at
        ``_SUBJECT_TRUNCATION`` chars each. Includes both conventional and
        non-conventional commits in source order (newest first, matching
        ``git log`` default).
    non_conventional_count:
        Count of commits whose subject did not match the conventional
        pattern. Useful as a "noise floor" signal.
    wall_seconds_span:
        Wall-clock seconds between the oldest and newest parsed commit's
        committer timestamp. ``0.0`` when only one commit was parsed or
        timestamps were unavailable. Useful for callers that want to
        weight the signal by recency density.
    """

    commit_count: int
    scope_counts: Dict[str, int] = field(default_factory=dict)
    type_counts: Dict[str, int] = field(default_factory=dict)
    latest_subjects: Tuple[str, ...] = ()
    non_conventional_count: int = 0
    wall_seconds_span: float = 0.0

    def top_scopes(self, n: int = 5) -> List[Tuple[str, int]]:
        """Top ``n`` scopes by count, sorted (count desc, name asc)."""
        return sorted(
            self.scope_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )[:n]

    def top_types(self, n: int = 4) -> List[Tuple[str, int]]:
        """Top ``n`` commit types by count, sorted (count desc, name asc)."""
        return sorted(
            self.type_counts.items(), key=lambda kv: (-kv[1], kv[0])
        )[:n]

    def is_empty(self) -> bool:
        """True when the snapshot contains no signal at all."""
        return self.commit_count == 0


def compute_recent_momentum(
    project_root: Path,
    max_commits: int = _DEFAULT_MAX_COMMITS,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> Optional[MomentumSnapshot]:
    """Run ``git log`` over the last ``max_commits`` and parse the result.

    Parameters
    ----------
    project_root:
        Repository root. Passed as ``cwd`` to the subprocess.
    max_commits:
        Maximum number of commits to inspect. Default 100. Negative values
        and zero are clamped to ``1`` (treated as "give me at least the
        most recent commit if any exists").
    timeout_s:
        Subprocess timeout in seconds. Default 5.0.

    Returns
    -------
    MomentumSnapshot when at least one commit was parsed, else ``None``.
    Returns ``None`` on any subprocess failure, malformed output, or
    timeout — never raises.

    Format
    ------
    Uses ``--pretty=format:%H|%ct|%s`` so each line is
    ``hash|committer_unix_ts|subject``. The pipe separator is safe because
    git hashes are hex and timestamps are integer seconds — neither can
    contain a literal pipe. Subjects can contain pipes; we split on the
    first two only.
    """
    n = max(1, int(max_commits))
    try:
        result = subprocess.run(
            ["git", "log", f"-{n}", "--pretty=format:%H|%ct|%s"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=float(timeout_s),
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None

    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    if not lines:
        return None

    scope_counts: Dict[str, int] = {}
    type_counts: Dict[str, int] = {}
    latest_subjects: List[str] = []
    non_conventional = 0
    timestamps: List[int] = []
    parsed = 0

    for raw in lines:
        # Split on the first two pipes only. Subjects may contain pipes.
        parts = raw.split("|", 2)
        if len(parts) != 3:
            # Malformed line — skip but do not abort the whole snapshot.
            continue
        _hash, ts_str, subject = parts
        subject = subject.strip()
        if not subject:
            continue
        parsed += 1
        try:
            timestamps.append(int(ts_str))
        except ValueError:
            pass

        m = _CONVENTIONAL_COMMIT_RE.match(subject)
        if not m:
            non_conventional += 1
            latest_subjects.append(subject[:_SUBJECT_TRUNCATION])
            continue
        t = (m.group("type") or "").lower()
        s = (m.group("scope") or "").lower()
        sub = (m.group("subject") or "").strip()
        if t:
            type_counts[t] = type_counts.get(t, 0) + 1
        if s:
            scope_counts[s] = scope_counts.get(s, 0) + 1
        if sub:
            latest_subjects.append(sub[:_SUBJECT_TRUNCATION])

    if parsed == 0:
        return None

    span = 0.0
    if len(timestamps) >= 2:
        span = float(max(timestamps) - min(timestamps))

    return MomentumSnapshot(
        commit_count=parsed,
        scope_counts=scope_counts,
        type_counts=type_counts,
        latest_subjects=tuple(latest_subjects),
        non_conventional_count=non_conventional,
        wall_seconds_span=span,
    )


def format_themes(snapshot: Optional[MomentumSnapshot]) -> List[str]:
    """Render a snapshot as the legacy ``_extract_git_themes`` theme list.

    Output format is byte-for-byte compatible with the pre-extraction
    ``StrategicDirectionService._extract_git_themes`` return value:

    * ``"Active scopes: <name> (<count>), ..."`` (top 5)
    * ``"Commit mix: <name>=<count>, ..."`` (top 4)
    * ``"Latest work: <subject> | <subject> | <subject>"`` (top 3)

    Returns ``[]`` for ``None`` snapshot or empty signal. Used by
    ``StrategicDirectionService`` to keep the prompt injection format
    stable across the extraction.
    """
    if snapshot is None or snapshot.is_empty():
        return []

    themes: List[str] = []

    if snapshot.scope_counts:
        themes.append(
            "Active scopes: "
            + ", ".join(
                f"{name} ({count})" for name, count in snapshot.top_scopes(5)
            )
        )

    if snapshot.type_counts:
        themes.append(
            "Commit mix: "
            + ", ".join(
                f"{name}={count}" for name, count in snapshot.top_types(4)
            )
        )

    if snapshot.latest_subjects:
        themes.append(
            "Latest work: " + " | ".join(snapshot.latest_subjects[:3])
        )

    return themes


__all__ = [
    "MomentumSnapshot",
    "compute_recent_momentum",
    "format_themes",
]
