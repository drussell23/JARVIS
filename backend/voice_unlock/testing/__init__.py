"""
Voice Unlock Testing Module.
=============================

Provides A/B testing framework for voice authentication models.

Author: JARVIS Trinity v81.0
"""

from .ab_testing import (
    ABTestingManager,
    ABTest,
    ExperimentConfig,
    ExperimentResult,
    ExperimentStatus,
    ExperimentVariant,
    WinnerDecision,
    get_ab_testing_manager,
    start_ab_test,
)

__all__ = [
    "ABTestingManager",
    "ABTest",
    "ExperimentConfig",
    "ExperimentResult",
    "ExperimentStatus",
    "ExperimentVariant",
    "WinnerDecision",
    "get_ab_testing_manager",
    "start_ab_test",
]
