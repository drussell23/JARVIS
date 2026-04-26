"""P4 Slice 3 — /metrics REPL dispatcher with sparkline rendering.

Per OUROBOROS_VENOM_PRD.md §9 Phase 4 P4 acceptance criteria:

  > ``/metrics 7d`` REPL shows trends.

Self-contained REPL surface for the convergence-metrics suite. Parses
operator input, queries the Slice 1 :class:`MetricsEngine` (latest
snapshot) + Slice 2 :class:`MetricsHistoryLedger` (window aggregates
+ history rows), and renders ASCII-strict output with **sparkline
trend visualization** so operators can see at a glance whether O+V
is improving, plateaued, oscillating, or degrading.

Mirrors the proven Slice-3 REPL pattern from P3 (inline approval) +
P2 (chat dispatcher):
  * Subcommands fire only when args match the expected shape; else
    fall through to a default dispatch / safe error so natural
    typos don't misroute.
  * ASCII-only via encode/decode round-trip pin.
  * Output capped at MAX_RENDERED_BYTES with explicit clipped footer.
  * Result dataclass with status enum so SerpentFlow can switch on
    the outcome.
  * NO SerpentFlow auto-wire — Slice 5 graduation lands the wiring +
    flag flip.

Subcommands:

  * ``/metrics current``     — latest snapshot summary (composite,
                               trend, all 5 net-new metrics).
  * ``/metrics 7d``          — 7-day window aggregate + sparkline.
  * ``/metrics 30d``         — 30-day window aggregate + sparkline.
  * ``/metrics composite``   — composite-score-only sparkline over
                               the full readable history.
  * ``/metrics trend``       — terse trend banner (state + slope).
  * ``/metrics why <id>``    — drill into one session's snapshot.
  * ``/metrics help``        — subcommand listing.

Bare ``/metrics`` (no subcommand) → ``current``.

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * Allowed: ``metrics_engine`` + ``metrics_history`` (own slice
    family). No subprocess / file I/O / env mutation / network.
  * Best-effort — every reader call is wrapped in ``try / except``;
    failures render as a polite error line, never raise.
  * ASCII-strict — render output passed through
    ``.encode("ascii", errors="replace")`` round-trip; pinned by tests.
  * Master flag default-false until Slice 5 graduation; module is
    importable + callable; gating happens at the SerpentFlow caller.
"""
from __future__ import annotations

import enum
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Optional, Sequence

from backend.core.ouroboros.governance.metrics_engine import (
    METRICS_SNAPSHOT_SCHEMA_VERSION,
    MetricsEngine,
    MetricsSnapshot,
    TrendDirection,
    get_default_engine,
)
from backend.core.ouroboros.governance.metrics_history import (
    DEFAULT_WINDOW_30D_DAYS,
    DEFAULT_WINDOW_7D_DAYS,
    AggregatedMetrics,
    MetricsHistoryLedger,
    get_default_ledger,
)

logger = logging.getLogger(__name__)


_TRUTHY = ("1", "true", "yes", "on")


# Cap on rendered output bytes per call so a runaway summary can't
# saturate the SerpentFlow pane.
MAX_RENDERED_BYTES: int = 16 * 1024  # 16 KiB

# Sparkline character ramp (low→high). ASCII-only — no Unicode
# block-element characters because §3 of P3 inline_approval pinned
# strict-ASCII for terminal compatibility.
SPARKLINE_CHARS: str = "_.-=*#"

# Sparkline default width if no specific cap requested. Keeps the
# pane render under ~80 columns even with a label prefix.
SPARKLINE_WIDTH: int = 60

# Maximum history rows pulled for the composite-only sparkline.
# Same ceiling as the ledger's MAX_LINES_READ to avoid asking for
# more than the reader will return.
COMPOSITE_HISTORY_MAX_ROWS: int = 8_192


