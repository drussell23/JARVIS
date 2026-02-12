"""
JARVIS Neural Mesh - Error Analyzer Agent

Intelligent error analysis, pattern recognition, and solution suggestion.
Uses semantic memory to find similar past errors and their solutions.
"""

from __future__ import annotations

import asyncio
import logging
import re
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..base.base_neural_mesh_agent import BaseNeuralMeshAgent
from ..data_models import AgentMessage, KnowledgeType, MessagePriority, MessageType

logger = logging.getLogger(__name__)


class ErrorAnalyzerAgent(BaseNeuralMeshAgent):
    """
    Error Analyzer Agent - Intelligent error analysis and solution finding.

    Capabilities:
    - analyze_error: Analyze an error and find solutions
    - find_similar: Find similar past errors
    - store_solution: Store a successful solution
    - get_error_patterns: Get common error patterns
    - suggest_fix: Suggest fixes based on past experiences
    """

    def __init__(self) -> None:
        super().__init__(
            agent_name="error_analyzer_agent",
            agent_type="intelligence",
            capabilities={
                "analyze_error",
                "find_similar",
                "store_solution",
                "get_error_patterns",
                "suggest_fix",
                "classify_error",
            },
            version="1.0.0",
        )

        self._error_patterns: Dict[str, int] = {}
        self._solutions_found: int = 0
        self._errors_analyzed: int = 0

    async def on_initialize(self) -> None:
        logger.info("Initializing ErrorAnalyzerAgent")

        # Subscribe to error messages
        await self.subscribe(
            MessageType.CUSTOM,
            self._handle_error_report,
        )

        logger.info("ErrorAnalyzerAgent initialized")

    async def on_start(self) -> None:
        logger.info("ErrorAnalyzerAgent started - ready for error analysis")

    async def on_stop(self) -> None:
        logger.info(
            f"ErrorAnalyzerAgent stopping - analyzed {self._errors_analyzed} errors, "
            f"found {self._solutions_found} solutions"
        )

    async def execute_task(self, payload: Dict[str, Any]) -> Any:
        action = payload.get("action", "")

        if action == "analyze_error":
            return await self._analyze_error(payload)
        elif action == "find_similar":
            return await self._find_similar_errors(payload)
        elif action == "store_solution":
            return await self._store_solution(payload)
        elif action == "get_error_patterns":
            return self._get_error_patterns()
        elif action == "suggest_fix":
            return await self._suggest_fix(payload)
        elif action == "classify_error":
            return self._classify_error(payload)
        else:
            raise ValueError(f"Unknown error analyzer action: {action}")

    async def _analyze_error(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze an error and find potential solutions."""
        error_message = payload.get("error_message", "")
        error_type = payload.get("error_type", "")
        stack_trace = payload.get("stack_trace", "")
        context = payload.get("context", {})

        self._errors_analyzed += 1

        # Parse error
        parsed = self._parse_error(error_message, error_type, stack_trace)

        # Classify error
        classification = self._classify_error({
            "error_message": error_message,
            "error_type": error_type,
        })

        # Track pattern
        pattern_key = f"{classification['category']}:{classification['subcategory']}"
        self._error_patterns[pattern_key] = self._error_patterns.get(pattern_key, 0) + 1

        # Find similar errors in memory
        similar = await self._find_similar_errors({
            "error_message": error_message,
            "error_type": error_type,
            "limit": 5,
        })

        # Generate suggestions
        suggestions = []
        if similar["results"]:
            for result in similar["results"]:
                data = result.get("data", {})
                if "solution" in data:
                    suggestions.append({
                        "solution": data["solution"],
                        "confidence": result.get("combined_score", 0.5),
                        "from_memory": result.get("memory_id"),
                    })
            self._solutions_found += len(suggestions)

        # Store this error for future reference
        if self.knowledge_graph:
            await self.add_knowledge(
                knowledge_type=KnowledgeType.ERROR,
                data={
                    "error_message": error_message,
                    "error_type": error_type,
                    "classification": classification,
                    "context": context,
                    "timestamp": datetime.now().isoformat(),
                },
                confidence=0.9,
            )

        return {
            "status": "analyzed",
            "parsed": parsed,
            "classification": classification,
            "similar_errors": similar["results"],
            "suggestions": suggestions,
            "pattern_count": self._error_patterns.get(pattern_key, 1),
        }

    def _parse_error(
        self,
        error_message: str,
        error_type: str,
        stack_trace: str,
    ) -> Dict[str, Any]:
        """Parse error details."""
        parsed = {
            "type": error_type or self._extract_error_type(error_message),
            "message": error_message,
            "locations": [],
            "variables": [],
        }

        # Extract file locations from stack trace
        if stack_trace:
            location_pattern = r'File "([^"]+)", line (\d+)'
            for match in re.finditer(location_pattern, stack_trace):
                parsed["locations"].append({
                    "file": match.group(1),
                    "line": int(match.group(2)),
                })

        # Extract variable names mentioned
        var_pattern = r"'(\w+)'"
        for match in re.finditer(var_pattern, error_message):
            parsed["variables"].append(match.group(1))

        return parsed

    def _extract_error_type(self, error_message: str) -> str:
        """Extract error type from message."""
        type_patterns = [
            r'^(\w+Error):',
            r'^(\w+Exception):',
            r'^(\w+Warning):',
        ]
        for pattern in type_patterns:
            match = re.match(pattern, error_message)
            if match:
                return match.group(1)
        return "UnknownError"

    def _classify_error(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Classify error into categories."""
        error_message = payload.get("error_message", "").lower()
        error_type = payload.get("error_type", "")

        # Classification logic
        categories = {
            "type_error": ("TypeError", ["type", "cannot", "expected"]),
            "attribute_error": ("AttributeError", ["attribute", "has no attribute"]),
            "import_error": ("ImportError", ["import", "module", "no module"]),
            "name_error": ("NameError", ["name", "not defined", "undefined"]),
            "value_error": ("ValueError", ["value", "invalid", "cannot convert"]),
            "key_error": ("KeyError", ["key", "not found"]),
            "index_error": ("IndexError", ["index", "out of range"]),
            "file_error": ("FileError", ["file", "no such file", "permission denied"]),
            "connection_error": ("ConnectionError", ["connection", "refused", "timeout"]),
            "syntax_error": ("SyntaxError", ["syntax", "invalid syntax"]),
        }

        category = "unknown"
        subcategory = "general"

        for cat, (type_name, keywords) in categories.items():
            if error_type == type_name or any(kw in error_message for kw in keywords):
                category = cat
                subcategory = self._get_subcategory(error_message, cat)
                break

        return {
            "category": category,
            "subcategory": subcategory,
            "severity": self._estimate_severity(category, error_message),
        }

    def _get_subcategory(self, error_message: str, category: str) -> str:
        """Get error subcategory."""
        subcategories = {
            "type_error": {
                "nonetype": "null_reference",
                "subscriptable": "indexing",
                "callable": "function_call",
            },
            "attribute_error": {
                "nonetype": "null_reference",
                "object": "missing_attribute",
            },
        }

        subs = subcategories.get(category, {})
        for keyword, subcat in subs.items():
            if keyword in error_message.lower():
                return subcat
        return "general"

    def _estimate_severity(self, category: str, error_message: str) -> str:
        """Estimate error severity."""
        critical_keywords = ["crash", "fatal", "critical", "corrupted", "data loss"]
        high_keywords = ["failed", "cannot", "unable"]

        if any(kw in error_message.lower() for kw in critical_keywords):
            return "critical"
        if any(kw in error_message.lower() for kw in high_keywords):
            return "high"
        if category in ["syntax_error", "import_error"]:
            return "high"
        return "medium"

    async def _find_similar_errors(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Find similar errors from memory."""
        error_message = payload.get("error_message", "")
        error_type = payload.get("error_type", "")
        limit = payload.get("limit", 5)

        # Build query
        query = f"{error_type} {error_message}"

        # Query knowledge graph
        results = []
        if self.knowledge_graph:
            entries = await self.query_knowledge(
                query=query,
                knowledge_types=[KnowledgeType.ERROR],
                limit=limit,
            )

            for entry in entries:
                results.append({
                    "memory_id": entry.id,
                    "content": entry.data.get("error_message", ""),
                    "data": entry.data,
                    "confidence": entry.confidence,
                    "combined_score": entry.confidence,
                })

        return {
            "status": "success",
            "query": query,
            "count": len(results),
            "results": results,
        }

    async def _store_solution(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Store a solution for an error."""
        error_message = payload.get("error_message", "")
        error_type = payload.get("error_type", "")
        solution = payload.get("solution", "")
        context = payload.get("context", {})

        if not solution:
            return {"status": "error", "error": "Solution required"}

        # Store in knowledge graph
        if self.knowledge_graph:
            entry = await self.add_knowledge(
                knowledge_type=KnowledgeType.SOLUTION,
                data={
                    "error_message": error_message,
                    "error_type": error_type,
                    "solution": solution,
                    "context": context,
                    "stored_at": datetime.now().isoformat(),
                },
                confidence=0.95,
            )

            return {
                "status": "stored",
                "knowledge_id": entry.id if entry else None,
            }

        return {"status": "error", "error": "Knowledge graph not available"}

    async def _suggest_fix(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Suggest fixes based on error analysis."""
        error_message = payload.get("error_message", "")
        error_type = payload.get("error_type", "")

        # Analyze first
        analysis = await self._analyze_error({
            "error_message": error_message,
            "error_type": error_type,
        })

        # Build suggestions
        suggestions = analysis.get("suggestions", [])

        # Add generic suggestions based on classification
        classification = analysis.get("classification", {})
        category = classification.get("category", "unknown")

        generic_suggestions = {
            "type_error": [
                "Check for None/null values before use",
                "Verify variable types match expected types",
                "Add type checking or validation",
            ],
            "attribute_error": [
                "Verify the object has the expected attribute",
                "Check if object is None before accessing attribute",
                "Review class definition for missing methods",
            ],
            "import_error": [
                "Verify package is installed",
                "Check import path and module name",
                "Ensure PYTHONPATH is correct",
            ],
            "key_error": [
                "Use .get() with default value",
                "Check if key exists before access",
                "Verify dictionary structure",
            ],
        }

        if category in generic_suggestions:
            for suggestion in generic_suggestions[category]:
                if not any(s.get("solution") == suggestion for s in suggestions):
                    suggestions.append({
                        "solution": suggestion,
                        "confidence": 0.6,
                        "type": "generic",
                    })

        return {
            "status": "success",
            "classification": classification,
            "suggestions": suggestions[:5],  # Top 5
        }

    def _get_error_patterns(self) -> Dict[str, Any]:
        """Get tracked error patterns."""
        sorted_patterns = sorted(
            self._error_patterns.items(),
            key=lambda x: x[1],
            reverse=True
        )

        return {
            "status": "success",
            "total_patterns": len(self._error_patterns),
            "patterns": [
                {"pattern": p, "count": c}
                for p, c in sorted_patterns[:20]
            ],
            "errors_analyzed": self._errors_analyzed,
            "solutions_found": self._solutions_found,
        }

    async def _handle_error_report(self, message: AgentMessage) -> None:
        """Handle error report messages."""
        if message.payload.get("type") == "error_report":
            result = await self._analyze_error({
                "error_message": message.payload.get("error_message", ""),
                "error_type": message.payload.get("error_type", ""),
                "stack_trace": message.payload.get("stack_trace", ""),
                "context": message.payload.get("context", {}),
            })

            # v238.0: Broadcast error analysis results
            if result and result.get("classification"):
                try:
                    classification = result["classification"]
                    await self.broadcast(
                        message_type=MessageType.ERROR_DETECTED,
                        payload={
                            "type": "error_analysis",
                            "error_type": classification.get("category", "unknown"),
                            "severity": classification.get("severity", "medium"),
                            "suggestions_count": len(result.get("suggestions", [])),
                            "pattern_count": result.get("pattern_count", 1),
                        },
                        priority=MessagePriority.HIGH,
                    )
                except Exception:
                    pass  # Best-effort broadcast
