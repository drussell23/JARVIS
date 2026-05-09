"""§39 Tier-4 #10 — Operator's-eye session story
(PRD v2.73 to v2.74, 2026-05-09).

Journal-style end-of-session narrative composed from
canonical :class:`LastSessionSummary` data. ZERO parallel
session aggregator — every datum read is a field on the
existing :class:`SessionRecord`.

Authority asymmetry: ZERO authority. Read-only renderer.

§38.11.5a.5 single-canonical-name discipline honored —
reuses canonical SessionRecord shape; the only NEW closed
taxonomy is :class:`StoryArc` (4 values mapping
SessionRecord stats to narrative beats).

§33 patterns invoked:
- §33.1 graduation contract (master default-FALSE)
- §33.5 versioned artifact (frozen :class:`StoryBeat` +
  :class:`SessionStory`)
"""
from __future__ import annotations

import enum
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


SESSION_STORY_SCHEMA_VERSION: str = "session_story.1"


_ENV_MASTER = "JARVIS_SESSION_STORY_ENABLED"
_ENV_MAX_SESSIONS = "JARVIS_SESSION_STORY_MAX_SESSIONS"

_DEFAULT_MAX_SESSIONS = 1
_MIN_MAX_SESSIONS = 1
_MAX_MAX_SESSIONS = 10


_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 graduation contract — master default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def _read_max_sessions() -> int:
    raw = os.environ.get(_ENV_MAX_SESSIONS, "").strip()
    if not raw:
        return _DEFAULT_MAX_SESSIONS
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SESSIONS
    return max(_MIN_MAX_SESSIONS, min(_MAX_MAX_SESSIONS, n))


# ===========================================================================
# Closed taxonomy — 4-value StoryArc
# ===========================================================================


class StoryArc(str, enum.Enum):
    """Closed 4-value vocabulary for narrative beats.

    Each beat maps a slice of SessionRecord stats to a
    journal-style sentence. AST-pinned so adding a beat
    requires the renderer's beat-table to extend in
    lockstep.
    """

    DOMINANT_ACTIVITY = "dominant_activity"   # what dominated the session
    KEY_FINDING = "key_finding"               # successful apply / verify wins
    SETBACK = "setback"                       # failed / cancelled work
    GROWTH = "growth"                         # convergence / drift status

    @classmethod
    def coerce(cls, raw: object) -> "StoryArc":
        if isinstance(raw, cls):
            return raw
        if isinstance(raw, str):
            s = raw.strip().lower()
            for m in cls:
                if m.value == s:
                    return m
        return cls.DOMINANT_ACTIVITY


# ===========================================================================
# Frozen §33.5 versioned artifacts
# ===========================================================================


@dataclass(frozen=True)
class StoryBeat:
    """One narrative beat. Frozen + hashable."""

    arc: StoryArc
    sentence: str
    weight: float = 0.0     # narrative emphasis 0..1 (stat-derived)
    schema_version: str = SESSION_STORY_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "arc": self.arc.value,
            "sentence": self.sentence,
            "weight": float(self.weight),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class SessionStory:
    """One operator's-eye session narrative."""

    session_id: str
    duration_human: str
    cost_human: str
    stop_reason: str
    beats: Tuple[StoryBeat, ...] = field(default_factory=tuple)
    aggregated_at_unix: float = 0.0
    schema_version: str = SESSION_STORY_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "duration_human": self.duration_human,
            "cost_human": self.cost_human,
            "stop_reason": self.stop_reason,
            "beats": [b.to_dict() for b in self.beats],
            "aggregated_at_unix": self.aggregated_at_unix,
            "schema_version": self.schema_version,
        }

    def beat_for_arc(
        self, arc: StoryArc,
    ) -> Optional[StoryBeat]:
        for b in self.beats:
            if b.arc is arc:
                return b
        return None


# ===========================================================================
# Aggregator — composes canonical LastSessionSummary
# ===========================================================================


