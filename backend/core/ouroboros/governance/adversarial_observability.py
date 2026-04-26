"""P5 Slice 4 — AdversarialReviewer observability surfaces.

Per OUROBOROS_VENOM_PRD.md §9 Phase 5 P5 + Forward-Looking Priority
Roadmap (priority 2). Closes the operator-visible surface for the
AdversarialReviewer subagent shipped in Slices 1-3:

  * ``/adversarial`` REPL dispatcher (5 subcommands).
  * 4 IDE GET endpoints under ``/observability/adversarial``.
  * SSE event ``adversarial_findings_emitted`` (added to broker
    allow-list in ``ide_observability_stream.py`` Slice 4 step 1).

All three surfaces read from Slice 2's JSONL audit ledger
(``.jarvis/adversarial_review_audit.jsonl``); they NEVER trigger a
new review (that's the orchestrator hook's job from Slice 3 +
Slice 5 graduation). Operator value: scan the last N reviews,
inspect why a particular review was skipped or what findings it
raised, see aggregate stats (totals + cost + skip-reason
histogram), get live SSE pings on new reviews.

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * Allowed: ``adversarial_reviewer`` + ``adversarial_reviewer_service``
    (own slice family) + ``ide_observability_stream`` (broker for
    SSE publish).
  * Allowed I/O: read-only of the JSONL ledger path. No subprocess
    / env mutation / network. Writes are forbidden — this module
    can ONLY observe what the service already wrote.
  * Best-effort throughout — every reader / publisher call wrapped
    in ``try / except``; failures NEVER raise into callers.
  * ASCII-strict rendering (encode/decode round-trip pin).
  * Surfaces gate on ``JARVIS_ADVERSARIAL_REVIEWER_ENABLED`` (Slice
    1 master flag): off → REPL renders "(disabled)", endpoints 403,
    SSE drops silently.

Default-off behind ``JARVIS_ADVERSARIAL_REVIEWER_ENABLED``. Slice 5
graduation flips the default + wires the IDE registration into
``EventChannelServer.start``.
"""
from __future__ import annotations

import enum
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

from backend.core.ouroboros.governance.adversarial_reviewer import (
    AdversarialReview,
    is_enabled,
)
from backend.core.ouroboros.governance.adversarial_reviewer_service import (
    AUDIT_LEDGER_SCHEMA_VERSION,
    audit_ledger_path,
)

logger = logging.getLogger(__name__)


# Cap on rendered REPL output bytes per call. Mirrors the P4 metrics
# REPL clip so all REPL surfaces behave identically.
MAX_RENDERED_BYTES: int = 16 * 1024  # 16 KiB

# Read cap for the JSONL ledger. Mirrors the P4 metrics history cap
# so REPL + IDE GET surfaces stay bounded under a long-running
# session that accumulates many reviews.
MAX_LINES_READ: int = 8_192

# Default history count for /adversarial history with no arg + IDE
# GET history endpoint. Operators usually want a quick look.
HISTORY_DEFAULT_N: int = 10
HISTORY_MAX_N: int = MAX_LINES_READ

# Schema version stamped into IDE GET responses so clients can pin a
# parser version. Independent of the audit ledger schema.
ADVERSARIAL_OBSERVABILITY_SCHEMA_VERSION: str = "1.0"

# Op-id grammar — same character class as the existing
# ``_SESSION_ID_RE`` in ide_observability + the metrics observability
# session_id regex so all /observability/* surfaces accept the same
# id shapes.
_OP_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")

# A turn-id always starts with a letter for the /adversarial why
# subcommand shape gate. Mirrors the chat dispatcher's gate pattern.
_WHY_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-:.]+$")


# ---------------------------------------------------------------------------
# Status enum + result dataclass (REPL)
# ---------------------------------------------------------------------------


class AdversarialReplStatus(str, enum.Enum):
    OK = "OK"
    EMPTY = "EMPTY"
    UNKNOWN_SUBCOMMAND = "UNKNOWN_SUBCOMMAND"
    UNKNOWN_OP = "UNKNOWN_OP"
    READ_ERROR = "READ_ERROR"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class AdversarialReplResult:
    status: AdversarialReplStatus
    rendered_text: str
    review_dict: Optional[Dict[str, Any]] = None
    notes: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Ledger reader (read-only, best-effort)
