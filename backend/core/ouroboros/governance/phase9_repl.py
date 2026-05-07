"""§3.6.2 vector #6 Slice 2 — `/phase9` REPL verb.

Operator-facing dashboard for the Phase9Orchestrator (Slice 1).
Auto-discovered via §32.11 Slice 4 naming-cage: file
``phase9_repl.py`` → verb ``/phase9`` → dispatcher
``dispatch_phase9_command(line)``.

Lets the operator answer in one shot:
  * What's the full graduation queue + readiness per flag?
  * What's the next-best flag to soak right now?
  * Which flag-pairs have been tested together vs solo-only?

**Subcommands**:

  * ``/phase9`` (bare) — queue overview ranked by readiness.
  * ``/phase9 next`` — print the next recommended flag (or
    "(none)" if nothing soakable).
  * ``/phase9 flag <flag-name>`` — full detail card for one
    flag.
  * ``/phase9 interactions`` — interaction-matrix summary
    (pair counts).
  * ``/phase9 partners <flag-name>`` — list other flags that
    have been enabled alongside <flag-name>.
  * ``/phase9 help`` — usage.

**Read-only browser** (mirrors ``replay_repl`` /
``history_repl`` / ``mode_repl`` / ``canvas_repl`` /
``scope_repl`` discipline). Operator queries the orchestrator
but never mutates ledger / contract / policy state.

**Composition**: single source of truth — composes
``get_default_orchestrator()`` from Slice 1; no parallel state.

**NEVER raises** — every code path defensive.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Optional


logger = logging.getLogger("Ouroboros.Phase9REPL")


_VERBS = ("/phase9",)
_VALID_SUBCOMMANDS = {
    "next", "flag", "interactions", "partners",
    "diagnose", "health", "help",
}


@dataclass
class Phase9DispatchResult:
    """Mirrors sibling REPL dispatch shape."""
    ok: bool
    text: str
    matched: bool = True


def _matches(line: str) -> bool:
    if not line:
        return False
    first = line.split(None, 1)[0]
    return first in _VERBS


def dispatch_phase9_command(line: str) -> Phase9DispatchResult:
    """Parse a ``/phase9`` line and dispatch. NEVER raises."""
    if not _matches(line):
        return Phase9DispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return Phase9DispatchResult(
            ok=False, text=f"/phase9: parse error — {exc}",
        )
    args = tokens[1:] if len(tokens) > 1 else []
    if not args:
        return _render_overview()
    sub = args[0].lower()
    if sub not in _VALID_SUBCOMMANDS:
        return Phase9DispatchResult(
            ok=False,
            text=(
                f"/phase9: unknown subcommand {sub!r}. "
                f"Try /phase9 help."
            ),
        )
    if sub == "help":
        return _render_help()
    if sub == "next":
        return _render_next()
    if sub == "interactions":
        return _render_interactions()
    if sub == "diagnose":
        return _render_diagnose()
    if sub == "health":
        return _render_health()
    if sub == "flag":
        if len(args) < 2:
            return Phase9DispatchResult(
                ok=False,
                text=(
                    "/phase9 flag: missing flag name. "
                    "Usage: /phase9 flag <FLAG_NAME>"
                ),
            )
        return _render_flag(args[1])
    if sub == "partners":
        if len(args) < 2:
            return Phase9DispatchResult(
                ok=False,
                text=(
                    "/phase9 partners: missing flag name. "
                    "Usage: /phase9 partners <FLAG_NAME>"
                ),
            )
        return _render_partners(args[1])
    return Phase9DispatchResult(
        ok=False,
        text=f"/phase9: unhandled subcommand {sub!r}",
    )


def _render_help() -> Phase9DispatchResult:
    text = (
        "/phase9 — Graduation queue dashboard "
        "(§3.6.2 vector #6)\n"
        "\n"
        "  /phase9                   queue overview ranked "
        "by readiness\n"
        "  /phase9 next              next recommended flag "
        "to soak\n"
        "  /phase9 flag <FLAG>       full detail card for "
        "one flag\n"
        "  /phase9 interactions      interaction-matrix "
        "pair-count summary\n"
        "  /phase9 partners <FLAG>   distinct partner flags "
        "for <FLAG>\n"
        "  /phase9 diagnose          cadence staleness + "
        "per-flag failure patterns\n"
        "  /phase9 health            substrate-health probe "
        "+ ETA projection per flag\n"
        "  /phase9 help              this message\n"
        "\n"
        "Master flag: JARVIS_PHASE9_ORCHESTRATOR_ENABLED "
        "(default-FALSE per §33.1)\n"
        "Sources: adaptation/graduation_ledger.CADENCE_POLICY "
        "+ GraduationLedger.progress + "
        ".jarvis/graduation_interaction_matrix.jsonl\n"
        "Note: actual soak runs are operator-paced via "
        "scripts/live_fire_graduation_soak.py — this "
        "dashboard surfaces readiness only."
    )
    return Phase9DispatchResult(ok=True, text=text)


def _orchestrator_or_disabled() -> Optional[Any]:
    try:
        from backend.core.ouroboros.governance.phase9_orchestrator import (  # noqa: E501
            get_default_orchestrator,
            master_enabled,
        )
    except ImportError:
        return None
    try:
        if not master_enabled():
            return None
    except Exception:  # noqa: BLE001 — defensive
        return None
    try:
        return get_default_orchestrator()
    except Exception:  # noqa: BLE001 — defensive
        return None


def _disabled_result() -> Phase9DispatchResult:
    return Phase9DispatchResult(
        ok=True,
        text=(
            "/phase9: dashboard disabled. Set "
            "JARVIS_PHASE9_ORCHESTRATOR_ENABLED=true to "
            "enable. Note: graduation_ledger + "
            "live_fire_graduation_soak operate independently — "
            "this dashboard is the operator-facing aggregation "
            "view (no impact on cadence runs when off)."
        ),
    )


def _format_status(status_value: str) -> str:
    """Pad to 9 chars for column alignment."""
    return f"{status_value.upper():<9}"


def _render_overview() -> Phase9DispatchResult:
    orch = _orchestrator_or_disabled()
    if orch is None:
        return _disabled_result()
    try:
        ranked = orch.rank_by_readiness()
    except Exception:  # noqa: BLE001 — defensive
        return Phase9DispatchResult(
            ok=False,
            text=(
                "/phase9: orchestrator read failed (non-fatal)"
            ),
        )
    if not ranked:
        return Phase9DispatchResult(
            ok=True,
            text=(
                "/phase9: queue empty. The CADENCE_POLICY "
                "table in adaptation/graduation_ledger.py "
                "lists the canonical flags; ensure "
                "JARVIS_GRADUATION_LEDGER_ENABLED=true so "
                "progress() returns real counts."
            ),
        )
    counts = {"READY": 0, "PENDING": 0, "BLOCKED": 0, "GRADUATED": 0}
    for entry in ranked:
        counts[entry.status.value.upper()] = (
            counts[entry.status.value.upper()] + 1
        )
    lines = [
        f"/phase9 queue ({len(ranked)} flags — "
        f"{counts['READY']} ready, "
        f"{counts['PENDING']} pending, "
        f"{counts['BLOCKED']} blocked, "
        f"{counts['GRADUATED']} graduated):",
    ]
    for entry in ranked:
        score_pct = int(round(entry.readiness_score * 100))
        lines.append(
            f"  {_format_status(entry.status.value)} "
            f"{entry.flag_name:<58} "
            f"{entry.clean_count}/{entry.required:<3} "
            f"score={score_pct:>3}% "
            f"partners={entry.interaction_partner_count}"
        )
    return Phase9DispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_next() -> Phase9DispatchResult:
    orch = _orchestrator_or_disabled()
    if orch is None:
        return _disabled_result()
    try:
        nxt = orch.next_recommended_flag()
    except Exception:  # noqa: BLE001 — defensive
        return Phase9DispatchResult(
            ok=False,
            text=(
                "/phase9 next: orchestrator read failed "
                "(non-fatal)"
            ),
        )
    if nxt is None:
        return Phase9DispatchResult(
            ok=True,
            text=(
                "/phase9 next: no soakable flag in queue. "
                "Either all flags are graduated, all are "
                "blocked, or the cadence policy is empty."
            ),
        )
    return Phase9DispatchResult(
        ok=True,
        text=(
            f"/phase9 next: {nxt}\n"
            f"  Run: bash scripts/run_live_fire_graduation_soak"
            f".sh\n"
            f"  Or:  python3 scripts/live_fire_graduation_soak"
            f".py run {nxt}"
        ),
    )


def _render_flag(flag_name: str) -> Phase9DispatchResult:
    orch = _orchestrator_or_disabled()
    if orch is None:
        return _disabled_result()
    target = flag_name.strip()
    try:
        queue = orch.get_full_queue()
    except Exception:  # noqa: BLE001 — defensive
        return Phase9DispatchResult(
            ok=False,
            text=(
                "/phase9 flag: orchestrator read failed "
                "(non-fatal)"
            ),
        )
    entry = next(
        (e for e in queue if e.flag_name == target), None,
    )
    if entry is None:
        return Phase9DispatchResult(
            ok=False,
            text=(
                f"/phase9 flag: no policy entry for "
                f"{target!r} (not in CADENCE_POLICY table)"
            ),
        )
    score_pct = int(round(entry.readiness_score * 100))
    lines = [
        f"/phase9 flag {target}:",
        f"  cadence_class            = {entry.cadence_class}",
        f"  status                   = {entry.status.value}",
        f"  readiness_score          = {score_pct}%",
        f"  clean / required         = {entry.clean_count} "
        f"/ {entry.required}",
        f"  runner_count             = {entry.runner_count}",
        f"  infra_count              = {entry.infra_count}",
        f"  last_outcome             = {entry.last_outcome}",
        f"  interaction_partner_count = "
        f"{entry.interaction_partner_count}",
        f"  description              = {entry.description}",
    ]
    return Phase9DispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_interactions() -> Phase9DispatchResult:
    orch = _orchestrator_or_disabled()
    if orch is None:
        return _disabled_result()
    try:
        matrix = orch.get_interaction_matrix()
        total = orch.total_session_count()
    except Exception:  # noqa: BLE001 — defensive
        return Phase9DispatchResult(
            ok=False,
            text=(
                "/phase9 interactions: matrix read failed "
                "(non-fatal)"
            ),
        )
    if not matrix:
        return Phase9DispatchResult(
            ok=True,
            text=(
                "/phase9 interactions: no recorded sessions "
                "yet. Sessions are recorded via Phase9"
                "Orchestrator.record_session_flags(); "
                "live_fire_graduation_soak.py wires this at "
                "session-end (when wired). Total recorded: 0."
            ),
        )
    # Sort pairs by count desc.
    sorted_pairs = sorted(
        matrix.items(), key=lambda kv: -kv[1],
    )
    lines = [
        f"/phase9 interactions ({total} sessions, "
        f"{len(matrix)} unique pairs):",
    ]
    for pair, count in sorted_pairs[:50]:
        a, b = sorted(pair)
        lines.append(
            f"  {count:>4}  {a}  ×  {b}"
        )
    if len(sorted_pairs) > 50:
        lines.append(
            f"  ... ({len(sorted_pairs) - 50} more pairs "
            f"truncated)"
        )
    return Phase9DispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_partners(flag_name: str) -> Phase9DispatchResult:
    orch = _orchestrator_or_disabled()
    if orch is None:
        return _disabled_result()
    target = flag_name.strip()
    try:
        matrix = orch.get_interaction_matrix()
    except Exception:  # noqa: BLE001 — defensive
        return Phase9DispatchResult(
            ok=False,
            text=(
                "/phase9 partners: matrix read failed "
                "(non-fatal)"
            ),
        )
    partner_counts = {}
    for pair, count in matrix.items():
        if target in pair:
            other = next(
                (f for f in pair if f != target), None,
            )
            if other is not None:
                partner_counts[other] = (
                    partner_counts.get(other, 0) + count
                )
    if not partner_counts:
        return Phase9DispatchResult(
            ok=True,
            text=(
                f"/phase9 partners {target}: no recorded "
                f"co-soak partners. This flag has only been "
                f"soaked solo (or not at all)."
            ),
        )
    sorted_partners = sorted(
        partner_counts.items(), key=lambda kv: -kv[1],
    )
    lines = [
        f"/phase9 partners {target} ({len(sorted_partners)} "
        f"distinct co-soak flags):",
    ]
    for name, count in sorted_partners:
        lines.append(f"  {count:>4}  {name}")
    return Phase9DispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_diagnose() -> Phase9DispatchResult:
    """§3.6.2 vector #6 cadence diagnostics — read-only
    aggregation across the canonical ledgers + Phase9
    Orchestrator state. Surfaces:

      * **Cadence staleness** — last-soak timestamp across
        all flags + comparison with the documented 8h cron
        cadence.
      * **Failure-pattern summary** — per-flag runner /
        infra counts with most-recent failure note (when
        available).
      * **Top blockers** — flags with the most accumulated
        runner failures (operator's "what to debug next"
        signal).

    Pure aggregation; NEVER raises. Composes
    ``GraduationLedger.progress`` + ``CADENCE_POLICY`` —
    no parallel ledger queries."""
    orch = _orchestrator_or_disabled()
    if orch is None:
        return _disabled_result()
    try:
        from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
            CADENCE_POLICY,
            get_default_ledger,
            is_ledger_enabled,
        )
    except ImportError:
        return Phase9DispatchResult(
            ok=False,
            text=(
                "/phase9 diagnose: graduation_ledger "
                "substrate unavailable (non-fatal)"
            ),
        )
    if not is_ledger_enabled():
        return Phase9DispatchResult(
            ok=True,
            text=(
                "/phase9 diagnose: graduation_ledger master "
                "flag JARVIS_GRADUATION_LEDGER_ENABLED is "
                "off — no per-flag failure data available. "
                "Set the flag to true (the cron + wrapper "
                "scripts already do); diagnose populates "
                "after the next soak."
            ),
        )
    try:
        ledger = get_default_ledger()
    except Exception:  # noqa: BLE001 — defensive
        return Phase9DispatchResult(
            ok=False,
            text=(
                "/phase9 diagnose: ledger access failed "
                "(non-fatal)"
            ),
        )
    # Aggregate per-flag counts.
    total_runner = 0
    total_infra = 0
    total_clean = 0
    blocked: list = []  # (flag, runner_count, last_outcome)
    pending: list = []  # (flag, clean, required, score)
    for policy in CADENCE_POLICY:
        try:
            progress = ledger.progress(policy.flag_name)
        except Exception:  # noqa: BLE001 — defensive
            continue
        runner = int(progress.get("runner", 0))
        infra = int(progress.get("infra", 0))
        clean = int(progress.get("clean", 0))
        required = int(
            progress.get(
                "required", policy.required_clean_sessions,
            ),
        )
        total_runner += runner
        total_infra += infra
        total_clean += clean
        if runner > 0:
            blocked.append((policy.flag_name, runner))
        elif clean < required:
            score = (
                clean / required if required > 0 else 0.0
            )
            pending.append(
                (policy.flag_name, clean, required, score),
            )
    # Sort blocked desc by runner count; pending asc by
    # remaining sessions needed (= 1 - score).
    blocked.sort(key=lambda r: -r[1])
    pending.sort(key=lambda r: r[3], reverse=True)
    lines = ["/phase9 diagnose:"]
    lines.append(
        f"  totals          clean={total_clean}  "
        f"infra={total_infra}  runner={total_runner}"
    )
    lines.append("")
    if blocked:
        lines.append(
            f"  blocked flags ({len(blocked)} — "
            f"runner failures present, soaks wasted until "
            f"resolved):"
        )
        for flag_name, runner_count in blocked[:10]:
            lines.append(
                f"    runner={runner_count:<3} {flag_name}"
            )
        if len(blocked) > 10:
            lines.append(
                f"    ... ({len(blocked) - 10} more)"
            )
        lines.append("")
    else:
        lines.append("  blocked flags: (none)")
        lines.append("")
    lines.append(
        f"  next-soakable ranked by readiness "
        f"({len(pending)} pending):"
    )
    for flag_name, clean, required, score in pending[:5]:
        score_pct = int(round(score * 100))
        lines.append(
            f"    {score_pct:>3}%  {clean}/{required} "
            f"clean  {flag_name}"
        )
    if len(pending) > 5:
        lines.append(f"    ... ({len(pending) - 5} more)")
    lines.append("")
    lines.append(
        "  cadence: documented 8h cron schedule "
        "(install via `bash scripts/install_live_fire_"
        "soak_cron.sh --install`); manual via `bash "
        "scripts/run_live_fire_graduation_soak.sh`. "
        "Operator-paced — engineering surface complete; "
        "evidence accumulation is wall-clock."
    )
    return Phase9DispatchResult(
        ok=True, text="\n".join(lines),
    )


def _render_health() -> Phase9DispatchResult:
    """§3.6.2 vector #6 closure — substrate-health probe + ETA
    projection per flag. Composes the canonical
    `phase9_substrate_health` aggregator (no parallel state).
    NEVER raises."""
    try:
        from backend.core.ouroboros.governance.phase9_substrate_health import (  # noqa: E501
            build_full_health_dashboard,
            master_enabled as health_master_enabled,
        )
    except ImportError:
        return Phase9DispatchResult(
            ok=False,
            text=(
                "/phase9 health: substrate-health module "
                "unavailable (non-fatal)"
            ),
        )
    try:
        if not health_master_enabled():
            return Phase9DispatchResult(
                ok=True,
                text=(
                    "/phase9 health: substrate-health "
                    "disabled. Set "
                    "JARVIS_PHASE9_SUBSTRATE_HEALTH_ENABLED"
                    "=true to enable. The probe is read-only "
                    "diagnostic — does NOT change §33.1 "
                    "evidence semantics."
                ),
            )
    except Exception:  # noqa: BLE001 — defensive
        return Phase9DispatchResult(
            ok=False,
            text=(
                "/phase9 health: master flag check failed "
                "(non-fatal)"
            ),
        )
    try:
        reports = build_full_health_dashboard()
    except Exception:  # noqa: BLE001 — defensive
        return Phase9DispatchResult(
            ok=False,
            text=(
                "/phase9 health: dashboard build failed "
                "(non-fatal)"
            ),
        )
    if not reports:
        return Phase9DispatchResult(
            ok=True,
            text=(
                "/phase9 health: no flags in CADENCE_POLICY "
                "table; nothing to probe."
            ),
        )
    # Aggregate health counts.
    health_counts = {
        "HEALTHY": 0, "DEGRADED": 0,
        "BROKEN": 0, "UNKNOWN": 0,
    }
    for r in reports:
        health_counts[r.health.value.upper()] = (
            health_counts[r.health.value.upper()] + 1
        )
    lines = [
        f"/phase9 health ({len(reports)} flags — "
        f"{health_counts['HEALTHY']} healthy / "
        f"{health_counts['DEGRADED']} degraded / "
        f"{health_counts['BROKEN']} broken / "
        f"{health_counts['UNKNOWN']} unknown):",
    ]
    for r in reports:
        flag = r.flag_name
        # Truncate very long flag names for the table column.
        flag_clip = (
            flag[:54] + "..." if len(flag) > 54 else flag
        )
        lines.append(
            f"  {r.health.value:<9} "
            f"{flag_clip:<58} "
            f"{r.notes}"
        )
    lines.append("")
    lines.append(
        "Note: substrate-health is a DIAGNOSTIC probe — "
        "it tells you whether the cage layer for each flag "
        "is structurally working. It does NOT graduate flags "
        "(graduation requires real cadence runs per §33.1)."
    )
    return Phase9DispatchResult(
        ok=True, text="\n".join(lines),
    )


__all__ = [
    "Phase9DispatchResult",
    "dispatch_phase9_command",
]