def _format_duration_human(seconds: float) -> str:
    if seconds is None or seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem_min = minutes % 60
    if rem_min == 0:
        return f"{hours}h"
    return f"{hours}h {rem_min}m"


def _format_cost_human(cost: float) -> str:
    if cost <= 0:
        return "free"
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def _arc_weight(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return max(0.0, min(1.0, numerator / denominator))


def aggregate_session_story(
    *,
    n_sessions: Optional[int] = None,
) -> List[SessionStory]:
    """Compose canonical LastSessionSummary into journal
    narratives. NEVER raises.

    Returns empty list when:
      * master flag off
      * no parseable session records
      * canonical substrate unavailable
    """
    if not master_enabled():
        return []
    n = (
        max(
            _MIN_MAX_SESSIONS,
            min(_MAX_MAX_SESSIONS, int(n_sessions)),
        )
        if n_sessions is not None
        else _read_max_sessions()
    )

    try:
        from backend.core.ouroboros.governance.last_session_summary import (  # noqa: E501
            get_default_summary,
        )
        summary = get_default_summary()
    except Exception:  # noqa: BLE001
        logger.debug(
            "session_story: LSS unavailable", exc_info=True,
        )
        return []

    try:
        records = summary.load(n_sessions=n)
    except Exception:  # noqa: BLE001
        logger.debug(
            "session_story: load failed", exc_info=True,
        )
        return []

    stories: List[SessionStory] = []
    now_unix = time.time()
    for rec in records:
        try:
            beats = _build_beats(rec)
            story = SessionStory(
                session_id=rec.session_id,
                duration_human=_format_duration_human(
                    rec.duration_s,
                ),
                cost_human=_format_cost_human(rec.cost_total),
                stop_reason=rec.stop_reason,
                beats=tuple(beats),
                aggregated_at_unix=now_unix,
            )
            stories.append(story)
            _publish_story_event(story)
        except Exception:  # noqa: BLE001
            continue
    return stories


def _build_beats(rec) -> List[StoryBeat]:  # noqa: ANN001
    """Pure-function narrative beat construction. NEVER
    raises."""
    beats: List[StoryBeat] = []
    attempted = max(0, int(rec.stats_attempted or 0))
    completed = max(0, int(rec.stats_completed or 0))
    failed = max(0, int(rec.stats_failed or 0))
    cancelled = max(0, int(rec.stats_cancelled or 0))

    # DOMINANT_ACTIVITY — what kind of session was it?
    if attempted == 0:
        dom_sentence = (
            "The session stayed mostly idle — "
            "no ops attempted."
        )
        dom_weight = 0.0
    else:
        success_pct = int(
            round(_arc_weight(completed, attempted) * 100),
        )
        dom_sentence = (
            f"You ran {attempted} op"
            f"{'s' if attempted != 1 else ''} "
            f"over {_format_duration_human(rec.duration_s)} "
            f"— {completed} completed "
            f"({success_pct}% success)."
        )
        dom_weight = _arc_weight(completed, attempted)
    beats.append(StoryBeat(
        arc=StoryArc.DOMINANT_ACTIVITY,
        sentence=dom_sentence,
        weight=dom_weight,
    ))

    # KEY_FINDING — last apply / verify wins
    apply_mode = (rec.last_apply_mode or "").lower()
    apply_files = rec.last_apply_files
    apply_op_short = (rec.last_apply_op_id or "")[:12]
    verify_passed = rec.last_verify_tests_passed
    verify_total = rec.last_verify_tests_total
    if apply_mode in ("single", "multi") and (
        apply_files is not None and apply_files > 0
    ):
        finding_parts = [
            f"Applied {apply_files} "
            f"file{'s' if apply_files != 1 else ''} "
            f"in op {apply_op_short}"
        ]
        if (
            verify_passed is not None
            and verify_total is not None
            and verify_total > 0
        ):
            finding_parts.append(
                f"({verify_passed}/{verify_total} tests passed)"
            )
        if rec.last_commit_hash:
            finding_parts.append(
                f"committed as {rec.last_commit_hash[:8]}"
            )
        finding_sentence = " — ".join(finding_parts) + "."
        finding_weight = (
            _arc_weight(verify_passed or 0, verify_total or 0)
            if verify_total
            else 0.5
        )
        beats.append(StoryBeat(
            arc=StoryArc.KEY_FINDING,
            sentence=finding_sentence,
            weight=finding_weight,
        ))

    # SETBACK — failures + cancellations
    setback_count = failed + cancelled
    if setback_count > 0:
        parts = []
        if failed:
            parts.append(
                f"{failed} op{'s' if failed != 1 else ''} failed"
            )
        if cancelled:
            parts.append(
                f"{cancelled} cancelled"
            )
        setback_sentence = (
            "Setbacks: " + ", ".join(parts) + "."
        )
        setback_weight = (
            _arc_weight(setback_count, attempted)
            if attempted else 1.0
        )
        beats.append(StoryBeat(
            arc=StoryArc.SETBACK,
            sentence=setback_sentence,
            weight=setback_weight,
        ))

    # GROWTH — convergence + drift
    convergence = (rec.convergence_state or "").strip()
    drift_status = (rec.drift_status or "").strip()
    drift_ratio = rec.drift_ratio
    growth_parts = []
    if convergence:
        growth_parts.append(
            f"convergence: {convergence}"
        )
    if drift_status:
        if drift_ratio is not None:
            growth_parts.append(
                f"drift: {drift_status} "
                f"({drift_ratio:.2f})"
            )
        else:
            growth_parts.append(
                f"drift: {drift_status}"
            )
    if growth_parts:
        growth_sentence = (
            "Strategic posture: " + " · ".join(growth_parts)
            + "."
        )
        beats.append(StoryBeat(
            arc=StoryArc.GROWTH,
            sentence=growth_sentence,
            weight=0.5,
        ))

    return beats


# ===========================================================================
# SSE composition
# ===========================================================================


def _publish_story_event(story: SessionStory) -> None:
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_SESSION_STORY_RENDERED,
            get_default_broker,
        )
        broker = get_default_broker()
        if broker is None:
            return
        broker.publish(
            EVENT_TYPE_SESSION_STORY_RENDERED,
            story.session_id,
            story.to_dict(),
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "session_story: SSE publish failed",
            exc_info=True,
        )


