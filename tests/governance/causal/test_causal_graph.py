"""TDD spine for Domain-1 Staging-2 Task 1 -- the in-memory CausalGraph fold.

Eight brief cases proving the O(1) emit_seq-monotonic fold and the fully
deterministic snapshot / fingerprint that Task 4's crash-recovery determinism
proof depends on:

  (a) symbols_added        -> nodes present with correct fields
  (b) resignature (higher) -> signature_hash updated
  (c) STALE (lower/equal)  -> NO-OP  (the commutativity pin)
  (d) removed (higher)     -> node gone; stale remove -> node stays
  (e) file_level_churn     -> that file's nodes re-stamped; OTHER files untouched
  (f) two orderings        -> identical state_fingerprint (Mandate-4 foundation)
  (g) snapshot roundtrip   -> identical state_fingerprint
  (h) malformed envelope   -> returns 0, never raises

Real ``StructuralDelta`` from Staging-0 -- no mocks. Envelopes are built the
way Staging-1 delivers them: ``{"delta": delta.to_dict(), "lineage": {...}}``.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.causal.causal_graph import (
    CausalGraph,
    CausalNode,
)
from backend.core.ouroboros.governance.causal.structural_delta import (
    ImportEdge,
    StructuralDelta,
    SymbolRecord,
)


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------

def _delta(
    repo: str,
    file_path: str,
    *,
    added=(),
    removed=(),
    resig=(),
    imp_added=(),
    imp_removed=(),
    churn: bool = False,
) -> StructuralDelta:
    return StructuralDelta(
        repo=repo,
        file_path=file_path,
        symbols_added=tuple(added),
        symbols_removed=tuple(removed),
        symbols_resignatured=tuple(resig),
        import_edges_added=tuple(imp_added),
        import_edges_removed=tuple(imp_removed),
        file_level_churn=churn,
        churn_counts={},
    )


def _env(delta: StructuralDelta, repo: str, emit_seq: int, head_sha: str = "sha") -> dict:
    return {
        "delta": delta.to_dict(),
        "lineage": {
            "repo": repo,
            "head_sha": head_sha,
            "parent_sha": "parent",
            "merge_base": "base",
            "emit_seq": emit_seq,
        },
    }


def _sym(symbol_id: str, kind: str, sig: str) -> SymbolRecord:
    return SymbolRecord(symbol_id=symbol_id, kind=kind, signature_hash=sig)


# ---------------------------------------------------------------------------
# (a) symbols_added -> nodes present with correct fields
# ---------------------------------------------------------------------------

def test_symbols_added_creates_nodes():
    g = CausalGraph()
    d = _delta(
        "brain",
        "mod.py",
        added=(_sym("brain:mod.py:A", "class", "h1"), _sym("brain:mod.py:f", "function", "h2")),
    )
    n = g.apply_delta(_env(d, "brain", 5, head_sha="deadbeef"))
    assert n == 2
    assert g.node_count() == 2

    a = g.node("brain:mod.py:A")
    assert isinstance(a, CausalNode)
    assert a.repo == "brain"
    assert a.file_path == "mod.py"
    assert a.kind == "class"
    assert a.signature_hash == "h1"
    assert a.last_emit_seq == 5
    assert a.last_head_sha == "deadbeef"
    assert a.imports == frozenset()

    repo_nodes = g.nodes_in_repo("brain")
    assert {x.symbol_id for x in repo_nodes} == {"brain:mod.py:A", "brain:mod.py:f"}


# ---------------------------------------------------------------------------
# (b) resignature (higher emit_seq) -> signature_hash updated
# ---------------------------------------------------------------------------

def test_resignature_higher_seq_updates_signature():
    g = CausalGraph()
    g.apply_delta(_env(_delta("brain", "mod.py", added=(_sym("brain:mod.py:A", "class", "h1"),)), "brain", 1))

    n = g.apply_delta(
        _env(_delta("brain", "mod.py", resig=(("brain:mod.py:A", "h1", "h2"),)), "brain", 2)
    )
    assert n == 1
    a = g.node("brain:mod.py:A")
    assert a.signature_hash == "h2"
    assert a.kind == "class"           # kind preserved across resignature
    assert a.last_emit_seq == 2


# ---------------------------------------------------------------------------
# (c) STALE (lower / equal emit_seq) -> NO-OP  (commutativity pin)
# ---------------------------------------------------------------------------

def test_stale_delta_is_noop():
    g = CausalGraph()
    g.apply_delta(_env(_delta("brain", "mod.py", added=(_sym("brain:mod.py:A", "class", "h1"),)), "brain", 5))
    g.apply_delta(_env(_delta("brain", "mod.py", resig=(("brain:mod.py:A", "h1", "hNEW"),)), "brain", 9))
    fp_before = g.state_fingerprint()
    assert g.node("brain:mod.py:A").signature_hash == "hNEW"
    assert g.node("brain:mod.py:A").last_emit_seq == 9

    # lower emit_seq -> ignored
    low = g.apply_delta(_env(_delta("brain", "mod.py", resig=(("brain:mod.py:A", "hNEW", "hLOW"),)), "brain", 3))
    # equal emit_seq -> ignored
    eq = g.apply_delta(_env(_delta("brain", "mod.py", resig=(("brain:mod.py:A", "hNEW", "hEQ"),)), "brain", 9))

    assert low == 0
    assert eq == 0
    assert g.node("brain:mod.py:A").signature_hash == "hNEW"
    assert g.state_fingerprint() == fp_before


# ---------------------------------------------------------------------------
# (d) removed (higher) -> gone; stale remove -> stays
# ---------------------------------------------------------------------------

def test_remove_higher_then_stale_remove_stays():
    g = CausalGraph()
    g.apply_delta(_env(_delta("brain", "mod.py", added=(_sym("brain:mod.py:A", "class", "h1"),)), "brain", 4))

    n = g.apply_delta(_env(_delta("brain", "mod.py", removed=(_sym("brain:mod.py:A", "class", "h1"),)), "brain", 7))
    assert n == 1
    assert g.node("brain:mod.py:A") is None
    assert g.node_count() == 0

    # re-add at seq 10, then a stale remove at seq 6 must NOT delete it
    g.apply_delta(_env(_delta("brain", "mod.py", added=(_sym("brain:mod.py:A", "class", "h9"),)), "brain", 10))
    stale = g.apply_delta(_env(_delta("brain", "mod.py", removed=(_sym("brain:mod.py:A", "class", "h9"),)), "brain", 6))
    assert stale == 0
    assert g.node("brain:mod.py:A") is not None
    assert g.node("brain:mod.py:A").last_emit_seq == 10


# ---------------------------------------------------------------------------
# (e) file_level_churn -> that file's nodes re-stamped; OTHER files untouched
# ---------------------------------------------------------------------------

def test_file_level_churn_scopes_to_one_file():
    g = CausalGraph()
    g.apply_delta(_env(_delta("brain", "a.py", added=(_sym("brain:a.py:A", "class", "ha"),)), "brain", 1))
    g.apply_delta(_env(_delta("brain", "b.py", added=(_sym("brain:b.py:B", "class", "hb"),)), "brain", 2))
    g.apply_delta(_env(_delta("prime", "c.py", added=(_sym("prime:c.py:C", "class", "hc"),)), "prime", 3))

    unrelated_before = g.node("prime:c.py:C")
    b_before = g.node("brain:b.py:B")

    # churn on brain/a.py at a newer seq
    n = g.apply_delta(_env(_delta("brain", "a.py", churn=True), "brain", 8, head_sha="churnsha"))
    assert n == 1

    a = g.node("brain:a.py:A")
    assert a.last_emit_seq == 8
    assert a.last_head_sha == "churnsha"
    assert a.signature_hash == "ha"     # structure elided -> unchanged

    # OTHER files byte-identical
    assert g.node("prime:c.py:C") == unrelated_before
    assert g.node("brain:b.py:B") == b_before

    # churn on a file with no known nodes -> no-op
    assert g.apply_delta(_env(_delta("prime", "nope.py", churn=True), "prime", 99)) == 0


# ---------------------------------------------------------------------------
# (f) two orderings -> identical fingerprint (Mandate-4 foundation)
# ---------------------------------------------------------------------------

def _order_independence_envelopes():
    # F1 in brain is touched by add@3, resig@4, and a WINNING full add@7.
    # Every other symbol is touched by exactly one delta. Out-of-order emit_seqs.
    return [
        _env(_delta("brain", "f.py", added=(_sym("brain:f.py:F1", "function", "s3"),)), "brain", 3, "c3"),
        _env(_delta("prime", "g.py", added=(_sym("prime:g.py:G1", "class", "sg"),)), "prime", 1, "cg"),
        _env(_delta("brain", "f.py", resig=(("brain:f.py:F1", "s3", "s4"),)), "brain", 4, "c4"),
        _env(_delta("reactor", "h.py", added=(_sym("reactor:h.py:H1", "method", "sh"),)), "reactor", 5, "ch"),
        _env(_delta("brain", "f.py", added=(_sym("brain:f.py:F1", "function", "s7"),)), "brain", 7, "c7"),
        _env(_delta("prime", "g.py", added=(_sym("prime:g.py:G2", "function", "sg2"),)), "prime", 2, "cg2"),
    ]


def test_two_orderings_same_fingerprint():
    import random

    envs = _order_independence_envelopes()

    g1 = CausalGraph()
    for e in envs:
        g1.apply_delta(e)

    shuffled = list(envs)
    random.Random(1234).shuffle(shuffled)
    assert [e["lineage"]["emit_seq"] for e in shuffled] != [e["lineage"]["emit_seq"] for e in envs]

    g2 = CausalGraph()
    for e in shuffled:
        g2.apply_delta(e)

    assert g1.state_fingerprint() == g2.state_fingerprint()

    # sanity: the WINNING full add@7 determines F1
    f1 = g1.node("brain:f.py:F1")
    assert f1.signature_hash == "s7"
    assert f1.last_emit_seq == 7
    assert g1.node_count() == 4


# ---------------------------------------------------------------------------
# (f2) order-independence WITH removes, re-adds, tombstones (Task 1 review)
# ---------------------------------------------------------------------------

def _tombstone_envelopes():
    # Every symbol's TERMINAL (highest-seq) op is a FULL write (add or remove),
    # so the fold is provably commutative under any permutation -- including
    # remove-before-add and a resignature interleaved mid-history.
    return [
        # A: add@1 then remove@3  -> terminal remove -> ABSENT, hwm 3
        _env(_delta("brain", "a.py", added=(_sym("brain:a.py:A", "class", "a1"),)), "brain", 1),
        _env(_delta("brain", "a.py", removed=(_sym("brain:a.py:A", "class", "a1"),)), "brain", 3),
        # B: add@2, resignature@5, re-add@8 -> terminal full add@8 -> PRESENT s8, hwm 8
        _env(_delta("prime", "b.py", added=(_sym("prime:b.py:B", "function", "b2"),)), "prime", 2),
        _env(_delta("prime", "b.py", resig=(("prime:b.py:B", "b2", "b5"),)), "prime", 5),
        _env(_delta("prime", "b.py", added=(_sym("prime:b.py:B", "function", "b8"),)), "prime", 8),
        # C: remove@4 with a STALE add@2 -> terminal remove -> ABSENT, hwm 4 (no resurrection)
        _env(_delta("reactor", "c.py", removed=(_sym("reactor:c.py:C", "method", "c0"),)), "reactor", 4),
        _env(_delta("reactor", "c.py", added=(_sym("reactor:c.py:C", "method", "c2"),)), "reactor", 2),
        # D: lone add@6 -> PRESENT, hwm 6
        _env(_delta("brain", "d.py", added=(_sym("brain:d.py:D", "class", "d6"),)), "brain", 6),
    ]


def test_order_independence_with_removes_and_tombstones():
    import random

    envs = _tombstone_envelopes()

    def _fold(order):
        g = CausalGraph()
        for e in order:
            g.apply_delta(e)
        return g

    base = _fold(envs)

    for seed in (1, 7, 42, 2026):
        shuffled = list(envs)
        random.Random(seed).shuffle(shuffled)
        g = _fold(shuffled)
        assert g.state_fingerprint() == base.state_fingerprint(), f"seed={seed}"

    # the converged truth
    assert base.node("brain:a.py:A") is None          # removed
    assert base.node("reactor:c.py:C") is None         # stale add never resurrected
    b = base.node("prime:b.py:B")
    assert b is not None and b.signature_hash == "b8" and b.last_emit_seq == 8
    assert base.node("brain:d.py:D") is not None
    assert base.node_count() == 2


# ---------------------------------------------------------------------------
# remove-before-add: the tombstone blocks resurrection (Task 1 review)
# ---------------------------------------------------------------------------

def test_out_of_order_remove_before_add_no_resurrection():
    # out-of-order: remove(3) before add(1)  vs  in-order: add(1) then remove(3).
    # Both must converge to ABSENT with an identical fingerprint (hwm[X]=3 both).
    ooo = CausalGraph()
    ooo.apply_delta(_env(_delta("brain", "x.py", removed=(_sym("brain:x.py:X", "class", "hx"),)), "brain", 3))
    ooo.apply_delta(_env(_delta("brain", "x.py", added=(_sym("brain:x.py:X", "class", "hx1"),)), "brain", 1))

    ordered = CausalGraph()
    ordered.apply_delta(_env(_delta("brain", "x.py", added=(_sym("brain:x.py:X", "class", "hx1"),)), "brain", 1))
    ordered.apply_delta(_env(_delta("brain", "x.py", removed=(_sym("brain:x.py:X", "class", "hx"),)), "brain", 3))

    assert ooo.node("brain:x.py:X") is None
    assert ordered.node("brain:x.py:X") is None
    assert ooo.state_fingerprint() == ordered.state_fingerprint()

    g = CausalGraph()

    # a remove arrives for a symbol never yet seen -> records the tombstone hwm
    n = g.apply_delta(_env(_delta("brain", "x.py", removed=(_sym("brain:x.py:X", "class", "hx"),)), "brain", 3))
    assert n == 0                       # no node existed to delete
    assert g.node("brain:x.py:X") is None

    # a STALE lower-seq add must NOT resurrect the symbol
    n = g.apply_delta(_env(_delta("brain", "x.py", added=(_sym("brain:x.py:X", "class", "hx1"),)), "brain", 1))
    assert n == 0
    assert g.node("brain:x.py:X") is None

    # equal-seq add is also stale -> still absent
    assert g.apply_delta(_env(_delta("brain", "x.py", added=(_sym("brain:x.py:X", "class", "hx3"),)), "brain", 3)) == 0
    assert g.node("brain:x.py:X") is None

    # a genuinely NEWER add (seq > tombstone) DOES resurrect it -- the guard is
    # monotonic, not a permanent gravestone
    n = g.apply_delta(_env(_delta("brain", "x.py", added=(_sym("brain:x.py:X", "class", "hx9"),)), "brain", 9))
    assert n == 1
    x = g.node("brain:x.py:X")
    assert x is not None and x.signature_hash == "hx9" and x.last_emit_seq == 9


# ---------------------------------------------------------------------------
# import edges fold onto the src node
# ---------------------------------------------------------------------------

def test_import_edges_update_src_node_imports():
    g = CausalGraph()
    # a node whose symbol_id is the import src
    g.apply_delta(_env(_delta("brain", "mod.py", added=(_sym("brain:mod.py:mod", "function", "h1"),)), "brain", 1))
    g.apply_delta(
        _env(
            _delta(
                "brain",
                "mod.py",
                imp_added=(
                    ImportEdge("brain:mod.py:mod", "os.path", "imports_from"),
                    ImportEdge("brain:mod.py:mod", "sys", "imports"),
                ),
            ),
            "brain",
            2,
        )
    )
    assert g.node("brain:mod.py:mod").imports == frozenset({"os.path", "sys"})

    # remove one at a newer seq
    g.apply_delta(
        _env(_delta("brain", "mod.py", imp_removed=(ImportEdge("brain:mod.py:mod", "sys", "imports"),)), "brain", 3)
    )
    assert g.node("brain:mod.py:mod").imports == frozenset({"os.path"})


# ---------------------------------------------------------------------------
# snapshot / fingerprint carry the FULL hwm map (live + tombstoned) -- review
# ---------------------------------------------------------------------------

def test_snapshot_includes_hwm():
    # (1) same LIVE node-set reached two ways, SAME hwm -> identical fingerprint.
    gc = CausalGraph()  # added -> removed -> re-added at higher seq
    gc.apply_delta(_env(_delta("brain", "m.py", added=(_sym("brain:m.py:X", "class", "s1"),)), "brain", 1))
    gc.apply_delta(_env(_delta("brain", "m.py", removed=(_sym("brain:m.py:X", "class", "s1"),)), "brain", 3))
    gc.apply_delta(_env(_delta("brain", "m.py", added=(_sym("brain:m.py:X", "class", "s5"),)), "brain", 5))

    gd = CausalGraph()  # direct add at seq 5
    gd.apply_delta(_env(_delta("brain", "m.py", added=(_sym("brain:m.py:X", "class", "s5"),)), "brain", 5))

    assert gc.node("brain:m.py:X") == gd.node("brain:m.py:X")
    assert gc.snapshot()["seq_hwm"] == gd.snapshot()["seq_hwm"] == {"brain:m.py:X": 5}
    assert gc.state_fingerprint() == gd.state_fingerprint()

    # (2) IDENTICAL live node-set (empty) but DIFFERENT hwm -> fingerprints DIFFER,
    # proving the tombstone hwm is part of the equality witness.
    ge = CausalGraph()  # add then remove -> empty live, hwm[Y]=3 tombstone
    ge.apply_delta(_env(_delta("brain", "n.py", added=(_sym("brain:n.py:Y", "class", "y1"),)), "brain", 1))
    ge.apply_delta(_env(_delta("brain", "n.py", removed=(_sym("brain:n.py:Y", "class", "y1"),)), "brain", 3))
    gf = CausalGraph()  # empty, no hwm

    assert ge.node_count() == gf.node_count() == 0
    assert ge.snapshot()["seq_hwm"] == {"brain:n.py:Y": 3}
    assert gf.snapshot()["seq_hwm"] == {}
    assert ge.state_fingerprint() != gf.state_fingerprint()

    # (3) roundtrip is stable WITH a tombstoned (removed) symbol in the hwm.
    rebuilt = CausalGraph.from_snapshot(ge.snapshot())
    assert rebuilt.state_fingerprint() == ge.state_fingerprint()
    assert rebuilt.snapshot()["seq_hwm"] == {"brain:n.py:Y": 3}
    # the tombstone survives the roundtrip: a stale add still cannot resurrect Y
    assert rebuilt.apply_delta(_env(_delta("brain", "n.py", added=(_sym("brain:n.py:Y", "class", "y2"),)), "brain", 2)) == 0
    assert rebuilt.node("brain:n.py:Y") is None


# ---------------------------------------------------------------------------
# (g) snapshot -> from_snapshot -> identical fingerprint
# ---------------------------------------------------------------------------

def test_snapshot_roundtrip_identical_fingerprint():
    g = CausalGraph()
    for e in _order_independence_envelopes():
        g.apply_delta(e)
    # add an import fact for good measure
    g.apply_delta(
        _env(_delta("brain", "f.py", imp_added=(ImportEdge("brain:f.py:F1", "collections.abc", "imports_from"),)), "brain", 11)
    )

    snap = g.snapshot()
    fp = g.state_fingerprint()

    g2 = CausalGraph.from_snapshot(snap)
    assert g2.state_fingerprint() == fp
    assert g2.node_count() == g.node_count()
    assert g2.node("brain:f.py:F1").imports == g.node("brain:f.py:F1").imports

    # snapshot is deterministic across calls
    assert g.snapshot() == snap


# ---------------------------------------------------------------------------
# (h) malformed envelope -> 0, no raise
# ---------------------------------------------------------------------------

def test_malformed_envelope_returns_zero_no_raise():
    g = CausalGraph()
    assert g.apply_delta(None) == 0
    assert g.apply_delta({}) == 0
    assert g.apply_delta({"delta": {}}) == 0                      # missing lineage
    assert g.apply_delta({"delta": {}, "lineage": {}}) == 0       # missing emit_seq / bad delta
    assert g.apply_delta({"lineage": {"emit_seq": 1}}) == 0       # missing delta
    assert g.apply_delta(
        {"delta": {}, "lineage": {"emit_seq": "notint", "head_sha": "x", "repo": "brain"}}
    ) == 0
    assert g.apply_delta("garbage") == 0
    assert g.node_count() == 0
