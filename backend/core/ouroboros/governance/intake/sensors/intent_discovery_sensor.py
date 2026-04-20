"""IntentDiscoverySensor — Intent-driven codebase exploration.

The missing link between the Manifesto/DreamEngine/GoalDecomposer and the
sensor layer. Instead of blind static analysis, this sensor:

1. Reads strategic direction (Manifesto principles) to understand WHAT we're building
2. Consults DreamEngine for unacted improvement blueprints
3. Uses Oracle semantic search to find relevant files
4. Uses DW 35B to synthesize concrete exploration intents
5. Creates envelopes that know WHY they exist

Manifesto §1 (Unified Organism): Ouroboros explores with PURPOSE, guided by
the developer's vision and the system's own learned priorities.

Safety: All intent-discovery envelopes require human acknowledgment (AC2).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.core.ouroboros.governance.intake.intent_envelope import make_envelope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_POLL_S = float(os.environ.get("JARVIS_INTENT_DISCOVERY_INTERVAL_S", "900"))
_MAX_INTENTS_PER_CYCLE = int(os.environ.get("JARVIS_INTENT_DISCOVERY_MAX_PER_CYCLE", "5"))
_DW_MODEL = os.environ.get("JARVIS_INTENT_DISCOVERY_MODEL", "Qwen/Qwen3.5-35B-A3B-FP8")
_DW_MAX_TOKENS = int(os.environ.get("JARVIS_INTENT_DISCOVERY_MAX_TOKENS", "2000"))


# ---------------------------------------------------------------------------
# Gap #4 migration: event-driven mode (consumes ConversationBridge turns)
# ---------------------------------------------------------------------------
#
# When ``JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED=true``, the sensor
# registers a turn-observer on the active ConversationBridge. Each
# dialogue turn (user / assistant / postmortem / ask_human) bumps a
# "last turn seen" timestamp. A silence-window loop fires DW inference
# only when the human has been quiet for at least SILENCE_S (default
# 30s) — so we never synthesize intent mid-thought.
#
# This is the FIRST gap-#4 sensor whose event source is NOT fs.changed
# — intent comes from the conversation bus, not the filesystem.
#
# Storm guard is layered (unique to this sensor because every trigger
# is a DW inference call, i.e. a real dollar cost):
#
#   1. Silence window (SILENCE_S, default 30s) — only fire after the
#      human has stopped typing. Prevents mid-sentence triggers.
#   2. Inference cooldown (COOLDOWN_S, default 300s = 5min) — a hard
#      floor between two DW inference calls regardless of how chatty
#      the human is. Caps worst-case latency to ~1 inference / 5min.
#   3. Hourly ceiling (HOURLY_CAP, default 10) — absolute upper bound
#      on inference invocations in any 3600s rolling window. Even if
#      both 1 and 2 say "go", #3 is the last line of defense against
#      a token-bill disaster.
#
# Poll demotes to ``JARVIS_INTENT_DISCOVERY_FALLBACK_INTERVAL_S``
# (default 14400s = 4h) — the fallback exists only to catch missed
# observer dispatches; the conversation bus is the primary path.
#
# Shadow default = off — flipped only after the full 3-constraint
# storm-guard is observed to hold under real chat load.
def events_enabled() -> bool:
    """Re-read ``JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED`` at call-time."""
    return os.environ.get(
        "JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED", "false",
    ).lower() in ("true", "1", "yes")


_INTENT_FALLBACK_INTERVAL_S: float = float(
    os.environ.get("JARVIS_INTENT_DISCOVERY_FALLBACK_INTERVAL_S", "14400")
)
_INTENT_SILENCE_S: float = float(
    os.environ.get("JARVIS_INTENT_DISCOVERY_SILENCE_S", "30")
)
_INTENT_COOLDOWN_S: float = float(
    os.environ.get("JARVIS_INTENT_DISCOVERY_COOLDOWN_S", "300")
)
_INTENT_HOURLY_CAP: int = int(
    os.environ.get("JARVIS_INTENT_DISCOVERY_HOURLY_CAP", "10")
)
# How often the silence-window loop wakes. Too short = wasted CPU; too
# long = latency between silence and fire. 5s is a reasonable default
# given SILENCE_S=30 (~6 checks per silence window).
_INTENT_CHECK_INTERVAL_S: float = float(
    os.environ.get("JARVIS_INTENT_DISCOVERY_CHECK_INTERVAL_S", "5")
)

_SYNTHESIS_SYSTEM = """\
You are the Intent Discovery Engine for the JARVIS Trinity AI ecosystem.
Given the developer's strategic vision (Manifesto principles), existing
improvement blueprints, and semantic search results, synthesize concrete
code improvement intents.

