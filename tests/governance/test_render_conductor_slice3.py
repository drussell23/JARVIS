"""RenderConductor Slice 3 — typed primitives regression suite.

Pins the producer-side typed primitives ``ReasoningStream`` (Gap #1)
and ``FileRef`` (Gap #2). The primitives are testable in isolation —
producer-side adoption (providers.py / generate_runner.py wiring)
is correctly deferred to Slice 7's graduation step where the master
flag flip and producer migration co-land.

Strict directives validated:

  * No hardcoded values: every operator-tunable knob (master flag,
    OSC 8 hyperlink toggle) flows through ``FlagRegistry``. No raw
    color strings, no raw event-kind strings in primitive code —
    everything via the closed taxonomies from ``render_conductor``.
  * Closed-taxonomy field set: FileRef.{path, line, column, anchor}
    AST-pinned. Adding a field without coordinated to_metadata + pin
    update fails at boot.
  * Lifecycle totality: ReasoningStream.{start, on_token, end}
    AST-pinned. Producer migration cannot silently lose a method.
  * Defensive everywhere: every method returns instead of raising;
    every conductor-publish path tolerates None / missing conductor.
  * Master flag gate: when ``JARVIS_REASONING_STREAM_ENABLED`` is
    false (Slice 3 default), all producer-side methods are no-ops —
    legacy direct stream_renderer path is the active route.
  * No authority imports: render_primitives is descriptive only,
    AST-pinned mirror of render_conductor.

Covers:

  §A   FileRef construction + __post_init__ shape validation
  §B   FileRef render_plain across field permutations
  §C   FileRef render_hyperlink — OSC 8 escape sequence + flag gate
  §D   FileRef to_metadata / from_metadata round-trip
  §E   publish_file_ref helper — happy path + missing conductor
  §F   ReasoningStream lifecycle — start / on_token / end + idempotency
  §G   ReasoningStream — master flag gate + missing conductor
  §H   ReasoningStream — token-without-start tolerance
  §I   ReasoningStream — concurrent independent instances
  §J   get_reasoning_stream_callback — happy path + flag-off-None
  §K   register_flags — count, types, defaults
  §L   AST pins (5) self-validate green + tampering caught
  §M   Auto-discovery integration
"""
from __future__ import annotations

