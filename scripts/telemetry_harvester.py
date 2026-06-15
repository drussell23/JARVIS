#!/usr/bin/env python3
"""Sovereign Telemetry Harvester — autonomous ingest + certification of a dogfood run.

Binds to ``.ouroboros/sessions/``, auto-detects the latest session created after launch,
async-tails its ``debug.log`` during the live run, and on FSM termination (the harness
finalizing ``summary.json``) runs the Metric A/B/C parse and prints a verdict.

INTEGRITY GUARDRAIL (load-bearing): this harvester will NOT print
``FIELD-CERTIFIED: READY FOR SOVEREIGN TASKING`` unless the self-heal path was genuinely
EXERCISED — the live-fire validator fired on a kernel-touching candidate, routed it back as
``failure_class=build``, and the op recovered. A clean run where the validator never
triggered is reported as ``OPERATIONAL (SELF-HEAL UNEXERCISED)`` — operational, but NOT a
certification of self-healing, because nothing was healed. Anomalies pivot to RCA. This
mirrors the same candor the engine enforces: a green run that never tripped the gate proves
deployment, not self-repair.

Stdlib only. Run alongside the dogfood:
    python3 scripts/telemetry_harvester.py            # waits for the next session, tails, certifies
    python3 scripts/telemetry_harvester.py --deployer-stdout /tmp/deploy.txt   # also ingest BOOT CHECK
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

SESSIONS_DIR = Path(".ouroboros/sessions")

# ── grounded log patterns (verified against real debug.log + the deployed wiring) ────────
_RE_PHASE = re.compile(r"phase=[A-Z_]+")
_RE_LIVEFIRE_FAIL = re.compile(r"\[LiveFire\] candidate FAILED live-fire boot:\s*(\S+)")
_RE_FAILCLASS_BUILD = re.compile(r"failure_class[=:][\"']?build\b")
_RE_RETRY = re.compile(r"GENERATE_RETRY|VALIDATE_RETRY")
_RE_RECOVERED = re.compile(r"state=applied|state=complete|phase=COMPLETE")
_RE_GATE_INERT = re.compile(r"GATE INERT")
_RE_TIMEOUT = re.compile(r"LiveFireTimeout|live-fire exceeded")
_RE_OOM = re.compile(r"process_memory_cap|MemoryError|\bOOM\b|emergency_brake|memory_pressure_changed.*(critical|emergency)")
_TERMINAL_OUTCOMES = {"complete", "incomplete_kill"}

# verdicts
FIELD_CERTIFIED = "FIELD_CERTIFIED"
OPERATIONAL_UNEXERCISED = "OPERATIONAL_UNEXERCISED"
ANOMALY = "ANOMALY"
INCOMPLETE = "INCOMPLETE"


@dataclass
class Metrics:
    # A — deployment integrity
    booted: bool = False
    boot_check_passed: int = 0           # from optional deployer stdout
    boot_check_failed: bool = False
    # B — live-fire trajectory
    livefire_fired: List[str] = field(default_factory=list)   # exception types caught
    routed_build: bool = False
    retried: bool = False
    recovered: bool = False
    # C — hardware/state
    gate_inert: bool = False
    livefire_timeout: bool = False
    oom: bool = False
    # termination
    session_outcome: str = ""
    stop_reason: str = ""
    cost_total: Optional[float] = None
    duration_s: Optional[float] = None


@dataclass
class CertResult:
    verdict: str
    headline: str
    reasons: List[str]


def parse_metrics(log_text: str, summary: Optional[Dict], deployer_stdout: str = "") -> Metrics:
    """Pure parse — grounded in real emitted strings. No side effects."""
    m = Metrics()
    m.booted = bool(_RE_PHASE.search(log_text))
    m.boot_check_passed = deployer_stdout.count("BOOT CHECK PASSED")
    m.boot_check_failed = "BOOT CHECK FAILED" in deployer_stdout

    m.livefire_fired = _RE_LIVEFIRE_FAIL.findall(log_text)
    m.routed_build = bool(_RE_FAILCLASS_BUILD.search(log_text))
    m.retried = bool(_RE_RETRY.search(log_text))
    m.recovered = bool(_RE_RECOVERED.search(log_text))

    m.gate_inert = bool(_RE_GATE_INERT.search(log_text))
    m.livefire_timeout = bool(_RE_TIMEOUT.search(log_text))
    m.oom = bool(_RE_OOM.search(log_text))

    if summary:
        m.session_outcome = str(summary.get("session_outcome", ""))
        m.stop_reason = str(summary.get("stop_reason", ""))
        m.cost_total = summary.get("cost_total")
        m.duration_s = summary.get("duration_s")
    return m


def certify(m: Metrics) -> CertResult:
    """Strict certification gate — refuses to stamp FIELD-CERTIFIED without exercised self-heal."""
    # Hard anomalies first — any of these is disqualifying.
    if m.boot_check_failed:
        return CertResult(ANOMALY, "ANOMALY — deployer BOOT CHECK FAILED",
                          ["A deployer auto-reverted; the wiring is not in place. Re-run deploy."])
    if m.gate_inert:
        return CertResult(ANOMALY, "ANOMALY — 'GATE INERT' present (stale wiring)",
                          ["GATE INERT is structurally impossible in the merged build; its presence "
                           "means an un-updated deployer wrote the pre-frozen-fix hook. Re-deploy from main."])
    if m.oom:
        return CertResult(ANOMALY, "ANOMALY — memory cap / OOM / pressure event",
                          ["The 16GB invariant was violated (process_memory_cap / OOM / emergency brake)."])

    # Run must have actually finished cleanly.
    if m.session_outcome not in _TERMINAL_OUTCOMES or not m.session_outcome:
        return CertResult(INCOMPLETE, "INCOMPLETE — run not finished / no finalized summary",
                          [f"session_outcome={m.session_outcome or 'n/a'} stop_reason={m.stop_reason or 'n/a'}; "
                           "harvest again after the FSM terminates."])
    if m.session_outcome == "incomplete_kill":
        return CertResult(INCOMPLETE, f"INCOMPLETE — session killed ({m.stop_reason})",
                          [f"stop_reason={m.stop_reason}; partial summary only."])
    if not m.booted:
        return CertResult(ANOMALY, "ANOMALY — no phase activity in debug.log",
                          ["Orchestrator never ran a phase; boot likely failed on the injected hooks."])

    # Self-heal EXERCISED?  This is the integrity guardrail.
    if not m.livefire_fired:
        return CertResult(OPERATIONAL_UNEXERCISED,
                          "OPERATIONAL (SELF-HEAL UNEXERCISED) — gate armed, never triggered",
                          ["Run completed cleanly but NO kernel-touching candidate reached the live-fire "
                           "gate, so the self-heal path was never demonstrated. This proves deployment + "
                           "stability, NOT self-repair. Submit a kernel-touching GOAL to certify."])
    # Validator fired — now demand the full trajectory.
    if not (m.routed_build and m.retried):
        return CertResult(ANOMALY,
                          "ANOMALY — validator fired but did not route back correctly",
                          [f"[LiveFire] caught {m.livefire_fired} but "
                           f"routed_build={m.routed_build} retried={m.retried}. "
                           "The frozen-ValidationResult rebind or retry routing did not engage — RCA the "
                           "VALIDATE choke point."])
    if not m.recovered:
        return CertResult(OPERATIONAL_UNEXERCISED,
                          "PARTIAL — self-test fired + routed, but op did not recover",
                          ["Live-fire correctly caught + routed the failure as build, but no subsequent "
                           "state=applied/complete was observed. Self-TEST proven; self-HEAL incomplete "
                           "(model may not have produced a passing fix within retries)."])

    # Full self-heal trajectory observed.
    return CertResult(FIELD_CERTIFIED, "FIELD-CERTIFIED: READY FOR SOVEREIGN TASKING",
                      [f"Live-fire fired ({', '.join(m.livefire_fired)}), routed back as build, retried, "
                       "and the op recovered (state=applied/complete). Self-test + self-heal proven on a "
                       "live LLM-driven run."])


def render_report(m: Metrics, cert: CertResult) -> str:
    cost = f"${m.cost_total:.4f}" if isinstance(m.cost_total, (int, float)) else "n/a"
    dur = f"{m.duration_s:.0f}s" if isinstance(m.duration_s, (int, float)) else "n/a"
    lines = [
        "=" * 72,
        "  J.A.R.M.A.T.R.I.X. — Sovereign Telemetry Harvest Report",
        "=" * 72,
        f"  Metric A · Deployment : booted={m.booted} boot_check_passed={m.boot_check_passed} "
        f"boot_check_failed={m.boot_check_failed}",
        f"  Metric B · LiveFire   : fired={m.livefire_fired or 'none'} routed_build={m.routed_build} "
        f"retried={m.retried} recovered={m.recovered}",
        f"  Metric C · State      : gate_inert={m.gate_inert} livefire_timeout={m.livefire_timeout} "
        f"oom={m.oom}",
        f"  Session               : outcome={m.session_outcome or 'n/a'} stop={m.stop_reason or 'n/a'} "
        f"cost={cost} dur={dur}",
        "-" * 72,
        f"  VERDICT: {cert.headline}",
    ]
    for r in cert.reasons:
        lines.append(f"    • {r}")
    lines.append("=" * 72)
    return "\n".join(lines)


# ── async watcher ────────────────────────────────────────────────────────────────────────
def find_latest_session(sessions_dir: Path, since_ts: float) -> Optional[Path]:
    if not sessions_dir.is_dir():
        return None
    cands = [d for d in sessions_dir.iterdir()
             if d.is_dir() and d.name.startswith("bt-") and d.stat().st_mtime >= since_ts - 1]
    return max(cands, key=lambda d: d.stat().st_mtime, default=None)


def read_summary(session_dir: Path) -> Optional[Dict]:
    p = session_dir / "summary.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _is_terminal(summary: Optional[Dict]) -> bool:
    return bool(summary) and str(summary.get("session_outcome", "")) in _TERMINAL_OUTCOMES


async def harvest(*, sessions_dir: Path, since_ts: float, deployer_stdout: str,
                  timeout_s: float, poll_s: float) -> int:
    deadline = time.monotonic() + timeout_s
    session: Optional[Path] = None
    print(f"[harvester] watching {sessions_dir}/ for a session started after launch …", flush=True)
    while session is None:
        session = find_latest_session(sessions_dir, since_ts)
        if session is None:
            if time.monotonic() > deadline:
                print("[harvester] TIMEOUT — no new session appeared.", file=sys.stderr)
                return 3
            await asyncio.sleep(poll_s)
    print(f"[harvester] bound to {session.name}; tailing debug.log …", flush=True)

    log_path = session / "debug.log"
    last_size = 0
    while True:
        summary = read_summary(session)
        if log_path.is_file():
            sz = log_path.stat().st_size
            if sz > last_size:           # tail: report progress, keep full text for final parse
                last_size = sz
        if _is_terminal(summary):
            outcome = summary.get("session_outcome") if summary else "?"
            print(f"[harvester] FSM terminated (outcome={outcome}). Parsing …", flush=True)
            break
        if time.monotonic() > deadline:
            print("[harvester] TIMEOUT — FSM did not finalize summary.json in time. "
                  "Parsing partial state.", file=sys.stderr)
            break
        await asyncio.sleep(poll_s)

    log_text = log_path.read_text(errors="replace") if log_path.is_file() else ""
    m = parse_metrics(log_text, read_summary(session), deployer_stdout)
    cert = certify(m)
    print(render_report(m, cert))
    return 0 if cert.verdict == FIELD_CERTIFIED else (1 if cert.verdict == ANOMALY else 2)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Sovereign Telemetry Harvester")
    ap.add_argument("--sessions-dir", default=str(SESSIONS_DIR))
    ap.add_argument("--deployer-stdout", default="", help="optional file with deployer BOOT CHECK output")
    ap.add_argument("--timeout", type=float, default=3600.0, help="max seconds to wait (default 1h)")
    ap.add_argument("--poll", type=float, default=2.0)
    args = ap.parse_args(argv)
    dep = ""
    if args.deployer_stdout and Path(args.deployer_stdout).is_file():
        dep = Path(args.deployer_stdout).read_text(errors="replace")
    return asyncio.run(harvest(
        sessions_dir=Path(args.sessions_dir), since_ts=time.time(),
        deployer_stdout=dep, timeout_s=args.timeout, poll_s=args.poll,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
