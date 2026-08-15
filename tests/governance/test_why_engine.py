"""Regression spine for `/why` — the causal account (PRD §27.5)."""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.battle_test import transcript_spine as ts
from backend.core.ouroboros.governance import why_engine as we
from backend.core.ouroboros.governance import why_repl as wr


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("JARVIS_WHY_LINEAGE_DEPTH", "JARVIS_WHY_BAND_ITEMS",
              "JARVIS_OUROBOROS_SESSION_DIR"):
        monkeypatch.delenv(k, raising=False)
    ts.reset_default_spine()
    we.set_live_source(None)
    yield
    ts.reset_default_spine()
    we.set_live_source(None)


def _seed(op="op-1"):
    sp = ts.get_default_spine()
    sp.append("op_block", "o-1", {"summary": "TodoScanner fired"}, op_id=op)
    sp.append("narrative", "n-1", {"text": "retry path is dead"}, op_id=op)
    sp.append("tool_render", "t-1", {"summary": "get_callers -> 0"}, op_id=op)
    sp.append("diff", "d-1", {"summary": "remove guard"}, op_id=op)
    return sp


# -- shape -----------------------------------------------------------------


def test_all_four_bands_always_render_in_a_fixed_order():
    """A band that vanished when empty would make 'it had no context' and
    'we did not record the context' render identically."""
    ts.get_default_spine().append("op_block", "o-1", {}, op_id="op-1")
    e = we.explain("o-1")
    assert [b.name for b in e.bands] == list(we.BAND_ORDER)


def test_an_empty_band_says_unknown_rather_than_disappearing():
    ts.get_default_spine().append("op_block", "o-1", {}, op_id="op-1")
    e = we.explain("o-1")
    ctx = e.band("context")
    assert ctx.certainty is we.Certainty.UNKNOWN
    assert "unknown" in wr.render(e).lower()


def test_records_route_to_the_band_their_vocabulary_answers():
    _seed()
    e = we.explain("o-1")
    assert "o-1" in e.band("trigger").items[0]
    assert "n-1" in e.band("context").items[0]
    assert "t-1" in e.band("logic").items[0]
    assert any("d-1" in i for i in e.band("action").items)


def test_a_bare_op_id_resolves_as_well_as_a_ref():
    """An operator reading a log has the op id; one reading the deck has the
    ref. Demanding they know which is the interface asking for a lookup."""
    _seed(op="op-xyz")
    assert we.explain("op-xyz").op_id == "op-xyz"


def test_an_unresolvable_ref_is_refused_not_answered_empty():
    """An empty account reads as 'it did nothing', which is a different
    claim from 'we cannot find it'."""
    with pytest.raises(we.RefNotFound):
        we.explain("o-999")
    out = wr.dispatch_why_command("/why o-999")
    assert out.ok is False and "resolves to nothing" in out.text


def test_no_model_call_is_made():
    """It must answer when every provider lane is dry, and it cannot invent
    a rationale."""
    import pathlib
    src = pathlib.Path(we.__file__).read_text(encoding="utf-8")
    for banned in ("providers", "candidate_generator", "anthropic",
                   "generate("):
        assert banned not in src


def test_the_engine_adds_no_fourth_parser():
    """spine owns records, timeline owns the fold, transcript_log owns
    recovery. A second parser is a second opinion about the same bytes."""
    import pathlib
    src = pathlib.Path(we.__file__).read_text(encoding="utf-8")
    assert "json.loads" not in src and "open(" not in src


# -- the in-flight race ----------------------------------------------------


def test_an_in_flight_op_is_joined_from_memory_not_reported_missing():
    """The operator asks about the thing they are watching — by definition
    the thing least likely to have reached disk."""
    we.set_live_source(lambda: {"op-live": {"phase": "GENERATE"}})
    e = we.explain("op-live")
    assert e.in_flight is True
    assert any("IN FLIGHT" in i for i in e.band("logic").items)


