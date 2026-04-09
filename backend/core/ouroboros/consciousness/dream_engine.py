"""backend/core/ouroboros/consciousness/dream_engine.py

DreamEngine — idle GPU speculative analysis for the Trinity Consciousness layer.

Design:
    - Runs a background asyncio loop that waits for *all five* readiness gates
      before sending speculative analysis prompts to J-Prime.
    - Uses aiohttp directly (NOT PrimeRouter/PrimeClient) so that
      ``record_jprime_activity()`` is never called and the VM idle timer
      continues counting down.  This is critical: if the VM shuts down
      while we are dreaming, that is correct behaviour — the user's idle
      timer is what matters, not our speculative traffic.  (TC29)
    - Between every HTTP call the preemption flag is checked; if set the
      job is abandoned immediately and partial state is saved for
      resume.  (TC17, TC30)
    - After preemption, re-entry into dream mode is blocked for
      ``config.dream_reentry_cooldown_s`` seconds (flap damping — TC18).
    - Job idempotency is enforced via ``compute_job_key()``: a completed
      key whose blueprint is still fresh is never recomputed.  (TC12)
    - Prompts are hard-capped at ``DREAM_MAX_PROMPT_CHARS`` characters.  (TC23)
    - When J-Prime is unavailable, DREAM_DORMANT is emitted via CommProtocol
      and no local heuristic fallback is attempted.  (TC24)
    - All state (blueprints, completed keys) is persisted to JSON on disk
      and restored on start.  (TC30)

Thread-safety:
    All mutable state is only touched inside the single asyncio event loop.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.core.ouroboros.consciousness.dream_metrics import DreamMetricsTracker
from backend.core.ouroboros.consciousness.types import (
    ConsciousnessConfig,
    ImprovementBlueprint,
    UserActivityMonitor,
    compute_blueprint_id,
    compute_job_key,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DREAM_MAX_PROMPT_CHARS: int = 2048
"""Hard cap on dream prompt text length (TC23)."""

_DREAM_LOOP_INTERVAL_S: float = 30.0
"""Seconds between dream-loop ticks when not actively computing."""

_JPRIME_GENERATE_ENDPOINT: str = "/v1/generate"
"""J-Prime HTTP endpoint for code generation."""

_JPRIME_TIMEOUT_S: float = 120.0
"""Per-request timeout for J-Prime HTTP calls."""


# ---------------------------------------------------------------------------
# Persistence helpers (pure functions)
# ---------------------------------------------------------------------------


def _blueprint_to_dict(bp: ImprovementBlueprint) -> Dict[str, Any]:
    """Serialize an ImprovementBlueprint to a JSON-safe dict."""
    return {
        "blueprint_id": bp.blueprint_id,
        "title": bp.title,
        "description": bp.description,
        "category": bp.category,
        "priority_score": bp.priority_score,
        "target_files": list(bp.target_files),
        "estimated_effort": bp.estimated_effort,
        "estimated_cost_usd": bp.estimated_cost_usd,
        "repo": bp.repo,
        "repo_sha": bp.repo_sha,
        "computed_at_utc": bp.computed_at_utc,
        "ttl_hours": bp.ttl_hours,
        "model_used": bp.model_used,
        "policy_hash": bp.policy_hash,
        "oracle_neighborhood": bp.oracle_neighborhood,
        "suggested_approach": bp.suggested_approach,
        "risk_assessment": bp.risk_assessment,
    }


def _blueprint_from_dict(d: Dict[str, Any]) -> ImprovementBlueprint:
    """Deserialize a dict back to an ImprovementBlueprint."""
    return ImprovementBlueprint(
        blueprint_id=d["blueprint_id"],
        title=d["title"],
        description=d["description"],
        category=d["category"],
        priority_score=float(d["priority_score"]),
        target_files=tuple(d.get("target_files", ())),
        estimated_effort=d["estimated_effort"],
        estimated_cost_usd=float(d["estimated_cost_usd"]),
        repo=d["repo"],
        repo_sha=d["repo_sha"],
        computed_at_utc=d["computed_at_utc"],
        ttl_hours=float(d["ttl_hours"]),
        model_used=d["model_used"],
        policy_hash=d["policy_hash"],
        oracle_neighborhood=d.get("oracle_neighborhood", {}),
        suggested_approach=d.get("suggested_approach", ""),
        risk_assessment=d.get("risk_assessment", ""),
    )


# ---------------------------------------------------------------------------
# DreamEngine
# ---------------------------------------------------------------------------


class DreamEngine:
    """Idle GPU speculative analysis engine for Trinity Consciousness.

    Monitors five readiness gates and, when all pass, sends speculative
    code-improvement prompts to J-Prime via direct HTTP.  Results are
    stored as :class:`ImprovementBlueprint` objects on disk for later
    consumption by the governance pipeline.

    Parameters
    ----------
    health_cortex:
        HealthCortex instance with ``get_snapshot() -> TrinityHealthSnapshot``.
    memory_engine:
        MemoryEngine instance with ``get_file_reputation(path) -> FileReputation``.
    activity_monitor:
        Any object implementing ``last_activity_s() -> float``.
    resource_governor:
        ResourceGovernor with ``async should_yield() -> bool``.
    metrics_tracker:
        DreamMetricsTracker for recording compute time, preemptions, etc.
    config:
        ConsciousnessConfig with dream_* parameters.
    jprime_url:
        Base URL for J-Prime HTTP API (e.g. ``http://136.113.252.164:8000``).
        Used directly via aiohttp — NOT PrimeRouter/PrimeClient (TC29).
    persistence_dir:
        Directory for storing blueprints and job keys on disk.
    comm:
        Optional CommProtocol instance for emitting DREAM_DORMANT (TC24).
    """

    def __init__(
        self,
        health_cortex: Any,
        memory_engine: Any,
        activity_monitor: UserActivityMonitor,
        resource_governor: Any,
        metrics_tracker: DreamMetricsTracker,
        config: ConsciousnessConfig,
        jprime_url: str = "",
        persistence_dir: Optional[Path] = None,
        comm: Any = None,
        dw_provider: Any = None,
        claude_provider: Any = None,
    ) -> None:
        self._health_cortex = health_cortex
        self._memory_engine = memory_engine
        self._activity_monitor = activity_monitor
        self._resource_governor = resource_governor
        self._metrics_tracker = metrics_tracker
        self._config = config
        self._jprime_url: str = jprime_url
        self._comm = comm

        # DW + Claude providers (preferred over raw J-Prime HTTP)
        self._dw_provider = dw_provider
        self._claude_provider = claude_provider

        # Persistence
        default_dir = (
            Path.home()
            / ".jarvis"
            / "ouroboros"
            / "consciousness"
            / "dreams"
        )
        self._persistence_dir: Path = persistence_dir or default_dir
        self._persistence_dir.mkdir(parents=True, exist_ok=True)

        # State
        self._blueprints: Dict[str, ImprovementBlueprint] = {}
        self._completed_keys: Set[str] = set()
        self._interrupted_jobs: Dict[str, Dict[str, Any]] = {}

        # Preemption (TC17)
        self._preempted: asyncio.Event = asyncio.Event()

        # Flap damping (TC18) — monotonic time of last user return
        self._last_user_return: float = 0.0

        # Current repo state — updated by callers or internal polling
        self._current_head: str = ""
        self._current_policy_hash: str = ""

        # Dream loop task
        self._loop_task: Optional[asyncio.Task[None]] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load persisted state from disk and start the background dream loop."""
        self._load_state()
        self._loop_task = asyncio.create_task(
            self._dream_loop(), name="dream_engine_loop",
        )
        logger.info(
            "[DreamEngine] Started (idle_threshold=%.0fs, max_min/day=%.0f, "
            "blueprints=%d, keys=%d)",
            self._config.dream_idle_threshold_s,
            self._config.dream_max_minutes_per_day,
            len(self._blueprints),
            len(self._completed_keys),
        )

    async def stop(self) -> None:
        """Cancel the dream loop, set preemption, and persist state to disk."""
        self._preempted.set()
        if self._loop_task is not None and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
        self._loop_task = None
        self._persist_state()
        logger.info("[DreamEngine] Stopped, state persisted.")

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_blueprints(self, top_n: int = 5) -> List[ImprovementBlueprint]:
        """Return up to *top_n* non-stale blueprints sorted by priority desc."""
        fresh: List[ImprovementBlueprint] = []
        for bp in self._blueprints.values():
            if not bp.is_stale(self._current_head, self._current_policy_hash):
                fresh.append(bp)
        fresh.sort(key=lambda b: b.priority_score, reverse=True)
        return fresh[:top_n]

    def get_blueprint(self, blueprint_id: str) -> Optional[ImprovementBlueprint]:
        """Return a specific blueprint by ID, or None if not found."""
        return self._blueprints.get(blueprint_id)

    def discard_stale(self) -> int:
        """Remove stale blueprints from the store.  Return count removed."""
        stale_keys: List[str] = []
        for key, bp in self._blueprints.items():
            if bp.is_stale(self._current_head, self._current_policy_hash):
                stale_keys.append(key)
        for key in stale_keys:
            del self._blueprints[key]
            self._completed_keys.discard(key)
            self._metrics_tracker.record_blueprint_discarded()
        if stale_keys:
            logger.info(
                "[DreamEngine] Discarded %d stale blueprints", len(stale_keys),
            )
        return len(stale_keys)

    # ------------------------------------------------------------------
    # Readiness gates
    # ------------------------------------------------------------------

    async def _can_dream(self) -> Tuple[bool, str]:
        """Check all five readiness gates.  Returns (can_dream, reason).

        Gates (checked in order):
            1. J-Prime healthy + model loaded  (TC09)
            2. User idle >= threshold           (TC10)
            3. VM warm from user traffic        (TC11)
            4. ResourceGovernor not yielding
            5. Daily dream-minutes budget
            + Flap damping cooldown             (TC18)
        """
        # Gate 0: Flap damping (TC18)
        if self._last_user_return > 0.0:
            elapsed = time.monotonic() - self._last_user_return
            if elapsed < self._config.dream_reentry_cooldown_s:
                remaining = self._config.dream_reentry_cooldown_s - elapsed
                return False, (
                    f"Flap damping cooldown: {remaining:.0f}s remaining "
                    f"(threshold {self._config.dream_reentry_cooldown_s:.0f}s)"
                )

        # Gate 1: Inference backend available
        # Original gate required J-Prime healthy + model loaded (TC09).
        # With DW 35B and Claude as inference backends, J-Prime is no longer
        # required — skip the health check if an alternative is available.
        _has_alt_backend = (
            self._dw_provider is not None or self._claude_provider is not None
        )
        if not _has_alt_backend:
            # Legacy path: require J-Prime health
            snapshot = self._health_cortex.get_snapshot()
            if snapshot is None:
                return False, "No health snapshot and no alternative inference backend"
            prime = snapshot.prime
            if prime.status != "healthy":
                return False, f"Prime not healthy: status={prime.status}"
            if not prime.details.get("model_loaded"):
                return False, "Prime model not loaded"

        # Gate 2: User idle (TC10)
        idle_s = self._activity_monitor.last_activity_s()
        if idle_s < self._config.dream_idle_threshold_s:
            return False, (
                f"User active: idle {idle_s:.0f}s < "
                f"threshold {self._config.dream_idle_threshold_s:.0f}s"
            )

        # Gate 3: VM warm from user traffic (TC11)
        # When using DW/Claude (cloud-based), VM warmth is irrelevant —
        # inference doesn't run on the local VM.
        if not _has_alt_backend:
            snapshot = self._health_cortex.get_snapshot()
            prime = snapshot.prime if snapshot else None
            uptime_s = prime.details.get("uptime_s", 0) if prime else 0
            if uptime_s < self._config.dream_idle_threshold_s:
                return False, (
                    f"VM uptime too short: {uptime_s:.0f}s < "
                    f"threshold {self._config.dream_idle_threshold_s:.0f}s "
                    "(VM may have been woken for dream, not by user warm traffic)"
                )

        # Gate 4: Resource governor
        should_yield = await self._resource_governor.should_yield()
        if should_yield:
            return False, "ResourceGovernor says yield — system under pressure"

        # Gate 5: Daily budget (TC23 budget)
        metrics = self._metrics_tracker.get_metrics()
        if metrics.opportunistic_compute_minutes >= self._config.dream_max_minutes_per_day:
            return False, (
                f"Dream minutes budget exhausted: "
                f"{metrics.opportunistic_compute_minutes:.1f} >= "
                f"{self._config.dream_max_minutes_per_day:.1f}"
            )

        return True, "all_gates_passed"

    # ------------------------------------------------------------------
    # Preemption
    # ------------------------------------------------------------------

    def _check_preempted(self) -> bool:
        """Return True if the preemption event has been set (TC17)."""
        return self._preempted.is_set()

    def _save_interrupted(self, job_key: str, candidate_info: Dict[str, Any]) -> None:
        """Save interrupted job info for potential resume (TC30)."""
        self._interrupted_jobs[job_key] = candidate_info
        logger.debug(
            "[DreamEngine] Saved interrupted job %s for resume", job_key[:16],
        )

    # ------------------------------------------------------------------
    # Idempotency
    # ------------------------------------------------------------------

    def _is_job_completed(
        self,
        job_key: str,
        current_head: str,
        current_policy_hash: str,
    ) -> bool:
        """Return True if the job key has been completed and its blueprint is fresh."""
        if job_key not in self._completed_keys:
            return False
        bp = self._blueprints.get(job_key)
        if bp is None:
            # Key exists but blueprint was removed — not completed
            self._completed_keys.discard(job_key)
            return False
        if bp.is_stale(current_head, current_policy_hash):
            # Blueprint is stale — needs recomputation
            self._completed_keys.discard(job_key)
            return False
        return True

    # ------------------------------------------------------------------
    # Token budget
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_prompt(text: str) -> str:
        """Truncate prompt text to DREAM_MAX_PROMPT_CHARS (TC23)."""
        if len(text) <= DREAM_MAX_PROMPT_CHARS:
            return text
        return text[:DREAM_MAX_PROMPT_CHARS]

    # ------------------------------------------------------------------
    # CommProtocol emission
    # ------------------------------------------------------------------

    async def _emit_dormant(self, reason: str) -> None:
        """Emit DREAM_DORMANT via CommProtocol (TC24).

        When J-Prime is unavailable, we emit this reason code and do NOT
        substitute local heuristics.
        """
        if self._comm is None:
            logger.debug(
                "[DreamEngine] DREAM_DORMANT (%s) — no comm, skipping emit",
                reason,
            )
            return
        try:
            await self._comm.emit_heartbeat(
                op_id="dream_engine",
                phase=f"DREAM_DORMANT:{reason}",
                progress_pct=0.0,
            )
            logger.info("[DreamEngine] Emitted DREAM_DORMANT: %s", reason)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[DreamEngine] Failed to emit DREAM_DORMANT", exc_info=True,
            )

    # ------------------------------------------------------------------
    # Dream loop
    # ------------------------------------------------------------------

    async def _dream_loop(self) -> None:
        """Background loop: check gates, pick candidate, compute blueprint."""
        while True:
            try:
                # Reset preemption at start of each cycle
                self._preempted.clear()

                can, reason = await self._can_dream()
                if not can:
                    logger.debug("[DreamEngine] Cannot dream: %s", reason)
                    # If prime is not available, emit dormant (TC24)
                    if "prime" in reason.lower() and "healthy" not in reason.lower():
                        await self._emit_dormant(reason)
                    await asyncio.sleep(_DREAM_LOOP_INTERVAL_S)
                    continue

                # Attempt to compute a blueprint
                await self._run_dream_job()

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[DreamEngine] Unexpected error in dream loop")

            try:
                await asyncio.sleep(_DREAM_LOOP_INTERVAL_S)
            except asyncio.CancelledError:
                raise

    async def _run_dream_job(self) -> Optional[ImprovementBlueprint]:
        """Execute a single dream job: generate an improvement blueprint.

        Returns the computed blueprint or None if preempted / skipped.
        """
        start_mono = time.monotonic()

        # Build candidate info (in a real implementation this would
        # come from the oracle/memory engine analysis of the repo)
        candidate = self._pick_candidate()
        if candidate is None:
            logger.debug("[DreamEngine] No candidate to dream about")
            return None

        job_key = compute_job_key(
            candidate["repo_sha"],
            candidate["policy_hash"],
            candidate["prompt_family"],
            candidate["model_class"],
        )

        # Idempotency check
        if self._is_job_completed(
            job_key, candidate["repo_sha"], candidate["policy_hash"],
        ):
            self._metrics_tracker.record_dedup()
            logger.debug("[DreamEngine] Job %s already completed, skipping", job_key[:16])
            return None

        # Check preemption before HTTP (TC17)
        if self._check_preempted():
            self._save_interrupted(job_key, candidate)
            self._metrics_tracker.record_preemption()
            self._last_user_return = time.monotonic()
            return None

        # Build prompt (TC23: capped)
        prompt = self._build_dream_prompt(candidate)
        prompt = self._truncate_prompt(prompt)

        # Send via DW (primary) → Claude (fallback) → J-Prime (legacy)
        result = await self._call_inference(prompt)

        # Check preemption after HTTP (TC17)
        if self._check_preempted():
            self._save_interrupted(job_key, candidate)
            self._metrics_tracker.record_preemption()
            self._last_user_return = time.monotonic()
            return None

        if result is None:
            # J-Prime unavailable — emit dormant (TC24)
            await self._emit_dormant("jprime_request_failed")
            return None

        # Build blueprint from result
        blueprint_id = compute_blueprint_id(
            candidate["repo_sha"],
            candidate["policy_hash"],
            candidate["prompt_family"],
            candidate["model_class"],
        )
        blueprint = self._parse_blueprint_result(
            blueprint_id, candidate, result,
        )
        if blueprint is not None:
            self._blueprints[blueprint_id] = blueprint
            self._completed_keys.add(job_key)
            self._metrics_tracker.record_blueprint_computed()

        # Record compute time
        elapsed_min = (time.monotonic() - start_mono) / 60.0
        self._metrics_tracker.record_compute_time(elapsed_min)

        return blueprint

    def _pick_candidate(self) -> Optional[Dict[str, Any]]:
        """Select the next candidate for speculative analysis.

        In a full implementation, this would consult the oracle for
        high-fragility files and the memory engine for recent failures.
        For now returns a skeleton candidate derived from current state.
        """
        # Check for interrupted jobs first (TC30: resume)
        if self._interrupted_jobs:
            key, info = next(iter(self._interrupted_jobs.items()))
            del self._interrupted_jobs[key]
            logger.info("[DreamEngine] Resuming interrupted job %s", key[:16])
            return info

        # Placeholder — real implementation would query oracle + memory
        if not self._current_head or not self._current_policy_hash:
            return None

        return {
            "repo": "jarvis",
            "repo_sha": self._current_head,
            "policy_hash": self._current_policy_hash,
            "prompt_family": "general_improvement",
            "model_class": "qwen2.5-7b",
        }

    def _build_dream_prompt(self, candidate: Dict[str, Any]) -> str:
        """Build the speculative analysis prompt for J-Prime."""
        return (
            f"Analyze the repository at SHA {candidate['repo_sha']} "
            f"for potential improvements in the '{candidate['prompt_family']}' "
            f"category.  Suggest one concrete, small improvement with "
            f"estimated effort, target files, risk assessment, and approach.  "
            f"Return JSON with keys: title, description, category, "
            f"priority_score, target_files, estimated_effort, "
            f"estimated_cost_usd, suggested_approach, risk_assessment."
        )

    async def _call_inference(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Send prompt via DW (primary) → Claude (fallback) → J-Prime (legacy).

        Tiered inference strategy:
            1. DW 35B for light dreaming (cheap, fast)
            2. Claude API as fallback (reliable, more expensive)
            3. J-Prime direct HTTP as legacy fallback (TC29)

        Returns parsed JSON response or None on any failure.
        """
        # Check preemption before any call (TC17)
        if self._check_preempted():
            return None

        # Tier 1: Doubleword (35B for light dreaming — 30x cheaper than Claude)
        if self._dw_provider is not None:
            try:
                _dw_model = os.environ.get(
                    "JARVIS_DREAM_DW_MODEL", "Qwen/Qwen3.5-35B-A3B-FP8"
                )
                raw = await self._dw_provider.prompt_only(
                    prompt=prompt,
                    model=_dw_model,
                    caller_id="dream_engine",
                    response_format={"type": "json_object"},
                    max_tokens=DREAM_MAX_PROMPT_CHARS,
                )
                if self._check_preempted():
                    return None
                if raw:
                    result = self._parse_json_response(raw)
                    if result is not None:
                        result["_inference_provider"] = "doubleword"
                        result["_inference_model"] = _dw_model
                        logger.debug("[DreamEngine] DW inference succeeded (model=%s)", _dw_model)
                        return result
                    logger.debug("[DreamEngine] DW returned non-JSON, trying next tier")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[DreamEngine] DW inference failed: %s", exc)

        # Tier 2: Claude API (reliable, handles complex reasoning)
        if self._claude_provider is not None:
            try:
                raw = await self._claude_provider.prompt_only(
                    prompt=prompt,
                    caller_id="dream_engine",
                    response_format={"type": "json_object"},
                    max_tokens=DREAM_MAX_PROMPT_CHARS,
                )
                if self._check_preempted():
                    return None
                if raw:
                    result = self._parse_json_response(raw)
                    if result is not None:
                        result["_inference_provider"] = "claude"
                        logger.debug("[DreamEngine] Claude inference succeeded")
                        return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("[DreamEngine] Claude inference failed: %s", exc)

        # Tier 3: J-Prime direct HTTP (legacy fallback — TC29)
        return await self._call_jprime_legacy(prompt)

    async def _call_jprime_legacy(self, prompt: str) -> Optional[Dict[str, Any]]:
        """Legacy J-Prime direct HTTP fallback (TC29).

        Does NOT use PrimeClient or PrimeRouter — uses aiohttp directly
        so record_jprime_activity() is never triggered.
        """
        if not self._jprime_url:
            logger.debug("[DreamEngine] No jprime_url configured, skipping HTTP")
            return None

        try:
            import aiohttp

            url = self._jprime_url.rstrip("/") + _JPRIME_GENERATE_ENDPOINT
            payload = {
                "prompt": prompt,
                "max_tokens": DREAM_MAX_PROMPT_CHARS,
                "temperature": 0.3,
                "source": "dream_engine",
            }

            if self._check_preempted():
                return None

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=_JPRIME_TIMEOUT_S),
                ) as resp:
                    if self._check_preempted():
                        return None
                    if resp.status != 200:
                        logger.warning(
                            "[DreamEngine] J-Prime returned %d", resp.status,
                        )
                        return None
                    result = await resp.json()
                    if result is not None:
                        result["_inference_provider"] = "jprime"
                    return result

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("[DreamEngine] J-Prime HTTP call failed: %s", exc)
            return None

    @staticmethod
    def _parse_json_response(raw: str) -> Optional[Dict[str, Any]]:
        """Parse a JSON response from any inference provider."""
        import json as _json
        text = raw.strip()
        # Handle markdown-wrapped JSON
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(ln for ln in lines if not ln.strip().startswith("```"))
        try:
            data = _json.loads(text)
            if isinstance(data, dict):
                return data
        except _json.JSONDecodeError:
            # Try to extract JSON object from text
            start = text.find("{")
            end = text.rfind("}")
            if start >= 0 and end > start:
                try:
                    return _json.loads(text[start:end + 1])
                except _json.JSONDecodeError:
                    pass
        return None

    def _parse_blueprint_result(
        self,
        blueprint_id: str,
        candidate: Dict[str, Any],
        result: Dict[str, Any],
    ) -> Optional[ImprovementBlueprint]:
        """Parse the J-Prime response into an ImprovementBlueprint."""
        try:
            return ImprovementBlueprint(
                blueprint_id=blueprint_id,
                title=result.get("title", "Untitled improvement"),
                description=result.get("description", ""),
                category=result.get("category", candidate.get("prompt_family", "general")),
                priority_score=float(result.get("priority_score", 0.5)),
                target_files=tuple(result.get("target_files", ())),
                estimated_effort=result.get("estimated_effort", "small"),
                estimated_cost_usd=float(result.get("estimated_cost_usd", 0.01)),
                repo=candidate.get("repo", "jarvis"),
                repo_sha=candidate["repo_sha"],
                computed_at_utc=datetime.now(timezone.utc).isoformat(),
                ttl_hours=self._config.dream_blueprint_ttl_hours,
                model_used=result.get("_inference_model", candidate.get("model_class", "unknown")),
                policy_hash=candidate["policy_hash"],
                oracle_neighborhood=result.get("oracle_neighborhood", {}),
                suggested_approach=result.get("suggested_approach", ""),
                risk_assessment=result.get("risk_assessment", ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning(
                "[DreamEngine] Failed to parse blueprint from J-Prime result: %s",
                exc,
            )
            return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist_state(self) -> None:
        """Write all blueprints and completed keys to disk."""
        try:
            # Persist each blueprint individually
            for bp_id, bp in self._blueprints.items():
                bp_path = self._persistence_dir / f"blueprint_{bp_id}.json"
                bp_path.write_text(
                    json.dumps(_blueprint_to_dict(bp), indent=2),
                    encoding="utf-8",
                )

            # Persist completed keys
            keys_path = self._persistence_dir / "job_keys.json"
            keys_path.write_text(
                json.dumps(sorted(self._completed_keys), indent=2),
                encoding="utf-8",
            )

            # Persist metrics via tracker
            metrics_path = self._persistence_dir / "metrics.json"
            self._metrics_tracker.persist(metrics_path)

            logger.debug(
                "[DreamEngine] Persisted %d blueprints, %d keys",
                len(self._blueprints),
                len(self._completed_keys),
            )
        except OSError as exc:
            logger.error("[DreamEngine] Failed to persist state: %s", exc)

    def _load_state(self) -> None:
        """Restore blueprints and completed keys from disk."""
        # Load blueprints
        try:
            for bp_file in self._persistence_dir.glob("blueprint_*.json"):
                try:
                    data = json.loads(bp_file.read_text(encoding="utf-8"))
                    bp = _blueprint_from_dict(data)
                    self._blueprints[bp.blueprint_id] = bp
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                    logger.warning(
                        "[DreamEngine] Skipping corrupt blueprint file %s: %s",
                        bp_file.name,
                        exc,
                    )
        except OSError as exc:
            logger.warning("[DreamEngine] Failed to scan blueprint files: %s", exc)

        # Load completed keys
        keys_path = self._persistence_dir / "job_keys.json"
        if keys_path.exists():
            try:
                data = json.loads(keys_path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self._completed_keys = set(data)
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "[DreamEngine] Failed to load job keys: %s", exc,
                )

        # Load metrics if available
        metrics_path = self._persistence_dir / "metrics.json"
        if metrics_path.exists():
            try:
                restored = DreamMetricsTracker.load(metrics_path)
                # Merge counters into the active tracker
                restored_metrics = restored.get_metrics()
                self._metrics_tracker.record_compute_time(
                    restored_metrics.opportunistic_compute_minutes,
                )
            except Exception as exc:
                logger.debug(
                    "[DreamEngine] Failed to load metrics (non-fatal): %s", exc,
                )

        logger.info(
            "[DreamEngine] Loaded %d blueprints, %d completed keys from disk",
            len(self._blueprints),
            len(self._completed_keys),
        )
