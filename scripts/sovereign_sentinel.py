#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sovereign Telemetry Sentinel -- autonomous local watcher for a live C2 soak node.

The system watches its own live operations: an async monitor that streams the
remote ``docker logs`` over ``gcloud compute ssh`` into a local buffer, parses
the stream for FSM state transitions (no raw-dump), and -- as a good API
citizen -- AUTOMATICALLY tears the node down (stops billing) if it stalls or
throws a fatal exception. On convergence (an orange review PR / graduation) it
reports success and stops killing.

Design (clean, isolated units):
  - ``SentinelConfig``   : env + argv config (no hardcoding; every knob env-driven).
  - ``EventMatcher``     : compiled pattern registry -> classify(line) -> Event.
  - ``LogStream``        : async ``gcloud ssh ... docker logs -f`` subprocess,
                           yields decoded lines, retries with backoff during boot.
  - ``NodeController``   : async ``gcloud instances delete`` (good-citizen kill).
  - ``Sentinel``         : tracks transitions, runs the boot/stall watchdog,
                           drives the auto-kill. ``run()`` gathers ingest + watch.

NOTHING here uses ``time.sleep`` -- it is asyncio top to bottom. Fail-soft: a
parse error never crashes the loop; a kill failure is logged, not fatal.

Usage:
    python3 scripts/sovereign_sentinel.py --node jarvis-ouroboros-soak-XXXX
    # zone/project/timeouts/auto-kill all override via flags or JARVIS_SENTINEL_* env.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import pathlib
import re
import sys
import time
from typing import Iterable, List, Optional, Pattern, Tuple


# --------------------------------------------------------------------------- #
# Config -- every tunable env-driven, flags override env, sensible defaults.
# --------------------------------------------------------------------------- #
def _env(name: str, default: str) -> str:
    v = os.environ.get(name, "")
    return v.strip() if v and v.strip() else default


@dataclasses.dataclass(frozen=True)
class SentinelConfig:
    node: str
    zone: str
    project: str
    container: str
    boot_timeout_s: float      # max wait for the FIRST log line before kill
    stall_timeout_s: float     # max gap between FSM transitions before kill
    heartbeat_s: float         # cadence of the idle heartbeat line
    auto_kill: bool            # good-citizen: tear down on stall/fatal
    kill_on_fatal: bool        # also tear down on a fatal-exception match
    dry_run_kill: bool         # print the delete command, do NOT execute it
    reconnect_cap_s: float     # max backoff between ssh reconnect attempts
    autopsy_enabled: bool      # extract a black-box state dump BEFORE any kill
    autopsy_dir: str           # local dir for autopsy reports
    autopsy_timeout_s: float   # hard cap on the extraction (never blocks the kill)
    autopsy_log_lines: int     # docker-log tail depth to capture
    autopsy_ledgers: Tuple[str, ...]  # in-container FSM ledger paths to capture

    @classmethod
    def build(cls, args: argparse.Namespace) -> "SentinelConfig":
        return cls(
            node=args.node or _env("JARVIS_SENTINEL_NODE", ""),
            zone=args.zone or _env("JARVIS_SENTINEL_ZONE", "us-central1-a"),
            project=args.project or _env("JARVIS_SENTINEL_PROJECT", "jarvis-473803"),
            container=args.container or _env("JARVIS_SENTINEL_CONTAINER", "jarvis-sovereign-prod"),
            boot_timeout_s=float(args.boot_timeout or _env("JARVIS_SENTINEL_BOOT_TIMEOUT_S", "1200")),
            stall_timeout_s=float(args.stall_timeout or _env("JARVIS_SENTINEL_STALL_TIMEOUT_S", "900")),
            heartbeat_s=float(_env("JARVIS_SENTINEL_HEARTBEAT_S", "60")),
            auto_kill=(not args.no_auto_kill) and _env("JARVIS_SENTINEL_AUTO_KILL", "true").lower()
            not in ("0", "false", "no", "off"),
            kill_on_fatal=_env("JARVIS_SENTINEL_KILL_ON_FATAL", "true").lower()
            not in ("0", "false", "no", "off"),
            dry_run_kill=bool(args.dry_run_kill),
            reconnect_cap_s=float(_env("JARVIS_SENTINEL_RECONNECT_CAP_S", "30")),
            autopsy_enabled=_env("JARVIS_SENTINEL_AUTOPSY_ENABLED", "true").lower()
            not in ("0", "false", "no", "off"),
            autopsy_dir=args.autopsy_dir or _env("JARVIS_SENTINEL_AUTOPSY_DIR", "autopsy_reports"),
            autopsy_timeout_s=float(_env("JARVIS_SENTINEL_AUTOPSY_TIMEOUT_S", "120")),
            autopsy_log_lines=int(_env("JARVIS_SENTINEL_AUTOPSY_LOG_LINES", "1000")),
            # In-container FSM ledgers (the black box). Default set covers the
            # intake/decompose/posture/DLQ trajectory + the latest session debug
            # log. Override the whole list via JARVIS_SENTINEL_AUTOPSY_LEDGERS
            # (comma-separated). Globs are resolved on the node by the shell.
            autopsy_ledgers=tuple(
                p.strip() for p in _env(
                    "JARVIS_SENTINEL_AUTOPSY_LEDGERS",
                    ".jarvis/goal_decomposition_ledger.jsonl,"
                    ".jarvis/intake_dlq.jsonl,"
                    ".jarvis/posture_current.jsonl,.jarvis/posture_history.jsonl,"
                    ".jarvis/a1_trace.jsonl,.jarvis/semantic_index.npz,"
                    ".ouroboros/sessions/*/summary.json,.ouroboros/sessions/*/debug.log",
                ).split(",") if p.strip()
            ),
        )


