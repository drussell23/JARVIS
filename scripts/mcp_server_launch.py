#!/usr/bin/env python3
"""Launcher for the Ouroboros MCP server (stdio transport).

Usage
-----

As a standalone server (no battle test attached) — read-only queries
still work against the ledger / oracle / memory:

    JARVIS_MCP_SERVER_ENABLED=1 python3 scripts/mcp_server_launch.py

With mutations (submit_intent) enabled — requires a battle test to
attach to the router:

    JARVIS_MCP_SERVER_ENABLED=1 \\
    JARVIS_MCP_ALLOW_MUTATIONS=1 \\
    python3 scripts/mcp_server_launch.py

Connecting from Claude Code — add to ``~/.claude/settings.json`` (or
your client's MCP config):

    {
      "mcpServers": {
        "ouroboros": {
          "command": "python3",
          "args": ["scripts/mcp_server_launch.py"],
          "env": {
            "JARVIS_MCP_SERVER_ENABLED": "1",
            "JARVIS_REPO_PATH": "/absolute/path/to/JARVIS-AI-Agent"
          }
        }
      }
    }

The server reads JSON-RPC 2.0 messages from stdin and writes responses
to stdout. Logs go to stderr so they don't corrupt the protocol frame.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def _ensure_repo_on_path() -> None:
    """Walk up from this file to find the repo root (contains ``backend/``)
    and prepend it to sys.path so ``backend.*`` imports resolve. Operators
    don't need to set ``PYTHONPATH=.`` themselves."""
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "backend").is_dir():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return


_ensure_repo_on_path()

from backend.core.ouroboros.mcp_server import main  # noqa: E402


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
