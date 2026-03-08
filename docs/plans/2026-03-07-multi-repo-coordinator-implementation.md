# Multi-Repo Coordinator (Layer 2) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the multi-repo coordinator that gives the autonomous pipeline awareness of all 3 repositories (JARVIS, Prime, reactor-core), cross-repo context for prompt building, blast radius analysis across repo boundaries, and per-repo pipeline routing.

**Architecture:** Four modules under `backend/core/ouroboros/governance/multi_repo/`. RepoRegistry wraps existing `RepoConnector`/`RepoType` from `cross_repo.py` with a frozen `RepoConfig` dataclass and env-var-driven registration. ContextBuilder reads related files across repos for generation prompts. CrossRepoBlastRadius extends the existing `BlastRadiusAdapter` to detect cross-repo impacts. RepoPipelineManager routes `IntentSignal`s to the correct repo's `GovernedLoopService`.

**Tech Stack:** Python 3.9+, asyncio, existing `RepoConnector`/`RepoType`/`CrossRepoConfig` from `cross_repo.py`, existing `BlastRadiusAdapter`, existing `GovernedLoopService`, existing `IntentSignal`.

**Design doc:** `docs/plans/2026-03-07-autonomous-layers-design.md` §3 (Layer 2)

**Existing code to build on:**
- `backend/core/ouroboros/cross_repo.py`: `RepoType` enum (JARVIS/PRIME/REACTOR), `RepoConnector` (file read/write, health), `CrossRepoConfig` (env var paths), `RepoState`
- `backend/core/ouroboros/governance/blast_radius_adapter.py`: `BlastRadiusAdapter.compute(file_path) -> BlastRadiusResult`
- `backend/core/ouroboros/governance/governed_loop_service.py`: `GovernedLoopService.submit(ctx, trigger_source=)`
- `backend/core/ouroboros/governance/intent/signals.py`: `IntentSignal` with `.repo`, `.target_files`

---

## Task 1: RepoConfig + RepoRegistry (`registry.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/multi_repo/__init__.py` (empty for now)
- Create: `backend/core/ouroboros/governance/multi_repo/registry.py`
- Create: `tests/governance/multi_repo/__init__.py` (empty)
- Test: `tests/governance/multi_repo/test_registry.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/multi_repo/test_registry.py"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch


class TestRepoConfig:
    def test_frozen_dataclass(self):
        from backend.core.ouroboros.governance.multi_repo.registry import RepoConfig

        cfg = RepoConfig(
            name="jarvis",
            local_path=Path("/tmp/jarvis"),
            canary_slices=("tests/",),
        )
        assert cfg.name == "jarvis"
        assert cfg.default_branch == "main"
        assert cfg.enabled is True
        with pytest.raises(AttributeError):
            cfg.name = "other"

    def test_custom_defaults(self):
        from backend.core.ouroboros.governance.multi_repo.registry import RepoConfig

        cfg = RepoConfig(
            name="prime",
            local_path=Path("/tmp/prime"),
            canary_slices=("tests/",),
            default_branch="develop",
            enabled=False,
        )
        assert cfg.default_branch == "develop"
        assert cfg.enabled is False


class TestRepoRegistryCreation:
    def test_from_configs(self):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        configs = (
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
            RepoConfig(name="prime", local_path=Path("/tmp/p"), canary_slices=("tests/",)),
        )
        registry = RepoRegistry(configs=configs)
        assert registry.get("jarvis").name == "jarvis"
        assert registry.get("prime").name == "prime"

    def test_get_unknown_raises(self):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
        ))
        with pytest.raises(KeyError):
            registry.get("unknown")

    def test_list_enabled_filters_disabled(self):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        configs = (
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",), enabled=True),
            RepoConfig(name="prime", local_path=Path("/tmp/p"), canary_slices=("tests/",), enabled=False),
        )
        registry = RepoRegistry(configs=configs)
        enabled = registry.list_enabled()
        assert len(enabled) == 1
        assert enabled[0].name == "jarvis"


class TestRepoRegistryFromEnv:
    def test_from_env_creates_jarvis_repo(self):
        from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry

        with patch.dict("os.environ", {"JARVIS_REPO_PATH": "/tmp/jarvis-test"}, clear=False):
            registry = RepoRegistry.from_env()
            cfg = registry.get("jarvis")
            assert cfg.local_path == Path("/tmp/jarvis-test")

    def test_from_env_includes_prime_when_set(self):
        from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry

        env = {
            "JARVIS_REPO_PATH": "/tmp/j",
            "JARVIS_PRIME_REPO_PATH": "/tmp/p",
        }
        with patch.dict("os.environ", env, clear=False):
            registry = RepoRegistry.from_env()
            assert registry.get("prime").local_path == Path("/tmp/p")

    def test_from_env_omits_prime_when_unset(self):
        from backend.core.ouroboros.governance.multi_repo.registry import RepoRegistry

        env = {"JARVIS_REPO_PATH": "/tmp/j"}
        with patch.dict("os.environ", env, clear=False):
            registry = RepoRegistry.from_env()
            with pytest.raises(KeyError):
                registry.get("prime")


class TestRepoRegistryFileOps:
    @pytest.mark.asyncio
    async def test_read_file_delegates_to_connector(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        # Create a real file in tmp_path
        (tmp_path / "src" / "foo.py").parent.mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("hello = 42")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        content = await registry.read_file("jarvis", "src/foo.py")
        assert content == "hello = 42"

    @pytest.mark.asyncio
    async def test_read_file_returns_none_for_missing(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        content = await registry.read_file("jarvis", "nonexistent.py")
        assert content is None

    @pytest.mark.asyncio
    async def test_search_files_finds_by_glob(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("pass")
        (tmp_path / "tests" / "test_b.py").write_text("pass")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        matches = await registry.search_files("tests/test_*.py", repo="jarvis")
        assert len(matches) == 2
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/multi_repo/test_registry.py -v`