# --------------------------------------------------------------------------- #
# Event taxonomy -- mirrors the governance FSM log markers. Each pattern is a
# (kind, regex, transition?, fatal?, success?) row; ALL overridable via
# JARVIS_SENTINEL_EXTRA_PATTERNS ("kind=regex;kind2=regex2", transitions).
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class Event:
    kind: str
    glyph: str
    is_transition: bool
    is_fatal: bool
    is_success: bool
    raw: str


# (kind, glyph, regex, transition, fatal, success)
_DEFAULT_RULES: Tuple[Tuple[str, str, str, bool, bool, bool], ...] = (
    ("DISPATCH",        "*", r"\[A1Trace\]",                                              True,  False, False),
    ("ADVISOR_BLOCK",   "#", r"Advisor BLOCKED|advisor_blocked|OperationAdvisor.*BLOCK",  True,  False, False),
    ("DECOMPOSE",       "+", r"BLOCK decomposed|block_decompose_reinject|decomposed into",True,  False, False),
    ("EGRESS_BLOCK",    "!", r"LocalEgressOverweightError|LOCAL_EGRESS_OVERWEIGHT",        True,  False, False),
    ("SOVEREIGN_YIELD", "~", r"\[SOVEREIGN YIELD\]|watchdog_self_heal_reinject",           True,  False, False),
    ("GENERATION",      ">", r"state=applied|emitting 2b|GENERATE.*ok|candidate generated",True,  False, False),
    ("CONVERGENCE",     "=", r"ouroboros/review|orange PR|\bPR #\d+|Pull request.*creat|"
                             r"\[SOVEREIGN GRADUATION\]|gh pr create|state=APPROVE|APPROVED",
                                                                                          True,  False, True),
    ("FATAL",           "X", r"Traceback \(most recent call last\)|FATAL|"
                             r"session_outcome=incomplete_kill|Fatal Python error|"
                             r"CRITICAL.*unhandled|panic:",                               False, True,  False),
)


