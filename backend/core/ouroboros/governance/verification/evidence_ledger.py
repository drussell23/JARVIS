"""Evidence read back from the ledger, because the carrier does not survive.

WHY THIS EXISTS
---------------
``OperationContext`` is a frozen dataclass whose docstring says it plainly:
"All mutations go through :meth:`advance` which returns a **new** instance."
``dataclasses.replace(ctx, ...)`` is called at more than ten sites across the
pipeline for entirely ordinary reasons — a provider override, a scoped target
list, a prefetch manifest, a blast token.

``evidence_capture`` stamps its findings with ``object.__setattr__``:

    object.__setattr__(ctx, "test_files_pre", inventory)

``test_files_pre``, ``test_files_post``, ``diff_text`` and
``target_files_post`` are **not declared fields**. ``replace()`` builds the
new instance from declared fields only, so every one of those stamps is
silently dropped at the next transition. Measured:

    after stamp     : ['a.py', 'b.py']
    after replace() : ABSENT

That is the whole reason 13,911 of 18,414 claims returned
INSUFFICIENT_EVIDENCE, and why exactly one of the three Priority A claims
worked: ``file_parses_after_change`` falls back to ``ctx.target_files``,
which IS a declared field and therefore survives. The other two have no
declared-field fallback, so they saw nothing, every time, for the entire
history of the ledger.

THE FIX IS NOT A BIGGER CONTEXT
-------------------------------
Adding these as declared fields would push a 3,469-entry test inventory and a
40 KB diff through a hash-chained object that is copied at every phase
transition — bloating the Merkle chain that exists to make replay
deterministic, to carry payloads that are not decisions.

Claims already solved this problem. A ``PropertyClaim`` is not carried on the
context either: it is RECORDED at PLAN and read back at COMPLETE by
``get_recorded_claims(op_id=...)``. Evidence is the other half of the same
transaction and gets the same lifecycle. A record keyed by ``op_id`` outlives
every copy of every context, survives a process restart, and is already the
thing the postmortem reads to know what was claimed.

So this module is the reader half, deliberately shaped like
``get_recorded_claims``: same session resolution, same JSONL walk, same
never-raises contract, same "last write wins" for a key seen twice.

WHAT IT READS
-------------
* ``evidence_snapshot`` — written by ``evidence_capture`` alongside each
  stamp. Merged across phases, later phases winning per key, because the
  post-APPLY snapshot of a file set is a better answer than the pre-PLAN one.

* ``provider_selection`` — already written on every GENERATE, carrying
  ``provider_name`` and ``model_id``. Nothing had to be added for this one:
  ``providers_used`` was derivable from the ledger all along, and the claim
  that needed it had no gatherer registered at all.

Authority invariants (mirrors the rest of the verification package):
  * Pure stdlib. No orchestrator / phase_runner / candidate_generator import.
  * NEVER raises out of any public function.
  * Read-only. This module never writes to the ledger.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger("Ouroboros.EvidenceLedger")

EVIDENCE_LEDGER_SCHEMA_VERSION = "evidence_ledger.v1"

#: The ledger record kind evidence snapshots are written under.
EVIDENCE_SNAPSHOT_KIND = "evidence_snapshot"

#: Already written on every GENERATE by the provider dispatch path. Read, not
#: introduced — 2,220 of these were sitting on this repository's ledger while
#: the claim that needed them evaluated INSUFFICIENT every single time.
PROVIDER_SELECTION_KIND = "provider_selection"

__all__ = [
    "EVIDENCE_LEDGER_SCHEMA_VERSION",
    "EVIDENCE_SNAPSHOT_KIND",
    "PROVIDER_SELECTION_KIND",
    "evidence_ledger_enabled",
    "recorded_evidence",
    "recorded_providers_used",
]


def evidence_ledger_enabled() -> bool:
    """``JARVIS_EVIDENCE_LEDGER_ENABLED`` (default ``true``).

    Off restores the previous behaviour exactly: gatherers see only the ctx,
    and a stamp lost to ``replace()`` stays lost. Kept as an escape hatch
    rather than a tuning knob — there is no reading of the evidence this
    makes WORSE, only readings it makes possible.
    """
    raw = os.environ.get("JARVIS_EVIDENCE_LEDGER_ENABLED", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _ledger_path(session_id: Optional[str] = None) -> Path:
    """The per-session decision ledger.

    Resolved through the same helper the postmortem reader uses so the two
    cannot disagree about which file holds this session's records — the
    failure mode where evidence is written to one path and read from another
    would look exactly like evidence that was never captured.
    """
    from backend.core.ouroboros.governance.verification.postmortem import (
        _ledger_path_for_session,
    )
    return _ledger_path_for_session(session_id)


def _iter_records(
    op_id: str,
    kind: str,
    session_id: Optional[str] = None,
):
    """Yield parsed ``output_repr`` payloads for one op and kind, in file
    order. NEVER raises."""
    safe_op = str(op_id or "").strip()
    if not safe_op:
        return
    try:
        path = _ledger_path(session_id)
    except Exception:  # noqa: BLE001 — resolution failed; nothing to read
        logger.debug("[EvidenceLedger] path unresolved", exc_info=True)
        return
    try:
        if not path.exists():
            return
    except OSError:
        return
    try:
        with path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    record = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, Mapping):
                    continue
                if record.get("op_id") != safe_op:
                    continue
                if record.get("kind") != kind:
                    continue
                payload = record.get("output_repr", "")
                if not isinstance(payload, str):
                    continue
                try:
                    parsed = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, Mapping):
                    yield record, parsed
    except OSError as exc:
        logger.debug("[EvidenceLedger] read failed at %s: %s", path, exc)
        return


def recorded_evidence(
    *,
    op_id: str,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Every evidence key recorded for ``op_id``, merged. NEVER raises.

    Later records win per key. An op stamps a test inventory at PLAN entry
    and again after APPLY; the post-APPLY snapshot is the one a post-hoc
    verdict wants, and it is written second. Merging rather than taking the
    last record whole matters because the two snapshots carry DIFFERENT keys
    — ``test_files_pre`` only exists in the first.
    """
    out: Dict[str, Any] = {}
    if not evidence_ledger_enabled():
        return out
    try:
        for _record, payload in _iter_records(
                op_id, EVIDENCE_SNAPSHOT_KIND, session_id):
            for key, value in payload.items():
                # A key recorded as null is an absence, not an answer.
                # Letting it overwrite a real earlier value would turn a
                # partial later snapshot into data loss.
                if value is None:
                    continue
                out[str(key)] = value
    except Exception:  # noqa: BLE001 — a reader must not break the verdict
        logger.debug("[EvidenceLedger] recorded_evidence degraded",
                     exc_info=True)
    return out


def recorded_providers_used(
    *,
    op_id: str,
    session_id: Optional[str] = None,
) -> Tuple[str, ...]:
    """Provider names this op actually dispatched to, in first-seen order.

    Derived from the ``provider_selection`` records the generate path already
    writes. This is what ``cost_contract_bg_op_did_not_use_claude`` needed:
    the evidence existed on the ledger for the entire history of the claim,
    and no gatherer had ever been registered to look for it.

    Order-preserving and de-duplicated: a BG op that retried the same
    provider twice used one provider, and a count would read as two.
    """
    seen: Dict[str, None] = {}
    if not evidence_ledger_enabled():
        return ()
    try:
        for _record, payload in _iter_records(
                op_id, PROVIDER_SELECTION_KIND, session_id):
            name = payload.get("provider_name")
            if not isinstance(name, str):
                continue
            cleaned = name.strip().lower()
            # A recorded selection with no provider is a dispatch that did
            # not happen. Counting it as a provider would let an op that
            # never called anyone fail a "did not use Claude" claim.
            if cleaned:
                seen.setdefault(cleaned, None)
    except Exception:  # noqa: BLE001
        logger.debug("[EvidenceLedger] recorded_providers_used degraded",
                     exc_info=True)
    return tuple(seen)
