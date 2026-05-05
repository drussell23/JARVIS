"""Tests for live_status_line (Gap #1+5 Slice 1)."""
from __future__ import annotations

from unittest import mock

import pytest

from backend.core.ouroboros.battle_test.live_status_line import (
    LIVE_STATUS_LINE_SCHEMA_VERSION,
    LiveStatusLineRender,
    MASTER_FLAG_ENV_VAR,
    compose,
    is_master_flag_enabled,
    make_bottom_toolbar_callable,
    render_status_segment,
)


# ===========================================================================
# Schema + master flag
# ===========================================================================


def test_schema_version_pinned():
    assert LIVE_STATUS_LINE_SCHEMA_VERSION == "live_status_line.v1"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    yield


def test_master_flag_default_on_post_graduation():
    """Slice 5 graduation flipped this default-true (2026-05-04).
    Operators opt OUT via ``=false`` / ``=0`` / ``=off``."""
    assert is_master_flag_enabled() is True


@pytest.mark.parametrize("raw,expected", [
    # Empty / unset → default ON post-graduation
    ("", True),
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("garbage", True),  # garbage not in off-token set → ON
    # Off-tokens
    ("false", False), ("0", False), ("no", False), ("off", False),
])
def test_master_flag_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, raw)
    assert is_master_flag_enabled() is expected


# ===========================================================================
# render_status_segment — defensive degradation
# ===========================================================================


def test_status_segment_empty_when_master_flag_off():
    assert render_status_segment() == ""


def test_status_segment_empty_when_no_builder_registered(monkeypatch):
    """No registered builder → empty string (legacy fallback)."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    with mock.patch(
        "backend.core.ouroboros.battle_test.status_line.get_status_line_builder",
        return_value=None,
    ):
        assert render_status_segment() == ""


def test_status_segment_empty_when_should_render_false(monkeypatch):
    """TTY-gate / kill-switch off via should_render() → empty."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    with mock.patch(
        "backend.core.ouroboros.battle_test.status_line.should_render",
        return_value=False,
    ):
        assert render_status_segment() == ""


