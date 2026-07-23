"""AST-Aware Checkpoint Intent Engine — functionally immortal Swarm map-reduce.

Root cause of lost map-reduce progress: a stateless (generic-string) intent. A
hard crash at file #400 of a 500-file soak restarts from file #1 — massive
compute + token waste. The fix is a deterministic, serialized JSON manifest that
tracks granular chunk-level completion, mutated IN PLACE inside the existing
``soak_intent_queue`` row (no new database).

Three guarantees:

  * **Deterministic manifest** — ``/enqueue_soak`` walks the target, extracts
    per-file AST chunks (top-level defs/classes), and hashes each to a stable
    ``chunk_id = sha256(rel_path:symbol:content_sha)[:16]``. The manifest is
    ``pending_chunks[]`` + an empty ``completed_chunks[]``, serialized with
    ``sort_keys`` so it is byte-identical for identical inputs.

  * **Atomic chunk commits** — the instant a sub-agent completes a chunk,
    :func:`mark_chunk_complete` moves that hash pending→completed under a
    ``BEGIN IMMEDIATE`` read-modify-write (SQLite's single-writer lock serializes
    concurrent committers; ``busy_timeout`` makes cross-process contenders wait
    rather than lose). Idempotent: re-committing an already-completed hash is a
    no-op success.

  * **Resume-from-hash idempotency** — on re-arm the runner parses the manifest
    and STRUCTURALLY ignores every hash already in ``completed_chunks``
    (:func:`resume_pending`), so a restarted Swarm processes only the remainder,
    never a duplicate. A file whose content CHANGED gets a new hash → correctly
    treated as fresh work.

DRY: mutates the existing intent row's ``manifest_json`` (soak_intent.py), reuses
``chunk_strategy.db``, and the runner wraps — never forks — the existing swarm.
Schema-versioned per Vision discipline. Fable is never referenced. Never raises.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, List, Optional

from backend.core.ouroboros.governance.soak_intent import (
    enqueue_soak_intent,
    ensure_intent_table,
    get_manifest_json,
)

logger = logging.getLogger("Ouroboros.CheckpointManifest")

MANIFEST_SCHEMA_VERSION = 1
_INTENT_TABLE = "soak_intent_queue"


def _emit_event(event_type: str, payload: dict) -> None:
    """Surface checkpoint progress on the broker so the operator watches the
    map-reduce tick live in ``/breadcrumbs`` (the Swarm's immortality made
    VISIBLE, not just durable in SQLite). Best-effort; never raises."""
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_task_event,
        )
        op_id = str(payload.get("intent_id", "soak")) or "soak"
        publish_task_event(event_type, op_id, dict(payload))
    except Exception:  # noqa: BLE001
        pass

_DEFAULT_EXTS = (".py",)


@dataclass(frozen=True)
class ChunkDescriptor:
    """One deterministically-hashed unit of soak work (a file-level AST chunk)."""
    chunk_id: str
    file_path: str
    symbol: str
    start_line: int
    end_line: int
    content_sha: str


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def chunk_id_for(rel_path: str, symbol: str, content_sha: str) -> str:
    """Stable id — identical inputs always hash identically; a content change
    (new ``content_sha``) yields a new id (→ treated as fresh work on resume)."""
    return _sha(f"{rel_path}:{symbol}:{content_sha}")[:16]


def _extract_file_chunks(root: str, abs_path: str) -> List[ChunkDescriptor]:
    """Top-level ``def``/``class`` chunks of one file. If the file cannot be
    AST-parsed (or has no top-level symbols) it is hashed as a single
    whole-file chunk (symbol ``"<module>"``) so nothing is silently skipped."""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            source = fh.read()
    except OSError:
        return []
    try:
        rel = os.path.relpath(abs_path, root)
    except ValueError:
        rel = abs_path
    out: List[ChunkDescriptor] = []
    try:
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = int(getattr(node, "lineno", 0) or 0)
                end = int(getattr(node, "end_lineno", start) or start)
                seg = ast.get_source_segment(source, node) or ""
                csha = _sha(seg)[:16]
                out.append(ChunkDescriptor(
                    chunk_id=chunk_id_for(rel, node.name, csha),
                    file_path=rel, symbol=node.name,
                    start_line=start, end_line=end, content_sha=csha,
                ))
    except SyntaxError:
        out = []
    if not out:
        csha = _sha(source)[:16]
        out.append(ChunkDescriptor(
            chunk_id=chunk_id_for(rel, "<module>", csha),
            file_path=rel, symbol="<module>",
            start_line=1, end_line=(source.count("\n") + 1),
            content_sha=csha,
        ))
    # Deterministic ordering (path, then start-line) → stable manifest.
    out.sort(key=lambda c: (c.file_path, c.start_line, c.symbol))
    return out


def _walk_sync(target: str, exts: tuple) -> List[ChunkDescriptor]:
    root = os.path.abspath(target)
    chunks: List[ChunkDescriptor] = []
    if os.path.isfile(root):
        base = os.path.dirname(root) or "."
        chunks.extend(_extract_file_chunks(base, root))
    elif os.path.isdir(root):
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.endswith(tuple(exts)):
                    chunks.extend(_extract_file_chunks(root, os.path.join(dirpath, name)))
    # Global determinism + dedup by chunk_id.
    seen: set = set()
    uniq: List[ChunkDescriptor] = []
    for c in sorted(chunks, key=lambda c: (c.file_path, c.start_line, c.symbol)):
        if c.chunk_id not in seen:
            seen.add(c.chunk_id)
            uniq.append(c)
    return uniq


async def walk_target(
    target: str, *, exts: tuple = _DEFAULT_EXTS,
) -> List[ChunkDescriptor]:
    """Asynchronously walk *target* (dir or single file) and return its
    deterministic AST chunk descriptors. The blocking os.walk / AST parse runs in
    a worker thread so the REPL event loop is never stalled. Never raises."""
    try:
        return await asyncio.to_thread(_walk_sync, target, exts)
    except Exception:  # noqa: BLE001
        logger.debug("[Checkpoint] walk_target failed for %s", target, exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Manifest (de)serialization — deterministic
# ---------------------------------------------------------------------------


def build_manifest(target: str, chunks: List[ChunkDescriptor], *, now: Optional[float] = None) -> dict:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "target": target,
        "created_ts": time.time() if now is None else float(now),
        "pending_chunks": [asdict(c) for c in chunks],
        "completed_chunks": [],
    }


def serialize_manifest(manifest: dict) -> str:
    """Deterministic serialization (sorted keys) — byte-identical for identical
    manifests, so a content-unchanged re-enqueue is provably stable."""
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"))


def deserialize_manifest(raw: Optional[str]) -> Optional[dict]:
    if not raw:
        return None
    try:
        m = json.loads(raw)
        return m if isinstance(m, dict) else None
    except (ValueError, TypeError):
        return None


def completed_ids(manifest: dict) -> set:
    return {
        str(c.get("chunk_id"))
        for c in (manifest or {}).get("completed_chunks", []) or []
        if isinstance(c, dict) and c.get("chunk_id")
    }


def resume_pending(manifest: dict) -> List[dict]:
    """The chunks still to do: every ``pending_chunks`` entry whose hash is NOT in
    ``completed_chunks``. Structural idempotency — a resumed Swarm sees only the
    remainder, never a duplicate. Never raises."""
    if not manifest:
        return []
    done = completed_ids(manifest)
    return [
        c for c in manifest.get("pending_chunks", []) or []
        if isinstance(c, dict) and str(c.get("chunk_id")) not in done
    ]


# ---------------------------------------------------------------------------
# Enqueue with manifest (the /enqueue_soak backend)
# ---------------------------------------------------------------------------


async def enqueue_soak_manifest(
    conn: Optional[sqlite3.Connection],
    target: str,
    *,
    kind: str = "agentic_swarm_soak",
    priority: int = 1,
    exts: tuple = _DEFAULT_EXTS,
    now: Optional[float] = None,
) -> Optional[dict]:
    """Walk *target*, build the manifest, and write it into a NEW pending intent
    row. Returns ``{intent_id, manifest, chunk_count}`` or ``None``. Never raises."""
    if conn is None:
        return None
    chunks = await walk_target(target, exts=exts)
    manifest = build_manifest(target, chunks, now=now)
    raw = serialize_manifest(manifest)
    iid = enqueue_soak_intent(
        conn, kind=kind, target=target, priority=priority, manifest_json=raw, now=now,
    )
    if iid is None:
        return None
    _emit_event("soak_manifest_enqueued", {
        "intent_id": iid, "target": target, "chunk_count": len(chunks), "kind": kind,
    })
    return {"intent_id": iid, "manifest": manifest, "chunk_count": len(chunks)}


# ---------------------------------------------------------------------------
# Atomic chunk commit — the per-file checkpoint
# ---------------------------------------------------------------------------


def mark_chunk_complete(
    conn: Optional[sqlite3.Connection],
    intent_id: str,
    chunk_id: str,
    *,
    result: str = "",
    now: Optional[float] = None,
) -> bool:
    """Atomically move ``chunk_id`` from ``pending_chunks`` to
    ``completed_chunks`` inside the intent row's manifest. Read-modify-write under
    ``BEGIN IMMEDIATE`` so concurrent committers serialize on SQLite's write lock.
    Idempotent (re-committing an already-completed hash returns ``True``). Returns
    ``True`` on a durable commit (or already-done), ``False`` on unknown chunk /
    error. Never raises."""
    if conn is None:
        return False
    when = time.time() if now is None else float(now)
    prev_isolation = getattr(conn, "isolation_level", "")
    try:
        ensure_intent_table(conn)
        try:
            conn.execute("PRAGMA busy_timeout=3000")
        except sqlite3.Error:
            pass
        conn.isolation_level = None  # manual transaction control
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                f"SELECT manifest_json FROM {_INTENT_TABLE} WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            manifest = deserialize_manifest(row[0] if row else None)
            if manifest is None:
                conn.execute("ROLLBACK")
                return False
            pending = manifest.get("pending_chunks", []) or []
            completed = manifest.get("completed_chunks", []) or []
            already = {str(c.get("chunk_id")) for c in completed if isinstance(c, dict)}
            if str(chunk_id) in already:
                conn.execute("ROLLBACK")   # idempotent no-op — already durable
                return True
            found = None
            remaining = []
            for c in pending:
                if isinstance(c, dict) and str(c.get("chunk_id")) == str(chunk_id):
                    found = c
                else:
                    remaining.append(c)
            if found is None:
                conn.execute("ROLLBACK")   # unknown hash — nothing moved
                return False
            entry = dict(found)
            entry["completed_ts"] = when
            entry["result"] = str(result)[:200]
            manifest["pending_chunks"] = remaining
            manifest["completed_chunks"] = list(completed) + [entry]
            conn.execute(
                f"UPDATE {_INTENT_TABLE} SET manifest_json=? WHERE intent_id=?",
                (serialize_manifest(manifest), intent_id),
            )
            conn.execute("COMMIT")
            return True
        except Exception:  # noqa: BLE001
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    except sqlite3.Error:
        logger.debug("[Checkpoint] mark_chunk_complete failed", exc_info=True)
        return False
    finally:
        try:
            conn.isolation_level = prev_isolation
        except Exception:  # noqa: BLE001
            pass


def read_manifest(
    conn: Optional[sqlite3.Connection], intent_id: str,
) -> Optional[dict]:
    """Parse the current manifest for an intent row (or ``None``). Never raises."""
    return deserialize_manifest(get_manifest_json(conn, intent_id))


# ---------------------------------------------------------------------------
# Checkpointed execution wrapper — the resume-aware map-reduce driver
# ---------------------------------------------------------------------------


# swarm_fn(chunk: dict) -> Awaitable[Any] — process ONE chunk (invoke the swarm on
# that file/symbol). Raising means "not done" → the chunk stays pending.
SwarmFn = Callable[[dict], Awaitable[Any]]


@dataclass
class CheckpointRunSummary:
    intent_id: str
    processed: List[str] = field(default_factory=list)   # chunk_ids done THIS run
    skipped: List[str] = field(default_factory=list)     # already-completed on entry
    failed: List[str] = field(default_factory=list)      # raised this run (stay pending)


async def run_checkpointed(
    conn: Optional[sqlite3.Connection],
    intent_id: str,
    swarm_fn: SwarmFn,
) -> CheckpointRunSummary:
    """Resume-aware execution: read the manifest, process ONLY the not-yet-done
    chunks (:func:`resume_pending`), and atomically commit each the instant its
    ``swarm_fn`` completes. A crash mid-run leaves every already-committed chunk
    durable; the next arm resumes from exactly here. The map-reduce driver that
    makes the Swarm functionally immortal. Never raises out."""
    summary = CheckpointRunSummary(intent_id=intent_id)
    manifest = read_manifest(conn, intent_id)
    if manifest is None:
        return summary
    summary.skipped = sorted(completed_ids(manifest))
    pending = resume_pending(manifest)
    total = len(summary.skipped) + len(pending)
    done = len(summary.skipped)
    # Resume breadcrumb — the operator sees exactly where the crash left off.
    _emit_event("soak_resumed", {
        "intent_id": intent_id, "total": total,
        "done": done, "remaining": len(pending),
    })
    for chunk in pending:
        cid = str(chunk.get("chunk_id"))
        try:
            result = await swarm_fn(chunk)
        except Exception as exc:  # noqa: BLE001 — an isolated chunk failure stays pending
            logger.debug("[Checkpoint] chunk %s failed: %s", cid, exc)
            summary.failed.append(cid)
            continue
        if mark_chunk_complete(conn, intent_id, cid, result=str(result) if result is not None else ""):
            summary.processed.append(cid)
            done += 1
            # Per-chunk tick — the map-reduce progress made visible live.
            _emit_event("soak_chunk_committed", {
                "intent_id": intent_id, "chunk_id": cid,
                "symbol": chunk.get("symbol", "?"), "file_path": chunk.get("file_path", "?"),
                "done": done, "total": total,
            })
        else:
            summary.failed.append(cid)
    _emit_event("soak_run_complete", {
        "intent_id": intent_id, "processed": len(summary.processed),
        "failed": len(summary.failed), "skipped": len(summary.skipped), "total": total,
    })
    return summary


def build_checkpointed_swarm_fn(
    *,
    client: Any,
    repo_root: str = ".",
    op_id_prefix: str = "soak",
) -> SwarmFn:
    """Bind a manifest chunk → the REAL big-file swarm egress
    (``intercept_full_content``, the same entry the generation hot path uses),
    constructing the ``ProductionAgentTurnFn`` identically. This is the DRY seam
    ``run_checkpointed`` drives per chunk — no cloned swarm logic. A chunk's
    ``file_path`` + ``symbol`` become the swarm's target file + symbol."""

    async def _swarm_fn(chunk: dict) -> Any:
        from backend.core.ouroboros.governance.full_content_interceptor import (
            intercept_full_content,
        )
        from backend.core.ouroboros.governance.agent_turn_adapter import (
            ProductionAgentTurnFn,
        )
        file_path = str(chunk.get("file_path", ""))
        symbol = str(chunk.get("symbol", ""))
        op_id = f"{op_id_prefix}-{chunk.get('chunk_id', '')}"
        try:
            with open(os.path.join(repo_root, file_path), "r",
                      encoding="utf-8", errors="replace") as fh:
                source = fh.read()
        except OSError:
            source = ""
        agent = ProductionAgentTurnFn(
            client=client, tool_backend=None, repo_root=repo_root, op_id=op_id,
            model_name=getattr(client, "_model", "") or "", system_prompt="",
            parse_fn=lambda raw: None, max_turns=1,
        )
        return await intercept_full_content(
            source, file_path, [symbol] if symbol and symbol != "<module>" else [],
            agent, op_id=op_id,
        )

    return _swarm_fn


async def run_pending_soak(
    conn: Optional[sqlite3.Connection],
    intent_id: str,
    *,
    client: Any,
    repo_root: str = ".",
) -> CheckpointRunSummary:
    """Convenience: resume-run a pending intent's manifest against the REAL swarm.
    The concrete, checkpoint-aware soak launch an AWE ``launch_fn`` / soak driver
    invokes — every chunk commits atomically, so it is crash-resumable. Never
    raises out."""
    swarm_fn = build_checkpointed_swarm_fn(client=client, repo_root=repo_root)
    return await run_checkpointed(conn, intent_id, swarm_fn)


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "ChunkDescriptor",
    "CheckpointRunSummary",
    "build_checkpointed_swarm_fn",
    "run_pending_soak",
    "build_manifest",
    "chunk_id_for",
    "completed_ids",
    "deserialize_manifest",
    "enqueue_soak_manifest",
    "mark_chunk_complete",
    "read_manifest",
    "resume_pending",
    "run_checkpointed",
    "serialize_manifest",
    "walk_target",
]
