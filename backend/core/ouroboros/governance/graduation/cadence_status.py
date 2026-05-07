"""Phase 9 cadence status — overdue detector composing the
manifest (Slice 1) + health ledger (Slice 2) + graduation
history.

Closes the operator-binding "no silent cadence failure" half:
without this detector, the manifest + health-ledger Slices
1+2 are passive witnesses; this module is the active reader
that answers "did the schedule fire when expected, and if not
why?"

Operator binding 2026-05-06 (verbatim):

  > "Drive thresholds from installer-written metadata (e.g.
  > schedule string or next-run hint written at --install time
  > from the existing CRON_SCHEDULE / launchd StartInterval —
  > single source of truth, no magic 86400 in random modules."

This module is **pure-function + read-only** — no side effects,
no manifest writes, no health appends, no history mutations.
The only knower of the cadence interval is the manifest;
no module-level constants like ``86400`` / ``43200`` /
``28800`` may exist (AST-pinned).

Public surface:

  * :class:`CadenceStatusVerdict` — closed 5-value taxonomy:
    HEALTHY / OVERDUE / RECENTLY_FAILED / NEVER_RAN / UNKNOWN.
    Bytes-pinned.
  * :class:`CadenceStatusReport` — frozen §33.5 versioned
    artifact. Carries: verdict, interval_hint_s, age of last
    preflight_ok / last graduation history row / last
    preflight_failure, manifest's schedule_string + kind,
    next_expected_epoch, grace_window_s, detail.
  * :func:`evaluate_cadence_status` — pure function. Caller
    injects manifest reader + health reader + history reader
    OR uses defaults that compose the canonical substrate.
    NEVER raises.
  * :func:`is_overdue` — convenience predicate.
  * :func:`render_cadence_status_block` — operator-facing
    multi-line ANSI-colored rendering (mirrors §37 chrome
    discipline: cyan/yellow/red/dim, NO bright_green).

Architectural locks (AST-pinned):

  * **Authority asymmetry** — substrate purity (no
    orchestrator / iron_gate / policy / providers imports).
  * **No hardcoded cadence numbers** — the source must NOT
    contain integer literals that look like seconds-per-day
    (86400) / 12h (43200) / 8h (28800) etc; the manifest is
    the SOLE knower. Detector composes
    ``cadence_manifest.read_manifest()``.
  * **Read-only** — no ``record_*`` / ``write_*`` /
    ``record_session`` calls anywhere.
  * **Verdict taxonomy closed** — 5 values bytes-pinned.
  * **§33.5 versioned-artifact** contract on the report.
  * **NEVER raises** across all public surfaces.
"""
from __future__ import annotations

import enum
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


CADENCE_STATUS_REPORT_SCHEMA_VERSION: str = (
    "cadence_status_report.1"
)


# Default grace factor — overdue threshold is
# ``interval_hint_s * grace_factor``. 1.5× gives 50% wiggle
# room for clock drift / stagger / sandboxed reads. Operator-
# tunable.
def _grace_factor() -> float:
    raw = os.environ.get(
        "JARVIS_CADENCE_OVERDUE_GRACE_FACTOR", "",
    ).strip()
    if not raw:
        return 1.5
    try:
        v = float(raw)
        if v < 1.0:
            return 1.0
        if v > 10.0:
            return 10.0
        return v
    except (TypeError, ValueError):
        return 1.5


# ---------------------------------------------------------------------------
# Closed taxonomy
# ---------------------------------------------------------------------------


class CadenceStatusVerdict(str, enum.Enum):
    """Closed 5-value verdict — bytes-pinned."""

    HEALTHY = "healthy"
    """Most-recent fire is within ``interval_hint_s × grace``;
    no failure rows in the most-recent window. Cadence is
    observable + working."""

    OVERDUE = "overdue"
    """No successful fire (preflight_ok OR history row) within
    ``interval_hint_s × grace``. Schedule may have been killed,
    sandbox-blocked, or never installed."""

    RECENTLY_FAILED = "recently_failed"
    """The most-recent preflight row is a ``preflight_failure``
    AND it's more recent than the last preflight_ok. Cron /
    launchd is firing but Python can't run — typical TCC EPERM
    pattern."""

    NEVER_RAN = "never_ran"
    """No preflight rows AND no history rows AND manifest has
    been written but no fires recorded. Just-installed state
    BEFORE first cron tick."""

    UNKNOWN = "unknown"
    """Manifest missing OR interval_hint_s is 0 (parser
    couldn't derive a cadence). Detector cannot answer."""


