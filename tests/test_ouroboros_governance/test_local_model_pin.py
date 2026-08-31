"""The local lane must dispatch to the model the operator chose.

``_parse_served_model`` picked the LARGEST model on disk, encoding the
assumption "one big model plus small sidecars". That held when this host
served a 32B and a 7B. It stopped holding the moment several LARGE models
were present -- a 32B (19.85GB), a 30B MoE (18.56GB) and a 27B (18GB) --
because "largest" then selects by an arbitrary property that has nothing
to do with which model was asked for. ``JARVIS_LOCAL_MODEL_NAME`` was
silently inert, so a three-model A/B would have run one model three times.

The name and the on-disk bytes must also come from ONE choice: the bytes
feed the num_ctx negotiator, and sizing a context window from a different
model than the one generating is the same class of confidently-wrong
number as the nvidia-l4 default.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.candidate_generator import (
    _parse_served_model,
    _parse_served_model_bytes,
    _select_served_entry,
)

_ENV_PIN = "JARVIS_LOCAL_MODEL_NAME"

# The real payload shape from this host's ollama /api/tags.
_QWEN25_32B = {"name": "qwen2.5-coder:32b", "size": 19_851_349_856}
_QWEN3_30B = {"name": "qwen3-coder:30b", "size": 18_556_701_184}
_QWEN38_27B = {"name": "qwen3.8:27b", "size": 18_000_000_000}
_QWEN25_7B = {"name": "qwen2.5-coder:7b", "size": 4_683_087_519}


def _tags(*entries: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    return {"models": list(entries)}


@pytest.fixture(autouse=True)
def _no_ambient_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_ENV_PIN, raising=False)


# ---------------------------------------------------------------------------
# Legacy behaviour is preserved when nothing is pinned
# ---------------------------------------------------------------------------


def test_unpinned_still_picks_largest() -> None:
    tags = _tags(_QWEN25_7B, _QWEN25_32B, _QWEN3_30B)
    assert _parse_served_model(tags) == "qwen2.5-coder:32b"


def test_unpinned_ignores_small_sidecar() -> None:
    assert _parse_served_model(_tags(_QWEN25_7B, _QWEN25_32B)) == (
        "qwen2.5-coder:32b"
    )


@pytest.mark.parametrize("bad", [None, {}, {"models": []}, {"models": None}])
def test_malformed_input_is_none(bad: Any) -> None:
    assert _parse_served_model(bad) is None
    assert _parse_served_model_bytes(bad) == 0


# ---------------------------------------------------------------------------
# The pin
# ---------------------------------------------------------------------------


def test_pin_wins_over_largest(monkeypatch: pytest.MonkeyPatch) -> None:
    """The regression that would have invalidated the A/B."""
    monkeypatch.setenv(_ENV_PIN, "qwen3-coder:30b")
    tags = _tags(_QWEN25_7B, _QWEN25_32B, _QWEN3_30B, _QWEN38_27B)
    assert _parse_served_model(tags) == "qwen3-coder:30b"


def test_pin_matches_by_base_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    """`qwen3.8` should find `qwen3.8:27b`."""
    monkeypatch.setenv(_ENV_PIN, "qwen3.8")
    tags = _tags(_QWEN25_32B, _QWEN38_27B)
    assert _parse_served_model(tags) == "qwen3.8:27b"


def test_exact_match_beats_base_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_PIN, "qwen3.8:27b")
    other = {"name": "qwen3.8:14b", "size": 99_000_000_000}
    assert _parse_served_model(_tags(other, _QWEN38_27B)) == "qwen3.8:27b"


def test_unserved_pin_falls_back_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    """Naming a model the node lacks is how a dispatch dies on
    KeyError('choices') -- so an unserved pin must NOT be sent."""
    monkeypatch.setenv(_ENV_PIN, "llama4:400b")
    with caplog.at_level("WARNING"):
        assert _parse_served_model(_tags(_QWEN25_32B, _QWEN3_30B)) == (
            "qwen2.5-coder:32b"
        )
    assert any("not served" in r.getMessage() for r in caplog.records)


def test_empty_pin_is_treated_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_PIN, "   ")
    assert _parse_served_model(_tags(_QWEN25_32B, _QWEN3_30B)) == (
        "qwen2.5-coder:32b"
    )


def test_pin_can_select_the_smallest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_ENV_PIN, "qwen2.5-coder:7b")
    tags = _tags(_QWEN25_7B, _QWEN25_32B, _QWEN3_30B)
    assert _parse_served_model(tags) == "qwen2.5-coder:7b"


# ---------------------------------------------------------------------------
# Name and bytes must describe the SAME model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pin", "expect_name", "expect_bytes"),
    [
        ("", "qwen2.5-coder:32b", 19_851_349_856),
        ("qwen3-coder:30b", "qwen3-coder:30b", 18_556_701_184),
        ("qwen3.8:27b", "qwen3.8:27b", 18_000_000_000),
        ("qwen2.5-coder:7b", "qwen2.5-coder:7b", 4_683_087_519),
    ],
)
def test_name_and_bytes_never_diverge(
    monkeypatch: pytest.MonkeyPatch, pin: str, expect_name: str,
    expect_bytes: int,
) -> None:
    """The bytes feed the num_ctx negotiator; if they described a
    different model than the one generating, the context window would be
    sized from the wrong weights."""
    if pin:
        monkeypatch.setenv(_ENV_PIN, pin)
    tags = _tags(_QWEN25_7B, _QWEN25_32B, _QWEN3_30B, _QWEN38_27B)
    assert _parse_served_model(tags) == expect_name
    assert _parse_served_model_bytes(tags) == expect_bytes


def test_selection_is_injectable_without_env() -> None:
    """`pin=` keeps the chooser pure for callers that already know."""
    tags = _tags(_QWEN25_32B, _QWEN3_30B)
    entry = _select_served_entry(tags, pin="qwen3-coder:30b")
    assert entry is not None and entry["name"] == "qwen3-coder:30b"


def test_entries_missing_name_fall_back_to_model_key() -> None:
    tags = _tags({"model": "qwen3.8:27b", "size": 18_000_000_000})
    assert _parse_served_model(tags) == "qwen3.8:27b"
