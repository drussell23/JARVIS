import pytest

from backend.audio.conversation_pipeline import (
    ConversationPipeline,
    ConversationSession,
)


class _IntentClassifier:
    def __init__(self, intent: str, confidence: float):
        self._intent = intent
        self._confidence = confidence

    def predict_intent(self, _text: str):
        return {
            "intent": self._intent,
            "confidence": self._confidence,
            "source": "test",
        }


class _CommandProcessor:
    def __init__(self, response="Executed"):
        self.calls = []
        self.response = response

    async def process_command(self, text: str):
        self.calls.append(text)
        return {"success": True, "response": self.response}


class _TTS:
    def __init__(self):
        self.spoken = []

    async def speak(self, text, play_audio=True, source=None):
        self.spoken.append((text, play_audio, source))


class _LLMClient:
    def __init__(self):
        self.requests = []

    async def generate_stream(self, request):
        self.requests.append(request)
        yield "ok"


@pytest.mark.asyncio
async def test_intent_routing_executes_for_confident_command_intent():
    pipeline = ConversationPipeline(
        intent_classifier=_IntentClassifier("system_command", 0.91)
    )

    decision = await pipeline._classify_turn_intent("restart backend services")

    assert decision["route"] == "execute"
    assert decision["intent"] == "system_command"


@pytest.mark.asyncio
async def test_intent_routing_authenticate_for_unlock_phrase():
    pipeline = ConversationPipeline(
        intent_classifier=_IntentClassifier("conversation", 0.95)
    )

    decision = await pipeline._classify_turn_intent("please unlock my screen")

    assert decision["route"] == "authenticate"
    assert decision["intent"] == "authenticate"


@pytest.mark.asyncio
async def test_execute_command_turn_speaks_and_appends_assistant_turn():
    command_processor = _CommandProcessor(response="Done. I opened Safari.")
    tts = _TTS()
    pipeline = ConversationPipeline(
        command_processor=command_processor,
        tts_engine=tts,
    )
    pipeline._session = ConversationSession()
    pipeline._session.add_turn("user", "open safari")

    handled = await pipeline._execute_command_turn(
        "open safari",
        intent_decision={"intent": "system_command"},
    )

    assert handled is True
    assert command_processor.calls == ["open safari"]
    assert pipeline._session.turns[-1].role == "assistant"
    assert "opened Safari" in pipeline._session.turns[-1].text
    assert tts.spoken


@pytest.mark.asyncio
async def test_llm_request_includes_task_metadata(monkeypatch):
    llm = _LLMClient()
    pipeline = ConversationPipeline(llm_client=llm)

    async def _fake_infer(_text: str):
        return "math_complex", "COMPLEX"

    monkeypatch.setattr(pipeline, "_infer_task_route", _fake_infer)

    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "solve x + 2 = 7"},
    ]
    chunks = [
        chunk
        async for chunk in pipeline._get_llm_stream(
            messages=messages,
            user_text="solve x + 2 = 7",
            intent_decision={"intent": "information_query", "confidence": 0.8},
        )
    ]

    assert chunks == ["ok"]
    assert len(llm.requests) == 1
    request = llm.requests[0]
    assert request.context["task_type"] == "math_complex"
    assert request.context["complexity_level"] == "COMPLEX"
    assert request.context["conversation_intent"] == "information_query"
    assert request.task_type.value == "reasoning"
