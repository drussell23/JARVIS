"""
Contact Resolver — macOS Contacts integration via JXA.

Queries macOS Contacts.app for contact information (name, phone, email)
using JavaScript for Automation (JXA) subprocess calls.

Boundary Principle (Manifesto §4):
  Deterministic: JXA query, result parsing, caching.
  Agentic: None — pure data retrieval.

Note: First call triggers macOS Contacts permission prompt (one-time approval).
      JXA launches Contacts.app if not running (lightweight, no visible window).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CACHE_TTL_S = int(os.environ.get("JARVIS_CONTACTS_CACHE_TTL_S", "3600"))


@dataclass
class ContactInfo:
    """Resolved contact from macOS Contacts."""

    name: str
    phones: List[Dict[str, str]] = field(default_factory=list)
    emails: List[Dict[str, str]] = field(default_factory=list)
    confidence: float = 1.0
    resolution_time_ms: float = 0.0


@dataclass
class _CacheEntry:
    contact: Optional[ContactInfo]
    created_at: float  # time.monotonic()


class ContactResolver:
    """Query macOS Contacts for contact information.

    Uses JXA (JavaScript for Automation) via ``osascript -l JavaScript``
    for native JSON output.  Results are cached with configurable TTL.
    """

    _instance: Optional["ContactResolver"] = None

    def __new__(cls) -> "ContactResolver":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self._cache: Dict[str, _CacheEntry] = {}
        logger.info("[ContactResolver] initialized")

    async def resolve(self, name: str) -> Optional[ContactInfo]:
        """Resolve a contact by name from macOS Contacts.

        Returns the best-matching ContactInfo or None if not found.
        """
        if not name or not name.strip():
            return None

        normalized = name.strip().lower()

        # Cache hit
        if normalized in self._cache:
            entry = self._cache[normalized]
            if time.monotonic() - entry.created_at < _CACHE_TTL_S:
                return entry.contact

        t0 = time.monotonic()

        # Sanitize for JXA string literal (escape backslash then double-quote)
        safe_name = name.replace("\\", "\\\\").replace('"', '\\"')

        jxa_script = (
            "var app = Application('Contacts');\n"
            f'var people = app.people.whose({{name: {{_contains: "{safe_name}"}}}});\n'
            "var results = [];\n"
            "for (var i = 0; i < Math.min(people.length, 5); i++) {\n"
            "  var p = people[i];\n"
            "  var phones = [];\n"
            "  try { phones = p.phones().map(function(ph) {"
            " return {label: ph.label(), value: ph.value()}; }); } catch(e) {}\n"
            "  var emails = [];\n"
            "  try { emails = p.emails().map(function(em) {"
            " return {label: em.label(), value: em.value()}; }); } catch(e) {}\n"
            "  results.push({name: p.name(), phones: phones, emails: emails});\n"
            "}\n"
            "JSON.stringify(results);"
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "osascript",
                "-l",
                "JavaScript",
                "-e",
                jxa_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)

            if proc.returncode != 0:
                err = stderr.decode("utf-8", errors="replace").strip()
                logger.warning("[ContactResolver] JXA error: %s", err[:200])
                self._cache[normalized] = _CacheEntry(None, time.monotonic())
                return None

            raw = stdout.decode("utf-8", errors="replace").strip()
            if not raw:
                self._cache[normalized] = _CacheEntry(None, time.monotonic())
                return None

            contacts: List[Dict[str, Any]] = json.loads(raw)
            if not contacts:
                self._cache[normalized] = _CacheEntry(None, time.monotonic())
                return None

            best = contacts[0]
            elapsed_ms = (time.monotonic() - t0) * 1000

            result = ContactInfo(
                name=best.get("name", name),
                phones=best.get("phones", []),
                emails=best.get("emails", []),
                resolution_time_ms=elapsed_ms,
            )

            self._cache[normalized] = _CacheEntry(result, time.monotonic())
            logger.info(
                "[ContactResolver] '%s' -> '%s' (%d phones, %d emails) in %.0fms",
                name,
                result.name,
                len(result.phones),
                len(result.emails),
                elapsed_ms,
            )
            return result

        except asyncio.TimeoutError:
            logger.warning("[ContactResolver] Contacts query timed out for '%s'", name)
            return None
        except json.JSONDecodeError as exc:
            logger.warning("[ContactResolver] Failed to parse JXA output: %s", exc)
            return None
        except Exception as exc:
            logger.warning("[ContactResolver] Unexpected error: %s", exc)
            return None

    def clear_cache(self) -> None:
        """Flush the contact cache."""
        self._cache.clear()


def get_contact_resolver() -> ContactResolver:
    """Get the singleton ContactResolver instance."""
    return ContactResolver()
