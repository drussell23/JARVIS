"""The chat surface reads like a conversation, not a bot's diary.

Born from the 2026-07-28 operator screenshot: four stacked Karen
greetings, a raw classifier dump as the "reply", and the typed question
homeless. Root causes and their pins:

  * the raw dump came from a STALE BINARY (the pyenv-shim trap) — the
    current renderer was already right, and the sentinel now says so
    loudly instead of letting a ghost interface masquerade as current;
  * greetings had no memory — the orchestrator now collapses a re-greet
    inside the cooldown to one quiet "Still here.";
  * the typed line now lands as a ❯ anchor block the moment Enter is
    pressed, on BOTH cockpit surfaces.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance import conversation_orchestrator as co


# --------------------------------------------------------------------------
# 1. greeting dedupe
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_greet_ledger():
    co._LAST_GREET.clear()
    yield
    co._LAST_GREET.clear()


def _social_action(orch, session):
    turn, decision = orch.dispatch("hey", session_id=session)
    assert decision.action == "social_ack"
    return decision.payload.get("reply", "")


def test_regreet_collapses_to_still_here() -> None:
    orch = co.ConversationOrchestrator()
    first = _social_action(orch, "s1")
    assert first and first != "Still here."
    assert _social_action(orch, "s1") == "Still here."
    assert _social_action(orch, "s1") == "Still here."


def test_sessions_greet_independently() -> None:
    orch = co.ConversationOrchestrator()
    _social_action(orch, "s1")
    other = _social_action(orch, "s2")
    assert other != "Still here."   # a NEW terminal deserves a hello


def test_cooldown_expiry_restores_the_rotation(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_CHAT_REGREET_COOLDOWN_S", "0")
    orch = co.ConversationOrchestrator()
    _social_action(orch, "s1")
    assert _social_action(orch, "s1") != "Still here."


def test_greet_ledger_is_bounded() -> None:
    orch = co.ConversationOrchestrator()
    for i in range(80):
        _social_action(orch, f"s{i}")
    assert len(co._LAST_GREET) <= 64


# --------------------------------------------------------------------------
# 2. the reply genre is voice + VERBOSE trace (regression pin for the
#    screenshot's dump — already true on main, must stay true)
# --------------------------------------------------------------------------

def test_claude_query_reply_is_voice_not_classifier_dump() -> None:
    from backend.core.ouroboros.governance.chat_response_style import (
        compose_reply,
    )
    reply = compose_reply(
        "claude_query", message="how you doing?",
        receipt="logged-claude-chat-abc(ctx=5)",
        turn_id="chat-abc", session_id="repl",
        intent="EXPLANATION", confidence=0.33,
        reasons=("question_shape",), reason="explanation verb",
        verbose=False,
    )
    # the operator-facing line is prose, the machine facts are absent
    assert "intent:" not in reply
    assert "conf=0.33" not in reply
    assert "question_shape" not in reply


# --------------------------------------------------------------------------
# 3. the ❯ message anchor
# --------------------------------------------------------------------------

class _SinkUI:
    def __init__(self):
        self.lines = []
        self.markup_sink = lambda ln, addressed=False: self.lines.append(ln)

    def flash(self, *_a, **_k):
        pass

    def should_flush_on_input(self):
        return False


class _NullClient:
    connected = True

    def send_input(self, _t):
        return True

    def send_audio(self, _c):
        return True

    def send_history(self, _t):
        return True


def test_submitted_text_lands_as_an_anchor_block() -> None:
    from backend.core.ouroboros.cli.ov import _route_operator_line
    ui = _SinkUI()
    outcome = _route_operator_line(_NullClient(), ui, "how you doing?")
    assert outcome == "sent"
    assert any("❯" in ln and "how you doing?" in ln for ln in ui.lines)


def test_multiline_anchor_indents_continuations() -> None:
    from backend.core.ouroboros.cli.ov import _echo_operator_line
    ui = _SinkUI()
    _echo_operator_line(ui, "line one\nline two")
    assert "❯" in ui.lines[0] and "line one" in ui.lines[0]
    assert "❯" not in ui.lines[1] and "line two" in ui.lines[1]


def test_anchor_escapes_markup_and_respects_kill_switch(
    monkeypatch,
) -> None:
    from backend.core.ouroboros.cli.ov import _echo_operator_line
    ui = _SinkUI()
    _echo_operator_line(ui, "[red]not a tag[/red]")
    assert "\\[red]" in ui.lines[0]
    monkeypatch.setenv("JARVIS_OPERATOR_ECHO_ENABLED", "false")
    before = len(ui.lines)
    _echo_operator_line(ui, "silent")
    assert len(ui.lines) == before


def test_shell_mode_owns_its_own_chrome() -> None:
    from pathlib import Path as _P
    import backend.core.ouroboros.cli.ov as ov
    src = _P(ov.__file__).read_text()
    block = src.split("The message anchor")[1][:220]
    assert 'not text.startswith("!")' in block


def test_daemon_cockpit_echoes_too() -> None:
    import inspect
    from backend.core.ouroboros.battle_test import serpent_flow
    src = inspect.getsource(serpent_flow.SerpentREPL._loop)
    accept = src.split("def _on_accept")[1]
    accept = accept.split("_aio.ensure_future")[0]
    assert "❯" in accept and "JARVIS_OPERATOR_ECHO_ENABLED" in accept


# --------------------------------------------------------------------------
# 4. the stale-binary sentinel
# --------------------------------------------------------------------------

def test_sentinel_silent_when_code_is_the_repo(monkeypatch) -> None:
    from backend.core.ouroboros.battle_test.daemon_provenance import (
        client_binary_warning,
    )
    import backend as b
    code_root = Path(b.__file__).resolve().parent.parent
    monkeypatch.setenv("JARVIS_REPO_PATH", str(code_root))
    assert client_binary_warning() == ""


def test_sentinel_loud_when_code_is_a_shadow_copy(monkeypatch, tmp_path):
    from backend.core.ouroboros.battle_test.daemon_provenance import (
        client_binary_warning,
    )
    monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
    warning = client_binary_warning()
    assert "stale ov binary" in warning
    assert "pip install -e" in warning


def test_sentinel_reaches_both_attach_surfaces() -> None:
    import backend.core.ouroboros.cli.ov as ov
    src = Path(ov.__file__).read_text()
    assert "client_binary_warning" in src
    assert "boot_warnings" in src           # survives the alt-screen mount
