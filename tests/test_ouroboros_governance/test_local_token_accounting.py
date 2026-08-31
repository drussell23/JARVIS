"""The local lane must report the engine's token counts, not a guess.

Two independent defects made ``tok/s`` unrecoverable on the local lane, and
between them they wrote ``completion_tokens=0, tokens_per_second=0.0`` into
every trajectory the corpus holds:

1. The recorder hook read ``response.output_tokens``. ``PrimeResponse`` has
   never had that attribute -- its completion count is ``tokens_used`` --
   so the read resolved to the ``-1`` sentinel on EVERY local generation and
   the recorder fell through to the GenerationResult's zero. The engine was
   reporting counts the whole time; one wrong attribute name discarded them.

2. The streaming path never ASKED for usage. An OpenAI-compat stream carries
   no accounting unless the client requests it, so the path fell back to
   ``len(text) // 4`` -- and, worse, reported that guess through the same
   field a measurement would use, so nothing downstream could tell them
   apart. The error is not even uniform across models (measured on this
   host: ~31% low for qwen3-coder:30b, ~10% low for qwen2.5-coder:32b),
   which biases precisely the model A/B the number exists to decide.

The fix is to ask the engine and to label the answer, NOT to reimplement
the model's tokenizer in-process: a second tokenizer is a second source of
truth that can drift from the one that actually produced the tokens.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.local_inference_director import (
    _SSE_DONE,
    _SSEUsage,
    _parse_sse_delta,
)


# --------------------------------------------------------------------------
# The accounting frame
# --------------------------------------------------------------------------

# Verbatim from ollama 0.33.1's /v1/chat/completions stream terminator.
_USAGE_FRAME = (
    b'data: {"id":"chatcmpl-276","object":"chat.completion.chunk",'
    b'"created":1788188021,"model":"qwen2.5-coder:7b","choices":[],'
    b'"usage":{"prompt_tokens":35,"completion_tokens":11,"total_tokens":46}}'
)


def test_usage_frame_is_surfaced_not_discarded() -> None:
    """``choices: []`` plus a usage object is accounting, not a keep-alive.

    The old parser returned None for anything with no choices, which is
    exactly the shape of the frame carrying the counts -- the reason the
    streaming path had nothing to report.
    """
    got = _parse_sse_delta(_USAGE_FRAME)
    assert isinstance(got, _SSEUsage)
    assert got.prompt_tokens == 35
    assert got.completion_tokens == 11


def test_usage_is_not_content() -> None:
    """The accounting frame must never be mistaken for output.

    A distinct type is what makes that structural rather than a convention:
    appending it to the response buffer would corrupt the generated file.
    """
    assert not isinstance(_parse_sse_delta(_USAGE_FRAME), str)


@pytest.mark.parametrize(
    "line",
    [
        b'data: {"choices":[]}',                       # keep-alive, no usage
        b'data: {"choices":[],"usage":{}}',            # usage present but empty
        b'data: {"choices":[],"usage":{"completion_tokens":0}}',  # zero == absent
        b"",                                            # blank
        b"event: ping",                                 # non-data line
        b"data: {not json",                             # malformed
    ],
)
def test_non_accounting_lines_stay_none(line: bytes) -> None:
    """Only a frame with a POSITIVE completion count is a measurement.

    Zero is what the engine sends when it is not really reporting, and
    accepting it would relabel a missing measurement as a measured zero --
    the exact laundering this slice exists to stop.
    """
    assert _parse_sse_delta(line) is None


def test_content_and_done_are_unchanged() -> None:
    """The pre-existing contract is untouched (byte-identical behaviour)."""
    assert _parse_sse_delta(b"data: [DONE]") is _SSE_DONE
    assert (
        _parse_sse_delta(b'data: {"choices":[{"delta":{"content":"hi"}}]}')
        == "hi"
    )
    # An empty content delta is falsy-but-valid; the loop treats it as no-op.
    assert _parse_sse_delta(b'data: {"choices":[{"delta":{"content":""}}]}') is None


# --------------------------------------------------------------------------
# Provenance survives to the corpus
# --------------------------------------------------------------------------


def test_local_completion_defaults_to_estimated() -> None:
    """A producer that says nothing has not earned the "measured" label.

    Defaulting the other way would let any future construction site
    silently promote a guess into the corpus as a measurement.
    """
    from backend.core.ouroboros.governance.local_inference_director import (
        LocalCompletion,
    )

    lc = LocalCompletion(text="x", output_tokens=1, ttft_ms=0.0, total_ms=1.0)
    assert lc.tokens_estimated is True
    assert lc.prompt_tokens == 0


def test_recorder_writes_token_provenance() -> None:
    """``tokens_estimated`` reaches the written event.

    Throughput analysis must be able to EXCLUDE estimated rows before
    ranking models, which it can only do if the flag is in the corpus.
    """
    from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501
        _PendingGeneration,
    )

    gen = _PendingGeneration(
        op_id="op", prompt="p", prompt_key="k", candidates=(),
        model_id="qwen3-coder:30b", provider_name="local", is_noop=False,
        latency_ms=1000.0, prompt_tokens=30, completion_tokens=98,
        cost_usd=0.0, task_type="code_repair", session_id="s",
    )
    # Default is the conservative one, matching LocalCompletion.
    assert gen.tokens_estimated is True

    measured = _PendingGeneration(
        op_id="op", prompt="p", prompt_key="k", candidates=(),
        model_id="qwen3-coder:30b", provider_name="local", is_noop=False,
        latency_ms=1000.0, prompt_tokens=30, completion_tokens=98,
        cost_usd=0.0, task_type="code_repair", session_id="s",
        tokens_estimated=False,
    )
    assert measured.tokens_estimated is False
    # The pairing that makes tok/s recoverable: split counts, not a total.
    assert measured.completion_tokens == 98
    assert measured.prompt_tokens == 30


def test_prime_response_has_no_output_tokens_attribute() -> None:
    """Pin the defect so it cannot silently return.

    ``getattr(response, "output_tokens", -1)`` compiled, ran, and returned
    the sentinel forever. A test that asserts the attribute's ABSENCE is
    what makes the next author reach for ``tokens_used`` instead.
    """
    from backend.core.prime_client import PrimeResponse

    resp = PrimeResponse(content="c", request_id="r")
    assert not hasattr(resp, "output_tokens")
    assert hasattr(resp, "tokens_used")
