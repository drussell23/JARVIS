"""MCP server — expose O+V's internal surface to external MCP clients.

**Scope honesty**: this is the MCP *tools* subset only — the methods
an MCP client (Claude Code, custom agents) actually invokes to drive
O+V: ``initialize``, ``tools/list``, ``tools/call``. The full MCP spec
also covers ``resources``, ``prompts``, ``logging``, ``sampling``,
capability negotiation details, and progress notifications — those are
deferred to V1.1 (or a later migration to the official ``mcp`` Python
SDK once it supports the Python version O+V targets).

V1 exposes 8 read-only tools + 1 opt-in mutation tool:

  * ``list_orphaned_ops``     — orphan ops from the ledger (same data as /resume list)
  * ``query_oracle``          — semantic index search
  * ``risk_classify``         — run the risk engine on a synthetic signal
  * ``session_status``        — cost / idle / active-op snapshot
  * ``list_sensors``          — registered sensor fleet (names + poll intervals)
  * ``list_memories``         — UserPreferenceMemory list_all()
  * ``search_memories``       — full-text memory search
  * ``preview_candidate``     — SemanticGuardian dry-run on (old, new) pairs
  * ``submit_intent``         — **mutation**, gated by JARVIS_MCP_ALLOW_MUTATIONS

Transport: JSON-RPC 2.0 over stdio. Newline-delimited messages each
containing exactly one JSON object. Matches the on-the-wire format
MCP clients speak — the protocol framing layer below is exactly what
``mcp.server.stdio.stdio_server()`` in the official SDK provides, just
without the SDK dependency.

Env gates (all default OFF — opt-in, fail-closed):

    JARVIS_MCP_SERVER_ENABLED=1
        Master switch. The server exits cleanly when disabled so
        operators can leave launch scripts in place without risk.

    JARVIS_MCP_ALLOW_MUTATIONS=1
        Required for ``submit_intent`` to be registered. Without this,
        ``tools/list`` doesn't even advertise the tool — remote clients
        can't submit work by accident.

    JARVIS_MCP_TOOL_PREFIX=ov_   (default)
        Prefix applied to every exposed tool name so MCP clients
        aggregating multiple servers can distinguish O+V tools from
        other sources.

Authority invariant: the server invokes existing O+V internals
(ResumeScanner, risk_engine, SemanticGuardian, UserPreferenceStore,
etc.) — it does NOT bypass any of them. The submit_intent path routes
through the normal intake router, so submitted intents go through
CLASSIFY → every gate the orchestrator already enforces.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("Ouroboros.MCPServer")

_ENV_ENABLED = "JARVIS_MCP_SERVER_ENABLED"
_ENV_MUTATIONS = "JARVIS_MCP_ALLOW_MUTATIONS"
_ENV_PREFIX = "JARVIS_MCP_TOOL_PREFIX"

_TRUTHY = frozenset({"1", "true", "yes", "on"})

_PROTOCOL_VERSION = "2024-11-05"      # MCP spec revision V1 targets
_SERVER_NAME = "ouroboros"
_SERVER_VERSION = "1.0.0"


def server_enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "0").strip().lower() in _TRUTHY


def mutations_enabled() -> bool:
    return os.environ.get(_ENV_MUTATIONS, "0").strip().lower() in _TRUTHY


def tool_prefix() -> str:
    return os.environ.get(_ENV_PREFIX, "ov_").strip() or "ov_"


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tool:
    """One registered MCP tool."""

    name: str                           # bare name (prefix applied on list)
    description: str
    input_schema: Dict[str, Any]        # JSON Schema (draft-07)
    handler: Callable[..., Awaitable[Any]]
    mutating: bool = False              # True → gated by JARVIS_MCP_ALLOW_MUTATIONS


@dataclass
class ToolRegistry:
    """In-memory registry. Built once at server boot."""

    tools: Dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.mutating and not mutations_enabled():
            logger.info(
                "[MCPServer] skipped mutating tool %s (mutations disabled)",
                tool.name,
            )
            return
        self.tools[tool.name] = tool

    def list_schemas(self, *, prefix: str) -> List[Dict[str, Any]]:
        out = []
        for t in self.tools.values():
            out.append({
                "name": f"{prefix}{t.name}",
                "description": t.description,
                "inputSchema": t.input_schema,
            })
        return out

    def lookup(self, *, prefix: str, qualified_name: str) -> Optional[Tool]:
        if not qualified_name.startswith(prefix):
            return None
        bare = qualified_name[len(prefix):]
        return self.tools.get(bare)


# ---------------------------------------------------------------------------
# JSON-RPC 2.0 — stdio transport
# ---------------------------------------------------------------------------


class _JsonRpcError(Exception):
    """Raised during request handling to produce a JSON-RPC error response."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _rpc_error(*, req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err: Dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _rpc_result(*, req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


