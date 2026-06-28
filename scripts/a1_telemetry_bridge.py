"""a1_telemetry_bridge — Resilient Multiplexed Telemetry Bridge (Gap-Closer T)
===============================================================================

Streams a remote GCP A1 node's debug.log over IAP-SSH, surviving network
drops via byte-offset-resume reconnect, and multiplexes three channel families:

  [A1Trace]            → cyan  (exploration proof-chain hops)
  [Cortex] / HEDGE GOVERNOR → yellow (stream-health / hedge governor)
  LEDGER_TERMINAL      → green (state=applied) / red (other terminal states)

READ-ONLY contract: the SSH tail NEVER writes to or signals the remote process.
A local Ctrl-C / stop_event tears down the LOCAL ssh subprocess only; the
remote soak is never killed or interrupted.

IAP-SSH command shape mirrors sovereign_iac_hypervisor._ssh_cmd + _tail_cmd:
  gcloud compute ssh <node> --project=... --zone=... --tunnel-through-iap
      --command "tail -c +<start> <log_q> 2>/dev/null || true"
where start = offset + 1 (tail -c is 1-indexed; offset is 0-indexed bytes).

Usage::

    python3 scripts/a1_telemetry_bridge.py \\
        --node a1-node-name --zone us-central1-a --project my-project \\
        --session-id <session-uuid>
"""
from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import shlex
import sys
import time
from typing import (
    AsyncGenerator,
    Callable,
    Coroutine,
    List,
    Optional,
    Set,
    Tuple,
)

# ---------------------------------------------------------------------------
# ANSI colour helpers (suppressed when NO_COLOR / non-TTY / --no-color)
# ---------------------------------------------------------------------------

