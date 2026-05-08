"""``/graduation`` REPL dispatcher — Unified Graduation Dashboard
operator surface (PRD §35, 2026-05-07).

Auto-discovered by §32.11 Slice 4 ``repl_dispatch_registry`` via
the §33.3 naming-cage convention:

  * file ends ``_repl.py`` → verb derived from basename
  * exposes module-level ``dispatch_graduation_command(line) ->
    GraduationReplDispatchResult``
  * SerpentREPL routes any line matching ``/graduation``,
    ``graduation``, ``/graduation …``, or ``graduation …``
    here zero-edit.

## Subcommands

  * ``/graduation``                  alias for ``status``
  * ``/graduation status``           per-verdict count summary
  * ``/graduation ready``            list every gate marked READY
  * ``/graduation failed``           list every gate marked
                                     EVIDENCE_FAILED (signal
                                     against graduation)
  * ``/graduation details [N]``      full per-row table (default
                                     all rows; N caps the output)
  * ``/graduation contract <name>``  render one row's diagnostic
                                     for the named contract or
                                     ledger flag
  * ``/graduation help``             this text (always available;
                                     bypasses master-flag gate)

## Architectural locks (operator mandate, AST-pinned)

  1. **Composes substrate** — invokes :func:`aggregate_dashboard`
     to build the snapshot. NO parallel reasoning about
     graduation readiness; NO direct contract calls.
  2. **Read-only** — no mutation of ledger / contract state.
     Pure operator snapshot surface.
  3. **Master-flag-gated** — every subcommand except ``help``
     short-circuits on :func:`is_dashboard_enabled`.
  4. **Authority asymmetry** — imports stdlib +
     unified_graduation_dashboard substrate ONLY. NEVER
     imports orchestrator / iron_gate / policy / providers /
     candidate_generator / change_engine / semantic_guardian.
  5. **NEVER raises** — every subcommand defensive; exceptions
     surface as a non-ok ``GraduationReplDispatchResult``.
"""
from __future__ import annotations

import logging
import shlex
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


GRADUATION_REPL_SCHEMA_VERSION: str = "graduation_repl.1"


_HELP = (
    "/graduation — Unified Graduation Dashboard (PRD §35)\n"
    "\n"
    "Aggregates ALL graduation gates across the codebase:\n"
    "  * 8 §33.1 graduation contracts\n"
    "  * 32 CADENCE_POLICY ledger flags (Phase 9)\n"
    "\n"
    "Subcommands:\n"
    "  /graduation                    alias for /graduation status\n"
    "  /graduation status             per-verdict summary\n"
    "  /graduation ready              gates marked READY\n"
    "  /graduation failed             gates with EVIDENCE_FAILED\n"
    "  /graduation details [N]        full per-row table\n"
    "  /graduation contract <name>    one row's diagnostic\n"
    "  /graduation help               this text\n"
    "\n"
    "Verdict ladder (unified):\n"
    "  READY                  — all gates green\n"
    "  EVIDENCE_GATHERING     — wired & producing, threshold\n"
    "                           not yet reached\n"
    "  EVIDENCE_INSUFFICIENT  — substrate unwired or inactive\n"
    "  EVIDENCE_FAILED        — observed signal says NOT-READY\n"
    "                           (excessive drift / runner\n"
    "                           failures / etc.)\n"
    "  DISABLED               — contract harness master off\n"
    "\n"
    "Master flag: JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED "
    "(default false).\n"
)


@dataclass(frozen=True)
class GraduationReplDispatchResult:
    """Result of a ``/graduation`` dispatch. Frozen for safe
    propagation. ``matched=False`` signals the line wasn't a
    ``/graduation`` invocation (caller routes elsewhere)."""

    ok: bool
    text: str
    matched: bool = True
    schema_version: str = GRADUATION_REPL_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Master-flag check — defers to dashboard substrate
# ---------------------------------------------------------------------------


def _master_enabled() -> bool:
    try:
        from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
            is_dashboard_enabled,
        )
        return bool(is_dashboard_enabled())
    except Exception:  # noqa: BLE001 — defensive
        return False


