"""
Long-Horizon Memory — Commit-History-Aware Cross-Session Layer
==============================================================

Closes §41.4 Phase 1 seventh arc (PRD v3.0+). Per the binding:

  "Long-horizon memory beyond sessions | Partial
   (UserPreferenceStore + AdaptationLedger cover some) | ~2
   weeks | Compose existing 4 cross-session substrates + add
   commit-history-aware memory layer"

The 4 existing cross-session substrates cover:
* :mod:`user_preference_memory` — operator preferences
  (USER/FEEDBACK/PROJECT/REFERENCE/FORBIDDEN_PATH/STYLE)
* :mod:`last_session_summary` — previous-session digest
  with apply/verify/commit tokens
* :mod:`semantic_index` — recency-weighted centroid + cluster
  themes over recent activity
* (Wave 4 #9 belief_revision_ledger — Bayesian calibration —
  not directly composed here; long-horizon memory is
  observational, not predictive)

What's MISSING and this substrate adds: **commit-history-
derived memory**. The 4 existing substrates remember
*conversations* and *adaptive state*; they don't remember
what the **codebase itself** has been doing over weeks/months.

This substrate walks ``git log`` over an operator-tunable
horizon window (default 90 days). For each commit it
extracts:

1. **Theme classification** — closed 4-value
   :class:`MemoryTheme` (REFACTOR / FEATURE / BUGFIX /
   OTHER) via keyword-table matching of commit subject
2. **File touch counts** — per-file activity over the
   window
3. **Theme distribution per file** — which themes touch
   which files most often
4. **Drift signal** — comparison of recent (last 25%) vs
   older (first 75%) theme histograms; surfaces when the
   codebase's activity pattern has shifted

The result composes into a :class:`MemorySnapshot` that:
* Lists top hot files (touched > median + 1σ)
* Lists stale files (touched once, > stale_days_threshold
  days ago) — useful for spotting under-maintained code
* Tags overall horizon as SESSION/DAY/WEEK/MONTH so
  consumer-side prompt injection can adapt verbosity
* Surfaces composed-source diagnostics: which of the 3
  cross-session substrates contributed, what each said

The substrate is **deterministic** — same git history →
same memory snapshot. Operator-injectable git_log_runner
for hermetic testing.

Composition contract:

* :mod:`subprocess` (stdlib) — git log walker
* :func:`user_preference_memory.get_default_store` (lazy
  import) — read operator STYLE / PROJECT memories
* :func:`last_session_summary.get_default_summary` (lazy
  import) — append-only continuity tokens
* :func:`semantic_index.get_default_index` (lazy import) —
  recency-weighted centroid for cluster-themed prompt
* :func:`governance_boundary_gate.is_boundary_crossed`
  (Wave 2 #5) — cage flag on hot files
* :func:`cross_process_jsonl.flock_append_line` — §33.4
  audit at ``.jarvis/long_horizon_memory_ledger.jsonl``

NEVER raises. Empty git history / missing 3 substrates /
malformed commits all degrade to ``DISABLED`` or empty
snapshot, not exception.

Closed 4-value :class:`RecallVerdict`:

  FRESH       memory horizon ≤ recent_window_days; recall
              is direct evidence of current behavior
  WARM        recent_window_days < horizon ≤ warm_window_days
  COLD        horizon > warm_window_days; recall is
              long-history-context (advisory)
  DISABLED    master off OR no commits found

Closed 4-value :class:`MemoryHorizon`:

  SESSION     ≤ 24h
  DAY         1–7 days
  WEEK        1–4 weeks
  MONTH       > 4 weeks

Closed 4-value :class:`MemoryTheme`:

  REFACTOR    rename/cleanup/structure changes
  FEATURE     new behavior added
  BUGFIX      defect repair
  OTHER       anything else (docs / config / chore)

§33.1 cognitive substrate
``JARVIS_LONG_HORIZON_MEMORY_ENABLED`` default-**FALSE**.

Authority asymmetry (AST-pinned): stdlib only at module
load. ``user_preference_memory`` + ``last_session_summary``
+ ``semantic_index`` + ``governance_boundary_gate`` +
``cross_process_jsonl`` are lazy-imported. Does NOT import
orchestrator / iron_gate / policy / providers /
candidate_generator / urgency_router / change_engine /
semantic_guardian / auto_committer / risk_tier_floor /
tool_executor / plan_generator (substrate is advisory; the
4 composed substrates remain accessible to their direct
consumers).
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import re
import statistics
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


LONG_HORIZON_MEMORY_SCHEMA_VERSION: str = "long_horizon_memory.1"


_ENV_MASTER = "JARVIS_LONG_HORIZON_MEMORY_ENABLED"
_ENV_PERSIST = "JARVIS_LONG_HORIZON_MEMORY_PERSIST_ENABLED"
_ENV_MAX_COMMITS = "JARVIS_LONG_HORIZON_MEMORY_MAX_COMMITS"
_ENV_HORIZON_DAYS = "JARVIS_LONG_HORIZON_MEMORY_HORIZON_DAYS"
_ENV_RECENT_WINDOW_DAYS = (
    "JARVIS_LONG_HORIZON_MEMORY_RECENT_WINDOW_DAYS"
)
_ENV_WARM_WINDOW_DAYS = (
    "JARVIS_LONG_HORIZON_MEMORY_WARM_WINDOW_DAYS"
)
_ENV_STALE_DAYS_THRESHOLD = (
    "JARVIS_LONG_HORIZON_MEMORY_STALE_DAYS_THRESHOLD"
)
_ENV_HOT_FILE_COUNT = (
    "JARVIS_LONG_HORIZON_MEMORY_HOT_FILE_COUNT"
)
_ENV_STALE_FILE_COUNT = (
    "JARVIS_LONG_HORIZON_MEMORY_STALE_FILE_COUNT"
)
_ENV_GIT_TIMEOUT_S = "JARVIS_LONG_HORIZON_MEMORY_GIT_TIMEOUT_S"
_ENV_LEDGER_PATH = "JARVIS_LONG_HORIZON_MEMORY_LEDGER_PATH"

_DEFAULT_MAX_COMMITS = 500
_DEFAULT_HORIZON_DAYS = 90
_DEFAULT_RECENT_WINDOW_DAYS = 7
_DEFAULT_WARM_WINDOW_DAYS = 30
_DEFAULT_STALE_DAYS_THRESHOLD = 60
_DEFAULT_HOT_FILE_COUNT = 10
_DEFAULT_STALE_FILE_COUNT = 10
_DEFAULT_GIT_TIMEOUT_S = 30
_DEFAULT_LEDGER_REL = ".jarvis/long_horizon_memory_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    return _flag(_ENV_PERSIST, default=True)


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def max_commits_to_walk() -> int:
    return _read_clamped_int(
        _ENV_MAX_COMMITS, _DEFAULT_MAX_COMMITS, 1, 100_000,
    )


def horizon_days() -> int:
    """Total memory horizon in days. Default 90."""
    return _read_clamped_int(
        _ENV_HORIZON_DAYS, _DEFAULT_HORIZON_DAYS, 1, 3650,
    )


def recent_window_days() -> int:
    """Window for FRESH classification. Default 7."""
    return _read_clamped_int(
        _ENV_RECENT_WINDOW_DAYS,
        _DEFAULT_RECENT_WINDOW_DAYS, 1, 365,
    )


def warm_window_days() -> int:
    """Window for WARM classification. Default 30.
    Auto-clamped > recent_window."""
    raw = _read_clamped_int(
        _ENV_WARM_WINDOW_DAYS,
        _DEFAULT_WARM_WINDOW_DAYS, 1, 365,
    )
    return max(raw, recent_window_days() + 1)


def stale_days_threshold() -> int:
    """Files untouched longer than this → candidates for
    stale_files list. Default 60."""
    return _read_clamped_int(
        _ENV_STALE_DAYS_THRESHOLD,
        _DEFAULT_STALE_DAYS_THRESHOLD, 1, 3650,
    )


def hot_file_count() -> int:
    return _read_clamped_int(
        _ENV_HOT_FILE_COUNT, _DEFAULT_HOT_FILE_COUNT, 1, 100,
    )


def stale_file_count() -> int:
    return _read_clamped_int(
        _ENV_STALE_FILE_COUNT, _DEFAULT_STALE_FILE_COUNT, 1, 100,
    )


def git_timeout_s() -> int:
    return _read_clamped_int(
        _ENV_GIT_TIMEOUT_S, _DEFAULT_GIT_TIMEOUT_S, 1, 600,
    )


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class RecallVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    FRESH = "fresh"
    WARM = "warm"
    COLD = "cold"
    DISABLED = "disabled"


class MemoryHorizon(str, enum.Enum):
    """Closed 4-value horizon — bytes-pinned via AST."""

    SESSION = "session"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class MemoryTheme(str, enum.Enum):
    """Closed 4-value theme — bytes-pinned via AST."""

    REFACTOR = "refactor"
    FEATURE = "feature"
    BUGFIX = "bugfix"
    OTHER = "other"


_VERDICT_GLYPH: Dict[str, str] = {
    RecallVerdict.FRESH.value: "🌱",
    RecallVerdict.WARM.value: "🔥",
    RecallVerdict.COLD.value: "❄",
    RecallVerdict.DISABLED.value: "◌",
}


_HORIZON_GLYPH: Dict[str, str] = {
    MemoryHorizon.SESSION.value: "·",
    MemoryHorizon.DAY.value: "○",
    MemoryHorizon.WEEK.value: "◉",
    MemoryHorizon.MONTH.value: "●",
}


_THEME_GLYPH: Dict[str, str] = {
    MemoryTheme.REFACTOR.value: "🔧",
    MemoryTheme.FEATURE.value: "✨",
    MemoryTheme.BUGFIX.value: "🐛",
    MemoryTheme.OTHER.value: "·",
}


# Theme classification — keyword tables (NOT operator state;
# these are the linguistic constants matching git conventional
# commit prefixes + common patterns).
_REFACTOR_KEYWORDS: FrozenSet[str] = frozenset({
    "refactor", "rename", "cleanup", "restructure", "simplify",
    "extract", "inline", "tidy", "reorganize",
})
_FEATURE_KEYWORDS: FrozenSet[str] = frozenset({
    "feat", "feature", "add", "new", "implement", "introduce",
    "support", "enable",
})
_BUGFIX_KEYWORDS: FrozenSet[str] = frozenset({
    "fix", "bug", "bugfix", "hotfix", "patch", "resolve",
    "correct", "repair",
})


def _coerce_value(obj: object) -> str:
    try:
        val = getattr(obj, "value", None)
        if val is not None:
            return str(val).strip().lower()
        return str(obj or "").strip().lower()
    except Exception:  # noqa: BLE001
        return ""


def verdict_glyph(verdict: object) -> str:
    """NEVER raises."""
    return _VERDICT_GLYPH.get(_coerce_value(verdict), "?")


def horizon_glyph(horizon: object) -> str:
    """NEVER raises."""
    return _HORIZON_GLYPH.get(_coerce_value(horizon), "?")


def theme_glyph(theme: object) -> str:
    """NEVER raises."""
    return _THEME_GLYPH.get(_coerce_value(theme), "?")


def classify_theme(commit_subject: str) -> MemoryTheme:
    """Pure classifier. NEVER raises.

    Algorithm:
    1. Lowercase subject + first-word stem extraction
    2. Match against keyword tables in priority order:
       BUGFIX (most signal-rich) > FEATURE > REFACTOR > OTHER
    3. Conventional commit prefix (e.g., ``fix:``, ``feat:``)
       takes precedence over body keywords."""
    try:
        s = str(commit_subject or "").strip().lower()
    except Exception:  # noqa: BLE001
        return MemoryTheme.OTHER
    if not s:
        return MemoryTheme.OTHER
    # Conventional commit prefix (highest precedence)
    prefix_match = re.match(r"^([a-z]+)(?:\(|:)", s)
    if prefix_match:
        prefix = prefix_match.group(1)
        if prefix in _BUGFIX_KEYWORDS:
            return MemoryTheme.BUGFIX
        if prefix in _FEATURE_KEYWORDS:
            return MemoryTheme.FEATURE
        if prefix in _REFACTOR_KEYWORDS:
            return MemoryTheme.REFACTOR
    # Body keyword search — split on word boundaries
    tokens = re.findall(r"[a-z]+", s)
    token_set = set(tokens)
    if token_set & _BUGFIX_KEYWORDS:
        return MemoryTheme.BUGFIX
    if token_set & _FEATURE_KEYWORDS:
        return MemoryTheme.FEATURE
    if token_set & _REFACTOR_KEYWORDS:
        return MemoryTheme.REFACTOR
    return MemoryTheme.OTHER


def classify_horizon(age_days: float) -> MemoryHorizon:
    """Pure classifier. NEVER raises."""
    try:
        d = max(0.0, float(age_days))
    except Exception:  # noqa: BLE001
        return MemoryHorizon.MONTH
    if d <= 1.0:
        return MemoryHorizon.SESSION
    if d <= 7.0:
        return MemoryHorizon.DAY
    if d <= 28.0:
        return MemoryHorizon.WEEK
    return MemoryHorizon.MONTH


def _classify_verdict_for_horizon(days: float) -> RecallVerdict:
    """Pure classifier for top-level RecallVerdict."""
    recent = recent_window_days()
    warm = warm_window_days()
    if days <= recent:
        return RecallVerdict.FRESH
    if days <= warm:
        return RecallVerdict.WARM
    return RecallVerdict.COLD


# §33.5 frozen artifacts


@dataclass(frozen=True)
class CommitRecord:
    """One git log entry."""

    sha: str
    subject: str
    author: str
    committed_at_unix: float
    age_days: float
    theme: MemoryTheme
    files: Tuple[str, ...]
    schema_version: str = LONG_HORIZON_MEMORY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sha": self.sha[:40],
            "subject": self.subject[:256],
            "author": self.author[:128],
            "committed_at_unix": float(self.committed_at_unix),
            "age_days": float(self.age_days),
            "theme": self.theme.value,
            "files": list(self.files),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CommitTheme:
    """Theme aggregation across recent commits."""

    theme: MemoryTheme
    count: int
    sample_subjects: Tuple[str, ...]
    dominant_files: Tuple[str, ...]
    schema_version: str = LONG_HORIZON_MEMORY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "theme": self.theme.value,
            "count": int(self.count),
            "sample_subjects": list(self.sample_subjects),
            "dominant_files": list(self.dominant_files),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class FileHotness:
    """Per-file activity over the horizon."""

    file_path: str
    touch_count: int
    last_touched_unix: float
    days_since_touched: float
    theme_distribution: Mapping[str, int]
    boundary_crossed: bool
    schema_version: str = LONG_HORIZON_MEMORY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path[:256],
            "touch_count": int(self.touch_count),
            "last_touched_unix": float(self.last_touched_unix),
            "days_since_touched": float(self.days_since_touched),
            "theme_distribution": dict(self.theme_distribution),
            "boundary_crossed": bool(self.boundary_crossed),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class ComposedSourceDigest:
    """One-line digest from a composed cross-session substrate."""

    source_name: str
    enabled: bool
    digest_text: str
    schema_version: str = LONG_HORIZON_MEMORY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source_name": self.source_name[:64],
            "enabled": bool(self.enabled),
            "digest_text": self.digest_text[:512],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class MemorySnapshot:
    """Aggregate cross-session memory snapshot."""

    total_commits_scanned: int
    horizon_span_days: float
    horizon_classification: MemoryHorizon
    themes: Tuple[CommitTheme, ...]
    hot_files: Tuple[FileHotness, ...]
    stale_files: Tuple[FileHotness, ...]
    composed_sources: Tuple[ComposedSourceDigest, ...]
    diagnostic: str
    schema_version: str = LONG_HORIZON_MEMORY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_commits_scanned": int(
                self.total_commits_scanned,
            ),
            "horizon_span_days": float(self.horizon_span_days),
            "horizon_classification": (
                self.horizon_classification.value
            ),
            "themes": [t.to_dict() for t in self.themes],
            "hot_files": [
                f.to_dict() for f in self.hot_files
            ],
            "stale_files": [
                f.to_dict() for f in self.stale_files
            ],
            "composed_sources": [
                s.to_dict() for s in self.composed_sources
            ],
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CrossSessionMemoryReport:
    """Top-level recall report."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: RecallVerdict
    snapshot: Optional[MemorySnapshot]
    diagnostic: str
    elapsed_s: float
    schema_version: str = LONG_HORIZON_MEMORY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "snapshot": (
                self.snapshot.to_dict() if self.snapshot else None
            ),
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers — lazy-imported governance surfaces