async def _handle_request(
    *,
    registry: ToolRegistry,
    msg: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Route a single JSON-RPC request to its handler.

    Returns the response object (to be serialized + written to stdout).
    Notifications (no ``id`` field) return None — caller suppresses them.
    Never raises — every error becomes a proper JSON-RPC error response.
    """
    method = str(msg.get("method", ""))
    req_id = msg.get("id")
    params = msg.get("params") or {}
    is_notification = "id" not in msg

    # ---- initialize ------------------------------------------------------
    if method == "initialize":
        client_info = params.get("clientInfo") or {}
        logger.info(
            "[MCPServer] initialize from client=%s version=%s",
            client_info.get("name", "?"),
            client_info.get("version", "?"),
        )
        return _rpc_result(req_id=req_id, result={
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {
                # Tools is the only capability we implement in V1.
                "tools": {},
            },
            "serverInfo": {
                "name": _SERVER_NAME,
                "version": _SERVER_VERSION,
            },
        })

    # MCP clients send this after initialize completes. Pure notification.
    if method == "notifications/initialized":
        return None

    # ---- tools/list ------------------------------------------------------
    if method == "tools/list":
        schemas = registry.list_schemas(prefix=tool_prefix())
        return _rpc_result(req_id=req_id, result={"tools": schemas})

    # ---- tools/call ------------------------------------------------------
    if method == "tools/call":
        name = str(params.get("name", ""))
        args = params.get("arguments") or {}
        tool = registry.lookup(prefix=tool_prefix(), qualified_name=name)
        if tool is None:
            return _rpc_error(
                req_id=req_id, code=-32601,
                message=f"Unknown tool: {name}",
            )
        try:
            output = await tool.handler(**args)
            # MCP spec: result is an object with ``content`` list of
            # TextContent / ImageContent / etc. We return plain text
            # blocks carrying JSON-serialized structured output, which
            # every MCP client can read.
            text = (
                output if isinstance(output, str)
                else json.dumps(output, indent=2, default=str)
            )
            return _rpc_result(req_id=req_id, result={
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except _JsonRpcError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[MCPServer] tool %s raised", name, exc_info=True,
            )
            return _rpc_result(req_id=req_id, result={
                "content": [
                    {"type": "text",
                     "text": f"Tool error: {type(exc).__name__}: {exc}"},
                ],
                "isError": True,
            })

    # Unknown method — return error for requests, drop for notifications.
    if is_notification:
        return None
    return _rpc_error(
        req_id=req_id, code=-32601,
        message=f"Method not found: {method}",
    )


async def serve_stdio(
    registry: ToolRegistry,
    *,
    stdin: Optional[asyncio.StreamReader] = None,
    stdout_write: Optional[Callable[[str], None]] = None,
) -> None:
    """Run the server loop over stdin/stdout until EOF.

    Injectable streams make this testable — tests pass in-memory
    reader/writer; the operator launcher uses real stdio.
    """
    if not server_enabled():
        logger.info(
            "[MCPServer] server disabled (set %s=1 to enable)", _ENV_ENABLED,
        )
        return

    if stdin is None:
        # Wire real stdin (async).
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_event_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        stdin = reader
    if stdout_write is None:
        def _write(line: str) -> None:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
        stdout_write = _write

    logger.info(
        "[MCPServer] ready: tools=%d mutations=%s prefix=%s",
        len(registry.tools),
        "on" if mutations_enabled() else "off",
        tool_prefix(),
    )

    while True:
        try:
            line_bytes = await stdin.readline()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("[MCPServer] stdin readline failed", exc_info=True)
            break
        if not line_bytes:
            # EOF — client disconnected.
            break
        line = line_bytes.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("[MCPServer] malformed line dropped: %r", line[:120])
            continue
        if not isinstance(msg, dict):
            continue
        try:
            response = await _handle_request(registry=registry, msg=msg)
        except _JsonRpcError as exc:
            response = _rpc_error(
                req_id=msg.get("id"),
                code=exc.code,
                message=str(exc),
                data=exc.data,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("[MCPServer] handler raised", exc_info=True)
            response = _rpc_error(
                req_id=msg.get("id"),
                code=-32603,
                message=f"Internal error: {type(exc).__name__}: {exc}",
            )
        if response is not None:
            stdout_write(json.dumps(response))


# ---------------------------------------------------------------------------
# Tool implementations — thin adapters over existing O+V internals
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    """Resolve the project root from env or working directory."""
    env = os.environ.get("JARVIS_REPO_PATH", "").strip()
    return Path(env).resolve() if env else Path.cwd().resolve()


async def _tool_list_orphaned_ops() -> Dict[str, Any]:
    """List orphaned ops from the ledger — same source as /resume list."""
    from backend.core.ouroboros.battle_test.resume_command import ResumeScanner
    repo = _repo_root()
    ledger_root = repo / ".ouroboros" / "state" / "ouroboros" / "ledger"
    orphans = ResumeScanner(ledger_root=ledger_root).scan_orphans()
    return {
        "count": len(orphans),
        "orphans": [
            {
                "op_id": o.op_id,
                "short_op_id": o.short_op_id,
                "last_state": o.last_state,
                "age_s": int(o.age_s),
                "goal": o.goal,
                "target_files": list(o.target_files),
                "near_terminal": o.is_near_terminal,
            }
            for o in orphans
        ],
    }


async def _tool_query_oracle(query: str, top_k: int = 5) -> Dict[str, Any]:
    """Semantic-index search via TheOracle. Read-only."""
    try:
        from backend.core.ouroboros.oracle import TheOracle
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Oracle import failed: {exc}"}
    oracle = TheOracle()
    try:
        await oracle.initialize()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Oracle init failed: {exc}", "query": query}

    # TheOracle's public search method varies across versions; try
    # the two most common shapes defensively.
    results: List[Dict[str, Any]] = []
    try:
        if hasattr(oracle, "search"):
            raw = await oracle.search(query=query, top_k=top_k)
        elif hasattr(oracle, "semantic_search"):
            raw = await oracle.semantic_search(query, k=top_k)
        else:
            return {"error": "Oracle has no search method", "query": query}
        for r in (raw or [])[:top_k]:
            if isinstance(r, dict):
                results.append(r)
            else:
                results.append({"raw": str(r)})
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Oracle search failed: {exc}", "query": query}
    return {"query": query, "top_k": top_k, "hits": results}


async def _tool_risk_classify(
    description: str,
    target_files: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run the risk engine on a synthetic signal (no side effects)."""
    target_files = target_files or []
    try:
        from backend.core.ouroboros.governance.risk_engine import (
            OperationProfile,
            RiskEngine,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"Risk engine import failed: {exc}"}

    # Build a minimal OperationProfile. The RiskEngine fills defaults
    # when optional fields are missing.
    try:
        profile = OperationProfile(
            description=description,
            target_files=tuple(target_files),
            files_affected=tuple(target_files),
            blast_radius=max(1, len(target_files)),
            test_confidence=0.80,
            touches_security_surface=False,
            touches_supervisor_surface=False,
            is_dependency_change=False,
            is_core_orchestration_path=False,
            action_kind="MODIFY",
        )
    except Exception as exc:  # noqa: BLE001
        # Fall back to a permissive duck-typed profile if the real
        # dataclass shape drifts — surface the error rather than crash.
        return {"error": f"Could not build OperationProfile: {exc}"}

    try:
        classification = RiskEngine().classify(profile)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"RiskEngine raised: {exc}"}
    return {
        "tier": classification.tier.name,
        "reason_code": classification.reason_code,
        "description_len": len(description or ""),
        "target_files": target_files,
    }