# ===========================================================================
# Renderer
# ===========================================================================


_ARC_GLYPHS = {
    StoryArc.DOMINANT_ACTIVITY: "📖",
    StoryArc.KEY_FINDING: "✨",
    StoryArc.SETBACK: "⚠",
    StoryArc.GROWTH: "🌱",
}


def format_session_story(
    story: Optional[SessionStory],
) -> str:
    """Render single session story. Empty when master off
    OR story is None."""
    if not master_enabled():
        return ""
    if story is None or not story.beats:
        return ""
    sid_short = (
        story.session_id[-12:]
        if len(story.session_id) > 12
        else story.session_id
    )
    parts = [
        f"[bright_yellow]📖 Session story "
        f"({sid_short}):[/]",
        f"  [dim]{story.duration_human} · "
        f"{story.cost_human} · "
        f"stopped: {story.stop_reason}[/]",
        "",
    ]
    for beat in story.beats:
        glyph = _ARC_GLYPHS.get(beat.arc, "•")
        parts.append(f"  {glyph} {beat.sentence}")
    return "\n".join(parts).rstrip()


def format_session_stories(
    stories: List[SessionStory],
) -> str:
    """Render multi-session story collection."""
    if not master_enabled() or not stories:
        return ""
    sections = []
    for s in stories:
        section = format_session_story(s)
        if section:
            sections.append(section)
    return "\n\n".join(sections)