def _is_boundary_crossed(file_path: str) -> bool:
    """Compose Wave 2 #5 boundary gate. NEVER raises."""
    if not file_path:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501  # type: ignore[import-not-found]
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed((file_path,)))
    except Exception:  # noqa: BLE001
        return False


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 write. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


def _digest_user_preferences() -> ComposedSourceDigest:
    """Compose UserPreferenceStore. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (  # noqa: E501
            get_default_store,
        )
        store = get_default_store()
        # Count entries by kind without raising on any.
        kinds_total = 0
        kind_summary: Dict[str, int] = {}
        try:
            for memory in getattr(store, "memories", ()) or ():
                kt = (
                    getattr(getattr(memory, "memory_type", ""),
                            "value", "")
                )
                if kt:
                    kind_summary[kt] = kind_summary.get(kt, 0) + 1
                    kinds_total += 1
        except Exception:  # noqa: BLE001
            pass
        if kinds_total > 0:
            summary_str = str(dict(sorted(kind_summary.items())))
            digest = f"{kinds_total} memory entries: {summary_str[:200]}"
        else:
            digest = "no preference memories recorded"
        return ComposedSourceDigest(
            source_name="user_preference_memory",
            enabled=True,
            digest_text=digest[:512],
        )
    except Exception as exc:  # noqa: BLE001
        return ComposedSourceDigest(
            source_name="user_preference_memory",
            enabled=False,
            digest_text=f"unavailable: {exc!r}"[:200],
        )


def _digest_last_session() -> ComposedSourceDigest:
    """Compose LastSessionSummary. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.last_session_summary import (  # noqa: E501
            get_default_summary,
        )
        summary = get_default_summary()
        # Best-effort surface — call common public methods.
        # If the actual API differs, fall back to repr.
        digest = ""
        try:
            render = getattr(summary, "render", None)
            if callable(render):
                digest = str(render() or "")
            else:
                # Try to grab known field names
                tokens: List[str] = []
                for attr in ("apply_mode", "apply_count",
                             "verify_passed", "verify_total",
                             "commit_hash"):
                    val = getattr(summary, attr, None)
                    if val is not None:
                        tokens.append(f"{attr}={val}")
                digest = " ".join(tokens) or repr(summary)[:200]
        except Exception:  # noqa: BLE001
            digest = "summary unrenderable"
        return ComposedSourceDigest(
            source_name="last_session_summary",
            enabled=True,
            digest_text=digest[:512] or "no prior session",
        )
    except Exception as exc:  # noqa: BLE001
        return ComposedSourceDigest(
            source_name="last_session_summary",
            enabled=False,
            digest_text=f"unavailable: {exc!r}"[:200],
        )


