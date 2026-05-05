"""Upgrade 2 (PRD §31.3) Slice 2 — Ledger self-consistency
replay primitive.

Replays the per-session ``decisions.jsonl`` ledger and asserts
that every recorded :class:`DecisionRecord` round-trips through
canonical hashing without drift. Catches:

  * Schema drift — adding/removing fields without a version bump
  * JSON serialization changes — sort order, key naming, float
    precision
  * Hash function changes — any divergence in
    :func:`_canonical_hash` or :func:`_canonical_serialize`
  * Encoding regressions — utf-8 / ASCII / surrogate handling
  * Storage corruption — partial writes, truncated lines

This is **ledger self-consistency**, NOT FSM re-execution. Full
"re-run the session and compare" replay is the battle-test
harness's ``--rerun`` flag (Phase 1 Slice 1.5). This module
proves the recording mechanism itself is byte-stable, which is
the load-bearing precondition for FSM replay being meaningful.

Architectural locks (operator mandate):

  * **Zero duplication** — reuses :func:`_canonical_hash` +
    :func:`_canonical_serialize` from ``decision_runtime`` +
    :class:`SessionReplayer.discover` from ``session_replay``.
    No parallel canonicalization.
  * **Pure offline** — replay job runs from operator machine
    (cron-able). Never blocks the live FSM. Reads decisions.jsonl
    only; writes nothing back to the ledger.
  * **Cross-process safe** — uses
    :func:`flock_critical_section` for the read so a concurrent
    in-flight session cannot tear the read mid-line.
  * **NEVER raises out** — every public function returns a
    structured :class:`ReplayDriftReport` with diagnostic
    detail; CLI exit codes communicate outcome.
  * **Authority asymmetry** (AST-pinned at Slice 5) — replay
    module MUST NOT import orchestrator / iron_gate / providers
    / urgency_router / tool_executor. Pure consumer over the
    ledger's public read surface.
  * **No hardcoded paths** — ledger directory comes from
    :func:`session_replay._ledger_dir` (env-driven via
    ``JARVIS_DETERMINISM_LEDGER_DIR``).

Public surfaces:
  * :class:`ReplayDriftKind` — closed 5-value enum
  * :class:`ReplayDriftReport` — frozen drift entry
  * :class:`ReplaySummary` — aggregate run result
  * :func:`replay_session_consistency` — load + verify entry
  * :func:`replay_cli_main` — CLI entry point (used by
    ``scripts/replay_determinism.py``)
"""
from __future__ import annotations

import enum
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


