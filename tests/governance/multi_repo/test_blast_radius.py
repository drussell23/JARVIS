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
