"""The sampling point must survive every seam between the ladder and the wire.

`sibling_entropy` computes a four-part sampling point per draw --
temperature, top_p, top_k, repeat_penalty -- plus a per-(op, draw,
escalation) seed. Until the change this file guards, exactly ONE of those
five values crossed `candidate_generator` -> `PrimeProvider.generate`:
`temperature`, because that was the only parameter the provider signature
accepted. The other four were computed, printed into the sibling log line by
`describe()`, and dropped.

The failure that produced was silent and expensive. The logs showed
`T=1.10 top_p=0.90 top_k=140 rp=1.15 seed=796451960` and looked fully
wired; the requests carried a temperature and nothing else. Soak
`bt-2026-09-02-025257` measured the consequence: siblings drawn at
different seeds came back at structural similarity **1.0000**, one group of
eight draws collapsed to a single `structure_id`, and the corpus reached
`grpo_preflight` with a reward spread of 6e-05 -- far under any floor that
makes GRPO's advantage-over-std meaningful. `sibling_entropy`'s own ladder
comment had already named the mechanism: raising temperature alone
re-weights a tail that `top_k`/`top_p` have already truncated away.

So these tests deliberately assert on the REQUEST BODY, not on a call
signature. A signature test would have passed throughout the entire period
the bug existed -- `generate(temperature=...)` was always accepted and
always forwarded. Only the body distinguishes "the knob is wired" from "the
knob reaches the engine".
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

LID = "backend.core.ouroboros.governance.local_inference_director"
ENT = "backend.core.ouroboros.governance.sibling_entropy"

_FIXED_TS = datetime(2026, 3, 10, 0, 0, 0, tzinfo=timezone.utc)
_FUTURE_DL = datetime(2099, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload):
        self._p = payload
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._p


class _RecordingSession:
    """Captures the exact JSON body posted to the engine."""

    def __init__(self, payload=None):
        self._p = payload or {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"completion_tokens": 3},
        }
        self.posts = []
        self.closed = False

    def post(self, url, **kw):
        self.posts.append((url, kw))
        return _FakeResp(self._p)

    async def close(self):
        self.closed = True

    @property
    def last_body(self):
        assert self.posts, "nothing was posted to the engine"
        return self.posts[-1][1]["json"]


@pytest.fixture()
def lid(monkeypatch):
    import importlib
    mod = importlib.import_module(LID)
    # Streaming would take a different transport; this arc is about the body,
    # and the body is built once for both paths.
    monkeypatch.setenv("JARVIS_LOCAL_STREAMING_ENABLED", "0")
    return mod


@pytest.fixture()
def ent(monkeypatch):
    import importlib
    mod = importlib.import_module(ENT)
    monkeypatch.delenv("JARVIS_SIBLING_ENTROPY_ENABLED", raising=False)
    return mod


def _client(lid, session, **cfg_over):
    cfg = lid.LocalConfig.from_env()
    if cfg_over:
        cfg = lid.LocalConfig(**{**cfg.__dict__, **cfg_over})
    return lid.LocalPrimeClient(cfg, session=session), cfg


# ---------------------------------------------------------------------------
# The wire: a sampling point must appear in the request body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sampling_point_reaches_the_request_body(lid, ent):
    """Every field of a non-legacy point lands where its engine reads it.

    This is the test that would have failed for the whole life of the bug.
    """
    sess = _RecordingSession()
    client, cfg = _client(lid, sess)

    point = ent.sampling_for(2, op_id="op-test")
    assert not point.is_legacy, "draw 2 must be a real ladder rung"

    await client.complete(
        system="<sys/>", user="<task/>", prompt_tokens=100, sampling=point,
    )
    body = sess.last_body

    # OpenAI-compatible spelling: top-level fields.
    assert body["top_p"] == pytest.approx(float(point.top_p))
    assert body["seed"] == int(point.seed)

    # ollama-native spelling: top_k and repeat_penalty have no OpenAI form.
    opts = body["options"]
    assert opts["top_k"] == int(point.top_k)
    assert opts["repeat_penalty"] == pytest.approx(float(point.repeat_penalty))
    assert opts["top_p"] == pytest.approx(float(point.top_p))
    assert opts["seed"] == int(point.seed)

    # Temperature is the one that always worked; it must not have regressed.
    assert body["temperature"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_sampling_survives_the_num_ctx_options_assignment(lid, ent):
    """`num_ctx` assigns `body["options"]` wholesale.

    The sampling block must run AFTER it, or top_k/repeat_penalty are
    silently overwritten -- the same class of "computed, then discarded"
    defect this arc is about, one layer lower.
    """
    sess = _RecordingSession()
    client, _ = _client(lid, sess, num_ctx=8192)

    point = ent.sampling_for(3, op_id="op-ctx")
    await client.complete(
        system="<sys/>", user="<task/>", prompt_tokens=10, sampling=point,
    )
    opts = sess.last_body["options"]

    assert opts["num_ctx"] == 8192, "num_ctx must survive"
    assert opts["top_k"] == int(point.top_k), "sampling must survive num_ctx"
    assert opts["repeat_penalty"] == pytest.approx(float(point.repeat_penalty))


@pytest.mark.asyncio
async def test_distinct_draws_post_distinct_seeds(lid, ent):
    """Two draws of one op must not ask the engine for the same trajectory.

    A reused seed reproduces one trajectory however high the temperature --
    the knob would look wired and change nothing, which is exactly the
    observed 1.0000 similarity.
    """
    sess = _RecordingSession()
    client, _ = _client(lid, sess)

    seeds = []
    for draw in (2, 3):
        point = ent.sampling_for(draw, op_id="op-same")
        await client.complete(
            system="<s/>", user="<u/>", prompt_tokens=10, sampling=point,
        )
        seeds.append(sess.last_body["seed"])

    assert seeds[0] != seeds[1], f"both draws posted seed={seeds[0]}"


@pytest.mark.asyncio
async def test_legacy_draw_posts_no_sampling_fields(lid, ent):
    """Draw 1 must be byte-identical to the pre-entropy request.

    The whole arc is only safe if a non-sibling generation is untouched: a
    regression here would change every op in the system, not just bonus
    draws.
    """
    sess = _RecordingSession()
    client, _ = _client(lid, sess)

    # Both spellings of "no point": an explicit legacy point, and None.
    await client.complete(
        system="<s/>", user="<u/>", prompt_tokens=10,
        sampling=ent.SiblingSampling(),
    )
    body_legacy = sess.last_body
    await client.complete(system="<s/>", user="<u/>", prompt_tokens=10)
    body_none = sess.last_body

    for body in (body_legacy, body_none):
        assert "top_p" not in body
        assert "seed" not in body
        assert "top_k" not in body.get("options", {})
        assert "repeat_penalty" not in body.get("options", {})


# ---------------------------------------------------------------------------
# The override is per-draw, never process-wide
# ---------------------------------------------------------------------------

def test_config_for_draw_never_mutates_the_shared_config(lid, ent):
    """One sibling's seed must not leak into every later op on the client."""
    cfg = lid.LocalConfig.from_env()
    point = ent.sampling_for(2, op_id="op-immutable")

    drawn = lid._config_for_draw(cfg, point)

    assert drawn is not cfg
    assert drawn.top_k == int(point.top_k)
    assert cfg.top_p is None and cfg.top_k is None
    assert cfg.seed is None and cfg.repeat_penalty is None