# ---------------------------------------------------------------------------


def _read_ledger_rows(
    path: Optional[Path] = None,
    *,
    limit: int = MAX_LINES_READ,
) -> List[Dict[str, Any]]:
    """Read parsed rows from the audit ledger tail. Best-effort:
    missing file → []; OSError → []; malformed JSON lines silently
    dropped (concurrent-writer truncation tolerance — same pattern
    as Slice 2's _AdversarialAuditLedger and the P4 metrics ledger
    reader)."""
    p = path or audit_ledger_path()
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(
            "[AdversarialObservability] ledger read failed: %s", exc,
        )
        return []
    cap = max(1, min(int(limit), MAX_LINES_READ))
    lines = text.splitlines()
    if len(lines) > cap:
        lines = lines[-cap:]
    out: List[Dict[str, Any]] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue  # truncated mid-write — drop
        if isinstance(row, dict):
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Aggregate stats
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AdversarialStats:
    """Aggregate over a window of reviews — used by REPL ``stats`` +
    IDE GET ``stats`` endpoint."""

    total_reviews: int = 0
    completed_reviews: int = 0   # not skipped
    skipped_reviews: int = 0
    skip_reason_histogram: Dict[str, int] = field(default_factory=dict)
    total_findings: int = 0
    severity_histogram: Dict[str, int] = field(default_factory=dict)
    total_cost_usd: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_reviews": self.total_reviews,
            "completed_reviews": self.completed_reviews,
            "skipped_reviews": self.skipped_reviews,
            "skip_reason_histogram": dict(self.skip_reason_histogram),
            "total_findings": self.total_findings,
            "severity_histogram": dict(self.severity_histogram),
            "total_cost_usd": self.total_cost_usd,
        }


def compute_stats(rows: List[Dict[str, Any]]) -> AdversarialStats:
    """Pure aggregator — testable without a ledger instance."""
    total = 0
    completed = 0
    skipped = 0
    skip_hist: Dict[str, int] = {}
    findings_total = 0
    sev_hist: Dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    cost_total = 0.0

    for r in rows:
        if not isinstance(r, dict):
            continue
        total += 1
        skip_reason = str(r.get("skip_reason") or "").strip()
        if skip_reason:
            skipped += 1
            skip_hist[skip_reason] = skip_hist.get(skip_reason, 0) + 1
        else:
            completed += 1
        try:
            cost_total += float(r.get("cost_usd") or 0.0)
        except (TypeError, ValueError):
            pass
        try:
            findings_total += int(r.get("filtered_findings_count") or 0)
        except (TypeError, ValueError):
            pass
        # Per-row severity histogram is in the audit row itself.
        row_hist = r.get("severity_histogram") or {}
        if isinstance(row_hist, Mapping):
            for k in ("HIGH", "MEDIUM", "LOW"):
                try:
                    sev_hist[k] += int(row_hist.get(k) or 0)
                except (TypeError, ValueError):
                    pass

    return AdversarialStats(
        total_reviews=total,
        completed_reviews=completed,
        skipped_reviews=skipped,
        skip_reason_histogram=skip_hist,
        total_findings=findings_total,
        severity_histogram=sev_hist,
        total_cost_usd=cost_total,
    )


# ---------------------------------------------------------------------------
# Renderers (ASCII-strict)
# ---------------------------------------------------------------------------


def render_help() -> str:
    return _ascii_clip("\n".join([
        "[adversarial] /adversarial subcommands:",
        "  /adversarial current      latest review summary",
        "  /adversarial history [N]  last N reviews (default 10, max 8192)",
        "  /adversarial why <op-id>  drill into one review's findings",
        "  /adversarial stats        aggregate stats",
        "  /adversarial help         this listing",
    ]))