def test_in_flight_state_is_labelled_in_the_render():
    we.set_live_source(lambda: {"op-live": {"phase": "VALIDATE"}})
    assert "IN FLIGHT" in wr.render(we.explain("op-live"))


def test_a_live_source_that_raises_degrades_to_disk_only():
    def _boom():
        raise RuntimeError("registry locked")
    we.set_live_source(_boom)
    _seed()
    e = we.explain("o-1")
    assert e.in_flight is False
    assert e.band("trigger").certainty is we.Certainty.OBSERVED


def test_without_a_live_source_disk_still_answers():
    _seed()
    assert we.explain("o-1").band("trigger").certainty is we.Certainty.OBSERVED


# -- the tombstone gap -----------------------------------------------------


def test_an_evicted_context_renders_a_tombstone_not_a_crash(monkeypatch):
    """The originating context was purged by retention. The join must keep
    mapping the surviving downstream nodes."""
    sp = ts.get_default_spine()
    sp.append("op_block", "o-1", {"summary": "trigger"}, op_id="op-1")
    monkeypatch.setattr(type(sp), "was_evicted", lambda self, ref: True)
    monkeypatch.setattr(type(sp), "evicted_count", 3, raising=False)
    e = we.explain("o-1")
    ctx = e.band("context")
    assert ctx.certainty is we.Certainty.TOMBSTONE
    assert "tombstone" in wr.render(e).lower()
    # Downstream survives.
    assert e.band("trigger").certainty is we.Certainty.OBSERVED


def test_a_tombstone_is_not_collapsed_into_unknown():
    """'We discarded this on a policy you control' and 'this was never
    recorded' call for different actions."""
    assert we.Certainty.TOMBSTONE != we.Certainty.UNKNOWN


def test_a_partial_account_says_so_loudly():
    """A partial account can be read as complete, and an operator acting on
    'it did nothing else' when records were evicted is the failure."""
    sp = ts.get_default_spine()
    sp.append("op_block", "o-1", {}, op_id="op-1")
    e = we.Explanation(ref="o-1", op_id="op-1", bands=we.explain("o-1").bands,
                       partial=True)
    assert "PARTIAL" in wr.render(e)


def test_a_corrupt_log_reports_where_it_stops(monkeypatch, tmp_path):
    """recover_log already returns a typed stop_reason and stop_frame — the
    engine reports rather than guesses."""
    class _Result:
        clean = False
        stop_reason = "crc_mismatch"
        stop_frame = 42
        trailing_bytes = 118
    monkeypatch.setenv("JARVIS_OUROBOROS_SESSION_DIR", str(tmp_path))
    monkeypatch.setattr(
        "backend.core.ouroboros.battle_test.transcript_log.recover_log",
        lambda *a, **k: _Result())
    sp = ts.get_default_spine()
    sp.append("op_block", "o-1", {}, op_id="op-1")
    e = we.explain("o-1")
    assert "frame 42" in e.loss_point and "crc_mismatch" in e.loss_point
    assert "DATA LOSS" in wr.render(e)


def test_a_clean_log_reports_no_loss(monkeypatch, tmp_path):
    class _Result:
        clean = True
    monkeypatch.setenv("JARVIS_OUROBOROS_SESSION_DIR", str(tmp_path))
    monkeypatch.setattr(
        "backend.core.ouroboros.battle_test.transcript_log.recover_log",
        lambda *a, **k: _Result())
    ts.get_default_spine().append("op_block", "o-1", {}, op_id="op-1")
    assert we.explain("o-1").loss_point == ""


# -- the chain reaction ----------------------------------------------------


def test_lineage_defaults_to_one_hop_not_the_whole_ancestry():
    """An explanation that unrolled eight ancestors into the terminal would
    be skipped, and a skipped explanation is worse than a short one."""
    we.set_live_source(lambda: {
        "d": {"parent_op_id": "c"}, "c": {"parent_op_id": "b"},
        "b": {"parent_op_id": "a"}, "a": {},
    })
    e = we.explain("d")
    assert e.lineage == ("c",)
    assert e.lineage_truncated is True


