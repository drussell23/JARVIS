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
        """Start background discovery loop."""
        self._running = True
        asyncio.create_task(self._poll_loop(), name="intent_discovery_poll")
        logger.info(
            "[IntentDiscovery] Started (interval=%.0fs, max_per_cycle=%d, model=%s)",
            self._poll_interval_s, _MAX_INTENTS_PER_CYCLE, _DW_MODEL,
        )

    def stop(self) -> None:
        """Stop the discovery loop."""
        self._running = False

    async def _poll_loop(self) -> None:
        """Background polling loop."""
        # Initial delay — let other subsystems boot first
        try:
            await asyncio.sleep(30.0)
        except asyncio.CancelledError:
            return

        while self._running:
            try:
                await self.scan_once()
            except Exception:
                logger.exception("[IntentDiscovery] poll error")
            try:
                await asyncio.sleep(self._poll_interval_s)
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
