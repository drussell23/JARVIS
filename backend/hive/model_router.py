"""
Hive Model Router

Cognitive-state-aware model selection for the Autonomous Engineering Hive.
Maps each CognitiveState to a verified Doubleword model ID, with all
identifiers configurable via environment variables (zero hardcoding).
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from backend.hive.thread_models import CognitiveState


class HiveModelRouter:
    """Routes model selection based on the current cognitive state.

    Model IDs and parameters are read from environment variables at
    construction time, allowing runtime reconfiguration without code
    changes.

    Environment variables
    ---------------------
    JARVIS_HIVE_REM_MODEL : str
        Model used during REM (light reasoning) state.
    JARVIS_HIVE_FLOW_MODEL : str
        Model used during FLOW (deep reasoning) state.
    JARVIS_HIVE_EMBEDDING_MODEL : str
        Model used for embedding generation.
    """

    _DEFAULT_REM_MODEL = "Qwen/Qwen3.5-35B-A3B-FP8"
    _DEFAULT_FLOW_MODEL = "Qwen/Qwen3.5-397B-A17B-FP8"
    _DEFAULT_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-8B"

    # Per-state generation parameters (max_tokens, temperature).
    # BASELINE intentionally zeroed — no model invocation needed.
    _STATE_PARAMS: Dict[CognitiveState, Dict[str, Any]] = {
        CognitiveState.BASELINE: {"max_tokens": 0, "temperature": 0},
        CognitiveState.REM: {"max_tokens": 4000, "temperature": 0.3},
        CognitiveState.FLOW: {"max_tokens": 10000, "temperature": 0.2},
    }

    def __init__(self) -> None:
        self._rem_model: str = os.environ.get(
            "JARVIS_HIVE_REM_MODEL", self._DEFAULT_REM_MODEL
        )
        self._flow_model: str = os.environ.get(
            "JARVIS_HIVE_FLOW_MODEL", self._DEFAULT_FLOW_MODEL
        )
        self._embedding_model: str = os.environ.get(
            "JARVIS_HIVE_EMBEDDING_MODEL", self._DEFAULT_EMBEDDING_MODEL
        )

        # Runtime lookup — avoids if/elif chains per the symbiotic manifesto.
        self._model_map: Dict[CognitiveState, Optional[str]] = {
            CognitiveState.BASELINE: None,
            CognitiveState.REM: self._rem_model,
            CognitiveState.FLOW: self._flow_model,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def embedding_model(self) -> str:
        """Return the configured embedding model ID."""
        return self._embedding_model

    def get_model(self, state: CognitiveState) -> Optional[str]:
        """Return the model ID for *state*, or ``None`` for BASELINE."""
        return self._model_map.get(state)

    def get_config(self, state: CognitiveState) -> Dict[str, Any]:
        """Return a full configuration dict for *state*.

        Keys: ``model``, ``max_tokens``, ``temperature``.
        """
        params = self._STATE_PARAMS.get(state, {})
        return {
            "model": self.get_model(state),
            "max_tokens": params.get("max_tokens", 0),
            "temperature": params.get("temperature", 0),
        }
