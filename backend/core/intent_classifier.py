"""IntentClassifier — agentic voice command routing for the E1 bridge.

Pillar 2 (Progressive Awakening):
    classify()       — reflex-arc, sub-millisecond, zero LLM dependency
    classify_async() — agentic semantic classification via PrimeRouter/J-Prime

Pillar 5 (Intelligence-Driven Routing):
    classify_async() uses J-Prime to semantically evaluate intent, extract
    structured fields (provider, search_query, target_app), and determine
    routing — no regex, no pattern tables, no hardcoded keyword lists.

    The sync classify() exists ONLY as a reflex arc for when the Mind is
    unavailable. The async path is the primary intelligence.

Architecture:
    Voice input → IntentClassifier.classify_async(text) → ClassificationResult
        │
        ├─ ACTION → RuntimeTaskOrchestrator.execute(query)
        │              ↓
        │           DAGPlanner → Neural Mesh agents (browser, app, shell)
        │
        └─ QUERY  → HybridOrchestrator.execute_command(text)
                       ↓
                    J-Prime / Claude API (text response)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple

logger = logging.getLogger(__name__)


class CommandIntent(str, Enum):
    """Classification of a voice command's intent."""
    ACTION = "action"    # Route to RuntimeTaskOrchestrator → Neural Mesh
    QUERY = "query"      # Route to HybridOrchestrator → J-Prime text


@dataclass(frozen=True)
class ClassificationResult:
    """Result of intent classification with reasoning.

    Structured fields (provider, search_query, url) are emitted here so the
    orchestrator never needs to scrape goal strings for routing data.
    """
    intent: CommandIntent
    confidence: float       # 0.0-1.0, higher = more certain
    matched_signal: str     # which pattern triggered the classification
    action_category: str    # "browser", "app_control", "system", "communication", etc.
    provider: str = ""      # "youtube", "google", "spotify", etc.
    search_query: str = ""  # extracted search term (e.g. "nba" from "search youtube for nba")
    url: str = ""           # pre-resolved URL if deterministic
    target_app: str = ""    # native app name (e.g. "apple music")


# ---------------------------------------------------------------------------
# Signal tables — ordered by specificity (most specific first)
# ---------------------------------------------------------------------------

# Multi-word action phrases (checked first — more specific = fewer false positives)
_ACTION_PHRASES: List[Tuple[str, str]] = [
    # Browser / web actions
    ("search youtube", "browser"),
    ("search on youtube", "browser"),
    ("search google", "browser"),
    ("search on google", "browser"),
    ("search online", "browser"),
    ("search the web", "browser"),
    ("search for", "browser"),
    ("look up", "browser"),
    ("go to", "browser"),
    ("browse to", "browser"),
    ("open website", "browser"),
    ("navigate to", "browser"),
    ("pull up", "browser"),
    ("show me", "browser"),

    # App control
    ("open app", "app_control"),
    ("open the app", "app_control"),
    ("close app", "app_control"),
    ("close the app", "app_control"),
    ("launch app", "app_control"),
    ("switch to", "app_control"),
    ("open apple music", "app_control"),
    ("open spotify", "app_control"),
    ("open safari", "app_control"),
    ("open chrome", "app_control"),
    ("open finder", "app_control"),
    ("open terminal", "app_control"),
    ("open slack", "app_control"),
    ("open discord", "app_control"),
    ("open linkedin", "app_control"),
    ("open twitter", "app_control"),
    ("open messages", "app_control"),
    ("open notes", "app_control"),
    ("open mail", "app_control"),
    ("open calendar", "app_control"),
    ("open settings", "app_control"),
    ("open system preferences", "app_control"),
    ("play music", "app_control"),
    ("play song", "app_control"),
    ("play the", "app_control"),
    ("pause music", "app_control"),
    ("stop music", "app_control"),
    ("next track", "app_control"),
    ("previous track", "app_control"),
    ("skip song", "app_control"),

    # System actions
    ("connect wifi", "system"),
    ("connect to wifi", "system"),
    ("disconnect wifi", "system"),
    ("turn on", "system"),
    ("turn off", "system"),
    ("enable", "system"),
    ("disable", "system"),
    ("set volume", "system"),
    ("volume up", "system"),
    ("volume down", "system"),
    ("mute", "system"),
    ("unmute", "system"),
    ("take screenshot", "system"),
    ("take a screenshot", "system"),
    ("record screen", "system"),
    ("lock screen", "system"),
    ("unlock screen", "system"),
    ("restart", "system"),
    ("shut down", "system"),
    ("sleep mode", "system"),

    # Communication
    ("send email", "communication"),
    ("send an email", "communication"),
    ("send message", "communication"),
    ("send a message", "communication"),
    ("schedule meeting", "communication"),
    ("schedule a meeting", "communication"),
    ("set reminder", "communication"),
    ("set a reminder", "communication"),
    ("set alarm", "communication"),
    ("set timer", "communication"),
    ("call", "communication"),

    # File / data operations
    ("download", "file_ops"),
    ("upload", "file_ops"),
    ("save file", "file_ops"),
    ("create file", "file_ops"),
    ("move file", "file_ops"),
    ("delete file", "file_ops"),
    ("copy file", "file_ops"),
    ("rename file", "file_ops"),

    # Code operations (route to action for Ouroboros execution)
    ("run the tests", "code"),
    ("run tests", "code"),
    ("run the code", "code"),
    ("execute", "code"),
    ("deploy", "code"),
    ("build the project", "code"),
    ("start the server", "code"),
    ("stop the server", "code"),
]