def is_enabled() -> bool:
    """Master flag — ``JARVIS_METRICS_SUITE_ENABLED`` (default false
    until Slice 5 graduation).

    SerpentFlow is the gating caller — when off, the REPL doesn't
    even construct the dispatcher. This module's behaviour does not
    change based on the flag; the helper is exported for SerpentFlow's
    convenience + symmetry with the P3 / P2 patterns."""
    return os.environ.get(
        "JARVIS_METRICS_SUITE_ENABLED", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


class MetricsReplStatus(str, enum.Enum):
    OK = "OK"                            # subcommand succeeded
    EMPTY = "EMPTY"                      # blank input
    UNKNOWN_SUBCOMMAND = "UNKNOWN_SUBCOMMAND"
    UNKNOWN_SESSION = "UNKNOWN_SESSION"  # /metrics why <unknown-id>
    READ_ERROR = "READ_ERROR"            # ledger read raised


@dataclass(frozen=True)
class MetricsReplResult:
    """Bundle returned to the SerpentFlow caller.

    ``rendered_text`` is what the operator should see in the pane.
    ``aggregate`` populated for 7d/30d/composite calls so future
    consumers (Slice 4 IDE GET) can serve it without re-aggregating.
    ``snapshot`` populated for ``current`` + ``why`` so callers can
    detail-link the underlying record."""

    status: MetricsReplStatus
    rendered_text: str
    aggregate: Optional[AggregatedMetrics] = None
    snapshot_dict: Optional[dict] = None
    notes: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Renderer helpers
# ---------------------------------------------------------------------------


def render_sparkline(
    values: Sequence[float],
    *,
    width: int = SPARKLINE_WIDTH,
) -> str:
    """ASCII sparkline. Down-samples to ``width`` bins via stride; each
    bin's mean is mapped onto the SPARKLINE_CHARS ramp.

    Returns ``""`` for empty input. All-equal input renders as the
    middle character (operators read this as a flat plateau)."""
    if not values:
        return ""
    width = max(1, min(int(width), 200))
    # Down-sample by stride averaging.
    if len(values) <= width:
        binned = list(values)
    else:
        bin_size = len(values) / width
        binned = []
        for i in range(width):
            lo = int(i * bin_size)
            hi = int((i + 1) * bin_size)
            chunk = values[lo:hi] if hi > lo else [values[lo]]
            binned.append(sum(chunk) / len(chunk))
    lo, hi = min(binned), max(binned)
    if hi == lo:
        # All flat — render as middle char to communicate plateau.
        mid_char = SPARKLINE_CHARS[len(SPARKLINE_CHARS) // 2]
        return mid_char * len(binned)
    span = hi - lo
    levels = len(SPARKLINE_CHARS) - 1
    out_chars = []
    for v in binned:
        idx = int(round(((v - lo) / span) * levels))
        idx = max(0, min(levels, idx))
        out_chars.append(SPARKLINE_CHARS[idx])
    return "".join(out_chars)


def render_help() -> str:
    return _ascii_clip("\n".join([
        "[metrics] /metrics subcommands:",
        "  /metrics current      latest snapshot summary",
        "  /metrics 7d           7-day window aggregate + sparkline",
        "  /metrics 30d          30-day window aggregate + sparkline",
        "  /metrics composite    composite-score sparkline (full history)",
        "  /metrics trend        terse trend banner",
        "  /metrics why <id>     drill into one session's snapshot",
        "  /metrics help         this listing",
    ]))


def render_current(snapshot: MetricsSnapshot) -> str:
    lines = [
        f"[metrics] current snapshot session={snapshot.session_id}",
        f"  schema:    v{snapshot.schema_version}",
        f"  composite: mean={_fmt(snapshot.composite_score_session_mean)} "
        f"min={_fmt(snapshot.composite_score_session_min)} "
        f"max={_fmt(snapshot.composite_score_session_max)}",
        f"  trend:     {snapshot.trend.value} "
        f"slope={_fmt(snapshot.convergence_slope)} "
        f"osc={_fmt(snapshot.convergence_oscillation_ratio)}",
        f"  completion_rate:        {_fmt_pct(snapshot.session_completion_rate)}",
        f"  self_formation_ratio:   {_fmt_pct(snapshot.self_formation_ratio)}",
        f"  postmortem_recall_rate: {_fmt_pct(snapshot.postmortem_recall_rate)}",
        f"  cost_per_apply:         {_fmt_money(snapshot.cost_per_successful_apply)}",
        f"  posture_stability:      {_fmt_seconds(snapshot.posture_stability_seconds)}",
        f"  ops_inspected:          {snapshot.ops_inspected}"
        f"{' (truncated)' if snapshot.ops_truncated else ''}",
    ]
    if snapshot.per_op_composite_scores:
        spark = render_sparkline(snapshot.per_op_composite_scores)
        lines.append(f"  per-op spark: {spark}")
    if snapshot.notes:
        lines.append(f"  notes: {', '.join(snapshot.notes)}")
    return _ascii_clip("\n".join(lines))


def render_window(
    agg: AggregatedMetrics,
    sparkline_values: Sequence[float] = (),
) -> str:
    lines = [
        f"[metrics] window={agg.window_days}d "
        f"snapshots={agg.snapshots_in_window}",
    ]
    if agg.snapshots_in_window == 0:
        lines.append("  (no snapshots in window)")
        if agg.notes:
            lines.append(f"  notes: {', '.join(agg.notes)}")
        return _ascii_clip("\n".join(lines))

    lines.extend([
        f"  composite: mean={_fmt(agg.composite_score_mean)} "
        f"min={_fmt(agg.composite_score_min)} "
        f"max={_fmt(agg.composite_score_max)}",
        f"  trend:     {agg.window_trend.value} "
        f"slope={_fmt(agg.window_slope)} "
        f"osc={_fmt(agg.window_oscillation_ratio)}",
        f"  completion_rate (mean):        {_fmt_pct(agg.completion_rate_mean)}",
        f"  self_formation_ratio (mean):   {_fmt_pct(agg.self_formation_ratio_mean)}",
        f"  postmortem_recall_rate (mean): {_fmt_pct(agg.postmortem_recall_rate_mean)}",
        f"  cost_per_apply (mean):         {_fmt_money(agg.cost_per_apply_mean)}",
        f"  posture_stability (mean):      {_fmt_seconds(agg.posture_stability_mean)}",
    ])
    if sparkline_values:
        lines.append(f"  composite spark: {render_sparkline(sparkline_values)}")
    if agg.notes:
        lines.append(f"  notes: {', '.join(agg.notes)}")
    return _ascii_clip("\n".join(lines))


def render_trend_banner(
    snapshot: Optional[MetricsSnapshot],
    agg7: Optional[AggregatedMetrics] = None,
) -> str:
    """One-line trend banner combining latest snapshot trend +
    7-day window trend if available."""
    cur = (
        f"latest={snapshot.trend.value}"
        if snapshot is not None else "latest=NO_DATA"
    )
    win = (
        f"7d={agg7.window_trend.value} slope={_fmt(agg7.window_slope)}"
        if agg7 is not None and agg7.snapshots_in_window > 0
        else "7d=NO_DATA"
    )
    return _ascii_clip(f"[metrics] trend  {cur}  |  {win}")


def render_composite_only_sparkline(
    composite_history: Sequence[float],
    rows_seen: int,
) -> str:
    if not composite_history:
        return _ascii_clip("[metrics] composite history: (empty)")
    spark = render_sparkline(composite_history)
    lo, hi = min(composite_history), max(composite_history)
    return _ascii_clip("\n".join([
        f"[metrics] composite history: {len(composite_history)} pts "
        f"(rows scanned: {rows_seen})",
        f"  range: min={_fmt(lo)}  max={_fmt(hi)}",
        f"  spark: {spark}",
    ]))


def render_why(snapshot_dict: dict) -> str:
    sid = snapshot_dict.get("session_id", "?")
    lines = [
        f"[metrics] why session={sid}",
        f"  schema:    v{snapshot_dict.get('schema_version', '?')}",
        f"  composite: mean={_fmt(snapshot_dict.get('composite_score_session_mean'))} "
        f"min={_fmt(snapshot_dict.get('composite_score_session_min'))} "
        f"max={_fmt(snapshot_dict.get('composite_score_session_max'))}",
        f"  trend:     {snapshot_dict.get('trend', '?')} "
        f"slope={_fmt(snapshot_dict.get('convergence_slope'))}",
        f"  completion_rate:        {_fmt_pct(snapshot_dict.get('session_completion_rate'))}",
        f"  self_formation_ratio:   {_fmt_pct(snapshot_dict.get('self_formation_ratio'))}",
        f"  postmortem_recall_rate: {_fmt_pct(snapshot_dict.get('postmortem_recall_rate'))}",
        f"  cost_per_apply:         {_fmt_money(snapshot_dict.get('cost_per_successful_apply'))}",
        f"  posture_stability:      {_fmt_seconds(snapshot_dict.get('posture_stability_seconds'))}",
        f"  ops_inspected:          {snapshot_dict.get('ops_inspected', 0)}",
    ]
    return _ascii_clip("\n".join(lines))


# ---- formatters ----


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.3f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_pct(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v) * 100:.1f}%"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_money(v: Any) -> str:
    if v is None:
        return "n/a (no commits)"
    try:
        return f"${float(v):.4f}"
    except (TypeError, ValueError):
        return "n/a"


def _fmt_seconds(v: Any) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{float(v):.0f}s"
    except (TypeError, ValueError):
        return "n/a"


def _ascii_clip(text: str) -> str:
    safe = text.encode("ascii", errors="replace").decode("ascii")
    if len(safe) <= MAX_RENDERED_BYTES:
        return safe
    return safe[: MAX_RENDERED_BYTES - 30] + "\n... (rendered output clipped)"


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


_SUBCOMMANDS = frozenset({
    "current", "7d", "30d", "composite", "trend", "why", "help",
})


@dataclass
class MetricsReplDispatcher:
    """Self-contained dispatcher for the ``/metrics`` REPL surface.

    Slice 3 ships **NO** orchestrator wiring — Slice 5 graduation
    constructs this with the live engine + ledger. Until then the
    dispatcher is unit-testable end-to-end with injected primitives.

    Latest-snapshot lookup strategy:
      * If the caller provides ``latest_snapshot_provider``, use it
        (Slice 4-5 wires this against the post-VERIFY summary path).
      * Otherwise fall back to "tail of the ledger" — read the last
        row + reconstruct enough of a snapshot for ``current`` /
        ``trend`` rendering. Falls through to NO_DATA cleanly.
    """

    engine: Optional[MetricsEngine] = None
    ledger: Optional[MetricsHistoryLedger] = None
    latest_snapshot_provider: Optional[Callable[[], Optional[MetricsSnapshot]]] = None

    def _eng(self) -> MetricsEngine:
        return self.engine or get_default_engine()

    def _ledger(self) -> MetricsHistoryLedger:
        return self.ledger or get_default_ledger()

    # ---- public entry point ----

    def handle(self, line: str) -> MetricsReplResult:
        """Dispatch a single REPL input line.

        Accepts:
          * ``"/metrics"``                 — bare → current
          * ``"/metrics current"``
          * ``"/metrics 7d"`` / ``"30d"``
          * ``"/metrics composite"``
          * ``"/metrics trend"``
          * ``"/metrics why <session-id>"``
          * ``"/metrics help"``
        """
        if not line or not line.strip():
            return MetricsReplResult(
                status=MetricsReplStatus.EMPTY,
                rendered_text="(empty input)",
            )
        stripped = line.strip()
        # Strip /metrics prefix if present (also accept bare subcommand).
        if stripped.startswith("/metrics"):
            tail = stripped[len("/metrics"):].lstrip()
        else:
            tail = stripped

        if not tail:
            return self._handle_current()

        first, _, rest = tail.partition(" ")
        first = first.strip().lower()
        rest = rest.strip()

        if first not in _SUBCOMMANDS:
            return MetricsReplResult(
                status=MetricsReplStatus.UNKNOWN_SUBCOMMAND,
                rendered_text=(
                    f"[metrics] unknown subcommand: {first!r}\n"
                    + render_help()
                ),
            )

        # Shape gate (mirrors P2/P3 patterns):
        if not self._args_match_subcommand(first, rest):
            return MetricsReplResult(
                status=MetricsReplStatus.UNKNOWN_SUBCOMMAND,
                rendered_text=(
                    f"[metrics] {first!r} subcommand expects different args\n"
                    + render_help()
                ),
            )

        if first == "current":
            return self._handle_current()
        if first == "7d":
            return self._handle_window(DEFAULT_WINDOW_7D_DAYS)
        if first == "30d":
            return self._handle_window(DEFAULT_WINDOW_30D_DAYS)
        if first == "composite":
            return self._handle_composite_history()
        if first == "trend":
            return self._handle_trend()
        if first == "why":
            return self._handle_why(rest)
        if first == "help":
            return MetricsReplResult(
                status=MetricsReplStatus.OK,
                rendered_text=render_help(),
            )
        return MetricsReplResult(
            status=MetricsReplStatus.UNKNOWN_SUBCOMMAND,
            rendered_text=render_help(),
        )

    # ---- shape gating ----

    @staticmethod
    def _args_match_subcommand(sub: str, args: str) -> bool:
        """Subcommand fires only when args match expected shape; else
        the dispatcher renders a help-prefixed error so a typo doesn't
        misroute. Matches the P3 / P2 dispatcher patterns."""
        if sub in ("current", "7d", "30d", "composite",
                   "trend", "help"):
            return not args
        if sub == "why":
            parts = args.split()
            return len(parts) == 1 and bool(re.match(r"^[\w\-]+$", parts[0]))
        return False

    # ---- subcommand handlers ----

    def _handle_current(self) -> MetricsReplResult:
        snap = self._latest_snapshot_safe()
        if snap is None:
            return MetricsReplResult(
                status=MetricsReplStatus.OK,
                rendered_text="[metrics] current: (no snapshot available)",
            )
        return MetricsReplResult(
            status=MetricsReplStatus.OK,
            rendered_text=render_current(snap),
            snapshot_dict=snap.to_dict(),
        )

    def _handle_window(self, days: int) -> MetricsReplResult:
        try:
            agg = self._ledger().aggregate_window(days)
            rows = self._ledger().read_window_days(days)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsReplDispatcher] window read failed: %s", exc,
            )
            return MetricsReplResult(
                status=MetricsReplStatus.READ_ERROR,
                rendered_text=f"[metrics] window read failed: {exc}",
            )
        composites = _composites_from_rows(rows)
        return MetricsReplResult(
            status=MetricsReplStatus.OK,
            rendered_text=render_window(agg, composites),
            aggregate=agg,
        )

    def _handle_composite_history(self) -> MetricsReplResult:
        try:
            rows = self._ledger().read_all(
                limit=COMPOSITE_HISTORY_MAX_ROWS,
            )
        except Exception as exc:  # noqa: BLE001
            return MetricsReplResult(
                status=MetricsReplStatus.READ_ERROR,
                rendered_text=f"[metrics] composite history read failed: {exc}",
            )
        composites = _composites_from_rows(rows)
        return MetricsReplResult(
            status=MetricsReplStatus.OK,
            rendered_text=render_composite_only_sparkline(
                composites, rows_seen=len(rows),
            ),
        )

    def _handle_trend(self) -> MetricsReplResult:
        snap = self._latest_snapshot_safe()
        try:
            agg7 = self._ledger().aggregate_window(DEFAULT_WINDOW_7D_DAYS)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsReplDispatcher] trend agg failed: %s", exc,
            )
            agg7 = None
        return MetricsReplResult(
            status=MetricsReplStatus.OK,
            rendered_text=render_trend_banner(snap, agg7),
            aggregate=agg7,
        )

    def _handle_why(self, session_id: str) -> MetricsReplResult:
        try:
            rows = self._ledger().read_all()
        except Exception as exc:  # noqa: BLE001
            return MetricsReplResult(
                status=MetricsReplStatus.READ_ERROR,
                rendered_text=f"[metrics] why read failed: {exc}",
            )
        match = next(
            (r for r in reversed(rows)
             if isinstance(r, dict) and r.get("session_id") == session_id),
            None,
        )
        if match is None:
            return MetricsReplResult(
                status=MetricsReplStatus.UNKNOWN_SESSION,
                rendered_text=(
                    f"[metrics] no snapshot with session_id={session_id!r}"
                ),
            )
        return MetricsReplResult(
            status=MetricsReplStatus.OK,
            rendered_text=render_why(match),
            snapshot_dict=match,
        )

    # ---- internals ----

    def _latest_snapshot_safe(self) -> Optional[MetricsSnapshot]:
        # Try the wired provider first; on raise OR None, fall back
        # to the ledger tail so operators always get something
        # readable when the engine wiring is degraded.
        if self.latest_snapshot_provider is not None:
            try:
                snap = self.latest_snapshot_provider()
                if snap is not None:
                    return snap
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[MetricsReplDispatcher] provider failed: %s "
                    "(falling back to ledger tail)", exc,
                )
        # Ledger-tail fallback.
        try:
            rows = self._ledger().read_all(limit=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[MetricsReplDispatcher] tail read failed: %s", exc,
            )
            return None
        if not rows:
            return None
        return _row_to_snapshot(rows[-1])