# ===========================================================================
# FlagRegistry seeds
# ===========================================================================


def register_flags(registry: Any) -> int:  # noqa: ANN001
    if registry is None:
        return 0
    n = 0
    specs = (
        (
            _ENV_MASTER, "bool",
            "§39 Tier-4 #10 session story master switch "
            "(graduation contract per §33.1; default FALSE).",
            "false",
        ),
        (
            _ENV_MAX_SESSIONS, "int",
            "Max prior sessions to render in story view "
            "(default 1; clamped 1..10).",
            "1",
        ),
    )
    for name, typ, desc, ex in specs:
        try:
            registry.register(
                name=name,
                type=typ,
                category="ux",
                description=desc,
                example=ex,
                source_file=(
                    "backend/core/ouroboros/governance/"
                    "session_story.py"
                ),
            )
            n += 1
        except Exception:  # noqa: BLE001
            pass
    return n


# ===========================================================================
# AST pins
# ===========================================================================


def register_shipped_invariants() -> list:
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        ShippedCodeInvariant,
    )
    import ast

    pins = []

    def _master_default_false(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                ok = False
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
                                ok = True
                if not ok:
                    return [
                        "master_enabled() must call _flag(...) "
                        "with default=False"
                    ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier4_10_master_default_false"
        ),
        description=(
            "§33.1 graduation contract — story master "
            "stays default-False until evidence ladder "
            "closes."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "session_story.py"
        ),
        validate=_master_default_false,
    ))

    def _authority_asymmetry(tree: ast.AST, src: str):
        bad = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.candidate_generator",
        )
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod.startswith(b) for b in bad):
                    violations.append(
                        f"forbidden authority import: {mod}"
                    )
        return violations

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier4_10_authority_asymmetry"
        ),
        description=(
            "Substrate purity — read-only renderer; no "
            "orchestrator/risk-tier authority."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "session_story.py"
        ),
        validate=_authority_asymmetry,
    ))

    def _arc_taxonomy(tree: ast.AST, src: str):
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "StoryArc"
            ):
                names = {
                    a.targets[0].id
                    for a in node.body
                    if isinstance(a, ast.Assign)
                    and isinstance(a.targets[0], ast.Name)
                }
                expected = {
                    "DOMINANT_ACTIVITY", "KEY_FINDING",
                    "SETBACK", "GROWTH",
                }
                missing = expected - names
                if missing:
                    return [
                        f"StoryArc missing values: "
                        f"{sorted(missing)}"
                    ]
                return []
        return ["StoryArc class not found"]

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier4_10_arc_taxonomy_4_values"
        ),
        description=(
            "Closed 4-value StoryArc taxonomy — adding a "
            "beat requires the renderer's _ARC_GLYPHS map "
            "+ _build_beats logic to extend in lockstep."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "session_story.py"
        ),
        validate=_arc_taxonomy,
    ))

    def _composes_lss(tree: ast.AST, src: str):
        if (
            "last_session_summary" not in src
            or "get_default_summary" not in src
        ):
            return [
                "must lazy-import last_session_summary + "
                "get_default_summary (canonical session "
                "data source — NO parallel summary parser)"
            ]
        return []

    pins.append(ShippedCodeInvariant(
        invariant_name=(
            "section_39_tier4_10_composes_canonical_"
            "last_session_summary"
        ),
        description=(
            "Story composes canonical LastSessionSummary "
            "for SessionRecord data — NO parallel summary "
            "parser."
        ),
        target_file=(
            "backend/core/ouroboros/governance/"
            "session_story.py"
        ),
        validate=_composes_lss,
    ))

    return pins


__all__ = [
    "SESSION_STORY_SCHEMA_VERSION",
    "StoryArc",
    "StoryBeat",
    "SessionStory",
    "master_enabled",
    "aggregate_session_story",
    "format_session_story",
    "format_session_stories",
    "register_flags",
    "register_shipped_invariants",
]
