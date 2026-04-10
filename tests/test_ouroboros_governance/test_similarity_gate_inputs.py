"""Confirm similarity_gate.check_similarity accepts the shapes the
orchestrator now feeds it after the bt-2026-04-10-184157 postmortem.

The crash was ``AttributeError: 'dict' object has no attribute 'splitlines'``
because the orchestrator passed ``best_candidate`` (a dict) directly into
``check_similarity``. We now extract the content string upstream. These
tests lock in the gate's single-responsibility: it takes two strings.
"""

from __future__ import annotations

from backend.core.ouroboros.governance.similarity_gate import check_similarity


SOURCE = """\
aiohttp==3.13.2
anthropic==0.75.0
httpx==0.28.1
rapidfuzz>=3.0.0
requests==2.32.5
"""

# Candidate makes an additive change — should NOT be flagged.
NEW_ADDITIVE = """\
aiohttp==3.13.2
anthropic==0.75.0
httpx==0.28.1
newdep==1.0.0
rapidfuzz>=3.0.0
requests==2.32.5
"""

# Candidate that replicates the source with only additions at end.
MOSTLY_COPY_PASTE = """\
aiohttp==3.13.2
anthropic==0.75.0
httpx==0.28.1
rapidfuzz>=3.0.0
requests==2.32.5
added-one==1.0.0
added-two==2.0.0
added-three==3.0.0
added-four==4.0.0
"""


def test_check_similarity_with_strings_does_not_crash() -> None:
    # Additive minor change — below the 3-line threshold, so returns None.
    result = check_similarity(NEW_ADDITIVE, SOURCE)
    assert result is None


def test_check_similarity_rejects_dict_input_fails_loudly() -> None:
    """Confirms the gate ITSELF refuses dict input with a clear error.
    The orchestrator must extract full_content before calling. This test
    pins the invariant so any caller that regresses to dict-passing will
    crash hard in tests, not silently PASS in production."""
    import pytest
    with pytest.raises(AttributeError):
        check_similarity({"full_content": NEW_ADDITIVE}, SOURCE)  # type: ignore[arg-type]


def test_check_similarity_handles_large_additions() -> None:
    """With 4+ additions (past MIN_ADDED_LINES=3), the gate examines n-grams."""
    result = check_similarity(MOSTLY_COPY_PASTE, SOURCE)
    # Either None (additive lines have low source-ngram overlap) or a
    # reason string — both are valid. We just care it doesn't crash.
    assert result is None or isinstance(result, str)
