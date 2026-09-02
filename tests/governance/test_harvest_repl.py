"""`/harvest` — the cockpit surface for "is the flywheel collecting anything?"

The gap it closes: O+V produced corpus state and could not show it. Soak
`bt-2026-09-01-213353` ran 36 minutes, wrote zero rows, and was
indistinguishable from a healthy harvest at the cockpit — the only report
was one log line at teardown.

The property worth pinning hardest is not that it renders. It is that its
headline number uses the SAME predicate as the filter that decides what
gets kept. An earlier draft counted a group as pairable whenever its rows
had distinct structure ids; measured on the live corpus that reported 8
pairable groups where the filter's own threshold says 2, because answers
differing by one unused import carry distinct ids at 0.9987 similarity.
A surface that disagrees with the mechanism it reports on is worse than no
surface, and the disagreement flattered the harvest.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.harvest_repl import (
    dispatch_harvest_command,
)
from backend.core.ouroboros.governance.observability.trajectory_recorder import (  # noqa: E501
    harvest_snapshot,
)

_ENV_DIR = "JARVIS_TRAJECTORY_RECORDER_DIR"

# Two answers that differ ONLY by an unused import — the exact shape the
# corpus was full of. Distinct ASTs, therefore distinct structure ids, and
# 0.99+ similar: two rows, one answer.
#
# The BODY has to be realistic in SIZE, and that is the point of the
# constant rather than a two-line snippet. A first draft used
# `def run(x): return x + 1`, where deleting an import removes a large
# FRACTION of a tiny tree: it measured 0.8833 — below threshold — so the
# fixture proved the opposite of what it claimed and the test failed
# against correct code. The real corpus rows were ~5 KB modules where one
# import is negligible; at that scale the pair measures 0.9940.
_BODY = "".join(
    f"def handler_{i}(payload):\n"
    f"    total = 0\n"
    f"    for item in payload:\n"
    f"        if item.get('active'):\n"
    f"            total += item['size']\n"
    f"    return {{'n': total}}\n\n"
    for i in range(8)
)
_NEAR_A = "from typing import Optional\n\n" + _BODY
_NEAR_B = _BODY

# A genuinely different implementation of the same job.
_DIFFERENT = (
    "def run(x):\n"
    "    total = 0\n"
    "    for i in range(x):\n"
    "        total += i\n"
    "    return total\n"
)


def _row(op: str, attempt: int, body: str, sid: str = "") -> dict:
    meta = {
        "op_id": op, "attempt_index": attempt, "n_candidates": 1,
        "should_train": True,
    }
    if sid:
        meta["structure_id"] = sid
    return {
        "event_type": "interaction",
        "user_input": f"prompt for {op}",
        "assistant_output": body,
        "outcome": "success",
        "metadata": meta,
    }


def _corpus(tmp_path: Path, rows: list) -> Path:
    d = tmp_path / "events"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "a.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    return d


# --------------------------------------------------------------------------
# The predicate that must not drift
# --------------------------------------------------------------------------


def test_near_duplicates_are_not_counted_pairable(tmp_path: Path) -> None:
    """Distinct structure ids are necessary, not sufficient.

    These two rows have different ASTs (one carries an unused import) so a
    naive id-count calls the group pairable. The acceptance filter would
    reject the second draw as redundant, and this surface must agree with
    it or it reports training signal that does not exist.
    """
    d = _corpus(tmp_path, [
        _row("op-1", 0, _NEAR_A, sid="aaaaaaaaaaaa"),
        _row("op-1", 1, _NEAR_B, sid="bbbbbbbbbbbb"),
    ])
    snap = harvest_snapshot(events_path=d)
    assert snap["groups"] == 1
    assert snap["groups_pairable"] == 0
    assert snap["groups_collapsed"] == 1


def test_a_genuinely_different_answer_is_pairable(tmp_path: Path) -> None:
    d = _corpus(tmp_path, [
        _row("op-2", 0, _NEAR_A),
        _row("op-2", 1, _DIFFERENT),
    ])
    snap = harvest_snapshot(events_path=d)
    assert snap["groups"] == 1
    assert snap["groups_pairable"] == 1


def test_identical_rows_collapse(tmp_path: Path) -> None:
    """Three rows, one answer — the measured corpus failure."""
    d = _corpus(tmp_path, [
        _row("op-3", i, _NEAR_B) for i in range(3)
    ])
    snap = harvest_snapshot(events_path=d)
    assert snap["groups"] == 1 and snap["groups_pairable"] == 0


def test_a_group_needs_two_rows(tmp_path: Path) -> None:
    """One row is not a group; counting it would inflate the denominator."""
    d = _corpus(tmp_path, [_row("op-4", 0, _NEAR_A)])
    snap = harvest_snapshot(events_path=d)
    assert snap["rows"] == 1 and snap["groups"] == 0


def test_rows_and_trainable_are_counted_separately(tmp_path: Path) -> None:
    rows = [_row("op-5", 0, _NEAR_A), _row("op-5", 1, _DIFFERENT)]
    rows[1]["metadata"]["should_train"] = False
    d = _corpus(tmp_path, rows)
    snap = harvest_snapshot(events_path=d)
    assert snap["rows"] == 2 and snap["rows_trainable"] == 1


# --------------------------------------------------------------------------
# Never raises
# --------------------------------------------------------------------------


def test_corrupt_lines_do_not_break_the_read(tmp_path: Path) -> None:
    d = tmp_path / "events"
    d.mkdir(parents=True)
    (d / "a.jsonl").write_text(
        "not json\n"
        + json.dumps(_row("op-6", 0, _NEAR_A)) + "\n"
        + "{unterminated\n"
        + json.dumps(_row("op-6", 1, _DIFFERENT)) + "\n",
        encoding="utf-8",
    )
    snap = harvest_snapshot(events_path=d)
    assert snap["rows"] == 2 and snap["groups"] == 1
    assert snap["error"] == ""


def test_unparseable_output_never_forges_an_answer(tmp_path: Path) -> None:
    """A row that will not parse has no fingerprint.

    It must not be folded together with another unparseable row into a
    shared "answer", nor counted as a distinct one.
    """
    d = _corpus(tmp_path, [
        _row("op-7", 0, "def broken(:\n"),
        _row("op-7", 1, "def also_broken(:\n"),
    ])
    snap = harvest_snapshot(events_path=d)
    assert snap["groups"] == 1 and snap["groups_pairable"] == 0


def test_missing_directory_is_empty_not_an_error(tmp_path: Path) -> None:
    snap = harvest_snapshot(events_path=tmp_path / "nope")
    assert snap["rows"] == 0 and snap["groups"] == 0 and snap["error"] == ""


def test_row_cap_is_reported_not_silent(tmp_path: Path) -> None:
    d = _corpus(tmp_path, [_row(f"op-{i}", 0, _NEAR_A) for i in range(10)])
    snap = harvest_snapshot(events_path=d, max_rows=4)
    assert snap["rows"] == 4 and snap["truncated"] is True


# --------------------------------------------------------------------------
# The verb
# --------------------------------------------------------------------------


@pytest.mark.parametrize("line", ["/cost", "/harvestx", "", "not a verb"])
def test_unrelated_lines_are_not_matched(line: str) -> None:
    assert dispatch_harvest_command(line).matched is False


@pytest.mark.parametrize("line", ["/harvest", "harvest", "/harvest status",
                                  "/harvest groups", "/harvest help"])
def test_every_documented_form_matches(line: str) -> None:
    r = dispatch_harvest_command(line)
    assert r.matched is True and r.text


def test_unknown_subcommand_shows_help_and_is_not_ok() -> None:
    r = dispatch_harvest_command("/harvest wat")
    assert r.matched is True and r.ok is False and "Subcommands" in r.text


def test_status_names_the_collapse_when_nothing_can_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rows climbing with zero pairable groups is THE failure mode.

    It must be stated, not left for the operator to infer from two numbers.
    """
    d = _corpus(tmp_path, [_row("op-8", i, _NEAR_B) for i in range(3)])
    monkeypatch.setenv(_ENV_DIR, str(d))
    r = dispatch_harvest_command("/harvest")
    assert r.ok is True
    assert "PAIRABLE=0" in r.text
    assert "no preference pair is constructible" in r.text


