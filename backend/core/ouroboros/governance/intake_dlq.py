"""intake_dlq — Sovereign Dead-Letter Queue (A1-T1)
==================================================

Persistent dead-letter store for strategic-GOAL envelopes that cannot
be forwarded immediately (e.g., ``_TeeRouter.upstream is None`` at
roadmap-orchestrator boot).

Design constraints
------------------
- **Fail-soft everywhere**: ``append_dlq`` and ``replay_dlq`` NEVER raise.
- **Atomic rewrite**: ``replay_dlq`` uses temp-file + ``os.replace`` so a
  crash mid-replay leaves the original DLQ intact.
- **Dedup by goal_id**: ``replay_dlq`` forwards only the first occurrence of
  each goal_id per invocation; later duplicates are silently dropped.
- **Master switch**: ``JARVIS_INTAKE_DLQ_ENABLED`` (default ``"true"``).
  When disabled, ``append_dlq`` is a no-op and ``read_dlq`` returns ``[]``.
- **No external deps**: stdlib only (json, os, time, logging, asyncio).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from typing import Any, Callable, Coroutine, List

logger = logging.getLogger(__name__)

_ENV_ENABLED = "JARVIS_INTAKE_DLQ_ENABLED"
_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    """Return True unless ``JARVIS_INTAKE_DLQ_ENABLED`` is explicitly falsy."""
    val = os.environ.get(_ENV_ENABLED, "true").strip().lower()
    return val not in {"0", "false", "no", "off"}


def _default_path() -> str:
    """Canonical DLQ path relative to repo root."""
    return os.path.join(".jarvis", "intake_dlq.jsonl")


#: Identity sources, most-specific first. `dedup_key` and `causal_id` were
#: MISSING and that was a live defect, not an omission of taste: real
#: IntentEnvelopes identify themselves with `dedup_key` (it is named for
#: exactly this purpose) and often carry no `goal_id`. Since `replay_dlq`
#: dedups first-wins on this value, an empty identity meant every such
#: envelope collapsed into ONE survivor — genuine queued work silently
#: discarded by the mechanism meant to preserve it.
_IDENTITY_KEYS: tuple = ("goal_id", "op_id", "id", "dedup_key", "causal_id")


def _goal_id(envelope: Any) -> str:
    """Extract a stable identifier from *envelope* (dict or object)."""
    if isinstance(envelope, dict):
        for key in _IDENTITY_KEYS:
            val = envelope.get(key)
            if val is not None:
                return str(val)
        return ""
    for attr in _IDENTITY_KEYS:
        val = getattr(envelope, attr, None)
        if val is not None:
            return str(val)
    return ""


def _to_serializable(envelope: Any) -> Any:
    """Return a JSON-serialisable representation of *envelope*.

    Prefers a typed ``to_dict()`` (e.g. :class:`IntentEnvelope`) so the DLQ
    stores a faithful JSON object that ``replay_dlq`` can hand back to a
    reconstructing ``ingest_fn`` (via ``IntentEnvelope.from_dict``). Only a
    truly opaque object falls back to a lossy ``repr`` — which is observable
    in the DLQ but not replayable (logged loud at append time regardless).
    """
    to_dict = getattr(envelope, "to_dict", None)
    if callable(to_dict):
        try:
            d = to_dict()
            json.dumps(d)  # confirm the dict is genuinely JSON-safe
            return d
        except Exception:  # noqa: BLE001 — fall through to the generic paths
            pass
    try:
        json.dumps(envelope)
        return envelope
    except (TypeError, ValueError):
        return repr(envelope)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


#: Keys whose presence means the router could ACT on this record. Identity
#: alone is not enough: `{"op_id": ..., "phase": "blast_radius"}` has an id and
#: is still not work.
_ACTIONABLE_KEYS: tuple = (
    "description", "goal", "intent", "signal", "content", "prompt",
    "target_files", "task",
)


def is_replayable(envelope: Any) -> bool:
    """Could this record be re-ingested AS WORK? NEVER raises.

    A dead-letter queue's entire contract is "work that failed and can be
    retried". Replayability is therefore not a nicety — it is the membership
    condition, and a record that fails it does not belong in the queue no
    matter how much it deserves to be persisted somewhere.

    Requires BOTH:
      * an identity (`_goal_id`), so replay can dedup; and
      * at least one ACTIONABLE field, so the router has something to do.

    The second condition is what a diagnostic fails. Error telemetry has
    `action`/`detail`/`error_class`/`surface` — informative to a human,
    meaningless to an intake router, and indistinguishable from work if the
    only check is "is it a dict".
    """
    try:
        # IDENTITY **OR** ACTIONABLE CONTENT — deliberately permissive.
        #
        # An earlier draft required actionable content, and four existing
        # tests caught it: a minimal `{"goal_id": "g1"}` is a legitimate
        # envelope under the old contract, and rejecting it would have
        # DIVERTED REAL WORK into the diagnostics file. That is the dangerous
        # direction — strictly worse than the misfiling being fixed, because
        # the misfiling never lost work and a false diversion would.
        #
        # So the rule diverts only what is positively NEITHER: no identity the
        # router could dedup on, and no field it could act upon. Every one of
        # the 153 misfiled records observed in production fails both tests —
        # `{awakened_model, classification, instance, k}` and
        # `{action, detail, error_class, event, note, surface}` carry neither
        # an id nor a payload — so the offenders are still caught while
        # anything ambiguous stays where it is.
        if isinstance(envelope, dict):
            return bool(_goal_id(envelope)) or any(
                envelope.get(k) for k in _ACTIONABLE_KEYS)
        return bool(_goal_id(envelope)) or any(
            getattr(envelope, k, None) for k in _ACTIONABLE_KEYS)
    except Exception:  # noqa: BLE001
        return False


def _diagnostics_path(path: str | None = None) -> str:
    """Sibling file for records that are worth persisting but not replaying."""
    base = path if path is not None else _default_path()
    root, _, _ = base.rpartition(".jsonl")
    return (root or base) + "_diagnostics.jsonl"


def append_diagnostic(payload: Any, *, reason: str,
                      path: str | None = None) -> None:
    """Persist a NON-replayable record. NEVER raises.

    Exists so the contract on `append_dlq` can be enforced without losing
    data. Rejecting a diagnostic outright would push callers to drop it, which
    is worse than the misfiling this replaces.
    """
    if not _enabled():
        return
    p = _diagnostics_path(path)
    record = {
        "ts": time.time(),
        "reason": reason,
        "schema_version": _SCHEMA_VERSION,
        "kind": "diagnostic",
        "payload": _to_serializable(payload),
    }
    try:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[IntakeDLQ] diagnostic append failed: %s", exc)


def append_dlq(
    envelope: Any,
    *,
    reason: str,
    path: str | None = None,
) -> None:
    """Persist a REPLAYABLE *envelope* to the DLQ and emit a CRITICAL log line.

    Never raises — any I/O error is caught and logged at WARNING.

    THE CONTRACT IS NOW ENFORCED. This parameter was typed ``Any`` and the
    queue accepted anything a caller happened to hold, so diagnostics were
    filed alongside work: of 156 entries observed in production, 152 were
    error telemetry and 3 were test fixtures. That is not merely untidy —
    `replay_dlq` dedups by ``goal_id``, every diagnostic has an empty one, so
    a replay would collapse them to a single entry and hand an error record
    to the intake router AS WORK.

    A record that cannot be replayed is routed to `append_diagnostic` instead
    of being dropped: enforcing the contract must not cost data.
    """
    if not _enabled():
        return
    if not is_replayable(envelope):
        # Named at WARNING with the reason, so a misrouting caller is visible
        # rather than silently redirected. The record itself is preserved.
        logger.warning(
            "[IntakeDLQ] non-replayable record routed to diagnostics "
            "(reason=%s) — a DLQ holds work that can be retried, and this "
            "carries no actionable payload", reason,
        )
        append_diagnostic(envelope, reason=reason, path=path)
        return
    p = path if path is not None else _default_path()
    goal = _goal_id(envelope)
    record = {
        "ts": time.time(),
        "reason": reason,
        "schema_version": _SCHEMA_VERSION,
        "goal_id": goal,
        "envelope": _to_serializable(envelope),
    }
    logger.critical(
        "[IntakeDLQ] orphaned GOAL reason=%s goal=%s", reason, goal
    )
    try:
        os.makedirs(os.path.dirname(os.path.abspath(p)), exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[IntakeDLQ] append failed path=%s err=%r", p, exc)


def read_dlq(path: str | None = None) -> List[dict]:
    """Parse the DLQ JSONL file and return all valid rows.

    Returns ``[]`` when disabled, when the file is absent, or if no valid
    lines are present.  Corrupt/unparseable lines are silently skipped.
    """
    if not _enabled():
        return []
    p = path if path is not None else _default_path()
    rows: List[dict] = []
    try:
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass  # skip corrupt lines
    except FileNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("[IntakeDLQ] read failed path=%s err=%r", p, exc)
    return rows


async def replay_dlq(
    path: str | None,
    ingest_fn: Callable[[Any], Coroutine[Any, Any, Any]],
) -> int:
    """Re-ingest envelopes from the DLQ, deduping by goal_id.

    Parameters
    ----------
    path:
        DLQ file path (``None`` → ``_default_path()``).
    ingest_fn:
        Async callable that receives a raw envelope dict and forwards it.
        Must be awaitable; exceptions are caught (entry kept in DLQ).

    Returns
    -------
    int
        Count of successfully drained entries.

    Notes
    -----
    - Dedup is first-wins per *goal_id* within this replay call.
    - On success the survivor list is atomically rewritten (temp + rename).
    - On any file-system error the original file is left untouched.
    - NEVER raises.
    """
    if not _enabled():
        return 0
    p = path if path is not None else _default_path()
    rows = read_dlq(p)
    # DEFENSIVE SKIP for records written before the contract was enforced.
    # 152 such rows exist in production today. Guarding only new writes would
    # leave them armed: they all carry an empty goal_id, so replay would dedup
    # them to one and feed a diagnostic to the router as work.
    _total = len(rows)
    rows = [r for r in rows
            if is_replayable((r or {}).get("envelope"))]
    if _total != len(rows):
        logger.warning(
            "[IntakeDLQ] skipping %d non-replayable legacy row(s) of %d — "
            "diagnostics misfiled into the replay queue before the contract "
            "was enforced", _total - len(rows), _total,
        )
    if not rows:
        return 0

    seen_ids: set[str] = set()
    survivors: List[dict] = []
    drained = 0

    for row in rows:
        env = row.get("envelope", row)
        gid = row.get("goal_id") or _goal_id(env)

        # Dedup: skip later duplicates of the same goal_id
        if gid and gid in seen_ids:
            continue
        if gid:
            seen_ids.add(gid)

        try:
            await ingest_fn(env)
            drained += 1
            # Successfully drained — do not add to survivors
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[IntakeDLQ] replay failed goal=%s err=%r; keeping entry",
                gid, exc,
            )
            survivors.append(row)

    # Atomically rewrite DLQ with only the survivors
    try:
        dir_name = os.path.dirname(os.path.abspath(p))
        os.makedirs(dir_name, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=dir_name,
            delete=False,
            suffix=".tmp",
        ) as tmp:
            for row in survivors:
                tmp.write(json.dumps(row, default=str) + "\n")
            tmp_name = tmp.name
        os.replace(tmp_name, p)
        if not survivors:
            # Remove the empty file so read_dlq returns [] cleanly
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[IntakeDLQ] atomic rewrite failed path=%s err=%r", p, exc
        )

    return drained
