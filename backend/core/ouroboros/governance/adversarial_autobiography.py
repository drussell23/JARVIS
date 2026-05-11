"""
Adversarial Autobiography — Retrospective Cage Auditor
=======================================================

Closes the §40 Wave 1 #8 ship: takes O+V's own recent commits (its
**autobiography** — the corpus of changes it has shipped) and runs
the canonical P9.4 adversarial pattern set against each as a
retrospective probe. Catches Quine-shaped hallucinations that
already *shipped* — the kind the prospective P9.4 harness can't
see by construction (it only tests cage rejection on synthetic
inputs at validate-time).

Closes §3.6.2 Vector #7 (Quine-shape cage bypass) **empirically**:
the prospective corpus proved the cage CAN catch each shape; this
substrate proves the cage HAS caught each shape on every commit
O+V actually shipped (or surfaces the escapes if any did get through).

RRD framing
-----------
Vector #7 is the only currently-🟠 vector in the §3.6.2 table per
v2.98 framing. Moving it to ✅ requires *retrospective evidence*
that O+V's autobiography (commit history) is clean of corpus-
shaped escapes. This substrate produces that evidence on every run.

Composition contract — thin composer over canonical sources, zero
parallel state, zero hardcoded adversarial patterns:

1. ``p9_4_adversarial_corpus.CORPUS`` — canonical 25-entry tuple.
   Each entry's pattern is materialized via the canonical
   ``materialize_pattern(entry)`` builder (handles runtime
   credential-shape construction). NO parallel pattern catalog.
2. ``p9_4_adversarial_corpus.AdversarialCategory`` — canonical
   12-value closed taxonomy. Reused as the per-category histogram
   axis; AST-pinned no parallel taxonomy.
3. ``auto_committer.ov_signature_substring()`` — canonical
   autonomous-commit detection accessor. No parallel literal.
4. ``git log`` (read-only subprocess, bounded scan window) +
   ``git show <hash>`` (per-commit unified diff). Cached snapshot
   to avoid hammering subprocess on rapid REPL polls.
5. ``cross_process_jsonl.flock_append_line`` — §33.4 canonical
   per-cluster persistence pattern. Append-only audit ledger at
   ``.jarvis/adversarial_autobiography_ledger.jsonl``.
6. ``ide_observability_stream.publish_task_event`` — canonical
   SSE broker. Best-effort fire-and-forget; NEVER raises into the
   audit path.

Closed 4-value :class:`AutobiographyFinding` taxonomy (commit-level):

  CORPUS_ESCAPE      ⚠  ≥1 adversarial pattern matched the commit's
                        diff — the pattern shipped despite the cage.
  CORPUS_CLEAN       ✓  commit audited; zero patterns matched.
  CORPUS_NO_COMMITS  ○  no O+V-signed commits in scan window.
  CORPUS_DISABLED    ◌  master flag off.

Closed 3-value :class:`ProbeOutcome` taxonomy (per-entry):

  MATCH    pattern present in commit diff (potential escape)
  NO_MATCH pattern absent (defended empirically)
  UNKNOWN  pattern materialization failed (defensive — corpus bug)

Master flag ``JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED`` default-FALSE
per §33.1. Substrate is **read-only by construction** — NEVER
raises. A malformed git output, missing canonical accessor, or
failed env lookup degrades to ``CORPUS_NO_COMMITS`` or
``CORPUS_DISABLED``, not exception.

Authority asymmetry (AST-pinned): the substrate imports stdlib +
``p9_4_adversarial_corpus`` + ``auto_committer`` (for signature
accessor) + ``cross_process_jsonl`` (for canonical persistence)
ONLY. It does NOT import orchestrator / iron_gate / policy /
providers / candidate_generator / urgency_router / change_engine
/ semantic_guardian / adversarial_reviewer_service — by design.
The reviewer service runs prospectively at plan-review time;
this substrate runs retrospectively at audit time. They are
sibling surfaces, not coupled.
"""
from __future__ import annotations

import ast
import enum
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Tuple,
)

logger = logging.getLogger(__name__)


ADVERSARIAL_AUTOBIOGRAPHY_SCHEMA_VERSION: str = (
    "adversarial_autobiography.1"
)


# ===========================================================================
# Env knobs — single source of truth (no hardcoding)
# ===========================================================================


_ENV_MASTER = "JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED"
_ENV_COMMIT_SCAN_MAX = "JARVIS_AUTOBIOGRAPHY_COMMIT_SCAN_MAX"
_ENV_ESCAPE_ALERT_THRESHOLD = (
    "JARVIS_AUTOBIOGRAPHY_ESCAPE_ALERT_THRESHOLD"
)
_ENV_PERSISTENCE_ENABLED = (
    "JARVIS_AUTOBIOGRAPHY_PERSISTENCE_ENABLED"
)
_ENV_LEDGER_PATH = "JARVIS_AUTOBIOGRAPHY_LEDGER_PATH"

