"""
Voice Unlock Learning Module.
==============================

Provides the unified learning loop components for voice authentication:
- Experience collection and forwarding to Reactor-Core
- Model deployment with A/B testing
- Automatic rollback on degradation

Author: JARVIS Trinity v81.0
"""

from .voice_experience_collector import (
    VoiceExperienceCollector,
    VoiceExperience,
    ExperienceOutcome,
    ExperienceQuality,
    get_voice_experience_collector,
    collect_voice_experience,
)

from .model_deployer import (
    VoiceModelDeployer,
    ModelType,
    ModelVersion,
    DeploymentStrategy,
    DeploymentResult,
    get_voice_model_deployer,
    deploy_voice_model,
)

__all__ = [
    # Experience Collector
    "VoiceExperienceCollector",
    "VoiceExperience",
    "ExperienceOutcome",
    "ExperienceQuality",
    "get_voice_experience_collector",
    "collect_voice_experience",
    # Model Deployer
    "VoiceModelDeployer",
    "ModelType",
    "ModelVersion",
    "DeploymentStrategy",
    "DeploymentResult",
    "get_voice_model_deployer",
    "deploy_voice_model",
]
