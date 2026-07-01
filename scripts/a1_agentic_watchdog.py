#!/usr/bin/env python3
"""Asynchronous Agentic Watchdog -- active log-tailing cognitive circuit breaker.

Tails a running A1 soak's debug.log in REAL TIME, streams cognitive milestones
(File Read / Search Executed / Patch Attempted / Retry / Applied), and trips a
CognitiveLoopDetected circuit breaker when the agent is stuck:

  * IDENTICAL TOOL LOOP: the same tool with the same arguments N times in a row
    (default 3) -- an agentic infinite loop / hallucination.
  * RETRY STALL: N GENERATE_RETRY rejections (default 3) with NO new exploration
    tool call in between -- the model is re-emitting patches without learning.

On a trip it SIGTERMs the soak's whole process group (the driver's signal handler
then reaps the GCP node + reverts chaos) and exits non-zero -- saving cloud budget
instead of waiting out the wall-clock. Read-only w.r.t. the soak's logic; the only
side effect is the kill on a confirmed cognitive loop.

Usage:
    python3 scripts/a1_agentic_watchdog.py --log <debug.log> [--proc-pattern isomorphic_a1_local.py]
    python3 scripts/a1_agentic_watchdog.py --auto   # auto-discover newest session debug.log
"""
from __future__ import annotations

import argparse
import os
import re
import signal
import sys
import time
from collections import deque
from typing import Deque, List, Optional, Tuple

# Exploration tools the Iron Gate credits (mirror orchestrator._EXPLORATION_TOOLS).
_EXPLORATION_TOOLS = frozenset(
    {"read_file", "search_code", "get_callers", "list_symbols", "glob_files", "list_dir"}
)

# Flexible tool-invocation matcher: matches common shapes the loop may log --
# `tool=read_file args={...}`, `[Venom] read_file(path=...)`, `executing read_file`.
_TOOL_CALL_RE = re.compile(
    r"(?:tool[=:\s]+|executing\s+|\bcall\s+|\[Venom\]\s+)(?P<tool>read_file|search_code|"
    r"get_callers|list_symbols|glob_files|list_dir|run_tests|bash|edit_file|write_file|web_fetch|web_search)"
    r"\b[^\n]{0,120}"
)
_ARGS_RE = re.compile(r"(?:args?|arguments|path|query|pattern)[=:\s]+(?P<args>[^\n]{0,80})")

# Milestone markers -> human labels (streamed live).
_MILESTONES: Tuple[Tuple[str, str], ...] = (
    ("[A1Trace] emit", "🌱 GOAL emitted"),
    ("[A1Trace] accept", "✅ Accepted → CLASSIFY"),
    ("routed generation to the awakened 32B", "🧠 Routed GENERATE → 32B"),
    ("PhaseRunnerDelegate] CLASSIFY", "🔍 CLASSIFY"),
    ("PhaseRunnerDelegate] APPROVE+APPLY+VERIFY", "🔧 APPLY/VERIFY"),
    ("PhaseRunnerDelegate] COMPLETE", "🏁 COMPLETE"),
    ("state=applied", "🎉 APPLIED (terminal)"),
    ("LocalLatencyLockup", "⏱  32B call timed out"),
    ("KeyError('choices')", "⚠️  32B response missing choices"),
)


class CognitiveLoopDetected(RuntimeError):
    """Raised/returned when the agent is stuck in a non-productive loop."""


class AgenticWatchdog:
    """Pure, testable cognitive-loop detector. Feed it log lines via observe();
    it returns a trip reason string (or None) and accumulates milestones."""

    def __init__(self, *, identical_tool_limit: int = 3, retry_stall_limit: int = 3) -> None:
        self.identical_tool_limit = max(2, identical_tool_limit)
        self.retry_stall_limit = max(2, retry_stall_limit)
        self._recent: Deque[Tuple[str, str]] = deque(maxlen=self.identical_tool_limit)
        self._retries_since_explore = 0
        self.milestones: List[str] = []
        self.explorations = 0
        self.retries = 0

    def observe(self, line: str) -> Optional[str]:
        """Ingest one log line. Returns a CognitiveLoopDetected reason or None."""
        if not line:
            return None

        # Milestone streaming.
        for marker, label in _MILESTONES:
            if marker in line:
                self.milestones.append(label)

        # (1) Identical-tool-call loop.
        tm = _TOOL_CALL_RE.search(line)
        if tm:
            tool = tm.group("tool")
            am = _ARGS_RE.search(line)
            args = (am.group("args").strip() if am else "").rstrip(")}\"' ")
            self._recent.append((tool, args))
            if tool in _EXPLORATION_TOOLS:
                self.explorations += 1
                self._retries_since_explore = 0  # real exploration clears the stall
                self.milestones.append("📄 %s %s" % (
                    "File Read" if tool == "read_file" else
                    "Search Executed" if tool == "search_code" else tool,
                    ("(" + args[:48] + ")") if args else "",
                ))
            if (len(self._recent) == self.identical_tool_limit
                    and len(set(self._recent)) == 1):
                return ("CognitiveLoopDetected:identical_tool_call:%s(%s) x%d"
                        % (tool, args[:40], self.identical_tool_limit))

        # (2) Retry stall (GENERATE_RETRY without new exploration).
        if "phase=GENERATE_RETRY" in line or "GENERATE_RETRY op=" in line:
            self.retries += 1
            self._retries_since_explore += 1
            self.milestones.append("🔁 Patch rejected → GENERATE_RETRY (%d)" % self.retries)
            if self._retries_since_explore >= self.retry_stall_limit:
                return ("CognitiveLoopDetected:retry_stall:%d GENERATE_RETRY with no "
                        "new exploration" % self._retries_since_explore)
        return None


