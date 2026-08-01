"""``/liveness`` — the registry that audits everything, finally auditable.

`dynamic_dispatch_registry` exists to answer "did this module actually run?"
for capabilities a static caller-index cannot see. It answers it correctly and
**in process memory only**. A soak running for three and a half hours holds a
fully-populated registry that nothing outside that process can read: no REPL
verb, no `/observability` route, no log line. An auditing registry that cannot
itself be audited across a process boundary is the gap it was built to close,
one layer up.

This is that surface. It is a VERB, not a new transport, because the transport
already exists and is already proven: `serpent_flow._dispatch_repl_command`
mirrors any auto-discovered verb's returned text to the attached cockpit via
``_flow._mirror_markup`` — the seam whose own comment calls itself "THE gap
that made 59 verbs invisible from `ov attach`". Addressing is handled at the
bridge from the dispatch ContextVar, so a verb typed in cockpit A returns to
cockpit A alone. Auto-discovery comes from the ``*_repl.py`` naming cage. No
socket code, no envelope, no registration.

TWO SURFACES, AND THE COST GAP BETWEEN THEM IS 6 ORDERS OF MAGNITUDE
---------------------------------------------------------------------
Measured on this tree:

    dynamic_dispatch.snapshot()      0.02 ms
    capability_liveness.snapshot()   21,442 ms

`dispatch_<verb>_command` may be ``async`` (the naming cage accepts a
coroutine function — see `repl_dispatch_registry`'s ``iscoroutinefunction``
branch), and this one is, for exactly that reason: a synchronous 21-second
scan would freeze the daemon's event loop, every attached cockpit, the
heartbeat and the running soak along with it. The scan goes through
``asyncio.to_thread`` and the loop stays live.

The default view is the REGISTRY — instant, and the thing that is actually
unreadable today. Capability rows are opt-in behind a flag, and memoised,
because 21 seconds is not a keystroke budget.

FILTERING HAPPENS BEFORE RENDERING
-----------------------------------
Which is the same thing as before transmission here: the daemon renders text
and mirrors the rendered text, so a row filtered out is a row that never
reaches the socket. ``--high`` on this tree is 3 rows out of 190.

Rich markup, not ANSI. The mirror carries markup by contract and each client
fits it to its own canvas; rendering ANSI here would bake this daemon's width
and colour depth into the wire.

Read-only throughout. NEVER raises — a verb that throws while an operator is
typing is worse than a verb that says it has nothing.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

#: Tab completion for the flags, via the module-level convention
#: `_dispatch_arg_spec` already reads (same place as ``__verb_help__`` /
#: ``__aliases__``). Declared rather than mined because `mine_subcommands`
#: skips tokens starting with ``-`` — correctly, since a bare ``-`` prefix in
#: a body is usually an option parse, not a vocabulary. Without this the verb
#: completes to nothing after the space, and a flag an operator cannot
#: discover is a flag that does not exist.
__verb_args__ = {"liveness": "[--high|--silent|--all|dispatch|help]"}

#: Rows shown before the tail is summarised. A cockpit pane is ~40 rows and a
#: verb that fills the transcript is a verb the operator stops typing.
_MAX_ROWS = 24

#: The expensive scan, memoised. 21s is not a keystroke budget, and an
#: operator comparing `--high` against `--silent` should not pay it twice.
_SCAN_TTL_S = 120.0
_scan_cache: Dict[str, Any] = {}


@dataclass(frozen=True)
class LivenessReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True


_HELP = (
    "  [bold cyan]/liveness — what actually ran[/bold cyan]\n"
    "\n"
    "  [dim]the dispatch registry (instant)[/dim]\n"
    "    /liveness                 modules reached by dispatch, and whether\n"
    "                              they ever FIRED\n"
    "    /liveness dispatch        the same, explicitly\n"
    "\n"
    "  [dim]capability severance (a ~21s scan, memoised 2 min)[/dim]\n"
    "    /liveness --high          only findings whose dormancy is PROVEN\n"
    "    /liveness --silent        SILENT rows, split by whether the silence\n"
    "                              is ledger-backed or merely unobserved\n"
    "    /liveness --all           every severance candidate above the floor\n"
    "\n"
    "  [dim]/liveness help[/dim]\n"
)


def _matches(line: str) -> bool:
    try:
        head = (line or "").strip().split(None, 1)[0].lower()
    except IndexError:
        return False
    return head in ("/liveness", "liveness")


def scan_ttl_s() -> float:
    """``JARVIS_LIVENESS_REPL_SCAN_TTL_S`` (default 120). NEVER raises."""
    try:
        return max(0.0, float(os.environ.get(
            "JARVIS_LIVENESS_REPL_SCAN_TTL_S", _SCAN_TTL_S)))
    except (TypeError, ValueError):
        return _SCAN_TTL_S


# ---------------------------------------------------------------------------
# the instant surface — the registry itself
# ---------------------------------------------------------------------------


def _render_dispatch() -> str:
    """The in-memory dispatch registry, rendered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.dynamic_dispatch_registry import (
            FIRING_DYNAMICALLY, REGISTERED_NEVER_INVOKED, snapshot,
        )
        snap = snapshot()
    except Exception as exc:  # noqa: BLE001
        return f"  [red]dispatch registry unavailable:[/red] [dim]{exc}[/dim]"

    if not snap.get("enabled", False):
        return ("  [yellow]dispatch registry is OFF[/yellow] [dim]"
                "(JARVIS_DYNAMIC_DISPATCH_REGISTRY_ENABLED=0) — every verdict "
                "falls back to the static index[/dim]")

    rows = list(snap.get("rows") or ())
    out: List[str] = [
        "  [bold cyan]dispatch registry[/bold cyan] "
        f"[dim]— {snap.get('tracked', 0)} module(s) tracked, "
        f"{snap.get('dropped', 0)} dropped[/dim]",
        "",
        f"    [green]{snap.get('firing', 0)}[/green] firing dynamically     "
        f"[yellow]{snap.get('registered_never_invoked', 0)}[/yellow] "
        "registered, never invoked",
    ]
    if not rows:
        out += [
            "",
            "  [dim]nothing recorded yet. The registry fills as handlers "
            "subscribe and fire — an empty registry in a live session means "
            "no instrumented dispatch has run, not that the registry is "
            "broken.[/dim]",
        ]
        return "\n".join(out)

    out += ["", f"    {'MODULE':<34}{'VERDICT':<26}{'REG':>5}{'INV':>6}"]
    for r in rows[:_MAX_ROWS]:
        verdict = str(r.get("verdict", "?"))
        colour = ("green" if verdict == FIRING_DYNAMICALLY
                  else "yellow" if verdict == REGISTERED_NEVER_INVOKED
                  else "dim")
        out.append(
            f"    [cyan]{str(r.get('module', '?'))[:33]:<34}[/cyan]"
            f"[{colour}]{verdict[:25]:<26}[/{colour}]"
            f"[dim]{r.get('registrations', 0):>5}{r.get('invocations', 0):>6}"
            f"[/dim]"
        )
    if len(rows) > _MAX_ROWS:
        out.append(f"    [dim]… {len(rows) - _MAX_ROWS} more[/dim]")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# the expensive surface — capability severance
