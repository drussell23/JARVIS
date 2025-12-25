"""
SOP Enforcement System for JARVIS - MetaGPT Inspired
=====================================================

Implements Standard Operating Procedures (SOPs) and action validation
based on MetaGPT's patterns for structured agent execution.

Features:
- ActionNode tree structure for hierarchical tasks
- BY_ORDER mode for forced sequential execution
- Pydantic-based validation for action outputs
- Message gating (only act on correct message type)
- Review and revision cycles
- Human-in-the-loop integration points

Author: JARVIS AI System
Version: 1.0.0
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import typing
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import (
    Any, Callable, Dict, Generic, List, Literal, Optional, Protocol,
    Set, Tuple, Type, TypeVar, Union, get_args, get_origin
)
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError, create_model, model_validator

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ============================================================================
# Configuration (Environment-Driven, No Hardcoding)
# ============================================================================

def _get_env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _get_env_int(key: str, default: int) -> int:
    try:
        return int(_get_env(key, str(default)))
    except ValueError:
        return default


def _get_env_bool(key: str, default: bool = False) -> bool:
    return _get_env(key, str(default)).lower() in ("true", "1", "yes")


@dataclass
class SOPConfig:
    """Configuration for SOP enforcement."""
    # Execution mode
    default_execution_mode: str = field(
        default_factory=lambda: _get_env("JARVIS_SOP_EXEC_MODE", "by_order")
    )

    # Validation settings
    strict_validation: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_STRICT_VALIDATION", True)
    )
    allow_partial_output: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_ALLOW_PARTIAL", False)
    )

    # Review settings
    auto_review: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_AUTO_REVIEW", True)
    )
    max_review_iterations: int = field(
        default_factory=lambda: _get_env_int("JARVIS_SOP_MAX_REVIEWS", 3)
    )

    # Retry settings
    max_retries: int = field(
        default_factory=lambda: _get_env_int("JARVIS_SOP_MAX_RETRIES", 3)
    )
    retry_delay_seconds: float = field(
        default_factory=lambda: float(_get_env("JARVIS_SOP_RETRY_DELAY", "1.0"))
    )

    # Human-in-the-loop
    require_human_approval: bool = field(
        default_factory=lambda: _get_env_bool("JARVIS_SOP_HUMAN_APPROVAL", False)
    )


# ============================================================================
# Enums
# ============================================================================

class ExecutionMode(str, Enum):
    """Execution mode for action sequences."""
    BY_ORDER = "by_order"       # Forced sequential execution
    PARALLEL = "parallel"       # Parallel execution where possible
    GRAPH = "graph"             # DAG-based execution with dependencies


class ReviewMode(str, Enum):
    """Review mode for action outputs."""
    HUMAN = "human"             # Human reviews output
    AUTO = "auto"               # Automated review
    NONE = "none"               # No review


class ReviseMode(str, Enum):
    """Revision mode for action outputs."""
    HUMAN = "human"             # Human revises
    HUMAN_REVIEW = "human_review"  # Human reviews, auto revises
    AUTO = "auto"               # Fully automated


class FillMode(str, Enum):
    """Fill mode for action nodes."""
    JSON_FILL = "json"          # JSON format output
    MARKDOWN_FILL = "markdown"  # Markdown format output
    XML_FILL = "xml"            # XML tag extraction
    CODE_FILL = "code"          # Code block extraction
    RAW_FILL = "raw"            # Raw text output


class ActionStatus(str, Enum):
    """Status of an action."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    REVIEW_NEEDED = "review_needed"
    APPROVED = "approved"


class MessageType(str, Enum):
    """Types of messages for message gating."""
    REQUIREMENT = "requirement"
    DESIGN = "design"
    CODE = "code"
    TEST = "test"
    REVIEW = "review"
    DOCUMENTATION = "documentation"
    DEPLOYMENT = "deployment"


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ActionResult:
    """Result of an action execution."""
    action_id: str
    status: ActionStatus
    output: Optional[Any] = None
    error: Optional[str] = None
    validation_errors: List[str] = field(default_factory=list)
    review_comments: Dict[str, str] = field(default_factory=dict)
    duration_ms: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)

    def is_success(self) -> bool:
        return self.status in (ActionStatus.COMPLETED, ActionStatus.APPROVED)


