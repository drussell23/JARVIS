"""A ceiling without its basis is an arbitrary constant.

`$0.00/$0.71` prompted "where is the $0.71 coming from?" — a fair question,
because 0.71 is DERIVED (p95 of the operator's own recorded sessions x a
headroom multiple) and nothing on screen said so. `derived_cost_cap()` already
returns `(usd, basis)` and its docstring is explicit:

    "The basis is not decoration — it is the difference between 'this ceiling
     reflects 143 sessions of real spend' and 'nobody has ever measured this',
     and a surface that shows the number without it repeats the exact defect
     this replaces."

The basis was computed, returned, and dropped: the spawner sent it to
`logger.debug` and the hydration payload had no field for it.
"""
from __future__ import annotations

import inspect

from rich.console import Console

from backend.core.ouroboros.battle_test import status_line as sl
from backend.core.ouroboros.cli import ov


class TestTheNumberCarriesItsBasis:
    def _render(self, payload) -> str:
        buf = Console(force_terminal=False, width=120, record=True)
        ov._render_hydration(buf, payload)
        return buf.export_text()

    def _payload(self, **status):
        base = {"phase": "IDLE", "cost_spent_usd": 0.0,
                "cost_budget_usd": 0.71,
                "cost_budget_basis": "observed — 98 sessions, p95=$0.24 x3"}
        base.update(status)
        return {"status": base, "liquidity": {}, "ops": []}

    def test_the_basis_is_rendered_beside_the_number(self):
        out = self._render(self._payload())
        assert "0.71" in out
        assert "98 sessions" in out and "p95" in out

    def test_no_basis_renders_no_line_rather_than_an_empty_one(self):
        """Restraint: an unexplained number is better than a blank
        explanation pretending to be one."""
        out = self._render(self._payload(cost_budget_basis=""))
        assert "budget $" not in out

    def test_a_zero_budget_does_not_advertise_a_basis(self):
        out = self._render(self._payload(cost_budget_usd=0.0))
        assert "budget $0.00 (" not in out

    def test_every_basis_shape_renders_readably(self):
        """The string varies: derived, clamped, unmeasured, operator-set. The
        surface must not parse it — only present it."""
        for basis in ("observed — 98 sessions, p95=$0.24 x3",
                      "clamped to the Aegis session cap $2.00",
                      "unmeasured — no prior session recorded a cost",
                      "operator"):
            out = self._render(self._payload(cost_budget_basis=basis))
            assert basis.split(" ")[0] in out

    def test_the_render_survives_a_hostile_payload(self):
        for bad in (None, {}, {"status": None}, {"status": {"cost_budget_basis": 12}}):
            ov._render_hydration(Console(force_terminal=False), bad or {})


class TestTheBasisTravelsWithTheNumber:
    def test_the_snapshot_carries_a_basis_field(self):
        assert "cost_budget_basis" in sl.StatusSnapshot.__dataclass_fields__

    def test_the_basis_is_read_from_the_spawn_decision_not_re_derived(self):
        """It belongs to the DECISION that set the ceiling. Re-deriving later
        would sample a different set of sessions and could explain the number
        with evidence that did not produce it."""
        src = inspect.getsource(sl.StatusLineBuilder._sample_cost_basis)
        assert "OUROBOROS_BATTLE_COST_CAP_BASIS" in src
        assert "derived_cost_cap" not in src

    def test_the_spawner_exports_the_basis_not_just_the_cap(self):
        from backend.core.ouroboros.cli import thin_client
        src = inspect.getsource(thin_client)
        assert "OUROBOROS_BATTLE_COST_CAP_BASIS" in src

    def test_the_hydration_payload_carries_it(self):
        from backend.core.ouroboros.battle_test import harness
        assert "cost_budget_basis" in inspect.getsource(harness)


class TestTheRenderIsNotSilentlyTruncated:
    def test_lines_below_the_budget_row_still_render(self):
        """A fail-soft wrapper turns a typo into MISSING OUTPUT rather than a
        crash. Using `_Text` where this function aliases `_T` raised
        NameError, and every line below — including the liquidity rows —
        silently stopped rendering. Verified by asserting the tail is present.
        """
        buf = Console(force_terminal=False, width=120, record=True)
        ov._render_hydration(buf, {
            "status": {"phase": "IDLE", "cost_spent_usd": 0.0,
                       "cost_budget_usd": 0.71,
                       "cost_budget_basis": "observed — 98 sessions"},
            "liquidity": {"providers": {"doubleword": {"tokens_remaining": None}},
                          "any_exhausted": True,
                          "economic": {"doubleword": {"state": "economic",
                                                      "reason": "status 402"}}},
            "ops": []})
        out = buf.export_text()
        assert "budget $0.71" in out          # the new line
        assert "liquidity doubleword" in out  # a line AFTER it
        assert "OUT OF CREDIT" in out         # and the warning after that
