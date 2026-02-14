"""
Context Awareness Intelligence (CAI) runtime implementation.

Provides:
- Intent prediction with persistent history
- User preference modeling in SQLite
- Context summarization across sessions
- Optional LangGraph-backed user-state reasoning
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from intelligence.intelligence_langgraph import create_enhanced_cai

logger = logging.getLogger(__name__)


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key, str(default))
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _default_db_path() -> Path:
    root = Path.home() / ".jarvis"
    root.mkdir(parents=True, exist_ok=True)
    return root / "context_awareness.sqlite3"


class ContextAwarenessIntelligence:
    """
    Context reasoning service with persistent intent and preference memory.

    Synchronous public APIs (`predict_intent`) are intentional for backward
    compatibility with thread-offloaded call sites.
    """

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self._lock = threading.RLock()
        requested_db_path = db_path or Path(
            os.getenv("JARVIS_CAI_DB_PATH", str(_default_db_path()))
        )
        self._conn, self._db_location = self._open_connection(requested_db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

        self._history_limit = max(100, _env_int("JARVIS_CAI_HISTORY_LIMIT", 1000))
        self._intent_history: Deque[Dict[str, Any]] = deque(maxlen=self._history_limit)
        self._preferences: Dict[str, Dict[str, Any]] = {}
        self._intent_profiles = self._load_intent_profiles()
        self._enhanced_cai = create_enhanced_cai()

        self._metrics: Dict[str, int] = {
            "intents_classified": 0,
            "context_analyses": 0,
            "preference_updates": 0,
            "reasoning_calls": 0,
        }
        self._load_recent_state()

        logger.info("[CAI] ContextAwarenessIntelligence initialized (%s)", self._db_location)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> Dict[str, Any]:
        """Initialize and return status."""
        return self.get_status()

    async def start(self) -> None:
        """Lifecycle no-op for adapter compatibility."""
        return None

    async def stop(self) -> None:
        """Lifecycle no-op for adapter compatibility."""
        return None

    async def start_monitoring(self) -> None:
        """No-op monitoring entrypoint for adapter compatibility."""
        return None

    async def stop_monitoring(self) -> None:
        """No-op monitoring exitpoint for adapter compatibility."""
        return None

    async def cleanup(self) -> None:
        """Cleanup resources."""
        self.close()

    async def shutdown(self) -> None:
        """Shutdown resources."""
        self.close()

    def close(self) -> None:
        """Close SQLite resources safely."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    # ------------------------------------------------------------------
    # Primary CAI API
    # ------------------------------------------------------------------

    def predict_intent(self, text: str) -> Dict[str, Any]:
        """Predict user intent from free-form text with confidence and suggestion."""
        cleaned = (text or "").strip()
        if not cleaned:
            return {
                "intent": "unknown",
                "confidence": 0.0,
                "suggestion": "Ask for clarification",
                "alternatives": [],
            }

        scores: Dict[str, float] = {}
        lowered = cleaned.lower()

        for intent, profile in self._intent_profiles.items():
            score = self._score_intent(lowered, profile)
            scores[intent] = score

        self._apply_history_boost(scores)

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        top_intent, top_score = ranked[0]
        total = sum(max(score, 0.0) for _, score in ranked) or 1.0
        confidence = min(0.99, max(0.05, top_score / total))

        suggestion = self._intent_suggestion(top_intent)
        alternatives = [
            {"intent": intent, "score": round(score, 4)}
            for intent, score in ranked[1:4]
        ]

        result = {
            "intent": top_intent,
            "confidence": round(confidence, 4),
            "suggestion": suggestion,
            "alternatives": alternatives,
            "scores": {k: round(v, 4) for k, v in ranked},
            "timestamp": time.time(),
        }

        self._record_intent(cleaned, result)
        self._metrics["intents_classified"] += 1
        return result

    async def understand_with_reasoning(
        self,
        signals: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Analyze user state using enhanced CAI reasoning and deterministic intent prediction.

        Output schema matches current hybrid orchestrator expectations.
        """
        signals = signals or {}
        text = str(signals.get("command") or signals.get("text") or "")
        workspace_state = signals.get("workspace_state") or {"signal_text": text}
        activity_data = signals.get("activity_data") or {
            "text": text,
            "source": signals.get("source", "runtime"),
        }

        try:
            cai_result = await self._enhanced_cai.analyze_user_state_with_reasoning(
                workspace_state=workspace_state,
                activity_data=activity_data,
            )
            if not isinstance(cai_result, dict):
                raise TypeError(
                    f"Unexpected CAI reasoning result type: {type(cai_result).__name__}"
                )
        except Exception as exc:
            logger.warning("[CAI] Reasoning fallback activated: %s", exc)
            cai_result = {
                "emotional_state": "neutral",
                "cognitive_load": "moderate",
                "confidence": 0.5,
                "reasoning_trace": "Fallback: direct intent/context analysis without graph reasoning.",
                "personality_adjustments": {},
                "recommendations": [],
                "insights": [],
            }
        intent = self.predict_intent(text)
        self._metrics["reasoning_calls"] += 1

        return {
            "emotional_state": cai_result.get("emotional_state", "neutral"),
            "cognitive_state": cai_result.get("cognitive_load", "moderate"),
            "confidence": cai_result.get("confidence", 0.0),
            "reasoning_chain": self._trace_to_chain(cai_result.get("reasoning_trace")),
            "personality_adaptation": cai_result.get("personality_adjustments", {}),
            "intent_prediction": intent,
            "recommendations": cai_result.get("recommendations", []),
            "insights": cai_result.get("insights", []),
        }

    async def analyze_user_state_with_reasoning(
        self,
        workspace_state: Dict[str, Any],
        activity_data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Direct compatibility wrapper to enhanced CAI API."""
        try:
            result = await self._enhanced_cai.analyze_user_state_with_reasoning(
                workspace_state=workspace_state,
                activity_data=activity_data or {},
            )
            if isinstance(result, dict):
                return result
            raise TypeError(
                f"Unexpected CAI reasoning result type: {type(result).__name__}"
            )
        except Exception as exc:
            logger.warning("[CAI] Direct reasoning fallback activated: %s", exc)
            return {
                "emotional_state": "neutral",
                "emotional_confidence": 0.5,
                "cognitive_load": "moderate",
                "work_context": "general",
                "insights": [],
                "recommendations": [],
                "personality_adjustments": {},
                "communication_style": "balanced",
                "confidence": 0.5,
                "thought_count": 0,
                "reasoning_trace": "Fallback: enhanced CAI unavailable.",
            }

    def analyze_context(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Provide contextual summary using recent history and preferences."""
        now = time.time()
        horizon = now - 3600.0
        with self._lock:
            recent = [item for item in self._intent_history if item["timestamp"] >= horizon]
            top_recent = self._top_intents(recent)
            preferences = dict(self._preferences)

        self._metrics["context_analyses"] += 1
        return {
            "recent_intent_distribution": top_recent,
            "recent_count": len(recent),
            "active_preferences": preferences,
            "input_summary": str(data)[:300],
            "timestamp": now,
        }

    def update_user_preference(
        self,
        key: str,
        value: Any,
        confidence: float = 0.7,
        source: str = "runtime",
    ) -> Dict[str, Any]:
        """Update a persisted user preference."""
        pref_key = (key or "").strip().lower()
        if not pref_key:
            raise ValueError("Preference key cannot be empty")

        payload = {
            "value": value,
            "confidence": float(max(0.0, min(1.0, confidence))),
            "source": source,
            "updated_at": time.time(),
        }

        with self._lock:
            self._preferences[pref_key] = payload
            self._upsert_preference(pref_key, payload)
            self._metrics["preference_updates"] += 1

        return {"key": pref_key, **payload}

    def get_user_preferences(self) -> Dict[str, Dict[str, Any]]:
        """Return all known user preferences."""
        with self._lock:
            return dict(self._preferences)

    def get_status(self) -> Dict[str, Any]:
        """Return CAI health/status payload."""
        with self._lock:
            db_available = self._conn is not None
            history_size = len(self._intent_history)
            preference_count = len(self._preferences)
            metrics = dict(self._metrics)

        return {
            "running": True,
            "db_path": self._db_location,
            "db_available": db_available,
            "history_size": history_size,
            "preference_count": preference_count,
            "metrics": metrics,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Create required SQLite schema."""
        cursor = self._conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS cai_intent_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                input_text TEXT NOT NULL,
                intent TEXT NOT NULL,
                confidence REAL NOT NULL,
                suggestion TEXT,
                alternatives_json TEXT,
                scores_json TEXT
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cai_intent_history_created_at
            ON cai_intent_history(created_at)
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS cai_user_preferences (
                preference_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                source TEXT,
                updated_at REAL NOT NULL
            )
            """
        )
        self._conn.commit()

    def _open_connection(self, preferred: Path) -> Tuple[sqlite3.Connection, str]:
        """Open SQLite connection with deterministic fallback order."""
        candidates = [preferred, Path("/tmp/jarvis_context_awareness.sqlite3")]
        last_error: Optional[Exception] = None

        for candidate in candidates:
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(candidate),
                    check_same_thread=False,
                    isolation_level=None,
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=5000")
                return conn, str(candidate)
            except Exception as exc:
                last_error = exc
                logger.warning("[CAI] DB path unavailable (%s): %s", candidate, exc)

        logger.warning(
            "[CAI] Falling back to in-memory SQLite after persistent DB failures: %s",
            last_error,
        )
        conn = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=5000")
        return conn, ":memory:"

    def _load_recent_state(self) -> None:
        """Load history and preferences into memory cache."""
        cursor = self._conn.cursor()
        history_rows = cursor.execute(
            """
            SELECT created_at, input_text, intent, confidence, suggestion
            FROM cai_intent_history
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (self._history_limit,),
        ).fetchall()

        for row in reversed(history_rows):
            self._intent_history.append(
                {
                    "timestamp": float(row["created_at"]),
                    "text": row["input_text"],
                    "intent": row["intent"],
                    "confidence": float(row["confidence"]),
                    "suggestion": row["suggestion"],
                }
            )

        pref_rows = cursor.execute(
            """
            SELECT preference_key, value_json, confidence, source, updated_at
            FROM cai_user_preferences
            """
        ).fetchall()

        for row in pref_rows:
            self._preferences[row["preference_key"]] = {
                "value": json.loads(row["value_json"]),
                "confidence": float(row["confidence"]),
                "source": row["source"],
                "updated_at": float(row["updated_at"]),
            }

    def _load_intent_profiles(self) -> Dict[str, Dict[str, Any]]:
        """Load intent profiles from env override or defaults."""
        raw = os.getenv("JARVIS_CAI_INTENT_PROFILES_JSON", "").strip()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed:
                    return parsed
            except json.JSONDecodeError:
                logger.warning("Invalid JARVIS_CAI_INTENT_PROFILES_JSON, using defaults")

        return {
            "display_control": {"keywords": ["display", "screen", "window", "monitor", "brightness"]},
            "system_command": {"keywords": ["restart", "shutdown", "kill", "process", "service", "lock"]},
            "information_query": {"keywords": ["what", "why", "how", "status", "show", "find"]},
            "automation_request": {"keywords": ["automate", "schedule", "run", "workflow", "pipeline"]},
            "preference_setting": {"keywords": ["prefer", "default", "always", "voice", "setting"]},
            "conversation": {"keywords": ["hello", "hi", "thanks", "good", "chat"]},
        }

    def _score_intent(self, lowered: str, profile: Dict[str, Any]) -> float:
        """Compute intent score from keyword profile."""
        keywords = profile.get("keywords", [])
        score = 0.01

        for keyword in keywords:
            token = str(keyword).strip().lower()
            if not token:
                continue
            if token in lowered:
                score += 1.0
                continue
            if re.search(r"\b" + re.escape(token) + r"\b", lowered):
                score += 0.8

        if lowered.endswith("?"):
            score += 0.25

        return score

    def _apply_history_boost(self, scores: Dict[str, float]) -> None:
        """Boost scores based on recent historical intent continuity."""
        with self._lock:
            recent = list(self._intent_history)[-25:]

        if not recent:
            return

        intent_counts: Dict[str, int] = {}
        for item in recent:
            intent = item.get("intent")
            if not intent:
                continue
            intent_counts[intent] = intent_counts.get(intent, 0) + 1

        total = len(recent)
        for intent, count in intent_counts.items():
            if intent in scores:
                scores[intent] += (count / total) * 0.4

    def _intent_suggestion(self, intent: str) -> str:
        """Default follow-up suggestion per predicted intent."""
        mapping = {
            "display_control": "Identify active display context and propose a precise action.",
            "system_command": "Run a dry-run safety validation before executing system changes.",
            "information_query": "Gather current system context and provide a concise status answer.",
            "automation_request": "Generate a reproducible plan with checkpoints and rollback hooks.",
            "preference_setting": "Persist preference update and confirm effective scope.",
            "conversation": "Respond naturally while keeping system state unchanged.",
        }
        return mapping.get(intent, "Request clarification and refine intent classification.")

    def _record_intent(self, text: str, result: Dict[str, Any]) -> None:
        """Store in-memory and persistent intent history."""
        item = {
            "timestamp": float(result.get("timestamp", time.time())),
            "text": text,
            "intent": result.get("intent"),
            "confidence": float(result.get("confidence", 0.0)),
            "suggestion": result.get("suggestion"),
        }

        with self._lock:
            self._intent_history.append(item)
            self._conn.execute(
                """
                INSERT INTO cai_intent_history (
                    created_at, input_text, intent, confidence, suggestion,
                    alternatives_json, scores_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["timestamp"],
                    text,
                    result.get("intent", "unknown"),
                    item["confidence"],
                    result.get("suggestion"),
                    json.dumps(result.get("alternatives", [])),
                    json.dumps(result.get("scores", {})),
                ),
            )
            self._conn.commit()

    def _upsert_preference(self, key: str, payload: Dict[str, Any]) -> None:
        """Persist preference value with confidence metadata."""
        self._conn.execute(
            """
            INSERT INTO cai_user_preferences (
                preference_key, value_json, confidence, source, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(preference_key)
            DO UPDATE SET
                value_json = excluded.value_json,
                confidence = excluded.confidence,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                key,
                json.dumps(payload.get("value")),
                payload.get("confidence", 0.0),
                payload.get("source", "runtime"),
                payload.get("updated_at", time.time()),
            ),
        )
        self._conn.commit()

    def _top_intents(self, items: List[Dict[str, Any]]) -> List[Tuple[str, int]]:
        """Return sorted intent frequencies for recent context snapshots."""
        counts: Dict[str, int] = {}
        for item in items:
            intent = item.get("intent")
            if not intent:
                continue
            counts[intent] = counts.get(intent, 0) + 1
        return sorted(counts.items(), key=lambda pair: pair[1], reverse=True)

    def _trace_to_chain(self, trace: Optional[str]) -> List[str]:
        """Convert textual reasoning trace into line-based chain output."""
        if not trace:
            return []
        chain = []
        for line in trace.splitlines():
            clean = line.strip()
            if not clean or clean.startswith("==="):
                continue
            chain.append(clean)
        return chain