**Step 3: Write implementation**

```python
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

        # Always include jarvis
        jarvis_path = os.environ.get("JARVIS_REPO_PATH", ".")
        configs.append(RepoConfig(
            name="jarvis",
            local_path=Path(jarvis_path),
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

        # Optional: reactor-core
        reactor_path = os.environ.get("JARVIS_REACTOR_REPO_PATH")
        if reactor_path:
            configs.append(RepoConfig(
                name="reactor-core",
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
        file_path = config.local_path / path
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
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/multi_repo/test_registry.py -v`

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/multi_repo/__init__.py \
       backend/core/ouroboros/governance/multi_repo/registry.py \
       tests/governance/multi_repo/__init__.py \
       tests/governance/multi_repo/test_registry.py
git commit -m "feat(multi-repo): add RepoConfig and RepoRegistry with env-var registration"
```

---

## Task 2: Cross-Repo Context Builder (`context_builder.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/multi_repo/context_builder.py`
- Test: `tests/governance/multi_repo/test_context_builder.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/multi_repo/test_context_builder.py"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock


class TestContextFile:
    def test_frozen_dataclass(self):
        from backend.core.ouroboros.governance.multi_repo.context_builder import ContextFile

        cf = ContextFile(
            repo="jarvis",
            path="src/foo.py",
            content="x = 1",
            relevance="import_dependency",
        )
        assert cf.repo == "jarvis"
        with pytest.raises(AttributeError):
            cf.repo = "other"


class TestCrossRepoContext:
    def test_creation(self):
        from backend.core.ouroboros.governance.multi_repo.context_builder import (
            CrossRepoContext, ContextFile,
        )

        ctx = CrossRepoContext(
            primary_file="tests/test_a.py",
            primary_repo="jarvis",
            related_files=(
                ContextFile(repo="jarvis", path="src/a.py", content="code", relevance="test"),
            ),
            total_tokens_estimate=500,
        )
        assert ctx.primary_repo == "jarvis"
        assert len(ctx.related_files) == 1
        assert ctx.total_tokens_estimate == 500