def test_full_walks_the_whole_chain():
    we.set_live_source(lambda: {
        "d": {"parent_op_id": "c"}, "c": {"parent_op_id": "b"},
        "b": {"parent_op_id": "a"}, "a": {},
    })
    e = we.explain("d", depth=64)
    assert e.lineage == ("c", "b", "a")
    assert e.lineage_truncated is False


def test_the_render_offers_the_drill_down_path():
    we.set_live_source(lambda: {"d": {"parent_op_id": "c"},
                                "c": {"parent_op_id": "b"}, "b": {}})
    text = wr.render(we.explain("d"))
    assert "CAUSED BY" in text and "--full" in text


def test_a_root_op_reports_no_lineage():
    _seed()
    assert we.explain("o-1").lineage == ()


def test_a_cycle_terminates_rather_than_hanging():
    """An op-id reused across sessions could otherwise loop forever."""
    we.set_live_source(lambda: {"a": {"parent_op_id": "b"},
                                "b": {"parent_op_id": "a"}})
    e = we.explain("a", depth=64)
    assert len(e.lineage) <= 2


# -- the surface -----------------------------------------------------------


def test_the_verb_is_auto_discoverable():
    assert callable(wr.dispatch_why_command) and "why" in wr.__verb_help__


def test_help_is_reachable():
    assert "causal" in wr.dispatch_why_command("/why help").text.lower()


def test_a_bare_why_prints_help_rather_than_failing():
    assert wr.dispatch_why_command("/why").ok is True


def test_the_render_uses_the_decks_own_glyphs():
    """One more op-scoped block in a transcript full of them, not a new
    visual language."""
    _seed()
    text = wr.render(we.explain("o-1"))
    assert text.startswith("⏺") and "⎿" in text


