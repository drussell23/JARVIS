"""Memory management module for the backend application.

This module provides memory-related functionality and utilities for the backend
system. It serves as the entry point for memory management components including
caching, data storage, and memory optimization features.

Components:
- ExperienceRecorder: Black Box for JARVIS Data Flywheel (RLHF training)
- Experience Types: Data models for interaction recording

The module exposes key memory management classes and functions that can be
imported and used throughout the application for efficient memory handling
and data persistence.
"""

# Experience Recorder - Data Flywheel for RLHF
from .experience_types import (
    ExperienceRecord,
    Outcome,
    OutcomeSignal,
    OutcomeUpdate,
    PromptContext,
    RecorderMetrics,
    ResponseType,
    ToolCategory,
    ToolUsage,
)

from .experience_recorder import (
    ExperienceConfig,
    ExperienceRecorder,
    get_experience_recorder,
    get_experience_recorder_async,
)

__version__ = "2.0.0"
__author__ = "JARVIS v5.0 Data Flywheel"

# Package-level exports
__all__ = [
    # Experience Recorder (Data Flywheel)
    "ExperienceRecorder",
    "ExperienceConfig",
    "get_experience_recorder",
    "get_experience_recorder_async",

    # Experience Types
    "ExperienceRecord",
    "Outcome",
    "OutcomeSignal",
    "OutcomeUpdate",
    "PromptContext",
    "RecorderMetrics",
    "ResponseType",
    "ToolCategory",
    "ToolUsage",
]