class EventMatcher:
    """Compiled pattern registry. ``classify`` returns the FIRST matching Event
    or ``None``. Fail-soft: a bad custom pattern is skipped, never raises."""

    def __init__(self, rules: Iterable[Tuple[str, str, str, bool, bool, bool]] = _DEFAULT_RULES) -> None:
        self._compiled: List[Tuple[str, str, Pattern[str], bool, bool, bool]] = []
        for kind, glyph, pat, tr, fa, su in rules:
            try:
                self._compiled.append((kind, glyph, re.compile(pat, re.IGNORECASE), tr, fa, su))
            except re.error:
                continue
        # operator-supplied extra transition patterns (kind=regex;...)
        extra = _env("JARVIS_SENTINEL_EXTRA_PATTERNS", "")
        for chunk in extra.split(";"):
            if "=" not in chunk:
                continue
            k, _, p = chunk.partition("=")
            try:
                self._compiled.append((k.strip().upper(), "?", re.compile(p.strip(), re.IGNORECASE), True, False, False))
            except re.error:
                continue

    def classify(self, line: str) -> Optional[Event]:
        try:
            for kind, glyph, rx, tr, fa, su in self._compiled:
                if rx.search(line):
                    return Event(kind=kind, glyph=glyph, is_transition=tr,
                                 is_fatal=fa, is_success=su, raw=line.rstrip())
        except Exception:  # noqa: BLE001 -- classification must never crash ingest
            return None
        return None