class TestContextBuilderFindImports:
    @pytest.mark.asyncio
    async def test_finds_import_in_same_repo(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.context_builder import ContextBuilder
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        # Set up a file with an import
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "a.py").write_text(
            "from backend.core.utils import helper\nx = 1\n"
        )
        (tmp_path / "backend" / "core").mkdir(parents=True)
        (tmp_path / "backend" / "core" / "utils.py").write_text("def helper(): pass\n")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        builder = ContextBuilder(registry=registry, token_budget=10000)
        related = await builder.find_related_files("jarvis", "src/a.py")
        paths = [f.path for f in related]
        assert any("utils.py" in p for p in paths)


class TestContextBuilderFindTestCompanion:
    @pytest.mark.asyncio
    async def test_finds_test_file_for_source(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.context_builder import ContextBuilder
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "utils.py").write_text("def foo(): pass\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_utils.py").write_text("def test_foo(): pass\n")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        builder = ContextBuilder(registry=registry, token_budget=10000)
        related = await builder.find_related_files("jarvis", "src/utils.py")
        paths = [f.path for f in related]
        assert any("test_utils.py" in p for p in paths)


class TestContextBuilderBuild:
    @pytest.mark.asyncio
    async def test_build_returns_context_with_primary_content(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.context_builder import ContextBuilder
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("def test_edge(): assert False\n")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        builder = ContextBuilder(registry=registry, token_budget=10000)

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_a.py",),
            repo="jarvis",
            description="test failure",
            evidence={"signature": "assert:test_a"},
            confidence=0.9,
            stable=True,
        )
        ctx = await builder.build(signal)
        assert ctx.primary_file == "tests/test_a.py"
        assert ctx.primary_repo == "jarvis"
        assert ctx.total_tokens_estimate > 0


class TestContextBuilderTokenBudget:
    @pytest.mark.asyncio
    async def test_respects_token_budget(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.context_builder import ContextBuilder
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        # Create a file with a huge body
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "big.py").write_text("x = 1\n" * 50000)
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_big.py").write_text(
            "from src.big import x\ndef test_x(): pass\n"
        )

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        builder = ContextBuilder(registry=registry, token_budget=100)

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_big.py",),
            repo="jarvis",
            description="test failure",
            evidence={"signature": "x:big"},
            confidence=0.9,
            stable=True,
        )
        ctx = await builder.build(signal)
        # Token estimate should be near or below budget
        assert ctx.total_tokens_estimate <= 200  # some slack for primary file
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/multi_repo/test_context_builder.py -v`

**Step 3: Write implementation**

```python
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

        # Read content and enforce token budget
        context_files: List[ContextFile] = []
        total_chars = 0

        for rel in related:
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
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/multi_repo/test_context_builder.py -v`

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/multi_repo/context_builder.py \
       tests/governance/multi_repo/test_context_builder.py
git commit -m "feat(multi-repo): add ContextBuilder for cross-repo prompt context"
```

---

