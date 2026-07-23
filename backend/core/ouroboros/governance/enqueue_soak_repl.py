"""``/enqueue_soak <target_path>`` — stage a crash-immortal Swarm soak.

Expresses workload INTENT to the Autonomous Supervisor: async-walks the target,
hashes its AST chunks into a deterministic checkpoint manifest, and writes it as
a pending row in the ``soak_intent_queue`` (#70050). The Supervisor then arms
itself the moment DW is DEGRADED, and the Swarm resumes from the manifest across
any crash (#70051).

Naming-cage: auto-discovered as verb ``enqueue_soak`` via the module-level
``dispatch_enqueue_soak_command``. Imports stdlib + the checkpoint/intent data
helpers ONLY — no orchestrator / providers / iron_gate authority. Never raises.
"""

from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass

logger = logging.getLogger("Ouroboros.EnqueueSoakRepl")

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_RED = "\033[31m"

_HELP = (
    f"  {_BOLD}{_CYAN}/enqueue_soak <target_path>{_RESET}\n"
    f"  {_DIM}Stage a crash-immortal Swarm soak. Walks the target, hashes its AST "
    f"chunks into a\n  deterministic checkpoint manifest, and queues it as pending "
    f"intent. The Autonomous\n  Supervisor auto-arms on DW recovery; the Swarm "
    f"resumes from the manifest across crashes.{_RESET}\n\n"
    f"  {_BOLD}Options:{_RESET}\n"
    f"    {_CYAN}--priority N{_RESET}   {_DIM}queue priority (lower = more urgent; default 1){_RESET}\n"
    f"    {_CYAN}--kind K{_RESET}       {_DIM}intent kind label (default agentic_swarm_soak){_RESET}\n"
)


@dataclass(frozen=True)
class EnqueueSoakDispatchResult:
    ok: bool
    text: str
    matched: bool = True


def _matches(line: str) -> bool:
    s = (line or "").strip()
    return s == "enqueue_soak" or s == "/enqueue_soak" or \
        s.startswith("enqueue_soak ") or s.startswith("/enqueue_soak ")


async def dispatch_enqueue_soak_command(line: str) -> EnqueueSoakDispatchResult:
    """Async — walks the target off the event loop, writes the manifest row."""
    if not _matches(line):
        return EnqueueSoakDispatchResult(ok=False, text="", matched=False)
    try:
        raw = (line or "").strip().lstrip("/")
        argv = shlex.split(raw)[1:]  # drop the verb token
    except ValueError as exc:
        return EnqueueSoakDispatchResult(ok=False, text=f"  /enqueue_soak parse error: {exc}")

    if not argv or argv[0] in ("help", "-h", "--help"):
        return EnqueueSoakDispatchResult(ok=True, text=_HELP)

    # Parse: <target_path> [--priority N] [--kind K]
    target = ""
    priority = 1
    kind = "agentic_swarm_soak"
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--priority" and i + 1 < len(argv):
            try:
                priority = int(argv[i + 1])
            except ValueError:
                return EnqueueSoakDispatchResult(ok=False, text=f"  {_RED}--priority must be an integer{_RESET}")
            i += 2
        elif tok == "--kind" and i + 1 < len(argv):
            kind = argv[i + 1]
            i += 2
        elif not target and not tok.startswith("--"):
            target = tok
            i += 1
        else:
            i += 1

    if not target:
        return EnqueueSoakDispatchResult(ok=False, text=f"  {_RED}usage: /enqueue_soak <target_path>{_RESET}")
    abs_target = os.path.abspath(os.path.expanduser(target))
    if not os.path.exists(abs_target):
        return EnqueueSoakDispatchResult(ok=False, text=f"  {_RED}no such path: {abs_target}{_RESET}")

    try:
        from backend.core.ouroboros.governance.dw_outage_forecaster import open_forecast_db
        from backend.core.ouroboros.governance.checkpoint_manifest import enqueue_soak_manifest
    except Exception as exc:  # noqa: BLE001
        return EnqueueSoakDispatchResult(ok=False, text=f"  /enqueue_soak unavailable: {exc}")

    conn = open_forecast_db()
    try:
        res = await enqueue_soak_manifest(conn, abs_target, kind=kind, priority=priority)
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:  # noqa: BLE001
            pass

    if not res:
        return EnqueueSoakDispatchResult(ok=False, text=f"  {_RED}enqueue failed (no writable substrate?){_RESET}")

    n = res["chunk_count"]
    iid = res["intent_id"]
    manifest = res.get("manifest", {})
    files = sorted({c.get("file_path", "?") for c in manifest.get("pending_chunks", [])})
    file_line = (
        f"{len(files)} file(s)" if len(files) != 1 else f"{files[0]}"
    )
    text = (
        f"  {_GREEN}⚡ soak intent queued{_RESET}  {_BOLD}{iid}{_RESET}  "
        f"{_DIM}({n} AST chunk(s) across {file_line}, priority {priority}){_RESET}\n"
        f"  {_DIM}manifest written — the Supervisor arms on DW recovery; the Swarm "
        f"resumes from checkpoint across any crash.{_RESET}"
    )
    logger.info("[EnqueueSoak] queued intent=%s chunks=%d target=%s", iid, n, abs_target)
    return EnqueueSoakDispatchResult(ok=True, text=text)


__all__ = ["EnqueueSoakDispatchResult", "dispatch_enqueue_soak_command"]