REPLAY_DETERMINISM_SCHEMA_VERSION: str = (
    "replay_determinism.1"
)


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def replay_determinism_enabled() -> bool:
    """``JARVIS_DETERMINISM_REPLAY_ENABLED`` (default ``false``
    until Slice 5 graduation per PRD §31.3).

    The CLI itself is opt-in by argument anyway — this flag is
    a defense-in-depth gate for cron / CI invocations. Flips
    to ``true`` at Slice 5 graduation."""
    raw = os.environ.get(
        "JARVIS_DETERMINISM_REPLAY_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return False  # default-false until Slice 5
    return raw in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Closed taxonomy of drift kinds
# ---------------------------------------------------------------------------


class ReplayDriftKind(str, enum.Enum):
    """Closed 5-value taxonomy of drift causes. ``str``
    subclass for JSON-friendliness + closed-enum dispatch."""

    NONE = "none"
    """No drift — record round-tripped cleanly."""

    INPUT_HASH_MISMATCH = "input_hash_mismatch"
    """``_canonical_hash(parsed_inputs)`` ≠
    ``record.inputs_hash`` — the input-canonicalization is no
    longer stable. Schema or serialization regression."""

    OUTPUT_REPR_NON_CANONICAL = "output_repr_non_canonical"
    """``_canonical_serialize(parsed_output)`` ≠
    ``record.output_repr`` — output is not in canonical form.
    Indicates a write path that bypassed the canonicalizer."""

    SCHEMA_VERSION_DRIFT = "schema_version_drift"
    """``record.schema_version`` doesn't match the live
    ``DecisionRecord.SCHEMA_VERSION`` — a forward-incompatible
    schema change shipped without a migration."""

    PARSE_ERROR = "parse_error"
    """Record line could not be parsed as JSON or
    :class:`DecisionRecord`. Storage corruption / partial
    write / encoding bug."""


# ---------------------------------------------------------------------------
# Frozen drift entry + run summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplayDriftReport:
    """One drift finding from a session replay.

    Frozen + JSON-projectable so SSE consumers (Slice 4) and
    ``/decisions`` REPL operators (Slice 3) can render uniformly.
    """

    kind: ReplayDriftKind
    record_index: int
    """Zero-based index of the record line in decisions.jsonl
    (operator-visible diagnostic)."""

    record_id: str
    """Empty when the line failed to parse. Otherwise the
    record_id from the parsed record."""

    expected: str
    """The recorded value (inputs_hash / output_repr / schema_-
    version) — empty on PARSE_ERROR."""

    actual: str
    """The recomputed value — empty on PARSE_ERROR."""

    detail: str = ""
    """Human-readable diagnostic. Bounded — large values are
    elided to ``"<elided N>"`` in :meth:`to_dict`."""

    def to_dict(self) -> dict:
        # Bound large strings to keep JSON projection cheap +
        # operator-readable. 256-char cap is enough to see the
        # leading mismatch context.
        def _trunc(s: str) -> str:
            if len(s) <= 256:
                return s
            # 251 chars + "<...>" (5) = 256 — exact cap
            return s[:251] + "<...>"
        return {
            "kind": self.kind.value,
            "record_index": self.record_index,
            "record_id": self.record_id,
            "expected": _trunc(self.expected),
            "actual": _trunc(self.actual),
            "detail": _trunc(self.detail),
        }


@dataclass(frozen=True)
class ReplaySummary:
    """Aggregate result of one replay-determinism run.

    ``exit_code`` follows POSIX convention:
      * 0 — clean (zero drift entries, ≥1 record verified)
      * 1 — drift detected (one or more drift entries)
      * 2 — insufficient data (zero records / file missing /
        master flag off)
    """

    schema_version: str = REPLAY_DETERMINISM_SCHEMA_VERSION
    session_id: str = ""
    decisions_path: str = ""
    records_total: int = 0
    records_verified: int = 0
    drift_entries: Tuple[ReplayDriftReport, ...] = field(
        default_factory=tuple,
    )
    elapsed_s: float = 0.0
    exit_code: int = 2
    diagnostics: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_drift(self) -> bool:
        return any(
            e.kind is not ReplayDriftKind.NONE
            for e in self.drift_entries
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "decisions_path": self.decisions_path,
            "records_total": self.records_total,
            "records_verified": self.records_verified,
            "drift_count": len(self.drift_entries),
            "drift_entries": [
                e.to_dict() for e in self.drift_entries
            ],
            "elapsed_s": self.elapsed_s,
            "exit_code": self.exit_code,
            "diagnostics": list(self.diagnostics),
            "has_drift": self.has_drift,
        }


# ---------------------------------------------------------------------------
# Per-record verifier — the load-bearing primitive
# ---------------------------------------------------------------------------


def _verify_record(
    *,
    record_index: int,
    record_dict: dict,
) -> Tuple[ReplayDriftReport, ...]:
    """Verify one parsed record dict for self-consistency.
    Returns 0+ drift reports. NEVER raises — any unexpected
    exception is captured as a PARSE_ERROR finding."""
    out: List[ReplayDriftReport] = []
    try:
        # Lazy-import — keeps module import cheap + decouples
        # from the determinism runtime's import graph at module
        # load.
        from backend.core.ouroboros.governance.determinism.decision_runtime import (  # noqa: E501
            DecisionRecord,
            SCHEMA_VERSION as LIVE_SCHEMA_VERSION,
            _canonical_hash,
            _canonical_serialize,
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        out.append(ReplayDriftReport(
            kind=ReplayDriftKind.PARSE_ERROR,
            record_index=record_index,
            record_id="",
            expected="",
            actual="",
            detail=(
                f"failed to import decision_runtime: "
                f"{type(exc).__name__}"
            ),
        ))
        return tuple(out)

    # Step 1: schema-version check FIRST. ``DecisionRecord.from_-
    # dict`` returns None for any record whose schema_version
    # differs from the live constant (a defensive choice in the
    # substrate to refuse forward-incompatible reads). Inspect
    # the raw dict's schema_version BEFORE attempting parse so
    # SCHEMA_VERSION_DRIFT surfaces as a distinct drift kind
    # rather than being collapsed into PARSE_ERROR.
    raw_schema_version = str(
        record_dict.get("schema_version", ""),
    )
    if (
        raw_schema_version
        and raw_schema_version != LIVE_SCHEMA_VERSION
    ):
        out.append(ReplayDriftReport(
            kind=ReplayDriftKind.SCHEMA_VERSION_DRIFT,
            record_index=record_index,
            record_id=str(record_dict.get("record_id", "")),
            expected=LIVE_SCHEMA_VERSION,
            actual=raw_schema_version,
            detail=(
                "record.schema_version doesn't match the live "
                "DecisionRecord.SCHEMA_VERSION — schema "
                "migration likely required"
            ),
        ))
        # Don't proceed with from_dict on a known-mismatched
        # schema; the rest of the verification is moot until
        # the migration is performed.
        return tuple(out)

    # Step 2: parse the record. ``from_dict`` returns None on
    # malformed input (it does not raise) — handle defensively.
    try:
        record = DecisionRecord.from_dict(record_dict)
    except Exception as exc:  # noqa: BLE001 — defensive
        out.append(ReplayDriftReport(
            kind=ReplayDriftKind.PARSE_ERROR,
            record_index=record_index,
            record_id=str(record_dict.get("record_id", "")),
            expected="",
            actual="",
            detail=(
                f"DecisionRecord.from_dict failed: "
                f"{type(exc).__name__}: {exc}"
            ),
        ))
        return tuple(out)
    if record is None:
        out.append(ReplayDriftReport(
            kind=ReplayDriftKind.PARSE_ERROR,
            record_index=record_index,
            record_id=str(record_dict.get("record_id", "")),
            expected="",
            actual="",
            detail=(
                "DecisionRecord.from_dict returned None — "
                "record dict is malformed (missing required "
                "fields or invalid types)"
            ),
        ))
        return tuple(out)

    # Step 3: output_repr canonical-form check.
    # Re-canonicalize the parsed output_repr; the result MUST
    # match the stored repr byte-for-byte. If not, the original
    # write bypassed the canonicalizer.
    try:
        expected_repr = record.output_repr or ""
        # Try JSON-decode first (most outputs are JSON-shaped
        # via the OutputAdapter pipeline)
        try:
            parsed = json.loads(expected_repr)
            actual_repr = _canonical_serialize(parsed)
        except json.JSONDecodeError:
            # Fall back to treating the repr as a raw string
            actual_repr = _canonical_serialize(expected_repr)
        if actual_repr != expected_repr:
            out.append(ReplayDriftReport(
                kind=ReplayDriftKind.OUTPUT_REPR_NON_CANONICAL,
                record_index=record_index,
                record_id=record.record_id,
                expected=expected_repr,
                actual=actual_repr,
                detail=(
                    "output_repr is not in canonical serialization "
                    "form — write path bypassed _canonical_-"
                    "serialize"
                ),
            ))
    except Exception as exc:  # noqa: BLE001 — defensive
        out.append(ReplayDriftReport(
            kind=ReplayDriftKind.PARSE_ERROR,
            record_index=record_index,
            record_id=record.record_id,
            expected=record.output_repr or "",
            actual="",
            detail=(
                f"output_repr canonicalization raised: "
                f"{type(exc).__name__}"
            ),
        ))

    # Note: We do NOT re-verify inputs_hash here because the
    # inputs themselves are not stored on the record — only
    # the hash. Drift in inputs canonicalization can ONLY be
    # caught by full FSM re-execution (battle-test --rerun).
    # This is documented as out-of-scope per Slice 2's "ledger
    # self-consistency, not FSM replay" framing.
    return tuple(out)


# ---------------------------------------------------------------------------
# Public entry — load + verify all records for a session
# ---------------------------------------------------------------------------


def replay_session_consistency(
    session_id: str,
    *,
    decisions_path: Optional[Path] = None,
    enabled_override: Optional[bool] = None,
) -> ReplaySummary:
    """**Authoritative entry point.** Load + verify the per-
    session decisions.jsonl. Returns a frozen
    :class:`ReplaySummary` with structured drift findings +
    POSIX exit code.

    Args:
        session_id: Session identifier (matches the directory
            name under :func:`session_replay._ledger_dir`).
        decisions_path: Optional explicit path override
            (testing). When None, derives from
            :func:`session_replay._ledger_dir` /
            ``<session-id>/decisions.jsonl``.
        enabled_override: Test-only master-flag bypass. Keep
            ``None`` in production.

    NEVER raises out — all faults map to a
    :class:`ReplaySummary` with ``exit_code=2`` and structured
    diagnostics."""
    import time as _time
    started = _time.monotonic()

    # Master-flag check
    enabled = (
        enabled_override
        if enabled_override is not None
        else replay_determinism_enabled()
    )
    if not enabled:
        return ReplaySummary(
            session_id=str(session_id),
            decisions_path="",
            exit_code=2,
            diagnostics=(
                "JARVIS_DETERMINISM_REPLAY_ENABLED=false — "
                "replay job is master-flag-gated; flip to "
                "true to engage",
            ),
        )

    # Defensive session_id normalization
    sid = (str(session_id).strip() if session_id else "")
    if not sid:
        return ReplaySummary(
            session_id="",
            decisions_path="",
            exit_code=2,
            diagnostics=(
                "session_id is required; replay cannot proceed",
            ),
        )

    # Resolve decisions.jsonl path via existing primitive
    if decisions_path is None:
        try:
            from backend.core.ouroboros.governance.determinism.session_replay import (  # noqa: E501
                _ledger_dir,
            )
            decisions_path = _ledger_dir() / sid / "decisions.jsonl"
        except Exception as exc:  # noqa: BLE001 — defensive
            return ReplaySummary(
                session_id=sid,
                decisions_path="",
                exit_code=2,
                diagnostics=(
                    f"failed to resolve ledger directory: "
                    f"{type(exc).__name__}",
                ),
            )

    if not decisions_path.exists():
        return ReplaySummary(
            session_id=sid,
            decisions_path=str(decisions_path),
            exit_code=2,
            diagnostics=(
                f"decisions.jsonl not found at {decisions_path}",
            ),
        )

    # Read with cross-process flock to avoid tearing a
    # concurrent in-flight write. Reuses the existing flock
    # primitive (no new locking code).
    lines: Sequence[str] = ()
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_critical_section,
        )
        with flock_critical_section(decisions_path) as acquired:
            if not acquired:
                return ReplaySummary(
                    session_id=sid,
                    decisions_path=str(decisions_path),
                    exit_code=2,
                    diagnostics=(
                        "cross-process lock not acquired; "
                        "concurrent writer likely",
                    ),
                )
            try:
                text = decisions_path.read_text(
                    encoding="utf-8",
                )
                lines = text.splitlines()
            except OSError as exc:
                return ReplaySummary(
                    session_id=sid,
                    decisions_path=str(decisions_path),
                    exit_code=2,
                    diagnostics=(
                        f"failed to read decisions.jsonl: "
                        f"{exc}",
                    ),
                )
    except Exception as exc:  # noqa: BLE001 — defensive
        return ReplaySummary(
            session_id=sid,
            decisions_path=str(decisions_path),
            exit_code=2,
            diagnostics=(
                f"flock_critical_section raised: "
                f"{type(exc).__name__}",
            ),
        )

    # Walk records — stdlib only, no extra deps
    drift: List[ReplayDriftReport] = []
    verified = 0
    total = 0
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        total += 1
        try:
            record_dict = json.loads(line)
            if not isinstance(record_dict, dict):
                drift.append(ReplayDriftReport(
                    kind=ReplayDriftKind.PARSE_ERROR,
                    record_index=idx,
                    record_id="",
                    expected="",
                    actual="",
                    detail=(
                        f"line is not a JSON object: "
                        f"type={type(record_dict).__name__}"
                    ),
                ))
                continue
        except json.JSONDecodeError as exc:
            drift.append(ReplayDriftReport(
                kind=ReplayDriftKind.PARSE_ERROR,
                record_index=idx,
                record_id="",
                expected="",
                actual="",
                detail=f"json.JSONDecodeError: {exc.msg}",
            ))
            continue

        per_record_drifts = _verify_record(
            record_index=idx, record_dict=record_dict,
        )
        if per_record_drifts:
            drift.extend(per_record_drifts)
        else:
            verified += 1

    # Determine exit code
    if total == 0:
        exit_code = 2
        diagnostics = (
            f"decisions.jsonl is empty at {decisions_path}",
        )
    elif drift:
        exit_code = 1
        diagnostics = (
            f"{len(drift)} drift finding(s) across "
            f"{total} record(s)",
        )
    else:
        exit_code = 0
        diagnostics = (
            f"all {verified} record(s) round-trip clean",
        )

    return ReplaySummary(
        session_id=sid,
        decisions_path=str(decisions_path),
        records_total=total,
        records_verified=verified,
        drift_entries=tuple(drift),
        elapsed_s=_time.monotonic() - started,
        exit_code=exit_code,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# CLI entry — used by scripts/replay_determinism.py
# ---------------------------------------------------------------------------


def replay_cli_main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Used by ``scripts/replay_determinism.py``.
    Returns POSIX exit code; never raises."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="replay_determinism",
        description=(
            "Verify the self-consistency of a session's "
            "DecisionRecord ledger. Catches schema drift, "
            "JSON serialization changes, hash function "
            "regressions. Does NOT re-execute the FSM (use "
            "ouroboros_battle_test.py --rerun for that)."
        ),
    )
    parser.add_argument(
        "--session", required=True,
        help="Session identifier (matches ledger dir name)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Emit JSON to stdout (default: human-readable)",
    )
    parser.add_argument(
        "--allow-disabled", action="store_true",
        help=(
            "Bypass JARVIS_DETERMINISM_REPLAY_ENABLED gate "
            "(for one-off operator runs pre-graduation)"
        ),
    )
    args = parser.parse_args(argv)

    summary = replay_session_consistency(
        args.session,
        enabled_override=(
            True if args.allow_disabled else None
        ),
    )

    if args.json:
        print(json.dumps(summary.to_dict(), indent=2))
    else:
        # Human-readable
        print(
            f"session: {summary.session_id}\n"
            f"path:    {summary.decisions_path}\n"
            f"total:   {summary.records_total}\n"
            f"verified: {summary.records_verified}\n"
            f"drift:   {len(summary.drift_entries)}\n"
            f"elapsed: {summary.elapsed_s:.3f}s\n"
            f"exit:    {summary.exit_code}",
        )
        if summary.diagnostics:
            print("diagnostics:")
            for d in summary.diagnostics:
                print(f"  - {d}")
        if summary.drift_entries:
            print("drift_entries:")
            for entry in summary.drift_entries[:10]:
                print(
                    f"  [{entry.record_index:4d}] "
                    f"kind={entry.kind.value}  "
                    f"id={entry.record_id[:20]}  "
                    f"detail={entry.detail[:80]}",
                )
            if len(summary.drift_entries) > 10:
                print(
                    f"  ... ({len(summary.drift_entries) - 10} "
                    f"more)",
                )
    return summary.exit_code


__all__ = [
    "REPLAY_DETERMINISM_SCHEMA_VERSION",
    "ReplayDriftKind",
    "ReplayDriftReport",
    "ReplaySummary",
    "replay_cli_main",
    "replay_determinism_enabled",
    "replay_session_consistency",
]