def test_status_segment_returns_builder_render_when_enabled(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    fake_builder = mock.Mock()
    fake_builder.render_plain.return_value = "GENERATE 14s · $0.04"
    with mock.patch(
        "backend.core.ouroboros.battle_test.status_line.should_render",
        return_value=True,
    ), mock.patch(
        "backend.core.ouroboros.battle_test.status_line.get_status_line_builder",
        return_value=fake_builder,
    ):
        assert render_status_segment() == "GENERATE 14s · $0.04"


def test_status_segment_handles_render_raising(monkeypatch):
    """Builder.render_plain() raising must NOT propagate."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    fake_builder = mock.Mock()
    fake_builder.render_plain.side_effect = RuntimeError("boom")
    with mock.patch(
        "backend.core.ouroboros.battle_test.status_line.should_render",
        return_value=True,
    ), mock.patch(
        "backend.core.ouroboros.battle_test.status_line.get_status_line_builder",
        return_value=fake_builder,
    ):
        assert render_status_segment() == ""


def test_status_segment_handles_non_string_return(monkeypatch):
    """Defensive: render_plain() returning non-string → empty."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    fake_builder = mock.Mock()
    fake_builder.render_plain.return_value = 42  # not a string
    with mock.patch(
        "backend.core.ouroboros.battle_test.status_line.should_render",
        return_value=True,
    ), mock.patch(
        "backend.core.ouroboros.battle_test.status_line.get_status_line_builder",
        return_value=fake_builder,
    ):
        assert render_status_segment() == ""


# ===========================================================================
# compose — merge swarm + status segments
# ===========================================================================


def test_compose_passes_through_swarm_when_status_empty():
    """Master flag off → status_segment is empty → combined is just swarm."""
    result = compose("  🐍 swarm:0")
    assert isinstance(result, LiveStatusLineRender)
    assert result.swarm_segment == "  🐍 swarm:0"
    assert result.status_segment == ""
    assert result.combined == "  🐍 swarm:0"


def test_compose_joins_with_newline_when_both_present(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    fake_builder = mock.Mock()
    fake_builder.render_plain.return_value = "GENERATE 14s"
    with mock.patch(
        "backend.core.ouroboros.battle_test.status_line.should_render",
        return_value=True,
    ), mock.patch(
        "backend.core.ouroboros.battle_test.status_line.get_status_line_builder",
        return_value=fake_builder,
    ):
        result = compose("  🐍 swarm:1")
    assert result.swarm_segment == "  🐍 swarm:1"
    assert result.status_segment == "GENERATE 14s"
    assert result.combined == "  🐍 swarm:1\nGENERATE 14s"


def test_compose_drops_empty_swarm():
    result = compose("")
    assert result.combined == ""


def test_compose_handles_non_string_swarm():
    result = compose(None)
    assert result.swarm_segment == ""
    assert result.combined == ""


def test_compose_returns_frozen_record():
    result = compose("x")
    assert result.schema_version == LIVE_STATUS_LINE_SCHEMA_VERSION
    with pytest.raises(Exception):
        result.combined = "tampered"  # type: ignore[misc]


# ===========================================================================
# make_bottom_toolbar_callable — wrapping contract
# ===========================================================================


def test_wrapper_passes_through_when_status_empty():
    """Master flag off → wrapper returns swarm_callable's output unchanged
    (byte-identical legacy behavior)."""
    inner = mock.Mock(return_value="  🐍 swarm:2")
    wrapped = make_bottom_toolbar_callable(inner)
    out = wrapped()
    assert out == "  🐍 swarm:2"
    inner.assert_called_once()


def test_wrapper_swallows_swarm_callable_exception():
    """Swarm callable raising must NOT propagate; the toolbar must
    keep rendering (empty fallback is acceptable)."""
    def _broken():
        raise RuntimeError("swarm raised")
    wrapped = make_bottom_toolbar_callable(_broken)
    # Should not raise; status segment is empty (master flag off).
    result = wrapped()
    # Result is the empty fallback (string or ANSI-wrapped empty).
    assert result is not None


def test_wrapper_combines_when_status_present(monkeypatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    fake_builder = mock.Mock()
    fake_builder.render_plain.return_value = "GENERATE 14s · $0.04"
    inner = mock.Mock(return_value="  🐍 swarm:1")
    wrapped = make_bottom_toolbar_callable(inner)

    with mock.patch(
        "backend.core.ouroboros.battle_test.status_line.should_render",
        return_value=True,
    ), mock.patch(
        "backend.core.ouroboros.battle_test.status_line.get_status_line_builder",
        return_value=fake_builder,
    ):
        out = wrapped()

    # The output is wrapped in ANSI(...) — extract the inner text.
    inner_text = getattr(out, "value", out)
    assert "swarm:1" in str(inner_text)
    assert "GENERATE" in str(inner_text)


def test_wrapper_handles_ansi_object_input(monkeypatch):
    """Inner callable returns prompt_toolkit ANSI(...) — wrapper must
    extract its .value before merging."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    try:
        from prompt_toolkit.formatted_text import ANSI
    except ImportError:
        pytest.skip("prompt_toolkit not available")

    fake_builder = mock.Mock()
    fake_builder.render_plain.return_value = "STATUS"
    inner = mock.Mock(return_value=ANSI("  🐍 swarm:0"))
    wrapped = make_bottom_toolbar_callable(inner)

    with mock.patch(
        "backend.core.ouroboros.battle_test.status_line.should_render",
        return_value=True,
    ), mock.patch(
        "backend.core.ouroboros.battle_test.status_line.get_status_line_builder",
        return_value=fake_builder,
    ):
        out = wrapped()

    # Extract inner text from ANSI wrapper
    inner_text = getattr(out, "value", str(out))
    assert "swarm:0" in inner_text
    assert "STATUS" in inner_text
    assert "\n" in inner_text  # both stacked


# ===========================================================================
# Authority invariant — module is read-only
# ===========================================================================


def test_module_does_not_mutate_status_line_module():
    """Slice 1 is consumer-only — must not actually CALL any
    setter / registration function. AST walk to detect real Call
    nodes (not docstring mentions or comments).
    """
    import ast
    import backend.core.ouroboros.battle_test.live_status_line as mod
    tree = ast.parse(open(mod.__file__).read())
    forbidden = {"register_status_line_builder"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in forbidden:
                pytest.fail(
                    f"live_status_line.py invokes {func.id} — "
                    "that's the harness's job, not the consumer's"
                )
            if isinstance(func, ast.Attribute) and func.attr in forbidden:
                pytest.fail(
                    f"live_status_line.py invokes {func.attr} — "
                    "consumer-only contract violated"
                )


def test_module_no_top_level_prompt_toolkit_import():
    """prompt_toolkit is imported lazily inside the wrapper — keeps
    the substrate import-cheap and headless-safe."""
    import backend.core.ouroboros.battle_test.live_status_line as mod
    src = open(mod.__file__).read()
    # Top-level imports only (everything before first def/class)
    top_lines = []
    for line in src.splitlines():
        if line.startswith(("def ", "class ")):
            break
        top_lines.append(line)
    top = "\n".join(top_lines)
    assert "from prompt_toolkit" not in top
    assert "import prompt_toolkit" not in top
