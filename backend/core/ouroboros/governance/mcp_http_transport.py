"""MCP HTTP Transport — FastAPI endpoint exposing OuroborosMCPServer.

Provides REST endpoints for external MCP clients to drive Ouroboros:
    POST /mcp/submit_intent     — submit a governance operation
    GET  /mcp/status/{op_id}    — query operation status
    POST /mcp/approve           — approve a pending operation
    POST /mcp/reject            — reject with correction reason
    POST /mcp/elicit_answer     — answer a structured elicitation
    GET  /mcp/health            — health check

Designed for mounting into an existing FastAPI app or running standalone.
The GLS instance is injected via set_gls() after supervisor boot.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Lazy FastAPI import — only fails if actually used without installation
_app = None
_mcp_server = None


def create_mcp_app() -> Any:
    """Create and return the FastAPI app for MCP transport.

    Returns None if fastapi is not installed.
    """
    global _app
    try:
        from fastapi import FastAPI
        from pydantic import BaseModel
    except ImportError:
        logger.warning("[MCPTransport] FastAPI not installed — MCP HTTP disabled")
        return None

    app = FastAPI(
        title="Ouroboros MCP Transport",
        description="REST interface for the Ouroboros governance pipeline",
        version="1.0.0",
    )

    # --- Request/Response models ---

    class SubmitIntentRequest(BaseModel):
        goal: str
        target_files: List[str] = []
        repo: str = "jarvis"

    class ApproveRequest(BaseModel):
        request_id: str
        approver: str = "mcp_client"

    class RejectRequest(BaseModel):
        request_id: str
        approver: str = "mcp_client"
        reason: str = ""

    class ElicitAnswerRequest(BaseModel):
        request_id: str
        answer: str

    # --- Endpoints ---

    @app.post("/mcp/submit_intent")
    async def submit_intent(req: SubmitIntentRequest) -> Dict[str, Any]:
        if _mcp_server is None:
            return {"status": "error", "error": "MCP server not initialized"}
        return await _mcp_server.submit_intent(
            goal=req.goal,
            target_files=req.target_files,
            repo=req.repo,
        )

    @app.get("/mcp/status/{op_id}")
    async def get_status(op_id: str) -> Dict[str, Any]:
        if _mcp_server is None:
            return {"status": "error", "error": "MCP server not initialized"}
        return await _mcp_server.get_operation_status(op_id)

    @app.post("/mcp/approve")
    async def approve(req: ApproveRequest) -> Dict[str, Any]:
        if _mcp_server is None:
            return {"status": "error", "error": "MCP server not initialized"}
        return await _mcp_server.approve_operation(
            request_id=req.request_id,
            approver=req.approver,
        )

    @app.post("/mcp/reject")
    async def reject(req: RejectRequest) -> Dict[str, Any]:
        if _mcp_server is None:
            return {"status": "error", "error": "MCP server not initialized"}
        return await _mcp_server.reject_operation(
            request_id=req.request_id,
            approver=req.approver,
            reason=req.reason,
        )

    @app.post("/mcp/elicit_answer")
    async def elicit_answer(req: ElicitAnswerRequest) -> Dict[str, Any]:
        if _mcp_server is None:
            return {"status": "error", "error": "MCP server not initialized"}
        return await _mcp_server.elicit_answer(
            request_id=req.request_id,
            answer=req.answer,
        )

    @app.get("/mcp/health")
    async def health() -> Dict[str, Any]:
        if _mcp_server is None:
            return {"status": "not_initialized"}
        try:
            gls_health = _mcp_server._gls.health() if hasattr(_mcp_server._gls, "health") else {}
            return {"status": "ok", "gls": gls_health}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}

    _app = app
    return app


def set_gls(gls: Any) -> None:
    """Inject the GovernedLoopService after supervisor boot.

    Called by unified_supervisor.py once GLS is initialized.
    """
    global _mcp_server
    from backend.core.ouroboros.governance.mcp_server import OuroborosMCPServer
    _mcp_server = OuroborosMCPServer(gls)
    logger.info("[MCPTransport] MCP server initialized with GLS")


def get_app() -> Any:
    """Get the FastAPI app, creating it if needed."""
    global _app
    if _app is None:
        return create_mcp_app()
    return _app
