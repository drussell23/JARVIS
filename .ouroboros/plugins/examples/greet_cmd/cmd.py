"""Example REPL command plugin — adds /greet."""
from __future__ import annotations

from backend.core.ouroboros.plugins.plugin_base import ReplCommandPlugin


class GreetCommand(ReplCommandPlugin):
    command_name = "greet"
    summary = "Trivial greeting — proves the REPL plugin dispatch works"

    async def run(self, args: str) -> str:
        target = (args or "").strip() or "operator"
        return f"Hello, {target}. Plugin dispatch is working."
