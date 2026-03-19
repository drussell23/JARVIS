"""
Interactive Brain Router — Extends BrainSelector to interactive commands.

Maps interactive tasks (voice commands, app control, vision, email) to the
optimal brain from brain_selection_policy.yaml.  Zero LLM calls, zero latency.

Usage:
    from backend.core.interactive_brain_router import get_interactive_brain_router

    router = get_interactive_brain_router()
    selection = router.select_for_task(
        task_type="step_decomposition",   # or "classification", "vision", "verification"
        command="message Zach on WhatsApp",
    )
    # selection.jprime_model   → "qwen-2.5-coder-7b" (or None if J-Prime not needed)
    # selection.claude_model   → "claude-sonnet-4-20250514" (fallback)
    # selection.vision_model   → None (not a vision task)
    # selection.brain_id       → "qwen_coder"
    # selection.complexity     → "light"

The router shares brain_selection_policy.yaml with the Ouroboros BrainSelector,
so all model names, fallback chains, and cost classes are defined in one place.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger("InteractiveBrainRouter")

_POLICY_PATH = Path(__file__).parent / "ouroboros" / "governance" / "brain_selection_policy.yaml"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class InteractiveBrainSelection:
    """Result of interactive brain routing."""

    brain_id: str              # "qwen_coder", "phi3_lightweight", "claude_fallback"
    jprime_model: Optional[str]   # model name to send to J-Prime (None if using Claude)
    claude_model: Optional[str]   # Claude model for paid fallback (None if J-Prime only)
    vision_model: Optional[str]   # vision model name (None if not a vision task)
    complexity: str            # "trivial", "light", "heavy", "complex"
    task_type: str             # original task_type passed in
    routing_reason: str        # causal code for logging
    fallback_chain: List[str]  # ordered fallback brain_ids

    def narration(self) -> str:
        """Human-readable routing for voice narrator."""
        if self.jprime_model:
            return f"Using {self.brain_id} for {self.task_type}."
        return f"Using Claude for {self.task_type}."


# ---------------------------------------------------------------------------
# Task type → complexity mapping
# ---------------------------------------------------------------------------

# Interactive task types and their default complexity.
# These are NOT code tasks — they're voice/interactive command tasks.
_INTERACTIVE_TASK_COMPLEXITY: Dict[str, str] = {
    # Trivial — no LLM needed, or tiny model sufficient
    "workspace_fastpath": "trivial",       # email/calendar detected locally
    "system_command": "trivial",           # volume, brightness, app open/close
    "reflex_match": "trivial",             # exact-match reflex manifest

    # Light — 7B model sufficient
    "classification": "light",             # J-Prime intent/domain classification
    "step_decomposition": "light",         # break goal into UI steps
    "email_triage": "light",               # score email priority
    "calendar_query": "light",             # check schedule, find free time

    # Heavy — 7B+ preferred, vision models for screenshots
    "vision_action": "heavy",              # screenshot → next action inference
    "vision_verification": "heavy",        # did the click work?
    "screen_observation": "heavy",         # "what do you see?" — single-shot describe
    "proactive_narration": "heavy",        # ambient screen monitor narration
    "email_compose": "heavy",              # draft email with context
    "browser_navigation": "heavy",         # navigate complex web pages

    # Complex — 14B+ or deep reasoning
    "multi_step_planning": "complex",      # complex multi-app workflows
    "goal_chain_step": "light",            # one step inside a GoalChain (7B sufficient)
    "email_summarization": "complex",      # summarize inbox trends
    "complex_reasoning": "complex",        # "why is X happening?"
}

# Complexity → J-Prime brain mapping (from brain_selection_policy.yaml)
_DEFAULT_BRAIN_MAP: Dict[str, str] = {
    "trivial": "phi3_lightweight",
    "light": "qwen_coder",
    "heavy": "qwen_coder",
    "complex": "qwen_coder_32b",
}

# Complexity → Claude fallback model
_CLAUDE_FALLBACK_MAP: Dict[str, str] = {
    "trivial": "claude-haiku-4-5-20251001",
    "light": "claude-sonnet-4-20250514",
    "heavy": "claude-sonnet-4-20250514",
    "complex": "claude-sonnet-4-20250514",
}

# Vision task types → vision model (all types that require a multimodal/vision model)
_VISION_TASK_TYPES = {
    "vision_action",
    "vision_verification",
    "screen_observation",    # "what do you see?" queries
    "proactive_narration",   # ambient screen monitor
}


# ---------------------------------------------------------------------------
# Keyword-based complexity escalation
# ---------------------------------------------------------------------------

_COMPLEX_KEYWORDS = re.compile(
    r"\b(analyze|summarize|compare|evaluate|investigate|explain why|root cause"
    r"|trend|pattern|insight|strategic)\b",
    re.IGNORECASE,
)

_TRIVIAL_KEYWORDS = re.compile(
    r"\b(open|close|lock|unlock|volume|brightness|screenshot|timer"
    r"|what time|weather)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# InteractiveBrainRouter
# ---------------------------------------------------------------------------

class InteractiveBrainRouter:
    """Deterministic brain selector for interactive commands.

    Reads brain definitions and fallback chains from brain_selection_policy.yaml.
    Zero LLM calls. Hot-reloads YAML on file change.
    """

    def __init__(self, policy_path: Optional[Path] = None) -> None:
        self._policy_path = policy_path or _POLICY_PATH
        self._policy: Dict = {}
        self._policy_mtime: float = 0.0
        self._brain_models: Dict[str, str] = {}     # brain_id → model_name
        self._fallback_chains: Dict[str, List[str]] = {}  # brain_id → [fallback_ids]
        self._vision_model: Optional[str] = None
        self._load_policy()

    # ── Public API ──────────────────────────────────────────────────────────

    def select_for_task(
        self,
        task_type: str,
        command: str = "",
    ) -> InteractiveBrainSelection:
        """Select the optimal brain for an interactive task.

        Args:
            task_type: One of the keys in _INTERACTIVE_TASK_COMPLEXITY.
            command: The user's command text (used for complexity escalation).

        Returns:
            InteractiveBrainSelection with model names and fallback chain.
        """
        self._maybe_reload()

        # 1. Base complexity from task type
        complexity = _INTERACTIVE_TASK_COMPLEXITY.get(task_type, "light")

        # 2. Escalate/de-escalate based on command content
        if command:
            if _COMPLEX_KEYWORDS.search(command):
                complexity = "complex"
            elif _TRIVIAL_KEYWORDS.search(command) and complexity == "light":
                complexity = "trivial"

        # 3. Select brain
        brain_id = _DEFAULT_BRAIN_MAP.get(complexity, "qwen_coder")

        # 4. Resolve model name from policy
        jprime_model = self._brain_models.get(brain_id)
        if not jprime_model:
            # Brain not in policy — fall through to qwen_coder
            brain_id = "qwen_coder"
            jprime_model = self._brain_models.get(brain_id, "qwen-2.5-coder-7b")

        # 5. Claude fallback
        claude_model = _CLAUDE_FALLBACK_MAP.get(complexity, "claude-sonnet-4-20250514")
        # Override from env if set
        env_claude = os.getenv("JARVIS_INTERACTIVE_CLAUDE_MODEL")
        if env_claude:
            claude_model = env_claude

        # 6. Vision model (only for vision tasks)
        vision_model = self._vision_model if task_type in _VISION_TASK_TYPES else None

        # 7. Fallback chain
        fallback_chain = self._fallback_chains.get(brain_id, [])

        reason = f"{task_type}→{complexity}→{brain_id}"

        return InteractiveBrainSelection(
            brain_id=brain_id,
            jprime_model=jprime_model,
            claude_model=claude_model,
            vision_model=vision_model,
            complexity=complexity,
            task_type=task_type,
            routing_reason=reason,
            fallback_chain=fallback_chain,
        )

    def get_vision_model(self) -> str:
        """Get the current vision model name (for direct vision requests)."""
        self._maybe_reload()
        return self._vision_model or "llava-v1.5-7b"

    def get_claude_model(self, complexity: str = "light") -> str:
        """Get the Claude model for a given complexity level."""
        return _CLAUDE_FALLBACK_MAP.get(complexity, "claude-sonnet-4-20250514")

    async def compare_with_remote(
        self,
        task_type: str,
        command: str,
        remote_classification: dict,
    ) -> Optional[dict]:
        """Shadow mode: compare local selection with remote J-Prime selection.

        Returns divergence dict if selections differ, None if they match.
        """
        local = self.select_for_task(task_type, command)
        remote_brain = remote_classification.get("brain_used", "")
        remote_complexity = remote_classification.get("complexity", "")

        divergences = {}
        if local.brain_id != remote_brain:
            divergences["brain_id"] = {
                "local": local.brain_id,
                "remote": remote_brain,
                "severity": "WARN",
            }
        if local.complexity != remote_complexity:
            divergences["complexity"] = {
                "local": local.complexity,
                "remote": remote_complexity,
                "severity": "WARN",
            }

        if divergences:
            logger.warning(
                "[Shadow] Divergence: task=%s command='%s' divergences=%s",
                task_type, command[:60], divergences,
            )
            return divergences
        return None

    # ── Policy loading ──────────────────────────────────────────────────────

    def _load_policy(self) -> None:
        """Load brain definitions from YAML policy."""
        try:
            if not self._policy_path.exists():
                logger.warning(
                    "[InteractiveBrainRouter] Policy not found at %s — using defaults",
                    self._policy_path,
                )
                return

            with open(self._policy_path) as f:
                self._policy = yaml.safe_load(f) or {}

            self._policy_mtime = self._policy_path.stat().st_mtime

            # Build brain_id → model_name map from policy
            brains_section = self._policy.get("brains", {})
            for brain_list in (brains_section.get("required", []),
                               brains_section.get("optional", [])):
                for brain in brain_list:
                    bid = brain.get("brain_id", "")
                    model = brain.get("model_name", "")
                    if bid and model:
                        self._brain_models[bid] = model

            # Build fallback chains from policy
            routing = self._policy.get("routing", {})
            self._fallback_chains = routing.get("fallback_chain", {})

            # Discover vision model — check if any brain has vision capabilities
            # Default to querying J-Prime's /v1/models on port 8001
            self._vision_model = os.getenv(
                "JARVIS_VISION_MODEL_NAME",
                self._discover_vision_model(),
            )

            logger.info(
                "[InteractiveBrainRouter] Loaded %d brains from policy. "
                "Vision model: %s",
                len(self._brain_models),
                self._vision_model,
            )

        except Exception as e:
            logger.warning("[InteractiveBrainRouter] Policy load failed: %s", e)

    def _discover_vision_model(self) -> str:
        """Discover the vision model name from J-Prime's vision server."""
        # Try to query /v1/models on the vision port
        import urllib.request
        import json

        host = os.getenv("JARVIS_PRIME_HOST", "136.113.252.164")
        port = os.getenv("JARVIS_PRIME_VISION_PORT", "8001")
        url = f"http://{host}:{port}/v1/models"

        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                models = data.get("data", [])
                if models:
                    model_id = models[0].get("id", "")
                    # Extract just the filename from the path
                    name = Path(model_id).stem if "/" in model_id else model_id
                    return name
        except Exception:
            pass

        return "llava-v1.5-7b"  # safe default

    def _maybe_reload(self) -> None:
        """Hot-reload policy if the YAML file changed."""
        try:
            if self._policy_path.exists():
                mtime = self._policy_path.stat().st_mtime
                if mtime > self._policy_mtime:
                    logger.info("[InteractiveBrainRouter] Policy file changed — reloading")
                    self._load_policy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_router: Optional[InteractiveBrainRouter] = None


def get_interactive_brain_router() -> InteractiveBrainRouter:
    """Get the global InteractiveBrainRouter singleton."""
    global _router
    if _router is None:
        _router = InteractiveBrainRouter()
    return _router
