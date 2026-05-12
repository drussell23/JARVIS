"""Pytest assertions for the string-processing pipeline."""
from __future__ import annotations

from before import clean, collect, process, shorten


# ---- clean ----


def test_clean_drops_tag():
    assert clean("<p>hi</p>") == "hi"


def test_clean_drops_nested_tags():
    assert clean("<div><b>hi</b> there</div>") == "hi there"


def test_clean_no_tags_unchanged():
    assert clean("plain text") == "plain text"


def test_clean_input_none():
    assert clean(None) == ""


# ---- shorten ----


def test_shorten_short_value_unchanged():
    assert shorten("hi", 10) == "hi"


def test_shorten_exact_length_unchanged():
    assert shorten("hello", 5) == "hello"


def test_shorten_long_value_appends_ellipsis():
    assert shorten("hello world", 5) == "hello..."


def test_shorten_input_none():
    assert shorten(None, 10) == ""


# ---- process ----


def test_process_strips_and_shortens():
    assert process(
        "<p>" + "a" * 200 + "</p>", limit=10,
    ) == "aaaaaaaaaa..."


def test_process_short_html_unchanged_length():
    assert process("<b>hi</b>") == "hi"


def test_process_input_none():
    assert process(None) == ""


# ---- collect ----


def test_collect_normal_list():
    assert collect([
        "<p>first</p>",
        "<b>second</b>",
        "<i>third</i>",
    ]) == ["first", "second", "third"]


def test_collect_empty_list():
    assert collect([]) == []


def test_collect_input_a():
    """Behavior reference: see the input vs the expected output."""
    assert collect([
        "<p>alpha</p>",
        None,
        "<b>beta</b>",
        None,
        "<i>gamma</i>",
    ]) == ["alpha", "beta", "gamma"]


def test_collect_input_b():
    assert collect([None, "<p>x</p>"]) == ["x"]


def test_collect_input_c():
    assert collect([None, None, None]) == []


def test_collect_preserves_order():
    assert collect([
        None,
        "<p>charlie</p>",
        None,
        "<p>alpha</p>",
        "<p>bravo</p>",
        None,
    ]) == ["charlie", "alpha", "bravo"]