# ---------------------------------------------------------------------------
# Helpers used by handlers + tests
# ---------------------------------------------------------------------------


def _composites_from_rows(rows: Iterable[dict]) -> List[float]:
    out: List[float] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        v = r.get("composite_score_session_mean")
        if v is None:
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _row_to_snapshot(row: dict) -> Optional[MetricsSnapshot]:
    """Reconstruct a partial :class:`MetricsSnapshot` from a JSONL row.
    Used by the dispatcher's tail-of-ledger fallback. Best-effort —
    a malformed row returns ``None``."""
    try:
        trend_raw = row.get("trend") or "INSUFFICIENT_DATA"
        try:
            trend = TrendDirection(trend_raw)
        except ValueError:
            trend = TrendDirection.INSUFFICIENT_DATA
        return MetricsSnapshot(
            schema_version=int(
                row.get("schema_version", METRICS_SNAPSHOT_SCHEMA_VERSION),
            ),
            session_id=str(row.get("session_id", "?")),
            computed_at_unix=float(row.get("computed_at_unix", 0.0)),
            composite_score_session_mean=row.get("composite_score_session_mean"),
            composite_score_session_min=row.get("composite_score_session_min"),
            composite_score_session_max=row.get("composite_score_session_max"),
            per_op_composite_scores=tuple(
                row.get("per_op_composite_scores") or ()
            ),
            trend=trend,
            convergence_slope=row.get("convergence_slope"),
            convergence_oscillation_ratio=row.get("convergence_oscillation_ratio"),
            convergence_scores_analyzed=int(
                row.get("convergence_scores_analyzed", 0),
            ),
            convergence_recommendation=str(
                row.get("convergence_recommendation", ""),
            ),
            session_completion_rate=row.get("session_completion_rate"),
            self_formation_ratio=row.get("self_formation_ratio"),
            postmortem_recall_rate=row.get("postmortem_recall_rate"),
            cost_per_successful_apply=row.get("cost_per_successful_apply"),
            posture_stability_seconds=row.get("posture_stability_seconds"),
            ops_inspected=int(row.get("ops_inspected", 0)),
            ops_truncated=bool(row.get("ops_truncated", False)),
            notes=tuple(row.get("notes") or ()),
        )
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "COMPOSITE_HISTORY_MAX_ROWS",
    "MAX_RENDERED_BYTES",
    "MetricsReplDispatcher",
    "MetricsReplResult",
    "MetricsReplStatus",
    "SPARKLINE_CHARS",
    "SPARKLINE_WIDTH",
    "is_enabled",
    "render_composite_only_sparkline",
    "render_current",
    "render_help",
    "render_sparkline",
    "render_trend_banner",
    "render_why",
    "render_window",
]