## Task 3: Cross-Repo Blast Radius (`blast_radius.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/multi_repo/blast_radius.py`
- Test: `tests/governance/multi_repo/test_blast_radius.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/multi_repo/test_blast_radius.py"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock


class TestAffectedFile:
    def test_frozen_dataclass(self):
        from backend.core.ouroboros.governance.multi_repo.blast_radius import AffectedFile

        af = AffectedFile(
            repo="prime",
            path="src/api_client.py",
            dependency_type="imports",
        )
        assert af.repo == "prime"
        with pytest.raises(AttributeError):
            af.repo = "other"


class TestBlastRadiusReport:
    def test_single_repo_no_boundary_crossing(self):
        from backend.core.ouroboros.governance.multi_repo.blast_radius import (
            BlastRadiusReport, AffectedFile,
        )

        report = BlastRadiusReport(
            target_repo="jarvis",
            target_files=("src/a.py",),
            affected_repos=("jarvis",),
            affected_files=(
                AffectedFile(repo="jarvis", path="tests/test_a.py", dependency_type="tests"),
            ),
            crosses_repo_boundary=False,
            risk_escalation=None,
            contract_impact=None,
        )
        assert not report.crosses_repo_boundary
        assert report.risk_escalation is None

    def test_cross_repo_boundary_sets_escalation(self):
        from backend.core.ouroboros.governance.multi_repo.blast_radius import (
            BlastRadiusReport, AffectedFile,
        )

        report = BlastRadiusReport(
            target_repo="jarvis",
            target_files=("src/api_client.py",),
            affected_repos=("jarvis", "prime"),
            affected_files=(
                AffectedFile(repo="prime", path="src/handler.py", dependency_type="calls_api"),
            ),
            crosses_repo_boundary=True,
            risk_escalation="approval_required",
            contract_impact="api_changed",
        )
        assert report.crosses_repo_boundary
        assert report.risk_escalation == "approval_required"


class TestCrossRepoBlastRadiusAnalyze:
    @pytest.mark.asyncio
    async def test_single_repo_file_no_cross_impact(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.blast_radius import (
            CrossRepoBlastRadius,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("def test_x(): pass\n")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=tmp_path, canary_slices=("tests/",)),
        ))
        analyzer = CrossRepoBlastRadius(registry=registry)

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_a.py",),
            repo="jarvis",
            description="test failure",
            evidence={"signature": "assert:test_a"},
            confidence=0.9,
            stable=True,
        )
        report = await analyzer.analyze(signal)
        assert report.target_repo == "jarvis"
        assert not report.crosses_repo_boundary
        assert report.risk_escalation is None

    @pytest.mark.asyncio
    async def test_cross_repo_import_detected(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.blast_radius import (
            CrossRepoBlastRadius,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        # Jarvis repo
        jarvis = tmp_path / "jarvis"
        jarvis.mkdir()
        (jarvis / "src").mkdir()
        (jarvis / "src" / "api_client.py").write_text("class APIClient: pass\n")

        # Prime repo references jarvis api_client
        prime = tmp_path / "prime"
        prime.mkdir()
        (prime / "src").mkdir()
        (prime / "src" / "handler.py").write_text(
            "# uses jarvis api_client\nimport api_client\n"
        )

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=jarvis, canary_slices=("tests/",)),
            RepoConfig(name="prime", local_path=prime, canary_slices=("tests/",)),
        ))
        analyzer = CrossRepoBlastRadius(registry=registry)

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("src/api_client.py",),
            repo="jarvis",
            description="test failure in api_client",
            evidence={"signature": "err:api_client"},
            confidence=0.9,
            stable=True,
        )
        report = await analyzer.analyze(signal)
        assert report.crosses_repo_boundary
        assert report.risk_escalation == "approval_required"
        assert "prime" in report.affected_repos

    @pytest.mark.asyncio
    async def test_blast_radius_count(self, tmp_path):
        from backend.core.ouroboros.governance.multi_repo.blast_radius import (
            CrossRepoBlastRadius,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        jarvis = tmp_path / "jarvis"
        jarvis.mkdir()
        (jarvis / "src").mkdir()
        (jarvis / "src" / "utils.py").write_text("def helper(): pass\n")
        (jarvis / "tests").mkdir()
        (jarvis / "tests" / "test_utils.py").write_text("from src.utils import helper\n")

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=jarvis, canary_slices=("tests/",)),
        ))
        analyzer = CrossRepoBlastRadius(registry=registry)

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("src/utils.py",),
            repo="jarvis",
            description="test failure",
            evidence={"signature": "err:utils"},
            confidence=0.9,
            stable=True,
        )
        report = await analyzer.analyze(signal)
        assert len(report.affected_files) >= 1
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/multi_repo/test_blast_radius.py -v`

**Step 3: Write implementation**

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/multi_repo/test_blast_radius.py -v`

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/multi_repo/blast_radius.py \
       tests/governance/multi_repo/test_blast_radius.py
git commit -m "feat(multi-repo): add CrossRepoBlastRadius with cross-boundary detection"
```

---

