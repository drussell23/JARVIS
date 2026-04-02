"""
REM Manifesto Compliance Reviewer

Scans recently changed files for alignment with the Symbiotic AI-Native
Manifesto.  Runs during REM cognitive cycles with a bounded API-call budget.

Workflow:
    1. Discover files changed since the last REM review via ``git log``.
    2. Filter out secrets, binary assets, and duplicates.
    3. Cap the file list to ``JARVIS_HIVE_REM_MAX_FILES`` (default 10).
    4. For each file (up to budget): create a Hive thread, attach an
       AgentLogMessage with the file content, transition to DEBATING,
       and invoke persona_engine for JARVIS OBSERVE reasoning.

Manifesto violations are always informational in v1 -- no escalation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Tuple

from backend.hive.thread_models import (
    AgentLogMessage,
    CognitiveState,
    PersonaIntent,
    ThreadState,
)

logger = logging.getLogger(__name__)

# ============================================================================
# CONSTANTS (env-configurable)
# ============================================================================

_MAX_FILES_DEFAULT = 10
_MAX_LINES_DEFAULT = 200

_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".zip",
        ".tar",
        ".gz",
        ".bin",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".pyc",
    }
)

_SECRET_PATTERN: re.Pattern[str] = re.compile(
    r"(\.env$|credentials|secret|\.key$|\.pem$|\.p12$|\.pfx$|\.ssh)",
    re.IGNORECASE,
)


# ============================================================================
# MANIFESTO REVIEWER
# ============================================================================


class ManifestoReviewer:
    """Review recently changed files for Manifesto compliance.

    Parameters
    ----------
    persona_engine:
        PersonaEngine instance (typed as ``Any`` to avoid circular imports).
    thread_manager:
        ThreadManager instance for creating and managing threads.
    relay:
        HudRelayAgent instance (typed as ``Any`` to avoid circular imports).
    repo_root:
        Root of the git repository.  Defaults to ``Path(".")``.
    state_dir:
        Directory for persisting state (e.g. ``last_rem_at``).
        Defaults to ``~/.jarvis/hive/`` or ``JARVIS_HIVE_STATE_DIR`` env.
    """

    def __init__(
        self,
        persona_engine: Any,
        thread_manager: Any,
        relay: Any,
        repo_root: Optional[Path] = None,
        state_dir: Optional[Path] = None,
    ) -> None:
        self._persona_engine = persona_engine
        self._thread_manager = thread_manager
        self._relay = relay
        self._repo_root = repo_root or Path(".")
        self._state_dir = state_dir or Path(
            os.environ.get(
                "JARVIS_HIVE_STATE_DIR",
                str(Path.home() / ".jarvis" / "hive"),
            )
        )
        self._max_files = int(
            os.environ.get("JARVIS_HIVE_REM_MAX_FILES", _MAX_FILES_DEFAULT)
        )
        self._max_lines = int(
            os.environ.get("JARVIS_HIVE_REM_MAX_LINES_PER_FILE", _MAX_LINES_DEFAULT)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self, budget: int
    ) -> Tuple[List[str], int, bool, Optional[str]]:
        """Execute a Manifesto compliance review cycle.

        Parameters
        ----------
        budget:
            Maximum number of LLM inference calls allowed.

        Returns
        -------
        tuple
            ``(thread_ids, calls_used, should_escalate, escalation_id)``
            Manifesto violations are informational in v1, so
            ``should_escalate`` is always ``False``.
        """
        # 1. Discover changed files.
        changed = await self._get_changed_files()

        # 2. Filter secrets and binary.
        changed = self._filter_secret_paths(changed)
        changed = [f for f in changed if not self._is_binary(f)]

        # 3. Cap to max files.
        changed = self._cap_files(changed, self._max_files)

        # 4. Persist timestamp (even if no files -- marks "we ran").
        self._save_last_rem_timestamp()

        # 5. Nothing changed -> early return.
        if not changed:
            return [], 0, False, None

        # 6. Create threads, one per file (up to budget).
        thread_ids: List[str] = []
        calls_used: int = 0

        for filepath in changed:
            if calls_used >= budget:
                break

            content = self._read_file(filepath)

            thread = self._thread_manager.create_thread(
                title=f"Manifesto Review: {filepath}",
                trigger_event="rem_manifesto_review",
                cognitive_state=CognitiveState.REM,
            )
            tid = thread.thread_id

            log_msg = AgentLogMessage(
                thread_id=tid,
                agent_name="manifesto_reviewer",
                trinity_parent="jarvis",
                severity="info",
                category="manifesto",
                payload={"file": filepath, "content_preview": content[:500]},
            )
            self._thread_manager.add_message(tid, log_msg)

            # Transition to DEBATING so persona can reason.
            self._thread_manager.transition(tid, ThreadState.DEBATING)

            # Generate persona reasoning (consumes 1 LLM call).
            reasoning_msg = await self._persona_engine.generate_reasoning(
                "jarvis", PersonaIntent.OBSERVE, thread
            )
            self._thread_manager.add_message(tid, reasoning_msg)
            calls_used += 1

            thread_ids.append(tid)

        # v1: no escalation for manifesto violations.
        return thread_ids, calls_used, False, None

    # ------------------------------------------------------------------
    # Git integration
    # ------------------------------------------------------------------

    async def _get_changed_files(self) -> List[str]:
        """Discover files changed since the last REM review via git.

        Falls back to ``git log -20`` if no last_rem_at timestamp exists.
        """
        last_ts = self._load_last_rem_timestamp()

        if last_ts:
            cmd = [
                "git",
                "log",
                f"--since={last_ts}",
                "--name-only",
                "--pretty=format:",
            ]
        else:
            cmd = [
                "git",
                "log",
                "-20",
                "--name-only",
                "--pretty=format:",
            ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._repo_root),
            )
            stdout, _ = await proc.communicate()
            return self._parse_changed_files(stdout.decode(errors="replace"))
        except Exception:
            logger.warning(
                "Failed to run git log for changed files", exc_info=True
            )
            return []

    def _parse_changed_files(self, git_output: str) -> List[str]:
        """Parse ``git log --name-only`` output into a sorted unique file list.

        Filters empty lines and returns a deterministic order.
        """
        seen: set[str] = set()
        result: List[str] = []
        for line in git_output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped not in seen:
                seen.add(stripped)
                result.append(stripped)
        return sorted(result)

    # ------------------------------------------------------------------
    # Filtering helpers
    # ------------------------------------------------------------------

    def _filter_secret_paths(self, files: List[str]) -> List[str]:
        """Remove files whose paths match secret/credential patterns."""
        return [f for f in files if not _SECRET_PATTERN.search(f)]

    def _is_binary(self, filepath: str) -> bool:
        """Return True if the file suffix indicates a binary format."""
        return Path(filepath).suffix.lower() in _BINARY_EXTENSIONS

    def _cap_files(self, files: List[str], max_files: int) -> List[str]:
        """Return at most *max_files* entries from the list."""
        return files[:max_files]

    # ------------------------------------------------------------------
    # File reading
    # ------------------------------------------------------------------

    def _read_file(self, filepath: str) -> str:
        """Read the first ``_max_lines`` lines of a file under repo_root.

        Returns an empty string on any error.
        """
        try:
            full_path = self._repo_root / filepath
            lines = full_path.read_text(encoding="utf-8", errors="replace").splitlines()
            return "\n".join(lines[: self._max_lines])
        except Exception:
            logger.debug("Could not read %s", filepath, exc_info=True)
            return ""

    # ------------------------------------------------------------------
    # Timestamp persistence
    # ------------------------------------------------------------------

    def _save_last_rem_timestamp(self) -> None:
        """Write the current UTC ISO timestamp to ``state_dir/last_rem_at``."""
        try:
            self._state_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(tz=timezone.utc).isoformat()
            (self._state_dir / "last_rem_at").write_text(ts, encoding="utf-8")
        except Exception:
            logger.warning("Failed to save last_rem_at", exc_info=True)

    def _load_last_rem_timestamp(self) -> Optional[str]:
        """Read the ISO timestamp from ``state_dir/last_rem_at``.

        Returns ``None`` if the file does not exist or is unreadable.
        """
        try:
            path = self._state_dir / "last_rem_at"
            if path.exists():
                return path.read_text(encoding="utf-8").strip() or None
        except Exception:
            logger.debug("Could not load last_rem_at", exc_info=True)
        return None