# ---------------------------------------------------------------------------
# Live tailing + kill action
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print("[Watchdog] %s" % msg, flush=True)


def _sigterm_process_group(proc_pattern: str) -> None:
    """SIGTERM the soak's whole process group so the driver's signal handler reaps
    the GCP node + reverts chaos. Fail-soft."""
    import subprocess
    try:
        out = subprocess.run(["pgrep", "-f", proc_pattern], capture_output=True, text=True)
        pids = [int(p) for p in out.stdout.split() if p.strip()]
    except Exception:  # noqa: BLE001
        pids = []
    for pid in pids:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            _log("SIGTERM -> process group of pid=%d" % pid)
        except Exception:  # noqa: BLE001
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:  # noqa: BLE001
                pass


def _auto_discover_log() -> Optional[str]:
    root = os.path.join(".ouroboros", "sessions")
    try:
        dirs = [os.path.join(root, d) for d in os.listdir(root) if d.startswith("bt-")]
        dirs = [d for d in dirs if os.path.isdir(d)]
        if not dirs:
            return None
        newest = max(dirs, key=lambda d: os.path.getmtime(d))
        return os.path.join(newest, "debug.log")
    except Exception:  # noqa: BLE001
        return None


def watch(log_path: str, *, proc_pattern: str, poll_s: float = 1.0,
          max_wall_s: float = 3600.0, wd: Optional[AgenticWatchdog] = None) -> int:
    """Tail log_path, stream milestones, trip the breaker. Returns 0 (clean/EOF),
    2 (CognitiveLoopDetected -> killed). NEVER raises."""
    wd = wd or AgenticWatchdog()
    _log("tailing %s (proc=%s)" % (log_path, proc_pattern))
    start = time.monotonic()
    pos = 0
    last_milestone = 0
    while time.monotonic() - start < max_wall_s:
        try:
            if os.path.isfile(log_path):
                with open(log_path, "r", encoding="utf-8", errors="ignore") as fh:
                    fh.seek(pos)
                    for line in fh:
                        reason = wd.observe(line)
                        if reason:
                            _log("🚨 %s" % reason)
                            _log("firing CognitiveLoopDetected -> SIGTERM process group + reap")
                            _sigterm_process_group(proc_pattern)
                            return 2
                    pos = fh.tell()
                # Stream any new milestones.
                while last_milestone < len(wd.milestones):
                    _log(wd.milestones[last_milestone])
                    last_milestone += 1
        except Exception as exc:  # noqa: BLE001
            _log("tail fail-soft: %r" % exc)
        # Stop if the soak process is gone (run finished).
        try:
            import subprocess
            if not subprocess.run(["pgrep", "-f", proc_pattern],
                                  capture_output=True, text=True).stdout.strip():
                _log("soak process exited -- watchdog done")
                return 0
        except Exception:  # noqa: BLE001
            pass
        time.sleep(poll_s)
    _log("max wall reached -- watchdog exiting")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="A1 agentic cognitive-loop watchdog")
    ap.add_argument("--log", default=None)
    ap.add_argument("--auto", action="store_true", help="auto-discover newest session debug.log")
    ap.add_argument("--proc-pattern", default="isomorphic_a1_local.py")
    ap.add_argument("--poll-s", type=float, default=1.0)
    ap.add_argument("--max-wall-s", type=float, default=3600.0)
    ap.add_argument("--identical-tool-limit", type=int, default=3)
    ap.add_argument("--retry-stall-limit", type=int, default=3)
    args = ap.parse_args(argv)
    log_path = args.log or _auto_discover_log()
    if not log_path:
        _log("no log path (pass --log or --auto)")
        return 1
    wd = AgenticWatchdog(
        identical_tool_limit=args.identical_tool_limit,
        retry_stall_limit=args.retry_stall_limit,
    )
    return watch(log_path, proc_pattern=args.proc_pattern, poll_s=args.poll_s,
                 max_wall_s=args.max_wall_s, wd=wd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
