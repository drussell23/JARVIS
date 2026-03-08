# Real-Time Communication (Layer 3) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the comms layer — three CommProtocol transports that give the Intent Engine a voice (VoiceNarrator), a TUI panel (TUISelfProgramPanel), and a human-readable ops log (OpsLogger).

**Architecture:** Four modules under `backend/core/ouroboros/governance/comms/`. Each is a CommProtocol transport implementing `async send(msg: CommMessage)`. VoiceNarrator calls `safe_say()` with debounce. TUISelfProgramPanel maintains state for the TUI dashboard. OpsLogger writes daily rotating log files. NarratorScript holds message templates.

**Tech Stack:** Python 3.9+, asyncio, existing CommProtocol/CommMessage, safe_say(), dataclasses, pathlib, aiofiles (optional, fallback to sync).

**Design doc:** `docs/plans/2026-03-07-autonomous-layers-design.md` §4 (Layer 3)

---

## Task 1: Narrator Script — Message Templates (`narrator_script.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/comms/__init__.py` (empty for now)
- Create: `backend/core/ouroboros/governance/comms/narrator_script.py`
- Create: `tests/governance/comms/__init__.py` (empty)
- Test: `tests/governance/comms/test_narrator_script.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/comms/test_narrator_script.py"""
import pytest


class TestNarratorScript:
    def test_format_signal_detected(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("signal_detected", {
            "test_count": 2,
            "file": "tests/test_utils.py",
        })
        assert "test_utils.py" in text
        assert "2" in text

    def test_format_generating(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("generating", {
            "file": "tests/test_utils.py",
            "provider": "gcp-jprime",
        })
        assert "test_utils.py" in text
        assert "gcp-jprime" in text

    def test_format_approve(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("approve", {
            "file": "prime_client.py",
            "goal": "fix connection timeout",
            "op_id": "op-047",
        })
        assert "prime_client.py" in text
        assert "op-047" in text

    def test_format_applied(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("applied", {"file": "test_utils.py"})
        assert "test_utils.py" in text

    def test_format_postmortem(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("postmortem", {
            "file": "api_handler.py",
            "reason": "AST parse failed",
        })
        assert "api_handler.py" in text
        assert "AST parse failed" in text

    def test_format_observe_error(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("observe_error", {
            "file": "prime_client.py",
            "error_summary": "ConnectionTimeout at line 342",
        })
        assert "prime_client.py" in text

    def test_unknown_phase_returns_fallback(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        text = format_narration("unknown_phase", {"op_id": "op-999"})
        assert text  # non-empty fallback
        assert "op-999" in text

    def test_missing_placeholder_does_not_crash(self):
        from backend.core.ouroboros.governance.comms.narrator_script import format_narration

        # "signal_detected" expects {test_count} and {file} but we omit them
        text = format_narration("signal_detected", {})
        assert isinstance(text, str)  # graceful degradation
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/comms/test_narrator_script.py -v`

**Step 3: Write implementation**

```python
"""backend/core/ouroboros/governance/comms/narrator_script.py

Message templates for voice narration at pipeline milestones.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §4
"""
from __future__ import annotations

from typing import Any, Dict

SCRIPTS: Dict[str, str] = {
    "signal_detected": (
        "Derek, I noticed {test_count} test failure{s} in {file}. "
        "Analyzing the issue now."
    ),
    "generating": "I'm generating a fix for {file} via {provider}.",
    "approve": (
        "I'd like to modify {file} to {goal}. "
        "This is approval-required. "
        "Use the CLI to approve or reject op {op_id}."
    ),
    "applied": "Fix applied and verified. {file} -- all tests passing now.",
    "postmortem": (
        "The fix for {file} didn't work. I've rolled back the changes. "
        "Reason: {reason}."
    ),
    "observe_error": (
        "I'm seeing repeated errors in {file} -- {error_summary}. "
        "Want me to investigate?"
    ),
    "cross_repo_impact": (
        "Heads up -- this change to {file} in {repo} affects "
        "{affected_count} file{s} in {other_repos}."
    ),
}

_FALLBACK = "Pipeline update for op {op_id}: phase {phase}."


def format_narration(phase: str, context: Dict[str, Any]) -> str:
    """Format a narration message for the given phase.

    Uses safe formatting -- missing keys are replaced with '?'
    rather than raising KeyError.
    """
    template = SCRIPTS.get(phase, _FALLBACK)
    # Add defaults for common placeholders
    safe_ctx: Dict[str, Any] = {
        "s": "s",
        "phase": phase,
        "op_id": "unknown",
        "file": "unknown",
        "provider": "unknown",
        "goal": "unknown",
        "reason": "unknown",
        "error_summary": "unknown",
        "test_count": "?",
        "repo": "unknown",
        "affected_count": "?",
        "other_repos": "unknown",
    }
    safe_ctx.update(context)
    # Pluralization
    if safe_ctx.get("test_count") == 1:
        safe_ctx["s"] = ""
    try:
        return template.format(**safe_ctx)
    except (KeyError, IndexError, ValueError):
        return f"Pipeline update: {phase} -- {context}"
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/comms/test_narrator_script.py -v`

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/__init__.py \
       backend/core/ouroboros/governance/comms/narrator_script.py \
       tests/governance/comms/__init__.py \
       tests/governance/comms/test_narrator_script.py
