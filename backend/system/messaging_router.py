"""
Messaging Router — Intelligent contact-to-app routing for JARVIS.

Routes "message X saying Y" commands to the correct messaging app
without requiring the user to specify which app.

Architecture (Symbiotic Manifesto §5 — Intelligence-Driven Routing):
  Tier 0 (Deterministic): Learned mappings → single-app installed
  Tier 1 (Agentic): Doubleword Qwen3.5-9B classification
  Fallback: Default to Messages (native macOS, safe)

Boundary Principle:
  Deterministic: Learned mapping lookup, single-app detection, app
                 discovery via AppLibrary, persistence.
  Agentic: Ambiguous routing via Doubleword 9B text model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — all env-driven (Manifesto §5)
# ---------------------------------------------------------------------------

_DW_API_KEY = os.environ.get("DOUBLEWORD_API_KEY", "")
_DW_BASE_URL = os.environ.get(
    "DOUBLEWORD_BASE_URL", "https://api.doubleword.ai/v1"
)
_DW_ROUTER_MODEL = os.environ.get("DOUBLEWORD_ROUTER_MODEL", "Qwen/Qwen3.5-9B")
_DW_TIMEOUT_S = float(os.environ.get("JARVIS_ROUTER_TIMEOUT_S", "5.0"))

# Messaging app taxonomy (deterministic — known category, Manifesto §5 Tier 0)
_DEFAULT_MESSAGING_APPS: List[str] = [
    "WhatsApp",
    "Messages",
    "Telegram",
    "Signal",
    "Slack",
    "Discord",
]

# Learned mappings persistence
_DATA_DIR = Path(
    os.environ.get("JARVIS_DATA_DIR", str(Path.home() / ".jarvis"))
)
_MAPPINGS_DIR = _DATA_DIR / "messaging_router"
_MAPPINGS_FILE = _MAPPINGS_DIR / "learned_mappings.json"


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class RoutingResult:
    """Result of messaging app routing."""

    app_name: str  # Resolved app name (e.g., "WhatsApp")
    contact_name: str  # Original contact name from voice
    confidence: float  # 0.0–1.0
    source: str  # "learned" | "single_app" | "doubleword" | "default"
    reasoning: str = ""
    routing_time_ms: float = 0.0


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class MessagingRouter:
    """Routes messaging commands to the correct app.

    Routing cascade (Manifesto §5):
      Tier 0a: Learned mappings                  (deterministic, <1ms)
      Tier 0b: Single messaging app installed     (deterministic, ~50ms)
      Tier 1:  Doubleword Qwen3.5-9B             (agentic, ~1–2s)
      Fallback: Default to Messages               (safe — native macOS)
    """

    _instance: Optional["MessagingRouter"] = None

    def __new__(cls) -> "MessagingRouter":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._learned: Dict[str, Dict[str, Any]] = {}
        self._installed_cache: Optional[List[str]] = None
        self._installed_cache_at: float = 0.0
        self._load_learned()
        logger.info(
            "[MessagingRouter] initialized (learned=%d mappings)", len(self._learned)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def route(
        self,
        contact_name: str,
        message_text: str = "",
    ) -> RoutingResult:
        """Resolve which messaging app to use for *contact_name*.

        Returns a RoutingResult with the resolved app, confidence, and
        reasoning.  The caller should rewrite the goal string and then
        hand off to the vision pipeline.
        """
        t0 = time.monotonic()
        normalized = contact_name.strip().lower()

        # --- Tier 0a: Learned mapping ---
        if normalized in self._learned:
            mapping = self._learned[normalized]
            count = mapping.get("success_count", 0)
            elapsed = (time.monotonic() - t0) * 1000
            logger.info(
                "[MessagingRouter] Learned: '%s' → '%s' (%dx)",
                contact_name,
                mapping["app"],
                count,
            )
            return RoutingResult(
                app_name=mapping["app"],
                contact_name=contact_name,
                confidence=min(0.95, 0.70 + count * 0.05),
                source="learned",
                reasoning=(
                    f"Previously messaged {contact_name} on "
                    f"{mapping['app']} ({count} times)"
                ),
                routing_time_ms=elapsed,
            )

        # --- Discover installed messaging apps ---
        installed = await self._discover_installed()

        if not installed:
            elapsed = (time.monotonic() - t0) * 1000
            logger.warning("[MessagingRouter] No messaging apps found")
            return RoutingResult(
                app_name="Messages",
                contact_name=contact_name,
                confidence=0.3,
                source="default",
                reasoning="No messaging apps detected — defaulting to Messages",
                routing_time_ms=elapsed,
            )

        # --- Tier 0b: Only one messaging app installed ---
        if len(installed) == 1:
            app = installed[0]
            elapsed = (time.monotonic() - t0) * 1000
            logger.info(
                "[MessagingRouter] Single app: '%s' → '%s'", contact_name, app
            )
            return RoutingResult(
                app_name=app,
                contact_name=contact_name,
                confidence=0.95,
                source="single_app",
                reasoning=f"Only messaging app installed: {app}",
                routing_time_ms=elapsed,
            )

        # --- Gather contact info for Doubleword context ---
        contact_context = await self._contact_context(contact_name)

        # --- Tier 1: Doubleword 9B ---
        if _DW_API_KEY:
            dw = await self._route_doubleword(
                contact_name, message_text, installed, contact_context
            )
            if dw is not None:
                dw.routing_time_ms = (time.monotonic() - t0) * 1000
                return dw

        # --- Fallback ---
        elapsed = (time.monotonic() - t0) * 1000
        logger.info("[MessagingRouter] Fallback: '%s' → Messages", contact_name)
        return RoutingResult(
            app_name="Messages",
            contact_name=contact_name,
            confidence=0.5,
            source="default",
            reasoning=(
                f"Multiple apps ({', '.join(installed)}) — "
                "defaulting to Messages"
            ),
            routing_time_ms=elapsed,
        )

    def learn(self, contact_name: str, app_name: str) -> None:
        """Record a successful message routing for future lookups."""
        normalized = contact_name.strip().lower()
        existing = self._learned.get(normalized)

        if existing and existing["app"] == app_name:
            existing["success_count"] = existing.get("success_count", 0) + 1
            existing["last_used"] = time.time()
        else:
            self._learned[normalized] = {
                "app": app_name,
                "success_count": 1,
                "last_used": time.time(),
                "display_name": contact_name,
            }

        self._persist_learned()
        logger.info("[MessagingRouter] Learned: '%s' → '%s'", contact_name, app_name)

    # ------------------------------------------------------------------
    # App discovery (via AppLibrary — Manifesto §2)
    # ------------------------------------------------------------------

    async def _discover_installed(self) -> List[str]:
        """Return installed messaging apps via AppLibrary (cached 5 min)."""
        if (
            self._installed_cache is not None
            and time.monotonic() - self._installed_cache_at < 300
        ):
            return self._installed_cache

        candidates = _messaging_app_list()
        installed: List[str] = []

        try:
            from backend.system.app_library import get_app_library

            lib = get_app_library()
            results = await asyncio.gather(
                *[
                    lib.resolve_app_name_async(n, include_running_status=False)
                    for n in candidates
                ],
                return_exceptions=True,
            )
            for name, res in zip(candidates, results):
                if isinstance(res, Exception):
                    continue
                if res.found:
                    installed.append(res.app_name or name)
        except ImportError:
            # Filesystem fallback
            for name in candidates:
                if Path(f"/Applications/{name}.app").exists():
                    installed.append(name)

        self._installed_cache = installed
        self._installed_cache_at = time.monotonic()
        logger.info("[MessagingRouter] Installed messaging apps: %s", installed)
        return installed

    # ------------------------------------------------------------------
    # Contact info (via ContactResolver)
    # ------------------------------------------------------------------

    async def _contact_context(self, name: str) -> Optional[Dict[str, Any]]:
        """Fetch lightweight contact context for the Doubleword prompt."""
        try:
            from backend.system.contact_resolver import get_contact_resolver

            contact = await get_contact_resolver().resolve(name)
            if contact:
                return {
                    "name": contact.name,
                    "has_phone": len(contact.phones) > 0,
                    "has_email": len(contact.emails) > 0,
                    "phone_count": len(contact.phones),
                    "email_count": len(contact.emails),
                }
        except Exception as exc:
            logger.debug("[MessagingRouter] Contact lookup failed: %s", exc)
        return None

    # ------------------------------------------------------------------
    # Doubleword 9B routing (Tier 1 — agentic)
    # ------------------------------------------------------------------

    _SYSTEM_PROMPT = (
        "You are a routing assistant for a macOS AI agent. "
        "Given a contact name, installed messaging apps, and optional context, "
        "decide which app to use.\n\n"
        "Considerations:\n"
        "- WhatsApp: common for international/casual messaging\n"
        "- Messages (iMessage): default for Apple-device contacts with phone numbers\n"
        "- Telegram: tech/privacy-conscious contacts\n"
        "- Slack/Discord: work or community contacts\n"
        "- If the contact has a phone number, WhatsApp and Messages are both viable\n"
        "- When uncertain, prefer the most popular general-purpose app in the list\n\n"
        "Respond with ONLY valid JSON (no markdown):\n"
        '{"app": "exact app name from the list", '
        '"confidence": 0.0-1.0, '
        '"reasoning": "one sentence"}'
    )

    async def _route_doubleword(
        self,
        contact_name: str,
        message_text: str,
        installed: List[str],
        contact_ctx: Optional[Dict[str, Any]],
    ) -> Optional[RoutingResult]:
        """Classify via Doubleword Qwen3.5-9B real-time completions."""
        try:
            import aiohttp
        except ImportError:
            logger.warning("[MessagingRouter] aiohttp unavailable")
            return None

        # Build prompt
        parts = [
            f"Contact: {contact_name}",
            f"Installed messaging apps: {', '.join(installed)}",
        ]
        if contact_ctx:
            parts.append(f"Contact info: {json.dumps(contact_ctx)}")
        if message_text:
            parts.append(f"Message preview: {message_text[:60]}")

        # Include learned patterns as context (up to 5 most recent)
        if self._learned:
            recent = sorted(
                self._learned.items(),
                key=lambda kv: kv[1].get("last_used", 0),
                reverse=True,
            )[:5]
            patterns = [
                f"{v.get('display_name', k)} → {v['app']}" for k, v in recent
            ]
            parts.append(f"User's messaging patterns: {'; '.join(patterns)}")

        payload = {
            "model": _DW_ROUTER_MODEL,
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(parts)},
            ],
            "max_tokens": 512,
            "temperature": 0.0,
        }

        url = f"{_DW_BASE_URL}/chat/completions"
        headers = {
            "Authorization": f"Bearer {_DW_API_KEY}",
            "Content-Type": "application/json",
        }

        try:
            timeout = aiohttp.ClientTimeout(total=_DW_TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning(
                            "[MessagingRouter] Doubleword %d: %s",
                            resp.status,
                            body[:200],
                        )
                        return None
                    data = await resp.json()

            choices = data.get("choices", [])
            if not choices:
                return None

            content = choices[0].get("message", {}).get("content", "")
            if not content:
                return None

            # Strip markdown fences if model wraps output
            text = content.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            result = json.loads(text)
            app = str(result.get("app", ""))

            # Validate against installed apps (fuzzy)
            matched_app = self._fuzzy_match_app(app, installed)
            if not matched_app:
                logger.warning(
                    "[MessagingRouter] Doubleword returned '%s' — not installed", app
                )
                return None

            logger.info(
                "[MessagingRouter] Doubleword: '%s' → '%s' (%.2f) — %s",
                contact_name,
                matched_app,
                result.get("confidence", 0),
                result.get("reasoning", ""),
            )
            return RoutingResult(
                app_name=matched_app,
                contact_name=contact_name,
                confidence=float(result.get("confidence", 0.7)),
                source="doubleword",
                reasoning=str(result.get("reasoning", "")),
            )

        except asyncio.TimeoutError:
            logger.warning(
                "[MessagingRouter] Doubleword timed out (%.1fs)", _DW_TIMEOUT_S
            )
            return None
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("[MessagingRouter] Doubleword parse error: %s", exc)
            return None
        except Exception as exc:
            logger.warning("[MessagingRouter] Doubleword error: %s", exc)
            return None

    @staticmethod
    def _fuzzy_match_app(candidate: str, installed: List[str]) -> Optional[str]:
        """Match Doubleword's answer to an actually-installed app name."""
        cl = candidate.lower().strip()
        for app in installed:
            if cl == app.lower():
                return app
        for app in installed:
            if cl in app.lower() or app.lower() in cl:
                return app
        return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_learned(self) -> None:
        if _MAPPINGS_FILE.exists():
            try:
                self._learned = json.loads(_MAPPINGS_FILE.read_text())
            except Exception as exc:
                logger.warning("[MessagingRouter] Load mappings failed: %s", exc)
                self._learned = {}
        else:
            self._learned = {}

    def _persist_learned(self) -> None:
        try:
            _MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
            _MAPPINGS_FILE.write_text(json.dumps(self._learned, indent=2))
        except Exception as exc:
            logger.warning("[MessagingRouter] Save mappings failed: %s", exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _messaging_app_list() -> List[str]:
    """Messaging app taxonomy — env-overridable."""
    raw = os.environ.get("JARVIS_MESSAGING_APPS", "").strip()
    if raw:
        return [n.strip() for n in raw.split(",") if n.strip()]
    return list(_DEFAULT_MESSAGING_APPS)


def get_messaging_router() -> MessagingRouter:
    """Get the singleton MessagingRouter instance."""
    return MessagingRouter()
