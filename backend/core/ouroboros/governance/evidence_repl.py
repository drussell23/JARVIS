"""``/evidence`` — what do the tests actually observe? PRD §28.

``source_assertion_audit`` measures the one number that explains every other
darkness finding: how many tests **cannot fail for a behavioural reason**. A
test that reads a source file and asserts a string appears in it passes while
the feature behind it is dead, and three such tests certified that
``/narrate verbose`` worked by confirming the handler's SOURCE mentioned a
flag no code read.

THIS MODULE WAS THE FINDING
---------------------------
The instrument shipped complete, with a full test file, and **zero
production callers**. Its own docstring anticipated this — *"an instrument
that measures dead code and is not itself covered is subject to its own
criterion"* — and it was right about the wrong axis: being covered did not
save it, because nothing an operator could type ever reached it. It was one
of the two real orphans `/reach` found.

So the mount is the point. Declaring ``__verb_help__`` and
``dispatch_evidence_command`` makes ``/evidence`` typeable through the
naming cage; declaring ``RATCHET`` makes its boot watchdog run. Neither
requires an edit anywhere else, which is the only property that would have
prevented the original defect.

WHY A RATE AND NOT A LIST
-------------------------
The reportable class is ``SOURCE_ONLY`` — every assertion in the function
touches source text, so nothing was executed. That is decidable rather than
a judgement, which is why this is a measurement and not a lint. Source
assertions are not wrong; some invariants are genuinely structural and
behaviour cannot express them. The pathology is the source assertion as the
**only** evidence.

The ratchet therefore watches the SET, not the rate: a rate that improves
because someone deleted tests is not progress, and a rate that worsens
because the suite grew is not a regression. A newly source-only test is.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from backend.core.ouroboros.governance import audit_ratchet as _ratchet

logger = logging.getLogger("Ouroboros.EvidenceRepl")

EVIDENCE_REPL_SCHEMA_VERSION: str = "evidence_repl.1"

__verb_help__ = {
    "evidence": "measure what the tests can actually observe, not what they name",
}

_HELP = (
    "/evidence — what do the tests actually observe? (PRD §28)\n"
    "\n"
    "  /evidence              what changed since the accepted baseline\n"
    "  /evidence rate         the headline number, with the breakdown\n"
    "  /evidence all          every test that can only ever pass\n"
    "  /evidence flags        the `/narrate verbose` class — source-only\n"
    "                         tests asserting an env-flag literal\n"
    "  /evidence <module>     source-only tests under a path fragment\n"
    "  /evidence accept       record the current state as the baseline\n"
    "  /evidence help         this text\n"
    "\n"
    "A source assertion is not a defect. A source assertion as the ONLY\n"
    "evidence is: that test cannot fail for a behavioural reason, and it\n"
    "will pass forever after the feature it names stops working.\n"
)


async def _audit() -> Any:
    """Run the canonical audit, off the loop. THE one seam.

    Every path here reaches the numbers through this function, so the
    offload cannot be forgotten by a future caller — the omission that let
    `/reach` block the daemon for the length of its own scan.
    """
    from backend.core.ouroboros.battle_test.source_assertion_audit import (
        audit_async,
    )
    return await audit_async()


def _key(verdict: Any) -> str:
    """A finding's stable identity across runs.

    ``module::name``, deliberately WITHOUT the line number: a test that
    everything above it shifted is the same test, and including the line
    would report the whole file as newly source-only after one insertion.
    """
    return f"{verdict.module}::{verdict.name}"


def _findings(reading: Any) -> Dict[str, Tuple[str, ...]]:
    """Two buckets, the second a strict subset of the first.

    Kept separate because they call for different actions: a source-only
    test is worth revisiting; a source-only test asserting a FLAG LITERAL is
    the exact shape that certified a dead feature, and is worth revisiting
    first.
    """
    return {
        "source_only": tuple(_key(v) for v in reading.source_only),
        "flag_literal": tuple(_key(v) for v in reading.flag_literal_tests),
    }


#: Declared, therefore mounted — `audit_ratchet` finds this through the same
#: naming cage that mounts the verb. No boot-seam edit, which is the only
#: property that would have prevented this module's own orphaning.
RATCHET = _ratchet.AuditRatchet(_ratchet.Instrument(
    name="evidence",
    run=lambda: _audit(),
    findings=_findings,
    scanned=lambda r: r.total,
    baseline_filename="source_assertion_baseline.json",
))


def baseline_path():
    return RATCHET.baseline_path()


def watchdog_enabled() -> bool:
    return RATCHET.watchdog_enabled()


async def run_watchdog() -> _ratchet.Drift:
    """Boot-time drift check. NEVER raises. Silent at steady state."""
    return await RATCHET.watchdog()


@dataclass(frozen=True)
class EvidenceReplDispatchResult:
    ok: bool
    text: str
    matched: bool = True
    schema_version: str = EVIDENCE_REPL_SCHEMA_VERSION


def _fmt(rows: List[str], cap: int = 12) -> str:
    if not rows:
        return "  (none)"
    shown = rows[:cap]
    tail = f"\n  … and {len(rows) - cap} more" if len(rows) > cap else ""
    return "\n".join(f"  {r}" for r in shown) + tail


def _row(verdict: Any) -> str:
    flags = (" [" + ",".join(verdict.flag_literals) + "]"
             if verdict.flag_literals else "")
    return f"{verdict.module}:{verdict.lineno} {verdict.name}{flags}"


async def dispatch_evidence_command(line: str) -> EvidenceReplDispatchResult:
    """Ask what the suite can actually see.

    Async because the audit parses every test file in the repo. A verb that
    does real work synchronously freezes the loop that runs the organism for
    the length of its own computation.
    """
    try:
        raw = (line or "").strip().lstrip("/")
        tokens = raw.split()
        sub = tokens[1].strip().lower() if len(tokens) > 1 else ""

        if sub in ("help", "-h", "--help"):
            return EvidenceReplDispatchResult(ok=True, text=_HELP)

        if sub == "accept":
            payload = await RATCHET.accept()
            if "error" in payload:
                return EvidenceReplDispatchResult(
                    ok=False,
                    text=f"could not write baseline: {payload['error']}")
            buckets = payload.get("buckets") or {}
            return EvidenceReplDispatchResult(
                ok=True,
                text=(f"baseline accepted — "
                      f"{len(buckets.get('source_only') or ())} source-only "
                      f"({len(buckets.get('flag_literal') or ())} of them "
                      f"flag-literal) across {payload.get('scanned', 0)} "
                      f"test(s).\nFuture drift reports only what changes "
                      f"from here."))

        if sub in ("", "drift"):
            d = await RATCHET.drift()
            if d.error:
                return EvidenceReplDispatchResult(
                    ok=False, text=f"audit unavailable: {d.error}")
            if not d.baseline_exists:
                return EvidenceReplDispatchResult(
                    ok=True,
                    text=("no baseline recorded — run `/evidence accept` to "
                          "pin the current state, then this reports only "
                          "what changes."))
            if not d.regressed and not d.resolved:
                return EvidenceReplDispatchResult(
                    ok=True,
                    text=(f"unchanged since the baseline "
                          f"({d.scanned} test(s) scanned)."))
            parts: List[str] = []
            if d.bucket("source_only"):
                parts.append("newly source-only — these can no longer fail "
                             "for a behavioural reason:")
                parts.append(_fmt(list(d.bucket("source_only"))))
            if d.bucket("flag_literal"):
                parts.append("newly flag-literal (the `/narrate verbose` "
                             "class):")
                parts.append(_fmt(list(d.bucket("flag_literal"))))
            if d.resolved:
                parts.append(f"resolved: {len(d.resolved)}")
            return EvidenceReplDispatchResult(ok=True, text="\n".join(parts))

        reading = await _audit()

        if sub == "rate":
            from backend.core.ouroboros.battle_test.source_assertion_audit import (  # noqa: E501
                render,
            )
            # Rendering delegated to the instrument: a second summary here
            # would be a second opinion about the same reading.
            return EvidenceReplDispatchResult(
                ok=True, text="\n".join(render(reading, limit=0)))

        if sub in ("all", "tests"):
            rows = [_row(v) for v in reading.source_only]
            return EvidenceReplDispatchResult(
                ok=True,
                text=(f"tests that can only ever pass ({len(rows)} of "
                      f"{reading.total}):\n{_fmt(rows, cap=30)}"))

        if sub == "flags":
            rows = [_row(v) for v in reading.flag_literal_tests]
            return EvidenceReplDispatchResult(
                ok=True,
                text=(f"source-only tests asserting a flag literal "
                      f"({len(rows)}) — the exact shape that certified a "
                      f"dead feature:\n{_fmt(rows, cap=30)}"))

        # Anything else is a path fragment, matched loosely so an operator
        # can type `governance` rather than a full relative path.
        needle = sub
        hits = [v for v in reading.source_only
                if needle in v.module.lower() or needle in v.name.lower()]
        if not hits:
            return EvidenceReplDispatchResult(
                ok=False,
                text=(f"no source-only test matching {sub!r} in "
                      f"{reading.total} scanned — try `/evidence all` or "
                      f"`/evidence help`"))
        return EvidenceReplDispatchResult(
            ok=True,
            text=(f"source-only tests matching {sub!r} ({len(hits)}):\n"
                  f"{_fmt([_row(v) for v in hits], cap=30)}"))
    except Exception as exc:  # noqa: BLE001
        logger.debug("[Evidence] dispatch degraded", exc_info=True)
        return EvidenceReplDispatchResult(
            ok=False, text=f"evidence audit unavailable ({type(exc).__name__})")


__all__ = [
    "EVIDENCE_REPL_SCHEMA_VERSION",
    "RATCHET",
    "EvidenceReplDispatchResult",
    "baseline_path",
    "dispatch_evidence_command",
    "run_watchdog",
    "watchdog_enabled",
]