## Task 4: Repo Pipeline Manager (`repo_pipeline.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/multi_repo/repo_pipeline.py`
- Test: `tests/governance/multi_repo/test_repo_pipeline.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/multi_repo/test_repo_pipeline.py"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


class TestRepoPipelineManagerSubmit:
    @pytest.mark.asyncio
    async def test_routes_signal_to_correct_repo_pipeline(self):
        from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
            RepoPipelineManager,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
            RepoConfig(name="prime", local_path=Path("/tmp/p"), canary_slices=("tests/",)),
        ))

        mock_jarvis_gls = AsyncMock()
        mock_jarvis_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-j01"))
        mock_prime_gls = AsyncMock()
        mock_prime_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-p01"))

        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": mock_jarvis_gls, "prime": mock_prime_gls},
        )

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_a.py",),
            repo="jarvis",
            description="test failure",
            evidence={"signature": "err:test_a"},
            confidence=0.9,
            stable=True,
        )
        result = await manager.submit(signal)
        mock_jarvis_gls.submit.assert_called_once()
        mock_prime_gls.submit.assert_not_called()
        assert result.op_id == "op-j01"

    @pytest.mark.asyncio
    async def test_routes_prime_signal_to_prime_pipeline(self):
        from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
            RepoPipelineManager,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
            RepoConfig(name="prime", local_path=Path("/tmp/p"), canary_slices=("tests/",)),
        ))

        mock_jarvis_gls = AsyncMock()
        mock_prime_gls = AsyncMock()
        mock_prime_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-p01"))

        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": mock_jarvis_gls, "prime": mock_prime_gls},
        )

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_prime.py",),
            repo="prime",
            description="prime test failure",
            evidence={"signature": "err:prime"},
            confidence=0.9,
            stable=True,
        )
        result = await manager.submit(signal)
        mock_prime_gls.submit.assert_called_once()
        mock_jarvis_gls.submit.assert_not_called()


class TestRepoPipelineManagerUnknownRepo:
    @pytest.mark.asyncio
    async def test_raises_for_unknown_repo(self):
        from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
            RepoPipelineManager,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
        ))
        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": AsyncMock()},
        )

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("tests/test_x.py",),
            repo="unknown_repo",
            description="test failure",
            evidence={"signature": "err:x"},
            confidence=0.9,
            stable=True,
        )
        with pytest.raises(KeyError, match="unknown_repo"):
            await manager.submit(signal)


class TestRepoPipelineManagerBlastRadius:
    @pytest.mark.asyncio
    async def test_attaches_blast_radius_to_context(self):
        from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
            RepoPipelineManager,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )
        from backend.core.ouroboros.governance.multi_repo.blast_radius import (
            BlastRadiusReport, CrossRepoBlastRadius,
        )
        from backend.core.ouroboros.governance.intent.signals import IntentSignal

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
        ))

        mock_gls = AsyncMock()
        mock_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-001"))

        mock_blast = AsyncMock(spec=CrossRepoBlastRadius)
        mock_blast.analyze = AsyncMock(return_value=BlastRadiusReport(
            target_repo="jarvis",
            target_files=("src/a.py",),
            affected_repos=("jarvis",),
            affected_files=(),
            crosses_repo_boundary=False,
            risk_escalation=None,
            contract_impact=None,
        ))

        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": mock_gls},
            blast_radius_analyzer=mock_blast,
        )

        signal = IntentSignal(
            source="intent:test_failure",
            target_files=("src/a.py",),
            repo="jarvis",
            description="test failure",
            evidence={"signature": "err:a"},
            confidence=0.9,
            stable=True,
        )
        await manager.submit(signal)

        # Blast radius should have been called
        mock_blast.analyze.assert_called_once_with(signal)

        # GLS.submit should have been called with the context
        mock_gls.submit.assert_called_once()
        call_args = mock_gls.submit.call_args
        assert call_args[1]["trigger_source"] == "intent:test_failure"


class TestRepoPipelineManagerLifecycle:
    @pytest.mark.asyncio
    async def test_start_all_calls_start_on_each(self):
        from backend.core.ouroboros.governance.multi_repo.repo_pipeline import (
            RepoPipelineManager,
        )
        from backend.core.ouroboros.governance.multi_repo.registry import (
            RepoConfig, RepoRegistry,
        )

        registry = RepoRegistry(configs=(
            RepoConfig(name="jarvis", local_path=Path("/tmp/j"), canary_slices=("tests/",)),
        ))

        mock_gls = AsyncMock()
        mock_gls.start = AsyncMock()
        mock_gls.stop = AsyncMock()

        manager = RepoPipelineManager(
            registry=registry,
            pipelines={"jarvis": mock_gls},
        )
        await manager.start_all()
        mock_gls.start.assert_called_once()

        await manager.stop_all()
        mock_gls.stop.assert_called_once()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/multi_repo/test_repo_pipeline.py -v`