# Single-word action verbs (only matched at start of command)
_ACTION_VERBS: List[Tuple[str, str]] = [
    ("open", "app_control"),
    ("launch", "app_control"),
    ("close", "app_control"),
    ("play", "app_control"),
    ("pause", "app_control"),
    ("stop", "app_control"),
    ("search", "browser"),
    ("find", "browser"),
    ("browse", "browser"),
    ("navigate", "browser"),
    ("download", "file_ops"),
    ("upload", "file_ops"),
    ("send", "communication"),
    ("call", "communication"),
    ("run", "code"),
    ("deploy", "code"),
    ("connect", "system"),
    ("disconnect", "system"),
    ("enable", "system"),
    ("disable", "system"),
    ("mute", "system"),
    ("unmute", "system"),
]

# Query signals — strongly indicate conversational/Q&A intent
_QUERY_PREFIXES: List[str] = [
    "what is",
    "what's",
    "what are",
    "what was",
    "what were",
    "tell me about",
    "tell me",
    "how does",
    "how do",
    "how is",
    "how are",
    "how can",
    "how would",
    "how many",
    "how much",
    "why is",
    "why does",
    "why did",
    "who is",
    "who was",
    "who are",
    "when is",
    "when was",
    "when did",
    "where is",
    "where was",
    "where are",
    "can you explain",
    "can you tell me",
    "could you explain",
    "please explain",
    "describe",
    "explain",
    "define",
    "summarize",
    "what do you think",
    "do you know",
    "is there",
    "are there",
    "have you",
]


# ---------------------------------------------------------------------------
# IntentClassifier
# ---------------------------------------------------------------------------

