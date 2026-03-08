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