def test_config_for_draw_accepts_a_plain_mapping(lid):
    """The seam is duck-typed so it can be exercised without a ladder."""
    cfg = lid.LocalConfig.from_env()
    drawn = lid._config_for_draw(cfg, {"top_p": 0.5, "top_k": 7})
    assert drawn.top_p == 0.5 and drawn.top_k == 7


def test_config_for_draw_drops_unknown_fields_instead_of_raising(lid):
    """A field this dataclass has not learned yet must degrade, not fail.

    `dataclasses.replace` raises TypeError on an unknown field, and a
    sampling improvement must never be able to fail a generation.
    """
    cfg = lid.LocalConfig.from_env()
    drawn = lid._config_for_draw(cfg, {"top_p": 0.5, "not_a_field": 1})
    assert drawn.top_p == 0.5
    assert not hasattr(drawn, "not_a_field")


def test_config_for_draw_is_a_noop_without_a_point(lid):
    cfg = lid.LocalConfig.from_env()
    assert lid._config_for_draw(cfg, None) is cfg
    assert lid._config_for_draw(cfg, {}) is cfg


# ---------------------------------------------------------------------------
# The provider seam: PrimeProvider must hand the point to its client
# ---------------------------------------------------------------------------

def _diff_response(file_path: str, orig: str, new_line: str) -> str:
    lines = orig.splitlines()
    ctx = "\n".join(f" {l}" for l in lines)
    n = len(lines)
    return json.dumps({
        "schema_version": "2b.1-diff",
        "candidates": [{
            "candidate_id": "c1",
            "file_path": file_path,
            "unified_diff": f"@@ -1,{n} +1,{n + 1} @@\n{ctx}\n+{new_line}\n",
            "rationale": "test",
        }],
        "provider_metadata": {"model_id": "test-model"},
    })