def render_review_summary(row: Dict[str, Any]) -> str:
    op_id = row.get("op_id", "?")
    skip = str(row.get("skip_reason") or "").strip()
    findings = int(row.get("filtered_findings_count") or 0)
    raw = int(row.get("raw_findings_count") or 0)
    cost = float(row.get("cost_usd") or 0.0)
    model = str(row.get("model_used") or "").strip() or "?"
    hist = row.get("severity_histogram") or {}
    high = int(hist.get("HIGH") or 0) if isinstance(hist, Mapping) else 0
    med = int(hist.get("MEDIUM") or 0) if isinstance(hist, Mapping) else 0
    low = int(hist.get("LOW") or 0) if isinstance(hist, Mapping) else 0

    lines = [f"[adversarial] op={op_id}"]
    if skip:
        lines.append(f"  skipped: {skip}")
    lines.extend([
        f"  findings: filtered={findings} raw={raw} "
        f"(high={high}, med={med}, low={low})",
        f"  cost_usd: {cost:.4f}  model: {model}",
    ])
    return _ascii_clip("\n".join(lines))


def render_review_detail(row: Dict[str, Any]) -> str:
    op_id = row.get("op_id", "?")
    skip = str(row.get("skip_reason") or "").strip()
    findings = row.get("findings") or []

    lines = [f"[adversarial] why op={op_id}"]
    if skip:
        lines.append(f"  skipped: {skip}")
    if not findings:
        lines.append("  (no findings)")
    else:
        for i, f in enumerate(findings, 1):
            if not isinstance(f, Mapping):
                continue
            sev = str(f.get("severity") or "?")
            cat = str(f.get("category") or "?")
            desc = str(f.get("description") or "")
            mit = str(f.get("mitigation_hint") or "")
            ref = str(f.get("file_reference") or "")
            lines.append(f"  {i}. [{sev}] [{cat}] {desc}")
            if ref:
                lines.append(f"     file: {ref}")
            if mit:
                lines.append(f"     mitigation: {mit}")
    notes = row.get("notes") or []
    if notes:
        lines.append(f"  notes: {', '.join(str(n) for n in notes[:8])}")
    return _ascii_clip("\n".join(lines))


