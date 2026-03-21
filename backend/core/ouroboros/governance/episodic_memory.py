"""EpisodicFailureMemory — per-file failure context injected into retries.

Based on Reflexion (Shinn et al., 2023): agents that receive structured
feedback about their previous failures perform dramatically better on retry.

This module stores per-file failure episodes within a single operation.
When VALIDATE fails and the orchestrator retries GENERATE, the episodic
memory is injected into the generation context so the brain knows exactly
what went wrong last time — not just "try again," but "try again, and
here's what failed on line 47: you returned a list but the caller expects
a generator."

Memory is scoped to a single operation — it does NOT leak between operations.
Frozen dataclass entries for immutability.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class FailureEpisode:
    """A single failure observation for one file in one retry attempt."""
    file_path: str
    attempt: int                   # which retry attempt (1-indexed)
    failure_class: str             # "test", "build", "infra", "security"
    error_summary: str             # human-readable short summary
    specific_errors: Tuple[str, ...]  # individual error messages/assertions
    line_numbers: Tuple[int, ...]  # line numbers where errors occurred (if available)
    timestamp: float = field(default_factory=time.monotonic)


class EpisodicFailureMemory:
    """Per-operation failure memory for retry context injection.

    Usage in orchestrator:
        1. Create at operation start: `memory = EpisodicFailureMemory(op_id)`
        2. On VALIDATE failure: `memory.record(file_path, attempt, failure_class, ...)`
        3. On GENERATE retry: inject `memory.format_for_prompt(file_path)` into context

    Memory is operation-scoped — create a new instance per operation.
    """

    def __init__(self, op_id: str) -> None:
        self._op_id = op_id
        self._episodes: Dict[str, List[FailureEpisode]] = {}  # file_path -> episodes

    @property
    def op_id(self) -> str:
        return self._op_id

    @property
    def total_episodes(self) -> int:
        return sum(len(eps) for eps in self._episodes.values())

    def record(
        self,
        file_path: str,
        attempt: int,
        failure_class: str,
        error_summary: str,
        specific_errors: Optional[List[str]] = None,
        line_numbers: Optional[List[int]] = None,
    ) -> None:
        """Record a failure episode for a file."""
        episode = FailureEpisode(
            file_path=file_path,
            attempt=attempt,
            failure_class=failure_class,
            error_summary=error_summary,
            specific_errors=tuple(specific_errors or []),
            line_numbers=tuple(line_numbers or []),
        )
        if file_path not in self._episodes:
            self._episodes[file_path] = []
        self._episodes[file_path].append(episode)

    def get_episodes(self, file_path: str) -> List[FailureEpisode]:
        """Get all failure episodes for a specific file."""
        return list(self._episodes.get(file_path, []))

    def get_all_episodes(self) -> Dict[str, List[FailureEpisode]]:
        """Get all failure episodes grouped by file."""
        return {k: list(v) for k, v in self._episodes.items()}

    def has_failures(self, file_path: Optional[str] = None) -> bool:
        """Check if there are any recorded failures."""
        if file_path:
            return bool(self._episodes.get(file_path))
        return bool(self._episodes)

    def format_for_prompt(self, file_path: Optional[str] = None) -> str:
        """Format failure memory as context for injection into generation prompt.

        If file_path is given, only return episodes for that file.
        Otherwise, return all episodes.

        This is the key method — it produces the text that gets injected
        into the retry prompt so the brain doesn't repeat the same mistakes.
        """
        if file_path:
            episodes = self.get_episodes(file_path)
            if not episodes:
                return ""
            return self._format_file_episodes(file_path, episodes)

        # All files
        sections = []
        for fpath, episodes in self._episodes.items():
            sections.append(self._format_file_episodes(fpath, episodes))
        return "\n\n".join(sections) if sections else ""

    def _format_file_episodes(self, file_path: str, episodes: List[FailureEpisode]) -> str:
        """Format episodes for a single file."""
        lines = [f"## Previous Failures for {file_path}"]
        lines.append(f"({len(episodes)} attempt(s) failed)")
        lines.append("")

        for ep in episodes:
            lines.append(f"### Attempt {ep.attempt} — {ep.failure_class}")
            lines.append(f"Summary: {ep.error_summary}")
            if ep.specific_errors:
                lines.append("Specific errors:")
                for err in ep.specific_errors:
                    lines.append(f"  - {err}")
            if ep.line_numbers:
                lines.append(f"Affected lines: {', '.join(str(ln) for ln in ep.line_numbers)}")
            lines.append("")

        lines.append(
            "IMPORTANT: Do not repeat these mistakes. "
            "Address each specific error listed above in your new attempt."
        )
        return "\n".join(lines)

    def clear(self) -> None:
        """Clear all episodes. Called when operation completes."""
        self._episodes.clear()