git commit -m "feat(comms): add narrator script with message templates for voice narration"
```

---

## Task 2: Voice Narrator Transport (`voice_narrator.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/comms/voice_narrator.py`
- Test: `tests/governance/comms/test_voice_narrator.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/comms/test_voice_narrator.py"""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.fixture
def mock_safe_say():
    return AsyncMock(return_value=True)


def _make_comm_message(msg_type, op_id="op-001", payload=None):
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    return CommMessage(
        msg_type=MessageType[msg_type] if isinstance(msg_type, str) else msg_type,
        op_id=op_id,
        seq=1,
        causal_parent_seq=None,
        payload=payload or {},
        timestamp=time.time(),
    )


class TestVoiceNarratorSend:
    @pytest.mark.asyncio
    async def test_narrates_intent_message(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("INTENT", payload={
            "goal": "fix test",
            "target_files": ["tests/test_a.py"],
        })
        await narrator.send(msg)
        mock_safe_say.assert_called_once()
        call_text = mock_safe_say.call_args[0][0]
        assert isinstance(call_text, str)
        assert len(call_text) > 0

    @pytest.mark.asyncio
    async def test_narrates_decision_message(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("DECISION", payload={
            "outcome": "applied",
            "reason_code": "tests_pass",
            "diff_summary": "added edge case",
        })
        await narrator.send(msg)
        mock_safe_say.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_heartbeat(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("HEARTBEAT", payload={
            "phase": "generating",
            "progress_pct": 50,
        })
        await narrator.send(msg)
        mock_safe_say.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_plan(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("PLAN", payload={"steps": ["step1"]})
        await narrator.send(msg)
        mock_safe_say.assert_not_called()


class TestVoiceNarratorDebounce:
    @pytest.mark.asyncio
    async def test_debounce_blocks_rapid_narrations(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=60.0)
        msg1 = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix 1", "target_files": ["a.py"],
        })
        msg2 = _make_comm_message("INTENT", op_id="op-002", payload={
            "goal": "fix 2", "target_files": ["b.py"],
        })
        await narrator.send(msg1)
        await narrator.send(msg2)
        assert mock_safe_say.call_count == 1  # second blocked by debounce

    @pytest.mark.asyncio
    async def test_debounce_allows_after_expiry(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg1 = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix 1", "target_files": ["a.py"],
        })
        msg2 = _make_comm_message("INTENT", op_id="op-002", payload={
            "goal": "fix 2", "target_files": ["b.py"],
        })
        await narrator.send(msg1)
        await asyncio.sleep(0.01)
        await narrator.send(msg2)
        assert mock_safe_say.call_count == 2


class TestVoiceNarratorIdempotency:
    @pytest.mark.asyncio
    async def test_same_op_same_phase_not_repeated(self, mock_safe_say):
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=mock_safe_say, debounce_s=0.0)
        msg = _make_comm_message("DECISION", op_id="op-001", payload={
            "outcome": "applied",
        })
        await narrator.send(msg)
        await narrator.send(msg)  # same op_id + same msg_type
        assert mock_safe_say.call_count == 1


class TestVoiceNarratorFailure:
    @pytest.mark.asyncio
    async def test_say_failure_does_not_propagate(self):
        failing_say = AsyncMock(side_effect=RuntimeError("TTS broke"))
        from backend.core.ouroboros.governance.comms.voice_narrator import VoiceNarrator

        narrator = VoiceNarrator(say_fn=failing_say, debounce_s=0.0)
        msg = _make_comm_message("INTENT", payload={
            "goal": "fix", "target_files": ["a.py"],
        })
        await narrator.send(msg)  # should not raise
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/comms/test_voice_narrator.py -v`

**Step 3: Write implementation**

```python
"""backend/core/ouroboros/governance/comms/voice_narrator.py

