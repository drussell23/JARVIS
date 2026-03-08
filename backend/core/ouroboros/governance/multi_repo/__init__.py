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
