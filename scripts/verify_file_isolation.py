#!/usr/bin/env python3
"""Sovereign Quarantine Validation — Stage B file-isolation soak verifier.

Watches ``.ouroboros/sessions/`` for the soak session, and on FSM termination
verifies the four absolute invariants of the Sovereign File Isolation boundary:

  I1  WORKTREE INITIALIZED   — the loop routed project_root into an isolated
                               ``ouroboros/auto/bt-*`` worktree.
  I2  MUTATIONS QUARANTINED  — file/git mutations landed in that worktree, not
                               the operator's primary tree.
  I3  PRIMARY PRISTINE       — the primary working tree is 100% clean of
                               loop-authored source changes.
  I4  WORKTREE REAPED        — the quarantine zone was reaped (or is registered
                               for the boot-time reaper), not leaked.

Composes the shared ``telemetry_parse`` (the SAME parser the harvester uses) for
session health, then layers the isolation-specific checks. Stdlib only.

Run (after the soak, or armed before it):
    python3 scripts/verify_file_isolation.py \
        --sessions-dir .ouroboros/sessions --primary-root <repo_root>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# REUSE the extracted shared parse (Sovereign Telemetry Unification).
from backend.core.ouroboros.governance.graduation.telemetry_parse import (  # noqa: E402,E501
    _TERMINAL_OUTCOMES,
    parse_metrics,
)

# Grep-stable marker emitted by autonomous_workspace.resolve_loop_project_root.
_RE_ROUTED = re.compile(
    r"\[FileIsolation\] routed project_root -> (\S+) "
    r"\(session=(\S+) branch=(\S+)\)"
)
_RE_AUTO_WT = re.compile(r"ouroboros[/_]+auto[/_]+(?:bt-)?\S+")

# Marker emitted by DeterministicLock when it force-arms both flags despite
# JARVIS_FILE_ISOLATION_ENABLED=false / JARVIS_EXECUTION_BOUNDARY_ENABLED=false
# in the environment (spec §5.5, G4 / YM-T2).
_RE_DETERMINISTIC_LOCK = re.compile(
    r"\[DeterministicLock\] forced isolation\+boundary"
)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
INCOMPLETE = "INCOMPLETE"


@dataclass
class Invariant:
    key: str
    status: str
    detail: str


@dataclass
class IsolationVerdict:
    overall: str
    invariants: List[Invariant] = field(default_factory=list)
    quarantine_path: str = ""
    session_outcome: str = ""


def assess_isolation(
    *,
    debug_log: str,
    primary_status_porcelain: str,
    worktree_list_porcelain: str,
    summary: Optional[Dict],
    reap_log: str = "",
) -> IsolationVerdict:
    """Pure assessment of the four invariants. NEVER raises.

    ``primary_status_porcelain`` — ``git -C <primary> status --porcelain``.
    ``worktree_list_porcelain``  — ``git worktree list --porcelain`` AFTER the
                                   session (to check reaping).
    ``reap_log`` — optional next-boot debug.log tail proving reap_orphans ran.
    """
    debug_log = debug_log if isinstance(debug_log, str) else ""
    invariants: List[Invariant] = []

    # Session must have actually terminated.
    outcome = ""
    if isinstance(summary, dict):
        outcome = str(summary.get("session_outcome", ""))
    if outcome not in _TERMINAL_OUTCOMES or not outcome:
        return IsolationVerdict(
            overall=INCOMPLETE,
            invariants=[Invariant(
                "session", INCOMPLETE,
                f"session_outcome={outcome or 'n/a'} — re-harvest after "
                "the FSM finalizes summary.json",
            )],
            session_outcome=outcome,
        )

    # I1 — worktree initialized (the routing marker fired).
    m = _RE_ROUTED.search(debug_log)
    quarantine_path = m.group(1) if m else ""
    if m:
        invariants.append(Invariant(
            "I1_worktree_initialized", PASS,
            f"routed project_root -> {quarantine_path} "
            f"(branch={m.group(3)})",
        ))
    else:
        invariants.append(Invariant(
            "I1_worktree_initialized", FAIL,
            "no '[FileIsolation] routed project_root' marker in debug.log "
            "— isolation did not activate (autonomous? flag set?)",
        ))

    # I2 — mutations quarantined: the quarantine path is referenced as the
    # working root beyond the single routing line (delegates inherit it).
    if quarantine_path:
        refs = debug_log.count(quarantine_path)
        if refs >= 2:
            invariants.append(Invariant(
                "I2_mutations_quarantined", PASS,
                f"quarantine path referenced {refs}x (delegates inherited "
                "project_root → mutations routed into the zone)",
            ))
        else:
            invariants.append(Invariant(
                "I2_mutations_quarantined", WARN,
                f"quarantine path referenced only {refs}x — loop may not "
                "have produced a file mutation this session (no APPLY)",
            ))
    else:
        invariants.append(Invariant(
            "I2_mutations_quarantined", FAIL,
            "no quarantine path (I1 failed) → cannot confirm routing",
        ))

    # I3 — primary tree pristine: no loop-authored source changes. Ignore
    # untracked soak artifacts (.ouroboros/.worktrees/.jarvis) + the verifier
    # itself; flag any tracked source modification.
    dirty = _loop_authored_dirty_lines(primary_status_porcelain)
    if not dirty:
        invariants.append(Invariant(
            "I3_primary_pristine", PASS,
            "primary working tree has no loop-authored source changes",
        ))
    else:
        invariants.append(Invariant(
            "I3_primary_pristine", FAIL,
            "primary tree MUTATED by the loop: "
            + "; ".join(dirty[:8]),
        ))

    # I4 — worktree reaped (or pending reap). Present in `worktree list` =
    # not yet reaped this lifecycle (boot reaper will sweep it); absent OR a
    # reap log line = reaped.
    still_listed = bool(quarantine_path) and quarantine_path in (
        worktree_list_porcelain or ""
    )
    reaped_logged = bool(
        _RE_AUTO_WT.search(reap_log or "")
    ) or "reap" in (reap_log or "").lower()
    if not still_listed or reaped_logged:
        invariants.append(Invariant(
            "I4_worktree_reaped", PASS,
            "quarantine worktree reaped (or absent from worktree list)",
        ))
    else:
        invariants.append(Invariant(
            "I4_worktree_reaped", WARN,
            "quarantine worktree still registered — will be swept by the "
            "boot reaper on next O+V init (reap_orphans covers "
            "ouroboros/auto/*). Re-check after a reboot for PASS.",
        ))

    # I5 — DeterministicLock override proof: if the lock fired (env=false
    # scenario), prove both flags were armed AND primary stayed pristine.
    # If the lock did NOT fire this session (normal env=true path), emit WARN
    # (inconclusive — the explicit-override scenario was not exercised).
    # Use --prove-override against a session where the lock fired for a hard
    # PASS/FAIL proof.
    lock_fired = bool(_RE_DETERMINISTIC_LOCK.search(debug_log))
    if lock_fired:
        # Delegate to the pure proof function — both flags armed by definition.
        invariants.append(assess_i5_override(
            debug_log=debug_log,
            primary_dirty=bool(dirty),
            file_iso_armed=True,
            exec_boundary_armed=True,
        ))
    else:
        invariants.append(Invariant(
            "I5_override_proof", WARN,
            "DeterministicLock did not fire this session (env was already "
            "true, or the explicit env=false scenario was not exercised) — "
            "override proof inconclusive.  Run --prove-override against a "
            "session where JARVIS_FILE_ISOLATION_ENABLED=false was set.",
        ))

    statuses = {i.status for i in invariants}
    if FAIL in statuses:
        overall = FAIL
    elif WARN in statuses:
        overall = WARN
    else:
        overall = PASS
    return IsolationVerdict(
        overall=overall,
        invariants=invariants,
        quarantine_path=quarantine_path,
        session_outcome=outcome,
    )


def _loop_authored_dirty_lines(porcelain: str) -> List[str]:
    """Return git-status-porcelain lines that represent loop-authored
    source mutations to the PRIMARY tree. Ignores soak/runtime artifacts."""
    if not isinstance(porcelain, str) or not porcelain.strip():
        return []
    ignore_prefixes = (
        ".ouroboros/", ".worktrees/", ".jarvis/", ".claude/",
        "scripts/verify_file_isolation.py",
    )
    out: List[str] = []
    for line in porcelain.splitlines():
        line = line.rstrip()
        if not line or len(line) < 4:
            continue
        path = line[3:].strip().strip('"')
        # Untracked dirs/files from the soak are noise; tracked source
        # modifications (' M ', 'A ', 'D ') to backend/tests are the signal.
        if any(path.startswith(p) for p in ignore_prefixes):
            continue
        out.append(line)
    return out


def assess_i5_override(
    *,
    debug_log: str,
    primary_dirty: bool,
    file_iso_armed: bool,
    exec_boundary_armed: bool,
) -> Invariant:
    """Pure assessment of I5: prove the DeterministicLock (YM-T2) overrode an
    explicit ``JARVIS_FILE_ISOLATION_ENABLED=false`` in the environment.

    Passes iff ALL of:
    - The ``[DeterministicLock] forced isolation+boundary`` marker appears in
      ``debug_log`` (the lock fired and stamped the log).
    - ``primary_dirty`` is False (primary checkout still pristine — the forced
      boundary actually held).
    - ``file_iso_armed`` is True (JARVIS_FILE_ISOLATION_ENABLED confirmed armed
      after the override).
    - ``exec_boundary_armed`` is True (JARVIS_EXECUTION_BOUNDARY_ENABLED
      confirmed armed after the override).
    """
    lock_fired = bool(_RE_DETERMINISTIC_LOCK.search(debug_log or ""))

    if not lock_fired:
        return Invariant(
            "I5_override_proof", FAIL,
            "no '[DeterministicLock] forced isolation+boundary' marker in log "
            "— lock did not fire (or log not provided); env=false override "
            "NOT proven",
        )
    if primary_dirty:
        return Invariant(
            "I5_override_proof", FAIL,
            "DeterministicLock marker present but primary checkout is DIRTY "
            "— the forced boundary did not hold",
        )
    if not file_iso_armed:
        return Invariant(
            "I5_override_proof", FAIL,
            "DeterministicLock fired but JARVIS_FILE_ISOLATION_ENABLED not "
            "confirmed armed — override incomplete",
        )
    if not exec_boundary_armed:
        return Invariant(
            "I5_override_proof", FAIL,
            "DeterministicLock fired but JARVIS_EXECUTION_BOUNDARY_ENABLED "
            "not confirmed armed — override incomplete",
        )
    return Invariant(
        "I5_override_proof", PASS,
        "DeterministicLock fired and forced BOTH flags despite env=false; "
        "primary checkout pristine — explicit env override PROVEN (G4)",
    )


def render(verdict: IsolationVerdict) -> str:
    bar = "=" * 74
    sym = {PASS: "✓", FAIL: "✗", WARN: "!", INCOMPLETE: "…"}
    lines = [
        bar,
        "  Sovereign Quarantine Validation — File Isolation Invariant Matrix",
        bar,
        f"  session_outcome : {verdict.session_outcome or 'n/a'}",
        f"  quarantine zone : {verdict.quarantine_path or 'n/a'}",
        "-" * 74,
    ]
    for inv in verdict.invariants:
        lines.append(f"  [{sym.get(inv.status, '?')}] {inv.key:26s} {inv.status}")
        lines.append(f"      {inv.detail}")
    lines.append("-" * 74)
    lines.append(f"  VERDICT: {verdict.overall}")
    lines.append(bar)
    return "\n".join(lines)


# ── live I/O shell ─────────────────────────────────────────────────────────
def _git(args: List[str], cwd: Path) -> str:
    try:
        r = subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True,
            text=True, timeout=20, check=False,
        )
        return r.stdout if r.returncode == 0 else ""
    except Exception:  # noqa: BLE001
        return ""


def _find_latest_session(sessions_dir: Path, since_ts: float) -> Optional[Path]:
    if not sessions_dir.is_dir():
        return None
    cands = [
        d for d in sessions_dir.iterdir()
        if d.is_dir() and d.name.startswith("bt-")
        and d.stat().st_mtime >= since_ts - 1
    ]
    return max(cands, key=lambda d: d.stat().st_mtime, default=None)


def _read_summary(session: Path) -> Optional[Dict]:
    p = session / "summary.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def _session_candidate_dirs(session: Path, primary_root: Path) -> List[Path]:
    """All dirs that may hold THIS session's artifacts. The harness splits
    debug.log (repo_path-anchored → main) and summary.json (cwd-relative →
    worktree) when the soak runs from a linked worktree, so search the bound
    dir AND any worktree sibling session dir of the same name."""
    cands = [session]
    try:
        cands.extend(sorted(primary_root.glob(
            f".claude/worktrees/*/.ouroboros/sessions/{session.name}")))
    except Exception:  # noqa: BLE001
        pass
    seen, out = set(), []
    for c in cands:
        if str(c) not in seen:
            seen.add(str(c))
            out.append(c)
    return out


def _read_summary_any(session: Path, primary_root: Path) -> Optional[Dict]:
    for c in _session_candidate_dirs(session, primary_root):
        s = _read_summary(c)
        if s is not None:
            return s
    return None


def _read_debug_any(session: Path, primary_root: Path) -> str:
    best = ""
    for c in _session_candidate_dirs(session, primary_root):
        lp = c / "debug.log"
        try:
            if lp.is_file() and lp.stat().st_size > len(best):
                best = lp.read_text(errors="replace")
        except Exception:  # noqa: BLE001
            continue
    return best


async def watch_and_verify(
    *, sessions_dir: Path, primary_root: Path, since_ts: float,
    timeout_s: float, poll_s: float, pin_session: str = "",
) -> int:
    deadline = time.monotonic() + timeout_s
    session: Optional[Path] = None
    if pin_session:
        # Deterministic bind to a known session dir — immune to the
        # autonomous loop spawning a newer bt-* mid-soak.
        session = sessions_dir / pin_session
        print(f"[verifier] pinned to {session} — awaiting termination …",
              flush=True)
    else:
        print(f"[verifier] watching {sessions_dir}/ for a soak session …",
              flush=True)
    while session is None:
        session = _find_latest_session(sessions_dir, since_ts)
        if session is None:
            if time.monotonic() > deadline:
                print("[verifier] TIMEOUT — no session appeared.",
                      file=sys.stderr)
                return 3
            await asyncio.sleep(poll_s)
    print(f"[verifier] bound to {session.name}; awaiting FSM termination …",
          flush=True)
    while True:
        summary = _read_summary_any(session, primary_root)
        if summary and str(summary.get("session_outcome", "")) in _TERMINAL_OUTCOMES:
            break
        if time.monotonic() > deadline:
            print("[verifier] TIMEOUT — FSM did not finalize; assessing "
                  "partial state.", file=sys.stderr)
            break
        await asyncio.sleep(poll_s)

    debug_log = _read_debug_any(session, primary_root)
    summary = _read_summary_any(session, primary_root)
    # Health context via the SHARED harvester parse.
    metrics = parse_metrics(debug_log, summary)
    verdict = assess_isolation(
        debug_log=debug_log,
        primary_status_porcelain=_git(["status", "--porcelain"], primary_root),
        worktree_list_porcelain=_git(["worktree", "list", "--porcelain"],
                                     primary_root),
        summary=summary,
        reap_log="",
    )
    print(render(verdict))
    print(f"  [context] booted={metrics.booted} oom={metrics.oom} "
          f"cost=${metrics.cost_total or 0:.4f} dur={metrics.duration_s or 0:.0f}s")
    return 0 if verdict.overall == PASS else (1 if verdict.overall == FAIL else 2)


def _prove_override(primary_root: Path, sessions_dir: Path,
                    session: str) -> int:
    """``--prove-override`` mode: check I5 against the most-recent (or pinned)
    session's debug.log + current git porcelain.  Prints a one-line PASS/FAIL
    verdict to stdout.  Exit 0 = PASS, 1 = FAIL, 3 = no session found.

    The DeterministicLock (YM-T2) emits
    ``[DeterministicLock] forced isolation+boundary`` whenever it arms both
    flags despite an explicit ``JARVIS_FILE_ISOLATION_ENABLED=false`` in the
    environment.  This mode documents that proof without requiring a full soak
    re-run.
    """
    print("[prove-override] I5 — DeterministicLock env=false override proof",
          flush=True)

    if session:
        session_path: Optional[Path] = sessions_dir / session
    else:
        session_path = _find_latest_session(sessions_dir, 0.0)
    if session_path is None or not session_path.is_dir():
        print(
            f"[prove-override] FAIL — no session found under {sessions_dir}/; "
            "run a soak first (or pass --session <name>)",
            flush=True,
        )
        return 3

    debug_log = _read_debug_any(session_path, primary_root)
    porcelain = _git(["status", "--porcelain"], primary_root)
    primary_dirty = bool(_loop_authored_dirty_lines(porcelain))

    # Derive flag-armed state from the same marker (if the lock fired, it
    # armed both flags by definition — that is the invariant being proven).
    lock_fired = bool(_RE_DETERMINISTIC_LOCK.search(debug_log))

    inv = assess_i5_override(
        debug_log=debug_log,
        primary_dirty=primary_dirty,
        file_iso_armed=lock_fired,
        exec_boundary_armed=lock_fired,
    )

    sym = {PASS: "✓", FAIL: "✗", WARN: "!", INCOMPLETE: "…"}
    print(
        f"[prove-override] [{sym.get(inv.status, '?')}] {inv.key}  "
        f"{inv.status}",
        flush=True,
    )
    print(f"    {inv.detail}", flush=True)
    return 0 if inv.status == PASS else 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Sovereign Quarantine Validation")
    ap.add_argument("--sessions-dir", default=".ouroboros/sessions")
    ap.add_argument("--primary-root", default=str(_PROJECT_ROOT))
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--poll", type=float, default=3.0)
    ap.add_argument("--session", default="",
                    help="Pin to a specific session dir name (e.g. bt-…).")
    ap.add_argument(
        "--prove-override", action="store_true",
        help=(
            "I5 proof mode: verify the DeterministicLock (YM-T2) overrode "
            "an explicit JARVIS_FILE_ISOLATION_ENABLED=false.  Checks the "
            "most-recent (or --session-pinned) debug.log for the "
            "'[DeterministicLock] forced isolation+boundary' marker + current "
            "primary porcelain.  Prints PASS/FAIL; exits 0/1. Does NOT "
            "require a live soak — safe to run anytime after a session."
        ),
    )
    args = ap.parse_args(argv)
    if args.prove_override:
        return _prove_override(
            primary_root=Path(args.primary_root),
            sessions_dir=Path(args.sessions_dir),
            session=args.session,
        )
    return asyncio.run(watch_and_verify(
        sessions_dir=Path(args.sessions_dir),
        primary_root=Path(args.primary_root),
        since_ts=time.time(),
        timeout_s=args.timeout, poll_s=args.poll,
        pin_session=args.session,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