async def _tool_session_status() -> Dict[str, Any]:
    """Current session snapshot — cost spent, idle, active ops.

    Reads the process-global status-line builder when registered
    (harness singleton). Returns ``{"attached": false, ...}`` when no
    battle-test is booted — the server is still useful for standalone
    tool queries against the ledger / oracle / memory.
    """
    try:
        from backend.core.ouroboros.battle_test.status_line import (
            get_status_line_builder,
        )
        builder = get_status_line_builder()
    except Exception:  # noqa: BLE001
        return {"attached": False, "reason": "status_line import failed"}
    if builder is None:
        return {"attached": False, "reason": "no battle-test session"}
    try:
        snap = builder.snapshot()
    except Exception as exc:  # noqa: BLE001
        return {"attached": False, "reason": f"snapshot failed: {exc}"}
    return {
        "attached": True,
        "phase": snap.phase,
        "phase_detail": snap.phase_detail,
        "cost_spent_usd": snap.cost_spent_usd,
        "cost_budget_usd": snap.cost_budget_usd,
        "idle_elapsed_s": snap.idle_elapsed_s,
        "idle_timeout_s": snap.idle_timeout_s,
        "primary_op_id": snap.primary_op_id,
        "extra_op_count": snap.extra_op_count,
        "route": snap.route,
        "provider": snap.provider,
    }


