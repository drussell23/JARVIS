"""Saga orchestration package for multi-repo applies."""
from .cross_repo_verifier import CrossRepoVerifier, VerifyFailureClass, VerifyResult
from .saga_apply_strategy import SagaApplyStrategy
from .saga_types import (
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaApplyResult,
    SagaTerminalState,
)

__all__ = [
    "CrossRepoVerifier",
    "FileOp",
    "PatchedFile",
    "RepoPatch",
    "SagaApplyResult",
    "SagaApplyStrategy",
    "SagaTerminalState",
    "VerifyFailureClass",
    "VerifyResult",
]
