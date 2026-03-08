"""backend/core/ouroboros/governance/multi_repo/context_builder.py

Cross-repo context builder for generation prompts.

When J-Prime generates a fix, the prompt needs context from the right repos.
This module finds related files (imports, tests, contracts) and reads them
while respecting a token budget.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §3
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from backend.core.ouroboros.governance.intent.signals import IntentSignal

from .registry import RepoRegistry

logger = logging.getLogger(__name__)

# Rough token estimate: ~4 chars per token
_CHARS_PER_TOKEN = 4

# Regex for Python import statements
_IMPORT_RE = re.compile(
    r"^(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE
)


@dataclass(frozen=True)
class ContextFile:
    """A file included in the cross-repo context."""

    repo: str
    path: str
    content: str
    relevance: str  # "import_dependency" | "test" | "contract" | "caller"


@dataclass(frozen=True)
class CrossRepoContext:
    """Complete context for a generation prompt."""

    primary_file: str
    primary_repo: str
    related_files: Tuple[ContextFile, ...]
    total_tokens_estimate: int


class ContextBuilder:
    """Builds cross-repo context for generation prompts."""

    def __init__(
        self,
        registry: RepoRegistry,
        token_budget: int = 8000,
    ) -> None:
        self._registry = registry
        self._token_budget = token_budget

    async def build(self, signal: IntentSignal) -> CrossRepoContext:
        """Build context for a signal's primary file."""
        primary_file = signal.target_files[0] if signal.target_files else ""
        primary_repo = signal.repo

        related = await self.find_related_files(primary_repo, primary_file)

        # Always include the primary file itself as first entry
        if primary_file:
            primary_content = await self._registry.read_file(primary_repo, primary_file)
            if primary_content is not None:
                related.insert(0, _RelatedFile(
                    repo=primary_repo, path=primary_file, relevance="primary",
                ))

        # Read content and enforce token budget
        context_files: List[ContextFile] = []
        total_chars = 0
        seen_paths: set = set()

        for rel in related:
            # Deduplicate
            key = (rel.repo, rel.path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            if total_chars // _CHARS_PER_TOKEN >= self._token_budget:
                break
            content = await self._registry.read_file(rel.repo, rel.path)
            if content is None:
                continue
            # Truncate if over budget
            remaining_chars = (self._token_budget - total_chars // _CHARS_PER_TOKEN) * _CHARS_PER_TOKEN
            if len(content) > remaining_chars:
                content = content[:remaining_chars] + "\n# ... truncated ..."
            total_chars += len(content)
            context_files.append(ContextFile(
                repo=rel.repo,
                path=rel.path,
                content=content,
                relevance=rel.relevance,
            ))

        return CrossRepoContext(
            primary_file=primary_file,
            primary_repo=primary_repo,
            related_files=tuple(context_files),
            total_tokens_estimate=total_chars // _CHARS_PER_TOKEN,
        )

    async def find_related_files(
        self, repo: str, file_path: str,
    ) -> List[_RelatedFile]:
        """Find files related to the given file across all repos."""
        related: List[_RelatedFile] = []

        # 1. Find test companion
        test_companion = await self._find_test_companion(repo, file_path)
        if test_companion:
            related.append(test_companion)

        # 2. Find import dependencies
        imports = await self._find_imports(repo, file_path)
        related.extend(imports)

        return related

    async def _find_test_companion(
        self, repo: str, file_path: str,
    ) -> Optional[_RelatedFile]:
        """Find the test file for a source file, or source for a test file."""
        name = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
        base = name.replace(".py", "")

        if base.startswith("test_"):
            # Looking for source file
            source_name = base[5:]  # remove "test_"
            matches = await self._registry.search_files(
                f"**/{source_name}.py", repo=repo,
            )
            for m in matches:
                if "test" not in m.path.split("/")[:-1]:  # not in a test dir
                    return _RelatedFile(repo=m.repo, path=m.path, relevance="test")
        else:
            # Looking for test file
            matches = await self._registry.search_files(
                f"**/test_{base}.py", repo=repo,
            )
            if matches:
                return _RelatedFile(
                    repo=matches[0].repo, path=matches[0].path, relevance="test",
                )

        return None

    async def _find_imports(
        self, repo: str, file_path: str,
    ) -> List[_RelatedFile]:
        """Parse imports from the file and find matching files."""
        content = await self._registry.read_file(repo, file_path)
        if content is None:
            return []

        related: List[_RelatedFile] = []
        for match in _IMPORT_RE.finditer(content):
            module_path = match.group(1) or match.group(2)
            # Convert dot notation to file path
            candidate = module_path.replace(".", "/") + ".py"
            # Search in all enabled repos
            for config in self._registry.list_enabled():
                full_path = config.local_path / candidate
                if full_path.exists():
                    related.append(_RelatedFile(
                        repo=config.name,
                        path=candidate,
                        relevance="import_dependency",
                    ))
                    break

        return related


@dataclass(frozen=True)
class _RelatedFile:
    """Internal: a related file before content is loaded."""

    repo: str
    path: str
    relevance: str