**Step 3: Write implementation**

```python
"""backend/core/ouroboros/governance/multi_repo/repo_pipeline.py

Per-repo GovernedLoopService orchestration.

Routes IntentSignals to the correct repo's pipeline, enriches the
OperationContext with blast radius data, and manages lifecycle.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §3
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from backend.core.ouroboros.governance.intent.signals import IntentSignal
from backend.core.ouroboros.governance.op_context import OperationContext
from backend.core.ouroboros.governance.operation_id import generate_operation_id

from .blast_radius import CrossRepoBlastRadius
from .registry import RepoRegistry

logger = logging.getLogger(__name__)


class RepoPipelineManager:
    """Manages GovernedLoopService instances per repo.

    Routes signals to the correct pipeline and enriches context
    with blast radius analysis.
    """

    def __init__(
        self,
        registry: RepoRegistry,
        pipelines: Dict[str, Any],
        blast_radius_analyzer: Optional[CrossRepoBlastRadius] = None,
    ) -> None:
        self._registry = registry
        self._pipelines = pipelines
        self._blast_analyzer = blast_radius_analyzer

    async def submit(self, signal: IntentSignal) -> Any:
        """Route signal to the correct repo's pipeline.

        1. Look up the pipeline for signal.repo
        2. Run blast radius analysis (if analyzer available)
        3. Build OperationContext
        4. Submit to the pipeline
        """
        repo_name = signal.repo
        if repo_name not in self._pipelines:
            raise KeyError(
                f"{repo_name}: no pipeline registered for this repo"
            )

        pipeline = self._pipelines[repo_name]

        # Blast radius analysis
        blast_report = None
        if self._blast_analyzer is not None:
            try:
                blast_report = await self._blast_analyzer.analyze(signal)
            except Exception:
                logger.warning(
                    "Blast radius analysis failed for %s, proceeding without",
                    signal.description,
                )

        # Build operation context
        op_id = generate_operation_id(signal.repo)
        ctx = OperationContext.create(
            target_files=signal.target_files,
            description=signal.description,
            op_id=op_id,
        )

        # Submit to the repo's pipeline
        result = await pipeline.submit(
            ctx,
            trigger_source=signal.source,
        )

        if blast_report and blast_report.crosses_repo_boundary:
            logger.info(
                "Cross-repo impact detected for op %s: affects %s",
                op_id,
                blast_report.affected_repos,
            )

        return result

    async def start_all(self) -> None:
        """Start all registered pipelines."""
        for name, pipeline in self._pipelines.items():
            try:
                await pipeline.start()
                logger.info("Pipeline started: %s", name)
            except Exception:
                logger.exception("Failed to start pipeline: %s", name)

    async def stop_all(self) -> None:
        """Stop all registered pipelines."""
        for name, pipeline in self._pipelines.items():
            try:
                await pipeline.stop()
                logger.info("Pipeline stopped: %s", name)
            except Exception:
                logger.exception("Failed to stop pipeline: %s", name)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/multi_repo/test_repo_pipeline.py -v`

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/multi_repo/repo_pipeline.py \
       tests/governance/multi_repo/test_repo_pipeline.py