async def _tool_list_sensors() -> Dict[str, Any]:
    """Enumerate the registered sensor names. Best-effort — if the
    intake layer isn't booted (standalone MCP server without a harness),
    returns an informative stub instead of raising."""
    from backend.core.ouroboros.battle_test.status_line import (
        get_status_line_builder,
    )
    builder = get_status_line_builder()
    if builder is None or builder._gls is None:
        # Fall back to the CLAUDE.md documented sensor list.
        return {
            "attached": False,
            "sensors": [
                "TestFailure", "VoiceCommand", "OpportunityMiner",
                "CapabilityGap", "Scheduled", "Backlog", "RuntimeHealth",
                "WebIntelligence", "PerformanceRegression", "DocStaleness",
                "GitHubIssue", "ProactiveExploration", "CrossRepoDrift",
                "TodoScanner", "CUExecution", "IntentDiscovery",
            ],
        }
    try:
        intake = getattr(builder._gls, "_intake_service", None)
        sensors_attr = getattr(intake, "_sensors", []) if intake else []
        names = []
        for s in sensors_attr:
            cls = type(s).__name__
            names.append(cls)
        return {"attached": True, "sensors": names}
    except Exception as exc:  # noqa: BLE001
        return {"attached": True, "error": str(exc)}


async def _tool_list_memories(
    memory_type: Optional[str] = None,
) -> Dict[str, Any]:
    """UserPreferenceMemory listing. Optional type filter."""
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (
            MemoryType,
            get_default_store,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"memory store import failed: {exc}"}
    store = get_default_store(_repo_root())
    if memory_type:
        try:
            t = MemoryType.from_str(memory_type)
            mems = store.find_by_type(t)
        except Exception:
            return {"error": f"unknown memory type: {memory_type}"}
    else:
        mems = store.list_all()
    return {
        "count": len(mems),
        "memories": [
            {
                "id": m.id,
                "type": m.type.value,
                "name": m.name,
                "description": m.description,
                "tags": list(m.tags or ()),
                "paths": list(m.paths or ()),
                "created_at": m.created_at,
                "updated_at": m.updated_at,
            }
            for m in mems
        ],
    }