_CYAN = "\033[36m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RESET = "\033[0m"

# ---------------------------------------------------------------------------
# Channel constants
# ---------------------------------------------------------------------------

CH_A1TRACE = "A1TRACE"
CH_CORTEX = "CORTEX"
CH_LEDGER = "LEDGER"
CH_OTHER = "OTHER"

_DEFAULT_CHANNELS: frozenset = frozenset({CH_A1TRACE, CH_CORTEX, CH_LEDGER})

# ---------------------------------------------------------------------------
# Transport defaults
# ---------------------------------------------------------------------------

_DEFAULT_BACKOFF_BASE_S: float = 2.0
_DEFAULT_BACKOFF_CAP_S: float = 30.0
_DEFAULT_MAX_RECONNECTS: int = 50
_DEFAULT_DEADLINE_S: float = 0.0      # 0 = no wall-clock deadline
_DEFAULT_READ_TIMEOUT_S: float = 60.0  # idle-read before reconnect
_DEFAULT_POLL_INTERVAL_S: float = 2.0  # poll interval between successful reads

# ---------------------------------------------------------------------------
# Line-classification regexes — match the REAL source formats
#   [A1Trace] …   from a1_trace.a1trace()   (WARNING level)
#   [A1Trace][emit-probe] … from a1_trace.emit_probe()
#   [Cortex] …    from doubleword_provider / candidate_generator
#   HEDGE GOVERNOR from doubleword_provider line ~2624
#   LEDGER_TERMINAL from orchestrator (Slice74Probe, INFO level)
# ---------------------------------------------------------------------------

_RE_A1TRACE = re.compile(r"\[A1Trace\]")
_RE_CORTEX = re.compile(r"\[Cortex\]|HEDGE GOVERNOR")
_RE_LEDGER = re.compile(r"LEDGER_TERMINAL")
_RE_LEDGER_APPLIED = re.compile(r"LEDGER_TERMINAL.*\bstate=applied\b")
_RE_LEDGER_TERMINAL_STATE = re.compile(
    r"LEDGER_TERMINAL.*\bstate=(applied|rolled_back|failed|blocked)\b"
)


# ---------------------------------------------------------------------------
# TelemetryMultiplexer
# ---------------------------------------------------------------------------

class TelemetryMultiplexer:
    """Classify and optionally colour-code a remote log line.

    Colour is suppressed when *no_color* is True, the ``NO_COLOR`` env var is
    set (https://no-color.org), or stdout is not a TTY.  The raw message body
    is NEVER reformatted — this module is a transport bridge, not a logger.
    """

    def __init__(
        self,
        channels: Optional[Set[str]] = None,
        no_color: bool = False,
    ) -> None:
        self._channels: frozenset = (
            frozenset(channels) if channels is not None else _DEFAULT_CHANNELS
        )
        _no_color_env = bool(os.environ.get("NO_COLOR", ""))
        _is_tty = getattr(sys.stdout, "isatty", lambda: False)()
        self._use_color: bool = not no_color and not _no_color_env and _is_tty

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(self, line: str) -> str:
        """Return the channel constant for *line*."""
        if _RE_A1TRACE.search(line):
            return CH_A1TRACE
        if _RE_CORTEX.search(line):
            return CH_CORTEX
        if _RE_LEDGER.search(line):
            return CH_LEDGER
        return CH_OTHER

    def is_terminal_sentinel(self, line: str) -> bool:
        """True if *line* is a LEDGER_TERMINAL with a known terminal state."""
        return bool(_RE_LEDGER_TERMINAL_STATE.search(line))

    def format_line(self, line: str, monotonic_ts: float) -> Optional[str]:
        """Classify, filter by active channels, and format *line* for output.

        Returns ``None`` when the line's channel is filtered out.
        The raw message body is passed through unchanged.
        """
        channel = self.classify(line)
        if channel not in self._channels:
            return None

        tag, color = self._channel_style(channel, line)
        ts_str = f"{monotonic_ts:012.3f}"
        body = line.rstrip("\n")

        if self._use_color and color:
            return f"{color}[{tag}|{ts_str}]{_RESET} {color}{body}{_RESET}\n"
        return f"[{tag}|{ts_str}] {body}\n"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _channel_style(self, channel: str, line: str) -> Tuple[str, str]:
        if channel == CH_A1TRACE:
            return "A1T", _CYAN
        if channel == CH_CORTEX:
            return "COR", _YELLOW
        if channel == CH_LEDGER:
            color = _GREEN if _RE_LEDGER_APPLIED.search(line) else _RED
            return "LED", color
        return "LOG", _DIM


# ---------------------------------------------------------------------------
# IAP-SSH command builder — mirrors sovereign_iac_hypervisor._ssh_cmd/_tail_cmd
# ---------------------------------------------------------------------------

def _build_tail_cmd(
    node: str,
    zone: str,
    project: str,
    remote_log_path: str,
    offset: int,
) -> List[str]:
    """Build the gcloud IAP-SSH command that tails from *offset* (0-indexed bytes).

    ``tail -c +N`` is 1-indexed, so ``start = max(0, offset) + 1``.
    Mirrors hypervisor ``_ssh_cmd`` / ``_tail_cmd`` exactly — no hardcoded
    project/zone.
    """
    start = max(0, int(offset)) + 1
    log_q = shlex.quote(remote_log_path)
    remote = f"tail -c +{start} {log_q} 2>/dev/null || true"
    return [
        "gcloud", "compute", "ssh", node,
        f"--project={project}",
        f"--zone={zone}",
        "--tunnel-through-iap",
        "--command", remote,
    ]


# ---------------------------------------------------------------------------
# Bridge-internal logger (stderr only — never pollutes the log stream)
# ---------------------------------------------------------------------------

def _log_bridge(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"[a1-bridge|{ts}] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Core streaming function
# ---------------------------------------------------------------------------

async def stream_telemetry(
    node: str,
    zone: str,
    project: str,
    remote_log_path: str,
    *,
    channels: Optional[Set[str]] = None,
    on_line: Optional[Callable[[str], None]] = None,
    no_color: bool = False,
    max_reconnects: int = _DEFAULT_MAX_RECONNECTS,
    deadline_s: float = _DEFAULT_DEADLINE_S,
    stop_event: Optional[asyncio.Event] = None,
    # --- Injection hooks for tests (None in production) ---
    _cmd_runner: Optional[
        Callable[[List[str]], AsyncGenerator[bytes, None]]
    ] = None,
    _sleep: Optional[Callable[[float], Coroutine]] = None,
) -> None:
    """Stream *remote_log_path* from *node* over IAP-SSH, surviving drops.

    The stream is READ-ONLY.  The remote tail/soak process is NEVER signalled.
    On network drop, the bridge resumes from the last byte-offset so no lines
    are duplicated or lost across reconnects.

    Args:
        node: GCP instance name.
        zone: GCP zone (e.g. ``us-central1-a``).
        project: GCP project ID.
        remote_log_path: Absolute path to the target log on the remote node.
        channels: Active channel set.  Defaults to all three.
        on_line: Callback for each formatted output line.
            Defaults to ``sys.stdout.write``.
        no_color: Suppress ANSI colour codes.
        max_reconnects: Max reconnect/retry count (0 = unlimited).
        deadline_s: Wall-clock deadline in seconds from call start (0 = none).
        stop_event: When set, the loop exits after tearing down the LOCAL ssh
            subprocess only — the remote run is NEVER signalled.
        _cmd_runner: Test injection.  ``_cmd_runner(cmd) -> AsyncGenerator[bytes]``.
            Raises to simulate a connection failure.
        _sleep: Test injection for asyncio.sleep.
    """
    mux = TelemetryMultiplexer(channels=channels, no_color=no_color)
    emit: Callable[[str], None] = (
        on_line if on_line is not None else sys.stdout.write
    )
    do_sleep = _sleep if _sleep is not None else asyncio.sleep

    start_wall = time.monotonic()
    last_offset: int = 0
    reconnect_count: int = 0
    backoff: float = _DEFAULT_BACKOFF_BASE_S

    # ------------------------------------------------------------------
    # _run_once: one transport connection
    # Returns (new_offset, terminal_seen).
    # Raises on connection failure so the outer loop can do backoff+retry.
    # ------------------------------------------------------------------
    async def _run_once(offset: int) -> Tuple[int, bool]:
        cmd = _build_tail_cmd(node, zone, project, remote_log_path, offset)
        new_offset = offset
        terminal_seen = False

        if _cmd_runner is not None:
            # Test-injection path — no real subprocess.
            async for chunk in _cmd_runner(cmd):
                # Check stop_event between chunks so the outer loop can exit.
                if stop_event is not None and stop_event.is_set():
                    return new_offset, terminal_seen
                new_offset += len(chunk)
                ts = time.monotonic() - start_wall
                for line in chunk.decode(errors="replace").splitlines(keepends=True):
                    formatted = mux.format_line(line, ts)
                    if formatted:
                        emit(formatted)
                    if mux.is_terminal_sentinel(line):
                        terminal_seen = True
            return new_offset, terminal_seen

        # Production path: real asyncio subprocess (IAP-SSH tail).
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            assert proc.stdout is not None
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    chunk = await asyncio.wait_for(
                        proc.stdout.read(4096),
                        timeout=_DEFAULT_READ_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    # No new data for read_timeout — break and reconnect.
                    break
                if not chunk:
                    break  # clean EOF from ssh
                new_offset += len(chunk)
                ts = time.monotonic() - start_wall
                for line in chunk.decode(errors="replace").splitlines(keepends=True):
                    formatted = mux.format_line(line, ts)
                    if formatted:
                        emit(formatted)
                    if mux.is_terminal_sentinel(line):
                        terminal_seen = True
                if terminal_seen:
                    break
        finally:
            # Tear down the LOCAL ssh subprocess only.
            # The remote tail/soak process is NEVER signalled — it runs
            # autonomously and will continue after the local bridge exits.
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:  # noqa: BLE001
                pass

        return new_offset, terminal_seen

    # ------------------------------------------------------------------
    # Outer reconnect loop
    # ------------------------------------------------------------------
    while True:
        # Wall-clock deadline
        if deadline_s > 0.0 and (time.monotonic() - start_wall) >= deadline_s:
            _log_bridge(f"deadline reached ({deadline_s:.0f}s) — bridge stopping")
            break

        # Stop event (checked before each attempt)
        if stop_event is not None and stop_event.is_set():
            _log_bridge(
                "stop event set — LOCAL bridge stopping (remote run untouched)"
            )
            break

        # Max reconnects exhausted
        if max_reconnects > 0 and reconnect_count >= max_reconnects:
            _log_bridge(
                f"max_reconnects={max_reconnects} exhausted — bridge stopping"
            )
            break

        try:
            new_offset, terminal_seen = await _run_once(last_offset)
        except Exception as exc:  # noqa: BLE001 — fail-soft, log + backoff + retry
            reconnect_count += 1
            jitter = random.uniform(0.0, backoff * 0.2)
            sleep_s = min(backoff + jitter, _DEFAULT_BACKOFF_CAP_S)
            _log_bridge(
                f"transport error: {exc!r} — reconnect #{reconnect_count} "
                f"in {sleep_s:.1f}s (offset={last_offset})"
            )
            await do_sleep(sleep_s)
            backoff = min(backoff * 2.0, _DEFAULT_BACKOFF_CAP_S)
            continue

        if terminal_seen:
            _log_bridge(
                f"terminal sentinel at offset={new_offset} — bridge done"
            )
            break

        # Check stop_event again before sleeping (avoids an unnecessary poll sleep).
        if stop_event is not None and stop_event.is_set():
            _log_bridge(
                "stop event set after _run_once — LOCAL bridge stopping"
            )
            break

        if new_offset > last_offset:
            # Progress: reset backoff, poll after brief interval.
            last_offset = new_offset
            backoff = _DEFAULT_BACKOFF_BASE_S
            await do_sleep(_DEFAULT_POLL_INTERVAL_S)
        else:
            # No new data: back off before retrying.
            reconnect_count += 1
            jitter = random.uniform(0.0, backoff * 0.2)
            sleep_s = min(backoff + jitter, _DEFAULT_BACKOFF_CAP_S)
            _log_bridge(
                f"no new data (offset={last_offset}) — backoff {sleep_s:.1f}s "
                f"(attempt #{reconnect_count})"
            )
            await do_sleep(sleep_s)
            backoff = min(backoff * 2.0, _DEFAULT_BACKOFF_CAP_S)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "Resilient IAP-SSH telemetry bridge for A1 cloud soaks.\n"
            "Streams debug.log from a remote GCP node, surviving network drops\n"
            "via byte-offset-resume reconnect.  READ-ONLY: never signals the remote."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--node", required=True, help="GCP instance name")
    p.add_argument(
        "--zone",
        default=os.environ.get("GCP_ZONE", ""),
        help="GCP zone (env: GCP_ZONE)",
    )
    p.add_argument(
        "--project",
        default=os.environ.get("GCP_PROJECT", ""),
        help="GCP project ID (env: GCP_PROJECT)",
    )

    log_group = p.add_mutually_exclusive_group()
    log_group.add_argument(
        "--session-id",
        dest="session_id",
        help="Session UUID — bridges .ouroboros/sessions/<id>/debug.log",
    )
    log_group.add_argument(
        "--remote-log",
        dest="remote_log",
        help="Explicit remote absolute path to the log file",
    )

    p.add_argument(
        "--channels",
        nargs="+",
        choices=[CH_A1TRACE, CH_CORTEX, CH_LEDGER],
        default=[CH_A1TRACE, CH_CORTEX, CH_LEDGER],
        metavar="CHANNEL",
        help=f"Channels to display: {CH_A1TRACE} {CH_CORTEX} {CH_LEDGER} (default: all)",
    )
    p.add_argument(
        "--max-reconnects",
        type=int,
        default=_DEFAULT_MAX_RECONNECTS,
        help="Max reconnect attempts before giving up (0 = unlimited, default 50)",
    )
    p.add_argument(
        "--deadline-s",
        type=float,
        default=_DEFAULT_DEADLINE_S,
        help="Total wall-clock deadline in seconds (0 = no deadline)",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable ANSI colour codes",
    )
    return p


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.zone:
        parser.error("--zone is required (or set GCP_ZONE env var)")
    if not args.project:
        parser.error("--project is required (or set GCP_PROJECT env var)")

    if args.remote_log:
        remote_log_path = args.remote_log
    elif args.session_id:
        remote_log_path = f".ouroboros/sessions/{args.session_id}/debug.log"
    else:
        parser.error("one of --session-id or --remote-log is required")
        return  # unreachable; satisfies type checker

    channels: Set[str] = set(args.channels)

    try:
        asyncio.run(
            stream_telemetry(
                node=args.node,
                zone=args.zone,
                project=args.project,
                remote_log_path=remote_log_path,
                channels=channels,
                no_color=args.no_color,
                max_reconnects=args.max_reconnects,
                deadline_s=args.deadline_s,
            )
        )
    except KeyboardInterrupt:
        _log_bridge("Ctrl-C — LOCAL bridge stopped (remote run untouched)")
        sys.exit(0)


if __name__ == "__main__":
    main()