def test_status_says_when_the_recorder_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"0 rows" is ambiguous; "recorder OFF" is not."""
    monkeypatch.setenv(_ENV_DIR, str(tmp_path / "events"))
    monkeypatch.setenv("JARVIS_TRAJECTORY_RECORDER_ENABLED", "false")
    r = dispatch_harvest_command("/harvest status")
    assert "recorder      OFF" in r.text
    assert "JARVIS_TRAJECTORY_RECORDER_ENABLED" in r.text


def test_groups_view_reports_the_min_similarity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The verdict follows the MOST DISTINCT pair, so that is what shows.

    Printing the maximum would read 1.0000 next to "PAIRABLE" for any group
    holding one duplicate — a line that contradicts itself.
    """
    d = _corpus(tmp_path, [
        _row("op-9", 0, _NEAR_A),
        _row("op-9", 1, _NEAR_A),
        _row("op-9", 2, _DIFFERENT),
    ])
    monkeypatch.setenv(_ENV_DIR, str(d))
    r = dispatch_harvest_command("/harvest groups")
    assert r.ok is True and "min_sim" in r.text and "PAIRABLE" in r.text


def test_empty_corpus_explains_what_it_means(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ENV_DIR, str(tmp_path / "events"))
    r = dispatch_harvest_command("/harvest groups")
    assert "no group has 2+ rows yet" in r.text


def test_groups_limit_is_clamped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = _corpus(tmp_path, [_row("op-a", i, _NEAR_A) for i in range(2)])
    monkeypatch.setenv(_ENV_DIR, str(d))
    for arg in ("0", "-3", "banana", "99999"):
        assert dispatch_harvest_command(f"/harvest groups {arg}").ok is True


# --------------------------------------------------------------------------
# Both surfaces, one definition
# --------------------------------------------------------------------------


def test_the_verb_is_auto_discovered_for_both_cockpits() -> None:
    """The daemon cockpit and the attach cockpit must not diverge.

    `cockpit_mount` exists because hand-wired surfaces drift; this verb
    opts into `repl_dispatch_registry` by FILENAME, so neither surface has
    a list to forget to edit.
    """
    from backend.core.ouroboros.battle_test import repl_dispatch_registry as R

    R.reset_registry_for_tests()
    # `reset` clears the cache and `list_verbs` READS it without rebuilding,
    # so priming is the caller's job — without it this asserts against an
    # empty tuple and looks like the verb was never discovered.
    R.prime_registry()
    assert "harvest" in R.list_verbs()
    assert "harvest" in R.list_dispatchable_verbs()