async def _tool_search_memories(query: str) -> Dict[str, Any]:
    """Full-text search across memory name + description + content."""
    try:
        from backend.core.ouroboros.governance.user_preference_memory import (
            get_default_store,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"memory store import failed: {exc}"}
    q = (query or "").strip().lower()
    if not q:
        return {"query": "", "hits": []}
    store = get_default_store(_repo_root())
    hits = []
    for m in store.list_all():
        haystack = " ".join([
            m.name or "", m.description or "", m.content or "",
            m.why or "", m.how_to_apply or "",
            " ".join(m.tags or ()), " ".join(m.paths or ()),
        ]).lower()
        if q in haystack:
            hits.append({
                "id": m.id, "type": m.type.value, "name": m.name,
                "description": m.description,
            })
    return {"query": query, "count": len(hits), "hits": hits}


async def _tool_preview_candidate(
    file_path: str,
    old_content: str,
    new_content: str,
) -> Dict[str, Any]:
    """Run SemanticGuardian on a (path, old, new) triple. Pure function;
    no side effects. Lets external clients probe what a candidate WOULD
    trigger without actually submitting an op."""
    from backend.core.ouroboros.governance.semantic_guardian import (
        SemanticGuardian,
        recommend_tier_floor,
    )
    guardian = SemanticGuardian()
    findings = guardian.inspect(
        file_path=file_path,
        old_content=old_content,
        new_content=new_content,
    )
    return {
        "findings_count": len(findings),
        "findings": [
            {
                "pattern": f.pattern,
                "severity": f.severity,
                "message": f.message,
                "file_path": f.file_path,
                "lines": list(f.lines),
            }
            for f in findings
        ],
        "recommended_tier_floor": recommend_tier_floor(findings) or "none",
    }


async def _tool_submit_intent(
    description: str,
    target_files: Optional[List[str]] = None,
    tdd_mode: bool = False,
    urgency: str = "normal",
) -> Dict[str, Any]:
    """MUTATION — submit a new IntentEnvelope via UnifiedIntakeRouter.

    Gated by ``JARVIS_MCP_ALLOW_MUTATIONS=1`` at registration time. The
    envelope goes through the normal intake pipeline (dedup, WAL,
    backpressure, CLASSIFY, every orchestrator gate) — MCP does NOT
    bypass governance. It's equivalent to a sensor firing a signal
    on the operator's behalf.
    """
    try:
        from backend.core.ouroboros.governance.intake.intent_envelope import (
            make_envelope,
        )
        from backend.core.ouroboros.battle_test.status_line import (
            get_status_line_builder,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"intake import failed: {exc}"}
    builder = get_status_line_builder()
    if builder is None or builder._gls is None:
        return {"error": "no attached battle-test session — cannot submit"}
    router = None
    for attr in ("_intake_router", "intake_router", "_router", "router"):
        cand = getattr(builder._gls, attr, None)
        if cand is not None:
            router = cand
            break
    if router is None:
        return {"error": "intake router unreachable"}

    evidence: Dict[str, Any] = {"mcp_source": True}
    if tdd_mode:
        evidence["tdd_mode"] = True

    try:
        envelope = make_envelope(
            source="voice_human",   # closest enum member for human-triggered
            description=description or "",
            target_files=tuple(target_files or ()),
            repo="jarvis",
            confidence=0.9,
            urgency=urgency,
            evidence=evidence,
            requires_human_ack=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"envelope build failed: {exc}"}
    try:
        verdict = await router.ingest(envelope)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"router.ingest raised: {exc}"}
    return {
        "outcome": verdict,
        "signal_id": getattr(envelope, "signal_id", ""),
        "causal_id": getattr(envelope, "causal_id", ""),
    }


