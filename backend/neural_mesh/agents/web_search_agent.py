"""
JARVIS Neural Mesh - Web Search Agent

Provides structured internet research capabilities:
- Search live web results
- Read pages safely
- Synthesize reports with source attribution
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import AgentMessage, KnowledgeType, MessagePriority, MessageType

from backend.intelligence.web_research_service import get_web_research_service

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
        return max(value, minimum)
    except ValueError:
        return default


class WebSearchAgent(BaseNeuralMeshAgent):
    """Neural Mesh agent for internet research and synthesis."""

    def __init__(self) -> None:
        super().__init__(
            agent_name="web_search_agent",
            agent_type="intelligence",
            capabilities={
                "web_search",
                "web_research",
                "read_web_page",
                "source_synthesis",
                "report_generation",
                "current_information_lookup",
            },
            version="1.0.0",
            dependencies={"context_tracker_agent"},
            description="Performs live internet research and structured synthesis",
        )
        self._service = get_web_research_service()
        self._history: List[Dict[str, Any]] = []
        self._history_limit = _env_int("JARVIS_WEBSEARCH_AGENT_HISTORY_LIMIT", 200, minimum=20)

    async def on_initialize(self, **kwargs) -> None:
        await self._service.initialize()

        # Optional subscription for cross-agent request/response flows.
        if self.message_bus is not None:
            await self.subscribe(MessageType.CUSTOM, self._handle_custom_request)

        logger.info("WebSearchAgent initialized")

    async def on_start(self) -> None:
        logger.info("WebSearchAgent started")

    async def on_stop(self) -> None:
        logger.info("WebSearchAgent stopping (history=%d)", len(self._history))

    @staticmethod
    def _query_of(payload: Dict[str, Any]) -> str:
        """The research query: explicit, else the classifier's extraction, else the goal."""
        for key in ("query", "topic", "search_query", "goal", "command"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        return ""

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        action = str(payload.get("action", "")).strip().lower()
        # Generic dispatchers (the core-context facade, the task orchestrator's
        # TaskEnvelope) know the user's GOAL, not this agent's verbs: they send
        # "execute" or no action at all, and every such request used to raise
        # here. A goal for a research agent means: find out and answer.
        if action in ("", "execute", "answer", "web_answer"):
            if not self._query_of(payload):
                raise ValueError("Missing required action or goal")
            return await self._answer(payload)

        if action in {"search", "web_search", "search_web"}:
            query = str(payload.get("query", payload.get("topic", ""))).strip()
            max_results = int(payload.get("max_results", 8))
            include_types = self._parse_type_filter(payload.get("include_types"))
            results = await self._service.search(query=query, max_results=max_results, include_types=include_types)
            response = {
                "status": "success",
                "action": "web_search",
                "query": query,
                "count": len(results),
                "results": results,
            }
            await self._record_history(response)
            await self._store_knowledge(query=query, report=response, knowledge_type=KnowledgeType.OBSERVATION)
            return response

        if action in {"read", "read_page", "web_read", "read_web_page"}:
            url = str(payload.get("url", "")).strip()
            max_chars = int(payload.get("max_chars", 12000))
            page = await self._service.read_page(url=url, max_chars=max_chars)
            response = {
                "status": "success",
                "action": "read_page",
                "result": page,
            }
            await self._record_history(response)
            return response

        if action in {"research", "web_research", "research_topic"}:
            query = str(payload.get("query", payload.get("topic", ""))).strip()
            max_results = int(payload.get("max_results", 8))
            max_sources = int(payload.get("max_sources", 5))
            include_types = self._parse_type_filter(payload.get("include_types"))
            report = await self._service.research(
                query=query,
                max_results=max_results,
                max_sources=max_sources,
                include_types=include_types,
            )
            response = {
                "status": "success",
                "action": "web_research",
                "report": report,
            }
            await self._record_history(response)
            await self._store_knowledge(query=query, report=report, knowledge_type=KnowledgeType.FACT)
            return response

        if action in {"report", "research_report", "research_markdown"}:
            query = str(payload.get("query", payload.get("topic", ""))).strip()
            report = await self._service.research(
                query=query,
                max_results=int(payload.get("max_results", 8)),
                max_sources=int(payload.get("max_sources", 5)),
                include_types=self._parse_type_filter(payload.get("include_types")),
            )
            return {
                "status": "success",
                "action": "research_report",
                "query": query,
                "markdown": report.get("markdown_report", ""),
                "report": report,
            }

        if action in {"service_health", "health"}:
            return {
                "status": "success",
                "action": "service_health",
                "health": self._service.get_health(),
            }

        if action in {"service_metrics", "metrics"}:
            return {
                "status": "success",
                "action": "service_metrics",
                "metrics": self._service.get_metrics(),
            }

        raise ValueError(f"Unknown web search action: {action}")

    async def _answer(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Research the goal on the live web and answer it from the sources read.

        The answer is composed by the Mind (J-Prime) grounded ONLY in the
        sources, with [n] citations. When the Mind is unreachable the
        research service's own extractive summary is the answer.
        """
        goal = str(payload.get("goal") or payload.get("command") or "").strip()
        query = self._query_of(payload)
        try:
            report = await self._service.research(
                query=query,
                max_results=int(payload.get("max_results", _env_int("JARVIS_WEB_ANSWER_MAX_RESULTS", 8, 1))),
                max_sources=int(payload.get("max_sources", _env_int("JARVIS_WEB_ANSWER_MAX_SOURCES", 5, 1))),
                include_types=self._parse_type_filter(payload.get("include_types")),
            )
        except Exception as exc:  # noqa: BLE001 -- offline, blocked, provider down
            logger.warning("[WebSearchAgent] research failed for %r: %s", query, exc)
            return {"status": "error", "success": False, "action": "web_answer",
                    "result": f"I couldn't search the web just now ({type(exc).__name__}: {exc})."}

        found = [s for s in report.get("sources", []) if s.get("url")]
        # Ground (and cite) only pages that were actually read: a page that
        # blocked the reader (a bot wall) has nothing to say about the query.
        # Unread search hits are the fallback when nothing could be read.
        sources = [s for s in found if s.get("read_success")] or found
        if not sources:
            return {"status": "success", "success": True, "action": "web_answer",
                    "result": f"I searched the web for \"{query}\" but found no usable sources.",
                    "report": report, "sources": []}

        answer = await self._compose_answer(goal or query, report, sources)
        listed = "\n".join(f"[{i}] {s.get('title') or s.get('domain') or s['url']} - {s['url']}"
                           for i, s in enumerate(sources, 1))
        response = {
            "status": "success",
            "success": True,
            "action": "web_answer",
            "query": query,
            "result": f"{answer}\n\nSources:\n{listed}",
            "answer": answer,
            "sources": [{"title": s.get("title", ""), "url": s["url"]} for s in sources],
            "report": report,
        }
        await self._record_history(response)
        await self._store_knowledge(query=query, report=report, knowledge_type=KnowledgeType.FACT)
        return response

    async def _compose_answer(self, goal: str, report: Dict[str, Any],
                              sources: Sequence[Dict[str, Any]]) -> str:
        budget = _env_int("JARVIS_WEB_ANSWER_CONTEXT_CHARS", 6000, minimum=1000)
        blocks: List[str] = []
        for i, src in enumerate(sources, 1):
            text = (src.get("content_excerpt") or src.get("snippet") or "").strip()
            blocks.append(f"[{i}] {src.get('title', '')} ({src['url']})\n{text}")
        points = "\n".join(f"- {p}" for p in report.get("key_points", []) if isinstance(p, str))
        context = ("\n\n".join(blocks) + (f"\n\nKey points:\n{points}" if points else ""))[:budget]
        try:
            from backend.core.prime_client import get_prime_client

            client = await get_prime_client()
            response = await asyncio.wait_for(
                client.generate(
                    prompt=f"Question: {goal}\n\nSources:\n{context}",
                    system_prompt=(
                        f"Today is {datetime.now().strftime('%A, %B %d, %Y')}. "
                        "Answer the question using ONLY the numbered sources. Cite them "
                        "inline as [n]. Be concise (a short paragraph). Questions about "
                        "\"this year\", \"latest\" or \"now\" mean relative to today: if the "
                        "sources only cover an earlier period, say so plainly instead of "
                        "presenting old information as current. If the sources do not "
                        "answer it, say what they do show."
                    ),
                    max_tokens=_env_int("JARVIS_WEB_ANSWER_MAX_TOKENS", 400, minimum=64),
                    temperature=0.2,
                ),
                timeout=float(os.getenv("JARVIS_WEB_ANSWER_TIMEOUT_S", "45")),
            )
            text = (getattr(response, "content", "") or "").strip()
            if text and getattr(response, "source", "") != "degraded":
                return text
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- the extractive summary still answers
            logger.info("[WebSearchAgent] Mind unavailable for synthesis (%s); extractive summary", exc)
        return str(report.get("summary") or "").strip() or "Here is what I found."

    async def _handle_custom_request(self, message: AgentMessage) -> None:
        msg_type = str(message.payload.get("type", "")).strip().lower()
        if msg_type not in {"web_search_request", "web_research_request"}:
            return

        try:
            result = await self.execute_task(message.payload)
        except Exception as exc:
            result = {"status": "error", "error": str(exc)}

        if not self.message_bus:
            return
        if not message.from_agent or message.from_agent == self.agent_name:
            return

        await self.publish(
            to_agent=message.from_agent,
            message_type=MessageType.CUSTOM,
            payload={
                "type": "web_research_response",
                "request_id": message.message_id,
                "result": result,
            },
            priority=MessagePriority.NORMAL,
        )

    async def _record_history(self, record: Dict[str, Any]) -> None:
        self._history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "record": record,
        })
        if len(self._history) > self._history_limit:
            self._history = self._history[-self._history_limit:]

    async def _store_knowledge(
        self,
        query: str,
        report: Dict[str, Any],
        knowledge_type: KnowledgeType,
    ) -> None:
        if not self.knowledge_graph:
            return
        try:
            await self.add_knowledge(
                knowledge_type=knowledge_type,
                data={
                    "query": query,
                    "summary": report.get("summary") or report.get("report", {}).get("summary", ""),
                    "sources": report.get("sources") or report.get("report", {}).get("sources", []),
                },
                confidence=0.75,
                tags={"web_research", "live_information"},
            )
        except Exception as exc:
            logger.debug("WebSearchAgent knowledge write skipped: %s", exc)

    @staticmethod
    def _parse_type_filter(raw: Optional[Any]) -> Optional[Sequence[str]]:
        if raw is None:
            return None
        if isinstance(raw, (list, tuple, set)):
            values = [str(item).strip() for item in raw if str(item).strip()]
            return values or None
        values = [token.strip() for token in str(raw).split(",") if token.strip()]
        return values or None

