"""The edge set is declared once, and nothing may re-spell it.

WHAT WENT WRONG
---------------
`probe_edge_compute` was added as edge 9. Five separate places went on
describing an eight-edge matrix:

  * the module docstring ("the 8-edge full-chain connectivity matrix",
    enumerating 1-8);
  * the hand-ordered list in `run_matrix` under a `# canonical edge order`
    comment;
  * two tests asserting `len(report.verdicts) == 8`, which failed on `main`
    for days;
  * ~38 `EdgeVerdict(...)` call sites, each repeating its own label string on
    every return path;
  * the test file itself, which spelled 25 more label literals.

None of them was authoritative, so none of them could be updated. The same
drift had already produced a genuine operator-visible collision: the
`--live` probe was numbered 9 when 9 was free, `probe_edge_compute` later
took 9 as well, and `run_doctor(live=True)` appends both to ONE report -- so
`ov doctor --live` rendered two rows numbered 9.

These tests pin the cure rather than the symptom: `EDGES` is the only place
the edge set is written down, and a literal cannot come back.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

from backend.core.ouroboros.cli import ov_doctor


class TestDeclaration:
    def test_numbers_are_unique(self):
        """The defect that shipped: two different edges both numbered 9."""
        numbers = [e.number for e in ov_doctor.EDGES]
        assert len(numbers) == len(set(numbers)), (
            f"duplicate edge number in EDGES: {numbers}"
        )

    def test_keys_are_unique(self):
        keys = [e.key for e in ov_doctor.EDGES]
        assert len(keys) == len(set(keys))

    def test_labels_are_unique(self):
        labels = [e.label for e in ov_doctor.EDGES]
        assert len(labels) == len(set(labels))

    def test_numbering_is_dense_and_ordered(self):
        """1..N with no gaps, in declaration order -- the operator reads these
        as positions, and a gap or a jump is a missing edge or a stale one."""
        assert [e.number for e in ov_doctor.EDGES] == list(
            range(1, len(ov_doctor.EDGES) + 1)
        )

    def test_label_is_derived_never_stored(self):
        for spec in ov_doctor.EDGES:
            assert spec.label == f"{spec.number} {spec.name}"

    def test_matrix_subset_excludes_opt_in_edges(self):
        assert all(not e.optional for e in ov_doctor.MATRIX_EDGES)
        assert ov_doctor.edge_count() == len(ov_doctor.MATRIX_EDGES)
        assert ov_doctor.edge_count() < len(ov_doctor.EDGES), (
            "the --live probe is opt-in; counting it would make every "
            "base-matrix assertion wrong by one"
        )


class TestLookup:
    def test_every_key_resolves(self):
        for spec in ov_doctor.EDGES:
            assert ov_doctor.edge(spec.key) == spec.label

    def test_unknown_key_is_visible_not_fatal_and_not_invented(self):
        """This module's contract is that no probe raises from a return path.

        A fabricated-but-plausible label would be worse than an unrecognised
        one: it would render as a real edge and match nothing."""
        assert ov_doctor.edge("no-such-edge") == "? no-such-edge"
        assert ov_doctor.edge(None) == "? None"  # type: ignore[arg-type]


class TestOrdering:
    def test_canonical_order_is_recovered_from_any_arrival_order(self):
        V = ov_doctor.EdgeVerdict
        S = ov_doctor.EdgeState
        shuffled = [
            V(ov_doctor.edge("compute"), S.OK),
            V(ov_doctor.edge("process"), S.OK),
            V(ov_doctor.edge("liveness"), S.OK),
            V(ov_doctor.edge("cockpit"), S.OK),
        ]
        ordered = [v.edge for v in ov_doctor.order_verdicts(shuffled)]
        assert ordered == [
            ov_doctor.edge("process"), ov_doctor.edge("cockpit"),
            ov_doctor.edge("liveness"), ov_doctor.edge("compute"),
        ]

    def test_unknown_edges_sort_last_but_are_never_dropped(self):
        """A matrix that silently discarded a row would lie about what it
        probed -- worse than showing one it cannot place."""
        V = ov_doctor.EdgeVerdict
        S = ov_doctor.EdgeState
        out = ov_doctor.order_verdicts([
            V("? mystery", S.DEGRADED),
            V(ov_doctor.edge("process"), S.OK),
        ])
        assert [v.edge for v in out] == [ov_doctor.edge("process"), "? mystery"]

    def test_ordering_never_raises_on_malformed_input(self):
        assert ov_doctor.order_verdicts([object()])  # type: ignore[list-item]


class TestNoSecondSourceOfTruth:
    """The cage: a numbered edge label may not be written as a literal."""

    @staticmethod
    def _numbered_string_literals(path: Path):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        # The declaration itself legitimately spells the names; everything
        # after it must go through `edge()`.
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant):
                continue
            val = node.value
            if not isinstance(val, str) or len(val) < 3:
                continue
            head = val.split(" ", 1)[0]
            if head.isdigit() and val[len(head):].startswith(" "):
                offenders.append((node.lineno, val))
        return offenders

    def test_module_has_no_numbered_edge_literals_outside_the_registry(self):
        path = Path(inspect.getfile(ov_doctor))
        src = path.read_text(encoding="utf-8").splitlines()
        # Registry block ends at the EdgeState enum; everything below it is
        # code that must reference the declaration.
        try:
            cutoff = next(i for i, l in enumerate(src, 1)
                          if l.startswith("class EdgeState("))
        except StopIteration:  # pragma: no cover
            cutoff = 0
        offenders = [(ln, v) for ln, v in self._numbered_string_literals(path)
                     if ln > cutoff]
        assert not offenders, (
            "numbered edge label spelled as a literal — use edge(<key>) so "
            f"EDGES stays the only source: {offenders}"
        )

    def test_every_edge_key_used_in_the_module_is_declared(self):
        """An undeclared key renders '? key' and matches nothing. The AST pin
        is what keeps that from ever shipping."""
        path = Path(inspect.getfile(ov_doctor))
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "edge"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)):
                used.add(node.args[0].value)
        declared = {e.key for e in ov_doctor.EDGES}
        assert used <= declared, f"undeclared edge key(s): {used - declared}"
