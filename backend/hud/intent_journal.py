"""Write-ahead journal for voice intents — and the replay rule that keeps it safe.

A spoken command enters at `VoiceCommandRouter.route()`, is classified by a
model, planned, and executed as N UI steps. If the process dies at step 3 the
whole command is gone: the operator said a sentence, watched half of it happen,
and has no way to know which half. Nothing in the HUD path checkpoints — that
was verified, not assumed (`grep fsm_checkpoint backend/hud/ backend/vision/`
returns nothing).

WHY THIS IS NOT A COPY OF `fsm_checkpoint`
--------------------------------------------
`fsm_checkpoint` already does suspend-and-resume for the Ouroboros op FSM, to
`.ouroboros/checkpoints`, HMAC-signed, so a preempted DAG "resumes where it left
off instead of re-paying the explore-from-scratch cost". That is the same
shape — and the voice path never reaches it, because `route()` dispatches
straight to executors and never enters the governed loop. So this reuses its
DIRECTORY convention (`.ouroboros/`), its append primitive
(`cross_process_jsonl.flock_append_line`), and its fail-soft posture, while
covering a path it does not.

THE RULE THAT SHAPES EVERYTHING: NOT EVERY NODE MAY BE REPLAYED
-----------------------------------------------------------------
The obvious design — journal each phase, resume from the last incomplete one —
is correct for *pure* work and dangerous for the rest. The CU executor's steps
are ``type``, ``click``, ``drag``, ``scroll``, ``hotkey``. They act on the real
world. Re-running "step 3 of message Alice" does not recompute a value; it
sends a second message.

So nodes declare what they are:

    PURE       classification, planning, parsing. Deterministic given the same
               input, no outside effect. Replay REUSES the recorded result and
               never re-calls the model — genuinely idempotent, and a real
               saving.
    EFFECTFUL  it touched something outside this process. NEVER auto-replayed.

And for an EFFECTFUL node that started and never finished, the honest state is
not "done" and not "not done" — it is UNKNOWN. The journal says so and the
resume plan asks rather than guesses. That is the same discipline as
`UNKNOWN != UNSET` in the provenance layer and `unverified != unsafe` in the
coordination probe: a system that cannot tell must not pick the convenient
answer and call it a fact.

Python 3.9+, ``from __future__ import annotations``.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger("JARVIS.IntentJournal")

INTENT_JOURNAL_SCHEMA_VERSION: str = "intent_journal.v1"


def journal_enabled() -> bool:
    """Master gate. Default TRUE — append-only, bounded, local. NEVER raises."""
    return (os.environ.get("JARVIS_INTENT_JOURNAL_ENABLED", "true")
            or "").strip().lower() not in ("0", "false", "no", "off")


def journal_path() -> Path:
    """`.ouroboros/intents/journal.jsonl` beside the checkpoint ledger.

    Same directory convention as `fsm_checkpoint.checkpoint_dir()` so an
    operator finds every resume artefact in one place. NEVER raises."""
    raw = (os.environ.get("JARVIS_INTENT_JOURNAL_PATH", "") or "").strip() 
    if raw:
        return Path(raw)
    base = (os.environ.get("JARVIS_INTENT_JOURNAL_DIR", "")
            or os.path.join(".ouroboros", "intents"))
    return Path(base) / "journal.jsonl"


def retention_s() -> float:
    """How long a completed intent stays replayable. NEVER raises."""
    try:
        v = float(os.environ.get("JARVIS_INTENT_JOURNAL_RETENTION_S", "86400"))
    except (TypeError, ValueError):
        v = 86400.0
    return max(60.0, min(v, 30 * 86400.0))


def max_lines() -> int:
    """Compaction trigger. NEVER raises."""
    try:
        return max(64, int(os.environ.get("JARVIS_INTENT_JOURNAL_MAX_LINES", "5000")))
    except (TypeError, ValueError):
        return 5000


class NodeKind(str, enum.Enum):
    """Whether a node may be replayed after a crash."""

    PURE = "pure"
    EFFECTFUL = "effectful"


class NodeState(str, enum.Enum):
    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    #: Started, never finished, and EFFECTFUL — so whether the world changed is
    #: unknown. Not a synonym for failed: a failed node is known not to have
    #: taken effect, this one might have.
    INDETERMINATE = "indeterminate"


class ResumeAction(str, enum.Enum):
    SKIP = "skip"              # PURE and completed — reuse the recorded result
    REDO = "redo"              # safe to run again
    CONFIRM = "confirm"        # EFFECTFUL and indeterminate — ASK, never guess
    START = "start"            # never attempted


@dataclass
class NodeVerdict:
    node: str
    action: ResumeAction
    kind: NodeKind = NodeKind.PURE
    result: Any = None
    detail: str = ""


@dataclass
class ResumePlan:
    """What to do with an interrupted intent. NEVER guesses."""

    intent_id: str
    command: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    verdicts: List[NodeVerdict] = field(default_factory=list)
    schema_version: str = INTENT_JOURNAL_SCHEMA_VERSION

    def for_node(self, node: str) -> NodeVerdict:
        for v in self.verdicts:
            if v.node == node:
                return v
        return NodeVerdict(node=node, action=ResumeAction.START)

    @property
    def needs_confirmation(self) -> List[NodeVerdict]:
        return [v for v in self.verdicts if v.action is ResumeAction.CONFIRM]

    @property
    def resumable(self) -> bool:
        """True when replay can proceed without asking a human."""
        return not self.needs_confirmation


class IntentJournal:
    """Append-only WAL for voice intents. Every method NEVER raises."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or journal_path()
        self._writes = 0
        self._write_failures = 0
        self._corrupt_lines = 0

    # -- writing ---------------------------------------------------------

    async def _append(self, entry: Dict[str, Any]) -> bool:
        if not journal_enabled():
            return False
        try:
            entry.setdefault("v", 1)
            entry.setdefault("t", time.time())
            line = json.dumps(entry, separators=(",", ":"), default=str)

            def _write() -> bool:
                from backend.core.ouroboros.governance.cross_process_jsonl import (
                    flock_append_line,
                )
                return flock_append_line(self._path, line)

            # Off-thread: `flock` blocks under contention, and a spoken command
            # must not wait on another process's journal write.
            ok = await asyncio.to_thread(_write)
            if ok:
                self._writes += 1
            else:
                self._write_failures += 1
            return ok
        except Exception:  # noqa: BLE001 — a journal never breaks the command
            self._write_failures += 1
            logger.debug("[IntentJournal] append degraded", exc_info=True)
            return False

    async def open_intent(self, command: str, *,
                          payload: Optional[Dict[str, Any]] = None,
                          intent_id: Optional[str] = None) -> str:
        """Record the raw command BEFORE anything executes. Returns its id.

        This is the write-ahead half: if the process dies one instruction
        later, the operator's sentence still exists somewhere.
        """
        iid = intent_id or uuid.uuid4().hex
        await self._append({
            "k": "intent", "id": iid, "command": command,
            "payload": payload or {},
            "schema_version": INTENT_JOURNAL_SCHEMA_VERSION,
        })
        return iid

    async def node_started(self, intent_id: str, node: str,
                           kind: NodeKind = NodeKind.PURE) -> None:
        await self._append({"k": "node", "id": intent_id, "node": node,
                            "kind": kind.value, "s": NodeState.STARTED.value})

    async def node_completed(self, intent_id: str, node: str,
                             result: Any = None,
                             kind: NodeKind = NodeKind.PURE) -> None:
        """Record the node's OUTPUT for PURE nodes so replay can reuse it.

        An effectful node's result is not stored for replay — it would invite
        exactly the reuse that is unsafe — only that it finished.
        """
        entry: Dict[str, Any] = {
            "k": "node", "id": intent_id, "node": node, "kind": kind.value,
            "s": NodeState.COMPLETED.value,
        }
        if kind is NodeKind.PURE:
            entry["result"] = result
        await self._append(entry)

    async def node_failed(self, intent_id: str, node: str, error: str,
                          kind: NodeKind = NodeKind.PURE) -> None:
        await self._append({"k": "node", "id": intent_id, "node": node,
                            "kind": kind.value, "s": NodeState.FAILED.value,
                            "error": str(error)[:500]})

    async def close_intent(self, intent_id: str, *, success: bool,
                           detail: str = "") -> None:
        await self._append({"k": "close", "id": intent_id,
                            "ok": bool(success), "detail": detail[:500]})

    # -- reading ---------------------------------------------------------

    def _read(self) -> List[Dict[str, Any]]:
        """Every parseable entry. Tolerant per LINE — a process killed
        mid-append leaves a torn last line, and discarding the journal for a
        missing byte would lose what it exists to keep. NEVER raises."""
        out: List[Dict[str, Any]] = []
        try:
            from backend.core.ouroboros.governance.workspace_resolver import (
                resolve_durable_path,
            )
            path = resolve_durable_path(self._path)
            if not path.exists():
                return out
            for raw in path.read_text(encoding="utf-8",
                                      errors="replace").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                    if isinstance(e, dict):
                        out.append(e)
                    else:
                        self._corrupt_lines += 1
                except Exception:  # noqa: BLE001
                    self._corrupt_lines += 1
        except Exception:  # noqa: BLE001
            logger.debug("[IntentJournal] read degraded", exc_info=True)
        return out

    def unfinished(self) -> List[str]:
        """Intent ids opened, never closed, still inside retention. NEVER raises."""
        try:
            now = time.time()
            opened: Dict[str, float] = {}
            closed = set()
            for e in self._read():
                iid = e.get("id")
                if not isinstance(iid, str):
                    continue
                if e.get("k") == "intent":
                    opened[iid] = float(e.get("t", now))
                elif e.get("k") == "close":
                    closed.add(iid)
            cutoff = now - retention_s()
            return [i for i, t in opened.items()
                    if i not in closed and t >= cutoff]
        except Exception:  # noqa: BLE001
            return []

    def resume_plan(self, intent_id: str) -> ResumePlan:
        """What may be skipped, redone, or must be asked about. NEVER raises."""
        plan = ResumePlan(intent_id=intent_id)
        try:
            states: Dict[str, Tuple[NodeState, NodeKind, Any]] = {}
            order: List[str] = []
            for e in self._read():
                if e.get("id") != intent_id:
                    continue
                if e.get("k") == "intent":
                    plan.command = str(e.get("command", ""))
                    payload = e.get("payload")
                    plan.payload = payload if isinstance(payload, dict) else {}
                elif e.get("k") == "node":
                    node = e.get("node")
                    if not isinstance(node, str):
                        continue
                    if node not in order:
                        order.append(node)
                    try:
                        st = NodeState(e.get("s"))
                        kind = NodeKind(e.get("kind", "pure"))
                    except ValueError:
                        self._corrupt_lines += 1
                        continue
                    prev = states.get(node)
                    # A later terminal state supersedes STARTED; a STARTED must
                    # never overwrite a COMPLETED (retry then crash).
                    if prev and prev[0] is NodeState.COMPLETED \
                            and st is NodeState.STARTED:
                        continue
                    states[node] = (st, kind, e.get("result"))
            for node in order:
                st, kind, result = states[node]
                if st is NodeState.COMPLETED:
                    if kind is NodeKind.PURE:
                        plan.verdicts.append(NodeVerdict(
                            node, ResumeAction.SKIP, kind, result,
                            "pure and already computed — reuse the result"))
                    else:
                        plan.verdicts.append(NodeVerdict(
                            node, ResumeAction.SKIP, kind, None,
                            "effect already applied — must not repeat it"))
                elif st is NodeState.FAILED:
                    # Known NOT to have taken effect: it reported failure.
                    plan.verdicts.append(NodeVerdict(
                        node, ResumeAction.REDO, kind, None,
                        "failed cleanly — safe to attempt again"))
                else:  # STARTED, never resolved
                    if kind is NodeKind.PURE:
                        plan.verdicts.append(NodeVerdict(
                            node, ResumeAction.REDO, kind, None,
                            "pure — recomputing costs time, never correctness"))
                    else:
                        plan.verdicts.append(NodeVerdict(
                            node, ResumeAction.CONFIRM, kind, None,
                            "started and never finished, and it touches the "
                            "world — whether it applied is UNKNOWN"))
        except Exception:  # noqa: BLE001
            logger.debug("[IntentJournal] resume_plan degraded", exc_info=True)
        return plan

    def stats(self) -> Dict[str, Any]:
        return {
            "schema_version": INTENT_JOURNAL_SCHEMA_VERSION,
            "enabled": journal_enabled(),
            "path": str(self._path),
            "writes": self._writes,
            "write_failures": self._write_failures,
            "corrupt_lines": self._corrupt_lines,
            "unfinished": len(self.unfinished()),
        }

    def compact(self) -> int:
        """Drop entries for intents closed outside retention. Returns lines
        kept. Under the cross-process lock — the read-trim-write a plain append
        cannot express. NEVER raises."""
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (
                flock_critical_section,
            )
            from backend.core.ouroboros.governance.workspace_resolver import (
                resolve_durable_path,
            )
            path = resolve_durable_path(self._path)
            if not path.exists():
                return 0
            with flock_critical_section(path) as ok:
                if not ok:
                    return -1
                now = time.time()
                cutoff = now - retention_s()
                raw_lines = path.read_text(encoding="utf-8",
                                           errors="replace").splitlines()
                stale = set()
                for raw in raw_lines:
                    try:
                        e = json.loads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    if e.get("k") == "close" and float(e.get("t", now)) < cutoff:
                        stale.add(e.get("id"))
                kept = []
                for raw in raw_lines:
                    try:
                        if json.loads(raw).get("id") in stale:
                            continue
                    except Exception:  # noqa: BLE001
                        continue      # corrupt lines are dropped by compaction
                    kept.append(raw)
                tmp = path.with_suffix(path.suffix + ".compact")
                tmp.write_text("\n".join(kept) + ("\n" if kept else ""),
                               encoding="utf-8")
                os.replace(tmp, path)
                return len(kept)
        except Exception:  # noqa: BLE001
            logger.debug("[IntentJournal] compact degraded", exc_info=True)
            return -1