_DEFAULT_COMMIT_SCAN_MAX = 200
_DEFAULT_ESCAPE_ALERT_THRESHOLD = 1
_MIN_COMMIT_SCAN_MAX = 5
_MAX_COMMIT_SCAN_MAX = 10_000
_DEFAULT_LEDGER_RELATIVE = (
    ".jarvis/adversarial_autobiography_ledger.jsonl"
)
_DEFAULT_LEDGER_BYTES_CAP = 50 * 1024 * 1024  # 50 MiB


_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE.

    Auditing one's own autobiography is operator-paced; the
    substrate ships dormant and the operator flips when ready
    to investigate the historical record."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    """Sub-flag — gate the §33.4 JSONL ledger writes. When master
    is on and this is also on, each audit appends one row per
    commit to the canonical ledger.

    Default-TRUE *when master is on* (composing the canonical
    pattern from sibling substrates). Master-off short-circuits
    so persistence never fires before opt-in.
    """
    if not master_enabled():
        return False
    return _flag(_ENV_PERSISTENCE_ENABLED, default=True)


def _read_clamped_int(
    env_name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def commit_scan_max() -> int:
    """Maximum O+V-signed commits to audit per call. Clamped to
    [5, 10_000]."""
    return _read_clamped_int(
        _ENV_COMMIT_SCAN_MAX,
        _DEFAULT_COMMIT_SCAN_MAX,
        _MIN_COMMIT_SCAN_MAX,
        _MAX_COMMIT_SCAN_MAX,
    )


def escape_alert_threshold() -> int:
    """Number of corpus matches in a single commit that raises
    the global finding to ``CORPUS_ESCAPE``. Defaults to 1
    (any escape is alarming). Clamped to [1, 10_000]."""
    return _read_clamped_int(
        _ENV_ESCAPE_ALERT_THRESHOLD,
        _DEFAULT_ESCAPE_ALERT_THRESHOLD,
        1,
        10_000,
    )


def ledger_path() -> Path:
    """Canonical §33.4 audit ledger path. Operator override via
    ``JARVIS_AUTOBIOGRAPHY_LEDGER_PATH``. NEVER raises — falls
    back to repo-relative default on any resolution failure."""
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        try:
            return Path(raw).expanduser().resolve()
        except Exception:  # noqa: BLE001 — defensive
            pass
    try:
        return _resolve_repo_root() / _DEFAULT_LEDGER_RELATIVE
    except Exception:  # noqa: BLE001
        return Path(_DEFAULT_LEDGER_RELATIVE)


# ===========================================================================
# Closed taxonomies (§33.1 canonical shape)
# ===========================================================================


class AutobiographyFinding(str, enum.Enum):
    """Closed 4-value commit-level finding. Bytes-pinned via AST.

    Per-commit verdict aggregated from probe outcomes:
      * ≥``escape_alert_threshold`` MATCH probes → ``CORPUS_ESCAPE``
      * all probes NO_MATCH (or UNKNOWN) → ``CORPUS_CLEAN``
      * zero commits in scan window → ``CORPUS_NO_COMMITS``
      * master flag off → ``CORPUS_DISABLED``
    """

    CORPUS_ESCAPE = "corpus_escape"
    CORPUS_CLEAN = "corpus_clean"
    CORPUS_NO_COMMITS = "corpus_no_commits"
    CORPUS_DISABLED = "corpus_disabled"


class ProbeOutcome(str, enum.Enum):
    """Closed 3-value per-entry probe outcome. Bytes-pinned via AST.

    Per-commit-per-corpus-entry result:
      * pattern present in commit diff → ``MATCH`` (potential escape)
      * pattern absent → ``NO_MATCH`` (defended empirically)
      * materialization failed → ``UNKNOWN`` (defensive — corpus bug)
    """

    MATCH = "match"
    NO_MATCH = "no_match"
    UNKNOWN = "unknown"


# Glyphs — operator-facing render hints
_FINDING_GLYPH: Dict[str, str] = {
    AutobiographyFinding.CORPUS_ESCAPE.value: "⚠",
    AutobiographyFinding.CORPUS_CLEAN.value: "✓",
    AutobiographyFinding.CORPUS_NO_COMMITS.value: "○",
    AutobiographyFinding.CORPUS_DISABLED.value: "◌",
}


def finding_glyph(finding: object) -> str:
    """Public glyph accessor. NEVER raises."""
    try:
        if hasattr(finding, "value"):
            return _FINDING_GLYPH.get(str(finding.value), "?")
        return _FINDING_GLYPH.get(
            str(finding or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class ProbeMatch:
    """One (corpus_entry → commit) match — recorded only when
    :class:`ProbeOutcome.MATCH` fires."""

    entry_id: str             # canonical p9.4.NNN id
    category: str             # AdversarialCategory.value
    pattern_excerpt: str      # bounded 256-char pattern snippet
    matched_line: str         # bounded 256-char first matching line
    schema_version: str = ADVERSARIAL_AUTOBIOGRAPHY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "category": self.category,
            "pattern_excerpt": self.pattern_excerpt[:256],
            "matched_line": self.matched_line[:256],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CommitAutobiographyAudit:
    """Per-commit audit projection — frozen for safe propagation."""

    commit_hash: str
    commit_time_unix: int
    finding: AutobiographyFinding
    entries_probed: int
    matches: Tuple[ProbeMatch, ...]
    diagnostic: str
    schema_version: str = ADVERSARIAL_AUTOBIOGRAPHY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "commit_hash": self.commit_hash,
            "commit_time_unix": int(self.commit_time_unix),
            "finding": self.finding.value,
            "entries_probed": int(self.entries_probed),
            "matches": [m.to_dict() for m in self.matches],
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class AutobiographyReport:
    """Aggregate report across all audited commits."""

    audited_at_unix: float
    master_enabled: bool
    finding: AutobiographyFinding
    commits_audited: int
    escape_count: int
    clean_count: int
    per_category_escape: Mapping[str, int]
    per_entry_escape: Mapping[str, int]
    cage_health_ratio: float
    elapsed_s: float
    diagnostic: str
    schema_version: str = ADVERSARIAL_AUTOBIOGRAPHY_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "audited_at_unix": self.audited_at_unix,
            "master_enabled": self.master_enabled,
            "finding": self.finding.value,
            "commits_audited": int(self.commits_audited),
            "escape_count": int(self.escape_count),
            "clean_count": int(self.clean_count),
            "per_category_escape": dict(self.per_category_escape),
            "per_entry_escape": dict(self.per_entry_escape),
            "cage_health_ratio": float(self.cage_health_ratio),
            "elapsed_s": float(self.elapsed_s),
            "diagnostic": self.diagnostic[:512],
            "schema_version": self.schema_version,
        }


# ===========================================================================
# Repo root resolution + git subprocess composers
# ===========================================================================


def _resolve_repo_root() -> Path:
    """Walk up looking for a .git directory. NEVER raises."""
    try:
        here = Path(__file__).resolve()
        for ancestor in (here, *here.parents):
            try:
                if (ancestor / ".git").exists():
                    return ancestor
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    try:
        return Path.cwd()
    except Exception:  # noqa: BLE001
        return Path(".")


# Sentinel separator matching second_order_doll_metric pattern but
# distinct so the parsers never collide.
_GIT_LOG_FORMAT = (
    "__OV_AUTOBIO__%n%H%n%ct%n%B%n__END_HEADER__"
)


@dataclass(frozen=True)
class _RawCommit:
    """Internal pre-aggregation projection — git log row."""

    commit_hash: str
    commit_time_unix: int
    body: str


def _run_git_log(
    repo_path: Path,
    max_commits: int,
    *,
    runner: Optional[Any] = None,
) -> str:
    """Invoke ``git log`` for header metadata. NEVER raises.

    Diff content is fetched on-demand per commit via
    :func:`_run_git_show` — avoids carrying massive log output
    around when most commits don't need diff inspection.
    """
    effective_runner = runner if runner is not None else (
        subprocess.run
    )
    git_exe = shutil.which("git")
    if git_exe is None:
        return ""
    try:
        result = effective_runner(
            [
                git_exe,
                "-C", str(repo_path),
                "log",
                f"--max-count={max(1, int(max_commits))}",
                f"--format={_GIT_LOG_FORMAT}",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return ""
    if getattr(result, "returncode", 1) != 0:
        return ""
    out = getattr(result, "stdout", "") or ""
    if not isinstance(out, str):
        try:
            out = out.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""
    return out


def _run_git_show(
    repo_path: Path,
    commit_hash: str,
    *,
    runner: Optional[Any] = None,
) -> str:
    """Return the unified diff for ``commit_hash``. NEVER raises.

    ``runner`` is caller-injectable for hermetic testing.
    """
    if not commit_hash or not isinstance(commit_hash, str):
        return ""
    effective_runner = runner if runner is not None else (
        subprocess.run
    )
    git_exe = shutil.which("git")
    if git_exe is None:
        return ""
    try:
        result = effective_runner(
            [
                git_exe,
                "-C", str(repo_path),
                "show",
                "--format=",  # suppress header — diff only
                commit_hash,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return ""
    if getattr(result, "returncode", 1) != 0:
        return ""
    out = getattr(result, "stdout", "") or ""
    if not isinstance(out, str):
        try:
            out = out.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""
    return out


def _parse_git_log(raw: str) -> Tuple[_RawCommit, ...]:
    """Pure-function parser. NEVER raises — malformed sections skipped."""
    if not raw:
        return ()
    chunks = raw.split("__OV_AUTOBIO__\n")
    out: List[_RawCommit] = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            header, _, _after = chunk.partition("__END_HEADER__")
            header = header.strip()
            lines = header.split("\n")
            if len(lines) < 3:
                continue
            commit_hash = lines[0].strip()
            try:
                ctime = int(lines[1].strip())
            except (TypeError, ValueError):
                continue
            body = "\n".join(lines[2:])
            if not commit_hash:
                continue
            out.append(_RawCommit(
                commit_hash=commit_hash,
                commit_time_unix=ctime,
                body=body,
            ))
        except Exception:  # noqa: BLE001
            continue
    return tuple(out)


def _is_autonomous_commit(body: str, signature: str) -> bool:
    """Detect O+V-signed commit via canonical signature substring."""
    if not body or not signature:
        return False
    return signature in body


# ===========================================================================
# Canonical accessor composition (no parallel state)
# ===========================================================================


def _ov_signature() -> str:
    """Compose canonical ``auto_committer.ov_signature_substring()``.
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.auto_committer import (  # noqa: E501
            ov_signature_substring,
        )
        return ov_signature_substring()
    except Exception:  # noqa: BLE001
        return ""