CommProtocol transport that narrates pipeline events via speech.
Subscribes to INTENT, DECISION, POSTMORTEM messages. Skips HEARTBEAT and PLAN.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §4
"""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Any, Callable, Coroutine, Dict, Optional, Set

from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType

from .narrator_script import format_narration

logger = logging.getLogger(__name__)

# Message types that trigger narration
_NARRATE_TYPES = {MessageType.INTENT, MessageType.DECISION, MessageType.POSTMORTEM}


class VoiceNarrator:
    """CommProtocol transport that narrates pipeline events via safe_say()."""

    def __init__(
        self,
        say_fn: Callable[..., Coroutine[Any, Any, bool]],
        debounce_s: float = 60.0,
        source: str = "intent_engine",
    ) -> None:
        self._say_fn = say_fn
        self._debounce_s = debounce_s
        self._source = source
        self._last_narration: float = 0.0  # monotonic
        self._narrated_ids: Set[str] = set()  # notification_id for idempotency

    async def send(self, msg: CommMessage) -> None:
        """CommProtocol transport interface. Called for every pipeline message."""
        if msg.msg_type not in _NARRATE_TYPES:
            return

        # Idempotency: don't repeat same op_id + msg_type
        notification_id = hashlib.sha256(
            f"{msg.op_id}:{msg.msg_type.name}".encode()
        ).hexdigest()[:12]
        if notification_id in self._narrated_ids:
            return
        self._narrated_ids.add(notification_id)

        # Debounce: max 1 narration per debounce_s
        now = time.monotonic()
        if (now - self._last_narration) < self._debounce_s:
            return

        # Build narration text
        phase = self._map_phase(msg)
        context = dict(msg.payload)
        context["op_id"] = msg.op_id
        # Extract file from target_files if present
        target_files = context.get("target_files", [])
        if target_files and isinstance(target_files, (list, tuple)):
            context.setdefault("file", target_files[0])

        text = format_narration(phase, context)

        try:
            await self._say_fn(text, source=self._source)
            self._last_narration = now
        except Exception:
            logger.debug("VoiceNarrator: say_fn failed for op %s", msg.op_id)

    @staticmethod
    def _map_phase(msg: CommMessage) -> str:
        """Map CommMessage type + payload to narrator script phase."""
        if msg.msg_type == MessageType.INTENT:
            return "signal_detected"
        elif msg.msg_type == MessageType.POSTMORTEM:
            return "postmortem"
        elif msg.msg_type == MessageType.DECISION:
            outcome = msg.payload.get("outcome", "")
            if outcome in ("applied", "validated"):
                return "applied"
            elif outcome == "blocked":
                return "approve"
            else:
                return "applied"
        return "signal_detected"
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/comms/test_voice_narrator.py -v`

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/voice_narrator.py \
       tests/governance/comms/test_voice_narrator.py
git commit -m "feat(comms): add VoiceNarrator transport with debounce and idempotency"
```

---

## Task 3: Ops Logger Transport (`ops_logger.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/comms/ops_logger.py`
- Test: `tests/governance/comms/test_ops_logger.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/comms/test_ops_logger.py"""
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock


def _make_comm_message(msg_type, op_id="op-001", payload=None):
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    return CommMessage(
        msg_type=MessageType[msg_type] if isinstance(msg_type, str) else msg_type,
        op_id=op_id,
        seq=1,
        causal_parent_seq=None,
        payload=payload or {},
        timestamp=time.time(),
    )