def render_history(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return _ascii_clip("[adversarial] history: (empty)")
    lines = [f"[adversarial] history: {len(rows)} reviews"]
    for r in rows:
        op_id = r.get("op_id", "?")
        skip = str(r.get("skip_reason") or "").strip()
        findings = int(r.get("filtered_findings_count") or 0)
        cost = float(r.get("cost_usd") or 0.0)
        if skip:
            lines.append(
                f"  {op_id} skipped={skip} cost_usd={cost:.4f}"
            )
        else:
            lines.append(
                f"  {op_id} findings={findings} cost_usd={cost:.4f}"
            )
    return _ascii_clip("\n".join(lines))


def render_stats(stats: AdversarialStats) -> str:
    if stats.total_reviews == 0:
        return _ascii_clip("[adversarial] stats: (no reviews on file)")
    lines = [
        "[adversarial] stats:",
        f"  total reviews:      {stats.total_reviews}",
        f"  completed:          {stats.completed_reviews}",
        f"  skipped:            {stats.skipped_reviews}",
        f"  total findings:     {stats.total_findings}",
        f"  severity:           high={stats.severity_histogram.get('HIGH', 0)} "
        f"med={stats.severity_histogram.get('MEDIUM', 0)} "
        f"low={stats.severity_histogram.get('LOW', 0)}",
        f"  total cost (USD):   {stats.total_cost_usd:.4f}",
    ]
    if stats.skip_reason_histogram:
        for reason, n in sorted(stats.skip_reason_histogram.items()):
            lines.append(f"  skip:{reason:<20s} {n}")
    return _ascii_clip("\n".join(lines))


def _ascii_clip(text: str) -> str:
    safe = text.encode("ascii", errors="replace").decode("ascii")
    if len(safe) <= MAX_RENDERED_BYTES:
        return safe
    return safe[: MAX_RENDERED_BYTES - 30] + "\n... (rendered output clipped)"


# ---------------------------------------------------------------------------
# REPL dispatcher
# ---------------------------------------------------------------------------


_SUBCOMMANDS = frozenset({"current", "history", "why", "stats", "help"})


@dataclass
class AdversarialReplDispatcher:
    """Self-contained REPL surface for the adversarial reviewer.

    Slice 4 ships NO SerpentFlow auto-wiring — Slice 5 graduation
    will mount this. Until then, callers (tests + Slice 5) instantiate
    explicitly + invoke ``handle(line)``."""

    ledger_path: Optional[Path] = None

    def handle(self, line: str) -> AdversarialReplResult:
        if not line or not line.strip():
            return AdversarialReplResult(
                status=AdversarialReplStatus.EMPTY,
                rendered_text="(empty input)",
            )
        # Master-off branch — single check at the entry point so all
        # subcommands share the same gating.
        if not is_enabled():
            return AdversarialReplResult(
                status=AdversarialReplStatus.DISABLED,
                rendered_text=(
                    "[adversarial] disabled "
                    "(JARVIS_ADVERSARIAL_REVIEWER_ENABLED=false)"
                ),
            )

        stripped = line.strip()
        if stripped.startswith("/adversarial"):
            tail = stripped[len("/adversarial"):].lstrip()
        else:
            tail = stripped
        if not tail:
            return self._handle_current()

        first, _, rest = tail.partition(" ")
        first = first.strip().lower()
        rest = rest.strip()

        if first not in _SUBCOMMANDS:
            return AdversarialReplResult(
                status=AdversarialReplStatus.UNKNOWN_SUBCOMMAND,
                rendered_text=(
                    f"[adversarial] unknown subcommand: {first!r}\n"
                    + render_help()
                ),
            )
        if not self._args_match_subcommand(first, rest):
            return AdversarialReplResult(
                status=AdversarialReplStatus.UNKNOWN_SUBCOMMAND,
                rendered_text=(
                    f"[adversarial] {first!r} expects different args\n"
                    + render_help()
                ),
            )

        if first == "current":
            return self._handle_current()
        if first == "history":
            return self._handle_history(rest)
        if first == "why":
            return self._handle_why(rest)
        if first == "stats":
            return self._handle_stats()
        if first == "help":
            return AdversarialReplResult(
                status=AdversarialReplStatus.OK,
                rendered_text=render_help(),
            )
        # Unreachable: every member of _SUBCOMMANDS handled above.
        return AdversarialReplResult(
            status=AdversarialReplStatus.UNKNOWN_SUBCOMMAND,
            rendered_text=render_help(),
        )

    @staticmethod
    def _args_match_subcommand(sub: str, args: str) -> bool:
        if sub in ("current", "stats", "help"):
            return not args
        if sub == "history":
            if not args:
                return True
            parts = args.split()
            if len(parts) != 1:
                return False
            try:
                return int(parts[0]) >= 0
            except ValueError:
                return False
        if sub == "why":
            parts = args.split()
            return len(parts) == 1 and bool(_WHY_TOKEN_RE.match(parts[0]))
        return False

    def _handle_current(self) -> AdversarialReplResult:
        try:
            rows = _read_ledger_rows(self.ledger_path, limit=1)
        except Exception as exc:  # noqa: BLE001
            return AdversarialReplResult(
                status=AdversarialReplStatus.READ_ERROR,
                rendered_text=f"[adversarial] read failed: {exc}",
            )
        if not rows:
            return AdversarialReplResult(
                status=AdversarialReplStatus.OK,
                rendered_text="[adversarial] current: (no reviews on file)",
            )
        return AdversarialReplResult(
            status=AdversarialReplStatus.OK,
            rendered_text=render_review_summary(rows[-1]),
            review_dict=rows[-1],
        )

    def _handle_history(self, args: str) -> AdversarialReplResult:
        n = HISTORY_DEFAULT_N
        if args:
            try:
                v = int(args.split()[0])
                n = max(1, min(v, HISTORY_MAX_N)) if v > 0 else HISTORY_DEFAULT_N
            except (TypeError, ValueError, IndexError):
                n = HISTORY_DEFAULT_N
        try:
            rows = _read_ledger_rows(self.ledger_path, limit=n)
        except Exception as exc:  # noqa: BLE001
            return AdversarialReplResult(
                status=AdversarialReplStatus.READ_ERROR,
                rendered_text=f"[adversarial] history read failed: {exc}",
            )
        return AdversarialReplResult(
            status=AdversarialReplStatus.OK,
            rendered_text=render_history(rows),
        )

    def _handle_why(self, op_id: str) -> AdversarialReplResult:
        try:
            rows = _read_ledger_rows(self.ledger_path)
        except Exception as exc:  # noqa: BLE001
            return AdversarialReplResult(
                status=AdversarialReplStatus.READ_ERROR,
                rendered_text=f"[adversarial] why read failed: {exc}",
            )
        match = next(
            (r for r in reversed(rows)
             if isinstance(r, dict) and r.get("op_id") == op_id),
            None,
        )
        if match is None:
            return AdversarialReplResult(
                status=AdversarialReplStatus.UNKNOWN_OP,
                rendered_text=f"[adversarial] no review with op_id={op_id!r}",
            )
        return AdversarialReplResult(
            status=AdversarialReplStatus.OK,
            rendered_text=render_review_detail(match),
            review_dict=match,
        )

    def _handle_stats(self) -> AdversarialReplResult:
        try:
            rows = _read_ledger_rows(self.ledger_path)
        except Exception as exc:  # noqa: BLE001
            return AdversarialReplResult(
                status=AdversarialReplStatus.READ_ERROR,
                rendered_text=f"[adversarial] stats read failed: {exc}",
            )
        return AdversarialReplResult(
            status=AdversarialReplStatus.OK,
            rendered_text=render_stats(compute_stats(rows)),
        )


# ---------------------------------------------------------------------------
# IDE GET endpoints
# ---------------------------------------------------------------------------


def register_adversarial_routes(
    app: Any,
    *,
    ledger_path: Optional[Path] = None,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Dict[str, str]]] = None,
) -> None:
    """Mount 4 GET routes on a caller-supplied aiohttp Application.

    Mirrors the P4 metrics observability shape: gate check (master
    flag + rate limit) → handler → ``_json`` response with
    ``schema_version`` + ``Cache-Control: no-store`` + CORS.

    When called with ``rate_limit_check=None``, every request is
    allowed (test convenience). Production callers MUST supply both
    callables (Slice 5 graduation does this in
    ``EventChannelServer.start``)."""
    handler = _AdversarialRoutesHandler(
        ledger_path=ledger_path,
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/adversarial", handler.handle_current,
    )
    app.router.add_get(
        "/observability/adversarial/history", handler.handle_history,
    )
    app.router.add_get(
        "/observability/adversarial/stats", handler.handle_stats,
    )
    app.router.add_get(
        "/observability/adversarial/{op_id}", handler.handle_detail,
    )