For each intent, identify:
1. target_files: specific file paths that need work
2. description: what should be done and WHY (linked to a principle)
3. urgency: critical/high/normal/low
4. confidence: 0.0-1.0 how certain this is valuable work

Return JSON array of objects:
[
  {{
    "target_files": ["path/to/file.py"],
    "description": "...",
    "urgency": "normal",
    "confidence": 0.7,
    "principle": "which manifesto principle this serves",
    "rationale": "why this matters now"
  }}
]

Rules:
- Maximum {max_intents} intents per response
- Only suggest files that actually exist in the search results
- Prefer high-impact, low-risk improvements
- Each intent should serve a different manifesto principle if possible
- Do NOT suggest test files or configuration files
"""


# ---------------------------------------------------------------------------
# IntentDiscoverySensor
# ---------------------------------------------------------------------------


class IntentDiscoverySensor:
    """Intent-driven codebase exploration sensor.

    Lazily resolves dependencies from GovernedLoopService at scan time,
    so constructor requires no async setup.

    Parameters
    ----------
    gls:
        GovernedLoopService instance (for resolving DW, Oracle, StrategicDirection).
    router:
        UnifiedIntakeRouter for envelope submission.
    repo:
        Repository name.
    project_root:
        Repository root path.
    poll_interval_s:
        Seconds between discovery cycles.
    """

    def __init__(
        self,
        gls: Any,
        router: Any,
        repo: str = "jarvis",
        project_root: Optional[Path] = None,
        poll_interval_s: float = _DEFAULT_POLL_S,
    ) -> None:
        self._gls = gls
        self._router = router
        self._repo = repo
        self._project_root = project_root or Path.cwd()
        self._poll_interval_s = poll_interval_s
        self._running = False

        # State
        self._cycle: int = 0
        self._total_intents_submitted: int = 0
        self._last_principles_hash: str = ""
        self._cooldown_files: Dict[str, int] = {}  # file → cycle when last submitted
        self._cooldown_cycles: int = 10  # don't re-submit same file for 10 cycles

        # --- Gap #4 event-driven state -------------------------------
        # Captured at __init__ so a runtime env flip does not
        # retroactively demote the poll loop; matches every earlier
        # gap-#4 migration.
        self._events_mode: bool = events_enabled()
        self._events_received: int = 0
        self._events_ignored: int = 0
        # Most recent turn-observer timestamp (monotonic seconds). Zero
        # means "no turn seen yet in this sensor lifetime".
        self._last_turn_ts: float = 0.0
        # Most recent DW inference fire (monotonic). Used by the
        # cooldown floor; zero means "never fired".
        self._last_inference_ts: float = 0.0
        # Rolling 3600s window of inference timestamps (monotonic).
        self._hourly_fires: List[float] = []
        # Storm-guard trip counters for telemetry — each counts the
        # number of times a silence-window evaluation was REJECTED for
        # that specific reason. Invariant:
        #   fires + no_turn_yet + silence_not_met + cooldown_active
        #         + hourly_cap_hit == total silence-window evaluations.
        self._sw_fires: int = 0
        self._sw_no_turn_yet: int = 0
        self._sw_silence_not_met: int = 0
        self._sw_cooldown_active: int = 0
        self._sw_hourly_cap_hit: int = 0
        # Observer cleanup — holds (bridge, callback) so stop() can
        # unregister on the original bridge object.
        self._bridge_ref: Any = None
        # Background tasks.
        self._silence_task: Optional[asyncio.Task] = None
        self._poll_task: Optional[asyncio.Task] = None
        # Guard against two concurrent scan_once invocations (silence
        # window racing the fallback poll during demotion).
        self._scan_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Dependency resolution (lazy, fault-tolerant)
    # ------------------------------------------------------------------

    def _get_dw(self) -> Any:
        """Resolve DoublewordProvider from GLS."""
        return getattr(self._gls, "_doubleword_ref", None)

    def _get_oracle(self) -> Any:
        """Resolve Oracle from GLS."""
        return getattr(self._gls, "_oracle", None)

    def _get_strategic(self) -> Any:
        """Resolve StrategicDirectionService from GLS."""
        return getattr(self._gls, "_strategic_direction", None)

    def _get_dream_engine(self) -> Any:
        """Resolve DreamEngine (via ConsciousnessBridge → consciousness → dream_engine)."""
        cb = getattr(self._gls, "_consciousness_bridge", None)
        if cb is None:
            return None
        consciousness = getattr(cb, "_consciousness", None)
        if consciousness is None:
            return None
        return getattr(consciousness, "_dream_engine", None)

    # ------------------------------------------------------------------
    # Core scan logic
    # ------------------------------------------------------------------

    async def scan_once(self) -> List[Dict[str, Any]]:
        """Run one intent discovery cycle.

        Returns list of submitted intent dicts for diagnostics.
        """
        self._cycle += 1
        t0 = time.monotonic()

        # Resolve deps
        dw = self._get_dw()
        oracle = self._get_oracle()
        strategic = self._get_strategic()
        dream = self._get_dream_engine()

        # Gate: need at least DW + (strategic OR oracle)
        if dw is None:
            logger.debug(
                "[IntentDiscovery] cycle %d skipped: no DW provider", self._cycle
            )
            return []

        if strategic is None and oracle is None:
            logger.debug(
                "[IntentDiscovery] cycle %d skipped: no strategic direction or oracle",
                self._cycle,
            )
            return []

        # Phase 1: Gather strategic context
        context_parts: List[str] = []

        if strategic is not None and getattr(strategic, "is_loaded", False):
            principles = getattr(strategic, "principles", [])
            digest = getattr(strategic, "digest", "")
            if principles:
                context_parts.append(
                    "## Strategic Vision (Manifesto Principles)\n"
                    + "\n".join(f"- {p}" for p in principles)
                )
            if digest:
                context_parts.append(f"## Architecture Digest\n{digest[:2000]}")

        # Phase 2: Gather DreamEngine blueprints (unacted improvement ideas)
        blueprints: List[Any] = []
        if dream is not None:
            try:
                blueprints = dream.get_blueprints(top_n=5)
            except Exception:
                pass

        if blueprints:
            bp_lines = []
            for bp in blueprints:
                bp_lines.append(
                    f"- [{bp.category}] {bp.title} "
                    f"(priority={bp.priority_score:.2f}, "
                    f"files={', '.join(bp.target_files[:3])})"
                )
            context_parts.append(
                "## Unacted Improvement Blueprints\n" + "\n".join(bp_lines)
            )

        # Phase 3: Oracle semantic search for related files
        search_queries = self._build_search_queries(strategic, blueprints)
        oracle_results: List[Tuple[str, float]] = []

        if oracle is not None and search_queries:
            for query in search_queries[:3]:
                try:
                    if hasattr(oracle, "semantic_search"):
                        results = await oracle.semantic_search(query, k=10)
                    elif hasattr(oracle, "_semantic_index"):
                        idx = oracle._semantic_index
                        if idx is not None:
                            results = await idx.semantic_search(query, k=10)
                        else:
                            results = []
                    else:
                        results = []
                    oracle_results.extend(results)
                except Exception as exc:
                    logger.debug(
                        "[IntentDiscovery] Oracle search failed for %r: %s",
                        query, exc,
                    )

        # Deduplicate oracle results by file path
        seen_files: Set[str] = set()
        unique_results: List[Tuple[str, float]] = []
        for file_key, score in oracle_results:
            # file_key may be "repo:path" or just "path"
            path = file_key.split(":", 1)[-1] if ":" in file_key else file_key
            if path not in seen_files:
                seen_files.add(path)
                unique_results.append((path, score))
        unique_results.sort(key=lambda x: x[1], reverse=True)
        unique_results = unique_results[:20]

        if unique_results:
            file_lines = [
                f"- {path} (relevance={score:.3f})"
                for path, score in unique_results
            ]
            context_parts.append(
                "## Semantically Related Files\n" + "\n".join(file_lines)
            )

        if not context_parts:
            logger.debug(
                "[IntentDiscovery] cycle %d: no context gathered, skipping synthesis",
                self._cycle,
            )
            return []

        # Phase 4: DW synthesis — ask the model to generate concrete intents
        prompt = self._build_synthesis_prompt(context_parts)
        try:
            raw = await dw.prompt_only(
                prompt=prompt,
                model=_DW_MODEL,
                caller_id="intent_discovery_sensor",
                response_format={"type": "json_object"},
                max_tokens=_DW_MAX_TOKENS,
            )
        except Exception as exc:
            logger.warning(
                "[IntentDiscovery] cycle %d: DW synthesis failed: %s",
                self._cycle, exc,
            )
            return []

        if not raw:
            logger.debug(
                "[IntentDiscovery] cycle %d: DW returned empty response", self._cycle
            )
            return []

        # Phase 5: Parse and submit intents
        intents = self._parse_intents(raw, unique_results)
        submitted: List[Dict[str, Any]] = []

        for intent in intents[:_MAX_INTENTS_PER_CYCLE]:
            target_files = tuple(intent.get("target_files", []))
            if not target_files:
                continue

            # Cooldown check
            if all(self._is_on_cooldown(f) for f in target_files):
                continue

            description = intent.get("description", "Intent-driven improvement")
            urgency = intent.get("urgency", "low")
            confidence = max(0.1, min(1.0, float(intent.get("confidence", 0.5))))

            envelope = make_envelope(
                source="intent_discovery",
                description=description,
                target_files=target_files,
                repo=self._repo,
                confidence=confidence,
                urgency=urgency,
                evidence={
                    "sensor": "intent_discovery",
                    "cycle": self._cycle,
                    "principle": intent.get("principle", ""),
                    "rationale": intent.get("rationale", ""),
                    "oracle_scores": {
                        f: s for f, s in unique_results
                        if f in target_files
                    },
                    "blueprint_count": len(blueprints),
                    "dw_model": _DW_MODEL,
                },
                requires_human_ack=True,  # AC2 safety invariant
            )

            try:
                result = await self._router.ingest(envelope)
                if result in ("enqueued", "pending_ack"):
                    for f in target_files:
                        self._cooldown_files[f] = self._cycle
                    self._total_intents_submitted += 1
                    submitted.append(intent)
                    logger.info(
                        "[IntentDiscovery] cycle %d: submitted %s → %s "
                        "(confidence=%.2f, urgency=%s, principle=%s)",
                        self._cycle, target_files, result,
                        confidence, urgency, intent.get("principle", "?"),
                    )
            except Exception:
                logger.debug(
                    "[IntentDiscovery] ingest failed for %s", target_files,
                    exc_info=True,
                )

        # Prune old cooldowns
        cutoff = self._cycle - self._cooldown_cycles * 2
        self._cooldown_files = {
            k: v for k, v in self._cooldown_files.items() if v >= cutoff
        }

        elapsed = time.monotonic() - t0
        logger.info(
            "[IntentDiscovery] cycle %d complete: %.1fs, "
            "context_parts=%d, oracle_files=%d, dw_intents=%d, submitted=%d, "
            "total_submitted=%d",
            self._cycle, elapsed, len(context_parts), len(unique_results),
            len(intents), len(submitted), self._total_intents_submitted,
        )
        return submitted

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_search_queries(
        self, strategic: Any, blueprints: List[Any],
    ) -> List[str]:
        """Build semantic search queries from strategic context + blueprints."""
        queries: List[str] = []

        # From manifesto principles — search for code related to each
        if strategic is not None and getattr(strategic, "is_loaded", False):
            principles = getattr(strategic, "principles", [])
            for p in principles[:4]:
                # Trim to key phrase for better semantic match
                query = f"implementation of: {p[:100]}"
                queries.append(query)

        # From blueprints — search for related code
        for bp in blueprints[:3]:
            queries.append(f"{bp.category}: {bp.title}")

        # Fallback: general architecture queries
        if not queries:
            queries = [
                "core pipeline architecture",
                "error handling and resilience patterns",
                "API integration and external services",
            ]

        return queries

    def _build_synthesis_prompt(self, context_parts: List[str]) -> str:
        """Build the DW synthesis prompt from gathered context."""
        system = _SYNTHESIS_SYSTEM.format(max_intents=_MAX_INTENTS_PER_CYCLE)
        context = "\n\n".join(context_parts)
        return (
            f"{system}\n\n"
            f"---\n\n"
            f"{context}\n\n"
            f"---\n\n"
            f"Based on the strategic vision, unacted blueprints, and semantically "
            f"related files above, synthesize up to {_MAX_INTENTS_PER_CYCLE} "
            f"concrete improvement intents. Return as a JSON array."
        )

    def _parse_intents(
        self,
        raw: str,
        oracle_results: List[Tuple[str, float]],
    ) -> List[Dict[str, Any]]:
        """Parse DW response into validated intent dicts."""
        # Extract JSON from response (may be wrapped in markdown)
        text = raw.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines (```json and ```)
            text = "\n".join(
                ln for ln in lines
                if not ln.strip().startswith("```")
            )

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to find JSON array in the text
            start = text.find("[")
            end = text.rfind("]")
            if start >= 0 and end > start:
                try:
                    data = json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    logger.debug("[IntentDiscovery] Failed to parse DW JSON response")
                    return []
            else:
                return []

        if isinstance(data, dict) and "intents" in data:
            data = data["intents"]

        if not isinstance(data, list):
            return []

        # Validate: only keep intents with files that exist in oracle results
        known_files = {path for path, _ in oracle_results}
        validated: List[Dict[str, Any]] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            target_files = item.get("target_files", [])
            if not isinstance(target_files, list) or not target_files:
                continue
            # Allow files that are in oracle results OR actually exist on disk
            valid_files = []
            for f in target_files:
                if f in known_files:
                    valid_files.append(f)
                elif (self._project_root / f).is_file():
                    valid_files.append(f)
            if valid_files:
                item["target_files"] = valid_files
                validated.append(item)

        return validated

    def _is_on_cooldown(self, file_path: str) -> bool:
        """Check if file is within cooldown window."""
        if file_path not in self._cooldown_files:
            return False
        return (self._cycle - self._cooldown_files[file_path]) < self._cooldown_cycles

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start background discovery loop (and silence-window loop)."""
        self._running = True
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name="intent_discovery_poll",
        )
        if self._events_mode:
            self._silence_task = asyncio.create_task(
                self._silence_window_loop(),
                name="intent_discovery_silence_window",
            )
        effective = (
            _INTENT_FALLBACK_INTERVAL_S
            if self._events_mode
            else self._poll_interval_s
        )
        mode = (
            "event-primary (conversation-bridge → silence-window → scan; poll=fallback)"
            if self._events_mode
            else "poll-primary"
        )
        logger.info(
            "[IntentDiscovery] Started poll_interval=%ds max_per_cycle=%d "
            "model=%s mode=%s silence_s=%ds cooldown_s=%ds hourly_cap=%d",
            int(effective), _MAX_INTENTS_PER_CYCLE, _DW_MODEL, mode,
            int(_INTENT_SILENCE_S), int(_INTENT_COOLDOWN_S), _INTENT_HOURLY_CAP,
        )

    def stop(self) -> None:
        """Stop the discovery loop + silence-window loop, unregister observer."""
        self._running = False
        # Unregister from bridge if we were subscribed.
        bridge = self._bridge_ref
        if bridge is not None:
            try:
                bridge.unregister_turn_observer(self._on_turn)
            except Exception:
                logger.debug(
                    "[IntentDiscovery] bridge unregister failed",
                    exc_info=True,
                )
            self._bridge_ref = None
        for task in (self._silence_task, self._poll_task):
            if task is not None and not task.done():
                task.cancel()

    # ------------------------------------------------------------------
    # Event-driven path (Manifesto §3: zero polling, conversation-driven)
    # ------------------------------------------------------------------

    def subscribe_to_bridge(self, bridge: Any) -> None:
        """Register a turn-observer on the supplied ConversationBridge.

        Gated by ``JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED`` (default
        OFF). When the flag is off this is a logged no-op so legacy
        15min-poll behavior is preserved. Unlike the other gap-#4
        sensors, this method is synchronous — the bridge's observer
        registry takes sync callables; the async silence-window loop
        is started separately in ``start()``.

        Registration failures are caught locally — the intake boot must
        never regress just because the bridge rejected a callback.
        """
        if not self._events_mode:
            logger.debug(
                "[IntentDiscovery] Bridge subscription skipped "
                "(JARVIS_INTENT_DISCOVERY_EVENTS_ENABLED=false). "
                "Poll-primary mode active — no gap #4 resolution.",
            )
            return

        try:
            bridge.register_turn_observer(self._on_turn)
        except Exception as exc:
            logger.warning(
                "[IntentDiscovery] Bridge subscription failed: %s "
                "(poll-fallback at %ds continues)",
                exc, int(_INTENT_FALLBACK_INTERVAL_S),
            )
            return

        self._bridge_ref = bridge
        logger.info(
            "[IntentDiscovery] subscribed to ConversationBridge — "
            "conversation turns now PRIMARY (poll demoted to %ds fallback, "
            "silence=%ds, inference_cooldown=%ds, hourly_cap=%d)",
            int(_INTENT_FALLBACK_INTERVAL_S), int(_INTENT_SILENCE_S),
            int(_INTENT_COOLDOWN_S), _INTENT_HOURLY_CAP,
        )

    def _on_turn(self, turn: Any) -> None:
        """Turn-observer callback — fires on the caller's thread.

        Kept deliberately cheap: just updates the "last turn" monotonic
        timestamp. The actual inference decision is made by the async
        silence-window loop. The observer contract requires this to be
        synchronous and non-raising.
        """
        try:
            self._last_turn_ts = time.monotonic()
            self._events_received += 1
        except Exception:
            # Should be unreachable — updating a float and int cannot
            # raise — but the observer contract demands we never
            # propagate, so the try/except is a belt-and-braces guard.
            self._events_ignored += 1

    async def _silence_window_loop(self) -> None:
        """Periodic evaluator — fires scan_once only when all three
        storm-guard constraints are satisfied simultaneously.

        Guard order matters for telemetry: the first failing constraint
        wins, and the corresponding ``_sw_*`` counter is bumped. The
        invariant
            fires + no_turn_yet + silence_not_met
                  + cooldown_active + hourly_cap_hit
        equals the total number of evaluations the loop has performed.
        """
        # Wait one period before the first evaluation — avoid firing
        # during the boot settling window.
        try:
            await asyncio.sleep(_INTENT_CHECK_INTERVAL_S)
        except asyncio.CancelledError:
            return

        while self._running:
            try:
                await self._evaluate_silence_window()
            except Exception:
                logger.exception(
                    "[IntentDiscovery] silence-window evaluation error",
                )
            try:
                await asyncio.sleep(_INTENT_CHECK_INTERVAL_S)
            except asyncio.CancelledError:
                break

    async def _evaluate_silence_window(self) -> bool:
        """Run the three-constraint check once.

        Returns True if a scan was actually fired. All rejection paths
        bump the relevant ``_sw_*`` counter and return False.
        """
        now = time.monotonic()

        # Constraint 1: we need at least one turn to have been seen.
        if self._last_turn_ts == 0.0:
            self._sw_no_turn_yet += 1
            return False

        # Constraint 2: silence window — time since last turn must meet
        # threshold. Prevents mid-sentence triggers.
        silence_elapsed = now - self._last_turn_ts
        if silence_elapsed < _INTENT_SILENCE_S:
            self._sw_silence_not_met += 1
            return False

        # Constraint 3: inference cooldown — hard floor between two
        # DW calls regardless of how chatty the human is.
        if self._last_inference_ts > 0.0:
            cooldown_elapsed = now - self._last_inference_ts
            if cooldown_elapsed < _INTENT_COOLDOWN_S:
                self._sw_cooldown_active += 1
                return False

        # Constraint 4: hourly cap — prune stale entries first, then
        # check count. Last line of defense against a token-bill blow-out.
        cutoff = now - 3600.0
        self._hourly_fires = [t for t in self._hourly_fires if t > cutoff]
        if len(self._hourly_fires) >= _INTENT_HOURLY_CAP:
            self._sw_hourly_cap_hit += 1
            return False

        # All checks passed — fire. Update cooldown + hourly ledger
        # BEFORE calling scan_once so a concurrent re-entry (fallback
        # poll during an active scan) still sees the updated state.
        self._last_inference_ts = now
        self._hourly_fires.append(now)
        self._sw_fires += 1
        logger.info(
            "[IntentDiscovery] scan trigger=conversation_silence_window "
            "silence_elapsed=%.1fs hourly_fires=%d",
            silence_elapsed, len(self._hourly_fires),
        )
        async with self._scan_lock:
            try:
                await self.scan_once()
            except Exception:
                logger.exception(
                    "[IntentDiscovery] silence-window scan_once failed",
                )
        return True

    async def _poll_loop(self) -> None:
        """Background polling loop — primary when events off, fallback when on."""
        # Initial delay — let other subsystems boot first
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            return

        while self._running:
            try:
                logger.debug(
                    "[IntentDiscovery] scan trigger=%s",
                    "fallback_poll" if self._events_mode else "poll",
                )
                async with self._scan_lock:
                    await self.scan_once()
            except Exception:
                logger.exception("[IntentDiscovery] poll error")
            effective_interval = (
                _INTENT_FALLBACK_INTERVAL_S
                if self._events_mode
                else self._poll_interval_s
            )
            try:
                await asyncio.sleep(effective_interval)
            except asyncio.CancelledError:
                break

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """Return health metrics."""
        return {
            "cycles": self._cycle,
            "total_submitted": self._total_intents_submitted,
            "cooldown_pool": len(self._cooldown_files),
            "has_dw": self._get_dw() is not None,
            "has_oracle": self._get_oracle() is not None,
            "has_strategic": self._get_strategic() is not None,
            "has_dream": self._get_dream_engine() is not None,
        }