class TestOpsLoggerSend:
    @pytest.mark.asyncio
    async def test_writes_intent_to_log_file(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        logger = OpsLogger(log_dir=tmp_path)
        msg = _make_comm_message("INTENT", payload={
            "goal": "fix test_edge_case",
            "target_files": ["tests/test_utils.py"],
            "risk_tier": "SAFE_AUTO",
        })
        await logger.send(msg)

        log_files = list(tmp_path.glob("*.log"))
        assert len(log_files) == 1
        content = log_files[0].read_text()
        assert "INTENT" in content
        assert "op-001" in content
        assert "test_utils.py" in content

    @pytest.mark.asyncio
    async def test_writes_decision_to_log_file(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        logger = OpsLogger(log_dir=tmp_path)
        msg = _make_comm_message("DECISION", payload={
            "outcome": "applied",
            "reason_code": "tests_pass",
        })
        await logger.send(msg)

        log_files = list(tmp_path.glob("*.log"))
        content = log_files[0].read_text()
        assert "DECISION" in content
        assert "applied" in content

    @pytest.mark.asyncio
    async def test_appends_multiple_entries(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        logger = OpsLogger(log_dir=tmp_path)
        msg1 = _make_comm_message("INTENT", op_id="op-001", payload={"goal": "fix a"})
        msg2 = _make_comm_message("DECISION", op_id="op-001", payload={"outcome": "applied"})
        await logger.send(msg1)
        await logger.send(msg2)

        log_files = list(tmp_path.glob("*.log"))
        assert len(log_files) == 1  # same day = same file
        content = log_files[0].read_text()
        assert "INTENT" in content
        assert "DECISION" in content


class TestOpsLoggerFormat:
    @pytest.mark.asyncio
    async def test_log_entry_has_timestamp(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        logger = OpsLogger(log_dir=tmp_path)
        msg = _make_comm_message("INTENT", payload={"goal": "test"})
        await logger.send(msg)

        content = list(tmp_path.glob("*.log"))[0].read_text()
        # Should have a timestamp like [2026-03-07 14:23:01]
        assert "[20" in content  # starts with year


class TestOpsLoggerRetention:
    @pytest.mark.asyncio
    async def test_cleanup_old_logs(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger
        import os

        # Create a fake old log file
        old_log = tmp_path / "2020-01-01-ops.log"
        old_log.write_text("old data")
        # Set mtime to the past
        old_time = time.time() - (40 * 86400)  # 40 days ago
        os.utime(old_log, (old_time, old_time))

        logger = OpsLogger(log_dir=tmp_path, retention_days=30)
        await logger.cleanup_old_logs()

        assert not old_log.exists()

    @pytest.mark.asyncio
    async def test_keeps_recent_logs(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        recent_log = tmp_path / "2026-03-07-ops.log"
        recent_log.write_text("recent data")

        logger = OpsLogger(log_dir=tmp_path, retention_days=30)
        await logger.cleanup_old_logs()

        assert recent_log.exists()


class TestOpsLoggerFailure:
    @pytest.mark.asyncio
    async def test_write_failure_does_not_propagate(self, tmp_path):
        from backend.core.ouroboros.governance.comms.ops_logger import OpsLogger

        # Point to a non-writable directory
        logger = OpsLogger(log_dir=tmp_path / "nonexistent" / "nested")
        msg = _make_comm_message("INTENT", payload={"goal": "test"})
        await logger.send(msg)  # should not raise
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/comms/test_ops_logger.py -v`

**Step 3: Write implementation**

```python
"""backend/core/ouroboros/governance/comms/ops_logger.py

Append-only ops log writer. Writes human-readable pipeline narratives
to daily log files at ~/.jarvis/ops/YYYY-MM-DD-ops.log.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §4
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.core.ouroboros.governance.comm_protocol import CommMessage

logger = logging.getLogger(__name__)

_DEFAULT_LOG_DIR = Path.home() / ".jarvis" / "ops"


class OpsLogger:
    """CommProtocol transport that writes human-readable ops logs."""

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        retention_days: int = 30,
    ) -> None:
        self._log_dir = Path(
            log_dir
            or os.environ.get("JARVIS_OPS_LOG_DIR", str(_DEFAULT_LOG_DIR))
        )
        self._retention_days = int(
            os.environ.get("JARVIS_OPS_LOG_RETENTION_DAYS", str(retention_days))
        )

    async def send(self, msg: CommMessage) -> None:
        """CommProtocol transport interface. Appends entry to daily log."""
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            log_path = self._log_dir / self._daily_filename()
            entry = self._format_entry(msg)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            logger.debug("OpsLogger: failed to write for op %s", msg.op_id)

    async def cleanup_old_logs(self) -> None:
        """Remove log files older than retention_days."""
        if not self._log_dir.exists():
            return
        cutoff = time.time() - (self._retention_days * 86400)
        for log_file in self._log_dir.glob("*-ops.log"):
            try:
                if log_file.stat().st_mtime < cutoff:
                    log_file.unlink()
            except Exception:
                logger.debug("OpsLogger: failed to remove %s", log_file)

    @staticmethod
    def _daily_filename() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d-ops.log")

    @staticmethod
    def _format_entry(msg: CommMessage) -> str:
        ts = datetime.fromtimestamp(msg.timestamp, tz=timezone.utc)
        ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
        msg_type = msg.msg_type.name

        lines = [f"[{ts_str}] {msg_type}  {msg.op_id}"]

        payload = msg.payload
        # Add key payload fields on indented lines
        for key in ("goal", "target_files", "outcome", "reason_code",
                     "root_cause", "failed_phase", "next_safe_action",
                     "risk_tier", "blast_radius", "steps",
                     "phase", "progress_pct", "diff_summary"):
            if key in payload:
                val = payload[key]
                if isinstance(val, (list, tuple)):
                    val = ", ".join(str(v) for v in val)
                lines.append(f"    {key}: {val}")

        return "\n".join(lines) + "\n\n"
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/comms/test_ops_logger.py -v`

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/ops_logger.py \
       tests/governance/comms/test_ops_logger.py
git commit -m "feat(comms): add OpsLogger transport with daily rotation and retention"
```

---

## Task 4: TUI Self-Program Panel Data Provider (`tui_panel.py`)

**Files:**
- Create: `backend/core/ouroboros/governance/comms/tui_panel.py`
- Test: `tests/governance/comms/test_tui_panel.py`

**Step 1: Write the failing tests**

```python
"""tests/governance/comms/test_tui_panel.py"""
import time
import pytest
from unittest.mock import MagicMock


def _make_comm_message(msg_type, op_id="op-001", payload=None):
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    return CommMessage(
        msg_type=MessageType[msg_type] if isinstance(msg_type, str) else msg_type,
        op_id=op_id,
        seq=1,
        causal_parent_seq=None,
        payload=payload or {},
        timestamp=time.time(),
    )


class TestTUIPanelState:
    @pytest.mark.asyncio
    async def test_intent_creates_active_op(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        msg = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix test",
            "target_files": ["tests/test_a.py"],
            "risk_tier": "SAFE_AUTO",
        })
        await panel.send(msg)

        state = panel.get_state()
        assert len(state.active_ops) == 1
        assert state.active_ops[0].op_id == "op-001"

    @pytest.mark.asyncio
    async def test_decision_completes_op(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        intent = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix test", "target_files": ["a.py"],
        })
        decision = _make_comm_message("DECISION", op_id="op-001", payload={
            "outcome": "applied", "reason_code": "tests_pass",
        })
        await panel.send(intent)
        await panel.send(decision)

        state = panel.get_state()
        assert len(state.active_ops) == 0
        assert len(state.recent_completions) == 1
        assert state.recent_completions[0].op_id == "op-001"
        assert state.recent_completions[0].outcome == "applied"

    @pytest.mark.asyncio
    async def test_postmortem_completes_op(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        intent = _make_comm_message("INTENT", op_id="op-002", payload={
            "goal": "fix bug", "target_files": ["b.py"],
        })
        postmortem = _make_comm_message("POSTMORTEM", op_id="op-002", payload={
            "root_cause": "AST parse failed",
        })
        await panel.send(intent)
        await panel.send(postmortem)

        state = panel.get_state()
        assert len(state.active_ops) == 0
        assert len(state.recent_completions) == 1
        assert state.recent_completions[0].outcome == "postmortem"

    @pytest.mark.asyncio
    async def test_heartbeat_updates_phase(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        intent = _make_comm_message("INTENT", op_id="op-001", payload={
            "goal": "fix", "target_files": ["a.py"],
        })
        heartbeat = _make_comm_message("HEARTBEAT", op_id="op-001", payload={
            "phase": "generating",
            "progress_pct": 50,
        })
        await panel.send(intent)
        await panel.send(heartbeat)

        state = panel.get_state()
        assert state.active_ops[0].phase == "generating"

    @pytest.mark.asyncio
    async def test_recent_completions_capped_at_10(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        for i in range(15):
            intent = _make_comm_message("INTENT", op_id=f"op-{i:03d}", payload={
                "goal": f"fix {i}", "target_files": [f"f{i}.py"],
            })
            decision = _make_comm_message("DECISION", op_id=f"op-{i:03d}", payload={
                "outcome": "applied",
            })
            await panel.send(intent)
            await panel.send(decision)

        state = panel.get_state()
        assert len(state.recent_completions) == 10

    @pytest.mark.asyncio
    async def test_ops_today_counter(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        for i in range(3):
            intent = _make_comm_message("INTENT", op_id=f"op-{i}", payload={
                "goal": "fix", "target_files": ["a.py"],
            })
            decision = _make_comm_message("DECISION", op_id=f"op-{i}", payload={
                "outcome": "applied",
            })
            await panel.send(intent)
            await panel.send(decision)

        state = panel.get_state()
        assert state.ops_today == 3


class TestTUIPanelTransport:
    @pytest.mark.asyncio
    async def test_unknown_op_heartbeat_ignored(self):
        from backend.core.ouroboros.governance.comms.tui_panel import TUISelfProgramPanel

        panel = TUISelfProgramPanel()
        heartbeat = _make_comm_message("HEARTBEAT", op_id="op-unknown", payload={
            "phase": "generating",
        })
        await panel.send(heartbeat)  # should not crash

        state = panel.get_state()
        assert len(state.active_ops) == 0
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/comms/test_tui_panel.py -v`

**Step 3: Write implementation**

```python
"""backend/core/ouroboros/governance/comms/tui_panel.py

TUI "Self-Programming" panel data provider. Tracks active ops,
pending approvals, and recent completions for the Textual TUI dashboard.

Design ref: docs/plans/2026-03-07-autonomous-layers-design.md §4
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType

logger = logging.getLogger(__name__)

_MAX_RECENT = 10
_TERMINAL_TYPES = {MessageType.DECISION, MessageType.POSTMORTEM}


@dataclass
class PipelineStatus:
    """Mutable tracking state for an active operation."""

    op_id: str
    phase: str
    target_file: str
    repo: str
    trigger_source: str
    provider: Optional[str]
    started_at: float  # monotonic
    started_at_utc: datetime
    awaiting_approval: bool = False

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at


@dataclass(frozen=True)
class CompletionSummary:
    """Immutable record of a completed operation."""

    op_id: str
    target_file: str
    outcome: str
    completed_at: datetime
    duration_s: float
    provider: Optional[str] = None


@dataclass(frozen=True)
class SelfProgramPanelState:
    """Snapshot of panel state for TUI rendering."""

    active_ops: Tuple[PipelineStatus, ...]
    pending_approvals: Tuple[PipelineStatus, ...]
    recent_completions: Tuple[CompletionSummary, ...]
    intent_engine_state: str = "watching"
    ops_today: int = 0
    ops_limit: int = 20
    repos_online: Tuple[str, ...] = ()


class TUISelfProgramPanel:
    """CommProtocol transport that maintains panel state for TUI rendering."""

    def __init__(self, ops_limit: int = 20) -> None:
        self._active: Dict[str, PipelineStatus] = {}
        self._completions: deque[CompletionSummary] = deque(maxlen=_MAX_RECENT)
        self._ops_today: int = 0
        self._ops_limit = ops_limit

    async def send(self, msg: CommMessage) -> None:
        """CommProtocol transport interface."""
        try:
            if msg.msg_type == MessageType.INTENT:
                self._handle_intent(msg)
            elif msg.msg_type == MessageType.HEARTBEAT:
                self._handle_heartbeat(msg)
            elif msg.msg_type in _TERMINAL_TYPES:
                self._handle_terminal(msg)
        except Exception:
            logger.debug("TUISelfProgramPanel: error processing %s", msg.op_id)

    def get_state(self) -> SelfProgramPanelState:
        """Return current panel state snapshot."""
        active = tuple(self._active.values())
        pending = tuple(op for op in active if op.awaiting_approval)
        return SelfProgramPanelState(
            active_ops=active,
            pending_approvals=pending,
            recent_completions=tuple(self._completions),
            ops_today=self._ops_today,
            ops_limit=self._ops_limit,
        )

    def _handle_intent(self, msg: CommMessage) -> None:
        payload = msg.payload
        target_files = payload.get("target_files", [])
        target_file = target_files[0] if target_files else "unknown"
        self._active[msg.op_id] = PipelineStatus(
            op_id=msg.op_id,
            phase="intent",
            target_file=target_file,
            repo=payload.get("repo", "jarvis"),
            trigger_source=payload.get("trigger_source", "unknown"),
            provider=payload.get("provider"),
            started_at=time.monotonic(),
            started_at_utc=datetime.now(timezone.utc),
        )

    def _handle_heartbeat(self, msg: CommMessage) -> None:
        status = self._active.get(msg.op_id)
        if status is None:
            return
        status.phase = msg.payload.get("phase", status.phase)
        if msg.payload.get("phase") == "approve":
            status.awaiting_approval = True

    def _handle_terminal(self, msg: CommMessage) -> None:
        status = self._active.pop(msg.op_id, None)
        if msg.msg_type == MessageType.POSTMORTEM:
            outcome = "postmortem"
        else:
            outcome = msg.payload.get("outcome", "complete")

        duration_s = status.elapsed_s if status else 0.0
        target_file = status.target_file if status else "unknown"
        provider = status.provider if status else None

        self._completions.append(CompletionSummary(
            op_id=msg.op_id,
            target_file=target_file,
            outcome=outcome,
            completed_at=datetime.now(timezone.utc),
            duration_s=duration_s,
            provider=provider,
        ))
        self._ops_today += 1
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/comms/test_tui_panel.py -v`

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/tui_panel.py \
       tests/governance/comms/test_tui_panel.py
git commit -m "feat(comms): add TUISelfProgramPanel data provider for TUI dashboard"
```

---

## Task 5: Package Exports + CommProtocol Wiring

**Files:**
- Modify: `backend/core/ouroboros/governance/comms/__init__.py`
- Modify: `backend/core/ouroboros/governance/__init__.py`
- Test: `tests/governance/comms/test_exports.py`

**Step 1: Write the failing test**

```python
"""tests/governance/comms/test_exports.py"""

def test_comms_public_api():
    from backend.core.ouroboros.governance.comms import (
        format_narration,
        SCRIPTS,
        VoiceNarrator,
        OpsLogger,
        TUISelfProgramPanel,
        SelfProgramPanelState,
        PipelineStatus,
        CompletionSummary,
    )
    assert VoiceNarrator is not None
    assert OpsLogger is not None
    assert TUISelfProgramPanel is not None
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/comms/test_exports.py -v`

**Step 3: Write implementation**

Update `backend/core/ouroboros/governance/comms/__init__.py`:

```python
"""Public API for the real-time communication layer."""
from .narrator_script import format_narration, SCRIPTS
from .voice_narrator import VoiceNarrator
from .ops_logger import OpsLogger
from .tui_panel import (
    TUISelfProgramPanel,
    SelfProgramPanelState,
    PipelineStatus,
    CompletionSummary,
)

__all__ = [
    "format_narration",
    "SCRIPTS",
    "VoiceNarrator",
    "OpsLogger",
    "TUISelfProgramPanel",
    "SelfProgramPanelState",
    "PipelineStatus",
    "CompletionSummary",
]
```

Append to `backend/core/ouroboros/governance/__init__.py`:

```python
# --- Real-Time Communication (Layer 3) ---
from .comms import (
    format_narration,
    SCRIPTS,
    VoiceNarrator,
    OpsLogger,
    TUISelfProgramPanel,
    SelfProgramPanelState,
    PipelineStatus,
    CompletionSummary,
)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/comms/test_exports.py -v`

**Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/comms/__init__.py \
       backend/core/ouroboros/governance/__init__.py \
       tests/governance/comms/test_exports.py
git commit -m "feat(comms): export public API and wire into governance package"
```

---

## Task 6: E2E Integration Test — CommProtocol with All 3 Transports

**Files:**
- Create: `tests/governance/comms/test_e2e_comms.py`

**Step 1: Write the integration test**

```python
"""tests/governance/comms/test_e2e_comms.py

End-to-end: CommProtocol emits messages through all 3 transports
(VoiceNarrator, OpsLogger, TUISelfProgramPanel) simultaneously.
"""
import asyncio
import time
import pytest
from unittest.mock import AsyncMock


def _make_comm_message(msg_type, op_id="op-001", payload=None):
    from backend.core.ouroboros.governance.comm_protocol import CommMessage, MessageType
    return CommMessage(
        msg_type=MessageType[msg_type] if isinstance(msg_type, str) else msg_type,
        op_id=op_id,
        seq=1,
        causal_parent_seq=None,
        payload=payload or {},
        timestamp=time.time(),
    )


@pytest.mark.asyncio
async def test_e2e_all_transports_receive_intent(tmp_path):
    from backend.core.ouroboros.governance.comms import (
        VoiceNarrator,
        OpsLogger,
        TUISelfProgramPanel,
    )

    mock_say = AsyncMock(return_value=True)
    narrator = VoiceNarrator(say_fn=mock_say, debounce_s=0.0)
    ops_logger = OpsLogger(log_dir=tmp_path)
    tui_panel = TUISelfProgramPanel()

    msg = _make_comm_message("INTENT", op_id="op-e2e", payload={
        "goal": "fix edge case",
        "target_files": ["tests/test_utils.py"],
        "risk_tier": "SAFE_AUTO",
    })

    # Deliver to all 3 transports
    await asyncio.gather(
        narrator.send(msg),
        ops_logger.send(msg),
        tui_panel.send(msg),
    )

    # Voice narrator spoke
    mock_say.assert_called_once()

    # Ops logger wrote
    log_files = list(tmp_path.glob("*.log"))
    assert len(log_files) == 1
    content = log_files[0].read_text()
    assert "op-e2e" in content

    # TUI panel tracked
    state = tui_panel.get_state()
    assert len(state.active_ops) == 1
    assert state.active_ops[0].op_id == "op-e2e"


@pytest.mark.asyncio
async def test_e2e_full_lifecycle(tmp_path):
    from backend.core.ouroboros.governance.comms import (
        VoiceNarrator,
        OpsLogger,
        TUISelfProgramPanel,
    )

    mock_say = AsyncMock(return_value=True)
    narrator = VoiceNarrator(say_fn=mock_say, debounce_s=0.0)
    ops_logger = OpsLogger(log_dir=tmp_path)
    tui_panel = TUISelfProgramPanel()

    transports = [narrator, ops_logger, tui_panel]

    # 1. INTENT
    intent = _make_comm_message("INTENT", op_id="op-lc", payload={
        "goal": "fix bug", "target_files": ["a.py"],
    })
    for t in transports:
        await t.send(intent)

    assert len(tui_panel.get_state().active_ops) == 1

    # 2. HEARTBEAT
    heartbeat = _make_comm_message("HEARTBEAT", op_id="op-lc", payload={
        "phase": "generating", "progress_pct": 50,
    })
    for t in transports:
        await t.send(heartbeat)

    assert tui_panel.get_state().active_ops[0].phase == "generating"

    # 3. DECISION (applied)
    decision = _make_comm_message("DECISION", op_id="op-lc", payload={
        "outcome": "applied", "reason_code": "tests_pass",
    })
    for t in transports:
        await t.send(decision)

    state = tui_panel.get_state()
    assert len(state.active_ops) == 0
    assert len(state.recent_completions) == 1
    assert state.ops_today == 1

    # Ops logger has all 3 entries
    content = list(tmp_path.glob("*.log"))[0].read_text()
    assert "INTENT" in content
    assert "HEARTBEAT" in content
    assert "DECISION" in content
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/governance/comms/test_e2e_comms.py -v`

**Step 3: Commit**

```bash
git add tests/governance/comms/test_e2e_comms.py
git commit -m "test(comms): add E2E integration tests for all 3 transports"
```

---

## Task 7: Full Test Suite Verification

**Step 1: Run all comms tests**

Run: `python3 -m pytest tests/governance/comms/ -v --tb=short`

**Step 2: Run all governance tests for regressions**

Run: `python3 -m pytest tests/governance/ -v --tb=short`

**Step 3: Verify imports**

Run: `python3 -c "from backend.core.ouroboros.governance.comms import VoiceNarrator, OpsLogger, TUISelfProgramPanel, format_narration; print('All imports OK')"`

---

## Summary

| Task | Module | Tests | Purpose |
|------|--------|-------|---------|
| 1 | `narrator_script.py` | 8 | Message templates for voice narration |
| 2 | `voice_narrator.py` | 8 | CommProtocol transport with debounce + idempotency |
| 3 | `ops_logger.py` | 7 | Daily rotating ops log writer |
| 4 | `tui_panel.py` | 7 | TUI dashboard data provider |
| 5 | `__init__.py` | 1 | Package exports |
| 6 | E2E tests | 2 | Full lifecycle across all transports |
| 7 | Suite run | -- | Regression check |

**Total: ~33 tests across 7 tasks, 4 new source files.**