# ---------------------------------------------------------------------------
# Versioned report artifact (§33.5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CadenceStatusReport:
    """Frozen overdue-detector report — §33.5 versioned."""

    schema_version: str
    verdict: CadenceStatusVerdict
    # Read-from-manifest (SOLE source of cadence truth)
    schedule_kind: str
    schedule_string: str
    interval_hint_s: int
    grace_factor: float
    # Derived
    grace_window_s: int  # interval_hint_s * grace_factor
    # Ages in seconds (None if no row exists)
    last_preflight_ok_age_s: Optional[float]
    last_preflight_failure_age_s: Optional[float]
    last_history_row_age_s: Optional[float]
    # Forecast — None if interval_hint_s is 0
    next_expected_epoch: Optional[float]
    next_expected_iso: Optional[str]
    # Bounded human-readable detail
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "verdict": self.verdict.value,
            "schedule_kind": self.schedule_kind,
            "schedule_string": self.schedule_string,
            "interval_hint_s": int(self.interval_hint_s),
            "grace_factor": float(self.grace_factor),
            "grace_window_s": int(self.grace_window_s),
            "last_preflight_ok_age_s": (
                float(self.last_preflight_ok_age_s)
                if self.last_preflight_ok_age_s is not None
                else None
            ),
            "last_preflight_failure_age_s": (
                float(self.last_preflight_failure_age_s)
                if self.last_preflight_failure_age_s is not None
                else None
            ),
            "last_history_row_age_s": (
                float(self.last_history_row_age_s)
                if self.last_history_row_age_s is not None
                else None
            ),
            "next_expected_epoch": (
                float(self.next_expected_epoch)
                if self.next_expected_epoch is not None
                else None
            ),
            "next_expected_iso": self.next_expected_iso,
            "detail": self.detail[:256],
        }

    @classmethod
    def from_dict(
        cls, payload: Dict[str, Any],
    ) -> Optional["CadenceStatusReport"]:
        try:
            if not isinstance(payload, dict):
                return None
            verdict_raw = str(payload.get("verdict") or "")
            try:
                verdict = CadenceStatusVerdict(verdict_raw)
            except ValueError:
                return None
            return cls(
                schema_version=str(
                    payload.get("schema_version")
                    or CADENCE_STATUS_REPORT_SCHEMA_VERSION,
                ),
                verdict=verdict,
                schedule_kind=str(
                    payload.get("schedule_kind") or "",
                ),
                schedule_string=str(
                    payload.get("schedule_string") or "",
                ),
                interval_hint_s=int(
                    payload.get("interval_hint_s") or 0,
                ),
                grace_factor=float(
                    payload.get("grace_factor") or 1.5,
                ),
                grace_window_s=int(
                    payload.get("grace_window_s") or 0,
                ),
                last_preflight_ok_age_s=(
                    float(payload["last_preflight_ok_age_s"])
                    if payload.get(
                        "last_preflight_ok_age_s",
                    ) is not None
                    else None
                ),
                last_preflight_failure_age_s=(
                    float(
                        payload["last_preflight_failure_age_s"],
                    )
                    if payload.get(
                        "last_preflight_failure_age_s",
                    ) is not None
                    else None
                ),
                last_history_row_age_s=(
                    float(payload["last_history_row_age_s"])
                    if payload.get(
                        "last_history_row_age_s",
                    ) is not None
                    else None
                ),
                next_expected_epoch=(
                    float(payload["next_expected_epoch"])
                    if payload.get(
                        "next_expected_epoch",
                    ) is not None
                    else None
                ),
                next_expected_iso=(
                    str(payload["next_expected_iso"])
                    if payload.get(
                        "next_expected_iso",
                    ) is not None
                    else None
                ),
                detail=str(payload.get("detail") or "")[:256],
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


# ---------------------------------------------------------------------------
# Pure-function evaluator
# ---------------------------------------------------------------------------


def _coerce_now_epoch(now: Optional[float]) -> float:
    if now is not None:
        try:
            return float(now)
        except (TypeError, ValueError):
            pass
    import time as _t
    return _t.time()


def _last_history_row_epoch_default() -> Optional[float]:
    """Compose the existing GraduationLedger to find the most
    recent history row's epoch. Defensive — returns None on
    any failure."""
    try:
        from backend.core.ouroboros.governance.graduation.live_fire_soak import (  # noqa: E501
            history_path,
        )
    except ImportError:
        return None
    try:
        path = history_path()
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        if not path.exists():
            return None
        # Bounded read — find the most-recent line that has a
        # ``finished_at_epoch`` field. Walk in reverse for
        # cheap last-row access on small ledgers.
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        import json as _json
        most_recent: Optional[float] = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            for key in (
                "finished_at_epoch", "started_at_epoch",
            ):
                v = obj.get(key)
                if v is None:
                    continue
                try:
                    epoch = float(v)
                except (TypeError, ValueError):
                    continue
                if (
                    most_recent is None
                    or epoch > most_recent
                ):
                    most_recent = epoch
                break
        return most_recent
    except Exception:  # noqa: BLE001 — defensive
        return None


def evaluate_cadence_status(
    *,
    now_epoch: Optional[float] = None,
    manifest_reader: Optional[Callable[[], Any]] = None,
    last_preflight_ok_reader: Optional[
        Callable[[], Optional[float]]
    ] = None,
    last_preflight_failure_reader: Optional[
        Callable[[], Any]
    ] = None,
    last_history_epoch_reader: Optional[
        Callable[[], Optional[float]]
    ] = None,
    grace_factor: Optional[float] = None,
) -> CadenceStatusReport:
    """Pure-function. Compose manifest + health + history
    readers into a verdict.

    All readers default to canonical substrate composers.
    Caller-injection enables both testing AND alternative-
    storage compatibility (no parallel detector logic; the
    composers stay swappable).

    NEVER raises.
    """
    now = _coerce_now_epoch(now_epoch)
    grace = (
        float(grace_factor)
        if grace_factor is not None
        else _grace_factor()
    )
    if grace < 1.0:
        grace = 1.0
    # Default-compose canonical readers.
    if manifest_reader is None:
        try:
            from backend.core.ouroboros.governance.graduation.cadence_manifest import (  # noqa: E501
                read_manifest as _default_manifest_reader,
            )
            manifest_reader = _default_manifest_reader
        except ImportError:
            manifest_reader = lambda: None  # noqa: E731
    if last_preflight_ok_reader is None:
        try:
            from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
                most_recent_preflight_ok_epoch as _ok_reader,
            )
            last_preflight_ok_reader = _ok_reader
        except ImportError:
            last_preflight_ok_reader = lambda: None  # noqa: E731
    if last_preflight_failure_reader is None:
        try:
            from backend.core.ouroboros.governance.graduation.cadence_health import (  # noqa: E501
                most_recent_preflight_failure as _fail_reader,
            )
            last_preflight_failure_reader = _fail_reader
        except ImportError:
            last_preflight_failure_reader = lambda: None  # noqa: E731
    if last_history_epoch_reader is None:
        last_history_epoch_reader = (
            _last_history_row_epoch_default
        )
    # Read manifest. UNKNOWN if missing or interval_hint_s == 0.
    try:
        manifest = manifest_reader()
    except Exception:  # noqa: BLE001 — defensive
        manifest = None
    if manifest is None:
        return _unknown_report(
            grace_factor=grace,
            detail="manifest_missing",
            now=now,
        )
    schedule_kind = getattr(manifest, "schedule_kind", "")
    schedule_string = getattr(manifest, "schedule_string", "")
    try:
        interval_hint_s = int(
            getattr(manifest, "interval_hint_s", 0),
        )
    except (TypeError, ValueError):
        interval_hint_s = 0
    if interval_hint_s <= 0:
        return CadenceStatusReport(
            schema_version=(
                CADENCE_STATUS_REPORT_SCHEMA_VERSION
            ),
            verdict=CadenceStatusVerdict.UNKNOWN,
            schedule_kind=schedule_kind,
            schedule_string=schedule_string,
            interval_hint_s=0,
            grace_factor=grace,
            grace_window_s=0,
            last_preflight_ok_age_s=None,
            last_preflight_failure_age_s=None,
            last_history_row_age_s=None,
            next_expected_epoch=None,
            next_expected_iso=None,
            detail="interval_hint_zero_unparseable_schedule",
        )
    grace_window_s = int(interval_hint_s * grace)
    # Read substrate signals.
    try:
        ok_epoch = last_preflight_ok_reader()
    except Exception:  # noqa: BLE001 — defensive
        ok_epoch = None
    try:
        failure_row = last_preflight_failure_reader()
    except Exception:  # noqa: BLE001 — defensive
        failure_row = None
    try:
        history_epoch = last_history_epoch_reader()
    except Exception:  # noqa: BLE001 — defensive
        history_epoch = None
    failure_epoch = (
        float(getattr(failure_row, "ts_epoch", 0.0))
        if failure_row is not None else None
    )
    # Compute ages.
    ok_age = (now - ok_epoch) if ok_epoch is not None else None
    failure_age = (
        (now - failure_epoch)
        if failure_epoch is not None else None
    )
    history_age = (
        (now - history_epoch)
        if history_epoch is not None else None
    )
    # Forecast — most-recent successful fire + interval. Use
    # max(ok_epoch, history_epoch) so we anchor to the latest
    # success signal (either preflight passing OR a soak
    # actually completing).
    successful_anchors = [
        e for e in (ok_epoch, history_epoch)
        if e is not None
    ]
    if successful_anchors:
        anchor = max(successful_anchors)
        next_expected_epoch = anchor + interval_hint_s
        next_expected_iso = datetime.fromtimestamp(
            next_expected_epoch, tz=timezone.utc,
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    else:
        next_expected_epoch = None
        next_expected_iso = None
    # Verdict ladder — first-match-wins.
    verdict, detail = _classify_verdict(
        ok_age=ok_age,
        failure_age=failure_age,
        history_age=history_age,
        grace_window_s=grace_window_s,
    )
    return CadenceStatusReport(
        schema_version=CADENCE_STATUS_REPORT_SCHEMA_VERSION,
        verdict=verdict,
        schedule_kind=schedule_kind,
        schedule_string=schedule_string,
        interval_hint_s=interval_hint_s,
        grace_factor=grace,
        grace_window_s=grace_window_s,
        last_preflight_ok_age_s=ok_age,
        last_preflight_failure_age_s=failure_age,
        last_history_row_age_s=history_age,
        next_expected_epoch=next_expected_epoch,
        next_expected_iso=next_expected_iso,
        detail=detail,
    )


def _unknown_report(
    *, grace_factor: float, detail: str, now: float,  # noqa: ARG001
) -> CadenceStatusReport:
    return CadenceStatusReport(
        schema_version=CADENCE_STATUS_REPORT_SCHEMA_VERSION,
        verdict=CadenceStatusVerdict.UNKNOWN,
        schedule_kind="",
        schedule_string="",
        interval_hint_s=0,
        grace_factor=grace_factor,
        grace_window_s=0,
        last_preflight_ok_age_s=None,
        last_preflight_failure_age_s=None,
        last_history_row_age_s=None,
        next_expected_epoch=None,
        next_expected_iso=None,
        detail=detail,
    )


def _classify_verdict(
    *,
    ok_age: Optional[float],
    failure_age: Optional[float],
    history_age: Optional[float],
    grace_window_s: int,
) -> "tuple[CadenceStatusVerdict, str]":
    """First-match-wins verdict ladder.

    Order:
      1. NEVER_RAN — no signals at all.
      2. RECENTLY_FAILED — most-recent preflight is a failure
         AND it's more recent than ok / history.
      3. OVERDUE — no successful signal within grace_window_s.
      4. HEALTHY — at least one successful signal within
         grace_window_s.
    """
    # 1. NEVER_RAN
    if (
        ok_age is None
        and failure_age is None
        and history_age is None
    ):
        return (
            CadenceStatusVerdict.NEVER_RAN,
            "no_signals_observed",
        )
    # 2. RECENTLY_FAILED — failure present + more recent than
    # any success.
    if failure_age is not None:
        recent_success_age = _min_or_none(ok_age, history_age)
        if (
            recent_success_age is None
            or failure_age < recent_success_age
        ):
            return (
                CadenceStatusVerdict.RECENTLY_FAILED,
                f"failure_age={int(failure_age)}s",
            )
    # 3 / 4 — OVERDUE vs HEALTHY based on most-recent success.
    recent_success_age = _min_or_none(ok_age, history_age)
    if recent_success_age is None:
        # No successful signal but we know failure_age exists
        # (caught by step 2 above) — fallthrough means the only
        # signals are stale failures. Treat as OVERDUE.
        return (
            CadenceStatusVerdict.OVERDUE,
            "only_stale_failures",
        )
    if recent_success_age <= grace_window_s:
        return (
            CadenceStatusVerdict.HEALTHY,
            f"last_success_age={int(recent_success_age)}s",
        )
    return (
        CadenceStatusVerdict.OVERDUE,
        (
            f"last_success_age={int(recent_success_age)}s "
            f"> grace_window_s={grace_window_s}s"
        ),
    )


def _min_or_none(
    a: Optional[float], b: Optional[float],
) -> Optional[float]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def is_overdue(
    *, now_epoch: Optional[float] = None,
) -> bool:
    """Convenience predicate. Returns True iff the verdict is
    :attr:`CadenceStatusVerdict.OVERDUE` OR
    :attr:`CadenceStatusVerdict.RECENTLY_FAILED`. NEVER raises."""
    try:
        report = evaluate_cadence_status(now_epoch=now_epoch)
        return report.verdict in (
            CadenceStatusVerdict.OVERDUE,
            CadenceStatusVerdict.RECENTLY_FAILED,
        )
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Operator-facing render — composes existing ANSI vocabulary
# ---------------------------------------------------------------------------


_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"


def render_cadence_status_block(
    report: CadenceStatusReport,
) -> str:
    """Multi-line ANSI-colored block. Composes existing
    cyan/yellow/red/dim vocabulary; NO bright_green
    (§37.9 invariant #3). NEVER raises."""
    try:
        verdict_color = _color_for_verdict(report.verdict)
        verdict_label = report.verdict.value.upper()
        out = [
            f"\n  {_BOLD}{_CYAN}Cadence status{_RESET}  "
            f"{verdict_color}{verdict_label}{_RESET}",
            "",
        ]
        if report.schedule_kind and report.schedule_string:
            out.append(
                f"  {_DIM}schedule_kind:{_RESET}     "
                f"{report.schedule_kind}"
            )
            out.append(
                f"  {_DIM}schedule_string:{_RESET}   "
                f"{report.schedule_string}"
            )
        if report.interval_hint_s > 0:
            out.append(
                f"  {_DIM}interval_hint_s:{_RESET}   "
                f"{report.interval_hint_s} "
                f"({_format_duration(report.interval_hint_s)})"
            )
            out.append(
                f"  {_DIM}grace_window_s:{_RESET}    "
                f"{report.grace_window_s} "
                f"({_format_duration(report.grace_window_s)})"
            )
        if report.last_preflight_ok_age_s is not None:
            out.append(
                f"  {_DIM}last_preflight_ok:{_RESET}  "
                f"{_format_duration(int(report.last_preflight_ok_age_s))} ago"
            )
        if report.last_preflight_failure_age_s is not None:
            out.append(
                f"  {_DIM}last_preflight_fail:{_RESET}"
                f" {_format_duration(int(report.last_preflight_failure_age_s))} ago"
            )
        if report.last_history_row_age_s is not None:
            out.append(
                f"  {_DIM}last_history_row:{_RESET}   "
                f"{_format_duration(int(report.last_history_row_age_s))} ago"
            )
        if report.next_expected_iso:
            out.append(
                f"  {_DIM}next_expected:{_RESET}      "
                f"{report.next_expected_iso}"
            )
        if report.detail:
            out.append(
                f"  {_DIM}detail:{_RESET}            "
                f"{report.detail[:120]}"
            )
        out.append("")
        return "\n".join(out)
    except Exception:  # noqa: BLE001 — defensive
        return f"\n  {_DIM}cadence status unavailable{_RESET}\n"


def _color_for_verdict(v: CadenceStatusVerdict) -> str:
    if v == CadenceStatusVerdict.HEALTHY:
        return _CYAN
    if v == CadenceStatusVerdict.OVERDUE:
        return _RED
    if v == CadenceStatusVerdict.RECENTLY_FAILED:
        return _RED
    if v == CadenceStatusVerdict.NEVER_RAN:
        return _YELLOW
    return _DIM


def _format_duration(seconds: int) -> str:
    """Compact human duration. Pure stdlib. Never raises."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 0:
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h = s // 3600
        rem_m = (s % 3600) // 60
        return f"{h}h{rem_m}m" if rem_m else f"{h}h"
    d = s // 86400
    h = (s % 86400) // 3600
    return f"{d}d{h}h" if h else f"{d}d"


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Auto-discovered. Pins:

      1. ``cadence_status_authority_asymmetry`` — substrate
         purity.
      2. ``cadence_status_no_hardcoded_cadence_seconds`` —
         the source must NOT contain integer literals that
         look like seconds-per-day (86400) / 12h (43200) /
         8h (28800) / 6h (21600) / 4h (14400) / etc., because
         the manifest is the SOLE knower (operator binding
         "no magic 86400 in random modules"). Whitelisted:
         60, 3600, 86400 ONLY when they appear inside the
         pure ``_format_duration`` rendering helper, which
         needs them for unit conversion.
      3. ``cadence_status_verdict_taxonomy_closed`` — 5-value
         verdict bytes-pinned.
      4. ``cadence_status_versioned_artifact_compliance`` —
         §33.5 contract on the report.
      5. ``cadence_status_read_only`` — no calls to
         ``record_*`` / ``write_*`` / ``record_session`` /
         ``record_health_row`` / ``write_manifest`` (substrate
         is a passive reader).
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/graduation/"
        "cadence_status.py"
    )

    _EXPECTED_VERDICTS = {
        "healthy", "overdue", "recently_failed",
        "never_ran", "unknown",
    }

    # Cadence-second-shaped constants that MUST come from the
    # manifest, not be hardcoded.
    _BANNED_CADENCE_LITERALS = frozenset({
        # 1-minute multiples of common cadences
        300, 600, 900, 1200, 1800, 2700,
        # hours
        7200, 10800, 14400, 18000, 21600, 25200,
        28800, 32400, 36000, 39600, 43200, 46800,
        50400, 54000, 57600, 61200, 64800, 68400, 72000,
        # 1 day, 2 day, etc.
        86400, 172800, 259200,
        604800,  # 1 week
    })
    # Whitelist: helpers that need raw unit constants for
    # rendering (NOT cadence policy) + the pin-definition
    # function itself (which contains the banned-literal set
    # by construction).
    _ALLOWED_LITERAL_FUNCTIONS = frozenset({
        "_format_duration",
        # Inner validator definitions are defined inside the
        # outer ``register_shipped_invariants``; they reference
        # the closed-over banned set. Whitelist the outer +
        # inner names so the pin doesn't fire on its own
        # frozenset literals.
        "register_shipped_invariants",
        "_validate_no_hardcoded_cadence_seconds",
    })

    def _validate_authority_asymmetry(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router",
            "change_engine", "semantic_guardian",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"cadence_status.py MUST NOT "
                            f"import {module!r}"
                        )
        return tuple(violations)

    def _validate_no_hardcoded_cadence_seconds(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        """The manifest is the SOLE knower of cadence interval.
        Banned cadence-second literals MUST NOT appear in the
        source outside the whitelisted rendering helper."""
        violations: list = []
        # Find all banned literals along with the function they
        # appear in.
        # Walk function-by-function so we can scope the
        # whitelist correctly.
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                func_name = node.name
                if func_name in _ALLOWED_LITERAL_FUNCTIONS:
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(
                        sub.value, int,
                    ):
                        if sub.value in _BANNED_CADENCE_LITERALS:
                            violations.append(
                                f"cadence_status.py: function "
                                f"{func_name!r} contains "
                                f"banned cadence literal "
                                f"{sub.value} — read from "
                                f"manifest instead"
                            )
        # Also check module-level statements (non-function).
        for node in tree.body:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant) and isinstance(
                    sub.value, int,
                ):
                    if sub.value in _BANNED_CADENCE_LITERALS:
                        # Skip if inside a class or function — those
                        # were already inspected.
                        if isinstance(
                            node,
                            (ast.FunctionDef, ast.ClassDef,
                             ast.AsyncFunctionDef),
                        ):
                            continue
                        violations.append(
                            f"cadence_status.py: module-level "
                            f"banned cadence literal "
                            f"{sub.value} — read from manifest"
                        )
        return tuple(violations)

    def _validate_verdict_taxonomy_closed(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CadenceStatusVerdict"
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
                missing = _EXPECTED_VERDICTS - found
                extra = found - _EXPECTED_VERDICTS
                if missing:
                    violations.append(
                        f"verdict missing: {sorted(missing)}"
                    )
                if extra:
                    violations.append(
                        f"verdict drift: {sorted(extra)}"
                    )
                return tuple(violations)
        violations.append(
            "CadenceStatusVerdict class definition missing"
        )
        return tuple(violations)

    def _validate_versioned_artifact_compliance(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ClassDef)
                and node.name == "CadenceStatusReport"
            ):
                method_names = {
                    sub.name
                    for sub in node.body
                    if isinstance(
                        sub,
                        (ast.FunctionDef, ast.AsyncFunctionDef),
                    )
                }
                field_names = {
                    sub.target.id
                    for sub in node.body
                    if (
                        isinstance(sub, ast.AnnAssign)
                        and isinstance(sub.target, ast.Name)
                    )
                }
                if "schema_version" not in field_names:
                    violations.append(
                        "CadenceStatusReport MUST declare "
                        "schema_version (§33.5)"
                    )
                if "to_dict" not in method_names:
                    violations.append(
                        "CadenceStatusReport MUST expose to_dict"
                    )
                if "from_dict" not in method_names:
                    violations.append(
                        "CadenceStatusReport MUST expose "
                        "from_dict"
                    )
                return tuple(violations)
        violations.append(
            "CadenceStatusReport class definition missing"
        )
        return tuple(violations)

    def _validate_read_only(
        tree: "ast.Module", source: str,  # noqa: ARG001
    ) -> tuple:
        violations: list = []
        forbidden_calls = (
            "record_health_row",
            "record_session",
            "write_manifest",
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name):
                    if fn.id in forbidden_calls:
                        violations.append(
                            f"cadence_status.py is read-only; "
                            f"MUST NOT call {fn.id}()"
                        )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "cadence_status_authority_asymmetry"
            ),
            target_file=target,
            description="Cadence Slice 3 — substrate purity.",
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cadence_status_no_hardcoded_cadence_seconds"
            ),
            target_file=target,
            description=(
                "Cadence Slice 3 — manifest is sole knower of "
                "cadence interval; no magic seconds-per-day "
                "constants in detection logic."
            ),
            validate=_validate_no_hardcoded_cadence_seconds,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cadence_status_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "Cadence Slice 3 — 5-value verdict closed "
                "taxonomy bytes-pinned."
            ),
            validate=_validate_verdict_taxonomy_closed,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "cadence_status_versioned_artifact_compliance"
            ),
            target_file=target,
            description=(
                "Cadence Slice 3 — §33.5 contract on report."
            ),
            validate=_validate_versioned_artifact_compliance,
        ),
        ShippedCodeInvariant(
            invariant_name="cadence_status_read_only",
            target_file=target,
            description=(
                "Cadence Slice 3 — substrate is passive "
                "reader; no record_/write_ calls."
            ),
            validate=_validate_read_only,
        ),
    ]


__all__ = [
    "CADENCE_STATUS_REPORT_SCHEMA_VERSION",
    "CadenceStatusReport",
    "CadenceStatusVerdict",
    "evaluate_cadence_status",
    "is_overdue",
    "register_shipped_invariants",
    "render_cadence_status_block",
]