@dataclass
class ActionContext:
    """Context for action execution."""
    input_data: Dict[str, Any] = field(default_factory=dict)
    previous_results: Dict[str, ActionResult] = field(default_factory=dict)
    global_context: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# ActionNode (MetaGPT Pattern)
# ============================================================================

class ActionNode:
    """
    ActionNode - Tree structure for hierarchical actions.

    Based on MetaGPT's ActionNode pattern, providing:
    - Hierarchical task decomposition
    - Type-validated outputs via Pydantic
    - Templated prompts with examples
    - Review and revision cycles
    """

    def __init__(
        self,
        key: str,
        expected_type: Type,
        instruction: str,
        example: Any = None,
        content: str = "",
        children: Optional[Dict[str, "ActionNode"]] = None,
        schema: str = "json",
    ):
        self.key = key
        self.expected_type = expected_type
        self.instruction = instruction
        self.example = example
        self.content = content
        self.children = children or {}
        self.schema = schema

        # Graph connections for DAG execution
        self.prevs: List["ActionNode"] = []
        self.nexts: List["ActionNode"] = []

        # Execution state
        self.instruct_content: Optional[BaseModel] = None
        self.status = ActionStatus.PENDING
        self.llm: Optional[Any] = None
        self.context: str = ""

    def __str__(self) -> str:
        return f"ActionNode({self.key}, {self.expected_type.__name__}, status={self.status.value})"

    def __repr__(self) -> str:
        return self.__str__()

    # -------------------------------------------------------------------------
    # Tree Operations
    # -------------------------------------------------------------------------

    def add_child(self, node: "ActionNode") -> None:
        """Add a child node."""
        self.children[node.key] = node

    def add_children(self, nodes: List["ActionNode"]) -> None:
        """Add multiple child nodes."""
        for node in nodes:
            self.add_child(node)

    def get_child(self, key: str) -> Optional["ActionNode"]:
        """Get a child by key."""
        return self.children.get(key)

    def add_prev(self, node: "ActionNode") -> None:
        """Add a predecessor node (for DAG execution)."""
        self.prevs.append(node)

    def add_next(self, node: "ActionNode") -> None:
        """Add a successor node (for DAG execution)."""
        self.nexts.append(node)

    @classmethod
    def from_children(cls, key: str, nodes: List["ActionNode"]) -> "ActionNode":
        """Create a parent node from a list of children."""
        obj = cls(key=key, expected_type=str, instruction="", example="")
        obj.add_children(nodes)
        return obj

    @classmethod
    def from_pydantic(cls, model: Type[BaseModel], key: Optional[str] = None) -> "ActionNode":
        """Create an ActionNode tree from a Pydantic model."""
        key = key or model.__name__
        root = cls(key=key, expected_type=model, instruction="", example="")

        for field_name, field_info in model.model_fields.items():
            field_type = field_info.annotation
            description = field_info.description or ""
            default = field_info.default

            # Handle nested models
            if isinstance(field_type, type) and issubclass(field_type, BaseModel):
                child = cls.from_pydantic(field_type, key=field_name)
            else:
                child = cls(
                    key=field_name,
                    expected_type=field_type,
                    instruction=description,
                    example=default,
                )

            root.add_child(child)

        return root

    # -------------------------------------------------------------------------
    # Type Mapping
    # -------------------------------------------------------------------------

    def _get_children_mapping(self, exclude: Optional[List[str]] = None) -> Dict[str, Any]:
        """Get mapping of child keys to types."""
        exclude = exclude or []
        mapping = {}

        for key, child in self.children.items():
            if key in exclude:
                continue
            if child.children:
                mapping[key] = self._get_children_mapping()
            else:
                mapping[key] = (child.expected_type, Field(
                    default=child.example,
                    description=child.instruction,
                ))

        return mapping

    def get_mapping(self, mode: str = "auto", exclude: Optional[List[str]] = None) -> Dict[str, Any]:
        """Get type mapping for validation."""
        if mode == "children" or (mode == "auto" and self.children):
            return self._get_children_mapping(exclude)
        if exclude and self.key in exclude:
            return {}
        return {self.key: (self.expected_type, ...)}

    def create_model_class(self, class_name: Optional[str] = None, exclude: Optional[List[str]] = None) -> Type[BaseModel]:
        """Create a Pydantic model from this node's structure."""
        class_name = class_name or f"{self.key}_AN"
        mapping = self.get_mapping(exclude=exclude)

        # Create validator for required fields
        def check_fields(cls, values):
            required = set()
            for k, v in mapping.items():
                if isinstance(v, tuple):
                    type_v, field_info = v
                    if not _is_optional_type(type_v):
                        required.add(k)
            missing = required - set(values.keys())
            if missing:
                raise ValueError(f"Missing required fields: {missing}")
            return values

        validators = {"check_fields": model_validator(mode="before")(check_fields)}

        # Build field definitions
        fields = {}
        for field_name, field_value in mapping.items():
            if isinstance(field_value, dict):
                # Nested structure
                nested_name = f"{class_name}_{field_name}"
                nested_cls = self._create_nested_model(nested_name, field_value)
                fields[field_name] = (nested_cls, ...)
            else:
                fields[field_name] = field_value

        return create_model(class_name, __validators__=validators, **fields)

    def _create_nested_model(self, name: str, mapping: Dict[str, Any]) -> Type[BaseModel]:
        """Create a nested Pydantic model."""
        fields = {}
        for field_name, field_value in mapping.items():
            if isinstance(field_value, dict):
                nested = self._create_nested_model(f"{name}_{field_name}", field_value)
                fields[field_name] = (nested, ...)
            else:
                fields[field_name] = field_value
        return create_model(name, **fields)

    # -------------------------------------------------------------------------
    # Prompt Compilation
    # -------------------------------------------------------------------------

    def compile_instruction(self, schema: str = "markdown", mode: str = "children", exclude: Optional[List[str]] = None) -> str:
        """Compile instructions for the node."""
        nodes = self._to_dict(
            format_func=lambda n: f"{n.expected_type.__name__}  # {n.instruction}",
            mode=mode,
            exclude=exclude,
        )
        if schema == "json":
            return json.dumps(nodes, indent=2)
        elif schema == "markdown":
            return self._dict_to_markdown(nodes)
        return str(nodes)

    def compile_example(self, schema: str = "json", mode: str = "children", tag: str = "", exclude: Optional[List[str]] = None) -> str:
        """Compile example output for the node."""
        nodes = self._to_dict(
            format_func=lambda n: n.example,
            mode=mode,
            exclude=exclude,
        )
        text = json.dumps(nodes, indent=2) if schema == "json" else self._dict_to_markdown(nodes)
        if tag:
            return f"[{tag}]\n{text}\n[/{tag}]"
        return text

    def compile(
        self,
        context: str,
        schema: str = "json",
        mode: str = "children",
        exclude: Optional[List[str]] = None,
    ) -> str:
        """Compile the full prompt for this action."""
        instruction = self.compile_instruction(schema="markdown", mode=mode, exclude=exclude)
        example = self.compile_example(schema=schema, tag="CONTENT", mode=mode, exclude=exclude)

        return f"""## Context
{context}

-----

## Format Example
{example}

## Nodes: "<node>: <type>  # <instruction>"
{instruction}

## Constraint
Language: Please use the same language as Human INPUT.
Format: output wrapped inside [CONTENT][/CONTENT] like format example, nothing else.

## Action
Follow instructions of nodes, generate output and make sure it follows the format example.
"""

    def _to_dict(
        self,
        format_func: Optional[Callable[["ActionNode"], Any]] = None,
        mode: str = "auto",
        exclude: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Convert node tree to dictionary."""
        if format_func is None:
            format_func = lambda n: n.instruction

        exclude = exclude or []

        if (mode == "children" or mode == "auto") and self.children:
            result = {}
            for key, child in self.children.items():
                if key not in exclude:
                    result[key] = child._to_dict(format_func, mode, exclude)
            return result
        else:
            return format_func(self)

    def _dict_to_markdown(self, d: Dict[str, Any], prefix: str = "### ") -> str:
        """Convert dictionary to markdown format."""
        lines = []
        for key, value in d.items():
            if isinstance(value, dict):
                lines.append(f"{prefix}{key}")
                lines.append(self._dict_to_markdown(value, prefix + "#"))
            else:
                lines.append(f"{prefix}{key}\n{value}")
        return "\n".join(lines)

    # -------------------------------------------------------------------------
    # Execution
    # -------------------------------------------------------------------------

    async def fill(
        self,
        context: str,
        llm: Any,
        schema: str = "json",
        mode: str = "auto",
        exclude: Optional[List[str]] = None,
        images: Optional[List[str]] = None,
        timeout: int = 60,
    ) -> "ActionNode":
        """
        Fill this node by querying the LLM.

        Args:
            context: The context/requirements for this action
            llm: The LLM client with an aask method
            schema: Output schema (json/markdown/raw)
            mode: Fill mode (auto/children/root)
            exclude: Keys to exclude
            images: Optional images to include
            timeout: LLM timeout in seconds

        Returns:
            Self with filled content
        """
        self.llm = llm
        self.context = context
        self.status = ActionStatus.IN_PROGRESS

        try:
            if schema == "raw":
                prompt = f"{context}\n\n## Actions\n{self.instruction}"
                self.content = await llm.aask(prompt, timeout=timeout)
                self.instruct_content = None
            else:
                prompt = self.compile(context, schema, mode, exclude)
                content = await llm.aask(prompt, images=images, timeout=timeout)
                self.content = content

                # Parse the output
                mapping = self.get_mapping(mode=mode, exclude=exclude)
                output_class = self.create_model_class(exclude=exclude)
                parsed = self._extract_content(content, schema)

                self.instruct_content = output_class(**parsed)

            self.status = ActionStatus.COMPLETED
        except Exception as e:
            self.status = ActionStatus.FAILED
            logger.error(f"Failed to fill ActionNode {self.key}: {e}")
            raise

        return self

    def _extract_content(self, text: str, schema: str) -> Dict[str, Any]:
        """Extract structured content from LLM output."""
        # Look for [CONTENT]...[/CONTENT] tags
        match = re.search(r"\[CONTENT\](.*?)\[/CONTENT\]", text, re.DOTALL)
        if match:
            text = match.group(1).strip()

        if schema == "json":
            # Try to parse as JSON
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # Try to extract JSON from code blocks
                json_match = re.search(r"```json?\s*(.*?)\s*```", text, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(1))
                raise
        else:
            # Markdown parsing would go here
            return {"content": text}

    # -------------------------------------------------------------------------
    # Review and Revision
    # -------------------------------------------------------------------------

    async def review(self, mode: ReviewMode = ReviewMode.AUTO) -> Dict[str, str]:
        """
        Review the action output.

        Returns:
            Dictionary of field names to review comments
        """
        if mode == ReviewMode.NONE:
            return {}

        if mode == ReviewMode.HUMAN:
            return await self._human_review()
        else:
            return await self._auto_review()

    async def _auto_review(self) -> Dict[str, str]:
        """Automated review of action output."""
        if not self.instruct_content:
            return {}

        review_comments = {}
        content_dict = self.instruct_content.model_dump()

        for key, value in content_dict.items():
            child = self.get_child(key)
            if child:
                # Check if value meets the instruction
                if not value or str(value).strip() == "":
                    review_comments[key] = f"Field is empty but required: {child.instruction}"

        return review_comments

    async def _human_review(self) -> Dict[str, str]:
        """Human review - returns empty for now, would integrate with UI."""
        logger.info(f"Human review requested for {self.key}")
        return {}

    async def revise(
        self,
        review_comments: Dict[str, str],
        mode: ReviseMode = ReviseMode.AUTO,
    ) -> "ActionNode":
        """
        Revise the action output based on review comments.

        Args:
            review_comments: Comments from review
            mode: Revision mode

        Returns:
            Self with revised content
        """
        if not review_comments:
            return self

        if mode == ReviseMode.HUMAN:
            return await self._human_revise(review_comments)
        else:
            return await self._auto_revise(review_comments)

    async def _auto_revise(self, review_comments: Dict[str, str]) -> "ActionNode":
        """Automated revision based on review comments."""
        if not self.llm:
            raise RuntimeError("Cannot revise without LLM")

        # Build revision prompt
        nodes_output = {}
        content_dict = self.instruct_content.model_dump() if self.instruct_content else {}

        for key, comment in review_comments.items():
            if key in content_dict:
                nodes_output[key] = {
                    "value": content_dict[key],
                    "comment": comment,
                }

        prompt = f"""## Context
Revise the following output based on the review comments.

### Current Output
{json.dumps(nodes_output, indent=2)}

-----

## Action
Change the values to address the review comments. Output in JSON format.
"""

        revised_content = await self.llm.aask(prompt)
        parsed = self._extract_content(revised_content, "json")

        if self.instruct_content:
            updated = self.instruct_content.model_dump()
            updated.update(parsed)
            output_class = self.create_model_class()
            self.instruct_content = output_class(**updated)

        return self

    async def _human_revise(self, review_comments: Dict[str, str]) -> "ActionNode":
        """Human revision - would integrate with UI."""
        logger.info(f"Human revision requested for {self.key}: {review_comments}")
        return self

    # -------------------------------------------------------------------------
    # Output Access
    # -------------------------------------------------------------------------

    def get(self, key: str) -> Any:
        """Get a value from the instruction content."""
        if not self.instruct_content:
            return None
        return self.instruct_content.model_dump().get(key)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary."""
        return {
            "key": self.key,
            "status": self.status.value,
            "content": self.content,
            "instruct_content": self.instruct_content.model_dump() if self.instruct_content else None,
            "children": {k: v.to_dict() for k, v in self.children.items()},
        }


def _is_optional_type(tp: Type) -> bool:
    """Check if a type is Optional[T]."""
    origin = get_origin(tp)
    if origin is Union:
        args = get_args(tp)
        return type(None) in args
    return False


# ============================================================================
# SOP (Standard Operating Procedure)
# ============================================================================

@dataclass
class SOPStep:
    """A single step in an SOP."""
    name: str
    action: ActionNode
    required_message_types: List[MessageType] = field(default_factory=list)
    produces_message_type: Optional[MessageType] = None
    dependencies: List[str] = field(default_factory=list)
    optional: bool = False


class StandardOperatingProcedure:
    """
    Standard Operating Procedure - A sequence of actions with strict ordering.

    Implements MetaGPT's BY_ORDER pattern for guaranteed sequential execution.
    """

    def __init__(
        self,
        name: str,
        steps: List[SOPStep],
        config: Optional[SOPConfig] = None,
    ):
        self.name = name
        self.steps = {s.name: s for s in steps}
        self.step_order = [s.name for s in steps]
        self.config = config or SOPConfig()
        self.current_step_index = 0
        self.results: Dict[str, ActionResult] = {}
        self.context = ActionContext()

    async def execute(
        self,
        llm: Any,
        initial_context: str,
        mode: ExecutionMode = ExecutionMode.BY_ORDER,
    ) -> Dict[str, ActionResult]:
        """
        Execute the SOP.

        Args:
            llm: The LLM client
            initial_context: Initial context/requirements
            mode: Execution mode

        Returns:
            Dictionary of step names to results
        """
        self.context.input_data["initial_context"] = initial_context

        if mode == ExecutionMode.BY_ORDER:
            return await self._execute_by_order(llm)
        elif mode == ExecutionMode.PARALLEL:
            return await self._execute_parallel(llm)
        elif mode == ExecutionMode.GRAPH:
            return await self._execute_graph(llm)
        else:
            raise ValueError(f"Unknown execution mode: {mode}")

    async def _execute_by_order(self, llm: Any) -> Dict[str, ActionResult]:
        """Execute steps sequentially in strict order."""
        for step_name in self.step_order:
            step = self.steps[step_name]
            result = await self._execute_step(step, llm)
            self.results[step_name] = result

            if not result.is_success() and not step.optional:
                logger.error(f"SOP {self.name} failed at step {step_name}")
                break

        return self.results

    async def _execute_parallel(self, llm: Any) -> Dict[str, ActionResult]:
        """Execute independent steps in parallel."""
        # Build dependency graph
        completed = set()
        pending = set(self.step_order)

        while pending:
            # Find steps with satisfied dependencies
            ready = []
            for step_name in pending:
                step = self.steps[step_name]
                if all(dep in completed for dep in step.dependencies):
                    ready.append(step_name)

            if not ready:
                # Deadlock or unsatisfied dependencies
                logger.error(f"Cannot proceed: unsatisfied dependencies for {pending}")
                break

            # Execute ready steps in parallel
            tasks = [self._execute_step(self.steps[name], llm) for name in ready]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for name, result in zip(ready, results):
                if isinstance(result, Exception):
                    self.results[name] = ActionResult(
                        action_id=name,
                        status=ActionStatus.FAILED,
                        error=str(result),
                    )
                else:
                    self.results[name] = result

                pending.discard(name)
                if isinstance(result, ActionResult) and result.is_success():
                    completed.add(name)

        return self.results

    async def _execute_graph(self, llm: Any) -> Dict[str, ActionResult]:
        """Execute based on ActionNode graph (prevs/nexts)."""
        # Similar to parallel but uses ActionNode connections
        return await self._execute_parallel(llm)

    async def _execute_step(self, step: SOPStep, llm: Any) -> ActionResult:
        """Execute a single step with validation and review."""
        import time
        start_time = time.time()
        action_id = f"{self.name}:{step.name}:{uuid4().hex[:8]}"

        try:
            # Build context from previous results
            context = self._build_step_context(step)

            # Execute the action
            await step.action.fill(context, llm)

            # Validate output
            validation_errors = self._validate_output(step)

            # Review if configured
            review_comments = {}
            if self.config.auto_review and not validation_errors:
                review_comments = await step.action.review(ReviewMode.AUTO)

                if review_comments:
                    # Attempt revision
                    for i in range(self.config.max_review_iterations):
                        await step.action.revise(review_comments, ReviseMode.AUTO)
                        review_comments = await step.action.review(ReviewMode.AUTO)
                        if not review_comments:
                            break

            duration_ms = (time.time() - start_time) * 1000

            if validation_errors:
                return ActionResult(
                    action_id=action_id,
                    status=ActionStatus.FAILED,
                    output=step.action.instruct_content,
                    validation_errors=validation_errors,
                    duration_ms=duration_ms,
                )

            status = ActionStatus.REVIEW_NEEDED if review_comments else ActionStatus.COMPLETED
            return ActionResult(
                action_id=action_id,
                status=status,
                output=step.action.instruct_content.model_dump() if step.action.instruct_content else None,
                review_comments=review_comments,
                duration_ms=duration_ms,
            )

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            return ActionResult(
                action_id=action_id,
                status=ActionStatus.FAILED,
                error=str(e),
                duration_ms=duration_ms,
            )

    def _build_step_context(self, step: SOPStep) -> str:
        """Build context for a step from previous results."""
        parts = [self.context.input_data.get("initial_context", "")]

        for dep in step.dependencies:
            if dep in self.results and self.results[dep].is_success():
                parts.append(f"\n## Previous: {dep}\n{json.dumps(self.results[dep].output, indent=2)}")

        return "\n".join(parts)

    def _validate_output(self, step: SOPStep) -> List[str]:
        """Validate step output against its schema."""
        errors = []

        if not step.action.instruct_content and not self.config.allow_partial_output:
            errors.append(f"Step {step.name} produced no output")

        return errors


# ============================================================================
# Message Gating
# ============================================================================

class MessageGate:
    """
    Message gating - ensures roles only process appropriate message types.

    Based on MetaGPT's pattern where each role watches for specific message types
    and only acts when receiving the correct type.
    """

    def __init__(self):
        self._subscriptions: Dict[str, Set[MessageType]] = {}
        self._message_queue: asyncio.Queue = asyncio.Queue()

    def subscribe(self, role_id: str, message_types: List[MessageType]) -> None:
        """Subscribe a role to specific message types."""
        self._subscriptions[role_id] = set(message_types)

    def unsubscribe(self, role_id: str) -> None:
        """Remove a role's subscriptions."""
        self._subscriptions.pop(role_id, None)

    def should_process(self, role_id: str, message_type: MessageType) -> bool:
        """Check if a role should process a message type."""
        if role_id not in self._subscriptions:
            return True  # No restrictions if not subscribed
        return message_type in self._subscriptions[role_id]

    async def publish(self, message_type: MessageType, content: Any, sender: str = "") -> None:
        """Publish a message to the queue."""
        await self._message_queue.put({
            "type": message_type,
            "content": content,
            "sender": sender,
            "timestamp": datetime.now(),
        })

    async def receive(self, role_id: str, timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """Receive a message if it matches the role's subscriptions."""
        try:
            message = await asyncio.wait_for(
                self._message_queue.get(),
                timeout=timeout,
            )
            if self.should_process(role_id, message["type"]):
                return message
            else:
                # Put it back for others
                await self._message_queue.put(message)
                return None
        except asyncio.TimeoutError:
            return None


# ============================================================================
# Pre-built SOPs for JARVIS
# ============================================================================

def create_code_review_sop(config: Optional[SOPConfig] = None) -> StandardOperatingProcedure:
    """Create an SOP for code review workflow."""
    steps = [
        SOPStep(
            name="understand_code",
            action=ActionNode(
                key="understand",
                expected_type=str,
                instruction="Understand the code's purpose, architecture, and key components",
                example="This code implements...",
            ),
            produces_message_type=MessageType.REVIEW,
        ),
        SOPStep(
            name="security_check",
            action=ActionNode(
                key="security",
                expected_type=Dict[str, Any],
                instruction="Check for security vulnerabilities (OWASP Top 10)",
                example={"issues": [], "severity": "none"},
            ),
            dependencies=["understand_code"],
        ),
        SOPStep(
            name="quality_check",
            action=ActionNode(
                key="quality",
                expected_type=Dict[str, Any],
                instruction="Check code quality, patterns, and best practices",
                example={"issues": [], "suggestions": []},
            ),
            dependencies=["understand_code"],
        ),
        SOPStep(
            name="summarize_review",
            action=ActionNode(
                key="summary",
                expected_type=str,
                instruction="Summarize the review with actionable recommendations",
                example="Overall, the code is well-structured...",
            ),
            dependencies=["security_check", "quality_check"],
            produces_message_type=MessageType.REVIEW,
        ),
    ]

    return StandardOperatingProcedure("code_review", steps, config)


def create_feature_implementation_sop(config: Optional[SOPConfig] = None) -> StandardOperatingProcedure:
    """Create an SOP for feature implementation workflow."""
    steps = [
        SOPStep(
            name="analyze_requirements",
            action=ActionNode(
                key="requirements",
                expected_type=Dict[str, Any],
                instruction="Analyze and clarify the feature requirements",
                example={"requirements": [], "questions": [], "assumptions": []},
            ),
            required_message_types=[MessageType.REQUIREMENT],
            produces_message_type=MessageType.DESIGN,
        ),
        SOPStep(
            name="design_solution",
            action=ActionNode(
                key="design",
                expected_type=Dict[str, Any],
                instruction="Design the technical solution and architecture",
                example={"components": [], "data_flow": "", "api": []},
            ),
            dependencies=["analyze_requirements"],
            produces_message_type=MessageType.DESIGN,
        ),
        SOPStep(
            name="implement_code",
            action=ActionNode(
                key="implementation",
                expected_type=Dict[str, Any],
                instruction="Implement the feature code",
                example={"files": [], "code": {}},
            ),
            dependencies=["design_solution"],
            produces_message_type=MessageType.CODE,
        ),
        SOPStep(
            name="write_tests",
            action=ActionNode(
                key="tests",
                expected_type=Dict[str, Any],
                instruction="Write unit and integration tests",
                example={"test_files": [], "coverage": 0},
            ),
            dependencies=["implement_code"],
            produces_message_type=MessageType.TEST,
        ),
    ]

    return StandardOperatingProcedure("feature_implementation", steps, config)


# ============================================================================
# Singleton and Convenience Functions
# ============================================================================

_gate_instance: Optional[MessageGate] = None


def get_message_gate() -> MessageGate:
    """Get the singleton message gate."""
    global _gate_instance
    if _gate_instance is None:
        _gate_instance = MessageGate()
    return _gate_instance


# ============================================================================
# Module Exports
# ============================================================================

__all__ = [
    # Configuration
    "SOPConfig",

    # Enums
    "ExecutionMode",
    "ReviewMode",
    "ReviseMode",
    "FillMode",
    "ActionStatus",
    "MessageType",

    # Data Classes
    "ActionResult",
    "ActionContext",
    "SOPStep",

    # Core Classes
    "ActionNode",
    "StandardOperatingProcedure",
    "MessageGate",

    # Pre-built SOPs
    "create_code_review_sop",
    "create_feature_implementation_sop",

    # Convenience Functions
    "get_message_gate",
]
