"""backend/core/ouroboros/governance/multi_repo/registry.py

RepoRegistry — knows all repositories JARVIS operates across.

Provides unified file search/read. Each repo is described by a frozen
RepoConfig dataclass. Registration is env-var-driven via from_env().

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §3
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepoConfig:
    """Immutable configuration for a single repository."""

    name: str
    local_path: Path
    canary_slices: Tuple[str, ...]
    default_branch: str = "main"
    enabled: bool = True


@dataclass(frozen=True)
class FileMatch:
    """A file matched by search_files()."""

    repo: str
    path: str


class RepoRegistry:
    """Knows about all repos JARVIS operates across.

    Provides unified file search/read. Each repo is described by RepoConfig.
    """

    def __init__(self, configs: Tuple[RepoConfig, ...]) -> None:
        self._repos: Dict[str, RepoConfig] = {c.name: c for c in configs}

    @classmethod
    def from_env(cls) -> RepoRegistry:
        """Build registry from environment variables."""
        configs: List[RepoConfig] = []

        # Always include jarvis. Normalize the env path to an ABSOLUTE, resolved
        # root: a set-but-empty / whitespace var must NOT become ``Path("")``, and
        # even ``.`` must be resolved — downstream codegen joins + relative_to over
        # ABSOLUTE target_files require an absolute root (mirrors the strip+resolve
        # pattern in session_archive / cursor_rule_guard). NEVER raises.
        jarvis_path = (os.environ.get("JARVIS_REPO_PATH") or "").strip() or "."
        try:
            jarvis_local = Path(jarvis_path).resolve()
        except Exception:  # noqa: BLE001
            jarvis_local = Path.cwd().resolve()
        configs.append(RepoConfig(
            name="jarvis",
            local_path=jarvis_local,
            canary_slices=("tests/",),
        ))

        # Optional: prime
        prime_path = os.environ.get("JARVIS_PRIME_REPO_PATH")
        if prime_path:
            configs.append(RepoConfig(
                name="prime",
                local_path=Path(prime_path),
                canary_slices=("tests/",),
            ))

        # Optional: reactor-core. Registered under the canonical key "reactor" — the SAME repo key
        # the Oracle uses in NodeID (oracle.py self._repos = {"jarvis","prime","reactor"}) — so
        # resolve_repo_roots("reactor") and the unified graph agree (single source of truth). Env
        # precedence: JARVIS_REACTOR_REPO_PATH > REACTOR_CORE_REPO_PATH > REACTOR_CORE_PATH (the var
        # the Oracle itself reads), so setting any one wires both the registry and the Oracle.
        reactor_path = (
            os.environ.get("JARVIS_REACTOR_REPO_PATH")
            or os.environ.get("REACTOR_CORE_REPO_PATH")
            or os.environ.get("REACTOR_CORE_PATH")
        )
        if reactor_path:
            configs.append(RepoConfig(
                name="reactor",
                local_path=Path(reactor_path),
                canary_slices=("tests/",),
            ))

        return cls(configs=tuple(configs))

    def get(self, name: str) -> RepoConfig:
        """Get a repo config by name. Raises KeyError if not found."""
        return self._repos[name]

    def list_enabled(self) -> Tuple[RepoConfig, ...]:
        """Return all enabled repos."""
        return tuple(c for c in self._repos.values() if c.enabled)

    def list_all(self) -> Tuple[RepoConfig, ...]:
        """Return all repos regardless of enabled state."""
        return tuple(self._repos.values())

    async def read_file(self, repo: str, path: str) -> Optional[str]:
        """Read a file from a repo. Returns None if file doesn't exist."""
        config = self._repos[repo]
        file_path = (config.local_path / path).resolve()
        # Path traversal guard
        if not file_path.is_relative_to(config.local_path.resolve()):
            logger.warning("Path traversal blocked: %s", path)
            return None
        if not file_path.exists():
            return None
        return await asyncio.to_thread(file_path.read_text, encoding="utf-8")

    async def search_files(
        self, pattern: str, repo: Optional[str] = None,
    ) -> List[FileMatch]:
        """Search for files matching a glob pattern across repos."""
        results: List[FileMatch] = []
        repos = [self._repos[repo]] if repo else list(self._repos.values())

        for config in repos:
            if not config.enabled:
                continue
            matched = await asyncio.to_thread(
                lambda p=config.local_path, pat=pattern: list(p.glob(pat))
            )
            for m in matched:
                rel = str(m.relative_to(config.local_path))
                results.append(FileMatch(repo=config.name, path=rel))

        return results
