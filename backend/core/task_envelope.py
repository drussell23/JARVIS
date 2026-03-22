"""TaskEnvelope — structured payload for agent dispatch.

Every agent's ``execute_task()`` receives a ``TaskEnvelope``.  No more
free-form dicts with ad-hoc keys; no goal-string scraping in the orchestrator.

The intent classifier and planner emit structured fields; the orchestrator
forwards them verbatim.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class TaskEnvelope:
    """Normalized payload passed to every agent's ``execute_task()``."""

    goal: str                         # natural-language goal ("search youtube for nba")
    provider: str = ""                # "youtube", "google", "spotify", etc.
    url: str = ""                     # pre-resolved URL if available
    search_query: str = ""            # extracted search term (not the full goal)
    app_name: str = ""                # target native application
    workspace_service: str = ""       # "email", "calendar", etc.
    action_category: str = ""         # from IntentClassifier: "browser", "app_control", ...
    source: str = ""                  # "voice_command", "text_input", "automation"
    client_id: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for agents that still accept dict payloads."""
        d: Dict[str, Any] = {"goal": self.goal}
        if self.url:
            d["url"] = self.url
        if self.search_query:
            d["search_query"] = self.search_query
        if self.app_name:
            d["app_name"] = self.app_name
        if self.workspace_service:
            d["service"] = self.workspace_service
        if self.provider:
            d["provider"] = self.provider
        if self.extra:
            d.update(self.extra)
        return d

    @classmethod
    def from_step(
        cls,
        step: Dict[str, Any],
        action_category: str = "",
        source: str = "",
        client_id: str = "",
    ) -> TaskEnvelope:
        """Build an envelope from a planner step dict + classification context."""
        return cls(
            goal=step.get("goal", ""),
            provider=step.get("provider", ""),
            url=step.get("url", ""),
            search_query=step.get("search_query", ""),
            app_name=step.get("target_app", ""),
            workspace_service=step.get("workspace_service", ""),
            action_category=action_category,
            source=source,
            client_id=client_id,
            extra={k: v for k, v in step.items()
                   if k not in ("goal", "provider", "url", "search_query",
                                "target_app", "workspace_service",
                                "priority", "dependencies", "category")},
        )