git commit -m "feat(multi-repo): add RepoPipelineManager for per-repo pipeline routing"
```

---

## Task 5: Package Exports + Governance Wiring

**Files:**
- Modify: `backend/core/ouroboros/governance/multi_repo/__init__.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`
- Test: `tests/governance/multi_repo/test_exports.py`

**Step 1: Write the failing test**

```python
"""tests/governance/multi_repo/test_exports.py"""


def test_multi_repo_public_api():
    from backend.core.ouroboros.governance.multi_repo import (
        RepoConfig,
        RepoRegistry,
        FileMatch,
        ContextBuilder,
        ContextFile,
        CrossRepoContext,
        CrossRepoBlastRadius,
        AffectedFile,
        BlastRadiusReport,
        RepoPipelineManager,
    )
    assert RepoConfig is not None
    assert RepoRegistry is not None
    assert RepoPipelineManager is not None
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/multi_repo/test_exports.py -v`

**Step 3: Write implementation**

Update `backend/core/ouroboros/governance/multi_repo/__init__.py`:

```python
"""Public API for the multi-repo coordinator layer."""
from .registry import RepoConfig, RepoRegistry, FileMatch
from .context_builder import ContextBuilder, ContextFile, CrossRepoContext
from .blast_radius import CrossRepoBlastRadius, AffectedFile, BlastRadiusReport
from .repo_pipeline import RepoPipelineManager

__all__ = [
    "RepoConfig",
    "RepoRegistry",
    "FileMatch",
    "ContextBuilder",
    "ContextFile",
    "CrossRepoContext",
    "CrossRepoBlastRadius",
    "AffectedFile",
    "BlastRadiusReport",
    "RepoPipelineManager",
]
```

Append to `backend/core/ouroboros/governance/__init__.py` (after the comms block):

```python
# --- Multi-Repo Coordinator (Layer 2) ---
from backend.core.ouroboros.governance.multi_repo import (
    RepoConfig,
    RepoRegistry,
    FileMatch,
    ContextBuilder,
    ContextFile,
    CrossRepoContext,
    CrossRepoBlastRadius,
    AffectedFile,
    BlastRadiusReport,
    RepoPipelineManager,
)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/multi_repo/test_exports.py -v`

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/multi_repo/__init__.py \
       backend/core/ouroboros/governance/__init__.py \
       tests/governance/multi_repo/test_exports.py
git commit -m "feat(multi-repo): export public API and wire into governance package"
```

---

## Task 6: E2E Integration Tests

**Files:**
- Create: `tests/governance/multi_repo/test_e2e_multi_repo.py`

**Step 1: Write the integration tests**