@dataclass
class _AdversarialRoutesHandler:
    ledger_path: Optional[Path] = None
    rate_limit_check: Optional[Callable[[Any], bool]] = None
    cors_headers: Optional[Callable[[Any], Dict[str, str]]] = None

    def _gate_check(self, request: Any) -> Optional[Any]:
        if not is_enabled():
            return self._error(
                request, 403, "ide_observability.disabled",
            )
        if self.rate_limit_check is not None:
            try:
                if not self.rate_limit_check(request):
                    return self._error(
                        request, 429, "ide_observability.rate_limited",
                    )
            except Exception:  # noqa: BLE001
                # Defensive — broken rate limiter shouldn't 500 the
                # endpoint. Treat as allowed; log debug.
                logger.debug(
                    "[AdversarialObservability] rate_limit_check raised",
                    exc_info=True,
                )
        return None

    def _json(
        self, request: Any, status: int, payload: Dict[str, Any],
    ) -> Any:
        from aiohttp import web
        if "schema_version" not in payload:
            payload = {
                "schema_version": ADVERSARIAL_OBSERVABILITY_SCHEMA_VERSION,
                **payload,
            }
        resp = web.json_response(payload, status=status)
        if self.cors_headers is not None:
            try:
                for k, v in self.cors_headers(request).items():
                    resp.headers[k] = v
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[AdversarialObservability] cors_headers raised",
                    exc_info=True,
                )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _error(self, request: Any, status: int, code: str) -> Any:
        return self._json(
            request, status, {"error": True, "reason_code": code},
        )

    async def handle_current(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        try:
            rows = _read_ledger_rows(self.ledger_path, limit=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AdversarialObservability] current read failed: %s",
                exc,
            )
            return self._json(
                request, 200,
                {"review": None, "reason_code": "read_failed"},
            )
        if not rows:
            return self._json(request, 200, {"review": None})
        return self._json(request, 200, {"review": rows[-1]})

    async def handle_history(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        try:
            limit = max(
                1, min(int(request.query.get("limit", str(HISTORY_DEFAULT_N))),
                       HISTORY_MAX_N),
            )
        except (TypeError, ValueError):
            return self._error(
                request, 400, "ide_observability.malformed_limit",
            )
        try:
            rows = _read_ledger_rows(self.ledger_path, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AdversarialObservability] history read failed: %s",
                exc,
            )
            return self._json(
                request, 200,
                {"reviews": [], "reason_code": "read_failed"},
            )
        return self._json(
            request, 200,
            {"reviews": rows, "rows_seen": len(rows)},
        )

    async def handle_stats(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        try:
            rows = _read_ledger_rows(self.ledger_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AdversarialObservability] stats read failed: %s",
                exc,
            )
            return self._json(
                request, 200,
                {"stats": None, "reason_code": "read_failed"},
            )
        return self._json(
            request, 200, {"stats": compute_stats(rows).to_dict()},
        )

    async def handle_detail(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        op_id = request.match_info.get("op_id", "")
        if not _OP_ID_RE.match(op_id):
            return self._error(
                request, 400, "ide_observability.bad_op_id",
            )
        try:
            rows = _read_ledger_rows(self.ledger_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AdversarialObservability] detail read failed: %s",
                exc,
            )
            return self._json(
                request, 200,
                {"review": None, "reason_code": "read_failed"},
            )
        match = next(
            (r for r in reversed(rows)
             if isinstance(r, dict) and r.get("op_id") == op_id),
            None,
        )
        if match is None:
            return self._error(
                request, 404, "ide_observability.review_not_found",
            )
        return self._json(request, 200, {"review": match})