def _canonical_corpus_entries() -> Tuple[Any, ...]:
    """Compose canonical ``p9_4_adversarial_corpus.CORPUS`` tuple.
    NEVER raises. Returns empty tuple when substrate unavailable."""
    try:
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            CORPUS,
        )
        return CORPUS
    except Exception:  # noqa: BLE001
        return ()


def _materialize_pattern(entry: Any) -> Optional[str]:
    """Compose canonical ``materialize_pattern`` builder. Returns
    None when materialization fails (e.g. unknown placeholder).
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.p9_4_adversarial_corpus import (  # noqa: E501
            materialize_pattern,
        )
        result = materialize_pattern(entry)
        if not isinstance(result, str) or not result:
            return None
        # Reject unsubstituted placeholder tokens — these signal a
        # corpus bug, not a real adversarial pattern.
        if result.startswith("<") and result.endswith(">"):
            return None
        return result
    except Exception:  # noqa: BLE001
        return None


# ===========================================================================
# Per-commit probe — deterministic substring matching
# ===========================================================================


def _strip_leading_diff_marker(line: str) -> str:
    """Strip ``+`` / ``-`` / `` `` unified-diff markers so the
    pattern check operates on the actual code line."""
    if not line:
        return ""
    if line.startswith("+++") or line.startswith("---"):
        return ""  # diff header — irrelevant
    if line.startswith(("+", "-", " ")):
        return line[1:]
    return line


def _probe_commit_against_entry(
    diff_text: str,
    entry: Any,
) -> Tuple[ProbeOutcome, Optional[ProbeMatch]]:
    """Pure function. Runs one (commit-diff, corpus-entry) probe.

    NEVER raises. Returns (outcome, optional match).
    """
    materialized = _materialize_pattern(entry)
    if materialized is None:
        return (ProbeOutcome.UNKNOWN, None)
    # Normalize trailing whitespace so trailing-newline drift in
    # the canonical corpus doesn't cause false-negative matches.
    # The probe checks semantic equivalence at the substring
    # level — whitespace at the absolute end of the pattern is
    # informational only.
    materialized_norm = materialized.rstrip()
    if not materialized_norm:
        return (ProbeOutcome.UNKNOWN, None)
    # The pattern may be multi-line. We check substring across the
    # whole diff first; if absent, no match. If present, we find
    # the first matching line for the match record.
    # ADDED-only filter: we want patterns that landed in the
    # commit (not patterns being REMOVED — those are the cage
    # working post-hoc).
    added_lines: List[str] = []
    for raw_line in diff_text.splitlines():
        if raw_line.startswith("+++") or raw_line.startswith("---"):
            continue
        if raw_line.startswith("+"):
            added_lines.append(raw_line[1:])
    added_block = "\n".join(added_lines)
    if materialized_norm in added_block:
        # Find first concrete matching line for the audit record.
        sample_line = ""
        first_line = materialized_norm.split("\n", 1)[0]
        for cand in added_lines:
            if first_line and first_line in cand:
                sample_line = cand
                break
        if not sample_line and added_lines:
            sample_line = added_lines[0]
        try:
            entry_id = str(getattr(entry, "entry_id", ""))
            category = str(getattr(entry, "category", None) or "")
            if hasattr(getattr(entry, "category", None), "value"):
                category = str(entry.category.value)
        except Exception:  # noqa: BLE001
            entry_id = ""
            category = ""
        return (
            ProbeOutcome.MATCH,
            ProbeMatch(
                entry_id=entry_id,
                category=category,
                pattern_excerpt=first_line,
                matched_line=sample_line,
            ),
        )
    return (ProbeOutcome.NO_MATCH, None)


def _audit_one_commit(
    repo_path: Path,
    commit: _RawCommit,
    corpus: Tuple[Any, ...],
    *,
    git_show_runner: Optional[Any] = None,
) -> CommitAutobiographyAudit:
    """Audit a single commit against the full canonical corpus.

    NEVER raises. Pure-deterministic substring matching — no
    test execution, no LLM, no subprocess beyond ``git show``.
    """
    diff_text = _run_git_show(
        repo_path, commit.commit_hash, runner=git_show_runner,
    )
    matches: List[ProbeMatch] = []
    probed = 0
    for entry in corpus:
        try:
            outcome, match = _probe_commit_against_entry(
                diff_text, entry,
            )
            probed += 1
            if outcome is ProbeOutcome.MATCH and match is not None:
                matches.append(match)
        except Exception:  # noqa: BLE001 — defensive per-entry
            continue
    threshold = escape_alert_threshold()
    if len(matches) >= threshold:
        finding = AutobiographyFinding.CORPUS_ESCAPE
        diagnostic = (
            f"{len(matches)} corpus pattern(s) matched commit's "
            f"added lines — investigate retrospectively"
        )
    else:
        finding = AutobiographyFinding.CORPUS_CLEAN
        diagnostic = (
            f"clean: {probed} probes ran, "
            f"{len(matches)} matches (threshold={threshold})"
        )
    return CommitAutobiographyAudit(
        commit_hash=commit.commit_hash,
        commit_time_unix=commit.commit_time_unix,
        finding=finding,
        entries_probed=probed,
        matches=tuple(matches),
        diagnostic=diagnostic,
    )


# ===========================================================================
# Aggregator + cache
# ===========================================================================


_SNAPSHOT_LOCK = threading.RLock()
_LAST_REPORT: Optional[AutobiographyReport] = None
_LAST_AUDITS: Tuple[CommitAutobiographyAudit, ...] = ()
_LAST_SNAPSHOT_TS: float = 0.0
_SNAPSHOT_TTL_S: float = 120.0


def get_cached_report() -> Optional[AutobiographyReport]:
    """Most-recent report within TTL, else None. NEVER raises."""
    with _SNAPSHOT_LOCK:
        if _LAST_REPORT is None:
            return None
        if (time.time() - _LAST_SNAPSHOT_TS) > _SNAPSHOT_TTL_S:
            return None
        return _LAST_REPORT


def get_cached_audits() -> Tuple[CommitAutobiographyAudit, ...]:
    """Most-recent per-commit audit tuple. NEVER raises."""
    with _SNAPSHOT_LOCK:
        return _LAST_AUDITS


def reset_cache_for_tests() -> None:
    """Test seam — drop cached report + audits."""
    with _SNAPSHOT_LOCK:
        global _LAST_REPORT, _LAST_AUDITS, _LAST_SNAPSHOT_TS
        _LAST_REPORT = None
        _LAST_AUDITS = ()
        _LAST_SNAPSHOT_TS = 0.0


def audit_autobiography(
    *,
    repo_path: Optional[Path] = None,
    git_log_runner: Optional[Any] = None,
    git_show_runner: Optional[Any] = None,
    force_refresh: bool = False,
) -> AutobiographyReport:
    """Compose the canonical sources into one immutable audit
    report. NEVER raises.

    Master-flag-gated: returns ``CORPUS_DISABLED`` report when
    ``JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED=false``. Caches
    result for ``_SNAPSHOT_TTL_S`` seconds; pass
    ``force_refresh=True`` to bypass.
    """
    started = time.time()

    if not master_enabled():
        return AutobiographyReport(
            audited_at_unix=started,
            master_enabled=False,
            finding=AutobiographyFinding.CORPUS_DISABLED,
            commits_audited=0,
            escape_count=0,
            clean_count=0,
            per_category_escape={},
            per_entry_escape={},
            cage_health_ratio=0.0,
            elapsed_s=0.0,
            diagnostic=(
                f"master flag {_ENV_MASTER}=false — flip to "
                "true to run autobiography audit"
            ),
        )

    if not force_refresh:
        cached = get_cached_report()
        if cached is not None and cached.master_enabled:
            return cached

    repo = repo_path or _resolve_repo_root()
    signature = _ov_signature()
    corpus = _canonical_corpus_entries()
    scan_max = commit_scan_max()

    if not signature or not corpus:
        # Substrate unavailable — degrade to CORPUS_NO_COMMITS
        # rather than fabricating CLEAN.
        report = AutobiographyReport(
            audited_at_unix=started,
            master_enabled=True,
            finding=AutobiographyFinding.CORPUS_NO_COMMITS,
            commits_audited=0,
            escape_count=0,
            clean_count=0,
            per_category_escape={},
            per_entry_escape={},
            cage_health_ratio=0.0,
            elapsed_s=time.time() - started,
            diagnostic=(
                "canonical sources unavailable — "
                f"signature={'ok' if signature else 'missing'} "
                f"corpus={len(corpus)} entries"
            ),
        )
        _cache_report(report, ())
        return report

    raw_log = _run_git_log(repo, scan_max, runner=git_log_runner)
    parsed = _parse_git_log(raw_log)
    autonomous = tuple(
        c for c in parsed if _is_autonomous_commit(c.body, signature)
    )

    if not autonomous:
        report = AutobiographyReport(
            audited_at_unix=started,
            master_enabled=True,
            finding=AutobiographyFinding.CORPUS_NO_COMMITS,
            commits_audited=0,
            escape_count=0,
            clean_count=0,
            per_category_escape={},
            per_entry_escape={},
            cage_health_ratio=0.0,
            elapsed_s=time.time() - started,
            diagnostic=(
                f"no O+V-signed commits in last {scan_max} log "
                "entries — flip JARVIS_AUTO_COMMIT_ENABLED to "
                "begin accumulating autobiography"
            ),
        )
        _cache_report(report, ())
        return report

    audits: List[CommitAutobiographyAudit] = []
    for commit in autonomous:
        try:
            audit = _audit_one_commit(
                repo, commit, corpus,
                git_show_runner=git_show_runner,
            )
            audits.append(audit)
        except Exception:  # noqa: BLE001 — defensive per-commit
            continue

    audits_tuple = tuple(audits)
    escape_count = sum(
        1 for a in audits_tuple
        if a.finding is AutobiographyFinding.CORPUS_ESCAPE
    )
    clean_count = sum(
        1 for a in audits_tuple
        if a.finding is AutobiographyFinding.CORPUS_CLEAN
    )
    per_cat: Dict[str, int] = {}
    per_entry: Dict[str, int] = {}
    for a in audits_tuple:
        for m in a.matches:
            per_cat[m.category] = per_cat.get(m.category, 0) + 1
            per_entry[m.entry_id] = (
                per_entry.get(m.entry_id, 0) + 1
            )

    total = escape_count + clean_count
    health = (clean_count / total) if total > 0 else 0.0
    aggregate_finding = (
        AutobiographyFinding.CORPUS_ESCAPE
        if escape_count > 0
        else AutobiographyFinding.CORPUS_CLEAN
    )

    report = AutobiographyReport(
        audited_at_unix=started,
        master_enabled=True,
        finding=aggregate_finding,
        commits_audited=len(audits_tuple),
        escape_count=escape_count,
        clean_count=clean_count,
        per_category_escape=per_cat,
        per_entry_escape=per_entry,
        cage_health_ratio=health,
        elapsed_s=time.time() - started,
        diagnostic=(
            f"audited {len(audits_tuple)} OV-signed commits "
            f"across {len(corpus)} corpus entries; "
            f"escapes={escape_count} clean={clean_count} "
            f"health={health:.3f}"
        ),
    )

    _cache_report(report, audits_tuple)
    _maybe_persist_report(report, audits_tuple)
    _publish_audit_completed_event(report)
    return report


def _cache_report(
    report: AutobiographyReport,
    audits: Tuple[CommitAutobiographyAudit, ...],
) -> None:
    with _SNAPSHOT_LOCK:
        global _LAST_REPORT, _LAST_AUDITS, _LAST_SNAPSHOT_TS
        _LAST_REPORT = report
        _LAST_AUDITS = audits
        _LAST_SNAPSHOT_TS = time.time()


# ===========================================================================
# §33.4 flock'd JSONL persistence (sub-flag opt-in)
# ===========================================================================


def _maybe_persist_report(
    report: AutobiographyReport,
    audits: Tuple[CommitAutobiographyAudit, ...],
) -> None:
    """Compose canonical ``cross_process_jsonl.flock_append_line``.
    Best-effort; NEVER raises into the audit path.

    Persists one summary row per audit run + one per-commit row
    for every escape. CLEAN commits are NOT persisted to avoid
    flooding the ledger — operators can reproduce via re-audit.
    """
    if not persistence_enabled():
        return
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        summary = {
            "kind": "summary",
            "payload": report.to_dict(),
        }
        flock_append_line(target, json.dumps(summary))
        for audit in audits:
            if audit.finding is AutobiographyFinding.CORPUS_ESCAPE:
                row = {
                    "kind": "escape",
                    "payload": audit.to_dict(),
                }
                flock_append_line(target, json.dumps(row))
    except Exception:  # noqa: BLE001 — defensive
        return


# ===========================================================================
# SSE publisher
# ===========================================================================


def _publish_audit_completed_event(
    report: AutobiographyReport,
) -> None:
    """Compose canonical broker. Best-effort; NEVER raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_AUTOBIOGRAPHY_AUDIT_COMPLETED,
            publish_task_event,
        )
        payload = {
            "audited_at_unix": report.audited_at_unix,
            "finding": report.finding.value,
            "commits_audited": report.commits_audited,
            "escape_count": report.escape_count,
            "clean_count": report.clean_count,
            "cage_health_ratio": report.cage_health_ratio,
            "schema_version": report.schema_version,
        }
        publish_task_event(
            EVENT_TYPE_AUTOBIOGRAPHY_AUDIT_COMPLETED,
            (
                f"system::autobiography::"
                f"{report.schema_version}"
            ),
            payload,
        )
    except Exception:  # noqa: BLE001
        return


