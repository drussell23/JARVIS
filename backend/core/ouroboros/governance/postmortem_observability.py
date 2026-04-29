"""Priority D Slice D1 — Postmortem ledger discoverability surfaces.

Per OUROBOROS_VENOM_PRD.md §25.5.4. Closes the operator-visible
surface for the determinism-ledger postmortem records produced by
Slice 2.4 (verification_postmortem, COMPLETE-phase happy path) and
Option E (terminal_postmortem, every non-COMPLETE termination).

Pre-D, soak #4 produced 101 property_claim records + 127
terminal_postmortem records — and an operator without code access
had no way to find them. The ledger lived at
``.jarvis/determinism/<session>/decisions.jsonl`` with no surface
exposing it. This module ships:

  * ``/postmortems`` REPL dispatcher (5 subcommands).
  * 4 IDE GET endpoints under ``/observability/postmortems``.
  * SSE event ``terminal_postmortem_persisted`` (added to broker
    allow-list in ide_observability_stream).
  * Distribution view that surfaces the ``total_claims=0`` anomaly
    directly — the single operator-visible signal that Phase 2 has
    silently regressed.

Design discipline: this module READS the ledger; it never writes.
The terminal_postmortem records are produced by Option E in
phase_dispatcher.py; the verification_postmortem records by
COMPLETERunner. This module surfaces them only.

Authority invariants (PRD §12.2):
  * No imports of orchestrator / policy / iron_gate / risk_tier /
    change_engine / candidate_generator / gate / semantic_guardian.
  * Allowed: verification.* (own slice family — read VerificationPostmortem)
    + ide_observability_stream (broker for SSE publish).
  * Allowed I/O: read-only of the per-session JSONL decisions.jsonl.
    No subprocess / env mutation / network. Writes are forbidden —
    this module can ONLY observe what other slices already wrote.
  * Best-effort throughout — every reader / publisher call wrapped
    in ``try / except``; failures NEVER raise into callers.
  * ASCII-strict rendering (encode/decode round-trip pin).
  * Surfaces gate on ``JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED`` —
    default ``true`` (graduated default-on with hot-revert).

Mirrors the P5 adversarial_observability shape so all observability
surfaces in O+V have the same ergonomics.
"""
from __future__ import annotations

import enum
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# Cap on rendered REPL output bytes per call. Mirrors P4/P5 REPL clip.
MAX_RENDERED_BYTES: int = 16 * 1024  # 16 KiB

# Read cap for the JSONL ledger. Postmortems are typically <1KB each
# and a long session might accumulate hundreds. The cap keeps the
# REPL + GET surfaces bounded under sustained load.
MAX_LINES_READ: int = 8_192

# Default record count for `/postmortems recent` with no arg + IDE
# GET history endpoint.
HISTORY_DEFAULT_N: int = 10
HISTORY_MAX_N: int = MAX_LINES_READ

# Schema version stamped into IDE GET responses so clients can pin a
# parser version. Independent of the underlying ledger schema.
POSTMORTEM_OBSERVABILITY_SCHEMA_VERSION: str = "1.0"

# SSE event type — added to ide_observability_stream broker
# _VALID_EVENT_TYPES allowlist by Slice D2 wiring.
EVENT_TYPE_TERMINAL_POSTMORTEM_PERSISTED: str = "terminal_postmortem_persisted"

# Op-id grammar — same character class as the existing
# ``_SESSION_ID_RE`` in ide_observability + the metrics observability
# session_id regex so all /observability/* surfaces accept the same
# id shapes.
_OP_ID_RE = re.compile(r"^[A-Za-z0-9_\-:.]{1,128}$")

# Ledger row kinds we surface. Two kinds: COMPLETE-phase happy-path
# (verification_postmortem) + Option E universal terminal postmortem
# (terminal_postmortem).
_LEDGER_KINDS: Tuple[str, ...] = (
    "verification_postmortem", "terminal_postmortem",
)


# ---------------------------------------------------------------------------
# Master flag — graduated default-true with hot-revert
# ---------------------------------------------------------------------------