# ---------------------------------------------------------------------------
# Dispatcher — auto-discovered by §32.11 Slice 4 registry
# ---------------------------------------------------------------------------


def _matches(line: str) -> bool:
    s = (line or "").strip()
    if not s:
        return False
    return (
        s == "/graduation"
        or s == "graduation"
        or s.startswith("/graduation ")
        or s.startswith("graduation ")
    )


def dispatch_graduation_command(
    line: str,
) -> GraduationReplDispatchResult:
    """Parse a ``/graduation`` line and dispatch. NEVER raises —
    exceptions surface as non-ok results.

    Auto-discovered by :mod:`repl_dispatch_registry` (§32.11
    Slice 4) — file ends ``_repl.py`` and the dispatcher
    function name matches the basename. Verb name is
    ``graduation``."""
    if not _matches(line):
        return GraduationReplDispatchResult(
            ok=False, text="", matched=False,
        )
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        return GraduationReplDispatchResult(
            ok=False,
            text=f"  /graduation parse error: {exc}",
        )
    args = tokens[1:] if tokens else []
    head = (args[0].lower() if args else "status")

    if head in ("help", "?"):
        return GraduationReplDispatchResult(
            ok=True, text=_HELP,
        )

    if not _master_enabled():
        return GraduationReplDispatchResult(
            ok=False,
            text=(
                "  /graduation: Unified Graduation Dashboard "
                "disabled (default per §33.1). Set "
                "JARVIS_UNIFIED_GRADUATION_DASHBOARD_ENABLED="
                "true to query."
            ),
        )

    try:
        if head == "status":
            return _render_status()
        if head == "ready":
            return _render_ready()
        if head == "failed":
            return _render_failed()
        if head == "details":
            return _render_details(_parse_limit(args))
        if head == "contract":
            return _render_contract(args[1:] if len(args) > 1 else [])
        return GraduationReplDispatchResult(
            ok=False,
            text=(
                f"  /graduation: unknown subcommand "
                f"{head!r}. Try /graduation help."
            ),
        )
    except Exception as exc:  # noqa: BLE001 — defensive
        return GraduationReplDispatchResult(
            ok=False,
            text=(
                f"  /graduation: internal error: "
                f"{type(exc).__name__}: {str(exc)[:200]}"
            ),
        )


def _parse_limit(args) -> Optional[int]:
    """Parse limit from ``args[1]`` (positional after ``details``).
    Returns None for "no limit" (default). NEVER raises."""
    if len(args) < 2:
        return None
    try:
        n = int(args[1])
        if n < 1:
            return 1
        if n > 1000:
            return 1000
        return n
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _aggregate():
    """Lazy-import + call the substrate aggregator. NEVER
    raises — always returns a snapshot (empty rows on failure)."""
    try:
        from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
            aggregate_dashboard,
        )
        return aggregate_dashboard()
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "aggregate_dashboard raised: %s",
            type(exc).__name__,
        )
        from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
            DashboardSnapshot,
        )
        return DashboardSnapshot()


