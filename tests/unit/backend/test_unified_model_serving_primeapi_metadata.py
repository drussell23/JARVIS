import json

import pytest

from backend.intelligence.unified_model_serving import (
    ModelRequest,
    PrimeAPIClient,
    TaskType,
)


class _FakeResponse:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class _AsyncBytes:
    def __init__(self, lines):
        self._lines = lines

    def __aiter__(self):
        self._iter = iter(self._lines)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeStreamResponse(_FakeResponse):
    def __init__(self, status, lines, headers=None):
        super().__init__(status=status, payload={}, headers=headers)
        self.content = _AsyncBytes(lines)


class _FakeSession:
    def __init__(self):
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        if json.get("stream"):
            return _FakeStreamResponse(
                status=200,
                lines=[
                    b'data: {"choices":[{"delta":{"content":"hi"}}]}\n',
                    b"data: [DONE]\n",
                ],
            )
        return _FakeResponse(
            status=200,
            payload={
                "model": "prime-test",
                "choices": [{"message": {"content": "hello"}}],
                "usage": {"total_tokens": 7},
            },
        )


@pytest.mark.asyncio
async def test_prime_api_client_generate_forwards_metadata(monkeypatch):
    client = PrimeAPIClient(base_url="http://prime.test")
    client._ready = True
    client._available_models = ["test-model"]
    fake_session = _FakeSession()

    async def _fake_get_session():
        return fake_session

    monkeypatch.setattr(client, "_get_session", _fake_get_session)

    request = ModelRequest(
        messages=[{"role": "user", "content": "solve x^2 = 9"}],
        task_type=TaskType.REASONING,
        context={"task_type": "math_complex", "complexity_level": "COMPLEX"},
    )

    response = await client.generate(request)

    assert response.success is True
    assert len(fake_session.calls) == 1
    payload = fake_session.calls[0][1]
    assert payload["metadata"]["task_type"] == "math_complex"
    assert payload["metadata"]["complexity_level"] == "COMPLEX"
    assert payload["metadata"]["model_task_type"] == "reasoning"


@pytest.mark.asyncio
async def test_prime_api_client_generate_stream_forwards_metadata(monkeypatch):
    client = PrimeAPIClient(base_url="http://prime.test")
    client._ready = True
    client._available_models = ["test-model"]
    fake_session = _FakeSession()

    async def _fake_get_session():
        return fake_session

    monkeypatch.setattr(client, "_get_session", _fake_get_session)

    request = ModelRequest(
        messages=[{"role": "user", "content": "write a python function"}],
        task_type=TaskType.CODE,
        context={"task_type": "code_simple", "complexity_level": "SIMPLE"},
    )

    chunks = [chunk async for chunk in client.generate_stream(request)]

    assert chunks == ["hi"]
    assert len(fake_session.calls) == 1
    payload = fake_session.calls[0][1]
    assert payload["stream"] is True
    assert payload["metadata"]["task_type"] == "code_simple"
    assert payload["metadata"]["complexity_level"] == "SIMPLE"
    assert payload["metadata"]["model_task_type"] == "code"