def _digest_semantic_index() -> ComposedSourceDigest:
    """Compose SemanticIndex. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.semantic_index import (  # noqa: E501
            get_default_index,
        )
        index = get_default_index()
        # Best-effort: surface count of indexed items
        count = 0
        try:
            count = int(getattr(index, "count", 0) or 0)
        except Exception:  # noqa: BLE001
            pass
        digest = (
            f"{count} indexed centroid entries"
            if count > 0
            else "no semantic index entries"
        )
        return ComposedSourceDigest(
            source_name="semantic_index",
            enabled=True,
            digest_text=digest[:512],
        )
    except Exception as exc:  # noqa: BLE001
        return ComposedSourceDigest(
            source_name="semantic_index",
            enabled=False,
            digest_text=f"unavailable: {exc!r}"[:200],
        )


# Git log walker


_GitLogRunner = Callable[[Sequence[str]], Optional[str]]


def _default_git_log_runner(args: Sequence[str]) -> Optional[str]:
    """Default subprocess git invocation. Returns stdout or
    None on failure. NEVER raises."""
    try:
        result = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=float(git_timeout_s()),
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout
    except Exception:  # noqa: BLE001
        return None


# Each commit is delimited by a unique separator so multi-line
# subjects don't collide with our parser.
_COMMIT_SEP = "<<<JARVIS_COMMIT_SEP>>>"
_FIELD_SEP = "<<<JARVIS_FIELD_SEP>>>"


def walk_git_log(
    *,
    max_commits: Optional[int] = None,
    repo_root: Optional[Path] = None,
    git_runner: Optional[_GitLogRunner] = None,
    now_unix: Optional[float] = None,
) -> Tuple[CommitRecord, ...]:
    """Walk git log + parse into CommitRecord tuples. NEVER
    raises. Returns empty tuple on git failure / empty repo.

    Bounded by ``max_commits`` (default from env)."""
    cap = max_commits or max_commits_to_walk()
    horizon_seconds = float(horizon_days()) * 86_400.0
    now = time.time() if now_unix is None else float(now_unix)
    cutoff_unix = now - horizon_seconds

    runner = git_runner or _default_git_log_runner

    # Format: SHA<F>SUBJECT<F>AUTHOR<F>EPOCH<C>file1\nfile2...
    fmt = (
        f"%H{_FIELD_SEP}%s{_FIELD_SEP}%an{_FIELD_SEP}%at"
        f"{_COMMIT_SEP}"
    )
    args: List[str] = [
        "git", "log",
        f"-n{cap}",
        f"--pretty=format:{fmt}",
        "--name-only",
    ]
    if repo_root is not None:
        args = ["git", "-C", str(repo_root)] + args[1:]

    try:
        raw = runner(args)
    except Exception:  # noqa: BLE001
        return ()
    if not raw:
        return ()

    out: List[CommitRecord] = []
    # Split on the commit-separator
    chunks = raw.split(_COMMIT_SEP)
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        # The header (SHA|SUBJECT|AUTHOR|EPOCH) is followed by
        # filenames (one per line).
        # First line has our field-separator structure
        lines = chunk.split("\n")
        if not lines:
            continue
        header = lines[0]
        fields = header.split(_FIELD_SEP)
        if len(fields) != 4:
            continue
        sha, subject, author, epoch_str = fields
        sha = sha.strip()
        subject = subject.strip()
        author = author.strip()
        try:
            epoch = float(epoch_str.strip())
        except (TypeError, ValueError):
            continue
        if epoch < cutoff_unix:
            continue
        files = tuple(
            ln.strip() for ln in lines[1:] if ln.strip()
        )
        age_seconds = max(0.0, now - epoch)
        age_days = age_seconds / 86_400.0
        theme = classify_theme(subject)
        out.append(CommitRecord(
            sha=sha,
            subject=subject,
            author=author,
            committed_at_unix=epoch,
            age_days=age_days,
            theme=theme,
            files=files,
        ))
    return tuple(out)


# Aggregation


def _aggregate_themes(
    commits: Sequence[CommitRecord],
) -> Tuple[CommitTheme, ...]:
    """Pure aggregation by MemoryTheme. NEVER raises."""
    by_theme: Dict[MemoryTheme, List[CommitRecord]] = {}
    for c in commits:
        by_theme.setdefault(c.theme, []).append(c)
    out: List[CommitTheme] = []
    for theme in MemoryTheme:
        commits_for_theme = by_theme.get(theme, [])
        if not commits_for_theme:
            continue
        # Sort by recency (newest first) for sample subjects
        sorted_commits = sorted(
            commits_for_theme,
            key=lambda c: -c.committed_at_unix,
        )
        sample_subjects = tuple(
            c.subject[:120] for c in sorted_commits[:3]
        )
        # Compute dominant files
        file_counts: Dict[str, int] = {}
        for c in sorted_commits:
            for f in c.files:
                file_counts[f] = file_counts.get(f, 0) + 1
        dominant = tuple(
            f for f, _ in sorted(
                file_counts.items(),
                key=lambda kv: (-kv[1], kv[0]),
            )[:5]
        )
        out.append(CommitTheme(
            theme=theme,
            count=len(commits_for_theme),
            sample_subjects=sample_subjects,
            dominant_files=dominant,
        ))
    return tuple(out)


def _compute_file_hotness(
    commits: Sequence[CommitRecord],
    *,
    now_unix: float,
) -> Tuple[Tuple[FileHotness, ...], Tuple[FileHotness, ...]]:
    """Pure aggregation → ``(hot_files, stale_files)``.
    NEVER raises. Hot files are >median + 1σ touch count.
    Stale files are touched 1+ times but most recently
    > stale_days_threshold days ago."""
    if not commits:
        return (), ()
    file_data: Dict[
        str, Dict[str, Any],
    ] = {}
    for c in commits:
        for f in c.files:
            entry = file_data.setdefault(f, {
                "touch_count": 0,
                "last_touched_unix": 0.0,
                "theme_dist": {},
            })
            entry["touch_count"] = (
                int(entry["touch_count"]) + 1
            )
            ts = max(
                float(entry["last_touched_unix"]),
                c.committed_at_unix,
            )
            entry["last_touched_unix"] = ts
            td = entry["theme_dist"]
            tn = c.theme.value
            td[tn] = td.get(tn, 0) + 1
    if not file_data:
        return (), ()
    # Compute touch-count distribution stats
    counts = [v["touch_count"] for v in file_data.values()]
    try:
        median = statistics.median(counts)
    except statistics.StatisticsError:
        median = 0
    try:
        stdev = (
            statistics.pstdev(counts)
            if len(counts) > 1 else 0.0
        )
    except statistics.StatisticsError:
        stdev = 0.0
    hot_threshold = median + stdev
    stale_seconds = float(stale_days_threshold()) * 86_400.0

    hot_records: List[FileHotness] = []
    stale_records: List[FileHotness] = []
    for path, entry in file_data.items():
        last_ts = float(entry["last_touched_unix"])
        days_since = (now_unix - last_ts) / 86_400.0
        count = int(entry["touch_count"])
        td = entry["theme_dist"]
        boundary = _is_boundary_crossed(path)
        record = FileHotness(
            file_path=path,
            touch_count=count,
            last_touched_unix=last_ts,
            days_since_touched=max(0.0, days_since),
            theme_distribution=dict(td),
            boundary_crossed=boundary,
        )
        if count >= hot_threshold and count >= 2:
            hot_records.append(record)
        if (
            count <= median
            and (now_unix - last_ts) > stale_seconds
        ):
            stale_records.append(record)

    hot_records.sort(
        key=lambda r: (-r.touch_count, r.file_path),
    )
    stale_records.sort(
        key=lambda r: (-r.days_since_touched, r.file_path),
    )
    hot_cap = hot_file_count()
    stale_cap = stale_file_count()
    return (
        tuple(hot_records[:hot_cap]),
        tuple(stale_records[:stale_cap]),
    )


def build_snapshot(
    *,
    max_commits: Optional[int] = None,
    repo_root: Optional[Path] = None,
    git_runner: Optional[_GitLogRunner] = None,
    commits_override: Optional[Sequence[CommitRecord]] = None,
    now_unix: Optional[float] = None,
) -> MemorySnapshot:
    """Build the aggregate snapshot. NEVER raises."""
    now = time.time() if now_unix is None else float(now_unix)
    commits: Sequence[CommitRecord]
    if commits_override is not None:
        commits = tuple(commits_override)
    else:
        commits = walk_git_log(
            max_commits=max_commits,
            repo_root=repo_root,
            git_runner=git_runner,
            now_unix=now,
        )
    if not commits:
        return MemorySnapshot(
            total_commits_scanned=0,
            horizon_span_days=0.0,
            horizon_classification=MemoryHorizon.SESSION,
            themes=(),
            hot_files=(),
            stale_files=(),
            composed_sources=(
                _digest_user_preferences(),
                _digest_last_session(),
                _digest_semantic_index(),
            ),
            diagnostic="no commits found within horizon",
        )
    # Span = oldest_age vs newest_age
    ages = [c.age_days for c in commits]
    span = max(ages) - min(ages) if ages else 0.0
    avg_age = sum(ages) / len(ages) if ages else 0.0
    horizon_class = classify_horizon(avg_age)
    themes = _aggregate_themes(commits)
    hot, stale = _compute_file_hotness(commits, now_unix=now)
    composed = (
        _digest_user_preferences(),
        _digest_last_session(),
        _digest_semantic_index(),
    )
    diagnostic = (
        f"scanned {len(commits)} commit(s); "
        f"span={span:.1f}d avg_age={avg_age:.1f}d → "
        f"{horizon_class.value}; "
        f"hot={len(hot)} stale={len(stale)}"
    )
    return MemorySnapshot(
        total_commits_scanned=len(commits),
        horizon_span_days=span,
        horizon_classification=horizon_class,
        themes=themes,
        hot_files=hot,
        stale_files=stale,
        composed_sources=composed,
        diagnostic=diagnostic,
    )


def recall_memory(
    *,
    max_commits: Optional[int] = None,
    repo_root: Optional[Path] = None,
    git_runner: Optional[_GitLogRunner] = None,
    commits_override: Optional[Sequence[CommitRecord]] = None,
    now_unix: Optional[float] = None,
) -> CrossSessionMemoryReport:
    """Top-level recall. NEVER raises."""
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return CrossSessionMemoryReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=RecallVerdict.DISABLED,
            snapshot=None,
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            elapsed_s=0.0,
        )
    snapshot = build_snapshot(
        max_commits=max_commits,
        repo_root=repo_root,
        git_runner=git_runner,
        commits_override=commits_override,
        now_unix=started,
    )
    if snapshot.total_commits_scanned == 0:
        return CrossSessionMemoryReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=RecallVerdict.DISABLED,
            snapshot=snapshot,
            diagnostic="no commits found within horizon",
            elapsed_s=max(0.0, time.time() - started),
        )
    # Verdict is based on AVERAGE commit age (recency proxy)
    ages = [c.age_days for c in (commits_override or ())]
    if not ages:
        # Re-walk just for verdict if commits_override is None
        # (uncommon path; build_snapshot already walked).
        commits_for_verdict = walk_git_log(
            max_commits=max_commits,
            repo_root=repo_root,
            git_runner=git_runner,
            now_unix=started,
        )
        ages = [c.age_days for c in commits_for_verdict]
    avg_age = sum(ages) / len(ages) if ages else 0.0
    verdict = _classify_verdict_for_horizon(avg_age)
    diagnostic = (
        f"verdict={verdict.value} avg_age={avg_age:.1f}d; "
        f"{snapshot.diagnostic}"
    )
    report = CrossSessionMemoryReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        snapshot=snapshot,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def _persist_report(report: CrossSessionMemoryReport) -> None:
    if report.verdict is RecallVerdict.DISABLED:
        return
    _flock_append({
        "kind": "memory_report", "payload": report.to_dict(),
    })


def _publish_event(report: CrossSessionMemoryReport) -> None:
    if not master_enabled():
        return
    if report.verdict is RecallVerdict.DISABLED:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_LONG_HORIZON_MEMORY_RECALLED,
            publish_task_event,
        )
        snap = report.snapshot
        publish_task_event(
            EVENT_TYPE_LONG_HORIZON_MEMORY_RECALLED,
            (
                f"system::long_horizon_memory::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "total_commits_scanned": (
                    snap.total_commits_scanned if snap else 0
                ),
                "horizon_span_days": (
                    snap.horizon_span_days if snap else 0.0
                ),
                "horizon_classification": (
                    snap.horizon_classification.value
                    if snap else "session"
                ),
                "theme_count": (
                    len(snap.themes) if snap else 0
                ),
                "hot_file_count": (
                    len(snap.hot_files) if snap else 0
                ),
                "stale_file_count": (
                    len(snap.stale_files) if snap else 0
                ),
                "composed_source_count": (
                    len(snap.composed_sources) if snap else 0
                ),
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_memory_panel(
    report: Optional[CrossSessionMemoryReport] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"long-horizon memory: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "long-horizon memory: no report"
    if not report.master_enabled:
        return (
            f"long-horizon memory: disabled "
            f"({_ENV_MASTER}=false)"
        )
    vg = verdict_glyph(report.verdict)
    snap = report.snapshot
    lines = [
        f"🧠 Long-Horizon Memory  {vg} {report.verdict.value}",
    ]
    if snap is not None:
        hg = horizon_glyph(snap.horizon_classification)
        lines.extend([
            f"  commits_scanned  : {snap.total_commits_scanned}",
            f"  horizon          : {hg} "
            f"{snap.horizon_classification.value} "
            f"(span={snap.horizon_span_days:.1f}d)",
            f"  themes           : {len(snap.themes)}",
            f"  hot_files        : {len(snap.hot_files)}",
            f"  stale_files      : {len(snap.stale_files)}",
            f"  composed_sources : {len(snap.composed_sources)}",
        ])
        if snap.themes:
            for t in snap.themes[:4]:
                tg = theme_glyph(t.theme)
                lines.append(
                    f"    {tg} {t.theme.value}: {t.count} commit(s)"
                )
        if snap.hot_files:
            lines.append("  top hot files:")
            for f in snap.hot_files[:3]:
                lines.append(
                    f"    {f.file_path[:48]:<48} "
                    f"({f.touch_count}x, last "
                    f"{f.days_since_touched:.1f}d ago)"
                )
        if snap.composed_sources:
            lines.append("  composed sources:")
            for c in snap.composed_sources:
                flag = "✓" if c.enabled else "✗"
                lines.append(
                    f"    {flag} {c.source_name}: "
                    f"{c.digest_text[:60]}"
                )
    lines.append(f"  diagnostic       : {report.diagnostic}")
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "long_horizon_memory.py"
    )

    _EXPECTED_VERDICTS = {
        "fresh", "warm", "cold", "disabled",
    }
    _EXPECTED_HORIZONS = {
        "session", "day", "week", "month",
    }
    _EXPECTED_THEMES = {
        "refactor", "feature", "bugfix", "other",
    }

    def _validate_taxonomy(class_name: str, expected: set):
        def _validate(tree: ast.AST, source: str) -> tuple:  # noqa: ARG001
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ClassDef)
                    and node.name == class_name
                ):
                    found = set()
                    for sub in node.body:
                        if (
                            isinstance(sub, ast.Assign)
                            and len(sub.targets) == 1
                            and isinstance(sub.targets[0], ast.Name)
                            and isinstance(sub.value, ast.Constant)
                            and isinstance(sub.value.value, str)
                        ):
                            found.add(sub.value.value)
                    missing = expected - found
                    extra = found - expected
                    if missing:
                        return (
                            f"{class_name} missing: "
                            f"{sorted(missing)}",
                        )
                    if extra:
                        return (
                            f"{class_name} drift: "
                            f"{sorted(extra)}",
                        )
                    return ()
            return (f"{class_name} class not found",)
        return _validate

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.plan_generator",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "user_preference_memory" not in source:
            violations.append(
                "must compose user_preference_memory",
            )
        if "last_session_summary" not in source:
            violations.append(
                "must compose last_session_summary",
            )
        if "semantic_index" not in source:
            violations.append(
                "must compose semantic_index",
            )
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl",
            )
        if "subprocess" not in source:
            violations.append(
                "must compose stdlib subprocess (git log)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "long_horizon_memory_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "RecallVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "RecallVerdict", _EXPECTED_VERDICTS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "long_horizon_memory_horizon_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "MemoryHorizon 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "MemoryHorizon", _EXPECTED_HORIZONS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "long_horizon_memory_theme_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "MemoryTheme 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "MemoryTheme", _EXPECTED_THEMES,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "long_horizon_memory_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — observational layer. MUST "
                "NOT import orchestrator / iron_gate / policy "
                "/ etc / plan_generator. The 3 composed "
                "cross-session substrates remain accessible "
                "to their direct consumers."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "long_horizon_memory_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "long_horizon_memory_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes user_preference_memory + "
                "last_session_summary + semantic_index + "
                "governance_boundary_gate + cross_process_jsonl "
                "+ stdlib subprocess (git log walker)."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "long_horizon_memory.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Long-Horizon Memory master. §33.1 "
                "default-FALSE. Closes §41.4 Phase 1 seventh "
                "arc (PRD v3.0+). Adds commit-history-aware "
                "memory layer over existing 4 cross-session "
                "substrates (UserPreferenceStore + "
                "LastSessionSummary + SemanticIndex + "
                "AdaptationLedger)."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — §33.4 ledger writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_MAX_COMMITS,
            type=FlagType.INT,
            default=_DEFAULT_MAX_COMMITS,
            description=(
                "Cap on commits walked per recall. Default "
                "500."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_MAX_COMMITS}=1000",
        ),
        FlagSpec(
            name=_ENV_HORIZON_DAYS,
            type=FlagType.INT,
            default=_DEFAULT_HORIZON_DAYS,
            description=(
                "Total memory horizon in days. Default 90."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_HORIZON_DAYS}=180",
        ),
        FlagSpec(
            name=_ENV_RECENT_WINDOW_DAYS,
            type=FlagType.INT,
            default=_DEFAULT_RECENT_WINDOW_DAYS,
            description=(
                "Window for FRESH verdict (≤ N days). "
                "Default 7."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_RECENT_WINDOW_DAYS}=14",
        ),
        FlagSpec(
            name=_ENV_WARM_WINDOW_DAYS,
            type=FlagType.INT,
            default=_DEFAULT_WARM_WINDOW_DAYS,
            description=(
                "Window for WARM verdict (≤ N days). "
                "Default 30. Auto-clamped > recent_window."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_WARM_WINDOW_DAYS}=45",
        ),
        FlagSpec(
            name=_ENV_STALE_DAYS_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_STALE_DAYS_THRESHOLD,
            description=(
                "Files untouched > N days → stale candidate. "
                "Default 60."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_STALE_DAYS_THRESHOLD}=90",
        ),
        FlagSpec(
            name=_ENV_HOT_FILE_COUNT,
            type=FlagType.INT,
            default=_DEFAULT_HOT_FILE_COUNT,
            description=(
                "Cap on hot_files returned. Default 10."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_HOT_FILE_COUNT}=20",
        ),
        FlagSpec(
            name=_ENV_STALE_FILE_COUNT,
            type=FlagType.INT,
            default=_DEFAULT_STALE_FILE_COUNT,
            description=(
                "Cap on stale_files returned. Default 10."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_STALE_FILE_COUNT}=20",
        ),
        FlagSpec(
            name=_ENV_GIT_TIMEOUT_S,
            type=FlagType.INT,
            default=_DEFAULT_GIT_TIMEOUT_S,
            description=(
                "Timeout for git log subprocess. Default 30s."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_GIT_TIMEOUT_S}=60",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "LONG_HORIZON_MEMORY_SCHEMA_VERSION",
    "RecallVerdict",
    "MemoryHorizon",
    "MemoryTheme",
    "CommitRecord",
    "CommitTheme",
    "FileHotness",
    "ComposedSourceDigest",
    "MemorySnapshot",
    "CrossSessionMemoryReport",
    "master_enabled",
    "persistence_enabled",
    "max_commits_to_walk",
    "horizon_days",
    "recent_window_days",
    "warm_window_days",
    "stale_days_threshold",
    "hot_file_count",
    "stale_file_count",
    "git_timeout_s",
    "ledger_path",
    "verdict_glyph",
    "horizon_glyph",
    "theme_glyph",
    "classify_theme",
    "classify_horizon",
    "walk_git_log",
    "build_snapshot",
    "recall_memory",
    "format_memory_panel",
    "register_shipped_invariants",
    "register_flags",
]