import ast
import threading
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance import render_conductor as rc
from backend.core.ouroboros.governance import render_primitives as rp


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_flag_env(monkeypatch: pytest.MonkeyPatch):
    for name in (
        "JARVIS_RENDER_CONDUCTOR_ENABLED",
        "JARVIS_RENDER_CONDUCTOR_THEME_NAME",
        "JARVIS_RENDER_CONDUCTOR_DENSITY_OVERRIDE",
        "JARVIS_RENDER_CONDUCTOR_POSTURE_DENSITY_MAP",
        "JARVIS_RENDER_CONDUCTOR_PALETTE_OVERRIDE",
        "JARVIS_REASONING_STREAM_ENABLED",
        "JARVIS_FILE_REF_HYPERLINK_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    rc.reset_render_conductor()


@pytest.fixture
def fresh_registry():
    from backend.core.ouroboros.governance import flag_registry as fr
    fr.reset_default_registry()
    reg = fr.ensure_seeded()
    yield reg
    fr.reset_default_registry()


class _RecordingBackend:
    name = "recorder"

    def __init__(self) -> None:
        self.events: List[Any] = []

    def notify(self, event: Any) -> None:
        self.events.append(event)

    def flush(self) -> None:
        pass

    def shutdown(self) -> None:
        pass


@pytest.fixture
def wired_conductor(monkeypatch: pytest.MonkeyPatch, fresh_registry):
    """Conductor with master flag on + a recording backend attached."""
    monkeypatch.setenv("JARVIS_RENDER_CONDUCTOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_REASONING_STREAM_ENABLED", "true")
    c = rc.RenderConductor()
    rec = _RecordingBackend()
    c.add_backend(rec)
    rc.register_render_conductor(c)
    yield c, rec
    rc.reset_render_conductor()


# ---------------------------------------------------------------------------
# §A — FileRef construction + shape validation
# ---------------------------------------------------------------------------


class TestFileRefConstruction:
    def test_minimal_path_only(self):
        fr = rp.FileRef(path="x.py")
        assert fr.path == "x.py"
        assert fr.line is None
        assert fr.column is None
        assert fr.anchor is None

    def test_full_fields(self):
        fr = rp.FileRef(
            path="backend/foo.py", line=42, column=3, anchor="MyClass.method",
        )
        assert fr.line == 42
        assert fr.column == 3
        assert fr.anchor == "MyClass.method"

    def test_frozen(self):
        fr = rp.FileRef(path="x.py")
        with pytest.raises(Exception):
            fr.path = "y.py"  # type: ignore[misc]

    def test_hashable(self):
        a = rp.FileRef(path="x.py", line=1)
        b = rp.FileRef(path="x.py", line=1)
        assert hash(a) == hash(b)
        assert a == b

    def test_empty_path_raises(self):
        with pytest.raises(ValueError, match="path"):
            rp.FileRef(path="")

    def test_whitespace_path_raises(self):
        with pytest.raises(ValueError, match="path"):
            rp.FileRef(path="   ")

    def test_non_string_path_raises(self):
        with pytest.raises(ValueError, match="path"):
            rp.FileRef(path=123)  # type: ignore[arg-type]

    def test_negative_line_raises(self):
        with pytest.raises(ValueError, match="line"):
            rp.FileRef(path="x.py", line=-1)

    def test_non_int_line_raises(self):
        with pytest.raises(ValueError, match="line"):
            rp.FileRef(path="x.py", line="42")  # type: ignore[arg-type]

    def test_negative_column_raises(self):
        with pytest.raises(ValueError, match="column"):
            rp.FileRef(path="x.py", column=-1)

    def test_non_string_anchor_raises(self):
        with pytest.raises(ValueError, match="anchor"):
            rp.FileRef(path="x.py", anchor=42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §B — render_plain across permutations
# ---------------------------------------------------------------------------


class TestFileRefRenderPlain:
    def test_path_only(self):
        assert rp.FileRef(path="x.py").render_plain() == "x.py"

    def test_path_line(self):
        assert rp.FileRef(path="x.py", line=42).render_plain() == "x.py:42"

    def test_path_line_column(self):
        out = rp.FileRef(path="x.py", line=42, column=3).render_plain()
        assert out == "x.py:42:3"

    def test_anchor_only(self):
        out = rp.FileRef(path="x.py", anchor="Foo").render_plain()
        assert out == "x.py#Foo"

    def test_anchor_with_line(self):
        out = rp.FileRef(
            path="x.py", line=42, anchor="Foo",
        ).render_plain()
        assert out == "x.py:42#Foo"

    def test_column_only_skipped(self):
        # column without line is a no-op (line None means whole file)
        out = rp.FileRef(path="x.py", column=3).render_plain()
        assert out == "x.py"


# ---------------------------------------------------------------------------
# §C — render_hyperlink: OSC 8 escape + flag gate
# ---------------------------------------------------------------------------


class TestFileRefRenderHyperlink:
    def test_default_emits_osc8(self, fresh_registry):
        out = rp.FileRef(path="x.py", line=42).render_hyperlink()
        # OSC 8 sequence: \x1b]8;;<uri>\x1b\\<text>\x1b]8;;\x1b\\
        assert out.startswith("\x1b]8;;file://")
        assert out.endswith("\x1b]8;;\x1b\\")
        assert "x.py:42" in out

    def test_includes_line_anchor(self, fresh_registry):
        out = rp.FileRef(path="x.py", line=42, column=3).render_hyperlink()
        assert "#L42C3" in out

    def test_path_only_no_anchor(self, fresh_registry):
        out = rp.FileRef(path="x.py").render_hyperlink()
        assert "#L" not in out

    def test_url_quotes_path(self, fresh_registry):
        out = rp.FileRef(path="path with space.py").render_hyperlink()
        # Quoted: "path%20with%20space.py"
        assert "path%20with%20space.py" in out

    def test_base_dir_absolutizes(self, fresh_registry):
        out = rp.FileRef(path="rel.py").render_hyperlink(base_dir="/abs")
        assert "file:///abs/rel.py" in out

    def test_base_dir_skipped_for_absolute_path(self, fresh_registry):
        out = rp.FileRef(path="/already/abs.py").render_hyperlink(
            base_dir="/different",
        )
        assert "/different" not in out

    def test_flag_off_falls_back_to_plain(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_FILE_REF_HYPERLINK_ENABLED", "false")
        out = rp.FileRef(path="x.py", line=42).render_hyperlink()
        assert out == "x.py:42"
        assert "\x1b" not in out


# ---------------------------------------------------------------------------
# §D — to_metadata / from_metadata round-trip
# ---------------------------------------------------------------------------


class TestFileRefMetadata:
    def test_to_metadata_includes_schema(self):
        md = rp.FileRef(path="x.py").to_metadata()
        assert md["schema_version"] == rp.RENDER_PRIMITIVES_SCHEMA_VERSION
        assert md["kind"] == "file_ref"

    def test_to_metadata_preserves_all_fields(self):
        fr = rp.FileRef(
            path="x.py", line=42, column=3, anchor="Foo",
        )
        md = fr.to_metadata()
        assert md["path"] == "x.py"
        assert md["line"] == 42
        assert md["column"] == 3
        assert md["anchor"] == "Foo"

    def test_round_trip(self):
        fr = rp.FileRef(path="a/b.py", line=10, column=5, anchor="X")
        md = fr.to_metadata()
        fr2 = rp.FileRef.from_metadata(md)
        assert fr == fr2

    def test_from_metadata_handles_missing_optional(self):
        fr = rp.FileRef.from_metadata({"path": "x.py"})
        assert fr is not None
        assert fr.path == "x.py"
        assert fr.line is None

    def test_from_metadata_returns_none_on_missing_path(self):
        assert rp.FileRef.from_metadata({"line": 1}) is None

    def test_from_metadata_returns_none_on_empty_path(self):
        assert rp.FileRef.from_metadata({"path": ""}) is None

    def test_from_metadata_strips_invalid_types(self):
        # line as string → coerced to None (defensive)
        fr = rp.FileRef.from_metadata({"path": "x.py", "line": "bad"})
        assert fr is not None
        assert fr.line is None

    def test_from_metadata_returns_none_on_garbage(self):
        assert rp.FileRef.from_metadata("not a dict") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# §E — publish_file_ref helper
# ---------------------------------------------------------------------------


class TestPublishFileRef:
    def test_publishes_event_when_conductor_wired(self, wired_conductor):
        c, rec = wired_conductor
        fr = rp.FileRef(path="x.py", line=42)
        ok = rp.publish_file_ref(fr, source_module="test", op_id="op-1")
        assert ok is True
        assert len(rec.events) == 1
        ev = rec.events[0]
        assert ev.kind is rc.EventKind.FILE_REF
        assert ev.region is rc.RegionKind.VIEWPORT
        assert ev.role is rc.ColorRole.METADATA
        assert ev.content == "x.py:42"
        assert ev.metadata["path"] == "x.py"
        assert ev.metadata["line"] == 42
        assert ev.op_id == "op-1"

    def test_returns_false_when_no_conductor(self, fresh_registry):
        rc.reset_render_conductor()
        fr = rp.FileRef(path="x.py")
        assert rp.publish_file_ref(fr, source_module="t") is False

    def test_explicit_region_role_overrides(self, wired_conductor):
        c, rec = wired_conductor
        fr = rp.FileRef(path="x.py")
        rp.publish_file_ref(
            fr, source_module="t",
            region=rc.RegionKind.PHASE_STREAM,
            role=rc.ColorRole.EMPHASIS,
        )
        assert rec.events[0].region is rc.RegionKind.PHASE_STREAM
        assert rec.events[0].role is rc.ColorRole.EMPHASIS

    def test_extra_metadata_merged(self, wired_conductor):
        c, rec = wired_conductor
        fr = rp.FileRef(path="x.py")
        rp.publish_file_ref(
            fr, source_module="t",
            extra_metadata={"reason": "diff_preview", "size": 1024},
        )
        md = rec.events[0].metadata
        assert md["path"] == "x.py"
        assert md["reason"] == "diff_preview"
        assert md["size"] == 1024


# ---------------------------------------------------------------------------
# §F — ReasoningStream lifecycle
# ---------------------------------------------------------------------------


class TestReasoningStreamLifecycle:
    def test_start_publishes_phase_begin(self, wired_conductor):
        c, rec = wired_conductor
        rs = rp.ReasoningStream()
        assert rs.start("op-1", "claude") is True
        assert len(rec.events) == 1
        assert rec.events[0].kind is rc.EventKind.PHASE_BEGIN
        assert rec.events[0].op_id == "op-1"
        assert rec.events[0].metadata.get("provider") == "claude"

    def test_on_token_publishes_reasoning_token(self, wired_conductor):
        c, rec = wired_conductor
        rs = rp.ReasoningStream()
        rs.start("op-1")
        rs.on_token("hello ")
        rs.on_token("world")
        token_events = [
            e for e in rec.events if e.kind is rc.EventKind.REASONING_TOKEN
        ]
        assert [e.content for e in token_events] == ["hello ", "world"]

    def test_end_publishes_phase_end(self, wired_conductor):
        c, rec = wired_conductor
        rs = rp.ReasoningStream()
        rs.start("op-1")
        rec.events.clear()
        rs.end()
        assert len(rec.events) == 1
        assert rec.events[0].kind is rc.EventKind.PHASE_END

    def test_state_cleared_after_end(self, wired_conductor):
        c, rec = wired_conductor
        rs = rp.ReasoningStream()
        rs.start("op-1", "claude")
        rs.end()
        assert rs.active is False
        assert rs.op_id == ""
        assert rs.provider == ""

    def test_double_start_auto_ends_prior(self, wired_conductor):
        c, rec = wired_conductor
        rs = rp.ReasoningStream()
        rs.start("op-1")
        rs.start("op-2")
        # PHASE_BEGIN, PHASE_END, PHASE_BEGIN
        kinds = [e.kind for e in rec.events]
        assert kinds.count(rc.EventKind.PHASE_BEGIN) == 2
        assert kinds.count(rc.EventKind.PHASE_END) == 1

    def test_double_end_idempotent(self, wired_conductor):
        c, rec = wired_conductor
        rs = rp.ReasoningStream()
        rs.start("op-1")
        rs.end()
        rec.events.clear()
        result = rs.end()
        assert result is False  # nothing published
        assert rec.events == []

    def test_empty_token_skipped(self, wired_conductor):
        c, rec = wired_conductor
        rs = rp.ReasoningStream()
        rs.start("op-1")
        rec.events.clear()
        assert rs.on_token("") is False
        assert rs.on_token(None) is False  # type: ignore[arg-type]
        assert rec.events == []


# ---------------------------------------------------------------------------
# §G — Master flag gate + missing conductor
# ---------------------------------------------------------------------------


class TestReasoningStreamGates:
    def test_master_flag_off_no_op_start(self, fresh_registry):
        # No env override → default false
        rs = rp.ReasoningStream()
        assert rs.start("op-1") is False
        assert rs.active is False

    def test_master_flag_off_no_op_token(self, fresh_registry):
        rs = rp.ReasoningStream()
        rs._active = True  # bypass gate to confirm token still gated
        assert rs.on_token("x") is False

    def test_master_flag_off_no_op_end(self, fresh_registry):
        rs = rp.ReasoningStream()
        rs._active = True
        assert rs.end() is False

    def test_master_on_but_no_conductor(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_REASONING_STREAM_ENABLED", "true")
        rc.reset_render_conductor()
        rs = rp.ReasoningStream()
        # start returns True (it sets active state) but publishes nothing
        # because there's no conductor — defensive degradation
        rs.start("op-1")
        # Then on_token returns False (no conductor to publish to)
        assert rs.on_token("x") is False


# ---------------------------------------------------------------------------
# §H — Token-without-start tolerance
# ---------------------------------------------------------------------------


class TestReasoningStreamTokenWithoutStart:
    def test_token_without_explicit_start(self, wired_conductor):
        c, rec = wired_conductor
        rs = rp.ReasoningStream()
        rs.on_token("orphan-token")
        assert rs.active is True
        assert any(
            e.kind is rc.EventKind.REASONING_TOKEN
            and e.content == "orphan-token"
            for e in rec.events
        )

    def test_end_after_token_without_start_works(self, wired_conductor):
        c, rec = wired_conductor
        rs = rp.ReasoningStream()
        rs.on_token("x")
        assert rs.end() is True


# ---------------------------------------------------------------------------
# §I — Concurrent independent instances
# ---------------------------------------------------------------------------


class TestReasoningStreamConcurrency:
    def test_two_streams_independent_op_ids(self, wired_conductor):
        c, rec = wired_conductor
        rs_a = rp.ReasoningStream()
        rs_b = rp.ReasoningStream()
        rs_a.start("op-A", "claude")
        rs_b.start("op-B", "dw")
        assert rs_a.op_id == "op-A"
        assert rs_b.op_id == "op-B"
        rs_a.on_token("a")
        rs_b.on_token("b")
        op_ids = {e.op_id for e in rec.events
                  if e.kind is rc.EventKind.REASONING_TOKEN}
        assert op_ids == {"op-A", "op-B"}

    def test_concurrent_publish_thread_safe(self, wired_conductor):
        c, rec = wired_conductor
        rs = rp.ReasoningStream()
        rs.start("op-1")
        N = 50

        def _emit() -> None:
            rs.on_token("x")

        threads = [threading.Thread(target=_emit) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        token_count = sum(
            1 for e in rec.events
            if e.kind is rc.EventKind.REASONING_TOKEN
        )
        assert token_count == N


# ---------------------------------------------------------------------------
# §J — get_reasoning_stream_callback factory
# ---------------------------------------------------------------------------


class TestReasoningStreamCallback:
    def test_returns_none_when_disabled(self, fresh_registry):
        cb = rp.get_reasoning_stream_callback("op-1")
        assert cb is None

    def test_returns_none_when_no_conductor(
        self, monkeypatch: pytest.MonkeyPatch, fresh_registry,
    ):
        monkeypatch.setenv("JARVIS_REASONING_STREAM_ENABLED", "true")
        rc.reset_render_conductor()
        assert rp.get_reasoning_stream_callback("op-1") is None

    def test_callback_publishes_tokens(self, wired_conductor):
        c, rec = wired_conductor
        cb = rp.get_reasoning_stream_callback("op-1", "claude")
        assert cb is not None
        cb("hello ")
        cb("world")
        tokens = [
            e.content for e in rec.events
            if e.kind is rc.EventKind.REASONING_TOKEN
        ]
        assert tokens == ["hello ", "world"]

    def test_end_callback_finalizes(self, wired_conductor):
        c, rec = wired_conductor
        cb = rp.get_reasoning_stream_callback("op-1")
        assert cb is not None
        cb("x")
        rec.events.clear()
        cb.end_callback()  # type: ignore[attr-defined]
        assert len(rec.events) == 1
        assert rec.events[0].kind is rc.EventKind.PHASE_END

    def test_callback_swallows_token_exception(
        self, monkeypatch: pytest.MonkeyPatch, wired_conductor,
    ):
        c, rec = wired_conductor
        cb = rp.get_reasoning_stream_callback("op-1")
        assert cb is not None
        # Patch publish to raise — callback must not propagate
        with pytest.MonkeyPatch.context() as mp:
            def _boom(*a, **k):
                raise RuntimeError("publish boom")
            mp.setattr(c, "publish", _boom)
            cb("x")  # should not raise


# ---------------------------------------------------------------------------
# §K — register_flags
# ---------------------------------------------------------------------------


class TestRegisterFlags:
    def test_returns_two(self):
        from backend.core.ouroboros.governance import flag_registry as fr
        reg = fr.FlagRegistry()
        count = rp.register_flags(reg)
        assert count == 2

    def test_master_flag_default_false(self):
        from backend.core.ouroboros.governance import flag_registry as fr
        reg = fr.FlagRegistry()
        rp.register_flags(reg)
        spec = reg.get_spec("JARVIS_REASONING_STREAM_ENABLED")
        assert spec is not None
        assert spec.default is False
        assert spec.category is fr.Category.SAFETY

    def test_hyperlink_flag_default_true(self):
        from backend.core.ouroboros.governance import flag_registry as fr
        reg = fr.FlagRegistry()
        rp.register_flags(reg)
        spec = reg.get_spec("JARVIS_FILE_REF_HYPERLINK_ENABLED")
        assert spec is not None
        assert spec.default is True


# ---------------------------------------------------------------------------
# §L — AST pins self-validate green + tampering caught
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def slice3_pins() -> list:
    return list(rp.register_shipped_invariants())


class TestSlice3ASTPinsClean:
    def test_five_pins_registered(self, slice3_pins):
        assert len(slice3_pins) == 5
        names = {i.invariant_name for i in slice3_pins}
        assert names == {
            "render_primitives_no_rich_import",
            "render_primitives_no_authority_imports",
            "render_primitives_fileref_closed_taxonomy",
            "render_primitives_reasoning_stream_lifecycle",
            "render_primitives_discovery_symbols_present",
        }

    @pytest.fixture(scope="class")
    def real_ast(self) -> tuple:
        import inspect
        src = inspect.getsource(rp)
        return ast.parse(src), src

    def test_no_rich_import_clean(self, slice3_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice3_pins
                   if p.invariant_name == "render_primitives_no_rich_import")
        assert pin.validate(tree, src) == ()

    def test_no_authority_imports_clean(self, slice3_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice3_pins
                   if p.invariant_name ==
                   "render_primitives_no_authority_imports")
        assert pin.validate(tree, src) == ()

    def test_fileref_closed_taxonomy_clean(self, slice3_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice3_pins
                   if p.invariant_name ==
                   "render_primitives_fileref_closed_taxonomy")
        assert pin.validate(tree, src) == ()

    def test_reasoning_stream_lifecycle_clean(self, slice3_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice3_pins
                   if p.invariant_name ==
                   "render_primitives_reasoning_stream_lifecycle")
        assert pin.validate(tree, src) == ()

    def test_discovery_symbols_clean(self, slice3_pins, real_ast):
        tree, src = real_ast
        pin = next(p for p in slice3_pins
                   if p.invariant_name ==
                   "render_primitives_discovery_symbols_present")
        assert pin.validate(tree, src) == ()


class TestSlice3ASTPinsCatchTampering:
    def test_rich_import_caught(self, slice3_pins):
        tampered = ast.parse("from rich.markup import escape\n")
        pin = next(p for p in slice3_pins
                   if p.invariant_name == "render_primitives_no_rich_import")
        violations = pin.validate(tampered, "")
        assert any("rich" in v for v in violations)

    def test_authority_import_caught(self, slice3_pins):
        tampered = ast.parse(
            "from backend.core.ouroboros.governance.policy import x\n"
        )
        pin = next(p for p in slice3_pins
                   if p.invariant_name ==
                   "render_primitives_no_authority_imports")
        violations = pin.validate(tampered, "")
        assert any("policy" in v for v in violations)

    def test_added_fileref_field_caught(self, slice3_pins):
        tampered_src = (
            "from dataclasses import dataclass\n"
            "from typing import Optional\n"
            "@dataclass(frozen=True)\n"
            "class FileRef:\n"
            "    path: str\n"
            "    line: Optional[int] = None\n"
            "    column: Optional[int] = None\n"
            "    anchor: Optional[str] = None\n"
            "    extra_field: Optional[str] = None\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice3_pins
                   if p.invariant_name ==
                   "render_primitives_fileref_closed_taxonomy")
        violations = pin.validate(tampered, tampered_src)
        assert violations

    def test_removed_fileref_field_caught(self, slice3_pins):
        tampered_src = (
            "from dataclasses import dataclass\n"
            "@dataclass(frozen=True)\n"
            "class FileRef:\n"
            "    path: str\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice3_pins
                   if p.invariant_name ==
                   "render_primitives_fileref_closed_taxonomy")
        violations = pin.validate(tampered, tampered_src)
        assert violations

    def test_missing_lifecycle_method_caught(self, slice3_pins):
        tampered_src = (
            "class ReasoningStream:\n"
            "    def start(self, op_id, provider=''): pass\n"
            "    # on_token + end intentionally missing\n"
        )
        tampered = ast.parse(tampered_src)
        pin = next(p for p in slice3_pins
                   if p.invariant_name ==
                   "render_primitives_reasoning_stream_lifecycle")
        violations = pin.validate(tampered, tampered_src)
        assert violations
        assert any("on_token" in v or "end" in v for v in violations)

    def test_missing_discovery_symbol_caught(self, slice3_pins):
        tampered = ast.parse("def something_else(): pass\n")
        pin = next(p for p in slice3_pins
                   if p.invariant_name ==
                   "render_primitives_discovery_symbols_present")
        violations = pin.validate(tampered, "")
        assert violations


# ---------------------------------------------------------------------------
# §M — Auto-discovery integration
# ---------------------------------------------------------------------------


class TestAutoDiscoveryIntegration:
    def test_flag_registry_picks_up_primitives(self, fresh_registry):
        names = [s.name for s in fresh_registry.list_all()]
        assert "JARVIS_REASONING_STREAM_ENABLED" in names
        assert "JARVIS_FILE_REF_HYPERLINK_ENABLED" in names

    def test_shipped_invariants_includes_slice3_pins(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        names = {
            i.invariant_name for i in sci.list_shipped_code_invariants()
        }
        for expected in (
            "render_primitives_no_rich_import",
            "render_primitives_no_authority_imports",
            "render_primitives_fileref_closed_taxonomy",
            "render_primitives_reasoning_stream_lifecycle",
            "render_primitives_discovery_symbols_present",
        ):
            assert expected in names

    def test_validate_all_no_slice3_violations(self):
        from backend.core.ouroboros.governance.meta import (
            shipped_code_invariants as sci,
        )
        results = sci.validate_all()
        slice3_failures = [
            r for r in results
            if r.invariant_name.startswith("render_primitives_")
        ]
        assert slice3_failures == [], (
            f"Slice 3 pins reporting violations: "
            f"{[r.to_dict() for r in slice3_failures]}"
        )
