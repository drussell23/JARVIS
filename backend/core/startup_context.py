# backend/core/startup_context.py
"""
StartupContext - Crash history and recovery state management.

Tracks previous run state to inform recovery decisions.
Integrates with existing shutdown_hook infrastructure.

Usage:
    from backend.core.startup_context import StartupContext, CrashHistory

    # Load context at startup
    ctx = StartupContext.load()

    if ctx.is_recovery_startup:
        logger.info("Recovering from crash")

    if ctx.needs_conservative_startup:
        logger.warning("Multiple recent crashes - enabling conservative mode")

    # Save context at shutdown
    ctx.save(exit_code=0, exit_reason="clean_shutdown")
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger("jarvis.startup_context")

DEFAULT_STATE_DIR = Path("~/.jarvis/state").expanduser()


class CrashHistory:
    """
    Persists and queries crash events.

    Stores crash events in a JSONL file for durability and queryability.
    Provides rolling window counting for crash frequency analysis.
    """

    DEFAULT_WINDOW = timedelta(hours=1)

    def __init__(self, state_dir: Path = DEFAULT_STATE_DIR):
        """
        Initialize CrashHistory.

        Args:
            state_dir: Directory to store crash history file.
        """
        self.state_dir = state_dir
        self.history_file = state_dir / "crash_history.jsonl"

    def record_crash(self, exit_code: int, reason: str) -> None:
        """
        Record a crash event.

        Args:
            exit_code: Exit code from the crashed process.
            reason: Description of why the crash occurred.
        """
        self.state_dir.mkdir(parents=True, exist_ok=True)

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "exit_code": exit_code,
            "reason": reason,
        }

        with open(self.history_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

        logger.debug(f"Recorded crash: exit_code={exit_code}, reason={reason}")

    def crashes_in_window(self, window: Optional[timedelta] = None) -> int:
        """
        Count crashes within the time window.

        Args:
            window: Time window to consider. Defaults to DEFAULT_WINDOW (1 hour).

        Returns:
            Number of crashes within the window.
        """
        window = window or self.DEFAULT_WINDOW
        cutoff = datetime.now(timezone.utc) - window

        count = 0
        for entry in self._read_entries():
            try:
                ts_str = entry["timestamp"]
                # Handle both timezone-aware and naive timestamps
                # Also handle 'Z' suffix for UTC
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts > cutoff:
                    count += 1
            except (KeyError, ValueError) as e:
                logger.debug(f"Skipping malformed crash entry: {e}")
                continue

        return count

    def _read_entries(self) -> List[Dict[str, Any]]:
        """
        Read all crash entries from the history file.

        Returns:
            List of crash entry dictionaries.
        """
        if not self.history_file.exists():
            return []

        entries = []
        with open(self.history_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        logger.debug(f"Skipping invalid JSON line: {e}")
                        continue
        return entries

    def clear_old_entries(self, retention: Optional[timedelta] = None) -> int:
        """
        Remove crash entries older than retention period.

        Args:
            retention: How long to keep entries. Defaults to 24 hours.

        Returns:
            Number of entries removed.
        """
        retention = retention or timedelta(hours=24)
        cutoff = datetime.now(timezone.utc) - retention

        entries = self._read_entries()
        original_count = len(entries)

        # Filter to keep only recent entries
        recent_entries = []
        for entry in entries:
            try:
                ts_str = entry["timestamp"]
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts > cutoff:
                    recent_entries.append(entry)
            except (KeyError, ValueError):
                continue

        # Rewrite file with recent entries only
        if len(recent_entries) < original_count:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            with open(self.history_file, "w") as f:
                for entry in recent_entries:
                    f.write(json.dumps(entry) + "\n")

        removed = original_count - len(recent_entries)
        if removed > 0:
            logger.debug(f"Cleared {removed} old crash entries")
        return removed


@dataclass
class StartupContext:
    """
    Information about previous run, used to inform recovery decisions.

    Exit code semantics:
    - 0: Clean shutdown
    - 1: Crash
    - 100: Update requested
    - 101: Rollback requested
    - 102: Restart requested

    Usage:
        # At startup
        ctx = StartupContext.load()
        if ctx.is_recovery_startup:
            # Enable recovery mode
            pass

        # At shutdown
        ctx.save(exit_code=0, exit_reason="clean")
    """

    previous_exit_code: Optional[int] = None
    previous_exit_reason: Optional[str] = None
    crash_count_recent: int = 0
    last_successful_startup: Optional[datetime] = None
    state_markers: Dict[str, Any] = field(default_factory=dict)

    # Class-level threshold for conservative startup
    CRASH_THRESHOLD = 3  # Crashes before conservative startup

    # Exit codes for controlled shutdowns (not crashes)
    CONTROLLED_EXIT_CODES = frozenset({0, 100, 101, 102})

    @property
    def is_recovery_startup(self) -> bool:
        """
        Check if this is a recovery startup (after crash).

        Returns True if the previous exit was a crash (exit code not in
        the set of controlled exit codes).

        Returns:
            True if recovering from crash, False otherwise.
        """
        if self.previous_exit_code is None:
            return False
        # Not a recovery if clean exit or controlled restart
        return self.previous_exit_code not in self.CONTROLLED_EXIT_CODES

    @property
    def needs_conservative_startup(self) -> bool:
        """
        Check if we should skip optional components (repeated crashes).

        Conservative startup mode is triggered when there have been
        CRASH_THRESHOLD or more crashes in the recent window.

        Returns:
            True if conservative startup should be used, False otherwise.
        """
        return self.crash_count_recent >= self.CRASH_THRESHOLD

    @classmethod
    def load(cls, state_dir: Path = DEFAULT_STATE_DIR) -> 'StartupContext':
        """
        Load context from state files.

        Reads the last_run.json file and crash history to construct
        a StartupContext representing the previous run's state.

        Args:
            state_dir: Directory containing state files.

        Returns:
            StartupContext populated from state files.
        """
        last_run_file = state_dir / "last_run.json"
        crash_history = CrashHistory(state_dir)

        previous_exit_code = None
        previous_exit_reason = None
        last_successful = None
        state_markers = {}

        if last_run_file.exists():
            try:
                data = json.loads(last_run_file.read_text())
                previous_exit_code = data.get("exit_code")
                previous_exit_reason = data.get("exit_reason")
                if data.get("last_successful_startup"):
                    last_successful = datetime.fromisoformat(
                        data["last_successful_startup"]
                    )
                state_markers = data.get("state_markers", {})
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"Failed to load last_run.json: {e}")

        return cls(
            previous_exit_code=previous_exit_code,
            previous_exit_reason=previous_exit_reason,
            crash_count_recent=crash_history.crashes_in_window(),
            last_successful_startup=last_successful,
            state_markers=state_markers,
        )

    def save(
        self,
        state_dir: Path = DEFAULT_STATE_DIR,
        exit_code: int = 0,
        exit_reason: str = "normal"
    ) -> None:
        """
        Save context to state file.

        Writes the current state to last_run.json for the next startup.

        Args:
            state_dir: Directory to store state files.
            exit_code: Exit code for this run.
            exit_reason: Human-readable reason for exit.
        """
        state_dir.mkdir(parents=True, exist_ok=True)
        last_run_file = state_dir / "last_run.json"

        data: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "exit_code": exit_code,
            "exit_reason": exit_reason,
            "state_markers": self.state_markers,
        }

        if exit_code == 0:
            data["last_successful_startup"] = datetime.now(timezone.utc).isoformat()

        last_run_file.write_text(json.dumps(data, indent=2))
        logger.debug(f"Saved startup context: exit_code={exit_code}")


def get_startup_context(state_dir: Path = DEFAULT_STATE_DIR) -> StartupContext:
    """
    Factory function to load StartupContext.

    Convenience function for loading the startup context from the
    default or specified state directory.

    Args:
        state_dir: Directory containing state files.

    Returns:
        StartupContext loaded from state files.
    """
    return StartupContext.load(state_dir)
