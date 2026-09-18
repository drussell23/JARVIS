#!/usr/bin/env python3
"""Sentinel-arc soak — does the liveness sorter actually unblock the queue?

## The question this run exists to answer

`d34f2ebb5f` fixed a priority inversion: every roadmap goal carries the same
0.75 evidence weight, so `sorted(key=-weight)` was a stable sort over EQUAL
keys and fell through to DOCUMENT ORDER on an append-only roadmap. With the
Sentinel taking `candidates[0]` behind a cap of 8, the queue was a window onto
the oldest eight goals. Statically, the fix moves the one landable
production-file goal from index 26 to 4. This run asks whether that holds when
the Sentinel is live: does the liveness census fire, does the head of the queue
change, and does an op reach APPLY.

## Why this is not another soakN.sh

The numbered launchers are a RECORD — `scripts/soaks/README.md` says so, and
their machine paths are deliberate. They also each transcribe ~40 exports, and
`production_envelope.py` was written precisely because that transcription IS
the drift. So this launcher declares nothing the envelope already derives.

`production_envelope.hydrate()` uses setdefault semantics — "operator override
always wins, the envelope fills the silence" — and `ouroboros_battle_test.py`
already calls it during boot. So arming the arc is a matter of exporting ONLY
what the arc changes and letting the envelope supply the rest. Three variables
instead of forty, and when a budget ratio changes upstream this run inherits it
without being edited.

Everything else is derived, not written down: the repo root from git, the
interpreter from the running process, the session budgets from the envelope's
own profile table.

## What it refuses to do

It does not raise the candidate cap. The cap of 8 is the constraint the
inversion hid behind, and a run that widened it would prove the sorter works by
removing the thing it has to work against.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

#: Lines that answer the question. Each maps to a claim the run must support or
#: refute; a pattern with no match is reported as ABSENT rather than passed
#: over, because "the proof line never appeared" is the single most common way
#: a soak in this repo has produced a confident wrong answer.
PROOF_PATTERNS: Dict[str, str] = {
    "liveness_census": r"\[GoalDiscovery\] liveness census",
    "sentinel_pass": r"\[Sentinel\] pass \d+ starting",
    "discovery_returned": r"\[Sentinel\] pass \d+ discovery returned (\d+) candidate",
    "sentinel_outcome": r"\[Sentinel\] (landed|failed|timed_out|refused|idle)",
    "queue_starved": r"ExecutionQueueStarved",
    "dead_skipped": r"candidate\(s\) skipped as dead targets",
    "capability_probe_fault": r"CapabilityProbeFault",
    "capability_resolved": r"schema capability resolved from the SERVED model",
    "probe_recovered": r"capability probe recovered on retry",
    "sentinel_armed": r"Autonomous Sentinel armed",
    "sentinel_dormant": r"\[Sentinel\] dormant",
}

#: Anomalies that must be surfaced, never absorbed. These are the structural
#: nuances this arc is known to hit.
ANOMALY_PATTERNS: Dict[str, str] = {
    "sentinel_ignition_failed": r"\[Sentinel\] ignition failed",
    "discovery_degraded": r"\[GoalDiscovery\].*degraded",
    "dispatch_state_drift": r"STATE DRIFT",
    "duplicate_dispatch": r"already signed — re-dispatching",
    "generation_starved": r"no_candidates_returned|generation_failed",
    "worktree_conflict": r"quarantin|conflict",
}


# ---------------------------------------------------------------------------
# Resolution — nothing below is written down that can be derived
# ---------------------------------------------------------------------------

def repo_root() -> Path:
    """The working tree this launcher lives in.

    Asked of git rather than assembled from `__file__`, so a worktree, a
    submodule or a relocated checkout all answer correctly. Path arithmetic is
    the fallback, not the method — and the fallback is still derived from this
    file's own location, never from a machine path.
    """
    here = Path(__file__).resolve()
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=here.parent, capture_output=True, text=True, timeout=15,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except Exception:  # noqa: BLE001 — a tree without git still runs
        pass
    return here.parent.parent.parent


def head_commit(root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() if out.returncode == 0 else "?"
    except Exception:  # noqa: BLE001
        return "?"


def require_commit(root: Path, sha: str, why: str) -> None:
    """Refuse to run without the code under test.

    The guard IS the fix for "ran stale code" — soak 25 answered a question
    about a commit that landed 33 minutes after its launch, and Python loads a
    module once per process, so the run could never have exercised it.
    """
    try:
        out = subprocess.run(
            ["git", "merge-base", "--is-ancestor", sha, "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            sys.stderr.write(f"REFUSING: HEAD does not contain {sha} ({why})\n")
            raise SystemExit(2)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"REFUSING: could not verify {sha} ({exc})\n")
        raise SystemExit(2)


def envelope_session_budget(root: Path) -> Tuple[int, int, float]:
    """``(wall_s, idle_s, cost_cap)`` from the envelope's own profile table.

    Imported rather than copied so a change to the soak profile reaches this
    run without anyone remembering to edit it. If the import fails the run is
    REFUSED: a session budget invented here would silently diverge from the
    budgets the same envelope hands the pipeline, and a generation budget
    larger than its own pipeline budget is a deadline no op can meet.
    """
    sys.path.insert(0, str(root))
    from backend.core.ouroboros.governance.production_envelope import PROFILES
    wall, idle, cost = PROFILES["soak"]
    return int(wall), int(idle), float(cost)


def arc_overrides() -> Dict[str, str]:
    """ONLY what the Sentinel arc changes. The envelope supplies the rest.

    Each entry is a capability being armed or a contending work source being
    silenced — no budgets, no thresholds, no paths. Those are the envelope's,
    and restating one here is how the two come to disagree.
    """
    return {
        # The two switches `_start_sentinel_loop` requires. They are separable
        # capabilities by design — discovery alone files goals a human still
        # approves; sentinel alone auto-approves goals a human still writes —
        # and unattended application of self-authored code is the composition.
        # The most consequential capability in this system is deliberately not
        # reachable by setting one variable, so the launcher sets both or the
        # loop stays dormant and says so.
        "JARVIS_SENTINEL_MODE_ENABLED": "true",
        "JARVIS_GOAL_DISCOVERY_ENABLED": "true",
        # The envelope arms the WorkOrderSensor with 41 staged orders because
        # that is the sibling-entropy lineage's WORK SOURCE. This arc's work
        # source is the roadmap, read through goal discovery. Leaving both on
        # would put 41 unrelated ops into a lane clamped to one concurrent
        # generation, and the Sentinel's own dispatches — one per pass, each
        # waiting on its outcome — would be measuring queue contention rather
        # than queue ordering.
        "JARVIS_WORK_ORDER_SENSOR_ENABLED": "false",
    }


def not_overridden() -> Tuple[str, ...]:
    """Knobs deliberately left for their own modules to derive.

    Named here so a later reader can see they were considered and declined,
    which is the difference between a decision and an omission.
    """
    return (
        # `loop_interval_s()` derives this from the pipeline budget — a session
        # whose ops take an hour should not re-scan every thirty seconds.
        "JARVIS_SENTINEL_INTERVAL_S",
        # THE constraint under test. Widening it would prove the sorter works
        # by deleting the thing it works against.
        "JARVIS_GOAL_DISCOVERY_MAX_CANDIDATES",
        # `_census_budget_s()` derives it as a fraction of one pipeline budget.
        "JARVIS_GOAL_DISCOVERY_CENSUS_BUDGET_S",
        # A new FATAL path across every capability. Its own docstring says it
        # should not be armed before a soak shows what it would refuse, and
        # this is that soak, not the one after it.
        "JARVIS_CAPABILITY_ATTESTATION_ENFORCE",
    )


# ---------------------------------------------------------------------------
# Telemetry — read the session log, never trust stdout
# ---------------------------------------------------------------------------

def newest_session_dir(root: Path, after_ts: float) -> Optional[Path]:
    """The session this run created, not whatever ran last.

    Filtered by mtime against the launch instant because a stale directory from
    an earlier run is indistinguishable by name, and reporting one would
    attribute another run's telemetry to this one.
    """
    base = root / ".ouroboros" / "sessions"
    if not base.is_dir():
        return None
    candidates = [
        p for p in base.iterdir()
        if p.is_dir() and p.stat().st_mtime >= after_ts - 5
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def scan_trace(log_paths: Sequence[Path]) -> Dict[str, List[str]]:
    """Every proof and anomaly line, keyed by claim. NEVER raises.

    Reads with `errors="replace"`: a session log carries ANSI and occasionally
    a truncated multi-byte write at a kill boundary, and a decode error here
    would lose the whole trace at exactly the moment it matters most.
    """
    found: Dict[str, List[str]] = {k: [] for k in
                                   list(PROOF_PATTERNS) + list(ANOMALY_PATTERNS)}
    compiled = {
        k: re.compile(v) for k, v in
        {**PROOF_PATTERNS, **ANOMALY_PATTERNS}.items()
    }
    for path in log_paths:
        try:
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    clean = re.sub(r"\x1b\[[0-9;]*m", "", line).rstrip()
                    for key, rx in compiled.items():
                        if rx.search(clean):
                            found[key].append(clean[-400:])
        except Exception:  # noqa: BLE001 — a partial trace beats none
            continue
    return found


def render_trace(found: Dict[str, List[str]], *, root: Path) -> str:
    """The operator-facing verdict. States ABSENT explicitly."""
    out: List[str] = []
    out.append("=" * 78)
    out.append("SENTINEL ARC SOAK — TELEMETRY TRACE")
    out.append("=" * 78)

    out.append("\n--- PROOF LINES (the question this run answers) ---")
    for key in PROOF_PATTERNS:
        hits = found.get(key, [])
        if not hits:
            out.append(f"  [ABSENT] {key}")
            continue
        out.append(f"  [{len(hits):>4}x] {key}")
        for line in hits[:3]:
            out.append(f"           {line[:150]}")
        if len(hits) > 3:
            out.append(f"           ... and {len(hits) - 3} more")

    out.append("\n--- ANOMALIES (isolated and logged, never worked around) ---")
    any_anom = False
    for key in ANOMALY_PATTERNS:
        hits = found.get(key, [])
        if hits:
            any_anom = True
            out.append(f"  [{len(hits):>4}x] {key}")
            for line in hits[:2]:
                out.append(f"           {line[:150]}")
    if not any_anom:
        out.append("  none observed")

    # The liveness census is THE line this run exists to produce. Pull the
    # head target out of it: a changed head is the fix taking effect.
    census = found.get("liveness_census", [])
    if census:
        out.append("\n--- QUEUE HEAD OVER THE RUN (liveness census) ---")
        heads: Dict[str, int] = {}
        for line in census:
            m = re.search(r"head=(\S+)", line)
            if m:
                heads[m.group(1)] = heads.get(m.group(1), 0) + 1
        for target, n in sorted(heads.items(), key=lambda kv: -kv[1]):
            out.append(f"  {n:>4}x  {target}")
        out.append(f"\n  first: {census[0][-170:]}")
        out.append(f"  last : {census[-1][-170:]}")
    else:
        out.append(
            "\n--- QUEUE HEAD ---\n  [ABSENT] the liveness census never "
            "logged. Either discovery never ran a pass, or this process did "
            "not load the ranker — check HEAD contains the fix."
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Supervision — a hang must report, not hang
# ---------------------------------------------------------------------------

async def _pump(stream, sink, state: Dict[str, float], tag: str) -> None:
    """Copy *stream* to *sink*, stamping the last-output instant.

    The stamp is what turns a silent process into a reportable one: the
    watchdog reads it rather than guessing from wall time, so a long but
    PRODUCTIVE generation is never mistaken for a stall.
    """
    while True:
        try:
            raw = await stream.readline()
        except (asyncio.LimitOverrunError, ValueError):
            # A single pathological line must not kill the pump.
            state["last_output"] = time.monotonic()
            continue
        except Exception:  # noqa: BLE001
            return
        if not raw:
            return
        state["last_output"] = time.monotonic()
        try:
            sink.write(raw.decode("utf-8", errors="replace"))
            sink.flush()
        except Exception:  # noqa: BLE001
            return


async def _watchdog(proc, state: Dict[str, float], stale_s: float,
                    report) -> None:
    """Report a stall; never kill on suspicion alone.

    The organism has its own external watchdog with its own budget. A second
    killer here would race it and make every stall ambiguous about which
    supervisor ended the run. This one OBSERVES and says so — the operator (or
    the wall clock) decides.
    """
    warned = False
    while proc.returncode is None:
        await asyncio.sleep(min(60.0, max(5.0, stale_s / 10.0)))
        quiet = time.monotonic() - state["last_output"]
        if quiet >= stale_s and not warned:
            warned = True
            report(
                f"[soak] NO OUTPUT for {quiet:.0f}s — the run is quiet, not "
                f"necessarily stuck (a local generation is minutes). "
                f"Telemetry will still be captured at exit."
            )
        elif quiet < stale_s:
            warned = False


async def run_soak(args: argparse.Namespace) -> int:
    root = repo_root()
    require_commit(root, args.require, "the liveness sorter under test")
    wall_s, idle_s, cost_cap = envelope_session_budget(root)
    if args.wall_seconds:
        # Scale the idle timeout WITH the wall, preserving the profile's own
        # ratio. Measured in the 600s ignition run: overriding only the wall
        # left idle at the profile's 1800s — larger than the whole session, so
        # the idle exit could never fire and a stalled run would have burned
        # the full wall instead of ending early and saying why.
        ratio = (idle_s / wall_s) if wall_s else 0.2
        wall_s = int(args.wall_seconds)
        idle_s = max(60, int(wall_s * ratio))

    env = dict(os.environ)
    overrides = arc_overrides()
    # Operator intent wins over the launcher, exactly as the envelope lets the
    # launcher win over itself. A value already exported is never replaced.
    applied, respected = {}, []
    for name, value in overrides.items():
        if env.get(name, "").strip():
            respected.append(name)
            continue
        env[name] = value
        applied[name] = value
    runner = root / "scripts" / "ouroboros_battle_test.py"
    if not runner.is_file():
        sys.stderr.write(f"REFUSING: runner not found at {runner}\n")
        return 2

    cmd = [
        sys.executable, "-u", str(runner),
        "--headless",
        "--repo-path", str(root),
        "--cost-cap", f"{cost_cap:g}",
        "--idle-timeout", str(idle_s),
        "--max-wall-seconds", str(wall_s),
        "-v",
    ]

    started = time.time()
    log_dir = root / ".ouroboros"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_log = log_dir / f"sentinel_arc_soak_{int(started)}.log"

    banner = [
        "=" * 78,
        "SENTINEL ARC SOAK",
        "=" * 78,
        f"  repo          {root}",
        f"  HEAD          {head_commit(root)}",
        f"  interpreter   {sys.executable}",
        f"  wall / idle   {wall_s}s / {idle_s}s   cost-cap ${cost_cap:g}",
        f"  arc deltas    {', '.join(sorted(applied)) or 'none (all operator-set)'}",
        f"  operator-set  {', '.join(sorted(respected)) or 'none'}",
        f"  derived by    production_envelope (~40 vars, not restated here)",
        f"  left derived  {', '.join(not_overridden())}",
        f"  stdout log    {stdout_log}",
        "=" * 78,
    ]
    print("\n".join(banner), flush=True)

    state = {"last_output": time.monotonic()}
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(root), env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        limit=1 << 20,
    )

    def _report(msg: str) -> None:
        print(msg, flush=True)

    with stdout_log.open("w", encoding="utf-8") as sink:
        pump = asyncio.create_task(_pump(proc.stdout, sink, state, "out"))
        dog = asyncio.create_task(
            _watchdog(proc, state, float(args.stale_seconds), _report)
        )
        try:
            # +margin: the harness ends itself on its own budget. Waiting a
            # little past it distinguishes "ended gracefully" from "had to be
            # cut", and the difference decides whether the result is usable.
            await asyncio.wait_for(proc.wait(), timeout=wall_s + 600)
        except asyncio.TimeoutError:
            _report(
                "[soak] the harness outlived its own wall budget — "
                "terminating and capturing the trace anyway"
            )
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=60)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            raise
        finally:
            dog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await dog
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(pump, timeout=30)

    session = newest_session_dir(root, started)
    logs = [stdout_log]
    if session is not None:
        # READ debug.log, not stdout — the harness routes INFO/DEBUG there, and
        # the census and probe lines are exactly what stdout does not carry.
        logs.insert(0, session / "debug.log")

    found = scan_trace(logs)
    trace = render_trace(found, root=root)
    print("\n" + trace, flush=True)
    print(f"\n  session dir   {session or '[none found]'}")
    print(f"  exit code     {proc.returncode}")

    report_path = log_dir / f"sentinel_arc_trace_{int(started)}.json"
    with contextlib.suppress(Exception):
        report_path.write_text(json.dumps({
            "head": head_commit(root),
            "exit_code": proc.returncode,
            "session_dir": str(session) if session else None,
            "wall_s": wall_s,
            "arc_overrides": applied,
            "counts": {k: len(v) for k, v in found.items()},
            "census": found.get("liveness_census", [])[-50:],
        }, indent=2), encoding="utf-8")
        print(f"  trace json    {report_path}")
    return int(proc.returncode or 0)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--require", default="d34f2ebb5f",
        help="commit that must be an ancestor of HEAD (the code under test)",
    )
    parser.add_argument(
        "--wall-seconds", type=int, default=None,
        help="override the envelope's session wall budget (default: derived)",
    )
    parser.add_argument(
        "--stale-seconds", type=float, default=900.0,
        help="report (never kill) after this much silence",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(run_soak(args))
    except KeyboardInterrupt:
        sys.stderr.write("\n[soak] interrupted by operator\n")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