# --------------------------------------------------------------------------- #
# Autopsy extractor -- the Black Box. BEFORE any kill, freeze and pull the last
# N docker-log lines + the in-container FSM ledgers to a local, FSM-stamped
# autopsy_reports/ dir. Bounded + fail-soft: it can never block the kill (a hung
# extraction must not keep the node billing), and a partial dump is still useful.
# --------------------------------------------------------------------------- #
class AutopsyExtractor:
    def __init__(self, cfg: SentinelConfig) -> None:
        self._cfg = cfg

    async def _ssh(self, remote_cmd: str, timeout_s: float) -> str:
        """Run one bounded ssh command, return merged stdout (or '' on failure)."""
        argv = [
            "gcloud", "compute", "ssh", self._cfg.node,
            "--zone", self._cfg.zone, "--project", self._cfg.project,
            "--command", remote_cmd,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
            return (out or b"").decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 -- extraction is best-effort
            return f"[autopsy ssh failed: {exc!r}]\n"

    async def extract(self, *, fsm_state: str, reason: str,
                      counts: dict, idle_s: float) -> Optional[pathlib.Path]:
        """Pull the black box to a local FSM-stamped dir. Returns the dir, or
        None if disabled / on total failure. NEVER raises."""
        if not self._cfg.autopsy_enabled:
            return None
        try:
            safe_state = re.sub(r"[^A-Za-z0-9_.-]", "_", fsm_state or "unknown")[:40]
            stamp = time.strftime("%Y%m%d-%H%M%S")
            outdir = pathlib.Path(self._cfg.autopsy_dir) / f"{self._cfg.node}_{stamp}_{safe_state}"
            outdir.mkdir(parents=True, exist_ok=True)

            half = max(10.0, self._cfg.autopsy_timeout_s / 2.0)
            # (1) the docker log tail (the recent trajectory).
            logs = await self._ssh(
                f"sudo docker logs --tail {self._cfg.autopsy_log_lines} {self._cfg.container} 2>&1",
                timeout_s=half,
            )
            (outdir / "docker_logs.txt").write_text(logs, encoding="utf-8")

            # (2) the in-container FSM ledgers (one bounded shell pass, cat each
            #     with a header; absent files noted, never fatal).
            paths = " ".join(f"'{p}'" for p in self._cfg.autopsy_ledgers)
            ledger_cmd = (
                f"sudo docker exec {self._cfg.container} sh -c '"
                f"for f in {paths}; do echo \"===== \"$f\" =====\"; "
                f"tail -c 262144 $f 2>/dev/null || echo \"(absent or unreadable)\"; echo; done'"
            )
            ledgers = await self._ssh(ledger_cmd, timeout_s=half)
            (outdir / "fsm_ledgers.txt").write_text(ledgers, encoding="utf-8")

            # (3) the manifest -- stamps the EXACT FSM state it was stuck in.
            manifest = {
                "node": self._cfg.node,
                "zone": self._cfg.zone,
                "captured_at": stamp,
                "stuck_fsm_state": fsm_state,
                "teardown_reason": reason,
                "idle_seconds": round(idle_s, 1),
                "transition_counts": dict(counts),
                "artifacts": {
                    "docker_logs.txt": (outdir / "docker_logs.txt").stat().st_size,
                    "fsm_ledgers.txt": (outdir / "fsm_ledgers.txt").stat().st_size,
                },
            }
            (outdir / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8",
            )
            return outdir
        except Exception as exc:  # noqa: BLE001 -- autopsy must NEVER block the kill
            _log(f"autopsy extraction error (proceeding to kill): {exc!r}")
            return None


# --------------------------------------------------------------------------- #
# Node controller -- the good-citizen auto-kill (async gcloud delete).
# --------------------------------------------------------------------------- #
class NodeController:
    def __init__(self, cfg: SentinelConfig) -> None:
        self._cfg = cfg

    def _delete_argv(self) -> List[str]:
        return [
            "gcloud", "compute", "instances", "delete", self._cfg.node,
            "--zone", self._cfg.zone, "--project", self._cfg.project, "--quiet",
        ]

    async def teardown(self, reason: str) -> None:
        argv = self._delete_argv()
        _log(f"GOOD-CITIZEN TEARDOWN ({reason}): {' '.join(argv)}")
        if self._cfg.dry_run_kill:
            _log("dry-run-kill set -> NOT executing the delete.")
            return
        if not self._cfg.auto_kill:
            _log("auto-kill disabled -> leaving the node up (manual teardown required).")
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await proc.communicate()
            _log(f"teardown rc={proc.returncode}: {(out or b'').decode('utf-8', 'replace').strip()[:300]}")
        except Exception as exc:  # noqa: BLE001 -- kill failure is logged, not fatal
            _log(f"teardown FAILED (manual delete needed): {exc!r}")


# --------------------------------------------------------------------------- #
# Async log stream -- gcloud ssh docker logs -f, retry-with-backoff on boot.
# --------------------------------------------------------------------------- #
class LogStream:
    def __init__(self, cfg: SentinelConfig) -> None:
        self._cfg = cfg

    def _ssh_argv(self) -> List[str]:
        return [
            "gcloud", "compute", "ssh", self._cfg.node,
            "--zone", self._cfg.zone, "--project", self._cfg.project,
            # ``sudo``: the gcloud ssh login user is NOT in the docker group on
            # the soak host, so a bare ``docker logs`` returns a permission-denied
            # error line that a naive watcher mistakes for a log line (-> a false
            # "node live, zero transitions" stall). sudo reads the real stream.
            "--command", f"sudo docker logs -f --tail 200 {self._cfg.container}",
        ]

    async def lines(self, stop: asyncio.Event) -> "asyncio.Queue[Optional[str]]":
        """Spawn the ssh log stream and pump decoded lines into a queue. On
        disconnect (boot not ready / ssh drop) retry with exponential backoff
        up to ``reconnect_cap_s``. Puts ``None`` as the terminal sentinel when
        ``stop`` is set. Runs as a background task; returns the queue."""
        q: "asyncio.Queue[Optional[str]]" = asyncio.Queue(maxsize=4096)
        asyncio.create_task(self._pump(q, stop))
        return q

    async def _pump(self, q: "asyncio.Queue[Optional[str]]", stop: asyncio.Event) -> None:
        backoff = 2.0
        while not stop.is_set():
            proc = None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *self._ssh_argv(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                backoff = 2.0  # reset on a successful spawn
                assert proc.stdout is not None
                while not stop.is_set():
                    raw = await proc.stdout.readline()
                    if not raw:
                        break  # stream closed -> reconnect
                    await q.put(raw.decode("utf-8", "replace").rstrip("\n"))
            except Exception as exc:  # noqa: BLE001
                _log(f"log-stream reconnect (boot not ready?): {exc!r}")
            finally:
                if proc is not None and proc.returncode is None:
                    try:
                        proc.terminate()
                    except Exception:  # noqa: BLE001
                        pass
            if stop.is_set():
                break
            await asyncio.sleep(min(backoff, self._cfg.reconnect_cap_s))
            backoff = min(backoff * 1.6, self._cfg.reconnect_cap_s)
        await q.put(None)


# --------------------------------------------------------------------------- #
# Sentinel -- transitions, boot/stall watchdog, auto-kill verdict.
# --------------------------------------------------------------------------- #
def _log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[sentinel {ts}] {msg}", flush=True)


class Sentinel:
    def __init__(self, cfg: SentinelConfig,
                 matcher: Optional[EventMatcher] = None,
                 controller: Optional[NodeController] = None,
                 stream: Optional[LogStream] = None,
                 autopsy: Optional[AutopsyExtractor] = None) -> None:
        self._cfg = cfg
        self._matcher = matcher or EventMatcher()
        self._controller = controller or NodeController(cfg)
        self._stream = stream or LogStream(cfg)
        self._autopsy = autopsy or AutopsyExtractor(cfg)
        self._stop = asyncio.Event()
        self._first_line_at: Optional[float] = None
        self._last_transition_at: Optional[float] = None
        self._last_kind: str = "boot"   # the FSM state for the autopsy stamp
        self._counts: dict = {}
        self._converged = False
        self.verdict: str = "running"

    # -- pure verdict helpers (unit-tested) -------------------------------- #
    def stall_verdict(self, now: float) -> Optional[str]:
        """Return a teardown reason if boot/stall thresholds are exceeded, else
        None. Pure given (now, internal clocks) -- no I/O."""
        if self._converged:
            return None
        if self._first_line_at is None:
            # still waiting for the first log line -> boot window.
            if now - self._started_at > self._cfg.boot_timeout_s:
                return f"boot_timeout ({self._cfg.boot_timeout_s:.0f}s, no logs)"
            return None
        ref = self._last_transition_at or self._first_line_at
        if now - ref > self._cfg.stall_timeout_s:
            return f"stall_timeout ({self._cfg.stall_timeout_s:.0f}s, no FSM transition)"
        return None

    def note(self, ev: Event, now: float) -> None:
        """Fold a matched event into sentinel state (pure bookkeeping)."""
        self._counts[ev.kind] = self._counts.get(ev.kind, 0) + 1
        if ev.is_transition:
            self._last_transition_at = now
            self._last_kind = ev.kind   # stamp the most recent FSM state
        if ev.is_success:
            self._converged = True

    # -- async run loop ---------------------------------------------------- #
    async def run(self) -> str:
        self._started_at = time.monotonic()
        if not self._cfg.node:
            _log("FATAL: no --node / JARVIS_SENTINEL_NODE given.")
            return "no_node"
        _log(f"watching node={self._cfg.node} zone={self._cfg.zone} "
             f"container={self._cfg.container} boot<={self._cfg.boot_timeout_s:.0f}s "
             f"stall<={self._cfg.stall_timeout_s:.0f}s auto_kill={self._cfg.auto_kill}")
        q = await self._stream.lines(self._stop)
        watchdog = asyncio.create_task(self._watch())
        try:
            while True:
                line = await q.get()
                if line is None:
                    break
                now = time.monotonic()
                if self._first_line_at is None:
                    self._first_line_at = now
                    _log("first log line received -- node is live; stall clock armed.")
                ev = self._matcher.classify(line)
                if ev is not None:
                    self.note(ev, now)
                    _log(f"{ev.glyph} {ev.kind:<15} | {ev.raw[-160:]}")
                    if ev.is_success and not self._stop.is_set():
                        self.verdict = "converged"
                        _log("=== CONVERGENCE DETECTED (orange PR / graduation). "
                             "Leaving the node up for inspection; auto-kill stood down. ===")
                    if ev.is_fatal and self._cfg.kill_on_fatal and not self._converged:
                        await self._teardown_and_stop(f"fatal:{ev.kind}")
                        break
        finally:
            self._stop.set()
            watchdog.cancel()
        return self.verdict

    async def _watch(self) -> None:
        """Boot/stall watchdog + idle heartbeat. asyncio, no time.sleep."""
        while not self._stop.is_set():
            await asyncio.sleep(min(self._cfg.heartbeat_s, 15.0))
            if self._stop.is_set():
                break
            now = time.monotonic()
            reason = self.stall_verdict(now)
            if reason is not None:
                await self._teardown_and_stop(reason)
                return
            # idle heartbeat
            ref = self._last_transition_at or self._first_line_at or self._started_at
            idle = now - ref
            last = max(self._counts, key=self._counts.get) if self._counts else "none"
            budget = self._cfg.stall_timeout_s if self._first_line_at else self._cfg.boot_timeout_s
            _log(f"heartbeat idle={idle:.0f}s/{budget:.0f}s transitions={sum(self._counts.values())} last_kind={last}")

    async def _teardown_and_stop(self, reason: str) -> None:
        self.verdict = f"killed:{reason}"
        _log(f"=== AUTO-KILL TRIGGERED: {reason} ===")
        # AUTOPSY YIELD (the Black Box): freeze the kill-sequence and extract the
        # state trajectory BEFORE vaporizing the node. Bounded by autopsy_timeout
        # so a hung extraction can never keep the node billing; fail-soft so the
        # kill ALWAYS proceeds even if the autopsy partially or fully fails.
        now = time.monotonic()
        ref = self._last_transition_at or self._first_line_at or getattr(self, "_started_at", now)
        idle_s = now - ref
        try:
            report = await asyncio.wait_for(
                self._autopsy.extract(
                    fsm_state=self._last_kind, reason=reason,
                    counts=self._counts, idle_s=idle_s,
                ),
                timeout=self._cfg.autopsy_timeout_s,
            )
            if report is not None:
                _log(f"AUTOPSY captured (stuck_fsm={self._last_kind}) -> {report}")
            else:
                _log("AUTOPSY disabled/empty -- proceeding to kill.")
        except Exception as exc:  # noqa: BLE001 -- never block the kill
            _log(f"AUTOPSY failed/timeout ({exc!r}) -- proceeding to kill.")
        await self._controller.teardown(reason)
        self._stop.set()


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #
def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sovereign Telemetry Sentinel -- autonomous C2 soak watcher.")
    p.add_argument("--node", default="", help="GCE instance name (or JARVIS_SENTINEL_NODE).")
    p.add_argument("--zone", default="")
    p.add_argument("--project", default="")
    p.add_argument("--container", default="")
    p.add_argument("--boot-timeout", default="", help="seconds to wait for first log before kill.")
    p.add_argument("--stall-timeout", default="", help="seconds between FSM transitions before kill.")
    p.add_argument("--no-auto-kill", action="store_true", help="watch only; never delete the node.")
    p.add_argument("--dry-run-kill", action="store_true", help="print the delete command, do not run it.")
    p.add_argument("--autopsy-dir", default="", help="local dir for black-box autopsy reports.")
    return p.parse_args(argv)


async def _amain(argv: Optional[List[str]] = None) -> int:
    cfg = SentinelConfig.build(_parse_args(argv))
    verdict = await Sentinel(cfg).run()
    _log(f"=== SENTINEL EXIT verdict={verdict} ===")
    return 0 if verdict in ("converged", "running") else 1


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return asyncio.run(_amain(argv))
    except KeyboardInterrupt:
        _log("interrupted -- exiting (node left as-is).")
        return 130


if __name__ == "__main__":
    sys.exit(main())