class IntentClassifier:
    """Deterministic intent classifier for voice commands.

    Zero LLM dependency. Pure pattern matching. Sub-millisecond.

    Classification hierarchy:
        1. Check QUERY prefixes first (explicit question patterns)
        2. Check multi-word ACTION phrases (high specificity)
        3. Check single-word ACTION verbs at command start (medium specificity)
        4. Default to QUERY (safe fallback — text response won't damage anything)

    Usage::

        classifier = IntentClassifier()
        result = classifier.classify("search YouTube for NBA highlights")
        assert result.intent == CommandIntent.ACTION
        assert result.action_category == "browser"
    """

    def __init__(self) -> None:
        self._action_phrases = _ACTION_PHRASES
        self._action_verbs = _ACTION_VERBS
        self._query_prefixes = _QUERY_PREFIXES

    def classify(self, command: str) -> ClassificationResult:
        """Classify a voice command as ACTION or QUERY.

        Returns ClassificationResult with intent, confidence, structured
        routing fields (provider, search_query, url, target_app).
        """
        if not command or not command.strip():
            return ClassificationResult(
                intent=CommandIntent.QUERY,
                confidence=0.0,
                matched_signal="empty_command",
                action_category="",
            )

        cmd = command.lower().strip()
        # Normalize whitespace
        cmd = re.sub(r'\s+', ' ', cmd)

        # --- Rule 1: Explicit query prefixes (highest priority) ---
        for prefix in self._query_prefixes:
            if cmd.startswith(prefix):
                return ClassificationResult(
                    intent=CommandIntent.QUERY,
                    confidence=0.95,
                    matched_signal=prefix,
                    action_category="",
                )

        # --- Rule 2: Multi-word action phrases (high specificity) ---
        for phrase, category in self._action_phrases:
            if phrase in cmd:
                fields = self._extract_structured_fields(cmd, category)
                return ClassificationResult(
                    intent=CommandIntent.ACTION,
                    confidence=0.90,
                    matched_signal=phrase,
                    action_category=category,
                    **fields,
                )

        # --- Rule 3: Action verb at start of command ---
        first_word = cmd.split()[0] if cmd.split() else ""
        for verb, category in self._action_verbs:
            if first_word == verb:
                fields = self._extract_structured_fields(cmd, category)
                return ClassificationResult(
                    intent=CommandIntent.ACTION,
                    confidence=0.75,
                    matched_signal=verb,
                    action_category=category,
                    **fields,
                )

        # --- Rule 4: Question mark → likely a query ---
        if cmd.endswith("?"):
            return ClassificationResult(
                intent=CommandIntent.QUERY,
                confidence=0.70,
                matched_signal="question_mark",
                action_category="",
            )

        # --- Default: QUERY (safe fallback) ---
        return ClassificationResult(
            intent=CommandIntent.QUERY,
            confidence=0.50,
            matched_signal="default_fallback",
            action_category="",
        )

    # --- Structured field extraction ---
    # The classifier identifies WHAT the user wants (provider + search_query).
    # It does NOT resolve HOW to get there (URLs) — that's the agent's job.

    # Provider detection: keyword → provider name (no URLs — agents resolve those)
    _PROVIDER_KEYWORDS: List[Tuple[str, str]] = [
        ("youtube", "youtube"),
        ("google", "google"),
        ("spotify", "spotify"),
        ("apple music", "apple_music"),
        ("linkedin", "linkedin"),
        ("twitter", "twitter"),
        ("reddit", "reddit"),
        ("github", "github"),
        ("amazon", "amazon"),
    ]

    # App detection: keyword → canonical app name
    _APP_KEYWORDS: List[Tuple[str, str]] = [
        ("apple music", "Apple Music"),
        ("spotify", "Spotify"),
        ("safari", "Safari"),
        ("chrome", "Google Chrome"),
        ("firefox", "Firefox"),
        ("finder", "Finder"),
        ("terminal", "Terminal"),
        ("slack", "Slack"),
        ("discord", "Discord"),
        ("messages", "Messages"),
        ("notes", "Notes"),
        ("mail", "Mail"),
        ("calendar", "Calendar"),
        ("settings", "System Preferences"),
    ]

    def _extract_structured_fields(
        self, cmd: str, category: str,
    ) -> dict:
        """Extract provider, search_query, target_app from command text.

        Emits semantic intent only — no URLs, no hardcoded paths.
        The executing agent (VisualBrowserAgent, etc.) decides HOW to reach
        the provider, including URL resolution via its own knowledge or J-Prime.
        """
        result: dict = {}

        # --- Provider + search_query ---
        if category == "browser":
            for keyword, provider in self._PROVIDER_KEYWORDS:
                if keyword in cmd:
                    result["provider"] = provider
                    search_term = self._isolate_search_term(cmd, keyword)
                    if search_term:
                        result["search_query"] = search_term
                    break

            # Explicit URL in the command (user said "go to https://...")
            url_match = re.search(r'https?://\S+', cmd)
            if url_match:
                result["url"] = url_match.group(0)

        # --- target_app ---
        if category == "app_control":
            for keyword, app_name in self._APP_KEYWORDS:
                if keyword in cmd:
                    result["target_app"] = app_name
                    break

        return result

    @staticmethod
    def _isolate_search_term(cmd: str, provider_keyword: str) -> str:
        """Extract just the search term from a command like 'search youtube for nba highlights'."""
        without_provider = cmd.replace(provider_keyword, " ")
        cleaned = re.sub(
            r'\b(search|find|look up|browse|go to|open|play|on|for|in|at|from|the|some|please)\b',
            ' ', without_provider,
        )
        return re.sub(r'\s+', ' ', cleaned).strip()

    # ------------------------------------------------------------------
    # Agentic classification (Pillar 5)
    # ------------------------------------------------------------------

    _CLASSIFY_SYSTEM_PROMPT = (
        "You are an intent classifier for a macOS desktop AI assistant. "
        "Given a voice command, classify it and extract structured fields.\n\n"
        "Respond ONLY with valid JSON, no markdown:\n"
        "{\n"
        '  "intent": "action" or "query",\n'
        '  "confidence": 0.0-1.0,\n'
        '  "action_category": "browser"|"app_control"|"system"|"communication"|"media"|"file"|"",\n'
        '  "provider": "youtube"|"google"|"spotify"|etc or "",\n'
        '  "search_query": "extracted search term" or "",\n'
        '  "target_app": "native macOS app name" or "",\n'
        '  "reasoning": "one-line explanation"\n'
        "}\n\n"
        "Rules:\n"
        '- "action" = the user wants to DO something (open, search, play, create, send, etc.)\n'
        '- "query" = the user wants to KNOW something (question, explanation, conversation)\n'
        "- Extract provider if a specific service is mentioned (youtube, google, spotify, etc.)\n"
        "- Extract search_query if the user is searching for something\n"
        "- Extract target_app if a native macOS app is mentioned\n"
        "- If uncertain, prefer query (safe — non-destructive)"
    )

    async def classify_async(self, command: str) -> ClassificationResult:
        """Agentic intent classification via J-Prime (Pillar 5).

        Falls back to reflex-arc classify() if the Mind is unavailable.
        """
        if not command or not command.strip():
            return self.classify(command)

        try:
            from backend.core.prime_router import get_prime_router
            router = await get_prime_router()

            response = await router.generate(
                prompt=f'Classify this voice command: "{command}"',
                system_prompt=self._CLASSIFY_SYSTEM_PROMPT,
                max_tokens=256,
                temperature=0.0,
                deadline=asyncio.get_event_loop().time() + 3.0,
            )

            result = json.loads(response.content)
            intent = CommandIntent.ACTION if result.get("intent") == "action" else CommandIntent.QUERY

            logger.info(
                "IntentClassifier.classify_async: '%s' → %s (confidence=%.2f, source=%s, latency=%.0fms)",
                command, intent.value, result.get("confidence", 0),
                response.source, response.latency_ms,
            )

            return ClassificationResult(
                intent=intent,
                confidence=float(result.get("confidence", 0.85)),
                matched_signal=f"ai:{response.source}",
                action_category=result.get("action_category", ""),
                provider=result.get("provider", ""),
                search_query=result.get("search_query", ""),
                target_app=result.get("target_app", ""),
            )

        except Exception as e:
            logger.warning(
                "IntentClassifier.classify_async failed, falling back to reflex: %s", e
            )
            return self.classify(command)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: IntentClassifier | None = None


def get_intent_classifier() -> IntentClassifier:
    """Get or create the singleton IntentClassifier."""
    global _instance
    if _instance is None:
        _instance = IntentClassifier()
    return _instance