```python
"""tests/governance/multi_repo/test_e2e_multi_repo.py

End-to-end: IntentSignal flows through ContextBuilder, BlastRadius,
and RepoPipelineManager to a mocked GovernedLoopService.
"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_e2e_single_repo_fix_pipeline(tmp_path):
    """Full flow: signal -> context -> blast radius -> pipeline submit."""
    from backend.core.ouroboros.governance.multi_repo import (
        RepoConfig,
        RepoRegistry,
        ContextBuilder,
        CrossRepoBlastRadius,
        RepoPipelineManager,
    )
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    # Set up a simple repo with source + test
    jarvis = tmp_path / "jarvis"
    jarvis.mkdir()
    (jarvis / "src").mkdir()
    (jarvis / "src" / "utils.py").write_text("def helper(): return 42\n")
    (jarvis / "tests").mkdir()
    (jarvis / "tests" / "test_utils.py").write_text(
        "from src.utils import helper\ndef test_helper(): assert helper() == 42\n"
    )

    registry = RepoRegistry(configs=(
        RepoConfig(name="jarvis", local_path=jarvis, canary_slices=("tests/",)),
    ))

    # Build context
    builder = ContextBuilder(registry=registry, token_budget=10000)
    signal = IntentSignal(
        source="intent:test_failure",
        target_files=("tests/test_utils.py",),
        repo="jarvis",
        description="test_helper failed",
        evidence={"signature": "assert:test_utils:42"},
        confidence=0.9,
        stable=True,
    )
    ctx = await builder.build(signal)
    assert ctx.primary_file == "tests/test_utils.py"
    assert len(ctx.related_files) >= 1  # should find utils.py

    # Blast radius
    blast = CrossRepoBlastRadius(registry=registry)
    report = await blast.analyze(signal)
    assert not report.crosses_repo_boundary

    # Submit through pipeline manager
    mock_gls = AsyncMock()
    mock_gls.submit = AsyncMock(return_value=MagicMock(op_id="op-e2e"))

    manager = RepoPipelineManager(
        registry=registry,
        pipelines={"jarvis": mock_gls},
        blast_radius_analyzer=blast,
    )
    result = await manager.submit(signal)
    assert result.op_id == "op-e2e"
    mock_gls.submit.assert_called_once()


@pytest.mark.asyncio
async def test_e2e_cross_repo_escalation(tmp_path):
    """Cross-repo signal escalates risk to approval_required."""
    from backend.core.ouroboros.governance.multi_repo import (
        RepoConfig,
        RepoRegistry,
        CrossRepoBlastRadius,
        RepoPipelineManager,
    )
    from backend.core.ouroboros.governance.intent.signals import IntentSignal

    # Jarvis has a module
    jarvis = tmp_path / "jarvis"
    jarvis.mkdir()
    (jarvis / "src").mkdir()
    (jarvis / "src" / "shared_api.py").write_text("class SharedAPI: pass\n")

    # Prime imports it
    prime = tmp_path / "prime"
    prime.mkdir()
    (prime / "src").mkdir()
    (prime / "src" / "consumer.py").write_text(
        "import shared_api\napi = shared_api.SharedAPI()\n"
    )

    registry = RepoRegistry(configs=(
        RepoConfig(name="jarvis", local_path=jarvis, canary_slices=("tests/",)),
        RepoConfig(name="prime", local_path=prime, canary_slices=("tests/",)),
    ))

    blast = CrossRepoBlastRadius(registry=registry)
    signal = IntentSignal(
        source="intent:test_failure",
        target_files=("src/shared_api.py",),
        repo="jarvis",
        description="SharedAPI test failure",
        evidence={"signature": "err:shared_api"},
        confidence=0.9,
        stable=True,
    )

    report = await blast.analyze(signal)
    assert report.crosses_repo_boundary
    assert report.risk_escalation == "approval_required"
    assert "prime" in report.affected_repos
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/governance/multi_repo/test_e2e_multi_repo.py -v`

**Step 3: Commit**

```bash
git add tests/governance/multi_repo/test_e2e_multi_repo.py
git commit -m "test(multi-repo): add E2E integration tests for cross-repo pipeline"
```

---

## Task 7: Full Test Suite Verification

**Step 1: Run all multi-repo tests**

Run: `python3 -m pytest tests/governance/multi_repo/ -v --tb=short`

**Step 2: Run all governance tests for regressions**

Run: `python3 -m pytest tests/governance/ -v --tb=short`

**Step 3: Verify imports**

Run: `python3 -c "from backend.core.ouroboros.governance.multi_repo import RepoConfig, RepoRegistry, ContextBuilder, CrossRepoBlastRadius, RepoPipelineManager; print('All imports OK')"`

---

## Summary

| Task | Module | Tests | Purpose |
|------|--------|-------|---------|
| 1 | `registry.py` | 10 | RepoConfig + RepoRegistry with env-var registration |
| 2 | `context_builder.py` | 6 | Cross-repo context for generation prompts |
| 3 | `blast_radius.py` | 5 | Cross-repo impact analysis |
| 4 | `repo_pipeline.py` | 5 | Per-repo GovernedLoopService routing |
| 5 | `__init__.py` | 1 | Package exports |
| 6 | E2E tests | 2 | Full pipeline + cross-repo escalation |
| 7 | Suite run | -- | Regression check |

**Total: ~29 tests across 7 tasks, 4 new source files.**
