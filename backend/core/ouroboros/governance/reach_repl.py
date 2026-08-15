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

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from backend.core.ouroboros.governance import audit_ratchet as _ratchet
from typing import Any, Dict, List, Tuple

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


def _findings(reading: Any) -> Dict[str, Tuple[str, ...]]:
    """Reachability's two buckets, as stable keys."""
    return {
        "asymmetric": tuple(m.module for m in reading.asymmetric()),
        "orphans": tuple(m.module for m in reading.orphans()),
    }


def _legacy_buckets(data: Dict[str, Any]) -> Dict[str, Any]:
    """Read a baseline accepted before this verb adopted the ratchet.

    The old file was flat — ``{"asymmetric": [...], "orphans": [...],
    "surfaces": [...], "scanned": N}``. Named explicitly rather than inferred
    from "every key holding a list of strings", because that rule would also
    adopt ``surfaces`` as a bucket and then report a renamed surface label as
    a regression.
    """
    return {"asymmetric": list(data.get("asymmetric") or ()),
            "orphans": list(data.get("orphans") or ())}


#: Declared, therefore mounted: `audit_ratchet.run_registered_watchdogs`
#: finds this through the naming cage. The previous `run_watchdog` was
#: correct, tested, and had zero production callers — the ratchet built to
#: catch unmounted features was itself unmounted.
RATCHET = _ratchet.AuditRatchet(_ratchet.Instrument(
    name="reach",
    run=lambda: _audit(),
    findings=_findings,
    scanned=lambda r: r.scanned,
    # Unchanged on purpose: adopting the ratchet must not orphan a baseline
    # an operator already accepted.
    baseline_filename="surface_reachability_baseline.json",
    legacy_buckets=_legacy_buckets,
))


def baseline_path() -> Path:
    """Where the accepted state lives. Per-repository, never global."""
    return RATCHET.baseline_path()


def watchdog_enabled() -> bool:
    """Boot-time drift check. Default TRUE — it is silent at steady state."""
    return RATCHET.watchdog_enabled()


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

    @classmethod
    def of(cls, d: "_ratchet.Drift") -> "ReachDrift":
        """Project the generic drift onto this verb's vocabulary.

        Kept rather than replaced by `Drift`: `new_asymmetric` and
        `new_orphans` are what the operator and this module's spine both
        speak, and a generic `new["asymmetric"]` at every call site would
        make the ratchet's shape the REPL's problem.
        """
        return cls(
            new_asymmetric=d.bucket("asymmetric"),
            new_orphans=d.bucket("orphans"),
            resolved=d.resolved, baseline_exists=d.baseline_exists,
            scanned=d.scanned, error=d.error,
        )

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


async def _audit() -> Any:
    """Run the canonical audit, off the loop. Raises what callers catch.

    THE one seam. The offload was previously in `run_watchdog` alone, so
    every path that reached the audit through the REPL blocked the daemon's
    event loop for the whole scan — the watchdog was correct and the verb an
    operator actually types was not. Putting it here means no future caller
    can forget: reaching the audit at all goes through this function.
    """
    from backend.core.ouroboros.battle_test.surface_reachability import (
        audit_async,
    )
    return await audit_async()


async def accept_baseline() -> Dict[str, Any]:
    """Record the current state as accepted. NEVER raises.

    Atomicity, degradation policy and the per-repository location all live
    in `audit_ratchet` now — one implementation for every instrument, so a
    fix to the persistence lands for all of them at once.
    """
    payload = await RATCHET.accept()
    if "error" in payload:
        return payload
    # Flattened for this verb's own render, which speaks buckets by name.
    buckets = payload.get("buckets") or {}
    return {"schema_version": REACH_REPL_SCHEMA_VERSION,
            "asymmetric": list(buckets.get("asymmetric") or ()),
            "orphans": list(buckets.get("orphans") or ()),
            "scanned": payload.get("scanned", 0)}


async def drift() -> ReachDrift:
    """What regressed since the baseline. NEVER raises.

    With NO baseline this reports nothing rather than everything. A first
    run on a fresh checkout has not regressed — it has not yet been
    measured — and reporting the whole standing list as new would be the
    4:1 false-positive flood this design exists to avoid.
    """
    return ReachDrift.of(await RATCHET.drift())


async def run_watchdog() -> ReachDrift:
    """Boot-time drift check, off the event loop. NEVER raises.

    Mounted by DECLARATION: `RATCHET` above is what
    `audit_ratchet.run_registered_watchdogs` discovers. This function stays
    as the named entry point for anything that wants just this instrument.

    Silent at steady state, by construction: with nothing new since the
    baseline there is no line to print.
    """
    return ReachDrift.of(await RATCHET.watchdog())


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


async def dispatch_reach_command(line: str) -> ReachReplDispatchResult:
    """Surface-reachability audit.

    Operator: find capabilities that shipped complete and unreachable.
    """
    try:
        tokens = (line or "").strip().lstrip("/").split()
        sub = tokens[1] if len(tokens) > 1 else ""

        if sub in ("help", "-h", "--help"):
            return ReachReplDispatchResult(ok=True, text=_HELP)

        if sub == "accept":
            payload = (await accept_baseline())
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
            d = (await drift())
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
            reading = await _audit()
            rows = [f"{m.module}  ({len(m.reached_by)}/"
                    f"{len(reading.surface_labels)})"
                    for m in reading.asymmetric()]
            return ReachReplDispatchResult(
                ok=True,
                text=(f"asymmetric modules ({len(rows)} of "
                      f"{reading.scanned}):\n{_fmt(rows, cap=30)}"))

        if sub == "orphans":
            reading = await _audit()
            rows = [m.module for m in reading.orphans()]
            return ReachReplDispatchResult(
                ok=True,
                text=(f"orphans — unreached by any surface OR entry point "
                      f"({len(rows)}):\n{_fmt(rows, cap=30)}"))

        # Anything else is treated as a module name. Matched loosely on the
        # tail so an operator can type `menu_bindings` rather than the full
        # dotted path — the path is an implementation detail of the tree.
        reading = await _audit()
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