# ---------------------------------------------------------------------------


def _collect_rows() -> Tuple[List[Dict[str, Any]], float]:
    """The 21-second scan. Runs OFF the event loop. NEVER raises.

    Memoised on wall-clock rather than on content: the scan reads the working
    tree and the session logs, so its answer legitimately changes between
    calls, and a content hash would cost as much as the scan.
    """
    now = time.time()
    cached = _scan_cache.get("rows")
    if cached is not None and (now - float(_scan_cache.get("at", 0.0))) < scan_ttl_s():
        return cached, float(_scan_cache["at"])
    try:
        from backend.core.ouroboros.governance import capability_liveness as cl
        from backend.core.ouroboros.governance.intake.sensors.liveness_sensor import (
            _severed_floor, severity_for,
        )
        snap = cl.snapshot()
        floor = _severed_floor()
        rows: List[Dict[str, Any]] = []
        for r in snap.get("severance_candidates") or ():
            fraction = float(r.get("fraction_severed") or 0.0)
            if fraction < floor:
                continue
            source = str(r.get("source_file") or "?")
            rows.append({
                "source_file": source.split("/")[-1],
                "flag": str(r.get("flag") or "?"),
                "firing": str(r.get("firing") or "UNKNOWN"),
                "ledger_backed": bool(r.get("ledger_backed")),
                "fraction": fraction,
                "severity": severity_for(
                    str(r.get("category") or ""), str(r.get("firing") or ""),
                    fraction, source.split("/")[-1],
                    ledger_backed=r.get("ledger_backed"),
                ),
            })
        rows.sort(key=lambda d: (d["severity"] != "high", -d["fraction"],
                                 d["source_file"]))
        _scan_cache["rows"] = rows
        _scan_cache["at"] = now
        return rows, now
    except Exception:  # noqa: BLE001
        return [], now


