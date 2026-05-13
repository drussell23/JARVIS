"""SWE-Bench-Pro report card renderer - Phase F (PRD section 40.7.10-f).

Pure-data aggregation + rendering layer above Phase D's
EvaluationResultStore. Phase F is the final milestone of the
SWE-Bench-Pro arc - operators consume aggregate cards (per-repo
pass-rates, per-difficulty-tier breakdowns, ScoreOutcome
distributions, top-N failure clusters) to triage benchmark runs.

Architectural contract
----------------------

  * **Composes canonical surfaces only**:
      - EvaluationResultStore.query / aggregate_score_outcomes /
        aggregate_evaluation_outcomes / pass_rate (Phase D)
      - ScoreOutcome / EvaluationOutcome enums
      - ProblemSpec (Phase A; optional - operator passes when
        per-difficulty + per-repo authoritative attribution is needed)

  * **No master flag** - Phase F is read-only over Phase D's store.
    An empty store renders an empty card cleanly. The Phase A
    JARVIS_SWE_BENCH_PRO_ENABLED master gates the whole arc; Phase F
    has no enablement of its own to add.

  * **Closed dataclass hierarchy**:
      ReportCard (top-level)
        |- score_distribution / eval_distribution / overall_pass_rate
        |- per_repo: Tuple[RepoStats, ...]
        |- per_difficulty: Tuple[DifficultyStats, ...]
        |- top_failures: Tuple[FailureCluster, ...]
        |- total_records / rendered_at_iso / schema_version

  * **Pure-function renderers**:
      render_markdown(card) -> str  (human-friendly)
      render_json(card) -> str      (machine-readable; lossless)
    Both are pure data transformations; no I/O.

  * **Optional async write helper**:
      async write_report_card(card, output_path, format='markdown')
      Composes canonical Path I/O via run_in_executor so the event
      loop is never blocked on disk write. NEVER raises.

  * **Repo derivation**: prefers problem.repo when ``problems``
    mapping is provided; falls back to instance_id prefix parsing
    (SWE-Bench convention ``{org}__{repo}-{N}`` - split on ``__``
    + strip trailing ``-N``).

Section 7 fail-closed contract
------------------------------

Every public surface NEVER raises (asyncio.CancelledError is the
sole exception that propagates from write_report_card per
orchestrator convention). build_report_card produces an empty
ReportCard on store query failure rather than crashing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    Dict,
    List,
    Mapping,
    Optional,
    Tuple,
)

from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import (
    ProblemSpec,
)
from backend.core.ouroboros.governance.swe_bench_pro.evaluator import (
    EvaluationOutcome,
)
from backend.core.ouroboros.governance.swe_bench_pro.result_store import (
    EvaluationRecord,
    EvaluationResultStore,
)
from backend.core.ouroboros.governance.swe_bench_pro.scorer import (
    ScoreOutcome,
)


logger = logging.getLogger("Ouroboros.SWEBenchPro.ReportCard")


# ===========================================================================
# Schema vocabulary
# ===========================================================================


REPORT_CARD_SCHEMA_VERSION: str = "swe_bench_pro_report_card.v1"


# Default top-N failure clusters to surface in cards. Operators
# typically want the top 5-10 diagnostics for triage; default 10
# is a round number that fits on a single terminal page.
_DEFAULT_TOP_N_FAILURES: int = 10


# Default difficulty bucket when problem.difficulty is empty/missing.
_UNKNOWN_DIFFICULTY: str = "unknown"


# Repo derivation from instance_id - SWE-Bench convention is
# ``{org}__{repo}-{N}`` (e.g., "octocat__hello-001"). Pattern matches
# the trailing -N suffix so we can strip it cleanly. NEVER raises
# (re.search returns None on no match).
_INSTANCE_ID_TRAILING_INT_RE = re.compile(r"-\d+$")


# ===========================================================================
# Frozen dataclasses (closed hierarchy; section 33.5 symmetric serialization)
# ===========================================================================


@dataclass(frozen=True)
class RepoStats:
    """Per-repo aggregate. ``repo`` is the canonical repo identifier
    (e.g., "octocat/hello"); ``pass_rate`` is PASS / (PASS+FAIL+
    PARTIAL+SCORING_ERROR) - SKIPPED excluded from denominator.
    """

    repo: str
    total: int
    pass_count: int
    fail_count: int
    partial_count: int
    error_count: int
    skipped_count: int
    pass_rate: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "repo": self.repo,
            "total": self.total,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "partial_count": self.partial_count,
            "error_count": self.error_count,
            "skipped_count": self.skipped_count,
            "pass_rate": self.pass_rate,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RepoStats":
        return cls(
            repo=str(payload["repo"]),
            total=int(payload.get("total", 0)),
            pass_count=int(payload.get("pass_count", 0)),
            fail_count=int(payload.get("fail_count", 0)),
            partial_count=int(payload.get("partial_count", 0)),
            error_count=int(payload.get("error_count", 0)),
            skipped_count=int(payload.get("skipped_count", 0)),
            pass_rate=float(payload.get("pass_rate", 0.0)),
        )


@dataclass(frozen=True)
class DifficultyStats:
    """Per-difficulty-tier aggregate. Same shape as RepoStats but
    keyed on ProblemSpec.difficulty. Only present when the
    ``problems`` mapping is supplied to build_report_card."""

    difficulty: str
    total: int
    pass_count: int
    fail_count: int
    partial_count: int
    error_count: int
    skipped_count: int
    pass_rate: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "difficulty": self.difficulty,
            "total": self.total,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "partial_count": self.partial_count,
            "error_count": self.error_count,
            "skipped_count": self.skipped_count,
            "pass_rate": self.pass_rate,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "DifficultyStats":
        return cls(
            difficulty=str(payload["difficulty"]),
            total=int(payload.get("total", 0)),
            pass_count=int(payload.get("pass_count", 0)),
            fail_count=int(payload.get("fail_count", 0)),
            partial_count=int(payload.get("partial_count", 0)),
            error_count=int(payload.get("error_count", 0)),
            skipped_count=int(payload.get("skipped_count", 0)),
            pass_rate=float(payload.get("pass_rate", 0.0)),
        )


@dataclass(frozen=True)
class FailureCluster:
    """One cluster in the top-N failures section. ``diagnostic_prefix``
    is the substring of ``ScoringResult.diagnostic`` before the first
    colon (e.g., "apply_failed", "patch_modified_tests"). Empty
    diagnostics are bucketed as "(empty)". ``example_instance_ids``
    holds the first 5 instance_ids in this cluster."""

    diagnostic_prefix: str
    count: int
    example_instance_ids: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "diagnostic_prefix": self.diagnostic_prefix,
            "count": self.count,
            "example_instance_ids": list(self.example_instance_ids),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FailureCluster":
        return cls(
            diagnostic_prefix=str(payload["diagnostic_prefix"]),
            count=int(payload.get("count", 0)),
            example_instance_ids=tuple(
                str(x) for x in payload.get("example_instance_ids", ())
            ),
        )


@dataclass(frozen=True)
class ReportCard:
    """Top-level aggregate card. Composes Phase D store
    aggregations + per-repo / per-difficulty / top-failures
    summaries into a single immutable payload renderable as
    Markdown or JSON."""

    total_records: int
    score_distribution: Dict[str, int]
    eval_distribution: Dict[str, int]
    overall_pass_rate: float
    per_repo: Tuple[RepoStats, ...]
    per_difficulty: Tuple[DifficultyStats, ...]
    top_failures: Tuple[FailureCluster, ...]
    rendered_at_iso: str
    schema_version: str = REPORT_CARD_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "rendered_at_iso": self.rendered_at_iso,
            "total_records": self.total_records,
            "overall_pass_rate": self.overall_pass_rate,
            "score_distribution": dict(self.score_distribution),
            "eval_distribution": dict(self.eval_distribution),
            "per_repo": [r.to_dict() for r in self.per_repo],
            "per_difficulty": [d.to_dict() for d in self.per_difficulty],
            "top_failures": [f.to_dict() for f in self.top_failures],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReportCard":
        return cls(
            schema_version=str(payload.get(
                "schema_version", REPORT_CARD_SCHEMA_VERSION,
            )),
            rendered_at_iso=str(payload.get("rendered_at_iso", "")),
            total_records=int(payload.get("total_records", 0)),
            overall_pass_rate=float(payload.get("overall_pass_rate", 0.0)),
            score_distribution=dict(payload.get("score_distribution", {})),
            eval_distribution=dict(payload.get("eval_distribution", {})),
            per_repo=tuple(
                RepoStats.from_dict(r)
                for r in payload.get("per_repo", ())
            ),
            per_difficulty=tuple(
                DifficultyStats.from_dict(d)
                for d in payload.get("per_difficulty", ())
            ),
            top_failures=tuple(
                FailureCluster.from_dict(f)
                for f in payload.get("top_failures", ())
            ),
        )


# ===========================================================================
# Pure helpers (NEVER raise)
# ===========================================================================


def _derive_repo(
    record: EvaluationRecord,
    problems: Optional[Mapping[str, ProblemSpec]],
) -> str:
    """Repo identifier for ``record``. Prefers ``problems`` mapping
    when provided; falls back to instance_id prefix parsing
    (SWE-Bench convention org__repo-N -> {org}/{repo}). NEVER raises."""
    instance_id = record.evaluation.problem_instance_id or ""
    if problems is not None:
        problem = problems.get(instance_id)
        if problem is not None:
            repo = getattr(problem, "repo", "") or ""
            if repo:
                return repo
    # Fallback: parse "{org}__{repo}-{N}" -> "{org}/{repo}".
    stripped = _INSTANCE_ID_TRAILING_INT_RE.sub("", instance_id)
    if "__" in stripped:
        parts = stripped.split("__", 1)
        return f"{parts[0]}/{parts[1]}"
    return stripped or "(unknown)"


def _derive_difficulty(
    record: EvaluationRecord,
    problems: Optional[Mapping[str, ProblemSpec]],
) -> str:
    """Difficulty tier for ``record``. Returns _UNKNOWN_DIFFICULTY
    when problems mapping is missing or problem.difficulty is empty.
    NEVER raises."""
    instance_id = record.evaluation.problem_instance_id or ""
    if problems is None:
        return _UNKNOWN_DIFFICULTY
    problem = problems.get(instance_id)
    if problem is None:
        return _UNKNOWN_DIFFICULTY
    difficulty = getattr(problem, "difficulty", "") or ""
    return difficulty or _UNKNOWN_DIFFICULTY


def _diagnostic_prefix(diagnostic: str) -> str:
    """Cluster key for a failure diagnostic. Splits on first colon;
    empty diagnostic -> "(empty)". Pure function."""
    if not diagnostic:
        return "(empty)"
    if ":" in diagnostic:
        return diagnostic.split(":", 1)[0]
    return diagnostic


def _compute_pass_rate(
    pass_count: int, fail_count: int, partial_count: int,
    error_count: int,
) -> float:
    """Denominator excludes SKIPPED records (consistent with Phase D
    EvaluationResultStore.pass_rate). Pure function."""
    scored = pass_count + fail_count + partial_count + error_count
    if scored == 0:
        return 0.0
    return round(pass_count / scored, 4)


def _empty_counter() -> Dict[str, int]:
    return {o.value: 0 for o in ScoreOutcome}


# ===========================================================================
# Public API - build_report_card
# ===========================================================================


def build_report_card(
    store: EvaluationResultStore,
    *,
    problems: Optional[Mapping[str, ProblemSpec]] = None,
    top_n_failures: int = _DEFAULT_TOP_N_FAILURES,
) -> ReportCard:
    """Build a ReportCard from a Phase D EvaluationResultStore.

    Parameters
    ----------
    store:
        Phase D EvaluationResultStore (in-memory snapshot is the
        source of truth). Phase F never reads the JSONL directly;
        operators wanting offline render call
        :func:`replay_from_disk` first then pass the populated
        store here.
    problems:
        Optional mapping ``instance_id -> ProblemSpec``. When
        provided, per-repo aggregation uses ``problem.repo``
        directly (authoritative) and per-difficulty aggregation
        becomes available. When ``None``, repo is derived from
        instance_id by SWE-Bench convention; per-difficulty is
        bucketed entirely under ``_UNKNOWN_DIFFICULTY``.
    top_n_failures:
        Cap on the number of failure clusters surfaced in the
        ``top_failures`` field. Default 10.

    Returns
    -------
    ReportCard
        Always populated; empty store yields an empty card cleanly.
        NEVER raises.
    """
    try:
        records = store.query()
    except Exception:  # noqa: BLE001 - defensive over Phase D contract
        logger.debug(
            "[SWEBenchPro.ReportCard] store.query raised; "
            "rendering empty card", exc_info=True,
        )
        records = ()

    try:
        score_distribution = store.aggregate_score_outcomes()
    except Exception:  # noqa: BLE001
        score_distribution = _empty_counter()
    try:
        eval_distribution = store.aggregate_evaluation_outcomes()
    except Exception:  # noqa: BLE001
        eval_distribution = {o.value: 0 for o in EvaluationOutcome}
    try:
        overall_pass_rate = store.pass_rate()
    except Exception:  # noqa: BLE001
        overall_pass_rate = 0.0

    # Per-repo aggregation. Counters keyed by repo identifier.
    repo_counters: Dict[str, Dict[str, int]] = {}
    diff_counters: Dict[str, Dict[str, int]] = {}
    failure_buckets: Dict[str, List[str]] = {}

    for record in records:
        repo = _derive_repo(record, problems)
        difficulty = _derive_difficulty(record, problems)
        outcome = record.scoring.outcome
        key = outcome.value

        for counter_map, bucket_key in (
            (repo_counters, repo),
            (diff_counters, difficulty),
        ):
            bucket = counter_map.setdefault(bucket_key, _empty_counter())
            bucket[key] = bucket.get(key, 0) + 1

        if outcome in (
            ScoreOutcome.FAIL, ScoreOutcome.SCORING_ERROR,
        ):
            prefix = _diagnostic_prefix(record.scoring.diagnostic)
            instances = failure_buckets.setdefault(prefix, [])
            instances.append(record.evaluation.problem_instance_id)

    per_repo: List[RepoStats] = []
    for repo, counter in repo_counters.items():
        passed = counter.get(ScoreOutcome.PASS.value, 0)
        failed = counter.get(ScoreOutcome.FAIL.value, 0)
        partial = counter.get(ScoreOutcome.PARTIAL.value, 0)
        error = counter.get(ScoreOutcome.SCORING_ERROR.value, 0)
        skipped = counter.get(ScoreOutcome.SKIPPED.value, 0)
        total = passed + failed + partial + error + skipped
        per_repo.append(RepoStats(
            repo=repo,
            total=total,
            pass_count=passed,
            fail_count=failed,
            partial_count=partial,
            error_count=error,
            skipped_count=skipped,
            pass_rate=_compute_pass_rate(passed, failed, partial, error),
        ))
    per_repo.sort(key=lambda r: (-r.pass_rate, -r.total, r.repo))

    # Per-difficulty aggregation. Always present in the dataclass
    # (empty when problems mapping is missing - tier collapses to
    # the single "_UNKNOWN_DIFFICULTY" bucket; operators see it as
    # the only row, which signals the problems mapping was omitted).
    per_difficulty: List[DifficultyStats] = []
    for difficulty, counter in diff_counters.items():
        passed = counter.get(ScoreOutcome.PASS.value, 0)
        failed = counter.get(ScoreOutcome.FAIL.value, 0)
        partial = counter.get(ScoreOutcome.PARTIAL.value, 0)
        error = counter.get(ScoreOutcome.SCORING_ERROR.value, 0)
        skipped = counter.get(ScoreOutcome.SKIPPED.value, 0)
        total = passed + failed + partial + error + skipped
        per_difficulty.append(DifficultyStats(
            difficulty=difficulty,
            total=total,
            pass_count=passed,
            fail_count=failed,
            partial_count=partial,
            error_count=error,
            skipped_count=skipped,
            pass_rate=_compute_pass_rate(passed, failed, partial, error),
        ))
    per_difficulty.sort(
        key=lambda d: (-d.pass_rate, -d.total, d.difficulty),
    )

    # Top-N failure clustering.
    clusters: List[FailureCluster] = []
    for prefix, instances in failure_buckets.items():
        clusters.append(FailureCluster(
            diagnostic_prefix=prefix,
            count=len(instances),
            example_instance_ids=tuple(instances[:5]),
        ))
    clusters.sort(key=lambda c: (-c.count, c.diagnostic_prefix))
    top_failures = tuple(clusters[: max(0, int(top_n_failures))])

    return ReportCard(
        total_records=len(records),
        score_distribution=dict(score_distribution),
        eval_distribution=dict(eval_distribution),
        overall_pass_rate=overall_pass_rate,
        per_repo=tuple(per_repo),
        per_difficulty=tuple(per_difficulty),
        top_failures=top_failures,
        rendered_at_iso=datetime.now(tz=timezone.utc).isoformat(),
    )


# ===========================================================================
# Pure renderers (NEVER raise)
# ===========================================================================


def _format_pass_rate(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def render_markdown(card: ReportCard) -> str:
    """Render the report card as human-friendly Markdown. Pure
    function; NEVER raises."""
    lines: List[str] = []
    lines.append("# SWE-Bench-Pro Report Card")
    lines.append("")
    lines.append(f"_Rendered at_: `{card.rendered_at_iso}`  ")
    lines.append(f"_Schema_: `{card.schema_version}`")
    lines.append("")

    lines.append("## Overall")
    lines.append("")
    lines.append(f"- **Total records**: {card.total_records}")
    lines.append(
        f"- **Overall pass rate**: "
        f"{_format_pass_rate(card.overall_pass_rate)} "
        f"(SKIPPED excluded from denominator)"
    )
    lines.append("")

    lines.append("## Score distribution")
    lines.append("")
    lines.append("| Outcome | Count |")
    lines.append("|---|---:|")
    for key in ("pass", "partial", "fail", "scoring_error", "skipped"):
        count = card.score_distribution.get(key, 0)
        lines.append(f"| {key} | {count} |")
    lines.append("")

    lines.append("## Evaluation distribution")
    lines.append("")
    lines.append("| Outcome | Count |")
    lines.append("|---|---:|")
    for key in (
        "resolved", "unresolved", "prepare_failed", "ingest_failed",
        "terminal_timeout", "cancelled", "master_flag_off",
    ):
        count = card.eval_distribution.get(key, 0)
        lines.append(f"| {key} | {count} |")
    lines.append("")

    if card.per_repo:
        lines.append("## Per-repo pass rate")
        lines.append("")
        lines.append(
            "| Repo | Pass rate | Pass | Fail | Partial | Error | "
            "Skipped | Total |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for r in card.per_repo:
            lines.append(
                f"| `{r.repo}` | "
                f"{_format_pass_rate(r.pass_rate)} | "
                f"{r.pass_count} | {r.fail_count} | {r.partial_count} | "
                f"{r.error_count} | {r.skipped_count} | {r.total} |"
            )
        lines.append("")

    if card.per_difficulty:
        lines.append("## Per-difficulty tier")
        lines.append("")
        lines.append(
            "| Difficulty | Pass rate | Pass | Fail | Partial | "
            "Error | Skipped | Total |"
        )
        lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
        for d in card.per_difficulty:
            lines.append(
                f"| `{d.difficulty}` | "
                f"{_format_pass_rate(d.pass_rate)} | "
                f"{d.pass_count} | {d.fail_count} | {d.partial_count} | "
                f"{d.error_count} | {d.skipped_count} | {d.total} |"
            )
        lines.append("")

    if card.top_failures:
        lines.append("## Top failure clusters")
        lines.append("")
        lines.append("| Diagnostic prefix | Count | Examples |")
        lines.append("|---|---:|---|")
        for f in card.top_failures:
            examples = ", ".join(
                f"`{x}`" for x in f.example_instance_ids[:3]
            )
            lines.append(
                f"| `{f.diagnostic_prefix}` | {f.count} | {examples} |"
            )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_json(card: ReportCard) -> str:
    """Render the report card as lossless JSON. Pure function;
    NEVER raises."""
    try:
        return json.dumps(card.to_dict(), sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - defensive over to_dict
        logger.debug(
            "[SWEBenchPro.ReportCard] render_json fallback", exc_info=True,
        )
        return "{}"


# ===========================================================================
# Optional async write helper
# ===========================================================================


def _write_sync(path: Path, content: str) -> bool:
    """Synchronous parent-mkdir + write. NEVER raises."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return False


async def write_report_card(
    card: ReportCard,
    output_path: Path,
    *,
    format: str = "markdown",
) -> bool:
    """Render ``card`` and write to ``output_path``. Returns True
    on success, False on any I/O failure. Disk I/O runs on the
    default thread executor so the event loop is never blocked.

    Format options:
        ``markdown`` (default) - human-friendly Markdown
        ``json``               - lossless JSON

    NEVER raises (asyncio.CancelledError propagates).
    """
    try:
        if format == "json":
            content = render_json(card)
        else:
            content = render_markdown(card)
        return await asyncio.get_running_loop().run_in_executor(
            None, _write_sync, Path(output_path), content,
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.debug(
            "[SWEBenchPro.ReportCard] write_report_card raised",
            exc_info=True,
        )
        return False


__all__ = [
    "REPORT_CARD_SCHEMA_VERSION",
    "DifficultyStats",
    "FailureCluster",
    "ReportCard",
    "RepoStats",
    "build_report_card",
    "render_json",
    "render_markdown",
    "write_report_card",
]