# ---------------------------------------------------------------------------
# SSE bridge helper
# ---------------------------------------------------------------------------


def publish_adversarial_findings_emitted(
    review: AdversarialReview,
) -> Optional[str]:
    """Fire the ``adversarial_findings_emitted`` SSE event for
    ``review``.

    Returns the broker-assigned event id when published, else None
    (broker missing / disabled / publish raised). Best-effort —
    NEVER raises. Mirrors P4's ``publish_metrics_updated`` pattern.

    Payload is summary-only (op_id, schema_version, severity counts,
    skip_reason, cost) — full record lives at the GET endpoint."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED,
            get_default_broker,
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        hist = review.severity_histogram()
        broker = get_default_broker()
        return broker.publish(
            event_type=EVENT_TYPE_ADVERSARIAL_FINDINGS_EMITTED,
            op_id=review.op_id,
            payload={
                "op_id": review.op_id,
                "schema_version": AUDIT_LEDGER_SCHEMA_VERSION,
                "filtered_findings_count": review.filtered_findings_count,
                "high": hist.get("HIGH", 0),
                "med": hist.get("MEDIUM", 0),
                "low": hist.get("LOW", 0),
                "skip_reason": review.skip_reason,
                "cost_usd": review.cost_usd,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[AdversarialObservability] publish swallowed: %s", exc,
        )
        return None


__all__ = [
    "ADVERSARIAL_OBSERVABILITY_SCHEMA_VERSION",
    "AdversarialReplDispatcher",
    "AdversarialReplResult",
    "AdversarialReplStatus",
    "AdversarialStats",
    "HISTORY_DEFAULT_N",
    "HISTORY_MAX_N",
    "MAX_LINES_READ",
    "MAX_RENDERED_BYTES",
    "compute_stats",
    "publish_adversarial_findings_emitted",
    "register_adversarial_routes",
    "render_help",
    "render_history",
    "render_review_detail",
    "render_review_summary",
    "render_stats",
]