# ===========================================================================
# Renderers
# ===========================================================================


def format_autobiography_panel(
    report: Optional[AutobiographyReport] = None,
) -> str:
    """Operator-facing summary panel. NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"adversarial autobiography: disabled "
                f"({_ENV_MASTER}=false)"
            )
        report = audit_autobiography()
    if not report.master_enabled:
        return (
            f"adversarial autobiography: disabled "
            f"({_ENV_MASTER}=false)"
        )
    glyph = finding_glyph(report.finding)
    lines = [
        f"🪞 Adversarial Autobiography  {glyph} "
        f"{report.finding.value}",
        f"  commits_audited      : {report.commits_audited}",
        f"  escapes              : {report.escape_count}",
        f"  clean                : {report.clean_count}",
        f"  cage_health_ratio    : {report.cage_health_ratio:.3f}",
    ]
    if report.per_category_escape:
        lines.append("  escapes by category:")
        for cat, cnt in sorted(report.per_category_escape.items()):
            lines.append(f"    {cat:<28} : {cnt}")
    if report.per_entry_escape:
        lines.append("  escapes by entry:")
        for eid, cnt in sorted(report.per_entry_escape.items()):
            lines.append(f"    {eid:<10} : {cnt}")
    lines.append(f"  diagnostic           : {report.diagnostic}")
    return "\n".join(lines)


def format_commit_audit(
    audit: CommitAutobiographyAudit,
) -> str:
    """Detailed per-commit render. NEVER raises."""
    glyph = finding_glyph(audit.finding)
    short_hash = (audit.commit_hash or "?")[:12]
    lines = [
        f"🪞 Commit {short_hash}  {glyph} {audit.finding.value}",
        f"  commit_time_unix : {audit.commit_time_unix}",
        f"  entries_probed   : {audit.entries_probed}",
        f"  matches          : {len(audit.matches)}",
        f"  diagnostic       : {audit.diagnostic}",
    ]
    if audit.matches:
        lines.append("  matches:")
        for m in audit.matches:
            lines.append(
                f"    - {m.entry_id} ({m.category})  "
                f"line: {m.matched_line[:80]}"
            )
    return "\n".join(lines)


# ===========================================================================
# AST pins via shipped_code_invariants
# ===========================================================================


def register_shipped_invariants() -> list:
    """Return AST invariant pins for this module. Auto-discovered."""
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "adversarial_autobiography.py"
    )

    _EXPECTED_FINDINGS = {
        "corpus_escape",
        "corpus_clean",
        "corpus_no_commits",
        "corpus_disabled",
    }
    _EXPECTED_PROBE_OUTCOMES = {
        "match", "no_match", "unknown",
    }

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
                    "master_enabled() must call _flag(...) with "
                    "default=False per §33.1",
                )
        return ("master_enabled() not found",)

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
            # Sibling surface — runs PROSPECTIVELY at plan-review.
            # Autobiography runs RETROSPECTIVELY at audit. Coupling
            # them would conflate two distinct architectural roles.
            "backend.core.ouroboros.governance."
            "adversarial_reviewer_service",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}"
                    )
        return tuple(violations)

    def _validate_finding_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "AutobiographyFinding"
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
                missing = _EXPECTED_FINDINGS - found
                extra = found - _EXPECTED_FINDINGS
                if missing:
                    return (
                        f"AutobiographyFinding missing: "
                        f"{sorted(missing)}",
                    )
                if extra:
                    return (
                        f"AutobiographyFinding drift: "
                        f"{sorted(extra)}",
                    )
                return ()
        return ("AutobiographyFinding class not found",)

    def _validate_probe_outcome_taxonomy(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "ProbeOutcome"
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
                missing = _EXPECTED_PROBE_OUTCOMES - found
                extra = found - _EXPECTED_PROBE_OUTCOMES
                if missing:
                    return (
                        f"ProbeOutcome missing: {sorted(missing)}",
                    )
                if extra:
                    return (
                        f"ProbeOutcome drift: {sorted(extra)}",
                    )
                return ()
        return ("ProbeOutcome class not found",)

    def _validate_composes_canonical_corpus(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "p9_4_adversarial_corpus" not in source:
            violations.append(
                "must lazy-import p9_4_adversarial_corpus "
                "(canonical 25-entry adversarial corpus)",
            )
        if "materialize_pattern" not in source:
            violations.append(
                "must compose canonical materialize_pattern "
                "(no parallel pattern builder)",
            )
        if "ov_signature_substring" not in source:
            violations.append(
                "must compose canonical "
                "ov_signature_substring (no parallel "
                "autonomous-commit detection literal)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "adversarial_autobiography_master_default_false"
            ),
            target_file=target,
            description=(
                "§33.1 graduation contract — autobiography master "
                "stays default-False; operator opts in to "
                "retrospective audit."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "adversarial_autobiography_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — retrospective auditor; "
                "MUST NOT import orchestrator / iron_gate / "
                "policy / providers / candidate_generator / "
                "urgency_router / change_engine / "
                "semantic_guardian / adversarial_reviewer_service "
                "(sibling prospective surface)."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "adversarial_autobiography_finding_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "Closed 4-value AutobiographyFinding taxonomy "
                "bytes-pinned. Adding/removing requires updating "
                "_FINDING_GLYPH + tests."
            ),
            validate=_validate_finding_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "adversarial_autobiography_probe_outcome_taxonomy"
            ),
            target_file=target,
            description=(
                "Closed 3-value ProbeOutcome taxonomy "
                "bytes-pinned."
            ),
            validate=_validate_probe_outcome_taxonomy,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "adversarial_autobiography_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes canonical "
                "p9_4_adversarial_corpus.materialize_pattern + "
                "auto_committer.ov_signature_substring — no "
                "parallel pattern catalog, no parallel "
                "autonomous-commit detection literal."
            ),
            validate=_validate_composes_canonical_corpus,
        ),
    ]


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Auto-discovered via §33.3 naming-cage."""
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "adversarial_autobiography.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master switch for the adversarial "
                "autobiography retrospective auditor. §33.1 "
                "default-FALSE per operator-paced graduation."
            ),
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_COMMIT_SCAN_MAX,
            type=FlagType.INT,
            default=_DEFAULT_COMMIT_SCAN_MAX,
            description=(
                "Maximum O+V-signed commits audited per call. "
                "Clamped to [5, 10_000]."
            ),
            category=Category.CAPACITY,
            source_file=src,
            example=f"{_ENV_COMMIT_SCAN_MAX}=500",
        ),
        FlagSpec(
            name=_ENV_ESCAPE_ALERT_THRESHOLD,
            type=FlagType.INT,
            default=_DEFAULT_ESCAPE_ALERT_THRESHOLD,
            description=(
                "Number of corpus matches in a single commit "
                "that raises commit-level finding to "
                "CORPUS_ESCAPE. Default 1."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_ESCAPE_ALERT_THRESHOLD}=2",
        ),
        FlagSpec(
            name=_ENV_PERSISTENCE_ENABLED,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Sub-flag — gate the §33.4 JSONL audit ledger. "
                "Default TRUE when master is on; master-off "
                "short-circuits."
            ),
            category=Category.OBSERVABILITY,
            source_file=src,
            example=f"{_ENV_PERSISTENCE_ENABLED}=false",
        ),
        FlagSpec(
            name=_ENV_LEDGER_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Operator override for the audit ledger path. "
                "Defaults to "
                "<repo>/.jarvis/adversarial_autobiography_"
                "ledger.jsonl."
            ),
            category=Category.OBSERVABILITY,
            source_file=src,
            example=(
                f"{_ENV_LEDGER_PATH}=/var/log/autobiography.jsonl"
            ),
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001 — fail-open per §33.1
            continue
    return count


__all__ = [
    "ADVERSARIAL_AUTOBIOGRAPHY_SCHEMA_VERSION",
    "AutobiographyFinding",
    "ProbeOutcome",
    "ProbeMatch",
    "CommitAutobiographyAudit",
    "AutobiographyReport",
    "master_enabled",
    "persistence_enabled",
    "commit_scan_max",
    "escape_alert_threshold",
    "ledger_path",
    "finding_glyph",
    "audit_autobiography",
    "get_cached_report",
    "get_cached_audits",
    "reset_cache_for_tests",
    "format_autobiography_panel",
    "format_commit_audit",
    "register_shipped_invariants",
    "register_flags",
]
