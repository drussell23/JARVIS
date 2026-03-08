"""backend/core/ouroboros/governance/multi_repo/blast_radius.py

Cross-repo blast radius analysis.

Before applying a fix, this module analyzes which files across all repos
reference the target files. Cross-repo impacts escalate risk to
APPROVAL_REQUIRED.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §3
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Set, Tuple

from backend.core.ouroboros.governance.intent.signals import IntentSignal

from .registry import RepoRegistry

logger = logging.getLogger(__name__)

# Simple regex to find references to a filename (without extension)
_REF_RE_TEMPLATE = r"(?:import|from)\s+[\w.]*{module_name}"


@dataclass(frozen=True)
class AffectedFile:
    """A file affected by a change."""

    repo: str
    path: str
    dependency_type: str  # "imports" | "calls_api" | "tests" | "implements_contract"


@dataclass(frozen=True)
class BlastRadiusReport:
    """Result of cross-repo blast radius analysis."""

    target_repo: str
    target_files: Tuple[str, ...]
    affected_repos: Tuple[str, ...]
    affected_files: Tuple[AffectedFile, ...]
    crosses_repo_boundary: bool
    risk_escalation: Optional[str]
    contract_impact: Optional[str]


class CrossRepoBlastRadius:
    """Analyzes cross-repo impact of changes."""

    def __init__(self, registry: RepoRegistry) -> None:
        self._registry = registry

    async def analyze(self, signal: IntentSignal) -> BlastRadiusReport:
        """Analyze blast radius for a signal's target files."""
        target_repo = signal.repo
        target_files = signal.target_files

        affected: List[AffectedFile] = []
        affected_repo_names: Set[str] = set()

        for target_file in target_files:
            # Extract the module name from the file path
            module_name = self._extract_module_name(target_file)
            if not module_name:
                continue

            # Search across all enabled repos for references
            file_affects = await self._find_references(
                module_name, target_repo, target_file,
            )
            affected.extend(file_affects)
            for af in file_affects:
                affected_repo_names.add(af.repo)

        # Always include the target repo
        affected_repo_names.add(target_repo)

        crosses_boundary = len(affected_repo_names) > 1
        risk_escalation = "approval_required" if crosses_boundary else None
        contract_impact = self._detect_contract_impact(target_files) if crosses_boundary else None

        return BlastRadiusReport(
            target_repo=target_repo,
            target_files=target_files,
            affected_repos=tuple(sorted(affected_repo_names)),
            affected_files=tuple(affected),
            crosses_repo_boundary=crosses_boundary,
            risk_escalation=risk_escalation,
            contract_impact=contract_impact,
        )

    async def _find_references(
        self,
        module_name: str,
        source_repo: str,
        source_file: str,
    ) -> List[AffectedFile]:
        """Find files across all repos that reference the given module."""
        affected: List[AffectedFile] = []
        pattern = re.compile(
            _REF_RE_TEMPLATE.format(module_name=re.escape(module_name))
        )

        for config in self._registry.list_enabled():
            # Search Python files in this repo
            py_files = await self._registry.search_files("**/*.py", repo=config.name)
            for file_match in py_files:
                # Skip the source file itself
                if config.name == source_repo and file_match.path == source_file:
                    continue
                content = await self._registry.read_file(config.name, file_match.path)
                if content and pattern.search(content):
                    dep_type = "tests" if "test" in file_match.path else "imports"
                    affected.append(AffectedFile(
                        repo=config.name,
                        path=file_match.path,
                        dependency_type=dep_type,
                    ))

        return affected

    @staticmethod
    def _extract_module_name(file_path: str) -> Optional[str]:
        """Extract the module name from a file path (e.g., 'src/utils.py' -> 'utils')."""
        name = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
        if name.endswith(".py"):
            name = name[:-3]
        return name if name else None

    @staticmethod
    def _detect_contract_impact(target_files: Tuple[str, ...]) -> Optional[str]:
        """Detect if target files are API/contract files."""
        for f in target_files:
            lower = f.lower()
            if any(kw in lower for kw in ("api", "contract", "schema", "protocol")):
                return "api_changed"
        return None
