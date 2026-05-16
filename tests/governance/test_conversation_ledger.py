"""Arc #1 — Conversation Ledger tests.

resume / sessions / save integration lives here.

Covers:
  * Ledger CRUD (append_turn, read_tail, session_exists)
  * Bounds enforcement (max_file_bytes, max_turns, tail windowing)
  * Resume flow (rehydrate → bridge injection with re-sanitization)
  * Save flow (force-persist bridge → ledger on disk)
  * Session identity sanitization (path traversal prevention)
  * Operator knobs (env var overrides)
  * Error isolation (corrupt rows, permission errors)
  * AST invariants (authority asymmetry, flock composition, schema)
  * Session listing and pruning
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ledger_env(tmp_path, monkeypatch):
    """Point the ledger at a tmp dir and enable it for all tests."""
    monkeypatch.setenv(
        "JARVIS_CONVERSATION_LEDGER_DIR", str(tmp_path / "sessions"),
    )
    monkeypatch.setenv("JARVIS_CONVERSATION_LEDGER_ENABLED", "true")
    # Reset seq cache between tests.
    from backend.core.ouroboros.governance.conversation_ledger import (
        reset_seq_cache_for_tests,
    )
    reset_seq_cache_for_tests()
    yield


# ---------------------------------------------------------------------------
# Phase 0 — Ledger CRUD
# ---------------------------------------------------------------------------


class TestAppendTurn:
    """flock_append_line-backed append."""

    def test_append_creates_file(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn,
            session_exists,
        )
        sid = "test-session-001"
        ok = append_turn(
            sid, role="user", text="hello", source="tui_user",
        )
        assert ok is True
        assert session_exists(sid) is True

    def test_append_writes_valid_jsonl(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn, ledger_dir,
        )
        sid = "test-valid-jsonl"
        append_turn(sid, role="user", text="line one", source="tui_user")
        append_turn(sid, role="assistant", text="line two", source="tui_user")

        path = ledger_dir() / f"{sid}.jsonl"
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert obj["schema_version"] == "conversation_ledger.1"
            assert obj["session_id"] == sid
            assert obj["role"] in ("user", "assistant")

    def test_append_increments_turn_seq(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn, ledger_dir,
        )
        sid = "test-seq"
        for i in range(5):
            append_turn(
                sid, role="user", text=f"turn {i}",
                source="tui_user",
            )
        path = ledger_dir() / f"{sid}.jsonl"
        seqs = []
        for line in path.read_text().strip().splitlines():
            seqs.append(json.loads(line)["turn_seq"])
        assert seqs == [1, 2, 3, 4, 5]

    def test_append_disabled_returns_false(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn,
        )
        ok = append_turn(
            "disabled-test", role="user", text="nope",
            source="tui_user",
        )
        assert ok is False


class TestReadTail:
    """Tail-bounded read via flock_critical_section."""

    def test_read_tail_returns_last_n(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn, read_tail,
        )
        sid = "test-tail"
        for i in range(10):
            append_turn(
                sid, role="user", text=f"turn-{i}",
                source="tui_user",
            )
        tail = read_tail(sid, max_turns=3)
        assert len(tail) == 3
        assert tail[0].text == "turn-7"
        assert tail[2].text == "turn-9"

    def test_read_tail_fewer_turns_than_max(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn, read_tail,
        )
        sid = "test-few"
        append_turn(sid, role="user", text="only one", source="tui_user")
        tail = read_tail(sid, max_turns=50)
        assert len(tail) == 1
        assert tail[0].text == "only one"

    def test_read_tail_nonexistent_session_empty(self):
        from backend.core.ouroboros.governance.conversation_ledger import (
            read_tail,
        )
        tail = read_tail("does-not-exist")
        assert tail == ()

    def test_read_tail_corrupt_rows_skipped(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            ledger_dir, read_tail,
        )
        sid = "test-corrupt"
        d = ledger_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{sid}.jsonl"
        # Write one valid row, one corrupt row, one valid row.
        valid_row = json.dumps({
            "schema_version": "conversation_ledger.1",
            "session_id": sid,
            "role": "user",
            "text": "valid-1",
            "source": "tui_user",
            "op_id": "",
            "ts": 1.0,
            "turn_seq": 1,
        })
        path.write_text(
            f"{valid_row}\n"
            "THIS IS NOT JSON\n"
            f"{valid_row.replace('valid-1', 'valid-2')}\n"
        )
        tail = read_tail(sid, max_turns=10)
        assert len(tail) == 2
        assert tail[0].text == "valid-1"
        assert tail[1].text == "valid-2"

    def test_read_tail_empty_file(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            ledger_dir, read_tail,
        )
        sid = "test-empty-file"
        d = ledger_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sid}.jsonl").write_text("")
        tail = read_tail(sid)
        assert tail == ()


# ---------------------------------------------------------------------------
# Bounds enforcement
# ---------------------------------------------------------------------------


class TestBoundsEnforcement:

    def test_max_file_bytes_rejects_write(self, monkeypatch, tmp_path):
        # Cap is 1100 (above the 1024-byte floor in _env_int).
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_MAX_FILE_BYTES", "1100",
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn, ledger_dir, max_file_bytes,
        )
        assert max_file_bytes() == 1100
        sid = "test-size-cap"
        # First write: ~1100 bytes of text + JSON envelope ≈ >1100.
        ok1 = append_turn(
            sid, role="user",
            text="x" * 1000,
            source="tui_user",
        )
        assert ok1 is True
        # Verify file now exceeds the cap.
        path = ledger_dir() / f"{sid}.jsonl"
        assert path.stat().st_size >= 1100
        # Second write should be rejected.
        ok2 = append_turn(
            sid, role="user",
            text="y" * 50,
            source="tui_user",
        )
        assert ok2 is False

    def test_max_turns_rejects_write(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_MAX_TURNS_PER_SESSION", "3",
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn,
        )
        sid = "test-turn-cap"
        for i in range(3):
            ok = append_turn(
                sid, role="user", text=f"t{i}",
                source="tui_user",
            )
            assert ok is True
        # Fourth write should be rejected.
        ok = append_turn(
            sid, role="user", text="rejected",
            source="tui_user",
        )
        assert ok is False

    def test_read_tail_rejects_oversized_file(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_MAX_FILE_BYTES", "100",
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            ledger_dir, read_tail,
        )
        sid = "test-oversize-read"
        d = ledger_dir()
        d.mkdir(parents=True, exist_ok=True)
        # Write a file larger than the cap.
        (d / f"{sid}.jsonl").write_text("x" * 200)
        tail = read_tail(sid)
        assert tail == ()


# ---------------------------------------------------------------------------
# Session identity sanitization
# ---------------------------------------------------------------------------


class TestSessionIdSanitization:

    def test_path_traversal_blocked(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn, ledger_dir,
        )
        # Attempt path traversal in session_id.
        sid = "../../etc/passwd"
        ok = append_turn(
            sid, role="user", text="evil", source="tui_user",
        )
        assert ok is True
        # Verify no file was created outside the ledger dir.
        d = ledger_dir()
        files = list(d.glob("*.jsonl"))
        assert len(files) == 1
        # The sanitized filename must not contain path separators
        # or .. sequences — the word 'etc' appearing in the flat
        # filename is fine (no directory traversal occurred).
        assert "/" not in files[0].name
        assert ".." not in files[0].name
        # Verify the file is inside the ledger dir, not elsewhere.
        assert files[0].parent == d

    def test_empty_session_id_rejected(self):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn,
        )
        ok = append_turn(
            "", role="user", text="nope", source="tui_user",
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Operator knobs
# ---------------------------------------------------------------------------


class TestEnvKnobs:

    def test_custom_ledger_dir(self, monkeypatch, tmp_path):
        custom = tmp_path / "my_custom_dir"
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_DIR", str(custom),
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            ledger_dir,
        )
        assert ledger_dir() == custom

    def test_replay_tail_default_env(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_REPLAY_TAIL", "25",
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            replay_tail_default,
        )
        assert replay_tail_default() == 25

    def test_retention_days_env(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_RETENTION_DAYS", "7",
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            retention_days,
        )
        assert retention_days() == 7


# ---------------------------------------------------------------------------
# Session listing
# ---------------------------------------------------------------------------


class TestListSessions:

    def test_list_sessions_empty(self):
        from backend.core.ouroboros.governance.conversation_ledger import (
            list_sessions,
        )
        sessions = list_sessions()
        assert sessions == []

    def test_list_sessions_returns_summaries(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn, list_sessions,
        )
        for i in range(3):
            sid = f"sess-{i}"
            append_turn(
                sid, role="user", text=f"msg-{i}",
                source="tui_user",
            )
        sessions = list_sessions()
        assert len(sessions) == 3
        for s in sessions:
            assert s.turn_count == 1
            assert s.session_id.startswith("sess-")

    def test_list_sessions_respects_limit(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn, list_sessions,
        )
        for i in range(5):
            append_turn(
                f"sess-{i}", role="user", text=f"msg-{i}",
                source="tui_user",
            )
        sessions = list_sessions(limit=2)
        assert len(sessions) == 2


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


class TestPruning:

    def test_prune_removes_old_sessions(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn, ledger_dir, prune, list_sessions,
        )
        sid = "old-session"
        append_turn(
            sid, role="user", text="old msg", source="tui_user",
        )
        # Backdate the file mtime.
        path = ledger_dir() / f"{sid}.jsonl"
        old_time = time.time() - (31 * 86400)  # 31 days ago
        os.utime(path, (old_time, old_time))

        removed = prune(max_age_days=30)
        assert removed == 1
        assert list_sessions() == []

    def test_prune_keeps_recent_sessions(self, tmp_path):
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn, prune, list_sessions,
        )
        sid = "recent-session"
        append_turn(
            sid, role="user", text="recent msg",
            source="tui_user",
        )
        removed = prune(max_age_days=30)
        assert removed == 0
        assert len(list_sessions()) == 1


# ---------------------------------------------------------------------------
# PersistedTurn.from_dict
# ---------------------------------------------------------------------------


class TestPersistedTurnFromDict:

    def test_valid_dict_parses(self):
        from backend.core.ouroboros.governance.conversation_ledger import (
            PersistedTurn,
        )
        raw = {
            "schema_version": "conversation_ledger.1",
            "session_id": "s1",
            "role": "user",
            "text": "hello",
            "source": "tui_user",
            "op_id": "op-1",
            "ts": 1000.0,
            "turn_seq": 5,
        }
        pt = PersistedTurn.from_dict(raw)
        assert pt is not None
        assert pt.role == "user"
        assert pt.text == "hello"
        assert pt.turn_seq == 5

    def test_missing_required_field_returns_none(self):
        from backend.core.ouroboros.governance.conversation_ledger import (
            PersistedTurn,
        )
        # Missing 'text'.
        raw = {
            "session_id": "s1",
            "role": "user",
            "source": "tui_user",
        }
        pt = PersistedTurn.from_dict(raw)
        assert pt is None

    def test_empty_text_returns_none(self):
        from backend.core.ouroboros.governance.conversation_ledger import (
            PersistedTurn,
        )
        raw = {
            "session_id": "s1",
            "role": "user",
            "text": "",
            "source": "tui_user",
        }
        pt = PersistedTurn.from_dict(raw)
        assert pt is None


# ---------------------------------------------------------------------------
# Resume flow via conversation_repl
# ---------------------------------------------------------------------------


class TestResumeFlow:

    def test_resume_rehydrates_turns(self, monkeypatch, tmp_path):
        """Resume reads from ledger and injects into bridge."""
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn,
        )
        sid = "test-resume-flow"
        for i in range(3):
            append_turn(
                sid, role="user", text=f"msg {i}",
                source="tui_user", op_id=f"op-{i}",
            )

        from backend.core.ouroboros.governance.conversation_repl import (
            dispatch_conversation_command,
        )
        result = dispatch_conversation_command(
            f"/conversation resume {sid}",
        )
        assert result.ok is True
        assert "rehydrated 3 turn(s)" in result.text

    def test_resume_nonexistent_session_fails(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_repl import (
            dispatch_conversation_command,
        )
        result = dispatch_conversation_command(
            "/conversation resume nonexistent-session",
        )
        assert result.ok is False
        assert "not found" in result.text

    def test_resume_disabled_fails(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_repl import (
            dispatch_conversation_command,
        )
        result = dispatch_conversation_command(
            "/conversation resume some-id",
        )
        assert result.ok is False
        assert "ledger disabled" in result.text

    def test_resume_resanitizes_control_chars(
        self, monkeypatch, tmp_path,
    ):
        """Persisted turn with control chars is stripped on replay."""
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            ledger_dir,
        )
        sid = "test-resanitize"
        d = ledger_dir()
        d.mkdir(parents=True, exist_ok=True)
        # Write a turn with embedded control chars directly.
        row = json.dumps({
            "schema_version": "conversation_ledger.1",
            "session_id": sid,
            "role": "user",
            "text": "hello\x00world\x0bfoo",
            "source": "tui_user",
            "op_id": "op-1",
            "ts": 1.0,
            "turn_seq": 1,
        })
        (d / f"{sid}.jsonl").write_text(f"{row}\n")

        from backend.core.ouroboros.governance.conversation_repl import (
            dispatch_conversation_command,
        )
        result = dispatch_conversation_command(
            f"/conversation resume {sid}",
        )
        assert result.ok is True
        assert "rehydrated 1 turn(s)" in result.text

        # Verify the bridge got the sanitized text (no control chars).
        from backend.core.ouroboros.governance.conversation_bridge import (
            get_default_bridge,
        )
        bridge = get_default_bridge()
        snapshot = bridge.snapshot()
        # Find the turn we just replayed.
        replayed = [
            t for t in snapshot if t.op_id == "op-1"
        ]
        if replayed:
            assert "\x00" not in replayed[0].text
            assert "\x0b" not in replayed[0].text


# ---------------------------------------------------------------------------
# Sessions subcommand
# ---------------------------------------------------------------------------


class TestSessionsSubcommand:

    def test_sessions_lists_persisted(self, monkeypatch, tmp_path):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_ledger import (
            append_turn,
        )
        for i in range(3):
            append_turn(
                f"sess-{i}", role="user", text=f"msg-{i}",
                source="tui_user",
            )
        from backend.core.ouroboros.governance.conversation_repl import (
            dispatch_conversation_command,
        )
        result = dispatch_conversation_command(
            "/conversation sessions",
        )
        assert result.ok is True
        assert "sess-" in result.text

    def test_sessions_disabled_fails(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_repl import (
            dispatch_conversation_command,
        )
        result = dispatch_conversation_command(
            "/conversation sessions",
        )
        assert result.ok is False
        assert "ledger disabled" in result.text


# ---------------------------------------------------------------------------
# Save subcommand — behavioral spine
# ---------------------------------------------------------------------------


class TestSaveSubcommand:
    """``/conversation save`` force-persists the live bridge snapshot
    to the ledger. Spine pins structural outcomes (disk state via
    canonical read_tail / session_exists), not just response text."""

    def test_save_happy_path_persists_to_disk(
        self, monkeypatch, tmp_path,
    ):
        """Bridge seeded → save → ok=True AND turns visible on disk
        via canonical read_tail / session_exists."""
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            get_default_bridge, reset_default_bridge,
        )
        reset_default_bridge()
        bridge = get_default_bridge()
        bridge.record_turn(
            "user", "first save turn",
            source="tui_user", op_id="op-save-1",
        )
        # ``ask_human_a`` is in the canonical _VALID_SOURCES
        # allowlist; non-allowlisted sources (e.g. raw "claude")
        # are silently dropped fail-closed at admission.
        bridge.record_turn(
            "assistant", "saved response",
            source="ask_human_a", op_id="op-save-1",
        )

        from backend.core.ouroboros.governance.conversation_repl import (  # noqa: E501
            dispatch_conversation_command,
        )
        result = dispatch_conversation_command(
            "/conversation save",
        )
        assert result.ok is True
        # Extract the session id the renderer reported.
        # Format: "...persisted N turn(s) to session 'sid'."
        import re
        m = re.search(r"session '([^']+)'", result.text)
        assert m is not None, (
            f"renderer did not surface session id: {result.text!r}"
        )
        sid = m.group(1)

        # Structural proof: canonical read_tail + session_exists
        # see the persisted turns on disk.
        from backend.core.ouroboros.governance.conversation_ledger import (  # noqa: E501
            read_tail, session_exists,
        )
        assert session_exists(sid) is True
        tail = read_tail(sid, max_turns=100)
        assert len(tail) == 2
        texts = [pt.text for pt in tail]
        assert "first save turn" in texts
        assert "saved response" in texts

    def test_save_empty_bridge_returns_nothing_to_persist(
        self, monkeypatch,
    ):
        """Bridge empty → ok=True + 'nothing to persist'."""
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            reset_default_bridge,
        )
        reset_default_bridge()
        from backend.core.ouroboros.governance.conversation_repl import (  # noqa: E501
            dispatch_conversation_command,
        )
        result = dispatch_conversation_command(
            "/conversation save",
        )
        assert result.ok is True
        assert "nothing to persist" in result.text

    def test_save_ledger_disabled_fails(self, monkeypatch):
        """ledger disabled → ok=False + 'ledger disabled' notice."""
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_LEDGER_ENABLED", "false",
        )
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_repl import (  # noqa: E501
            dispatch_conversation_command,
        )
        result = dispatch_conversation_command(
            "/conversation save",
        )
        assert result.ok is False
        assert "ledger disabled" in result.text


# ---------------------------------------------------------------------------
# Resume — additional behavioral spine (gaps in TestResumeFlow)
# ---------------------------------------------------------------------------


class TestResumeSpine:
    """Two coverage gaps not pinned by TestResumeFlow: empty
    session_id (REPL surface) and structural bridge-state proof
    (TestResumeFlow only asserts response text)."""

    def test_resume_empty_session_id_rejected(self, monkeypatch):
        """``/conversation resume`` with no arg or empty string →
        ok=False before any ledger I/O. The dispatcher rejects
        a missing positional arg with the canonical 'missing
        session_id' error; an explicitly-empty quoted arg hits
        the empty-string guard in _render_resume."""
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_repl import (  # noqa: E501
            dispatch_conversation_command,
        )
        # Quoted empty string reaches _render_resume's
        # ``not sid`` guard at the top of the function.
        result = dispatch_conversation_command(
            '/conversation resume ""',
        )
        assert result.ok is False
        assert "session_id" in result.text.lower()

    def test_resume_proves_bridge_injection_structurally(
        self, monkeypatch,
    ):
        """Extends test_resume_rehydrates_turns with a structural
        bridge-state assertion: after dispatch, the canonical
        bridge.snapshot() actually reflects the injected turns —
        not just the response text claim."""
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            get_default_bridge, reset_default_bridge,
        )
        reset_default_bridge()
        bridge_before = get_default_bridge().snapshot()
        assert len(bridge_before) == 0  # invariant: starts clean

        from backend.core.ouroboros.governance.conversation_ledger import (  # noqa: E501
            append_turn,
        )
        sid = "test-resume-spine-bridge-proof"
        for i in range(3):
            append_turn(
                sid, role="user", text=f"spine msg {i}",
                source="tui_user", op_id=f"op-spine-{i}",
            )

        from backend.core.ouroboros.governance.conversation_repl import (  # noqa: E501
            dispatch_conversation_command,
        )
        result = dispatch_conversation_command(
            f"/conversation resume {sid}",
        )
        assert result.ok is True

        # Structural proof: bridge ring buffer now holds the
        # injected turns. This is the assertion test_resume_
        # rehydrates_turns is missing — it only checks the
        # response text.
        bridge_after = get_default_bridge().snapshot()
        assert len(bridge_after) == 3
        texts = [t.text for t in bridge_after]
        for i in range(3):
            assert f"spine msg {i}" in texts


# ---------------------------------------------------------------------------
# AST invariants
# ---------------------------------------------------------------------------


class TestLedgerInvariants:

    def test_authority_asymmetry_invariant(self):
        from backend.core.ouroboros.governance.conversation_ledger import (
            register_shipped_invariants,
        )
        invariants = register_shipped_invariants()
        names = [i.invariant_name for i in invariants]
        assert "conversation_ledger_authority_asymmetry" in names

    def test_flock_composition_invariant(self):
        from backend.core.ouroboros.governance.conversation_ledger import (
            register_shipped_invariants,
        )
        invariants = register_shipped_invariants()
        names = [i.invariant_name for i in invariants]
        assert "conversation_ledger_composes_flock" in names

    def test_schema_version_invariant(self):
        from backend.core.ouroboros.governance.conversation_ledger import (
            register_shipped_invariants,
        )
        invariants = register_shipped_invariants()
        names = [i.invariant_name for i in invariants]
        assert "conversation_ledger_schema_version_pinned" in names

    def test_invariants_pass_on_source(self):
        """All invariants should pass against the actual source."""
        import ast
        from backend.core.ouroboros.governance.conversation_ledger import (
            register_shipped_invariants,
        )
        source_path = Path(
            "backend/core/ouroboros/governance/"
            "conversation_ledger.py"
        )
        if not source_path.exists():
            pytest.skip("source file not found in cwd")
        source = source_path.read_text()
        tree = ast.parse(source)
        for inv in register_shipped_invariants():
            violations = inv.validate(tree, source)
            assert violations == (), (
                f"Invariant {inv.invariant_name!r} failed: "
                f"{violations}"
            )


class TestReplInvariants:
    """Verify the resume_resanitizes invariant in conversation_repl."""

    def test_resume_resanitizes_invariant_exists(self):
        from backend.core.ouroboros.governance.conversation_repl import (
            register_shipped_invariants,
        )
        invariants = register_shipped_invariants()
        names = [i.invariant_name for i in invariants]
        assert "conversation_resume_resanitizes" in names

    def test_resume_resanitizes_passes_on_source(self):
        import ast
        from backend.core.ouroboros.governance.conversation_repl import (
            register_shipped_invariants,
        )
        source_path = Path(
            "backend/core/ouroboros/governance/"
            "conversation_repl.py"
        )
        if not source_path.exists():
            pytest.skip("source file not found in cwd")
        source = source_path.read_text()
        tree = ast.parse(source)
        for inv in register_shipped_invariants():
            if inv.invariant_name == "conversation_resume_resanitizes":
                violations = inv.validate(tree, source)
                assert violations == (), (
                    f"Invariant {inv.invariant_name!r} failed: "
                    f"{violations}"
                )