def _filter(rows: List[Dict[str, Any]], mode: str) -> List[Dict[str, Any]]:
    """Filter BEFORE rendering — which here is before transmission."""
    if mode == "high":
        return [r for r in rows if r["severity"] == "high"]
    if mode == "silent":
        return [r for r in rows if r["firing"] == "SILENT"]
    return rows


def _render_capabilities(rows: List[Dict[str, Any]], mode: str,
                         scanned_at: float, total: int) -> str:
    if not rows:
        return (f"  [bold cyan]capability severance[/bold cyan] [dim]— no rows "
                f"match --{mode} (of {total} above the floor)[/dim]")
    age = max(0, int(time.time() - scanned_at))
    out = [
        f"  [bold cyan]capability severance[/bold cyan] [dim]— {len(rows)} of "
        f"{total} above the floor · --{mode} · scanned {age}s ago[/dim]",
        "",
        f"    {'SEV':<6}{'SOURCE':<28}{'FIRING':<10}{'PROOF':<8}{'SEVERED':>8}",
    ]
    for r in rows[:_MAX_ROWS]:
        sev = r["severity"]
        sev_colour = "red" if sev == "high" else "dim"
        # PROOF is the distinction the sensor now acts on: a ledger channel is
        # evidence-of-work, a log-only channel is ambiguous by construction.
        proof = "ledger" if r["ledger_backed"] else "log"
        proof_colour = "yellow" if r["ledger_backed"] else "dim"
        out.append(
            f"    [{sev_colour}]{sev:<6}[/{sev_colour}]"
            f"[cyan]{r['source_file'][:27]:<28}[/cyan]"
            f"[dim]{r['firing'][:9]:<10}[/dim]"
            f"[{proof_colour}]{proof:<8}[/{proof_colour}]"
            f"[dim]{r['fraction'] * 100:>7.0f}%[/dim]"
        )
    if len(rows) > _MAX_ROWS:
        out.append(f"    [dim]… {len(rows) - _MAX_ROWS} more[/dim]")
    if mode == "silent":
        ledger = sum(1 for r in rows if r["ledger_backed"])
        out += [
            "",
            f"    [dim]{ledger} ledger-backed (dormancy PROVEN) · "
            f"{len(rows) - ledger} log-only (unobserved, not proven — an "
            f"absent log tag may mean it ran silently)[/dim]",
        ]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------


async def dispatch_liveness_command(line: str) -> LivenessReplDispatchResult:
    """What actually ran — the dispatch registry and capability severance.

    Operator: Show which modules have really fired, and which look severed.

    ``async`` deliberately: the capability scan measures 21 seconds on this
    tree, and a synchronous dispatcher would hold the daemon's event loop for
    all of it — freezing every attached cockpit and the running soak. It goes
    through ``asyncio.to_thread``; the loop stays live. NEVER raises.
    """
    if not _matches(line):
        return LivenessReplDispatchResult(ok=False, text="", matched=False)
    try:
        tokens = shlex.split(line or "")
    except ValueError as exc:
        return LivenessReplDispatchResult(
            ok=False, text=f"  /liveness parse error: {exc}")
    args = [a.lower() for a in tokens[1:]]

    if any(a in ("help", "?", "--help", "-h") for a in args):
        return LivenessReplDispatchResult(ok=True, text=_HELP)

    mode: Optional[str] = None
    for flag in ("--high", "--silent", "--all"):
        if flag in args:
            mode = flag[2:]
            break
    if mode is None:
        # Default and `dispatch` are the same view: the registry, instantly.
        return LivenessReplDispatchResult(ok=True, text=_render_dispatch())

    try:
        rows, scanned_at = await asyncio.to_thread(_collect_rows)
    except Exception:  # noqa: BLE001 — never let a scan fault reach the loop
        return LivenessReplDispatchResult(
            ok=False,
            text="  [red]capability scan unavailable[/red] "
                 "[dim](the dispatch view still works: /liveness)[/dim]")
    filtered = _filter(rows, mode)
    return LivenessReplDispatchResult(
        ok=True,
        text=_render_capabilities(filtered, mode, scanned_at, len(rows)),
    )


def reset_cache_for_tests() -> None:
    """Drop the memoised scan. Test-only."""
    _scan_cache.clear()
