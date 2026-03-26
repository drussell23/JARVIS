"""
OperationDialogue — Per-operation reasoning journal for ConsciousnessBridge.

Closes the "conversational context" gap. Records the full reasoning chain
for each governance operation: CLASSIFY reasoning, ROUTE decision, EXPAND
context, GENERATE results, VALIDATE outcomes. Queryable for future similar
operations so the organism can say "last time I did something like this,
I classified it as X and routed to Y."

Boundary Principle:
  Deterministic: Data recording, JSON persistence, lookup by domain key.
  Agentic: The INTERPRETATION of past dialogues is done by the model
  when the dialogue is injected into the generation prompt.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_DIALOGUES_PER_DOMAIN = int(
    os.environ.get("JARVIS_MAX_DIALOGUES_PER_DOMAIN", "10")
)
_PERSISTENCE_DIR = Path(
    os.environ.get(
        "JARVIS_DIALOGUE_DIR",
        str(Path.home() / ".jarvis" / "ouroboros" / "dialogues"),
    )
)


@dataclass
class DialogueEntry:
    """One phase in the operation's reasoning chain."""
    phase: str                 # CLASSIFY, ROUTE, EXPAND, GENERATE, VALIDATE, etc.
    timestamp: float
    reasoning: str             # WHY this decision was made
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OperationDialogueRecord:
    """Complete reasoning journal for one governance operation."""
    op_id: str
    domain_key: str
    description: str
    target_files: Tuple[str, ...]
    entries: List[DialogueEntry] = field(default_factory=list)
    outcome: str = ""          # "success", "failed", "cancelled"
    started_at: float = field(default_factory=time.time)
    completed_at: float = 0.0

    def add_entry(self, phase: str, reasoning: str, **data: Any) -> None:
        self.entries.append(DialogueEntry(
            phase=phase,
            timestamp=time.time(),
            reasoning=reasoning,
            data=data,
        ))

    def complete(self, outcome: str) -> None:
        self.outcome = outcome
        self.completed_at = time.time()


class OperationDialogueStore:
    """Persistent store of operation reasoning journals.

    Records the full dialogue for each governance operation, indexed
    by domain key. On future similar operations, the store provides
    past reasoning chains as context.

    This is the organism's "inner monologue" — not just what happened,
    but WHY each decision was made, retrievable by domain similarity.
    """

    def __init__(self, persistence_dir: Path = _PERSISTENCE_DIR) -> None:
        self._persistence_dir = persistence_dir
        self._dialogues: Dict[str, List[OperationDialogueRecord]] = defaultdict(list)
        self._active: Dict[str, OperationDialogueRecord] = {}
        self._load()

    def start_dialogue(
        self,
        op_id: str,
        domain_key: str,
        description: str,
        target_files: Tuple[str, ...],
    ) -> OperationDialogueRecord:
        """Start recording a new operation dialogue."""
        record = OperationDialogueRecord(
            op_id=op_id,
            domain_key=domain_key,
            description=description,
            target_files=target_files,
        )
        self._active[op_id] = record
        return record

    def get_active(self, op_id: str) -> Optional[OperationDialogueRecord]:
        return self._active.get(op_id)

    def complete_dialogue(self, op_id: str, outcome: str) -> None:
        """Complete and archive a dialogue."""
        record = self._active.pop(op_id, None)
        if record is None:
            return
        record.complete(outcome)
        self._dialogues[record.domain_key].append(record)
        # Prune old dialogues
        if len(self._dialogues[record.domain_key]) > _MAX_DIALOGUES_PER_DOMAIN:
            self._dialogues[record.domain_key] = \
                self._dialogues[record.domain_key][-_MAX_DIALOGUES_PER_DOMAIN:]
        self._persist()

    def get_past_dialogues(
        self, domain_key: str, limit: int = 3
    ) -> List[OperationDialogueRecord]:
        """Get past dialogues for a domain. Most recent first."""
        records = self._dialogues.get(domain_key, [])
        return list(reversed(records[-limit:]))

    def format_for_prompt(self, domain_key: str) -> str:
        """Format past dialogues as context for generation prompt."""
        past = self.get_past_dialogues(domain_key)
        if not past:
            return ""

        lines = [f"## Past Operation Reasoning for Domain: {domain_key}"]
        for record in past:
            outcome_icon = "OK" if record.outcome == "success" else "FAIL"
            lines.append(f"\n### [{outcome_icon}] {record.description[:80]}")
            for entry in record.entries[-5:]:  # Last 5 phases
                lines.append(f"  - **{entry.phase}**: {entry.reasoning}")

        lines.append(
            "\nUse these past reasoning chains to inform your current approach."
        )
        return "\n".join(lines)

    def _persist(self) -> None:
        try:
            self._persistence_dir.mkdir(parents=True, exist_ok=True)
            path = self._persistence_dir / "dialogues.json"
            data = {}
            for domain, records in self._dialogues.items():
                data[domain] = [
                    {
                        "op_id": r.op_id,
                        "domain_key": r.domain_key,
                        "description": r.description,
                        "target_files": list(r.target_files),
                        "entries": [
                            {
                                "phase": e.phase,
                                "timestamp": e.timestamp,
                                "reasoning": e.reasoning,
                            }
                            for e in r.entries
                        ],
                        "outcome": r.outcome,
                        "started_at": r.started_at,
                        "completed_at": r.completed_at,
                    }
                    for r in records
                ]
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            logger.debug("[OperationDialogue] Persist failed", exc_info=True)

    def _load(self) -> None:
        try:
            path = self._persistence_dir / "dialogues.json"
            if not path.exists():
                return
            data = json.loads(path.read_text())
            for domain, records_data in data.items():
                for rd in records_data:
                    record = OperationDialogueRecord(
                        op_id=rd["op_id"],
                        domain_key=rd["domain_key"],
                        description=rd["description"],
                        target_files=tuple(rd["target_files"]),
                        entries=[
                            DialogueEntry(
                                phase=e["phase"],
                                timestamp=e["timestamp"],
                                reasoning=e["reasoning"],
                            )
                            for e in rd.get("entries", [])
                        ],
                        outcome=rd.get("outcome", ""),
                        started_at=rd.get("started_at", 0),
                        completed_at=rd.get("completed_at", 0),
                    )
                    self._dialogues[domain].append(record)
        except Exception:
            logger.debug("[OperationDialogue] Load failed", exc_info=True)