_JOURNAL: Optional[IntentJournal] = None


def get_intent_journal() -> IntentJournal:
    """Process-wide journal. NEVER raises."""
    global _JOURNAL
    if _JOURNAL is None:
        _JOURNAL = IntentJournal()
    return _JOURNAL


def reset_intent_journal() -> None:
    """Testing seam. NEVER raises."""
    global _JOURNAL
    _JOURNAL = None


# ---------------------------------------------------------------------------
# The replay engine
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """One step of an intent's DAG."""

    name: str
    run: Any                      # async callable(ctx) -> result
    kind: NodeKind = NodeKind.PURE


async def run_dag(nodes: Sequence[Node], *, command: str,
                  journal: Optional[IntentJournal] = None,
                  intent_id: Optional[str] = None,
                  payload: Optional[Dict[str, Any]] = None,
                  ctx: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run *nodes* in order, journalling each, resuming a prior attempt.

    Resuming is driven entirely by the journal, so it works across a process
    death — the case a retry loop inside the executor cannot survive.

    Returns ``{intent_id, results, completed, blocked_on}``. ``blocked_on`` is
    non-empty when an EFFECTFUL node was interrupted and replay would have to
    guess; the DAG stops rather than risk repeating a real-world action.
    """
    j = journal or get_intent_journal()
    ctx = dict(ctx or {})
    plan: Optional[ResumePlan] = None
    if intent_id:
        plan = j.resume_plan(intent_id)
        if plan.needs_confirmation:
            return {"intent_id": intent_id, "results": {}, "completed": False,
                    "blocked_on": [v.node for v in plan.needs_confirmation]}
    else:
        intent_id = await j.open_intent(command, payload=payload)

    results: Dict[str, Any] = {}
    for node in nodes:
        verdict = plan.for_node(node.name) if plan else None
        if verdict is not None and verdict.action is ResumeAction.SKIP:
            results[node.name] = verdict.result
            ctx[node.name] = verdict.result
            logger.info("[IntentJournal] %s: skipping '%s' — %s",
                        intent_id[:8], node.name, verdict.detail)
            continue
        await j.node_started(intent_id, node.name, node.kind)
        try:
            result = await node.run(ctx)
        except Exception as exc:  # noqa: BLE001 — the caller decides retry
            await j.node_failed(intent_id, node.name, repr(exc), node.kind)
            raise
        results[node.name] = result
        ctx[node.name] = result
        await j.node_completed(intent_id, node.name, result, node.kind)
    await j.close_intent(intent_id, success=True)
    return {"intent_id": intent_id, "results": results, "completed": True,
            "blocked_on": []}
