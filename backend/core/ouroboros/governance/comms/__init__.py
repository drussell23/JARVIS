"""Public API for the real-time communication layer."""
from .narrator_script import format_narration, SCRIPTS
from .voice_narrator import VoiceNarrator
from .ops_logger import OpsLogger
from .cross_repo_narrator import CrossRepoNarrator
from .tui_panel import (
    TUISelfProgramPanel,
    SelfProgramPanelState,
    PipelineStatus,
    CompletionSummary,
)

__all__ = [
    "format_narration",
    "SCRIPTS",
    "VoiceNarrator",
    "OpsLogger",
    "CrossRepoNarrator",
    "TUISelfProgramPanel",
    "SelfProgramPanelState",
    "PipelineStatus",
    "CompletionSummary",
]
