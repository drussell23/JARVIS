"""Regression spine for §40 Wave 3 #17 — ConversationBridge V1.2
voice integration.

Covers:

* V1.2 ``_capture_voice`` sub-flag default-TRUE
* ``record_voice_transcript`` public entry point — every gate
  + defensive branch (master off, sub-flag off, empty/None text,
  bad confidence)
* SOURCE_VOICE turns land in the canonical ring buffer + flow
  through Tier -1 sanitizer + secret redaction
* SSE event ``voice_transcript_recorded`` registered in canonical
  ``_VALID_EVENT_TYPES`` frozenset
* JarvisVoiceBridge.on_transcript composes record_voice_transcript
  (source-AST check — Pyright can't break the wiring silently)
* FlagRegistry seed auto-discovered (4 specs: master + 3 sub-flags)
* Beacon payload bounds (no raw text leaks to SSE)
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import (
    conversation_bridge as cb,
)
from backend.core.ouroboros.governance.conversation_bridge import (
    SOURCE_ASK_HUMAN_Q,
    SOURCE_POSTMORTEM,
    SOURCE_TUI_USER,
    SOURCE_VOICE,
    _capture_ask_human,
    _capture_postmortem,
    _capture_voice,
    get_default_bridge,
    record_voice_transcript,
    reset_default_bridge,
)


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    reset_default_bridge()
    for env in (
        "JARVIS_CONVERSATION_BRIDGE_ENABLED",
        "JARVIS_CONVERSATION_BRIDGE_CAPTURE_VOICE",
        "JARVIS_CONVERSATION_BRIDGE_CAPTURE_ASK_HUMAN",
        "JARVIS_CONVERSATION_BRIDGE_CAPTURE_POSTMORTEM",
    ):
        monkeypatch.delenv(env, raising=False)
    yield
    reset_default_bridge()


@pytest.fixture
def master_on(monkeypatch):
    monkeypatch.setenv("JARVIS_CONVERSATION_BRIDGE_ENABLED", "true")


# ---------------------------------------------------------------------------
# V1.2 sub-flag default-TRUE when master on
# ---------------------------------------------------------------------------


class TestVoiceSubFlag:
    def test_default_true(self):
        """V1.2 sub-gate default-TRUE — voice flows the moment
        operator flips the master flag."""
        assert _capture_voice() is True

    @pytest.mark.parametrize(
        "falsy", ["0", "false", "no", "off"],
    )
    def test_explicit_false(self, monkeypatch, falsy):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_CAPTURE_VOICE", falsy,
        )
        assert _capture_voice() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on"])
    def test_explicit_true(self, monkeypatch, truthy):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_CAPTURE_VOICE", truthy,
        )
        assert _capture_voice() is True


# ---------------------------------------------------------------------------
# record_voice_transcript — every gate
# ---------------------------------------------------------------------------


class TestRecordVoiceTranscript:
    def test_master_off_returns_false(self):
        """Default master off → record returns False, no turns
        admitted."""
        ok = record_voice_transcript("hello")
        assert ok is False
        bridge = get_default_bridge()
        assert bridge.snapshot() == []

    def test_master_on_records_voice_source(self, master_on):
        ok = record_voice_transcript("hello jarvis")
        assert ok is True
        turns = get_default_bridge().snapshot()
        assert len(turns) == 1
        assert turns[0].source == SOURCE_VOICE
        assert turns[0].role == "user"
        assert turns[0].text == "hello jarvis"

    def test_sub_flag_off_drops_voice(
        self, monkeypatch, master_on,
    ):
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_CAPTURE_VOICE", "false",
        )
        ok = record_voice_transcript("voice should not land")
        assert ok is False
        assert get_default_bridge().snapshot() == []

    def test_op_id_threaded(self, master_on):
        ok = record_voice_transcript(
            "op-tagged voice", op_id="op-abc123",
        )
        assert ok is True
        turns = get_default_bridge().snapshot()
        assert turns[0].op_id == "op-abc123"

    @pytest.mark.parametrize(
        "bad", ["", "   ", "\n", "\t  \n"],
    )
    def test_empty_or_whitespace_drops(self, master_on, bad):
        ok = record_voice_transcript(bad)
        assert ok is False
        assert get_default_bridge().snapshot() == []

    def test_non_str_input_drops(self, master_on):
        # Defensive against ASR producers that pass None
        assert record_voice_transcript(None) is False  # type: ignore[arg-type]
        assert record_voice_transcript(42) is False  # type: ignore[arg-type]

    def test_confidence_accepted_but_optional(self, master_on):
        # Confidence supplied
        assert record_voice_transcript(
            "high-confidence utterance", confidence=0.95,
        ) is True
        # Confidence omitted
        reset_default_bridge()
        assert record_voice_transcript(
            "no-confidence utterance",
        ) is True


# ---------------------------------------------------------------------------
# Tier -1 sanitizer + secret redaction (V1.1 pipeline reused)
# ---------------------------------------------------------------------------


class TestSanitizerPipeline:
    def test_secret_redaction_applies_to_voice(self, master_on):
        """Voice transcripts run through the SAME Tier -1
        sanitizer + redaction pipeline as tui_user input.
        Operator-binding load-bearing: voice MUST NOT bypass
        the credential-shape regex set."""
        ok = record_voice_transcript(
            "my api key is sk-abcdefghijklmnopqrstuv1234567890",
        )
        assert ok is True
        turns = get_default_bridge().snapshot()
        assert len(turns) == 1
        # Raw secret MUST NOT appear; redacted marker MUST
        assert "sk-abcdef" not in turns[0].text
        assert "REDACTED" in turns[0].text

    def test_control_chars_stripped(self, master_on):
        ok = record_voice_transcript(
            "hello\x00voice\x01world",
        )
        assert ok is True
        text = get_default_bridge().snapshot()[0].text
        assert "\x00" not in text
        assert "\x01" not in text


# ---------------------------------------------------------------------------
# Cross-source coexistence — voice doesn't interfere with V1.1
# ---------------------------------------------------------------------------


class TestCoexistence:
    def test_voice_and_tui_user_both_recorded(
        self, master_on,
    ):
        bridge = get_default_bridge()
        record_voice_transcript("voice line")
        bridge.record_turn(
            role="user", text="tui line", source=SOURCE_TUI_USER,
        )
        turns = bridge.snapshot()
        sources = {t.source for t in turns}
        assert SOURCE_VOICE in sources
        assert SOURCE_TUI_USER in sources

    def test_voice_subflag_off_doesnt_affect_tui(
        self, monkeypatch, master_on,
    ):
        """Operator can shed voice while keeping TUI input."""
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_CAPTURE_VOICE", "false",
        )
        record_voice_transcript("voice should drop")
        get_default_bridge().record_turn(
            role="user", text="tui should land",
            source=SOURCE_TUI_USER,
        )
        turns = get_default_bridge().snapshot()
        sources = {t.source for t in turns}
        assert SOURCE_VOICE not in sources
        assert SOURCE_TUI_USER in sources

    def test_all_three_subflags_independent(self, monkeypatch):
        """Each sub-flag controls only its own source."""
        # Ask-human + postmortem off, voice on
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_CAPTURE_ASK_HUMAN",
            "false",
        )
        monkeypatch.setenv(
            "JARVIS_CONVERSATION_BRIDGE_CAPTURE_POSTMORTEM",
            "false",
        )
        assert _capture_voice() is True
        assert _capture_ask_human() is False
        assert _capture_postmortem() is False


# ---------------------------------------------------------------------------
# SOURCE_VOICE constant integrity
# ---------------------------------------------------------------------------


class TestSourceVoiceConstant:
    def test_source_voice_value(self):
        assert SOURCE_VOICE == "voice"

    def test_source_voice_in_allowed_sources(self):
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            _ALLOWED_SOURCES,
        )
        assert SOURCE_VOICE in _ALLOWED_SOURCES


# ---------------------------------------------------------------------------
# SSE event registration
# ---------------------------------------------------------------------------


class TestSseEvent:
    def test_voice_event_registered_in_valid_set(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_VOICE_TRANSCRIPT_RECORDED,
            _VALID_EVENT_TYPES,
        )
        assert (
            EVENT_TYPE_VOICE_TRANSCRIPT_RECORDED
            in _VALID_EVENT_TYPES
        )
        assert (
            EVENT_TYPE_VOICE_TRANSCRIPT_RECORDED
            == "voice_transcript_recorded"
        )

    def test_beacon_payload_omits_raw_text(self, master_on):
        """Load-bearing privacy: the beacon publishes length +
        op_id + confidence only. Raw transcript MUST NOT
        traverse the SSE stream."""
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            _publish_voice_transcript_beacon,
        )
        # Inspect source-AST: payload dict should reference
        # 'length_chars' / 'op_id' / 'confidence' but NEVER
        # a 'text' or 'transcript' key.
        src = Path(cb.__file__).read_text(encoding="utf-8")
        # Find the payload dict in the beacon function
        tree = ast.parse(src)
        payload_keys: List[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "_publish_voice_transcript_beacon"
            ):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Dict):
                        for key in sub.keys:
                            if isinstance(key, ast.Constant):
                                payload_keys.append(
                                    str(key.value),
                                )
                break
        # 'text' / 'transcript' must NOT appear as payload keys
        assert "text" not in payload_keys
        assert "transcript" not in payload_keys
        # Required bounded keys present
        assert "length_chars" in payload_keys


# ---------------------------------------------------------------------------
# JarvisVoiceBridge.on_transcript wiring (structural)
# ---------------------------------------------------------------------------


class TestVoiceBridgeWiring:
    """The voice subsystem's on_transcript hook MUST compose
    record_voice_transcript. Source AST pin guards against
    regression — if the wiring disappears, V1.2 silently no-ops."""

    def test_voice_bridge_imports_record_voice_transcript(self):
        path = Path(
            "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
            "backend/voice/jarvis_voice_bridge.py",
        )
        src = path.read_text(encoding="utf-8")
        # The wiring is a lazy-import — must reference both
        # the module path AND the public function name
        assert (
            "from backend.core.ouroboros.governance."
            "conversation_bridge import" in src
        )
        assert "record_voice_transcript" in src

    def test_voice_bridge_calls_record_inside_on_transcript(self):
        path = Path(
            "/Users/djrussell23/Documents/repos/JARVIS-AI-Agent/"
            "backend/voice/jarvis_voice_bridge.py",
        )
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        # Find on_transcript, check for record_voice_transcript call
        wired = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "on_transcript"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "record_voice_transcript"
                    ):
                        wired = True
                        break
                break
        assert wired, (
            "JarvisVoiceBridge.on_transcript MUST call "
            "record_voice_transcript — V1.2 wiring regression"
        )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


class TestFlagSeeds:
    def test_all_4_seeds_auto_discovered(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        names = {f.name for f in reg.list_all()}
        for expected in [
            "JARVIS_CONVERSATION_BRIDGE_ENABLED",
            "JARVIS_CONVERSATION_BRIDGE_CAPTURE_VOICE",
            "JARVIS_CONVERSATION_BRIDGE_CAPTURE_ASK_HUMAN",
            "JARVIS_CONVERSATION_BRIDGE_CAPTURE_POSTMORTEM",
        ]:
            assert expected in names, f"missing seed: {expected}"

    def test_master_seed_safety_default_false(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            f for f in reg.list_all()
            if f.name == "JARVIS_CONVERSATION_BRIDGE_ENABLED"
        )
        assert spec.default is False
        assert spec.category.value == "safety"

    def test_voice_seed_integration_default_true(self):
        from backend.core.ouroboros.governance import (
            flag_registry as fr,
        )
        fr.reset_default_registry()
        reg = fr.ensure_seeded()
        spec = next(
            f for f in reg.list_all()
            if f.name == "JARVIS_CONVERSATION_BRIDGE_CAPTURE_VOICE"
        )
        assert spec.default is True
        assert spec.category.value == "integration"


# ---------------------------------------------------------------------------
# Defensive — record_voice_transcript NEVER raises
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_substrate_unavailable_returns_false(
        self, monkeypatch, master_on,
    ):
        """When the bridge singleton accessor raises (e.g.,
        construction failure), record_voice_transcript must
        return False without propagating."""
        def _boom(*args, **kwargs):
            raise RuntimeError("singleton failed")
        monkeypatch.setattr(
            cb, "get_default_bridge", _boom,
        )
        assert record_voice_transcript("anything") is False


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


class TestPublicApi:
    def test_record_voice_transcript_exposed(self):
        from backend.core.ouroboros.governance import (
            conversation_bridge,
        )
        assert hasattr(
            conversation_bridge, "record_voice_transcript",
        )

    def test_source_voice_exposed(self):
        from backend.core.ouroboros.governance import (
            conversation_bridge,
        )
        assert hasattr(conversation_bridge, "SOURCE_VOICE")
        assert conversation_bridge.SOURCE_VOICE == "voice"
