"""IntentClassifier — deterministic voice command routing for the E1 bridge.

Rules-based, zero LLM dependency, sub-millisecond classification.
Determines if a voice command should route to:
    ACTION → RuntimeTaskOrchestrator (Neural Mesh agents, browser, apps, tools)
    QUERY  → HybridOrchestrator (J-Prime text Q&A, conversation, explanation)

Design constraints:
    - Deterministic: same input → same output, every time
    - Fast: pure string matching, no model inference
    - Extensible: add patterns without modifying logic
    - Safe default: unknown commands route to QUERY (non-destructive)

Architecture:
    Voice input → IntentClassifier.classify(text) → CommandIntent
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

import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Tuple


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

    # --- Structured field extraction (no heuristics in the orchestrator) ---

    # Provider patterns: keyword in goal → (provider, url_template)
    _PROVIDER_PATTERNS: List[Tuple[str, str, str]] = [
        ("youtube", "youtube", "https://www.youtube.com/results?search_query={q}"),
        ("google", "google", "https://www.google.com/search?q={q}"),
        ("spotify", "spotify", ""),
        ("apple music", "apple_music", ""),
        ("linkedin", "linkedin", "https://www.linkedin.com/search/results/all/?keywords={q}"),
        ("twitter", "twitter", "https://twitter.com/search?q={q}"),
        ("reddit", "reddit", "https://www.reddit.com/search/?q={q}"),
        ("github", "github", "https://github.com/search?q={q}"),
        ("amazon", "amazon", "https://www.amazon.com/s?k={q}"),
    ]

    # App patterns: keyword in goal → target_app
    _APP_PATTERNS: List[Tuple[str, str]] = [
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
        """Extract provider, search_query, url, target_app from command text.

        Called once per classification; results are frozen into ClassificationResult
        so the orchestrator never parses goal strings.
        """
        from urllib.parse import quote_plus

        result: dict = {}

        # --- Provider + search_query + url ---
        if category == "browser":
            for keyword, provider, url_template in self._PROVIDER_PATTERNS:
                if keyword in cmd:
                    result["provider"] = provider
                    search_term = self._isolate_search_term(cmd, keyword)
                    if search_term:
                        result["search_query"] = search_term
                        if url_template:
                            result["url"] = url_template.replace("{q}", quote_plus(search_term))
                    elif url_template:
                        # No search term → provider home page
                        base = url_template.split("/results")[0].split("/search")[0]
                        result["url"] = base
                    break

            # Explicit URL in the command
            if not result.get("url"):
                url_match = re.search(r'https?://\S+', cmd)
                if url_match:
                    result["url"] = url_match.group(0)

        # --- target_app ---
        if category == "app_control":
            for keyword, app_name in self._APP_PATTERNS:
                if keyword in cmd:
                    result["target_app"] = app_name
                    break

        return result

    @staticmethod
    def _isolate_search_term(cmd: str, provider_keyword: str) -> str:
        """Extract just the search term from a command like 'search youtube for nba highlights'."""
        # Remove the provider keyword
        without_provider = cmd.replace(provider_keyword, " ")
        # Remove command verbs and prepositions
        cleaned = re.sub(
            r'\b(search|find|look up|browse|go to|open|play|on|for|in|at|from|the|some|please)\b',
            ' ', without_provider,
        )
        # Collapse whitespace and strip
        return re.sub(r'\s+', ' ', cleaned).strip()


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