def test_dispatch_never_raises_on_a_broken_engine(monkeypatch):
    monkeypatch.setattr(we, "explain",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    out = wr.dispatch_why_command("/why o-1")
    assert out.ok is False and "could not build" in out.text


def test_the_explanation_is_serialisable():
    _seed()
    json.dumps(we.explain("o-1").to_dict())


# -- the boot binding: DI, not a singleton ---------------------------------


class _FakeGLS:
    """Stands in for GovernedLoopService: an `_active_ops` set, nothing more."""

    def __init__(self, ops=()):
        self._active_ops = set(ops)


def _code_of(module, *names):
    """Executable source only — docstrings stripped.

    Prose that EXPLAINS a mechanism reads identically to the mechanism when
    grepped, so a docstring saying "we do not use os.access" satisfies a
    search for os.access. Assertions here must see code.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if names and node.name not in names:
            continue
        body = list(node.body)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0], "value", None), ast.Constant)
                and isinstance(body[0].value.value, str)):
            body = body[1:]
        out.extend(ast.unparse(stmt) for stmt in body)
    return "\n".join(out)


def test_a_set_registry_is_adapted_without_the_engine_knowing_its_shape():
    svc = _FakeGLS({"op-a", "op-b"})
    assert we.weak_live_source(svc)() == {"op-a": {}, "op-b": {}}


def test_a_mapping_registry_passes_through_unchanged():
    svc = _FakeGLS()
    svc._active_ops = {"op-a": {"phase": "GENERATE"}}
    assert we.weak_live_source(svc)()["op-a"]["phase"] == "GENERATE"


def test_a_collected_service_yields_disk_only_not_a_frozen_snapshot():
    import gc

    svc = _FakeGLS({"op-a"})
    read = we.weak_live_source(svc)
    assert read() == {"op-a": {}}
    del svc
    gc.collect()
    # The dangling pointer: a strong closure would still answer {"op-a": {}}
    # for a service nobody else can reach.
    assert read() == {}


def test_the_reader_is_weak_so_binding_does_not_prolong_the_service():
    import gc
    import weakref

    svc = _FakeGLS()
    ref = weakref.ref(svc)
    we.set_live_source(we.weak_live_source(svc))
    del svc
    gc.collect()
    assert ref() is None


def test_a_missing_registry_degrades_rather_than_raising():
    assert we.weak_live_source(_FakeGLS(), "_nope")() == {}


def test_an_unweakrefable_service_is_refused_not_pinned_strongly():
    """Falling back to a strong ref would silently restore the bug."""
    assert we.weak_live_source(object())() == {}


def test_a_stopping_instance_cannot_unbind_its_successor():
    old, new = _FakeGLS({"old"}), _FakeGLS({"new"})
    old_read, new_read = we.weak_live_source(old), we.weak_live_source(new)
    we.set_live_source(old_read)
    we.set_live_source(new_read)          # successor binds first
    assert we.release_live_source(old_read) is False   # predecessor stops
    assert we._live_ops() == {"new": {}}   # successor still visible


def test_an_instance_releases_the_reader_it_actually_lent():
    svc = _FakeGLS({"op-a"})
    read = we.weak_live_source(svc)
    we.set_live_source(read)
    assert we.release_live_source(read) is True
    assert we._live_ops() == {}


def test_releasing_twice_is_not_an_error():
    read = we.weak_live_source(_FakeGLS())
    we.set_live_source(read)
    assert we.release_live_source(read) is True
    assert we.release_live_source(read) is False


def test_why_survives_being_spammed_while_the_binding_churns():
    """Boot-time thread safety: bind/release racing reads, no NoneType."""
    import threading

    svc = _FakeGLS({"op-a"})
    stop = threading.Event()
    errors = []

    def churn():
        while not stop.is_set():
            read = we.weak_live_source(svc)
            we.set_live_source(read)
            we.release_live_source(read)

    def ask():
        while not stop.is_set():
            try:
                we._live_ops()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=churn), threading.Thread(target=ask),
               threading.Thread(target=ask)]
    for t in threads:
        t.start()
    stop.wait(0.4)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert not errors


def test_a_registry_mutating_under_the_read_does_not_drop_the_live_half():
    """A busy organism is exactly when `/why` is asked."""
    import threading

    svc = _FakeGLS({f"op-{i}" for i in range(200)})
    stop = threading.Event()
    read = we.weak_live_source(svc)
    errors, empties = [], []

    def mutate():
        i = 0
        while not stop.is_set():
            svc._active_ops.add(f"x-{i}")
            svc._active_ops.discard(f"x-{i - 50}")
            i += 1

    def sample():
        while not stop.is_set():
            try:
                if not read():
                    empties.append(1)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=mutate), threading.Thread(target=sample)]
    for t in threads:
        t.start()
    stop.wait(0.4)
    stop.set()
    for t in threads:
        t.join(timeout=5)
    assert not errors and not empties


def test_the_engine_never_reaches_into_the_orchestrator():
    """DI direction: the projection must not import the service."""
    import inspect

    src = inspect.getsource(we)
    assert "governed_loop_service" not in src
    assert "orchestrator" not in _code_of(we)


def test_the_boot_seam_binds_and_both_teardowns_release():
    """The wired-but-inert trap: a binding nobody calls is not a binding."""
    from backend.core.ouroboros.governance import governed_loop_service as gls

    code = _code_of(gls)
    assert "set_live_source" in code, "boot seam never binds /why"
    assert "weak_live_source" in code, "boot seam binds a STRONG reader"

    for seam in ("stop", "_teardown_partial"):
        assert "_release_why_live_source" in _code_of(gls, seam), (
            f"{seam}() leaves the /why reader dangling")
    assert "release_live_source" in _code_of(gls, "_release_why_live_source")
