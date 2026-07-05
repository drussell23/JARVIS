"""Domain-1 Staging-1 Task 1 -- StructuralDeltaSensor (Body publisher).

The transport hop. Staging-0 built the Body-local AST structural-delta engine
(``structural_delta.py``); this sensor is the PUBLISHER: it computes a
content-free structural delta, stamps it with git lineage + the durable emit
sequence, and publishes it as ``causal.delta.<repo>`` over the proven
Stage-2/3/4 ``TrinityEventBus`` bridge.

Design commitments:

* **No new durability code.** The sensor only publishes; the Stage-3
  ``DurableOutbound`` WAL on the same broker journals the event across a severed
  link. Durability is INHERITED (proven by the ``_journal_local_origin_only``
  test), not re-coded here.
* **Reflective repo validation.** ``repo`` is validated at construction via
  ``RepoType(repo)`` -- the enum IS the allowlist. A non-trinity repo raises at
  construction time; there is NO hardcoded string allowlist.
* **Explicit source identity.** The event is built with ``source=RepoType(repo)``
  and published via ``publish(event)`` (NOT ``publish_raw``): ``publish_raw``
  has no ``source`` kwarg -- it derives source from ``data["source"]`` and would
  force us to pollute the content-free envelope. Building the ``TrinityEvent``
  directly is the only path that yields ``event.source == RepoType(repo)`` while
  keeping ``event.payload`` byte-identical to the ``stamp_delta`` envelope, so
  the fingerprint + the Brain's reflective read see the authoritative repo.
* **Fail-soft, non-blocking.** ``emit_file_change`` never raises (log + return
  ``None`` on any error) and awaits exactly one ``publish`` -- no sleeps, no
  retries.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from backend.core.trinity_event_bus import RepoType, TrinityEvent
from backend.core.ouroboros.governance.causal.structural_delta import (
    DeltaLineage,
    EmitSequence,
    StructuralDelta,
    compute_file_delta,
    stamp_delta,
)

logger = logging.getLogger(__name__)

CAUSAL_DELTA_TOPIC_PREFIX = "causal.delta."

# Lazily-constructed module-level emit sequence -- used only when the caller
# does not inject one. Never touched under test (tests inject a fake).
_MODULE_EMIT_SEQ: Optional[EmitSequence] = None


def _module_emit_seq() -> EmitSequence:
    global _MODULE_EMIT_SEQ
    if _MODULE_EMIT_SEQ is None:
        _MODULE_EMIT_SEQ = EmitSequence()
    return _MODULE_EMIT_SEQ


@dataclass(frozen=True)
class GitLineage:
    """The git-read SHAs, injectable so unit tests need no real repo.

    Staging 1 does NOT read git -- the sensor accepts these already-resolved
    SHAs. The actual git reader is a later concern.
    """

    head_sha: str
    parent_sha: str
    merge_base: str


class StructuralDeltaSensor:
    """Compute a structural delta and publish it as ``causal.delta.<repo>``."""

    def __init__(
        self,
        trinity_bus: Any,
        *,
        repo: str,
        emit_seq: Optional[EmitSequence] = None,
        sha_reader: Optional[Callable[[str], "GitLineage"]] = None,
    ) -> None:
        # Reflective validation: RepoType(repo) raises ValueError on an unknown
        # repo -- the enum IS the allowlist. Re-raise with a clear message so a
        # non-trinity repo can never construct a sensor.
        try:
            self._repo_enum = RepoType(repo)
        except ValueError as exc:
            raise ValueError(
                "StructuralDeltaSensor: refusing to construct for non-trinity "
                "repo %r (not a RepoType)" % (repo,)
            ) from exc

        # BROADCAST is a TARGET semantic, not a valid SOURCE identity: a causal
        # delta must originate from ONE concrete repo. Reflective enum-member
        # check (no hardcoded string set).
        if self._repo_enum == RepoType.BROADCAST:
            raise ValueError(
                "StructuralDeltaSensor requires a concrete source repo, not "
                "BROADCAST"
            )

        self._bus = trinity_bus
        self._repo = repo
        self._emit_seq = emit_seq
        self._sha_reader = sha_reader

    # -- helpers -----------------------------------------------------------

    def _seq(self) -> EmitSequence:
        return self._emit_seq if self._emit_seq is not None else _module_emit_seq()

    @staticmethod
    def _has_structural_change(delta: StructuralDelta) -> bool:
        """True iff there is something to publish: a file-level churn collapse
        OR any non-empty per-symbol/per-edge tuple."""
        if delta.file_level_churn:
            return True
        return bool(
            delta.symbols_added
            or delta.symbols_removed
            or delta.symbols_resignatured
            or delta.import_edges_added
            or delta.import_edges_removed
        )

    # -- publish -----------------------------------------------------------

    async def emit_file_change(
        self,
        file_path: str,
        before_source: str,
        after_source: str,
        *,
        lineage: "GitLineage",
    ) -> Optional[str]:
        """Compute -> stamp -> publish. Returns the event_id, or ``None`` when
        nothing structural changed. Fail-soft: never raises."""
        try:
            delta = compute_file_delta(
                self._repo, file_path, before_source, after_source
            )
            if not self._has_structural_change(delta):
                # Nothing structural crossed -- do not publish noise.
                return None

            seq = self._seq().next(self._repo)
            envelope = stamp_delta(
                delta,
                DeltaLineage(
                    repo=self._repo,
                    head_sha=lineage.head_sha,
                    parent_sha=lineage.parent_sha,
                    merge_base=lineage.merge_base,
                    emit_seq=seq,
                ),
            )

            # Build the event explicitly so source == RepoType(repo) and the
            # payload stays byte-identical to the content-free envelope.
            event = TrinityEvent(
                topic=CAUSAL_DELTA_TOPIC_PREFIX + self._repo,
                source=self._repo_enum,
                payload=envelope,
                correlation_id=lineage.head_sha,
                causation_id=lineage.parent_sha,
            )
            return await self._bus.publish(event)
        except Exception as exc:  # noqa: BLE001 -- fail-soft by contract
            logger.error(
                "[StructuralDeltaSensor] emit_file_change failed for %s (%r) "
                "-- fail-soft, no publish",
                file_path,
                exc,
            )
            return None
