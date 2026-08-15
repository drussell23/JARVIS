"""``/reach`` — the unmounted-feature detector, given an operator and a ratchet.

``surface_reachability`` already answers the question that matters: which of
`ov`'s rendering surfaces can actually reach a module. Its docstring names
five modules that shipped complete, tested and invisible, and PRD §28's C2
was the sixth. The detector was right about all of them.

It had no operator verb and no tests, so the instrument for finding unmounted
features was itself unmounted. This module is the fix, and it deliberately
adds no analysis: every number here comes from ``surface_reachability.audit``.

WHY A BASELINE AND NOT A REPORT
-------------------------------
The naive watchdog prints every asymmetric module at boot. That watchdog is
muted by the second week, and the detector's own history says why: of 39
modules once reported orphaned, **32 were reachable from an entry point the
audit had not been taught about**. A signal with a 4:1 false-positive rate is
noise, and an operator who learns to skip a line stops reading the line that
finally matters.

So the audit is a **ratchet**. The accepted state is recorded, and the
watchdog reports only what has changed since — a module that became
asymmetric, or newly orphaned. Steady state is silence. That inverts the
economics: silence means "nothing regressed", and any output is by
construction new.

The baseline lives in ``.jarvis/`` — per-repository, alongside the other
local metadata, because "these surfaces are mounted the way I want" is a
judgement about THIS checkout and does not travel with the branch.

Auto-discovered by ``repl_dispatch_registry`` through the naming cage, the
same way ``trust_repl`` is: a module named ``*_repl`` exposing
``dispatch_*_command`` and ``__verb_help__`` becomes a verb without a
registration table anyone can forget to update.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.ReachRepl")

REACH_REPL_SCHEMA_VERSION: str = "reach_repl.1"

__verb_help__ = {
    # Leads with a verb, not "which": the palette's description arbiter
    # classifies a fragment head as RESIDUE, and a residue row loses to any
    # mined candidate — so the shipped prose would have been replaced by a
    # scrape of this module's own subcommand names.
    "reach": "audit module reachability — find features nothing can call",
}

_HELP = (
    "/reach — surface reachability (PRD §28)\n"
    "\n"
    "  /reach                 what changed since the accepted baseline\n"
    "  /reach all             every asymmetric module, baseline or not\n"
    "  /reach orphans         unreached by any surface OR entry point\n"
    "  /reach <module>        which surfaces reach one module\n"
    "  /reach accept          record the current state as the baseline\n"
    "  /reach help            this text\n"
    "\n"
    "Asymmetry is EVIDENCE, not a verdict: a module reachable from one\n"
    "surface may be correct. It is the shape every unmounted feature had.\n"
)


def baseline_path() -> Path:
    """Where the accepted state lives. Per-repository, never global."""
    root = Path(os.environ.get("JARVIS_PROJECT_ROOT", "."))
    return Path(os.environ.get(
        "JARVIS_REACH_BASELINE_PATH",
        str(root / ".jarvis" / "surface_reachability_baseline.json"),
    ))


def watchdog_enabled() -> bool:
    """Boot-time drift check. Default TRUE — it is silent at steady state."""
    return (os.environ.get("JARVIS_REACH_WATCHDOG_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class ReachDrift:
    """What changed since the baseline was accepted."""

    new_asymmetric: Tuple[str, ...] = ()
    new_orphans: Tuple[str, ...] = ()
    resolved: Tuple[str, ...] = ()
    baseline_exists: bool = True
    scanned: int = 0
    error: str = ""

    @property
    def regressed(self) -> bool:
        return bool(self.new_asymmetric or self.new_orphans)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": REACH_REPL_SCHEMA_VERSION,
            "new_asymmetric": list(self.new_asymmetric),
            "new_orphans": list(self.new_orphans),
            "resolved": list(self.resolved),
            "baseline_exists": self.baseline_exists,
            "scanned": self.scanned, "regressed": self.regressed,
            "error": self.error,
        }


def _audit() -> Any:
    """Run the canonical audit. Raises only what the caller will catch."""
    from backend.core.ouroboros.battle_test.surface_reachability import audit
    return audit()


def _load_baseline() -> Optional[Dict[str, Any]]:
    """The accepted state, or None. NEVER raises.

    A corrupt baseline is treated as ABSENT rather than as an empty one:
    an empty baseline would report every asymmetric module as newly
    regressed, burying a real regression under a hundred false ones on the
    first boot after a disk fault.
    """
    try:
        raw = baseline_path().read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, ValueError):
        return None


def accept_baseline() -> Dict[str, Any]:
    """Record the current state as accepted. NEVER raises.

    Written through ``durable_io.atomic_replace``: a baseline half-written
    by a crash would be read as corrupt on the next boot, which degrades to
    "no baseline" and floods the operator exactly when they are already
    recovering from something.
    """
    try:
        reading = _audit()
        payload = {
            "schema_version": REACH_REPL_SCHEMA_VERSION,
            "asymmetric": sorted(m.module for m in reading.asymmetric()),
            "orphans": sorted(m.module for m in reading.orphans()),
            "surfaces": list(reading.surface_labels),
            "scanned": reading.scanned,
        }
        path = baseline_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True),
                       encoding="utf-8")
        from backend.core.ouroboros.governance.durable_io import atomic_replace
        atomic_replace(tmp, path)
        logger.info("[Reach] baseline accepted — %d asymmetric, %d orphan(s)",
                    len(payload["asymmetric"]), len(payload["orphans"]))
        return payload
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Reach] baseline write failed: %s", exc, exc_info=True)
        return {"error": str(exc)}


def drift() -> ReachDrift:
    """What regressed since the baseline. NEVER raises.

    With NO baseline this reports nothing rather than everything. A first
    run on a fresh checkout has not regressed — it has not yet been
    measured — and reporting the whole standing list as new would be the
    4:1 false-positive flood this design exists to avoid.
    """
    try:
        reading = _audit()
    except Exception as exc:  # noqa: BLE001
        return ReachDrift(error=f"{type(exc).__name__}: {exc}")
    now_asym = {m.module for m in reading.asymmetric()}
    now_orph = {m.module for m in reading.orphans()}
    base = _load_baseline()
    if base is None:
        return ReachDrift(baseline_exists=False, scanned=reading.scanned)
    was_asym = set(base.get("asymmetric") or ())
    was_orph = set(base.get("orphans") or ())
    return ReachDrift(
        new_asymmetric=tuple(sorted(now_asym - was_asym)),
        new_orphans=tuple(sorted(now_orph - was_orph)),
        resolved=tuple(sorted((was_asym | was_orph) - (now_asym | now_orph))),
        scanned=reading.scanned,
    )


async def run_watchdog() -> ReachDrift:
    """Boot-time drift check, off the event loop. NEVER raises.

    The audit walks the module tree and parses every file, which is far too
    much work for the loop that also runs the organism — so it goes to a
    thread. Bounded by the caller's own supervision rather than a timer
    here: a scan that overran would be a scan worth noticing, and killing
    it silently would hide that.

    Silent at steady state, by construction: with nothing new since the
    baseline there is no line to print.
    """
    if not watchdog_enabled():
        return ReachDrift(baseline_exists=False)
    try:
        import asyncio
        result = await asyncio.to_thread(drift)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Reach] watchdog degraded: %s", exc)
        return ReachDrift(error=str(exc))
    if result.error:
        logger.debug("[Reach] watchdog could not audit: %s", result.error)
    elif not result.baseline_exists:
        logger.info(
            "[Reach] no reachability baseline — run `/reach accept` to "
            "record the current surface topology as expected")
    elif result.regressed:
        # WARNING, not INFO: a capability that shipped unreachable is the
        # defect class this detector exists for, and it has landed six times.
        logger.warning(
            "[Reach] surface reachability REGRESSED — new asymmetric: %s; "
            "new orphans: %s. A module reachable from fewer surfaces than "
            "before is the shape every unmounted feature had.",
            ", ".join(result.new_asymmetric) or "-",
            ", ".join(result.new_orphans) or "-",
        )
    return result


@dataclass(frozen=True)
class ReachReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = REACH_REPL_SCHEMA_VERSION


def _fmt(names: List[str], cap: int = 12) -> str:
    if not names:
        return "  (none)"
    shown = names[:cap]
    tail = f"\n  … and {len(names) - cap} more" if len(names) > cap else ""
    return "\n".join(f"  {n}" for n in shown) + tail


def dispatch_reach_command(line: str) -> ReachReplDispatchResult:
    """Surface-reachability audit.

    Operator: find capabilities that shipped complete and unreachable.
    """
    try:
        tokens = (line or "").strip().lstrip("/").split()
        sub = tokens[1] if len(tokens) > 1 else ""

        if sub in ("help", "-h", "--help"):
            return ReachReplDispatchResult(ok=True, text=_HELP)

        if sub == "accept":
            payload = accept_baseline()
            if "error" in payload:
                return ReachReplDispatchResult(
                    ok=False, text=f"could not write baseline: {payload['error']}")
            return ReachReplDispatchResult(
                ok=True,
                text=(f"baseline accepted — {len(payload['asymmetric'])} "
                      f"asymmetric, {len(payload['orphans'])} orphan(s) across "
                      f"{payload['scanned']} module(s).\n"
                      f"Future drift reports only what changes from here."),
            )

        if sub in ("", "drift"):
            d = drift()
            if d.error:
                return ReachReplDispatchResult(
                    ok=False, text=f"audit unavailable: {d.error}")
            if not d.baseline_exists:
                return ReachReplDispatchResult(
                    ok=True,
                    text=("no baseline recorded — run `/reach accept` to "
                          "pin the current topology, then this reports only "
                          "what changes."))
            if not d.regressed and not d.resolved:
                return ReachReplDispatchResult(
                    ok=True,
                    text=f"reachability unchanged ({d.scanned} modules).")
            parts = [f"reachability drift ({d.scanned} modules):"]
            if d.new_asymmetric:
                parts.append("newly asymmetric:")
                parts.append(_fmt(list(d.new_asymmetric)))
            if d.new_orphans:
                parts.append("newly orphaned:")
                parts.append(_fmt(list(d.new_orphans)))
            if d.resolved:
                parts.append(f"resolved: {len(d.resolved)}")
            return ReachReplDispatchResult(ok=True, text="\n".join(parts))

        if sub in ("all", "asymmetric"):
            reading = _audit()
            rows = [f"{m.module}  ({len(m.reached_by)}/"
                    f"{len(reading.surface_labels)})"
                    for m in reading.asymmetric()]
            return ReachReplDispatchResult(
                ok=True,
                text=(f"asymmetric modules ({len(rows)} of "
                      f"{reading.scanned}):\n{_fmt(rows, cap=30)}"))

        if sub == "orphans":
            reading = _audit()
            rows = [m.module for m in reading.orphans()]
            return ReachReplDispatchResult(
                ok=True,
                text=(f"orphans — unreached by any surface OR entry point "
                      f"({len(rows)}):\n{_fmt(rows, cap=30)}"))

        # Anything else is treated as a module name. Matched loosely on the
        # tail so an operator can type `menu_bindings` rather than the full
        # dotted path — the path is an implementation detail of the tree.
        reading = _audit()
        needle = sub.strip().lower()
        hits = [m for m in reading.modules
                if needle in m.module.lower()]
        if not hits:
            return ReachReplDispatchResult(
                ok=False,
                text=(f"no module matching {sub!r} in {reading.scanned} "
                      f"scanned — try `/reach all` or `/reach help`"))
        lines = []
        for m in hits[:8]:
            reached = ", ".join(sorted(m.reached_by)) or "NOTHING"
            lines.append(f"  {m.module}\n      reached by: {reached}")
        return ReachReplDispatchResult(
            ok=True,
            text=(f"surfaces: {', '.join(reading.surface_labels)}\n"
                  + "\n".join(lines)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ReachRepl] dispatch degraded", exc_info=True)
        return ReachReplDispatchResult(
            ok=False, text=f"reach audit unavailable ({type(exc).__name__})")


__all__ = [
    "REACH_REPL_SCHEMA_VERSION",
    "ReachDrift",
    "ReachReplDispatchResult",
    "accept_baseline",
    "baseline_path",
    "dispatch_reach_command",
    "drift",
    "run_watchdog",
    "watchdog_enabled",
]