def postmortem_observability_enabled() -> bool:
    """``JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED`` (default ``true``
    — Slice D1 ships graduated since the surface is read-only and
    the underlying ledger already exists). Hot-revert path: ``export
    JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED=false`` returns:

      * REPL renders ``"(disabled)"`` for operational subcommands;
        ``help`` still works (discoverability).
      * GET endpoints return 403 with ``reason_code=ide_observability
        .disabled``.
      * SSE publisher returns None silently.
    """
    raw = os.environ.get(
        "JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Status enum + result dataclass (REPL)
# ---------------------------------------------------------------------------


class PostmortemReplStatus(str, enum.Enum):
    OK = "OK"
    EMPTY = "EMPTY"
    UNKNOWN_SUBCOMMAND = "UNKNOWN_SUBCOMMAND"
    UNKNOWN_OP = "UNKNOWN_OP"
    BAD_LIMIT = "BAD_LIMIT"
    READ_ERROR = "READ_ERROR"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class PostmortemReplResult:
    status: PostmortemReplStatus
    rendered_text: str
    record: Optional[Dict[str, Any]] = None
    notes: tuple = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Ledger reader (read-only, best-effort)
# ---------------------------------------------------------------------------


def _ledger_path_for_session(
    session_id: Optional[str] = None,
) -> Path:
    """Resolve the per-session decisions.jsonl path. Mirrors the
    Slice 1.3 / 2.3 / 2.4 reader convention. NEVER raises."""
    sid = (str(session_id).strip() if session_id else "")
    if not sid:
        sid = os.environ.get(
            "OUROBOROS_BATTLE_SESSION_ID", "",
        ).strip() or "default"
    base = os.environ.get(
        "JARVIS_DETERMINISM_LEDGER_DIR",
        ".jarvis/determinism",
    ).strip()
    return Path(base) / sid / "decisions.jsonl"


def _read_postmortem_rows(
    path: Optional[Path] = None,
    *,
    limit: int = MAX_LINES_READ,
    session_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read parsed rows from the determinism ledger filtered to
    postmortem kinds. Best-effort:

      * missing file → []
      * OSError → []
      * malformed JSON lines silently dropped (concurrent-writer
        truncation tolerance)
      * unparseable output_repr silently dropped (corrupt record)

    Each returned row is a ``Dict[str, Any]`` with the original
    ledger fields PLUS a ``"postmortem"`` key carrying the parsed
    output_repr (the actual VerificationPostmortem dict + Option E's
    ``_terminal_context`` block). Row order is insertion order
    (newest-last). Caller provides ``limit`` to cap the tail."""
    p = path or _ledger_path_for_session(session_id)
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning(
            "[PostmortemObservability] ledger read failed at %s: %s",
            p, exc,
        )
        return []
    cap = max(1, min(int(limit), MAX_LINES_READ))
    matched: List[Dict[str, Any]] = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            continue  # truncated mid-write — drop
        if not isinstance(row, dict):
            continue
        if row.get("kind") not in _LEDGER_KINDS:
            continue
        # Parse output_repr into a typed dict so consumers don't
        # need to JSON-parse twice.
        out = row.get("output_repr")
        if isinstance(out, str):
            try:
                row["postmortem"] = json.loads(out)
            except json.JSONDecodeError:
                continue  # corrupt — drop
        elif isinstance(out, Mapping):
            row["postmortem"] = dict(out)
        else:
            continue
        matched.append(row)
    if len(matched) > cap:
        matched = matched[-cap:]
    return matched


# ---------------------------------------------------------------------------
# Distribution + stats — pure aggregators (testable without ledger)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostmortemDistribution:
    """Aggregate over a window of postmortems — terminal_phase +
    reason histograms + the empty-claim signature that motivated
    Priority B's MetaSensor.
    """

    total: int = 0
    empty_claim_count: int = 0
    empty_claim_rate: float = 0.0
    must_hold_failed_count: int = 0
    has_blocking_count: int = 0
    terminal_phase_histogram: Dict[str, int] = field(default_factory=dict)
    reason_histogram: Dict[str, int] = field(default_factory=dict)
    kind_histogram: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "empty_claim_count": self.empty_claim_count,
            "empty_claim_rate": round(self.empty_claim_rate, 4),
            "must_hold_failed_count": self.must_hold_failed_count,
            "has_blocking_count": self.has_blocking_count,
            "terminal_phase_histogram": dict(self.terminal_phase_histogram),
            "reason_histogram": dict(self.reason_histogram),
            "kind_histogram": dict(self.kind_histogram),
        }


def compute_distribution(
    rows: List[Dict[str, Any]],
) -> PostmortemDistribution:
    """Pure aggregator over already-read rows. NEVER raises —
    per-row defects are silently skipped."""
    total = 0
    empty_claim_count = 0
    must_hold_failed_count = 0
    has_blocking_count = 0
    terminal_phase_hist: Dict[str, int] = {}
    reason_hist: Dict[str, int] = {}
    kind_hist: Dict[str, int] = {}

    for r in rows:
        if not isinstance(r, dict):
            continue
        total += 1
        kind = str(r.get("kind") or "unknown")
        kind_hist[kind] = kind_hist.get(kind, 0) + 1
        pm = r.get("postmortem") or {}
        if not isinstance(pm, dict):
            continue
        try:
            tc = int(pm.get("total_claims") or 0)
        except (TypeError, ValueError):
            tc = 0
        if tc == 0:
            empty_claim_count += 1
        try:
            mhf = int(pm.get("must_hold_failed") or 0)
        except (TypeError, ValueError):
            mhf = 0
        must_hold_failed_count += mhf
        if pm.get("has_blocking_failures"):
            has_blocking_count += 1
        # Pull terminal_phase + reason from Option E's enrichment
        # block. verification_postmortem records (Slice 2.4 happy
        # path) lack this block — they implicitly have
        # terminal_phase="COMPLETE" + reason="planned".
        ctx = pm.get("_terminal_context") or {}
        if isinstance(ctx, Mapping):
            tp = str(ctx.get("terminal_phase") or "").strip() or "COMPLETE"
            rsn = str(ctx.get("reason") or "").strip() or "planned"
        else:
            tp = "COMPLETE"
            rsn = "planned"
        terminal_phase_hist[tp] = terminal_phase_hist.get(tp, 0) + 1
        # Truncate the reason for histogram bucketing (long error
        # messages would make a noisy histogram).
        rsn_bucket = rsn[:80]
        reason_hist[rsn_bucket] = reason_hist.get(rsn_bucket, 0) + 1

    rate = (
        empty_claim_count / total if total > 0 else 0.0
    )
    return PostmortemDistribution(
        total=total,
        empty_claim_count=empty_claim_count,
        empty_claim_rate=rate,
        must_hold_failed_count=must_hold_failed_count,
        has_blocking_count=has_blocking_count,
        terminal_phase_histogram=terminal_phase_hist,
        reason_histogram=reason_hist,
        kind_histogram=kind_hist,
    )


# ---------------------------------------------------------------------------
# Renderers (ASCII-strict)
# ---------------------------------------------------------------------------


def _ascii_clip(text: str) -> str:
    """Clip to MAX_RENDERED_BYTES + ASCII-encode round-trip so the
    REPL never emits non-ASCII. Mirrors P4/P5 surface convention."""
    try:
        encoded = text.encode("ascii", errors="replace")
    except Exception:  # noqa: BLE001
        encoded = text.encode("utf-8", errors="replace")
    if len(encoded) > MAX_RENDERED_BYTES:
        encoded = encoded[:MAX_RENDERED_BYTES] + b"\n... (clipped)"
    return encoded.decode("ascii", errors="replace")


def render_help() -> str:
    return _ascii_clip("\n".join([
        "[postmortems] /postmortems subcommands:",
        "  /postmortems recent [N]            last N postmortems "
        "(default 10, max 8192)",
        "  /postmortems for-op <op-id>        show one postmortem by op_id",
        "  /postmortems distribution          terminal_phase + reason "
        "histogram + empty-claim rate",
        "  /postmortems stats                 alias for distribution",
        "  /postmortems help                  this listing",
    ]))


def render_postmortem_summary(row: Dict[str, Any]) -> str:
    op_id = str(row.get("op_id") or "?")
    kind = str(row.get("kind") or "?")
    phase = str(row.get("phase") or "?")
    pm = row.get("postmortem") or {}
    tc = int(pm.get("total_claims") or 0) if isinstance(pm, Mapping) else 0
    mhc = int(pm.get("must_hold_count") or 0) if isinstance(pm, Mapping) else 0
    mhf = int(pm.get("must_hold_failed") or 0) if isinstance(pm, Mapping) else 0
    insuf = int(pm.get("insufficient_count") or 0) if isinstance(pm, Mapping) else 0
    blocking = bool(pm.get("has_blocking_failures") or False) if isinstance(pm, Mapping) else False
    ctx = pm.get("_terminal_context") if isinstance(pm, Mapping) else None
    reason = ""
    if isinstance(ctx, Mapping):
        reason = str(ctx.get("reason") or "")[:80]
    lines = [
        f"[postmortem] op={op_id}",
        f"  kind={kind} phase={phase}",
        f"  claims: total={tc} must_hold={mhc} failed={mhf} insufficient={insuf}",
        f"  blocking={blocking}",
    ]
    if reason:
        lines.append(f"  reason: {reason}")
    return _ascii_clip("\n".join(lines))


def render_postmortem_detail(row: Dict[str, Any]) -> str:
    op_id = str(row.get("op_id") or "?")
    kind = str(row.get("kind") or "?")
    phase = str(row.get("phase") or "?")
    pm = row.get("postmortem") or {}
    if not isinstance(pm, Mapping):
        return _ascii_clip(f"[postmortem] op={op_id} (corrupt record)")
    lines = [
        f"[postmortem] for op={op_id}",
        f"  kind={kind} phase={phase}",
    ]
    ctx = pm.get("_terminal_context")
    if isinstance(ctx, Mapping):
        tp = str(ctx.get("terminal_phase") or "?")
        st = str(ctx.get("status") or "?")
        rsn = str(ctx.get("reason") or "")[:200]
        lines.append(f"  terminal: phase={tp} status={st}")
        if rsn:
            lines.append(f"  reason: {rsn}")
    tc = int(pm.get("total_claims") or 0)
    mhc = int(pm.get("must_hold_count") or 0)
    mhf = int(pm.get("must_hold_failed") or 0)
    insuf = int(pm.get("insufficient_count") or 0)
    err = int(pm.get("error_count") or 0)
    lines.append(
        f"  claims: total={tc} must_hold={mhc} failed={mhf} "
        f"insufficient={insuf} errors={err}"
    )
    outcomes = pm.get("outcomes") or []
    if isinstance(outcomes, list) and outcomes:
        lines.append(f"  outcomes:")
        for i, oc in enumerate(outcomes[:8], 1):
            if not isinstance(oc, Mapping):
                continue
            claim = oc.get("claim") or {}
            verdict = oc.get("verdict") or {}
            ckind = str(claim.get("property", {}).get("kind") or "?") \
                if isinstance(claim.get("property"), Mapping) else "?"
            sev = str(claim.get("severity") or "?")
            vstr = str(verdict.get("verdict") or "?")
            reason = str(verdict.get("reason") or "")[:80]
            lines.append(
                f"    {i}. [{sev}] {ckind} -> {vstr}: {reason}"
            )
        if len(outcomes) > 8:
            lines.append(f"    ... and {len(outcomes) - 8} more")
    return _ascii_clip("\n".join(lines))


def render_distribution(dist: PostmortemDistribution) -> str:
    lines = [
        f"[postmortems] distribution over {dist.total} record(s)",
        f"  empty_claim_count: {dist.empty_claim_count} "
        f"({dist.empty_claim_rate:.1%})",
        f"  must_hold_failed: {dist.must_hold_failed_count}",
        f"  has_blocking: {dist.has_blocking_count}",
    ]
    if dist.empty_claim_rate >= 0.7 and dist.total >= 20:
        lines.append(
            f"  WARNING: empty_claim_rate >= 70% — verification "
            f"loop may not be exercising. See MetaSensor "
            f"empty_postmortem_rate detector."
        )
    if dist.kind_histogram:
        lines.append(f"  kinds:")
        for k, v in sorted(
            dist.kind_histogram.items(), key=lambda kv: -kv[1],
        ):
            lines.append(f"    {k}: {v}")
    if dist.terminal_phase_histogram:
        lines.append(f"  terminal_phase:")
        for k, v in sorted(
            dist.terminal_phase_histogram.items(),
            key=lambda kv: -kv[1],
        ):
            lines.append(f"    {k}: {v}")
    if dist.reason_histogram:
        lines.append(f"  top reasons (truncated to 8):")
        ranked = sorted(
            dist.reason_histogram.items(), key=lambda kv: -kv[1],
        )[:8]
        for k, v in ranked:
            lines.append(f"    {k}: {v}")
    return _ascii_clip("\n".join(lines))


# ---------------------------------------------------------------------------
# REPL dispatcher
# ---------------------------------------------------------------------------


def dispatch_postmortems_command(
    argv: List[str],
    *,
    ledger_path: Optional[Path] = None,
    session_id: Optional[str] = None,
) -> PostmortemReplResult:
    """Dispatch a ``/postmortems`` REPL command. NEVER raises.

    Subcommands:
      * ``recent [N]`` — render summaries of last N postmortems
      * ``for-op <op_id>`` — render one detail by op_id
      * ``distribution`` / ``stats`` — render aggregate
      * ``help`` — usage listing

    ``help`` bypasses the master-flag gate (discoverability — operators
    can always learn the subcommand surface). Operational subcommands
    return PostmortemReplStatus.DISABLED when off."""
    if not argv:
        return PostmortemReplResult(
            status=PostmortemReplStatus.UNKNOWN_SUBCOMMAND,
            rendered_text=render_help(),
        )
    sub = str(argv[0]).strip().lower()
    if sub == "help":
        return PostmortemReplResult(
            status=PostmortemReplStatus.OK,
            rendered_text=render_help(),
        )
    if not postmortem_observability_enabled():
        return PostmortemReplResult(
            status=PostmortemReplStatus.DISABLED,
            rendered_text=_ascii_clip(
                "[postmortems] (disabled — set "
                "JARVIS_POSTMORTEM_OBSERVABILITY_ENABLED=true)"
            ),
        )
    if sub == "recent":
        limit = HISTORY_DEFAULT_N
        if len(argv) >= 2:
            try:
                limit = max(
                    1, min(int(argv[1]), HISTORY_MAX_N),
                )
            except (TypeError, ValueError):
                return PostmortemReplResult(
                    status=PostmortemReplStatus.BAD_LIMIT,
                    rendered_text=_ascii_clip(
                        f"[postmortems] bad limit: {argv[1]!r} "
                        f"(must be 1..{HISTORY_MAX_N})"
                    ),
                )
        rows = _read_postmortem_rows(
            ledger_path, limit=limit, session_id=session_id,
        )
        if not rows:
            return PostmortemReplResult(
                status=PostmortemReplStatus.EMPTY,
                rendered_text=_ascii_clip(
                    "[postmortems] (no records — ledger empty or "
                    "session has not produced postmortems yet)"
                ),
            )
        text = "\n\n".join(
            render_postmortem_summary(r) for r in rows
        )
        return PostmortemReplResult(
            status=PostmortemReplStatus.OK,
            rendered_text=_ascii_clip(text),
            notes=(f"rows={len(rows)}",),
        )
    if sub == "for-op":
        if len(argv) < 2 or not _OP_ID_RE.match(str(argv[1])):
            return PostmortemReplResult(
                status=PostmortemReplStatus.UNKNOWN_OP,
                rendered_text=_ascii_clip(
                    f"[postmortems] bad op_id: {argv[1] if len(argv) >= 2 else '<missing>'!r}"
                ),
            )
        op_id = str(argv[1])
        rows = _read_postmortem_rows(
            ledger_path, session_id=session_id,
        )
        match = next(
            (r for r in reversed(rows)
             if isinstance(r, dict) and r.get("op_id") == op_id),
            None,
        )
        if match is None:
            return PostmortemReplResult(
                status=PostmortemReplStatus.UNKNOWN_OP,
                rendered_text=_ascii_clip(
                    f"[postmortems] no record for op_id={op_id!r}"
                ),
            )
        return PostmortemReplResult(
            status=PostmortemReplStatus.OK,
            rendered_text=render_postmortem_detail(match),
            record=match,
        )
    if sub in ("distribution", "stats"):
        rows = _read_postmortem_rows(
            ledger_path, session_id=session_id,
        )
        dist = compute_distribution(rows)
        return PostmortemReplResult(
            status=PostmortemReplStatus.OK,
            rendered_text=render_distribution(dist),
        )
    return PostmortemReplResult(
        status=PostmortemReplStatus.UNKNOWN_SUBCOMMAND,
        rendered_text=render_help(),
    )


# ---------------------------------------------------------------------------
# IDE GET endpoints
# ---------------------------------------------------------------------------


@dataclass
class _PostmortemRoutesHandler:
    ledger_path: Optional[Path] = None
    rate_limit_check: Optional[Callable[[Any], bool]] = None
    cors_headers: Optional[Callable[[Any], Dict[str, str]]] = None

    def _gate_check(self, request: Any) -> Optional[Any]:
        if not postmortem_observability_enabled():
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
                logger.debug(
                    "[PostmortemObservability] rate_limit_check raised",
                    exc_info=True,
                )
        return None

    def _json(
        self, request: Any, status: int, payload: Dict[str, Any],
    ) -> Any:
        from aiohttp import web
        if "schema_version" not in payload:
            payload = {
                "schema_version": POSTMORTEM_OBSERVABILITY_SCHEMA_VERSION,
                **payload,
            }
        resp = web.json_response(payload, status=status)
        if self.cors_headers is not None:
            try:
                for k, v in self.cors_headers(request).items():
                    resp.headers[k] = v
            except Exception:  # noqa: BLE001
                logger.debug(
                    "[PostmortemObservability] cors_headers raised",
                    exc_info=True,
                )
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _error(self, request: Any, status: int, code: str) -> Any:
        return self._json(
            request, status, {"error": True, "reason_code": code},
        )

    async def handle_recent(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        try:
            limit = max(
                1, min(
                    int(request.query.get("limit", str(HISTORY_DEFAULT_N))),
                    HISTORY_MAX_N,
                ),
            )
        except (TypeError, ValueError):
            return self._error(
                request, 400, "ide_observability.malformed_limit",
            )
        try:
            rows = _read_postmortem_rows(
                self.ledger_path, limit=limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[PostmortemObservability] recent read failed: %s", exc,
            )
            return self._json(
                request, 200,
                {"postmortems": [], "reason_code": "read_failed"},
            )
        return self._json(
            request, 200,
            {"postmortems": rows, "rows_seen": len(rows)},
        )

    async def handle_for_op(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        op_id = request.match_info.get("op_id", "")
        if not _OP_ID_RE.match(op_id):
            return self._error(
                request, 400, "ide_observability.bad_op_id",
            )
        try:
            rows = _read_postmortem_rows(self.ledger_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[PostmortemObservability] for-op read failed: %s", exc,
            )
            return self._json(
                request, 200,
                {"postmortem": None, "reason_code": "read_failed"},
            )
        match = next(
            (r for r in reversed(rows)
             if isinstance(r, dict) and r.get("op_id") == op_id),
            None,
        )
        if match is None:
            return self._error(
                request, 404, "ide_observability.postmortem_not_found",
            )
        return self._json(request, 200, {"postmortem": match})

    async def handle_distribution(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        try:
            rows = _read_postmortem_rows(self.ledger_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[PostmortemObservability] distribution read failed: %s",
                exc,
            )
            return self._json(
                request, 200,
                {"distribution": None, "reason_code": "read_failed"},
            )
        return self._json(
            request, 200,
            {"distribution": compute_distribution(rows).to_dict()},
        )

    async def handle_current(self, request: Any) -> Any:
        err = self._gate_check(request)
        if err is not None:
            return err
        try:
            rows = _read_postmortem_rows(self.ledger_path, limit=1)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[PostmortemObservability] current read failed: %s", exc,
            )
            return self._json(
                request, 200,
                {"postmortem": None, "reason_code": "read_failed"},
            )
        if not rows:
            return self._json(request, 200, {"postmortem": None})
        return self._json(request, 200, {"postmortem": rows[-1]})


def register_postmortem_routes(
    app: Any,
    *,
    ledger_path: Optional[Path] = None,
    rate_limit_check: Optional[Callable[[Any], bool]] = None,
    cors_headers: Optional[Callable[[Any], Dict[str, str]]] = None,
) -> None:
    """Mount 4 GET routes on a caller-supplied aiohttp Application.

    Mirrors P4/P5 observability shape: gate check (master flag +
    rate limit) → handler → ``_json`` response with
    ``schema_version`` + ``Cache-Control: no-store`` + CORS.

    Routes:
      * GET /observability/postmortems              — most recent record
      * GET /observability/postmortems/recent       — last N records
      * GET /observability/postmortems/distribution — aggregate
      * GET /observability/postmortems/{op_id}      — drill-down

    ``rate_limit_check=None`` allows all requests (test convenience).
    Production caller (Slice D2 wires this in
    ``EventChannelServer.start``) MUST supply both callables."""
    handler = _PostmortemRoutesHandler(
        ledger_path=ledger_path,
        rate_limit_check=rate_limit_check,
        cors_headers=cors_headers,
    )
    app.router.add_get(
        "/observability/postmortems", handler.handle_current,
    )
    app.router.add_get(
        "/observability/postmortems/recent", handler.handle_recent,
    )
    app.router.add_get(
        "/observability/postmortems/distribution",
        handler.handle_distribution,
    )
    app.router.add_get(
        "/observability/postmortems/{op_id}", handler.handle_for_op,
    )


# ---------------------------------------------------------------------------
# SSE bridge helper
# ---------------------------------------------------------------------------


def publish_terminal_postmortem_persisted(
    *,
    op_id: str,
    record_id: str,
    terminal_phase: str,
    total_claims: int,
    has_blocking_failures: bool,
    reason: str = "",
) -> Optional[str]:
    """Fire the ``terminal_postmortem_persisted`` SSE event after
    Option E persists a record. Best-effort — NEVER raises.

    Returns the broker-assigned event id when published, else None
    (broker missing / disabled / publish raised). Mirrors P5's
    ``publish_adversarial_findings_emitted`` pattern.

    Payload is summary-only — full record lives at the GET endpoint."""
    if not postmortem_observability_enabled():
        return None
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            get_default_broker,
        )
    except Exception:  # noqa: BLE001
        return None
    try:
        broker = get_default_broker()
        return broker.publish(
            event_type=EVENT_TYPE_TERMINAL_POSTMORTEM_PERSISTED,
            op_id=str(op_id),
            payload={
                "op_id": str(op_id),
                "record_id": str(record_id),
                "terminal_phase": str(terminal_phase),
                "total_claims": int(total_claims),
                "has_blocking_failures": bool(has_blocking_failures),
                "reason": str(reason or "")[:200],
                "schema_version": POSTMORTEM_OBSERVABILITY_SCHEMA_VERSION,
                "wall_ts": time.time(),
            },
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "[PostmortemObservability] publish failed", exc_info=True,
        )
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "EVENT_TYPE_TERMINAL_POSTMORTEM_PERSISTED",
    "HISTORY_DEFAULT_N",
    "HISTORY_MAX_N",
    "MAX_LINES_READ",
    "MAX_RENDERED_BYTES",
    "POSTMORTEM_OBSERVABILITY_SCHEMA_VERSION",
    "PostmortemDistribution",
    "PostmortemReplResult",
    "PostmortemReplStatus",
    "compute_distribution",
    "dispatch_postmortems_command",
    "postmortem_observability_enabled",
    "publish_terminal_postmortem_persisted",
    "register_postmortem_routes",
    "render_distribution",
    "render_help",
    "render_postmortem_detail",
    "render_postmortem_summary",
]
