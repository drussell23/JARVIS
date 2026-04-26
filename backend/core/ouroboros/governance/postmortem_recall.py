"""Postmortem Recall Service — P0 of OUROBOROS_VENOM_PRD.md Phase 1.

Closes the rooted "system has perfect memory and zero recall" gap from
the PRD §4.2 Shallow #2.

Today, POSTMORTEM messages get written to session debug.log + summary.json
+ ConversationBridge buffer — and then nothing reads them at the next op's
decision time. The system makes the same mistake, writes the same
postmortem, learns nothing.

This service provides a thin recall layer:

    recall = PostmortemRecallService(...)
    lessons = recall.recall_for_op(op_signature, top_k=3)
    if lessons:
        prompt += render_recall_section(lessons)

Architecture:
- Reads POSTMORTEM events from `.ouroboros/sessions/<id>/debug.log`
  files (matches existing comm_protocol output format).
- Computes similarity between current op signature and each prior
  postmortem using the SemanticIndex `_Embedder` (cosine similarity).
- Time-decays by halflife (default 30 days) so old postmortems matter
  less than recent ones.
- Returns top-k matches above a similarity threshold.

Authority preservation (per PRD §12.2):
- Read-only — never mutates code, never writes to git/, never opens
  approval surfaces, never invokes Iron Gate / risk-tier-floor / etc.
- Best-effort — any failure (no semantic index, no embedder, parse
  errors) returns an empty list. Caller renders nothing.
- Master flag default OFF (`JARVIS_POSTMORTEM_RECALL_ENABLED=true`
  to enable). Per PRD discipline.

Authority invariants (grep-pinned per PRD §11 Layer 1):
- Does NOT import: orchestrator, policy, iron_gate, risk_tier,
  change_engine, candidate_generator, gate, semantic_guardian.
- Read-only data access — only reads files; never writes (except its
  own JSONL ledger).
- Uses narrow regex parsing for postmortem payload — does NOT call
  ``ast.literal_eval`` or any code-evaluation function (per security
  invariant).

PRD reference: §9 Phase 1 P0.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


logger = logging.getLogger("Ouroboros.PostmortemRecall")


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        return max(minimum, v)
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """Master flag — `JARVIS_POSTMORTEM_RECALL_ENABLED` (default ``false``).

    Default-off until graduation cadence (3 clean live sessions per PRD §11
    Layer 4). Master-off → service is a no-op; never queries postmortems,
    never logs.
    """
    return _env_bool("JARVIS_POSTMORTEM_RECALL_ENABLED", False)


def top_k() -> int:
    """`JARVIS_POSTMORTEM_RECALL_TOP_K` — default ``3``.

    Maximum number of past postmortems to inject into a single prompt.
    PRD §9 P0 specifies "up to 3 relevant lessons". Tunable for ops
    where deeper recall might help (or shallower for cost/context
    discipline)."""
    return _env_int("JARVIS_POSTMORTEM_RECALL_TOP_K", 3, minimum=0)


def decay_days() -> float:
    """`JARVIS_POSTMORTEM_RECALL_DECAY_DAYS` — default ``30.0``.

    Half-life for postmortem relevance. Older postmortems get weighted
    less in the similarity score (multiplied by 2^(-age_days/halflife)).
    Operator binding 2026-04-25 (PRD §16): default 30d, env-tunable.
    """
    return _env_float("JARVIS_POSTMORTEM_RECALL_DECAY_DAYS", 30.0, minimum=0.1)


def similarity_threshold() -> float:
    """`JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD` — default ``0.5``.

    Below this cosine similarity, postmortems are filtered out. Prevents
    spurious injection when there's no meaningful match. 0.5 is
    conservative; tighten to 0.7 in HARDEN posture for noise reduction.
    """
    return _env_float("JARVIS_POSTMORTEM_RECALL_SIM_THRESHOLD", 0.5, minimum=0.0)


def max_postmortems_to_scan() -> int:
    """`JARVIS_POSTMORTEM_RECALL_MAX_SCAN` — default ``500``.

    Hard ceiling on how many recent postmortems get embedded + scored.
    Prevents unbounded scan-time on long-running deployments.
    """
    return _env_int("JARVIS_POSTMORTEM_RECALL_MAX_SCAN", 500, minimum=1)


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PostmortemRecord:
    """One postmortem extracted from a session debug.log."""

    op_id: str
    session_id: str
    root_cause: str
    failed_phase: str
    next_safe_action: str
    target_files: tuple
    timestamp_iso: str
    timestamp_unix: float

    def signature_text(self) -> str:
        """Build the embeddable text representation for similarity scoring."""
        files_text = ", ".join(sorted(self.target_files))[:300]
        return (
            f"phase={self.failed_phase} | root_cause={self.root_cause[:200]} | "
            f"files={files_text}"
        )

    def lesson_text(self) -> str:
        """Render a one-line lesson suitable for prompt injection."""
        files_summary = ", ".join(sorted(self.target_files)[:3])
        if len(self.target_files) > 3:
            files_summary += f" (+{len(self.target_files) - 3} more)"
        next_action = (
            f" — next-safe-action: {self.next_safe_action}"
            if self.next_safe_action and self.next_safe_action != "none"
            else ""
        )
        return (
            f"op={self.op_id[:16]} failed at {self.failed_phase} "
            f"because: {self.root_cause[:150]} (files: {files_summary}){next_action}"
        )


@dataclass(frozen=True)
class RecallMatch:
    """A scored postmortem match for the current op."""

    record: PostmortemRecord
    raw_similarity: float
    decayed_similarity: float
    age_days: float

    def to_ledger_dict(self) -> Dict[str, Any]:
        """JSONL-serializable representation for postmortem_recall_history.jsonl."""
        return {
            "schema_version": "postmortem_recall.1",
            "op_id": self.record.op_id,
            "session_id": self.record.session_id,
            "failed_phase": self.record.failed_phase,
            "root_cause": self.record.root_cause[:300],
            "raw_similarity": round(self.raw_similarity, 4),
            "decayed_similarity": round(self.decayed_similarity, 4),
            "age_days": round(self.age_days, 2),
            "matched_at_iso": datetime.now(tz=timezone.utc).isoformat(),
        }


# ---------------------------------------------------------------------------
# Postmortem extraction (narrow regex parser — never calls ast/eval)
# ---------------------------------------------------------------------------


# Matches: 2026-04-25T01:08:13 [...comm_protocol] INFO [CommProtocol] POSTMORTEM op=op-019dc... seq=N payload={...}
_POSTMORTEM_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+"
    r"\[.*comm_protocol.*\]\s+\w+\s+\[CommProtocol\]\s+POSTMORTEM\s+"
    r"op=(?P<op_id>[a-zA-Z0-9_\-]+)\s+seq=\d+\s+payload=(?P<payload>.+)$"
)

# Narrow extractors for each payload field. Avoid ast.literal_eval (security
# hook flags it; also overkill for our 4 known string/list fields).
# Format is Python repr() — single quotes, with True/False/None literals.
_PAYLOAD_FIELD_STR_RE = re.compile(
    r"['\"]({field})['\"]\s*:\s*['\"]([^'\"]*?)['\"](?=\s*[,}}])"
)
_PAYLOAD_FIELD_LIST_RE = re.compile(
    r"['\"]({field})['\"]\s*:\s*\[([^\]]*)\]"
)


def _extract_payload_str(payload: str, field: str) -> str:
    """Extract a single string-typed field value from a Python-repr dict.
    Returns empty string if not found."""
    pattern = re.compile(
        r"['\"]" + re.escape(field) + r"['\"]\s*:\s*['\"]([^'\"]*?)['\"]"
    )
    m = pattern.search(payload)
    return m.group(1).strip() if m else ""


def _extract_payload_list(payload: str, field: str) -> List[str]:
    """Extract a string-list-typed field value from a Python-repr dict.
    Returns empty list if not found."""
    pattern = re.compile(
        r"['\"]" + re.escape(field) + r"['\"]\s*:\s*\[([^\]]*)\]"
    )
    m = pattern.search(payload)
    if not m:
        return []
    inner = m.group(1)
    # Split on commas; strip whitespace + surrounding quotes.
    items = []
    for part in inner.split(","):
        cleaned = part.strip().strip("'\"")
        if cleaned:
            items.append(cleaned)
    return items


def _parse_postmortem_line(
    line: str, session_id: str,
) -> Optional[PostmortemRecord]:
    """Parse one debug.log line into a PostmortemRecord, or None on miss.

    Defensive parsing — comm_protocol uses Python repr() for payload
    serialization (NOT JSON). We use narrow regex extractors for each
    field rather than ast.literal_eval (security hook + overkill).
    Skips any line that doesn't parse cleanly.
    """
    m = _POSTMORTEM_LINE_RE.match(line.strip())
    if m is None:
        return None
    payload = m.group("payload").strip()
    if not payload.startswith("{"):
        return None

    root_cause = _extract_payload_str(payload, "root_cause")
    failed_phase = _extract_payload_str(payload, "failed_phase")
    next_safe_action = _extract_payload_str(payload, "next_safe_action")
    target_files = _extract_payload_list(payload, "target_files")

    ts_raw = m.group("ts")
    try:
        ts_dt = datetime.strptime(ts_raw, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        ts_unix = ts_dt.timestamp()
    except (ValueError, TypeError):
        return None

    return PostmortemRecord(
        op_id=str(m.group("op_id")),
        session_id=session_id,
        root_cause=root_cause,
        failed_phase=failed_phase,
        next_safe_action=next_safe_action,
        target_files=tuple(target_files),
        timestamp_iso=ts_dt.isoformat(),
        timestamp_unix=ts_unix,
    )


def _scan_session_debug_log(
    debug_log: Path, session_id: str, limit: int,
) -> List[PostmortemRecord]:
    """Extract POSTMORTEM records from a single session debug.log."""
    records: List[PostmortemRecord] = []
    if not debug_log.exists():
        return records
    try:
        with debug_log.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "POSTMORTEM" not in line:
                    continue
                rec = _parse_postmortem_line(line, session_id=session_id)
                if rec is None:
                    continue
                # Skip "root_cause=none" — those are clean COMPLETE ops,
                # not failures with lessons. (Per ConversationBridge
                # filter at format_postmortem_payload.)
                if rec.root_cause.lower() in ("", "none"):
                    continue
                records.append(rec)
                if len(records) >= limit:
                    break
    except Exception:  # noqa: BLE001
        logger.debug(
            "[PostmortemRecall] scan failed for %s — best-effort skip",
            debug_log, exc_info=True,
        )
    return records


def _gather_recent_postmortems(
    sessions_dir: Path, max_total: int,
) -> List[PostmortemRecord]:
    """Walk sessions newest-first, accumulate postmortems up to max_total."""
    all_records: List[PostmortemRecord] = []
    if not sessions_dir.exists() or not sessions_dir.is_dir():
        return all_records
    # Newest sessions first (lexicographic on bt-YYYY-MM-DD-HHMMSS).
    session_dirs = sorted(
        [p for p in sessions_dir.iterdir() if p.is_dir() and p.name.startswith("bt-")],
        reverse=True,
    )
    per_session_cap = max(1, max_total // max(1, min(20, len(session_dirs))))
    for session_dir in session_dirs:
        debug_log = session_dir / "debug.log"
        recs = _scan_session_debug_log(
            debug_log, session_id=session_dir.name, limit=per_session_cap,
        )
        all_records.extend(recs)
        if len(all_records) >= max_total:
            break
    return all_records[:max_total]


# ---------------------------------------------------------------------------
# Similarity + recall
# ---------------------------------------------------------------------------


def _decay_factor(age_seconds: float, halflife_days: float) -> float:
    """Standard half-life decay: 2^(-age_days/halflife)."""
    age_days = age_seconds / 86400.0
    if halflife_days <= 0:
        return 1.0
    return float(2.0 ** (-age_days / halflife_days))


class PostmortemRecallService:
    """Recall prior postmortems similar to the current op for prompt injection.

    Construction is cheap; embedding happens lazily on first recall_for_op().

    Parameters
    ----------
    sessions_dir:
        Path to ``.ouroboros/sessions/`` (where battle-test sessions
        write their debug.log + summary.json).
    semantic_index:
        Optional pre-constructed ``SemanticIndex`` instance. Reserved
        for future use; currently lazy-loads the embedder directly.
    ledger_path:
        Optional path to write the recall history JSONL. Defaults to
        ``.jarvis/postmortem_recall_history.jsonl``.
    """

    def __init__(
        self,
        sessions_dir: Path,
        semantic_index: Any = None,
        ledger_path: Optional[Path] = None,
    ) -> None:
        self._sessions_dir = Path(sessions_dir)
        self._semantic_index = semantic_index
        self._ledger_path = ledger_path or Path(".jarvis/postmortem_recall_history.jsonl")
        self._embedder: Optional[Any] = None  # lazy

    def _ensure_embedder(self) -> Optional[Any]:
        """Lazy-init the embedder. Returns None if unavailable (best-effort)."""
        if self._embedder is not None:
            return self._embedder
        try:
            from backend.core.ouroboros.governance.semantic_index import (
                _Embedder as _SemanticEmbedder,
                _embedder_name as _emb_name,
            )
            emb = _SemanticEmbedder(model_name=_emb_name())
            if emb.disabled:
                return None
            self._embedder = emb
            return emb
        except Exception:  # noqa: BLE001
            logger.debug(
                "[PostmortemRecall] embedder lazy-init failed", exc_info=True,
            )
            return None

    def recall_for_op(
        self,
        op_signature: str,
        top_k_override: Optional[int] = None,
    ) -> List[RecallMatch]:
        """Find prior postmortems similar to ``op_signature``."""
        if not is_enabled():
            return []
        if not op_signature or not op_signature.strip():
            return []
        try:
            return self._recall_inner(op_signature, top_k_override)
        except Exception:  # noqa: BLE001
            logger.debug(
                "[PostmortemRecall] recall_for_op failed — returning []",
                exc_info=True,
            )
            return []

    def _recall_inner(
        self,
        op_signature: str,
        top_k_override: Optional[int],
    ) -> List[RecallMatch]:
        embedder = self._ensure_embedder()
        if embedder is None:
            logger.debug("[PostmortemRecall] no embedder available — returning []")
            return []

        records = _gather_recent_postmortems(
            self._sessions_dir, max_total=max_postmortems_to_scan(),
        )
        if not records:
            return []

        # Embed query + corpus in one batch for efficiency.
        texts = [op_signature] + [r.signature_text() for r in records]
        vectors = embedder.embed(texts)
        if vectors is None or len(vectors) != len(texts):
            logger.debug(
                "[PostmortemRecall] embedding returned %s vectors for %d texts — skip",
                "None" if vectors is None else len(vectors), len(texts),
            )
            return []

        from backend.core.ouroboros.governance.semantic_index import _cosine

        query_vec = vectors[0]
        corpus_vecs = vectors[1:]
        now_unix = time.time()
        threshold = similarity_threshold()
        halflife = decay_days()

        scored: List[RecallMatch] = []
        for rec, vec in zip(records, corpus_vecs):
            raw_sim = float(_cosine(query_vec, vec))
            age_seconds = max(0.0, now_unix - rec.timestamp_unix)
            decay = _decay_factor(age_seconds, halflife)
            decayed = raw_sim * decay
            if decayed < threshold:
                continue
            scored.append(RecallMatch(
                record=rec,
                raw_similarity=raw_sim,
                decayed_similarity=decayed,
                age_days=age_seconds / 86400.0,
            ))

        scored.sort(key=lambda m: m.decayed_similarity, reverse=True)
        k = top_k_override if top_k_override is not None else top_k()
        result = scored[:max(0, k)]
        if result:
            self._persist_to_ledger(op_signature, result)
            logger.info(
                "[PostmortemRecall] op_signature=%r matched %d postmortems "
                "(threshold=%.2f, top_k=%d, decayed_top=%.3f)",
                op_signature[:60], len(result), threshold, k,
                result[0].decayed_similarity,
            )
        return result

    def _persist_to_ledger(
        self,
        op_signature: str,
        matches: List[RecallMatch],
    ) -> None:
        """Append a recall event to the JSONL ledger. Best-effort."""
        try:
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "schema_version": "postmortem_recall.1",
                "ts_iso": datetime.now(tz=timezone.utc).isoformat(),
                "op_signature": op_signature[:300],
                "match_count": len(matches),
                "matches": [m.to_ledger_dict() for m in matches],
            }
            with self._ledger_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except Exception:  # noqa: BLE001
            logger.debug(
                "[PostmortemRecall] ledger write failed", exc_info=True,
            )


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def render_recall_section(matches: Sequence[RecallMatch]) -> Optional[str]:
    """Render recalled postmortems as a prompt section, or None if empty."""
    if not matches:
        return None
    lines = ["## Lessons from prior similar ops"]
    lines.append("")
    for m in matches:
        lines.append(f"- {m.record.lesson_text()}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Default-singleton accessor
# ---------------------------------------------------------------------------


_default_service: Optional[PostmortemRecallService] = None


def get_default_service(
    sessions_dir: Optional[Path] = None,
) -> Optional[PostmortemRecallService]:
    """Return the process-wide PostmortemRecallService.

    Lazily constructs on first call. Returns None if disabled (master
    flag off) so callers can short-circuit cleanly.
    """
    if not is_enabled():
        return None
    global _default_service
    if _default_service is None:
        sd = sessions_dir or Path(".ouroboros/sessions")
        _default_service = PostmortemRecallService(sessions_dir=sd)
    return _default_service


def reset_default_service() -> None:
    """Reset the singleton — for tests and config reload."""
    global _default_service
    _default_service = None


__all__ = [
    "PostmortemRecord",
    "RecallMatch",
    "PostmortemRecallService",
    "is_enabled",
    "top_k",
    "decay_days",
    "similarity_threshold",
    "max_postmortems_to_scan",
    "render_recall_section",
    "get_default_service",
    "reset_default_service",
]
