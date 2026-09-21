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
from typing import Any, Callable, Dict, List, Optional, Tuple


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

    @classmethod
    def for_op(cls, ctx: Any, repo_root: Any) -> "EpisodicFailureMemory":
        """A memory whose retry prompt can ANSWER an error with the installed
        library's contract for the type it names.

        The soak that motivated this: nine attempts, every one failing with
        `argument of type 'JSONResponse' is not iterable`, and the retry
        prompt saying only "do not repeat these mistakes". The model had no
        fact to correct itself with. Now, when an episode's error quotes a
        type that the subject (or the failing candidate) imports from an
        installed package, that type's contract -- constructor, the instance
        attributes `__init__` sets, public methods -- rides along with the
        failure. One type, the one the error named, at the moment the model
        is about to try again. Nothing here runs third-party code.
        """
        memory = cls(str(getattr(ctx, "op_id", "") or ""))
        try:
            from pathlib import Path as _Path

            from backend.core.ouroboros.governance import library_contract as _lc
            from backend.core.ouroboros.governance.ast_signature_anchor import (
                collect_anchor_sources,
            )
            root = _Path(repo_root)
            targets = list(getattr(ctx, "target_files", ()) or ())
            description = str(getattr(ctx, "description", "") or "")

            def _resolver(type_name: str, extra_sources: List[str]) -> str:
                sources: List[str] = list(extra_sources)
                try:
                    for _label, src in collect_anchor_sources(targets, description, root):
                        sources.append(_Path(src).read_text(encoding="utf-8", errors="replace"))
                except Exception:  # noqa: BLE001
                    pass
                return _lc.contract_for_type(type_name, sources)

            memory._contract_resolver = _resolver
        except Exception:  # noqa: BLE001 -- a memory without a resolver is the old memory
            memory._contract_resolver = None
        return memory

    def __init__(self, op_id: str) -> None:
        self._op_id = op_id
        self._episodes: Dict[str, List[FailureEpisode]] = {}
        self._contract_resolver: Optional[Callable[[str, List[str]], str]] = None
        self._candidate_sources: Dict[Tuple[str, int], str] = {}  # file_path -> episodes

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
        candidate_source: str = "",
    ) -> None:
        """Record a failure episode for a file."""
        # Slice 33 Arc 0 — diagnostic only.
        from backend.core.ouroboros.telemetry.loop_sink import (
            sink_sync as _ls_sink_sync,
        )
        with _ls_sink_sync("episodic_memory.FailureMemory.record"):
            episode = FailureEpisode(
                file_path=file_path,
                attempt=attempt,
                failure_class=failure_class,
                error_summary=error_summary,
                specific_errors=tuple(specific_errors or []),
                line_numbers=tuple(line_numbers or []),
            )
            # Kept BESIDE the episode, not on it: the source is input for
            # resolving the types an error names, and PostmortemRecord
            # mirrors FailureEpisode field-for-field -- a persisted postmortem
            # must not carry every failing candidate's full text.
            if candidate_source:
                self._candidate_sources[(file_path, int(attempt))] = str(candidate_source)
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

        contracts = self._contracts_for(episodes)
        if contracts:
            lines.append(
                "## API contract for the type(s) these errors name — read from the "
                "INSTALLED package, not from memory"
            )
            lines.append(
                "Inspect these objects only through what is listed. A response's "
                "payload is its `body` (bytes); it is not iterable and not a dict."
            )
            lines.append("```python")
            lines.extend(contracts)
            lines.append("```")
            lines.append("")
        lines.append(
            "IMPORTANT: Do not repeat these mistakes. "
            "Address each specific error listed above in your new attempt."
        )
        return "\n".join(lines)

    def _contracts_for(self, episodes: List[FailureEpisode]) -> List[str]:
        """One contract per DISTINCT library type the episodes' errors name.
        NEVER raises; no resolver, or nothing resolvable, is simply nothing."""
        resolver = self._contract_resolver
        if resolver is None:
            return []
        try:
            from backend.core.ouroboros.governance.library_contract import (
                type_names_in_error,
            )
            out: List[str] = []
            seen: set = set()
            for ep in episodes:
                text = " ".join([ep.error_summary, *ep.specific_errors])
                for name in type_names_in_error(text):
                    if name in seen:
                        continue
                    seen.add(name)
                    source = self._candidate_sources.get((ep.file_path, int(ep.attempt)), "")
                    block = resolver(name, [source] if source else [])
                    if block:
                        out.append(block)
            return out
        except Exception:  # noqa: BLE001
            return []

    def clear(self) -> None:
        """Clear all episodes. Called when operation completes."""
        self._episodes.clear()
        self._candidate_sources.clear()