def _render_status() -> GraduationReplDispatchResult:
    snap = _aggregate()
    summary = snap.summary()
    ready = len(snap.ready_rows())
    failed = len(snap.failed_rows())
    total = len(snap.rows)
    parts = ["# /graduation status"]
    parts.append(f"  total gates       : {total}")
    parts.append(f"  ready             : {ready}")
    parts.append(f"  evidence_gathering: "
                 f"{summary.get('evidence_gathering', 0)}")
    parts.append(f"  evidence_insufficient: "
                 f"{summary.get('evidence_insufficient', 0)}")
    parts.append(f"  evidence_failed   : {failed}")
    parts.append(f"  disabled          : "
                 f"{summary.get('disabled', 0)}")
    parts.append(f"  elapsed           : {snap.elapsed_s:.3f}s")
    if ready > 0:
        parts.append("")
        parts.append("# Try /graduation ready for the list.")
    if failed > 0:
        parts.append("# Try /graduation failed for blockers.")
    return GraduationReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_ready() -> GraduationReplDispatchResult:
    snap = _aggregate()
    rows = snap.ready_rows()
    if not rows:
        return GraduationReplDispatchResult(
            ok=True,
            text="# /graduation ready — no gates currently READY",
        )
    parts = [f"# /graduation ready — {len(rows)} gate(s)"]
    for r in rows:
        parts.append(
            f"  [{r.source}] {r.name}  →  {r.diagnostic}"
        )
    return GraduationReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_failed() -> GraduationReplDispatchResult:
    snap = _aggregate()
    rows = snap.failed_rows()
    if not rows:
        return GraduationReplDispatchResult(
            ok=True,
            text=(
                "# /graduation failed — no gates with "
                "EVIDENCE_FAILED"
            ),
        )
    parts = [f"# /graduation failed — {len(rows)} gate(s)"]
    for r in rows:
        parts.append(
            f"  [{r.source}] {r.name}  →  {r.diagnostic}"
        )
        if r.raw_verdict:
            parts.append(f"      (raw: {r.raw_verdict})")
    return GraduationReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_details(limit: Optional[int]) -> GraduationReplDispatchResult:
    snap = _aggregate()
    rows = snap.rows
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        return GraduationReplDispatchResult(
            ok=True,
            text="# /graduation details — no rows",
        )
    parts = [
        f"# /graduation details — {len(rows)} row(s)",
        f"# {'verdict':<22} {'source':<10} {'name':<40} "
        f"diagnostic",
    ]
    for r in rows:
        parts.append(
            f"  {r.verdict.value:<22} {r.source:<10} "
            f"{r.name:<40} {r.diagnostic}"
        )
    return GraduationReplDispatchResult(
        ok=True, text="\n".join(parts),
    )


def _render_contract(args) -> GraduationReplDispatchResult:
    if not args:
        return GraduationReplDispatchResult(
            ok=False,
            text=(
                "  /graduation contract <name> — name "
                "required. Try /graduation details for the "
                "full list of names."
            ),
        )
    name = str(args[0]).strip()
    snap = _aggregate()
    matches = [r for r in snap.rows if r.name == name]
    if not matches:
        # Try case-insensitive substring match for operator
        # convenience.
        lc = name.lower()
        matches = [
            r for r in snap.rows
            if lc in r.name.lower()
        ]
    if not matches:
        return GraduationReplDispatchResult(
            ok=False,
            text=(
                f"  /graduation contract {name!r}: not found. "
                f"Try /graduation details for the full list."
            ),
        )
    parts = [f"# /graduation contract {name}"]
    for r in matches:
        parts.append(f"  source     : {r.source}")
        parts.append(f"  name       : {r.name}")
        parts.append(f"  verdict    : {r.verdict.value}")
        if r.raw_verdict:
            parts.append(f"  raw_verdict: {r.raw_verdict}")
        parts.append(f"  diagnostic : {r.diagnostic}")
        parts.append(f"  elapsed_s  : {r.elapsed_s:.3f}")
        parts.append("")
    return GraduationReplDispatchResult(
        ok=True, text="\n".join(parts).rstrip(),
    )


# ---------------------------------------------------------------------------
# /help auto-discovery hook
# ---------------------------------------------------------------------------


def register_verbs(registry: Any) -> int:  # noqa: ANN001
    """Register the ``/graduation`` verb with the help-dispatcher
    registry (best-effort)."""
    if registry is None:
        return 0
    try:
        registry.register(
            verb="graduation",
            description=(
                "Unified Graduation Dashboard — aggregate "
                "all 8 §33.1 contracts + 32 ledger flags"
            ),
            help_text=_HELP,
            source_file=(
                "backend/core/ouroboros/governance/"
                "graduation_repl.py"
            ),
        )
        return 1
    except Exception:  # noqa: BLE001 — defensive
        try:
            logger.debug(
                "[graduation_repl] register_verbs swallowed",
            )
        except Exception:  # noqa: BLE001
            pass
        return 0


__all__ = [
    "GRADUATION_REPL_SCHEMA_VERSION",
    "GraduationReplDispatchResult",
    "dispatch_graduation_command",
    "register_verbs",
]
