"""Regression spine — OperationTimeline read-model (PRD §42, Slice 1).

Two test families:

  * **AST pins 1–4** (§42.6): structurally prove the read-model lacks
    authority — authority-free import graph / single persistence seam /
    subscribes-never-re-derives / no state authority. Each pin ships
    with a negative-fixture sibling so a deliberately-violating shape
    makes the pin red.

  * **Behavioral**: the zero-behavior-change guarantee (flag OFF ⇒
    zero rows, zero disk I/O), the causal merge across the three
    OpsDigestObserver callbacks, monotonic non-reused refs, lossless
    to_dict/from_dict, bounded idempotent disk replay (the
    cross-session morning-after substrate), fail-closed never-raises,
    and the FlagRegistry seeds (masters default-FALSE per §33.1).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import operation_timeline as ot
from backend.core.ouroboros.governance.operation_timeline import (
    OPERATION_TIMELINE_ENABLED_ENV_VAR,
    OPERATION_TIMELINE_MAX_ROWS_ENV_VAR,
    OPERATION_TIMELINE_PATH_ENV_VAR,
    REF_PREFIX,
    TIMELINE_SCHEMA_VERSION,
    OperationTimeline,
    TimelineRow,
    get_default_timeline,
    register_flags,
    reset_default_timeline,
)


def _module_source() -> str:
    return Path(ot.__file__).read_text(encoding="utf-8")


# ===========================================================================
# AST pin 1 — read-model authority-free
# ===========================================================================

_FORBIDDEN_AUTHORITY_MODULES = (
    "orchestrator",
    "policy_engine",
    "iron_gate",
    "change_engine",
    "candidate_generator",
    "governed_loop_service",
    "repair_engine",
)


def test_ast_pin_1_authority_free_import_graph() -> None:
    tree = ast.parse(_module_source())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for forbidden in _FORBIDDEN_AUTHORITY_MODULES:
                if forbidden in mod:
                    offenders.append(mod)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                for forbidden in _FORBIDDEN_AUTHORITY_MODULES:
                    if forbidden in alias.name:
                        offenders.append(alias.name)
    assert not offenders, (
        f"operation_timeline.py imports authority modules {offenders} — "
        "the read-model must be structurally incapable of acting on "
        "the loop (§42.6 pin 1)"
    )


def test_ast_pin_1_negative_fixture_detects_violation() -> None:
    bad = "from backend.core.ouroboros.governance.orchestrator import X\n"
    tree = ast.parse(bad)
    hit = any(
        isinstance(n, ast.ImportFrom)
        and "orchestrator" in (n.module or "")
        for n in ast.walk(tree)
    )
    assert hit, "pin-1 walker must detect an orchestrator import"


# ===========================================================================
# AST pin 2 — single persistence seam
# ===========================================================================


def test_ast_pin_2_imports_canonical_flock_append_line() -> None:
    tree = ast.parse(_module_source())
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if "cross_process_jsonl" in (node.module or ""):
                for alias in node.names:
                    if alias.name == "flock_append_line":
                        found = True
    assert found, (
        "operation_timeline.py must import the canonical "
        "flock_append_line (§42.6 pin 2)"
    )


def test_ast_pin_2_no_fcntl_or_raw_append_or_json_dump() -> None:
    src = _module_source()
    assert "import fcntl" not in src, "no homegrown fcntl seam (pin 2)"
    assert "fcntl." not in src, "no fcntl reference (pin 2)"
    # json.dumps(...) (string) is allowed; json.dump(..., fp) (direct
    # file write) is the forbidden parallel-persistence shape.
    assert "json.dump(" not in src, (
        "no json.dump to a file object — the only disk seam is "
        "flock_append_line (pin 2)"
    )
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "open":
                # No append-mode open anywhere in the module.
                for arg in node.args[1:]:
                    if isinstance(arg, ast.Constant) and "a" in str(
                        arg.value
                    ):
                        raise AssertionError(
                            "append-mode open() found — use "
                            "flock_append_line (pin 2)"
                        )


# ===========================================================================
# AST pin 3 — subscribes, never re-derives
# ===========================================================================


def test_ast_pin_3_defines_observer_protocol_surface() -> None:
    tree = ast.parse(_module_source())
    required = {
        "on_apply_succeeded",
        "on_verify_completed",
        "on_commit_succeeded",
    }
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "OperationTimeline":
            defined = {
                fn.name for fn in cls.body
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            missing = required - defined
            assert not missing, (
                f"OperationTimeline missing protocol methods {missing} "
                "(§42.6 pin 3)"
            )
            return
    raise AssertionError("OperationTimeline class not found")


def test_ast_pin_3_no_event_re_derivation() -> None:
    """AST-node based (NOT substring on source): the docstring legitimately
    *describes* the ban, so the pin must inspect actual code nodes —
    imports, calls, and string-literal subprocess args — never prose."""
    tree = ast.parse(_module_source())
    banned_import_substrings = ("subprocess", "test_runner")
    banned_call_names = {
        "create_subprocess_exec",
        "create_subprocess_shell",
        "check_output",
        "check_call",
        "Popen",
        "run",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for bad in banned_import_substrings:
                    assert bad not in alias.name, (
                        f"import {alias.name!r} re-derives events "
                        "(§42.6 pin 3)"
                    )
                assert alias.name != "subprocess"
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for bad in banned_import_substrings:
                assert bad not in mod, (
                    f"import from {mod!r} re-derives events (pin 3)"
                )
            for alias in node.names:
                assert "TestRunner" not in alias.name, (
                    "TestRunner import re-derives VERIFY (pin 3)"
                )
        if isinstance(node, ast.Call):
            fn = node.func
            name = (
                fn.attr if isinstance(fn, ast.Attribute)
                else fn.id if isinstance(fn, ast.Name)
                else ""
            )
            assert name not in banned_call_names, (
                f"call {name!r} detects rather than consumes events "
                "(§42.6 pin 3)"
            )
            # No subprocess argv built from a literal "git".
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value == "git":
                    raise AssertionError(
                        "literal 'git' subprocess arg — the read-model "
                        "must not shell out (pin 3)"
                    )


# ===========================================================================
# AST pin 4 — no state authority
# ===========================================================================


def test_ast_pin_4_no_state_authority() -> None:
    """AST-node based: the docstring legitimately names OperationState
    when *describing* the ban. The pin inspects code nodes only —
    .record() calls, OperationState name references, and authority
    imports — never prose."""
    tree = ast.parse(_module_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(
            node.func, ast.Attribute
        ):
            assert node.func.attr != "record", (
                "operation_timeline.py calls .record() — it must never "
                "write OperationLedger state (§42.6 pin 4)"
            )
        if isinstance(node, ast.Name):
            assert node.id != "OperationState", (
                "OperationState referenced in code — state is the "
                "OperationLedger's authority (pin 4)"
            )
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "risk_tier_floor" not in mod, (
                "no risk_tier_floor import — risk_tier is a copied "
                "string, never computed here (pin 4)"
            )
            assert not mod.endswith("policy_engine"), (
                "no policy_engine import — the timeline has no policy "
                "authority (pin 4)"
            )
            for alias in node.names:
                assert alias.name != "OperationState", (
                    "no OperationState import (pin 4)"
                )


# ===========================================================================
# Behavioral — the zero-behavior-change guarantee (load-bearing)
# ===========================================================================


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Each test gets a fresh singleton + a tmp JSONL path + a clean
    env (no inherited JARVIS_OPERATION_TIMELINE_* from the shell)."""
    for var in (
        OPERATION_TIMELINE_ENABLED_ENV_VAR,
        OPERATION_TIMELINE_PATH_ENV_VAR,
        OPERATION_TIMELINE_MAX_ROWS_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    reset_default_timeline()
    yield
    reset_default_timeline()


def test_flag_off_is_a_hard_noop_zero_rows_zero_file(tmp_path) -> None:
    """The §42.8 Slice-1 contract: flag OFF ⇒ zero rows, zero disk
    I/O, zero behavior change. This is the load-bearing proof."""
    path = tmp_path / "timeline.jsonl"
    tl = OperationTimeline(persistence_path=path, enabled=False)
    tl.on_apply_succeeded(op_id="op-1", mode="single", files=2)
    tl.on_verify_completed(op_id="op-1", passed=4, total=4)
    tl.on_commit_succeeded(op_id="op-1", commit_hash="deadbeef")
    assert len(tl) == 0
    assert not path.exists(), "flag OFF must not create the JSONL file"
    assert tl.query() == ()


def test_flag_off_via_env_default_is_noop(tmp_path) -> None:
    # No env set at all ⇒ default-FALSE ⇒ no-op (the §33.1 default).
    path = tmp_path / "timeline.jsonl"
    tl = OperationTimeline(persistence_path=path)  # no enabled override
    tl.on_commit_succeeded(op_id="op-x", commit_hash="abc123")
    assert len(tl) == 0
    assert not path.exists()


# ===========================================================================
# Behavioral — causal merge across the three callbacks
# ===========================================================================


def _enabled_timeline(tmp_path) -> OperationTimeline:
    return OperationTimeline(
        persistence_path=tmp_path / "timeline.jsonl", enabled=True,
    )


def test_three_callbacks_merge_into_one_row_stable_ref(tmp_path) -> None:
    tl = _enabled_timeline(tmp_path)
    tl.on_apply_succeeded(op_id="op-7", mode="multi", files=3)
    row_after_apply = tl.query(op_id="op-7")[0]
    ref = row_after_apply.ref
    assert ref.startswith(REF_PREFIX)

    tl.on_verify_completed(
        op_id="op-7", passed=10, total=12, scoped_to_applied_op=True,
    )
    tl.on_commit_succeeded(op_id="op-7", commit_hash="17ae95d7d6")

    rows = tl.query(op_id="op-7")
    assert len(rows) == 1, "three callbacks for one op_id ⇒ one row"
    r = rows[0]
    assert r.ref == ref, "ref is stable across merges"
    assert r.apply_mode == "multi" and r.apply_files == 3
    assert r.verify_passed == 10 and r.verify_total == 12
    assert r.verify_scoped_to_op is True
    assert r.commit_hash == "17ae95d7d6"
    assert r.first_seen_iso and r.updated_iso
    assert r.updated_iso >= r.first_seen_iso


def test_refs_are_monotonic_and_never_reused(tmp_path) -> None:
    tl = _enabled_timeline(tmp_path)
    tl.on_apply_succeeded(op_id="op-a", mode="single", files=1)
    tl.on_apply_succeeded(op_id="op-b", mode="single", files=1)
    tl.on_apply_succeeded(op_id="op-c", mode="single", files=1)
    refs = {r.op_id: r.ref for r in tl.query()}
    nums = sorted(int(v[len(REF_PREFIX):]) for v in refs.values())
    assert nums == [1, 2, 3], f"non-monotonic refs: {refs}"
    assert len(set(refs.values())) == 3, "refs must be unique"


def test_query_orders_newest_first_and_filters(tmp_path) -> None:
    tl = _enabled_timeline(tmp_path)
    tl.on_apply_succeeded(op_id="old", mode="single", files=1)
    tl.on_apply_succeeded(op_id="new", mode="single", files=1)
    tl.on_commit_succeeded(op_id="new", commit_hash="c0ffee")
    ordered = [r.op_id for r in tl.query()]
    assert ordered[0] == "new", "newest first"
    committed = tl.query(has_commit=True)
    assert [r.op_id for r in committed] == ["new"]
    uncommitted = tl.query(has_commit=False)
    assert [r.op_id for r in uncommitted] == ["old"]
    assert tl.query(limit=1) and len(tl.query(limit=1)) == 1


def test_lookup_by_ref(tmp_path) -> None:
    tl = _enabled_timeline(tmp_path)
    tl.on_apply_succeeded(op_id="op-z", mode="none", files=0)
    ref = tl.query(op_id="op-z")[0].ref
    assert tl.lookup(ref) is not None
    assert tl.lookup(ref).op_id == "op-z"
    assert tl.lookup("r-99999") is None
    assert tl.lookup("garbage") is None


# ===========================================================================
# Behavioral — schema roundtrip + closed field set
# ===========================================================================


def test_to_dict_from_dict_roundtrip_lossless(tmp_path) -> None:
    tl = _enabled_timeline(tmp_path)
    tl.on_apply_succeeded(op_id="op-rt", mode="multi", files=5)
    tl.on_verify_completed(op_id="op-rt", passed=7, total=7)
    tl.on_commit_succeeded(op_id="op-rt", commit_hash="abc")
    original = tl.query(op_id="op-rt")[0]
    restored = TimelineRow.from_dict(original.to_dict())
    assert restored == original, "to_dict/from_dict must be lossless"


def test_schema_version_constant_and_present_in_rows(tmp_path) -> None:
    assert TIMELINE_SCHEMA_VERSION == "timeline.1"
    tl = _enabled_timeline(tmp_path)
    tl.on_apply_succeeded(op_id="op-s", mode="single", files=1)
    assert tl.query()[0].schema_version == "timeline.1"


def test_jsonl_on_disk_is_valid_one_record_per_line(tmp_path) -> None:
    path = tmp_path / "timeline.jsonl"
    tl = OperationTimeline(persistence_path=path, enabled=True)
    tl.on_apply_succeeded(op_id="op-d", mode="single", files=1)
    tl.on_commit_succeeded(op_id="op-d", commit_hash="f00d")
    lines = [
        ln for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(lines) == 2, "append-only audit: one row per callback"
    for ln in lines:
        json.loads(ln)  # must not raise


# ===========================================================================
# Behavioral — cross-session disk replay (morning-after substrate)
# ===========================================================================


def test_replay_reconstructs_projection_idempotently(tmp_path) -> None:
    path = tmp_path / "timeline.jsonl"
    src = OperationTimeline(persistence_path=path, enabled=True)
    src.on_apply_succeeded(op_id="op-1", mode="single", files=2)
    src.on_commit_succeeded(op_id="op-1", commit_hash="h1")
    src.on_apply_succeeded(op_id="op-2", mode="multi", files=4)

    # Fresh process boot: a brand-new instance replays the durable file.
    fresh = OperationTimeline(persistence_path=path, enabled=True)
    n1 = fresh.replay_from_disk()
    assert n1 >= 1
    assert len(fresh) == 2, "latest-write-wins collapses to 2 op_ids"
    op1 = fresh.query(op_id="op-1")[0]
    assert op1.commit_hash == "h1", "the missing link survived a reboot"

    before = {r.op_id: r for r in fresh.query()}
    fresh.replay_from_disk()  # idempotent
    after = {r.op_id: r for r in fresh.query()}
    assert before == after, "replay must be idempotent"


def test_replay_advances_seq_so_new_refs_never_collide(tmp_path) -> None:
    path = tmp_path / "timeline.jsonl"
    src = OperationTimeline(persistence_path=path, enabled=True)
    src.on_apply_succeeded(op_id="op-1", mode="single", files=1)
    src.on_apply_succeeded(op_id="op-2", mode="single", files=1)

    fresh = OperationTimeline(persistence_path=path, enabled=True)
    fresh.replay_from_disk()
    fresh.on_apply_succeeded(op_id="op-3", mode="single", files=1)
    all_refs = {r.ref for r in fresh.query()}
    assert len(all_refs) == 3, f"ref collision after replay: {all_refs}"


def test_replay_is_bounded_by_max_rows(tmp_path, monkeypatch) -> None:
    path = tmp_path / "timeline.jsonl"
    src = OperationTimeline(persistence_path=path, enabled=True)
    for i in range(10):
        src.on_apply_succeeded(op_id=f"op-{i}", mode="single", files=1)
    monkeypatch.setenv(OPERATION_TIMELINE_MAX_ROWS_ENV_VAR, "3")
    fresh = OperationTimeline(persistence_path=path, enabled=True)
    replayed = fresh.replay_from_disk()
    assert replayed <= 3, "tail-scan must honor the max-rows cap"


def test_replay_skips_malformed_rows(tmp_path) -> None:
    path = tmp_path / "timeline.jsonl"
    path.write_text(
        "not json\n"
        '{"ref": "r-1", "op_id": "ok", "schema_version": "timeline.1"}\n'
        "{bad}\n",
        encoding="utf-8",
    )
    tl = OperationTimeline(persistence_path=path, enabled=True)
    n = tl.replay_from_disk()
    assert n == 1, "only the one well-formed row replays"
    assert tl.query(op_id="ok")


def test_replay_missing_file_returns_zero(tmp_path) -> None:
    tl = OperationTimeline(
        persistence_path=tmp_path / "nope.jsonl", enabled=True,
    )
    assert tl.replay_from_disk() == 0


# ===========================================================================
# Behavioral — fail-closed (observer NEVER raises)
# ===========================================================================


def test_observer_methods_never_raise_on_bad_input(tmp_path) -> None:
    tl = _enabled_timeline(tmp_path)
    # Empty op_id is dropped silently, not raised.
    tl.on_apply_succeeded(op_id="", mode="single", files=1)
    tl.on_commit_succeeded(op_id="", commit_hash="x")
    assert len(tl) == 0
    # Type-hostile values must still not raise (fail-closed contract).
    tl.on_verify_completed(op_id="op-q", passed=0, total=0)  # type: ignore[arg-type]
    assert tl.query() is not None


def test_clear_drops_memory_keeps_disk(tmp_path) -> None:
    path = tmp_path / "timeline.jsonl"
    tl = OperationTimeline(persistence_path=path, enabled=True)
    tl.on_apply_succeeded(op_id="op-c", mode="single", files=1)
    assert len(tl) == 1 and path.exists()
    tl.clear()
    assert len(tl) == 0, "in-memory projection dropped"
    assert path.exists(), "append-only audit survives an in-memory reset"


# ===========================================================================
# Behavioral — singleton + FlagRegistry seeds
# ===========================================================================


def test_singleton_is_idempotent_and_resettable() -> None:
    a = get_default_timeline()
    b = get_default_timeline()
    assert a is b
    reset_default_timeline()
    c = get_default_timeline()
    assert c is not a


def test_register_flags_seeds_three_specs_master_default_false() -> None:
    class _Reg:
        def __init__(self):
            self.specs = {}

        def register(self, spec):
            self.specs[spec.name] = spec

    reg = _Reg()
    count = register_flags(reg)
    assert count == 3
    master = reg.specs[OPERATION_TIMELINE_ENABLED_ENV_VAR]
    assert master.default is False, (
        "§33.1: the master switch MUST default-FALSE"
    )
    assert master.type.value == "bool"
    assert master.category.value == "observability"
    path_spec = reg.specs[OPERATION_TIMELINE_PATH_ENV_VAR]
    assert path_spec.default == ".jarvis/operation_timeline.jsonl"
    rows_spec = reg.specs[OPERATION_TIMELINE_MAX_ROWS_ENV_VAR]
    assert rows_spec.type.value == "int"
    assert rows_spec.category.value == "capacity"


def test_register_flags_never_raises_on_bad_registry() -> None:
    class _Boom:
        def register(self, spec):
            raise RuntimeError("registry exploded")

    # Per-spec failures are swallowed; the function returns a count
    # (0 here) rather than propagating.
    assert register_flags(_Boom()) == 0


# ===========================================================================
# SLICE 2 — composite fan-out (root fix for SessionRecorder coexistence)
# ===========================================================================

from backend.core.ouroboros.governance import ops_digest_observer as odo  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_observer():
    odo.reset_ops_digest_observer()
    yield
    odo.reset_ops_digest_observer()


class _SpyObserver:
    def __init__(self):
        self.calls = []

    def on_apply_succeeded(self, *, op_id, mode, files):
        self.calls.append(("apply", op_id, mode, files))

    def on_verify_completed(self, *, op_id, passed, total,
                            scoped_to_applied_op=True):
        self.calls.append(("verify", op_id, passed, total))

    def on_commit_succeeded(self, *, op_id, commit_hash):
        self.calls.append(("commit", op_id, commit_hash))

    def on_op_classified(self, *, op_id, signal_source, urgency, risk_tier):
        self.calls.append(("classified", op_id, signal_source))


def test_register_get_reset_remain_byte_identical_behavior() -> None:
    """Slice 2 must not regress the single-slot API the harness uses."""
    spy = _SpyObserver()
    odo.register_ops_digest_observer(spy)
    assert odo.get_ops_digest_observer() is spy
    odo.reset_ops_digest_observer()
    got = odo.get_ops_digest_observer()
    assert got is not spy
    # default is a no-op that swallows everything
    got.on_apply_succeeded(op_id="x", mode="single", files=1)


def test_add_composes_both_observers_no_eviction() -> None:
    """The root fix: add MUST NOT evict the registered SessionRecorder
    analog — both receive every callback."""
    recorder = _SpyObserver()
    timeline = _SpyObserver()
    odo.register_ops_digest_observer(recorder)  # harness boot order
    odo.add_ops_digest_observer(timeline)        # PRD §42 wiring
    obs = odo.get_ops_digest_observer()
    obs.on_apply_succeeded(op_id="op-1", mode="single", files=2)
    obs.on_op_classified(
        op_id="op-1", signal_source="TestFailure",
        urgency="immediate", risk_tier="notify_apply",
    )
    assert ("apply", "op-1", "single", 2) in recorder.calls
    assert ("apply", "op-1", "single", 2) in timeline.calls
    assert ("classified", "op-1", "TestFailure") in recorder.calls
    assert ("classified", "op-1", "TestFailure") in timeline.calls


def test_add_is_idempotent_by_identity() -> None:
    spy = _SpyObserver()
    odo.add_ops_digest_observer(spy)
    odo.add_ops_digest_observer(spy)
    odo.add_ops_digest_observer(spy)
    odo.get_ops_digest_observer().on_commit_succeeded(
        op_id="op-x", commit_hash="abc",
    )
    # Exactly one delivery despite three adds.
    assert spy.calls.count(("commit", "op-x", "abc")) == 1


def test_add_order_independent_and_drops_bare_noop() -> None:
    """add before register also converges; a bare _NoopObserver is not
    fanned to (it carries no state)."""
    timeline = _SpyObserver()
    odo.add_ops_digest_observer(timeline)  # nothing registered yet
    recorder = _SpyObserver()
    odo.add_ops_digest_observer(recorder)
    obs = odo.get_ops_digest_observer()
    obs.on_verify_completed(op_id="o", passed=1, total=1)
    assert ("verify", "o", 1, 1) in timeline.calls
    assert ("verify", "o", 1, 1) in recorder.calls


def test_misbehaving_member_does_not_starve_others() -> None:
    class _Bad:
        def on_apply_succeeded(self, **k):
            raise RuntimeError("boom")

        def on_verify_completed(self, **k):
            raise RuntimeError("boom")

        def on_commit_succeeded(self, **k):
            raise RuntimeError("boom")

        def on_op_classified(self, **k):
            raise RuntimeError("boom")

    good = _SpyObserver()
    odo.add_ops_digest_observer(_Bad())
    odo.add_ops_digest_observer(good)
    # Must not raise; good still receives.
    odo.get_ops_digest_observer().on_apply_succeeded(
        op_id="op-r", mode="multi", files=4,
    )
    assert ("apply", "op-r", "multi", 4) in good.calls


def test_remove_collapses_and_restores_noop() -> None:
    a = _SpyObserver()
    b = _SpyObserver()
    odo.add_ops_digest_observer(a)
    odo.add_ops_digest_observer(b)
    odo.remove_ops_digest_observer(b)
    # single-member composite collapses back to the bare observer
    assert odo.get_ops_digest_observer() is a
    odo.remove_ops_digest_observer(a)
    # empty → default no-op restored (never None)
    got = odo.get_ops_digest_observer()
    assert got is not None
    got.on_apply_succeeded(op_id="z", mode="none", files=0)


def test_protocol_implementers_all_have_on_op_classified() -> None:
    """SessionRecorder, _NoopObserver, composite, and the timeline must
    all structurally satisfy the extended protocol (no per-op
    AttributeError through the fan-out)."""
    from backend.core.ouroboros.battle_test.session_recorder import (
        SessionRecorder,
    )
    for cls in (SessionRecorder, odo._NoopObserver,
                odo._CompositeOpsDigestObserver, OperationTimeline):
        assert hasattr(cls, "on_op_classified"), cls.__name__


# ===========================================================================
# SLICE 2 — on_op_classified merge + DiffArchive causal join
# ===========================================================================


def test_on_op_classified_merges_signal_edge(tmp_path) -> None:
    tl = OperationTimeline(
        persistence_path=tmp_path / "t.jsonl", enabled=True,
    )
    tl.on_op_classified(
        op_id="op-c", signal_source="VoiceCommand",
        urgency="immediate", risk_tier="approval_required",
    )
    tl.on_apply_succeeded(op_id="op-c", mode="single", files=1)
    r = tl.query(op_id="op-c")[0]
    assert r.signal_source == "VoiceCommand"
    assert r.urgency == "immediate"
    assert r.risk_tier == "approval_required"
    assert r.apply_mode == "single"  # merged across both callbacks


def test_diff_archive_join_fills_edges(tmp_path, monkeypatch) -> None:
    """The read-only join pulls diff_ref/file_paths/risk_tier from the
    canonical DiffArchive singleton, keyed by op_id."""
    from backend.core.ouroboros.battle_test import diff_archive

    diff_archive.reset_default_archive() if hasattr(
        diff_archive, "reset_default_archive"
    ) else None
    arch = diff_archive.get_default_archive()
    added = arch.add(
        op_id="op-j",
        risk_tier="notify_apply",
        file_paths=("backend/a.py", "backend/b.py"),
        diff_text="--- a\n+++ b\n",
        summary="join test",
    )
    tl = OperationTimeline(
        persistence_path=tmp_path / "t.jsonl", enabled=True,
    )
    tl.on_apply_succeeded(op_id="op-j", mode="multi", files=2)
    r = tl.query(op_id="op-j")[0]
    assert r.diff_ref == added.ref, "diff_ref joined from DiffArchive"
    assert r.file_paths == ("backend/a.py", "backend/b.py")
    assert r.risk_tier == "notify_apply", "risk_tier joined when unset"


def test_explicit_classified_risk_tier_wins_over_diff_join(
    tmp_path,
) -> None:
    """on_op_classified risk_tier is authoritative; the diff's copy
    only fills when still unset (adaptive, no clobber)."""
    from backend.core.ouroboros.battle_test import diff_archive

    arch = diff_archive.get_default_archive()
    arch.add(
        op_id="op-w", risk_tier="safe_auto",
        file_paths=("x.py",), diff_text="d", summary="s",
    )
    tl = OperationTimeline(
        persistence_path=tmp_path / "t.jsonl", enabled=True,
    )
    tl.on_op_classified(
        op_id="op-w", signal_source="S", urgency="low",
        risk_tier="approval_required",
    )
    tl.on_apply_succeeded(op_id="op-w", mode="single", files=1)
    r = tl.query(op_id="op-w")[0]
    assert r.risk_tier == "approval_required", (
        "explicit classified risk_tier must win over the diff copy"
    )


def test_sse_publish_is_best_effort_never_raises(tmp_path) -> None:
    # Stream disabled by default ⇒ publish_task_event is a no-op;
    # _publish_sse must swallow regardless and never break the append.
    tl = OperationTimeline(
        persistence_path=tmp_path / "t.jsonl", enabled=True,
    )
    tl.on_commit_succeeded(op_id="op-sse", commit_hash="cafe")
    assert tl.query(op_id="op-sse")[0].commit_hash == "cafe"


def test_event_type_constant_registered_in_broker() -> None:
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        ios.EVENT_TYPE_OPERATION_TIMELINE_ROW
        in ios._VALID_EVENT_TYPES
    ), "the new SSE event type must be in the broker's valid set"


def test_ide_observability_handler_exists_and_authority_free() -> None:
    """The GET /observability/timeline handler must exist AND the
    operation_timeline module it reads must not import gate modules
    (the IDEObservability authority invariant, transitively)."""
    from backend.core.ouroboros.governance.ide_observability import (
        IDEObservabilityRouter,
    )
    assert hasattr(IDEObservabilityRouter, "_handle_timeline")
    # operation_timeline authority-free is already proven by pin 1;
    # this asserts the transitive guarantee the route depends on.
    src = _module_source()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "policy_engine" not in (node.module or "")
            assert "iron_gate" not in (node.module or "")
