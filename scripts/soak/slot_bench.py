#!/usr/bin/env python3
"""Deterministic capacity bench: the same frozen tasks through every arm.

Why this exists
---------------
The 2026-10-10 slot A/B fed each arm whatever the live sensors produced: the
3-slot arm got repair goals, the 2-slot arm got test synthesis, so "0 vs 40
landings" measured the goal mix, not the engine. And the arm driver kept no
progress, so when the WSL VM stopped under arm 2 (14:56:22) the run was lost.

What it does
------------
``prepare`` freezes an ordered task list from the signed roadmap (goals with
no dependencies, not already satisfied, picked by a seeded hash so the sample
is reproducible) and snapshots ``.jarvis`` once. Every arm then starts from:

  * the same base commit, in its own worktree (``bench/<run>/arm-<i>``),
    so one arm's landings never become the next arm's starting tree;
  * the same ``.jarvis`` seed (lessons, caches, ledgers), so arm 1's learning
    does not help arm 2;
  * the same task list, admitted through the same window.

Admission is held HERE, not by the intake router, because the router
re-ranks whatever it is handed. The arm's signed roadmap contains only the
first ``window`` unfinished tasks in list order; when the reconciliation
ledger records a task ``terminal`` (or ``satisfied``), the window slides and
the roadmap is re-signed through the same signer and reader the cage
consults. Every other intake source is switched off (benchmark isolation,
Sentinel, discovery, planner, conception, proposals) and promotion into main
is off.

Resume
------
``state.json`` is checkpointed atomically on every ledger event and every
30 s of active time, so a VM stop (which no signal trap can catch: it is a
SIGKILL from outside the guest) loses at most 30 s of accounting. SIGHUP /
SIGTERM / SIGINT flush the state, stop the soak gracefully and exit 75. On
the next ``arm`` call the arm resumes in the same worktree with its
remaining ACTIVE budget; a task that was in flight when the arm died is
re-queued at its position and counted in ``interrupted``.

Usage (inside WSL, as jarvis_svc)::

    slot_bench.py prepare --run R --base <sha> [--tasks 60] [--arms 3,2,1]
                          [--arm-seconds 7200] [--window 4] [--seed S]
    slot_bench.py arm --run R      # run/resume the current arm (foreground)
    slot_bench.py status --run R   # one JSON line: current arm, slots, done
    slot_bench.py report --run R

``arm`` exits 0 when it finished an arm and more remain, 10 when every arm
is done, 75 when interrupted, 1 on error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

SOURCE_REPO = Path(os.environ.get("OV_REPO", "/home/jarvis_svc/jarvis"))
BENCH_ROOT = Path(os.environ.get("OV_SLOT_BENCH_ROOT", Path.home() / "slot_bench"))
PYTHON = os.environ.get("OV_PYTHON", "/home/jarvis_svc/.venvs/ov/bin/python3")

#: .jarvis entries never copied into an arm seed: run recordings, logs,
#: write-ahead state of the source tree, and the ledgers each arm must own.
SEED_EXCLUDE = (
    "determinism", "logs", "intake_wal", "goal_reconciliation_ledger",
    "roadmap_reader_ledger", "roadmap", ".lock", ".bak", ".prev", ".new",
    "work_order_seen", "intake_router.lock", "proposal_inbox",
)

#: Exported before the soak launcher, whose own defaults use ${VAR:-...} and
#: whose envelope uses setdefault, so these win.
ARM_ENV = {
    # Every autonomous source off; the roadmap window is the only feed.
    "JARVIS_BENCHMARK_ISOLATION_MODE": "true",
    "JARVIS_SENTINEL_MODE_ENABLED": "false",
    "JARVIS_GOAL_DISCOVERY_ENABLED": "false",
    "JARVIS_CONCEPTION_BRIDGE_ENABLED": "false",
    "JARVIS_EPISTEMIC_PLANNER_ENABLED": "false",
    "JARVIS_AUTONOMOUS_PROPOSAL_SANCTION_ENABLED": "false",
    "JARVIS_L2_EXERCISE_CORPUS_ENABLED": "false",
    "JARVIS_SWE_BENCH_PRO_HARNESS_INJECT_ENABLED": "false",
    "JARVIS_ROADMAP_ORCHESTRATOR_ENABLED": "true",
    "JARVIS_ROADMAP_READER_ENABLED": "true",
    # How soon a freed window slot is refilled: pinned, so it is not a variable.
    "JARVIS_ROADMAP_CADENCE_BASE_S": "20",
    "JARVIS_ROADMAP_CADENCE_MAX_S": "60",
    # Landings stay on the arm's accumulation branch.
    "JARVIS_ACCUMULATION_PROMOTION_ENABLED": "false",
    "JARVIS_ORANGE_PR_ENABLED": "false",
    "JARVIS_REMOTE_PUSH_AIRGAP": "true",
}

TERMINAL_STATES = ("satisfied", "terminal", "stalled")
CHECKPOINT_S = 30.0
POLL_S = 5.0
#: A task in the window that is never dispatched (refused at intake, cage)
#: would hold its slot forever; the same rule ends it in every arm.
STALL_S = float(os.environ.get("OV_SLOT_BENCH_STALL_S", "2400"))
MAX_SOAK_RESTARTS = 3


# ---------------------------------------------------------------- state io

def run_dir(run: str) -> Path:
    return BENCH_ROOT / run


def load_state(run: str) -> Dict[str, Any]:
    return json.loads((run_dir(run) / "state.json").read_text(encoding="utf-8"))


def save_state(state: Dict[str, Any]) -> None:
    """Atomic and durable: a VM stop mid-write leaves the previous state."""
    path = run_dir(state["run"]) / "state.json"
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def log(state: Dict[str, Any], msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
    try:
        print(line, flush=True)
    except (BrokenPipeError, OSError):
        # The console that started us is gone (its shell was signalled, or
        # the driver's wsl.exe went away). bench.log below is the record.
        pass
    with open(run_dir(state["run"]) / "bench.log", "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def git(*args: str, cwd: Path = SOURCE_REPO) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


def env_file_value(name: str, env_path: Path) -> str:
    """One value from .env without executing it (sourcing .env runs code)."""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith(name + "="):
            return s.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ---------------------------------------------------------------- prepare

def freeze_tasks(base: str, count: int, seed: str) -> List[Dict[str, Any]]:
    import yaml  # noqa: PLC0415
    sys.path.insert(0, str(SOURCE_REPO))
    from backend.core.ouroboros.governance.goal_reconciliation_ledger import (  # noqa: PLC0415
        read_records, satisfied_goal_ids,
    )
    doc = yaml.safe_load((SOURCE_REPO / ".jarvis/roadmap.yaml").read_text(encoding="utf-8"))
    goals = [g for g in doc.get("goals") or [] if isinstance(g, dict) and g.get("id")]
    # Without the secret the ledger's MACs fail and its tail is dropped,
    # which would hide recent satisfactions.
    secret = env_file_value("JARVIS_ROADMAP_READER_HMAC_SECRET", SOURCE_REPO / ".env")
    recs = read_records(SOURCE_REPO / ".jarvis/goal_reconciliation_ledger.jsonl",
                        secret=secret or None)
    done = satisfied_goal_ids([g["id"] for g in goals], records=recs)
    tracked = set(git("ls-tree", "-r", "--name-only", base).splitlines())

    def is_test(f: str) -> bool:
        return Path(f).name.startswith("test_")

    def eligible(g: Dict[str, Any]) -> bool:
        """Work must remain at the base: a test target the goal CREATES must
        still be absent (812 of the roadmap's goals are test synthesis, and
        many were since written by other means; those would no-op and pad
        throughput), and a source target it EDITS must exist."""
        if g["id"] in done or g.get("depends_on"):
            return False
        files = g.get("target_files") or g.get("files") or []
        return bool(files) and all(
            (f not in tracked) if is_test(f) else (f in tracked) for f in files
        )

    pool = [g for g in goals if eligible(g)]
    pool.sort(key=lambda g: hashlib.sha256(f"{seed}:{g['id']}".encode()).hexdigest())
    return pool[:count]


def snapshot_seed(dst: Path) -> None:
    src = SOURCE_REPO / ".jarvis"

    def ignore(d: str, names: List[str]) -> List[str]:
        # Sockets and fifos (cockpit_attach.sock) belong to a live process.
        def special(n: str) -> bool:
            p = Path(d) / n
            return not (p.is_symlink() or p.is_file() or p.is_dir())
        return [n for n in names if special(n) or any(x in n for x in SEED_EXCLUDE)]

    shutil.copytree(src, dst, ignore=ignore, symlinks=True)


def cmd_prepare(a: argparse.Namespace) -> int:
    rd = run_dir(a.run)
    if rd.exists():
        print(f"refusing: {rd} exists (pick a new --run, or `arm` to resume)")
        return 1
    base = git("rev-parse", a.base)
    slots = [int(s) for s in a.arms.split(",") if s.strip()]
    tasks = freeze_tasks(base, a.tasks, a.seed)
    if len(tasks) < a.tasks:
        print(f"only {len(tasks)} eligible tasks (asked {a.tasks})")
    rd.mkdir(parents=True)
    (rd / "taskset.json").write_text(json.dumps(tasks, indent=1), encoding="utf-8")
    snapshot_seed(rd / "jarvis_seed")
    state = {
        "run": a.run, "base": base, "seed": a.seed, "window": a.window,
        "arm_seconds": a.arm_seconds, "task_ids": [t["id"] for t in tasks],
        "current": 0, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "arms": [{
            "index": i, "slots": n, "status": "pending",
            "worktree": str(rd / "arms" / f"arm-{i}-s{n}"),
            "branch": f"bench/{a.run}/arm-{i}-s{n}",
            "active_s": 0.0, "sessions": [], "restarts": 0,
            "goals": {t["id"]: {"state": "queued", "interrupted": 0}
                      for t in tasks},
        } for i, n in enumerate(slots)],
    }
    save_state(state)
    log(state, f"prepared run={a.run} base={base[:10]} tasks={len(tasks)} "
               f"arms={slots} window={a.window} arm_s={a.arm_seconds}")
    return 0


# ---------------------------------------------------------------- arm

def ensure_worktree(state: Dict[str, Any], arm: Dict[str, Any]) -> Path:
    wt = Path(arm["worktree"])
    if not wt.exists():
        git("worktree", "add", "-q", "-b", arm["branch"], str(wt), state["base"])
    marker = wt / ".jarvis" / ".slot_bench_seeded"
    if not marker.exists():
        # Overlay: the repo tracks some .jarvis files, so the dir exists.
        shutil.copytree(run_dir(state["run"]) / "jarvis_seed", wt / ".jarvis",
                        symlinks=True, dirs_exist_ok=True)
        shutil.copy2(SOURCE_REPO / ".env", wt / ".env")
        marker.write_text(state["base"], encoding="utf-8")
        log(state, f"arm {arm['index']}: worktree {wt} seeded at {state['base'][:10]}")
    return wt


def window_ids(state: Dict[str, Any], arm: Dict[str, Any]) -> List[str]:
    open_ids = [g for g in state["task_ids"]
                if arm["goals"][g]["state"] not in TERMINAL_STATES]
    return open_ids[: state["window"]]


def write_roadmap(state: Dict[str, Any], wt: Path, ids: List[str]) -> None:
    """Sign the window through the canonical signer and verify it through the
    reader before it replaces the live document (operator_goal_sanction)."""
    import yaml  # noqa: PLC0415
    sys.path.insert(0, str(wt))
    from backend.core.ouroboros.governance import operator_goal_sanction as ogs  # noqa: PLC0415
    tasks = {t["id"]: t for t in json.loads(
        (run_dir(state["run"]) / "taskset.json").read_text(encoding="utf-8"))}
    secret = env_file_value("JARVIS_ROADMAP_READER_HMAC_SECRET", wt / ".env")
    if not secret:
        raise RuntimeError("JARVIS_ROADMAP_READER_HMAC_SECRET missing from .env")
    # The reader that verifies the window takes the secret from the process
    # environment, as the organism does after loading the same .env.
    os.environ["JARVIS_ROADMAP_READER_HMAC_SECRET"] = secret
    doc = {
        "version": 1, "operator_id": "drussell23",
        "source": "operator_directed_agent_signed", "authority": "operator_directed",
        "note": f"slot_bench {state['run']} window", "goals": [tasks[i] for i in ids],
        "signed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    rendered = yaml.safe_dump(ogs._sign(doc, secret), sort_keys=False, allow_unicode=True)
    ok, detail = ogs._write_verified(wt / ".jarvis/roadmap.yaml", rendered)
    if not ok:
        raise RuntimeError(f"roadmap window did not verify: {detail}")


def read_ledger(wt: Path) -> List[Any]:
    sys.path.insert(0, str(wt))
    from backend.core.ouroboros.governance.goal_reconciliation_ledger import (  # noqa: PLC0415
        read_records,
    )
    secret = env_file_value("JARVIS_ROADMAP_READER_HMAC_SECRET", wt / ".env")
    return list(read_records(wt / ".jarvis/goal_reconciliation_ledger.jsonl",
                             secret=secret or None))


def apply_ledger(state: Dict[str, Any], arm: Dict[str, Any], records: List[Any],
                 now: float) -> bool:
    """Fold ledger events into goal states. True when anything changed."""
    changed = False
    for rec in records:
        g = arm["goals"].get(getattr(rec, "goal_id", ""))
        if g is None or g["state"] in TERMINAL_STATES:
            continue
        ev, op = rec.event, getattr(rec, "op_id", "")
        if ev == "dispatched" and g["state"] != "dispatched":
            if op and op in g.get("dead_ops", []):
                continue                      # an attempt from before a resume
            g.update(state="dispatched", op=op, t_dispatch=now)
            changed = True
        elif ev == "satisfied":
            g.update(state="satisfied", sha=getattr(rec, "commit_sha", ""),
                     t_end=now, active_at_end=arm["active_s"])
            changed = True
        elif ev == "terminal" and g["state"] == "dispatched" and op == g.get("op"):
            g.update(state="terminal", t_end=now, active_at_end=arm["active_s"])
            changed = True
    return changed


def mark_stalls(arm: Dict[str, Any], ids: List[str], now: float) -> bool:
    changed = False
    for gid in ids:
        g = arm["goals"][gid]
        g.setdefault("t_window", now)
        if g["state"] == "queued" and now - g["t_window"] > STALL_S:
            g.update(state="stalled", t_end=now, active_at_end=arm["active_s"])
            changed = True
    return changed


def launch_soak(state: Dict[str, Any], arm: Dict[str, Any], wt: Path,
                remaining: float) -> subprocess.Popen:
    env = dict(os.environ)
    env.update(ARM_ENV)
    env.update({
        "OV_REPO": str(wt), "PYTHONPATH": str(wt),
        "OV_SOAK_LOG_DIR": str(run_dir(state["run"]) / "logs"),
        "JARVIS_ROADMAP_PATH": str(wt / ".jarvis/roadmap.yaml"),
        "JARVIS_ROADMAP_READER_PATH": str(wt / ".jarvis/roadmap.yaml"),
        "JARVIS_GOAL_RECONCILIATION_LEDGER_PATH": str(wt / ".jarvis/goal_reconciliation_ledger.jsonl"),
        "JARVIS_ROADMAP_READER_MAX_GOALS": str(state["window"]),
        "JARVIS_TRINITY_ROOT": str(wt),
    })
    # The bench owns the stop; the soak's own cap is only a backstop.
    wall = int(remaining) + 900
    proc = subprocess.Popen(
        ["bash", str(wt / "scripts/soak/launch_supervised_soak.sh"), str(wall)],
        cwd=wt, env=env, start_new_session=True,
    )
    log(state, f"arm {arm['index']} (slots={arm['slots']}): soak pid {proc.pid}, "
               f"remaining {remaining:.0f}s active")
    return proc


def newest_session(wt: Path, after: float) -> str:
    root = wt / ".ouroboros/sessions"
    if not root.is_dir():
        return ""
    cands = [p for p in root.iterdir() if p.is_dir() and p.stat().st_mtime >= after]
    return max(cands, key=lambda p: p.stat().st_mtime).name if cands else ""


def stop_soak(proc: Optional[subprocess.Popen], grace: float = 180.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def cmd_arm(a: argparse.Namespace) -> int:
    state = load_state(a.run)
    if state["current"] >= len(state["arms"]):
        return 10
    arm = state["arms"][state["current"]]
    wt = ensure_worktree(state, arm)

    # Resume: an attempt in flight when the arm died is re-queued in place.
    for g in arm["goals"].values():
        if g["state"] == "dispatched":
            g["interrupted"] += 1
            g.setdefault("dead_ops", []).append(g.get("op", ""))
            g.update(state="queued", op="")
            g.pop("t_window", None)
    if arm["status"] == "running":
        log(state, f"arm {arm['index']}: RESUMING at active {arm['active_s']:.0f}s")
    arm["status"] = "running"
    save_state(state)

    stop = {"sig": 0}

    def on_signal(signum, _frame):
        stop["sig"] = signum
    for s in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(s, on_signal)

    ids = window_ids(state, arm)
    write_roadmap(state, wt, ids)
    remaining = state["arm_seconds"] - arm["active_s"]
    started_wall = time.time()
    proc = launch_soak(state, arm, wt, remaining)
    last_tick = time.monotonic()
    last_ckpt = last_tick
    try:
        while True:
            time.sleep(POLL_S)
            now_m = time.monotonic()
            arm["active_s"] += now_m - last_tick
            last_tick = now_m
            now = time.time()
            changed = apply_ledger(state, arm, read_ledger(wt), now)
            changed |= mark_stalls(arm, ids, now)
            sid = newest_session(wt, started_wall)
            if sid and sid not in arm["sessions"]:
                arm["sessions"].append(sid)
                changed = True
            new_ids = window_ids(state, arm)
            if new_ids != ids:
                ids = new_ids
                if ids:
                    write_roadmap(state, wt, ids)
                done = sum(arm["goals"][g]["state"] in TERMINAL_STATES for g in state["task_ids"])
                log(state, f"arm {arm['index']}: {done}/{len(state['task_ids'])} done, "
                           f"window={ids} active={arm['active_s']:.0f}s")
            if changed or now_m - last_ckpt >= CHECKPOINT_S:
                save_state(state)
                last_ckpt = now_m
            if stop["sig"]:
                log(state, f"arm {arm['index']}: signal {stop['sig']}, checkpointing and stopping soak")
                save_state(state)
                stop_soak(proc)
                save_state(state)
                return 75
            if not ids or arm["active_s"] >= state["arm_seconds"]:
                why = "task list drained" if not ids else "active budget spent"
                log(state, f"arm {arm['index']}: {why}; stopping soak")
                stop_soak(proc)
                break
            if proc.poll() is not None:
                arm["restarts"] += 1
                if arm["restarts"] > MAX_SOAK_RESTARTS:
                    log(state, f"arm {arm['index']}: soak exited {proc.returncode} "
                               f"{arm['restarts']}x; ending arm")
                    break
                log(state, f"arm {arm['index']}: soak exited {proc.returncode}; relaunching")
                save_state(state)
                time.sleep(30)
                proc = launch_soak(state, arm, wt, state["arm_seconds"] - arm["active_s"])
                last_tick = time.monotonic()
    except Exception as exc:  # noqa: BLE001 — never leave a soak running unowned
        log(state, f"arm {arm['index']}: driver error {exc!r}; stopping soak")
        save_state(state)
        stop_soak(proc)
        return 1
    arm["status"] = "done"
    arm["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    state["current"] += 1
    save_state(state)
    log(state, f"arm {arm['index']} done: {summarize_arm(state, arm)}")
    return 10 if state["current"] >= len(state["arms"]) else 0


# ---------------------------------------------------------------- report

def summarize_arm(state: Dict[str, Any], arm: Dict[str, Any]) -> Dict[str, Any]:
    gs = [arm["goals"][g] for g in state["task_ids"]]
    landed = sum(g["state"] == "satisfied" for g in gs)
    hours = max(arm["active_s"], 1.0) / 3600.0
    return {
        "slots": arm["slots"], "status": arm["status"],
        "active_h": round(arm["active_s"] / 3600.0, 2),
        "landed": landed, "landed_per_h": round(landed / hours, 2),
        "failed": sum(g["state"] == "terminal" for g in gs),
        "stalled": sum(g["state"] == "stalled" for g in gs),
        "open": sum(g["state"] not in TERMINAL_STATES for g in gs),
        "interrupted": sum(g["interrupted"] for g in gs),
        "sessions": arm["sessions"], "restarts": arm["restarts"],
    }


def cmd_status(a: argparse.Namespace) -> int:
    state = load_state(a.run)
    cur = state["current"]
    arm = state["arms"][cur] if cur < len(state["arms"]) else None
    print(json.dumps({
        "run": state["run"], "current": cur, "arms": len(state["arms"]),
        "slots": arm["slots"] if arm else None,
        "status": arm["status"] if arm else "all_done",
        "active_s": round(arm["active_s"]) if arm else None,
    }))
    return 0


def cmd_report(a: argparse.Namespace) -> int:
    state = load_state(a.run)
    rows = [summarize_arm(state, arm) for arm in state["arms"]]
    # Same task, every arm: did they agree on WHICH tasks land?
    per_task = {g: [arm["goals"][g]["state"] for arm in state["arms"]]
                for g in state["task_ids"]}
    print(json.dumps({"run": state["run"], "base": state["base"][:10],
                      "tasks": len(state["task_ids"]), "window": state["window"],
                      "arms": rows, "per_task": per_task}, indent=1))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--run", required=True)
    p.add_argument("--base", default="main")
    p.add_argument("--tasks", type=int, default=60)
    p.add_argument("--arms", default="3,2,1")
    p.add_argument("--arm-seconds", type=int, default=7200)
    p.add_argument("--window", type=int, default=4)
    p.add_argument("--seed", default="slotbench-1")
    for name in ("arm", "status", "report"):
        sub.add_parser(name).add_argument("--run", required=True)
    a = ap.parse_args(argv)
    return {"prepare": cmd_prepare, "arm": cmd_arm,
            "status": cmd_status, "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
