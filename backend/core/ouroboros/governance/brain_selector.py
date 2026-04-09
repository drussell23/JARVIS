"""
Brain Selector — Deterministic 3-Layer Escalation Gate
=======================================================

Selects the optimal GCP j-prime brain for an operation WITHOUT any LLM call.
Zero latency.  Hot-reloadable YAML policy.

3-Layer Gate (evaluated in order):

    Layer 1 — Task Gate:     classify complexity from description + target_files
    Layer 2 — Resource Gate: local M1 pressure → offload to GCP
    Layer 3 — Cost Gate:     daily budget enforcement with file-backed persistence

Returns a ``BrainSelectionResult`` that carries:
  • ``brain_id``       — which brain tier was selected
  • ``model_name``     — exact model name to pass to j-prime (or fallback)
  • ``routing_reason`` — causal code recorded in the ledger
  • ``task_complexity`` — classified tier

The BrainSelector is owned by GovernedLoopService and called once per
``submit()`` invocation, before the orchestrator is dispatched.

Cost recording:
    After generation, GLS calls ``brain_selector.record_cost(provider, usd)``
    so the cost gate has accurate daily totals.  State persists across restarts.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, Optional, Tuple

from backend.core.ouroboros.governance.resource_monitor import (
    ResourceSnapshot,  # retained in signatures for caller compatibility
)

logger = logging.getLogger("Ouroboros.BrainSelector")

_POLICY_PATH = Path(__file__).parent / "brain_selection_policy.yaml"

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class TaskComplexity(Enum):
    """Coarse complexity tiers used to select brain."""

    TRIVIAL = "trivial"        # single-file, trivial pattern: append/comment
    LIGHT = "light"            # single-file fix, bug fix, docs
    HEAVY_CODE = "heavy_code"  # refactor / implement / multi-file
    COMPLEX = "complex"        # architecture / cross-repo / deep reasoning


@dataclass(frozen=True)
class BrainSelectionResult:
    """Immutable output of the 3-layer gate."""

    brain_id: str           # "phi3_lightweight" | "qwen_coder" | "qwen_coder_14b" | "qwen_coder_32b" | "deepseek_r1"
    model_name: str         # exact name passed to j-prime
    fallback_model: str     # j-prime uses this if primary model not loaded
    routing_reason: str     # causal code for ledger + narration
    task_complexity: str    # TaskComplexity.value
    estimated_prompt_tokens: int = 0
    provider_tier: str = "gcp_prime"  # "gcp_prime" | "claude_api" | "queued"
    schema_capability: str = "full_content_only"  # "full_content_only" | "full_content_and_diff"

    def narration(self) -> str:
        """Human-readable routing announcement for VoiceNarrator."""
        if self.provider_tier == "queued":
            return (
                "Task queued. Daily budget reached — "
                "resuming when budget resets at midnight."
            )
        model_display = self.model_name.replace("-", " ").title()
        reason_display = self.routing_reason.replace("_", " ")
        return (
            f"Routing {self.task_complexity} task to {model_display} on G-C-P. "
            f"Reason: {reason_display}."
        )


@dataclass(frozen=True)
class BrainSelection:
    """Pure intent/complexity routing result — no resource fields.

    BrainSelector is a pure intent+complexity classifier.  Resource gating
    is the responsibility of RouteDecisionService (which receives pre-fetched
    ResourceState from TelemetryContextualizer).
    """

    brain_id: str        # e.g. "qwen_coder_32b", "phi3_lightweight"
    model_alias: str     # e.g. "qwen-2.5-coder-32b"
    reason_code: str     # causal code for ledger
    complexity: str      # TaskComplexity.value
    intent_type: str     # CAI intent string e.g. "code_generation"

    def narration(self) -> str:
        """Human-readable routing announcement for VoiceNarrator."""
        model_display = self.model_alias.replace("-", " ").title()
        reason_display = self.reason_code.replace("_", " ")
        return (
            f"Routing {self.complexity} task to {model_display} on G-C-P. "
            f"Reason: {reason_display}."
        )


# ---------------------------------------------------------------------------
# BrainSelector
# ---------------------------------------------------------------------------


class BrainSelector:
    """
    Deterministic brain selector.  No LLM calls.

    Policy is loaded from ``brain_selection_policy.yaml`` on first use and
    hot-reloaded whenever the file mtime changes.

    Cost state is persisted to a JSON file so the daily budget survives
    process restarts.
    """

    def __init__(
        self,
        policy_path: Optional[Path] = None,
        persist_path: Optional[Path] = None,
    ) -> None:
        self._policy_path = policy_path or _POLICY_PATH
        self._policy: Dict = {}
        self._policy_mtime: float = 0.0

        _env_persist = os.environ.get(
            "OUROBOROS_COST_STATE_PATH",
            "~/.jarvis/ouroboros/cost_state.json",
        )
        self._persist_path = persist_path or Path(os.path.expanduser(_env_persist))

        self._daily_spend_gcp: float = 0.0
        self._daily_spend_claude: float = 0.0
        self._cost_date: str = ""

        self._load_policy()
        self._load_cost_state()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def select(
        self,
        description: str,
        target_files: Tuple[str, ...],
        snapshot: ResourceSnapshot,
        blast_radius: int = 1,
    ) -> BrainSelectionResult:
        """Run the 3-layer gate and return a deterministic BrainSelectionResult.

        Parameters
        ----------
        description:
            Natural-language task description from OperationContext.
        target_files:
            Tuple of relative file paths targeted by the operation.
        snapshot:
            Current ResourceSnapshot from ResourceMonitor.
        blast_radius:
            Number of files affected (risk engine estimate; 1 = single file).
        """
        self._maybe_reload_policy()
        self._maybe_reset_daily_spend()

        gate_cfg: Dict = self._policy.get("gates", {})
        brains: Dict = self._resolve_brains_dict()

        # ── Layer 1: Task Gate ────────────────────────────────────────────────
        complexity, est_tokens = self._classify_task(
            description, target_files, blast_radius, gate_cfg
        )

        if complexity == TaskComplexity.TRIVIAL:
            return self._result_for(
                "phi3_lightweight", brains, "task_gate_trivial", complexity, est_tokens
            )

        # ── Layer 2: Resource Gate — REMOVED (Phase 1 P0) ────────────────────
        # Host-binding invariant: telemetry_host == selector_host == execution_host.
        # Local host memory pressure MUST NOT influence GCP routing decisions.
        # Resource-pressure routing is now handled by TelemetryContextualizer,
        # which fetches telemetry from the remote execution host only.
        # snapshot is retained in the signature for caller compatibility but
        # is no longer used for routing decisions here.

        # ── Layer 3: Cost Gate ────────────────────────────────────────────────
        cost_cfg = gate_cfg.get("cost_gate", {})
        daily_budget = float(
            os.environ.get("OUROBOROS_GCP_DAILY_BUDGET", None)
            or cost_cfg.get("daily_budget_usd", 0.50)
        )
        total_spend = self._daily_spend_gcp + self._daily_spend_claude

        if total_spend >= daily_budget:
            exceeded_action = cost_cfg.get("budget_exceeded_action", "queue")
            if complexity in (TaskComplexity.HEAVY_CODE, TaskComplexity.COMPLEX):
                if exceeded_action == "queue":
                    logger.warning(
                        "[BrainSelector] Cost gate: %.4f >= %.4f — queuing heavy task",
                        total_spend, daily_budget,
                    )
                    return BrainSelectionResult(
                        brain_id="queued",
                        model_name="queued",
                        fallback_model="queued",
                        routing_reason="cost_gate_triggered_queue",
                        task_complexity=complexity.value,
                        estimated_prompt_tokens=est_tokens,
                        provider_tier="queued",
                    )
            # Light tasks: fall back to cheapest brain (phi3/mistral)
            logger.info(
                "[BrainSelector] Cost gate: %.4f >= %.4f — downgrade to phi3",
                total_spend, daily_budget,
            )
            return self._result_for(
                "phi3_lightweight", brains,
                "cost_gate_triggered_fallback_to_phi3",
                complexity, est_tokens,
            )

        # ── Default: route by task complexity ─────────────────────────────────
        brain_id = self._brain_for_complexity(complexity)
        reason = f"complexity_match_{complexity.value}"
        return self._result_for(brain_id, brains, reason, complexity, est_tokens)

    def _apply_resource_and_cost_gates(
        self,
        brain_id: str,
        complexity: TaskComplexity,
        description: str,
        target_files: Tuple[str, ...],
        snapshot: ResourceSnapshot,
        blast_radius: int,
        routing_reason: str,
    ) -> BrainSelectionResult:
        """Apply Layers 2+3 (resource + cost gates) given a pre-classified complexity/brain.

        Called by RouteDecisionService after CAI has already determined the
        (complexity, brain_id) in Layer 1, bypassing the regex Task Gate.
        """
        self._maybe_reload_policy()
        self._maybe_reset_daily_spend()

        gate_cfg = self._policy.get("gates", {})
        brains = self._policy.get("brains", {})
        # Rough token estimate
        n_files = max(blast_radius, len(target_files))
        est_tokens = max(100, len(description) // 4 + n_files * 200)

        # ── Layer 2: Resource Gate — REMOVED (Phase 1 P0) ────────────────────
        # Same invariant as select(): local resource pressure must not influence
        # routing decisions for remote execution.  snapshot is accepted for
        # caller compatibility but ignored here.

        # ── Layer 3: Cost Gate ────────────────────────────────────────────────
        cost_cfg = gate_cfg.get("cost_gate", {})
        daily_budget = float(
            os.environ.get("OUROBOROS_GCP_DAILY_BUDGET", None)
            or cost_cfg.get("daily_budget_usd", 0.50)
        )
        total_spend = self._daily_spend_gcp + self._daily_spend_claude

        if total_spend >= daily_budget:
            exceeded_action = cost_cfg.get("budget_exceeded_action", "queue")
            if complexity in (TaskComplexity.HEAVY_CODE, TaskComplexity.COMPLEX):
                if exceeded_action == "queue":
                    logger.warning(
                        "[BrainSelector] Cost gate: %.4f >= %.4f — queuing heavy task (via RouteDecisionService)",
                        total_spend, daily_budget,
                    )
                    return BrainSelectionResult(
                        brain_id="queued",
                        model_name="queued",
                        fallback_model="queued",
                        routing_reason="cost_gate_triggered_queue",
                        task_complexity=complexity.value,
                        estimated_prompt_tokens=est_tokens,
                        provider_tier="queued",
                    )
            logger.info(
                "[BrainSelector] Cost gate: %.4f >= %.4f — downgrade to phi3 (via RouteDecisionService)",
                total_spend, daily_budget,
            )
            return self._result_for(
                "phi3_lightweight", brains,
                "cost_gate_triggered_fallback_to_phi3",
                complexity, est_tokens,
            )

        return self._result_for(brain_id, brains, routing_reason, complexity, est_tokens)

    def record_cost(self, provider: str, cost_usd: float) -> None:
        """Record actual generation cost.  Persists to disk atomically."""
        if cost_usd <= 0.0:
            return
        self._maybe_reset_daily_spend()
        if "claude" in provider.lower():
            self._daily_spend_claude += cost_usd
        else:
            self._daily_spend_gcp += cost_usd
        logger.debug(
            "[BrainSelector] Cost recorded: provider=%s cost=%.4f "
            "total_gcp=%.4f total_claude=%.4f",
            provider, cost_usd, self._daily_spend_gcp, self._daily_spend_claude,
        )
        self._save_cost_state()

    @property
    def daily_spend(self) -> float:
        """Total daily spend across all providers."""
        self._maybe_reset_daily_spend()
        return round(self._daily_spend_gcp + self._daily_spend_claude, 6)

    @property
    def daily_spend_breakdown(self) -> Dict[str, float]:
        self._maybe_reset_daily_spend()
        return {
            "gcp_usd": round(self._daily_spend_gcp, 6),
            "claude_usd": round(self._daily_spend_claude, 6),
            "total_usd": self.daily_spend,
        }

    # -------------------------------------------------------------------------
    # Internal — Brain Dict Resolution
    # -------------------------------------------------------------------------

    def _resolve_brains_dict(self) -> Dict:
        """Resolve the flat brain_id→config dict from the policy.

        The YAML policy stores brains in two formats:
          1. ``routing.legacy_brains`` — flat dict keyed by brain_id (preferred)
          2. ``brains`` — flat dict (used by _DEFAULT_POLICY)
        The structured ``brains.required`` / ``brains.optional`` lists are NOT
        used here; they are consumed by the admission gate in the supervisor.
        """
        # Prefer routing.legacy_brains (YAML format)
        legacy = self._policy.get("routing", {}).get("legacy_brains", {})
        if legacy and isinstance(legacy, dict):
            return legacy
        # Fallback: flat brains dict (default policy format)
        brains = self._policy.get("brains", {})
        if isinstance(brains, dict) and not any(
            k in brains for k in ("required", "optional")
        ):
            return brains
        return {}

    # -------------------------------------------------------------------------
    # Internal — Task Classification
    # -------------------------------------------------------------------------

    def _classify_task(
        self,
        description: str,
        target_files: Tuple[str, ...],
        blast_radius: int,
        gate_cfg: Dict,
    ) -> Tuple[TaskComplexity, int]:
        """Layer 1 classifier.  Pure text analysis — no LLM."""
        desc_lower = description.lower()
        tg_cfg = gate_cfg.get("task_gate", {})
        trivial_patterns = tg_cfg.get("trivial_patterns", [])
        heavy_patterns = tg_cfg.get("heavy_patterns", [])
        trivial_max_files = int(tg_cfg.get("trivial_max_files", 1))
        heavy_min_files = int(tg_cfg.get("heavy_min_files", 3))

        n_files = max(blast_radius, len(target_files))
        # Rough prompt token estimate: description chars / 4 + 200 tokens per file
        est_tokens = max(100, len(description) // 4 + n_files * 200)

        # TRIVIAL: matches trivial pattern AND single file
        if n_files <= trivial_max_files and any(
            re.search(p, desc_lower) for p in trivial_patterns
        ):
            return TaskComplexity.TRIVIAL, est_tokens

        # COMPLEX: cross-repo indicators OR large blast radius
        _complex_keywords = {
            "architecture", "cross-repo", "cross repo", "redesign",
            "migrate all", "reason about", "analyze codebase",
            "system design", "root cause",
        }
        if n_files > 5 or any(kw in desc_lower for kw in _complex_keywords):
            return TaskComplexity.COMPLEX, est_tokens

        # HEAVY_CODE: matches heavy pattern OR multi-file threshold
        if n_files >= heavy_min_files or any(
            re.search(p, desc_lower) for p in heavy_patterns
        ):
            return TaskComplexity.HEAVY_CODE, est_tokens

        # DEFAULT: light
        return TaskComplexity.LIGHT, est_tokens

    # -------------------------------------------------------------------------
    # Internal — Brain Mapping
    # -------------------------------------------------------------------------

    def _brain_for_complexity(self, complexity: TaskComplexity) -> str:
        return {
            TaskComplexity.TRIVIAL: "phi3_lightweight",
            TaskComplexity.LIGHT: "qwen_coder",
            TaskComplexity.HEAVY_CODE: "qwen_coder_32b",
            TaskComplexity.COMPLEX: "qwen_coder_32b",
        }.get(complexity, "qwen_coder")

    def _result_for(
        self,
        brain_id: str,
        brains: Dict,
        routing_reason: str,
        complexity: TaskComplexity,
        est_tokens: int,
    ) -> BrainSelectionResult:
        cfg = brains.get(brain_id, {})
        model_name = cfg.get("model_name", "qwen-2.5-coder-7b")
        fallback = cfg.get("fallback_model", "qwen-2.5-coder-7b")
        schema_cap = cfg.get("schema_capability", "full_content_only")
        logger.info(
            "[BrainSelector] Selected: brain=%s model=%s reason=%s complexity=%s schema=%s",
            brain_id, model_name, routing_reason, complexity.value, schema_cap,
        )
        return BrainSelectionResult(
            brain_id=brain_id,
            model_name=model_name,
            fallback_model=fallback,
            routing_reason=routing_reason,
            task_complexity=complexity.value,
            estimated_prompt_tokens=est_tokens,
            provider_tier="gcp_prime",
            schema_capability=schema_cap,
        )

    # -------------------------------------------------------------------------
    # Internal — Policy Hot-Reload
    # -------------------------------------------------------------------------

    def _load_policy(self) -> None:
        try:
            import yaml  # PyYAML (already in most envs; gracefully degrade without)
            with open(self._policy_path, encoding="utf-8") as fh:
                self._policy = yaml.safe_load(fh) or {}
            self._policy_mtime = self._policy_path.stat().st_mtime
            logger.info(
                "[BrainSelector] Policy v%s loaded from %s",
                self._policy.get("version", "?"), self._policy_path.name,
            )
        except ImportError:
            logger.warning(
                "[BrainSelector] PyYAML not available — using built-in defaults"
            )
            self._policy = _DEFAULT_POLICY
        except Exception as exc:
            logger.warning("[BrainSelector] Policy load failed (%s) — using defaults", exc)
            self._policy = _DEFAULT_POLICY

    def _maybe_reload_policy(self) -> None:
        try:
            mtime = self._policy_path.stat().st_mtime
            if mtime != self._policy_mtime:
                self._load_policy()
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Internal — Cost Persistence
    # -------------------------------------------------------------------------

    def _load_cost_state(self) -> None:
        today = time.strftime("%Y-%m-%d")
        self._cost_date = today
        try:
            if self._persist_path.exists():
                data = json.loads(self._persist_path.read_text())
                if data.get("date") == today:
                    self._daily_spend_gcp = float(data.get("gcp_usd", 0.0))
                    self._daily_spend_claude = float(data.get("claude_usd", 0.0))
                    logger.debug(
                        "[BrainSelector] Cost state loaded: gcp=%.4f claude=%.4f",
                        self._daily_spend_gcp, self._daily_spend_claude,
                    )
        except Exception as exc:
            logger.debug("[BrainSelector] Cost state load failed: %s", exc)

    def _save_cost_state(self) -> None:
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "date": self._cost_date,
                        "gcp_usd": round(self._daily_spend_gcp, 6),
                        "claude_usd": round(self._daily_spend_claude, 6),
                    },
                    indent=2,
                )
            )
            tmp.replace(self._persist_path)  # atomic
        except Exception as exc:
            logger.warning("[BrainSelector] Cost state save failed: %s", exc)

    def _maybe_reset_daily_spend(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._cost_date:
            logger.info("[BrainSelector] New day — resetting daily spend")
            self._daily_spend_gcp = 0.0
            self._daily_spend_claude = 0.0
            self._cost_date = today
            self._save_cost_state()


# ---------------------------------------------------------------------------
# Built-in default policy (used when YAML is missing or PyYAML unavailable)
# ---------------------------------------------------------------------------

_DEFAULT_POLICY: Dict = {
    "version": "default-v2",
    "brains": {
        "phi3_lightweight": {
            "model_name": "llama-3.2-1b", "fallback_model": "qwen-2.5-coder-7b",
            "schema_capability": "full_content_only",
        },
        "mistral_planning": {
            "model_name": "mistral-7b", "fallback_model": "qwen-2.5-coder-7b",
            "schema_capability": "full_content_only",
        },
        "qwen_coder": {
            "model_name": "qwen-2.5-coder-7b", "fallback_model": "qwen-2.5-coder-14b",
            "schema_capability": "full_content_only",
        },
        "qwen_coder_14b": {
            "model_name": "qwen-2.5-coder-14b", "fallback_model": "qwen-2.5-coder-7b",
            "schema_capability": "full_content_only",
        },
        "qwen_coder_32b": {
            "model_name": "qwen-2.5-coder-32b", "fallback_model": "qwen-2.5-coder-14b",
            "schema_capability": "full_content_and_diff",
        },
        "deepseek_r1": {
            "model_name": "deepseek-r1-qwen-7b", "fallback_model": "qwen-2.5-coder-32b",
            "schema_capability": "full_content_only",
        },
    },
    "gates": {
        "task_gate": {
            "trivial_patterns": [
                "append.*line", "add.*comment", "monitored by", "single.*line",
                "fix.*todo", "add.*docstring", "bump.*version", "fix.*typo",
                "update.*import", "remove.*unused", "rename.*variable",
                "add.*type.*hint", "fix.*lint", "fix.*format",
            ],
            "heavy_patterns": ["refactor", "implement", "redesign", "migrate", "optimize.*performance"],
            "trivial_max_files": 1,
            "heavy_min_files": 3,
        },
        "resource_gate": {"local_pressure_redirect_threshold": "ELEVATED"},
        "cost_gate": {"daily_budget_usd": 0.50, "budget_exceeded_action": "queue"},
    },
}