# ---------------------------------------------------------------------------
# Default registry factory
# ---------------------------------------------------------------------------


def build_default_registry() -> ToolRegistry:
    """Construct a registry with every V1 tool registered.

    Mutation tools are registered only when
    ``JARVIS_MCP_ALLOW_MUTATIONS=1``. That gate happens inside
    ``ToolRegistry.register`` so the contract is centralized.
    """
    reg = ToolRegistry()

    reg.register(Tool(
        name="list_orphaned_ops",
        description=(
            "List orphaned in-flight operations from the ledger. "
            "Read-only; same data the /resume list REPL command surfaces."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_tool_list_orphaned_ops,
    ))

    reg.register(Tool(
        name="query_oracle",
        description=(
            "Semantic-index search over the codebase via TheOracle. "
            "Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_tool_query_oracle,
    ))

    reg.register(Tool(
        name="risk_classify",
        description=(
            "Run the deterministic risk engine on a synthetic signal. "
            "Returns the tier (SAFE_AUTO|NOTIFY_APPLY|APPROVAL_REQUIRED|"
            "BLOCKED) + reason_code. No side effects."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["description"],
            "additionalProperties": False,
        },
        handler=_tool_risk_classify,
    ))

    reg.register(Tool(
        name="session_status",
        description=(
            "Current battle-test session snapshot — phase, cost, idle, "
            "active ops. Returns attached=false when no session is live."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_tool_session_status,
    ))

    reg.register(Tool(
        name="list_sensors",
        description=(
            "Enumerate the 16 autonomous sensor names. Returns attached=true "
            "when a battle-test is booted with actual sensor instances."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        handler=_tool_list_sensors,
    ))

    reg.register(Tool(
        name="list_memories",
        description=(
            "List UserPreferenceMemory entries. Optional type filter: "
            "user | feedback | project | reference | forbidden_path | style."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "memory_type": {"type": "string"},
            },
            "additionalProperties": False,
        },
        handler=_tool_list_memories,
    ))

    reg.register(Tool(
        name="search_memories",
        description=(
            "Full-text search across memory name + description + "
            "content + tags. Read-only."
        ),
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=_tool_search_memories,
    ))

    reg.register(Tool(
        name="preview_candidate",
        description=(
            "Run SemanticGuardian on a (path, old, new) triple to see "
            "what patterns would fire + the recommended tier floor. "
            "Pure function — no ledger writes, no side effects."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_content": {"type": "string"},
                "new_content": {"type": "string"},
            },
            "required": ["file_path", "old_content", "new_content"],
            "additionalProperties": False,
        },
        handler=_tool_preview_candidate,
    ))

    # Mutation (gated inside register()).
    reg.register(Tool(
        name="submit_intent",
        description=(
            "MUTATION: submit a new IntentEnvelope to the intake "
            "router. Goes through every orchestrator gate (CLASSIFY, "
            "risk engine, semantic guardian, tier floor). Requires "
            "JARVIS_MCP_ALLOW_MUTATIONS=1."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "description": {"type": "string"},
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "tdd_mode": {"type": "boolean"},
                "urgency": {
                    "type": "string",
                    "enum": ["low", "normal", "high", "critical"],
                },
            },
            "required": ["description"],
            "additionalProperties": False,
        },
        handler=_tool_submit_intent,
        mutating=True,
    ))

    return reg


async def main() -> None:
    """Standalone entry point: build registry, serve stdio until EOF."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        stream=sys.stderr,  # stdout reserved for MCP protocol
    )
    registry = build_default_registry()
    await serve_stdio(registry)


if __name__ == "__main__":
    asyncio.run(main())