def _mock_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.content = content
    resp.model = "test"
    resp.tokens_used = 10
    resp.metadata = {}
    return resp


def _ctx_for(target: str):
    from backend.core.ouroboros.governance.op_context import OperationContext
    return OperationContext.create(
        target_files=(target,),
        description="Add a line",
        _timestamp=_FIXED_TS,
    )


@pytest.mark.asyncio
async def test_prime_provider_forwards_the_point_to_its_client(tmp_path, ent):
    """The seam that dropped four of five values.

    Asserted on the client call rather than a signature: the parameter was
    always *accepted* here, it was simply never *passed on*.
    """
    from backend.core.ouroboros.governance.providers import PrimeProvider

    doc = tmp_path / "docs"
    doc.mkdir()
    original = "first line\nsecond line\n"
    (doc / "file.md").write_text(original)

    client = AsyncMock()
    client.generate = AsyncMock(return_value=_mock_response(
        _diff_response("docs/file.md", original.rstrip("\n"), "third line")
    ))

    point = ent.sampling_for(2, op_id="op-forward")
    provider = PrimeProvider(prime_client=client, repo_root=tmp_path)
    await provider.generate(
        _ctx_for("docs/file.md"), _FUTURE_DL,
        temperature=point.temperature, sampling=point,
    )

    assert client.generate.await_count >= 1
    kwargs = client.generate.await_args.kwargs
    assert kwargs.get("sampling") is point, (
        "PrimeProvider dropped the sampling point; only temperature crossed "
        "the seam, which is the original defect"
    )
    assert kwargs.get("temperature") == pytest.approx(float(point.temperature))


@pytest.mark.asyncio
async def test_prime_provider_omits_sampling_for_a_legacy_call(tmp_path):
    """A non-sibling generation must post the pre-entropy request shape."""
    from backend.core.ouroboros.governance.providers import PrimeProvider

    doc = tmp_path / "docs"
    doc.mkdir()
    original = "alpha\nbeta\n"
    (doc / "file.md").write_text(original)

    client = AsyncMock()
    client.generate = AsyncMock(return_value=_mock_response(
        _diff_response("docs/file.md", original.rstrip("\n"), "gamma")
    ))

    provider = PrimeProvider(prime_client=client, repo_root=tmp_path)
    await provider.generate(_ctx_for("docs/file.md"), _FUTURE_DL)

    assert "sampling" not in client.generate.await_args.kwargs


# ---------------------------------------------------------------------------
# The contract: no seat in the cascade may TypeError on a sampling point
# ---------------------------------------------------------------------------

def test_every_provider_seat_accepts_a_sampling_point():
    """One caller describes a draw; any seat may receive it.

    A seat that raised TypeError on `sampling=` would fail the op at the
    cascade boundary rather than degrade to an ordinary generation.
    """
    import inspect
    from backend.core.ouroboros.governance.providers import (
        ClaudeProvider, PrimeProvider,
    )
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )

    for seat in (PrimeProvider, ClaudeProvider, DoublewordProvider):
        params = inspect.signature(seat.generate).parameters
        assert "sampling" in params, f"{seat.__name__} cannot be handed a point"
        assert any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        ), f"{seat.__name__} would TypeError on an unknown sampling field"
