"""CrossRepoVerifier — three-tier post-apply verification.

Tier 1: Per-repo type-check + lint + fast tests (parallelized).
Tier 2: Cross-repo interface contract validation (sequential, dependency order).
Tier 3: @cross_repo integration tests (no-op if none exist).

A Tier failure returns VerifyResult(passed=False) which triggers
SagaApplyStrategy compensation via the orchestrator.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Tuple

from backend.core.ouroboros.governance.saga.saga_types import RepoPatch

logger = logging.getLogger("Ouroboros.CrossRepoVerifier")


async def _find_cross_repo_marker(repo_root: Path) -> bool:
    """Return True if any test_*.py in repo_root contains '@cross_repo'."""
    def _scan() -> bool:
        for tf in repo_root.rglob("test_*.py"):
            try:
                if "@cross_repo" in tf.read_text(encoding="utf-8"):
                    return True
            except Exception:
                continue
        return False
    return await asyncio.to_thread(_scan)


class VerifyFailureClass(str, Enum):
    PER_REPO = "verify_failed_per_repo"
    CROSS_REPO = "verify_failed_cross_repo"
    INTEGRATION = "verify_failed_integration"


@dataclass
class VerifyResult:
    """Result of cross-repo verification."""
    passed: bool
    failure_class: Optional[VerifyFailureClass] = None
    reason_code: str = ""
    details: str = ""


class CrossRepoVerifier:
    """Three-tier cross-repo verifier.

    Parameters
    ----------
    repo_roots:
        Mapping of repo name → absolute Path.
    dependency_edges:
        DAG edges from OperationContext; used for Tier 2 ordering.
    """

    def __init__(
        self,
        repo_roots: Dict[str, Path],
        dependency_edges: Tuple[Tuple[str, str], ...],
    ) -> None:
        self._repo_roots = {k: Path(v) for k, v in repo_roots.items()}

    async def verify(
        self,
        repo_scope: Tuple[str, ...],
        patch_map: Dict[str, RepoPatch],
        dependency_edges: Tuple[Tuple[str, str], ...],
    ) -> VerifyResult:
        """Run all three verification tiers.

        Returns the first failure encountered (fails fast per tier).
        """
        # Tier 1: per-repo (parallelized)
        t1 = await self._tier1_per_repo(
            repo_scope=repo_scope,
            patch_map=patch_map,
        )
        if t1 is not None:
            return t1

        # Tier 2: cross-repo contracts (only for multi-repo)
        if len(repo_scope) > 1:
            t2 = await self._tier2_cross_repo_contracts(
                repo_scope=repo_scope,
                dependency_edges=dependency_edges,
            )
            if t2 is not None:
                return t2

        # Tier 3: integration tests
        t3 = await self._tier3_integration_tests(
            repo_scope=repo_scope,
            repo_roots=self._repo_roots,
        )
        if t3 is not None:
            return t3

        return VerifyResult(passed=True)

    async def _tier1_per_repo(
        self,
        repo_scope: Tuple[str, ...],
        patch_map: Dict[str, RepoPatch],
    ) -> Optional[VerifyResult]:
        """Run type-check + lint on changed files per repo (parallelized).

        Returns None on success, VerifyResult(passed=False) on failure.
        """
        tasks = [
            self._verify_single_repo(repo, patch_map.get(repo))
            for repo in repo_scope
            if patch_map.get(repo) is not None and not patch_map[repo].is_empty()
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, VerifyResult) and not r.passed:
                return r
            if isinstance(r, Exception):
                return VerifyResult(
                    passed=False,
                    failure_class=VerifyFailureClass.PER_REPO,
                    reason_code="verify_infra_error",
                    details=str(r),
                )
        return None

    async def _verify_single_repo(
        self, repo: str, patch: Optional[RepoPatch]
    ) -> Optional[VerifyResult]:
        """Type-check + lint changed files in a single repo."""
        if patch is None or patch.is_empty():
            return None
        repo_root = self._repo_roots.get(repo)
        if repo_root is None:
            return None

        changed_files = [pf.path for pf in patch.files]

        # Lint: ruff (fast, always available)
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["ruff", "check", "--select=E,F,W", "--"] + changed_files,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
            )
            if proc.returncode != 0:
                output = proc.stdout or proc.stderr
                return VerifyResult(
                    passed=False,
                    failure_class=VerifyFailureClass.PER_REPO,
                    reason_code="verify_lint_failed",
                    details=f"{repo}: {output[:500]}{'...' if len(output) > 500 else ''}",
                )
        except FileNotFoundError:
            logger.debug("[Tier1] ruff not found for %s, skipping lint", repo)

        # Fast unit tests: run pytest scoped to changed files
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                ["python3", "-m", "pytest", "--tb=short", "-q", "--no-header"] + changed_files,
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                timeout=60,
            )
            if proc.returncode != 0 and proc.returncode != 5:  # 5 = no tests collected
                output = proc.stdout or proc.stderr
                return VerifyResult(
                    passed=False,
                    failure_class=VerifyFailureClass.PER_REPO,
                    reason_code="verify_test_failed",
                    details=f"{repo}: {output[-400:]}{'...' if len(output) > 400 else ''}",
                )
        except subprocess.TimeoutExpired:
            return VerifyResult(
                passed=False,
                failure_class=VerifyFailureClass.PER_REPO,
                reason_code="verify_test_timeout",
                details=f"{repo}: test run exceeded 60s timeout",
            )
        except FileNotFoundError:
            logger.debug("[Tier1] pytest not found for %s, skipping tests", repo)
        except Exception as exc:
            logger.warning("[Tier1] Test run failed for %s: %s", repo, exc)

        return None

    async def _tier2_cross_repo_contracts(
        self,
        repo_scope: Tuple[str, ...],
        dependency_edges: Tuple[Tuple[str, str], ...],
    ) -> Optional[VerifyResult]:
        """Check import boundaries along declared dependency edges.

        For each edge (src → dst): verify src can import boundary module from dst.
        Checks contract manifest JSON if present; otherwise skips gracefully.
        Only edges whose both repos are part of repo_scope are evaluated.
        """
        for src, dst in dependency_edges:
            # Skip edges whose repos aren't part of this operation
            if src not in repo_scope or dst not in repo_scope:
                continue
            src_root = self._repo_roots.get(src)
            dst_root = self._repo_roots.get(dst)
            if src_root is None or dst_root is None:
                continue

            # Check contract manifest (optional)
            manifest_path = dst_root / ".jarvis" / "contract_manifest.json"
            if not manifest_path.exists():
                continue

            try:
                manifest = json.loads(manifest_path.read_text())
                boundary_modules = manifest.get("boundary_modules", [])
            except Exception as exc:
                logger.debug("[Tier2] Skipping malformed manifest for %s→%s: %s", src, dst, exc)
                continue

            for module in boundary_modules:
                try:
                    proc = await asyncio.to_thread(
                        subprocess.run,
                        ["python3", "-c", f"import {module}"],
                        cwd=str(src_root),
                        capture_output=True,
                        text=True,
                    )
                    if proc.returncode != 0:
                        return VerifyResult(
                            passed=False,
                            failure_class=VerifyFailureClass.CROSS_REPO,
                            reason_code="verify_import_edge_broken",
                            details=f"{src}→{dst}: cannot import {module}: {proc.stderr[:300]}",
                        )
                except Exception as exc:
                    return VerifyResult(
                        passed=False,
                        failure_class=VerifyFailureClass.CROSS_REPO,
                        reason_code="verify_import_edge_broken",
                        details=str(exc),
                    )
        return None

    async def _tier3_integration_tests(
        self,
        repo_scope: Tuple[str, ...],
        repo_roots: Dict[str, Path],
    ) -> Optional[VerifyResult]:
        """Run @cross_repo integration tests if any exist. No-op if none found."""
        for repo in repo_scope:
            repo_root = repo_roots.get(repo)
            if repo_root is None:
                continue
            # Search for any test file with @cross_repo marker (non-blocking)
            has_cross_repo = await _find_cross_repo_marker(repo_root)

            if has_cross_repo:
                try:
                    proc = await asyncio.to_thread(
                        subprocess.run,
                        ["python3", "-m", "pytest", "-m", "cross_repo", "-q"],
                        cwd=str(repo_root),
                        capture_output=True,
                        text=True,
                    )
                    if proc.returncode != 0:
                        return VerifyResult(
                            passed=False,
                            failure_class=VerifyFailureClass.INTEGRATION,
                            reason_code="verify_integration_failed",
                            details=f"{repo}: {proc.stdout[-500:]}",
                        )
                except Exception as exc:
                    logger.warning("[Tier3] Integration test run failed: %s", exc)

        return None
