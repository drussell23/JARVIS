"""The budget buys SIGNAL, not the first N lines.

Head+tail elision is content-blind, so two thirds of a bash body was the
first two thirds of a pytest run — `===== test session starts =====`,
`collected 47 items`, a line of progress dots, a bar of `=`. The operator's
screenshot showed thirty-five rows in which the answer (`3 failed, 44
passed`) was one of them.

Shrinking the budget makes that WORSE, not better: the preamble is at the
front and the budget is spent front-first, so a smaller window keeps
proportionally more scaffolding. The fix is to spend rows on lines that carry
information.

Nothing is lost — `BoundedBodyStore` parks the full body and the elided count
includes what was denoised, so `/expand t-N` returns every byte. These tests
pin that: what is DROPPED, what is KEPT, and that the count stays honest.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test.tool_render_registry import (
    BodyShape, ToolStatus, _extract_body, _is_diff_noise, _is_log_noise,
)

_PYTEST = "\n".join([
    "============================= test session starts ======================",
    "collected 47 items",
    "",
    "tests/governance/test_risk_tier_floor.py ..F..F......F................",
    "",
    "================================== FAILURES ============================",
    "_____________________________ test_scoped_paths ________________________",
    "        def test_scoped_paths():",
    ">       assert floor is NOTIFY_APPLY",
    "E       AssertionError: assert SAFE_AUTO is NOTIFY_APPLY",
    "tests/governance/test_risk_tier_floor.py:88: AssertionError",
    "=========================== short test summary info ====================",
    "FAILED tests/governance/test_risk_tier_floor.py::test_scoped_paths",
    "3 failed, 44 passed in 4.12s",
])


class TestWhatALogSpendsOnItself:
    @pytest.mark.parametrize("line", [
        "",
        "   ",
        "============================= test session starts ==================",
        "_____________________________ test_scoped_paths ____________________",
        "=======================================================",
        "tests/governance/test_risk_tier_floor.py ..F..F......F..........",
    ])
    def test_scaffolding_is_noise(self, line):
        assert _is_log_noise(line)

    @pytest.mark.parametrize("line", [
        ">       assert floor is NOTIFY_APPLY",
        "E       AssertionError: assert SAFE_AUTO is NOTIFY_APPLY",
        "3 failed, 44 passed in 4.12s",
        "FAILED tests/governance/test_risk_tier_floor.py::test_scoped_paths",
        "collected 47 items",
        "        def test_scoped_paths():",
    ])
    def test_content_is_not(self, line):
        assert not _is_log_noise(line)

    def test_a_ruled_line_is_matched_by_DENSITY_not_vocabulary(self):
        """Matching bare bars missed `===== test session starts =====`, which
        is what a log actually spends its rows on. A line that is
        three-quarters punctuation is a divider whatever it says."""
        assert _is_log_noise("##### BUILD FAILED " + "#" * 40) is False
        assert _is_log_noise("===== anything at all " + "=" * 50)

    def test_the_verdict_survives(self):
        body, _ = _extract_body(_PYTEST, 8, BodyShape.LOG)
        assert any("3 failed, 44 passed" in ln for ln in body)

    def test_the_assertion_survives(self):
        body, _ = _extract_body(_PYTEST, 8, BodyShape.LOG)
        text = "\n".join(body)
        assert "AssertionError" in text

    def test_a_log_is_tail_weighted(self):
        """A run's answer is its last line. A uniform split spends most of a
        small budget on the setup that produced it."""
        numbered = "\n".join(f"line {i}" for i in range(100))
        body, _ = _extract_body(numbered, 10, BodyShape.LOG)
        tail = [ln for ln in body if "line 9" in ln]
        assert len(tail) >= 4, "the end of the log was not preferred"


class TestWhatADiffSpendsOnItself:
    def test_the_unified_preamble_is_dropped(self):
        """`⏺ Update(risk_tier_floor.py)` already names the file. `--- a/…`
        and `+++ b/…` restate it twice, at two rows a block."""
        assert _is_diff_noise("--- a/backend/x.py")
        assert _is_diff_noise("+++ b/backend/x.py")

    def test_the_hunk_header_is_KEPT(self):
        """It says WHERE, which the header does not."""
        assert not _is_diff_noise("@@ -410,7 +410,22 @@")

    def test_a_removed_line_is_not_mistaken_for_the_preamble(self):
        """`-    except Exception:` starts with a dash and is the whole point
        of the diff."""
        assert not _is_diff_noise("-    except Exception:  # noqa: BLE001")
        assert not _is_diff_noise("+    except RiskFloorConfigError:")


class TestTheCountStaysHonest:
    def test_denoised_lines_are_counted_as_unseen(self):
        """They are not on screen. A count that ignored them would tell the
        operator they had seen everything."""
        body, elided = _extract_body(_PYTEST, 60, BodyShape.LOG)
        assert len(body) < len(_PYTEST.splitlines())
        assert elided == len(_PYTEST.splitlines()) - len(body)

    def test_nothing_is_actually_lost(self):
        """The store parks the FULL body — `/expand` is the escape hatch, so
        this decides what is worth a ROW, not what is worth keeping.

        Asserted by recovering a line the deck deliberately dropped: if
        denoising ever reached the store, the operator's recovery path would
        return the same edited view they were trying to escape.
        """
        from backend.core.ouroboros.battle_test.tool_render_store import (
            BoundedBodyStore,
        )
        from backend.core.ouroboros.battle_test.tool_render_view import compose
        store = BoundedBodyStore()
        out = compose("bash", "cmd", _PYTEST, status=ToolStatus.ERROR,
                      store=store)
        assert "test session starts" not in "\n".join(out.body_lines_markup)
        refs = store.all_refs()
        assert refs, "nothing was parked, so nothing could be expanded"
        parked = store.lookup(refs[-1])
        assert parked is not None
        assert "test session starts" in str(getattr(parked, "body", parked))

    def test_an_uncut_body_reports_nothing_elided(self):
        body, elided = _extract_body("a\nb\nc", 10, BodyShape.MULTI_LINE)
        assert body == ("a", "b", "c") and elided == 0


class TestItNeverEmptiesTheBody:
    def test_an_all_noise_body_keeps_its_lines(self):
        """A filter that empties the body has told us the predicate is wrong
        for this content, not that the content was worthless."""
        bars = "\n".join("=" * 40 for _ in range(5))
        body, _ = _extract_body(bars, 10, BodyShape.LOG)
        assert body

    def test_uniform_shapes_are_left_alone(self):
        """Every grep hit is equally the answer; there is no scaffolding to
        drop and dropping any would be arbitrary."""
        hits = "\n".join(f"a.py:{i}: hit" for i in range(5))
        body, elided = _extract_body(hits, 10, BodyShape.MULTI_LINE)
        assert len(body) == 5 and elided == 0

    @pytest.mark.parametrize("shape", list(BodyShape))
    def test_every_shape_survives_junk(self, shape):
        for junk in ("", "\n\n\n", "x" * 5000):
            body, elided = _extract_body(junk, 5, shape)
            assert isinstance(body, tuple) and elided >= 0
