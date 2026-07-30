"""The daemon cockpit must be able to see the state it produces.

`capability_handoff` measured `serpent_flow` at 7 of 18 cockpit hooks while
`ov attach` filled nearly all of them, and the DIRECTION of the gap was the
finding rather than the count:

    pending_apply   the daemon calls `note_pending` / `clear_pending`, so it is
                    the source of the NOTIFY_APPLY countdown — and never mounted
                    the strip that draws it.
    panic_arbiter   the daemon calls `arbitrate` from its own loop exception
                    handler, so it is where a task dies — and the FATAL overlay
                    only ever rendered on a remote client.

An operator at the daemon's own terminal could not see a gate this process was
running or a task it had just lost.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import cockpit_mount as cm


@pytest.fixture(autouse=True)
def _clean_state():
    """Both sources are process-global; a leaked panic or pending op would make
    these tests pass or fail depending on their order."""
    from backend.core.ouroboros.battle_test import panic_arbiter as pa
    try:
        pa.reset_for_tests()
    except Exception:  # noqa: BLE001
        pass
    yield
    try:
        pa.reset_for_tests()
    except Exception:  # noqa: BLE001
        pass


class TestTheDaemonSeesWhatItProduces:
    def test_a_pending_apply_reaches_the_daemons_own_strip(self):
        """The countdown for a gate THIS process is running."""
        from backend.core.ouroboros.battle_test import pending_apply as pd
        pd.note_pending("7759-86", delay_s=5.0, reason="NOTIFY_APPLY")
        try:
            rows = cm.daemon_pending_rows()
        finally:
            pd.clear_pending("7759-86")
        assert any("7759-86" in r for r in rows), (
            f"the daemon cannot see its own pending apply: {rows!r}")

    def test_a_panic_reaches_the_daemons_own_overlay(self):
        """The crash overlay on the process where the task actually died."""
        from backend.core.ouroboros.battle_test import panic_arbiter as pa
        try:
            raise RuntimeError("cannot import name 'get_active_harness'")
        except RuntimeError as exc:
            pa.report(exc, origin="doc_staleness.sweep")
        rows = cm.daemon_panic_rows()
        assert rows, "the daemon cannot see its own panic"
        assert any("get_active_harness" in r for r in rows)

    def test_the_panic_overlay_carries_its_traceback(self):
        """The bug this nearly shipped. `Panic` stores ``traceback_text``;
        `render_panic` reads ``"traceback"``. Passing the dataclass's own
        ``__dict__`` renders the alarm and silently drops the only part of it
        anybody needs."""
        from backend.core.ouroboros.battle_test import panic_arbiter as pa
        try:
            raise ValueError("boom in the sweep")
        except ValueError as exc:
            pa.report(exc, origin="unit")
        joined = "\n".join(cm.daemon_panic_rows())
        assert "Traceback" in joined or "ValueError" in joined, (
            "the overlay rendered without its traceback — the "
            "traceback_text/traceback rename was not adapted")

    def test_the_payload_keys_are_the_ones_the_renderer_reads(self):
        """Derived from `render_panic`'s own source rather than restated, so a
        rename on either side breaks this instead of quietly emptying a field."""
        import inspect
        import re

        from backend.core.ouroboros.battle_test import panic_arbiter as pa

        wanted = set(re.findall(r'\.get\(\s*"([a-z_]+)"',
                                inspect.getsource(pa.render_panic)))

        class _P:
            exc_type, message, origin = "E", "m", "o"
            traceback_text = "tb"

        payload = cm._panic_payload(_P())
        assert wanted <= set(payload), (
            f"render_panic reads {sorted(wanted)}; payload supplies "
            f"{sorted(payload)}")
        assert payload["traceback"] == "tb"

    def test_a_dict_payload_passes_through_untouched(self):
        """Already the wire shape. Re-mapping it would corrupt the client's own
        payload if this were ever reused on that side."""
        wire = {"exc_type": "E", "message": "m", "origin": "o",
                "traceback": "tb"}
        assert cm._panic_payload(wire) is wire

    def test_nothing_pending_and_nothing_crashed_draws_nothing(self):
        """A cockpit must cost zero rows when there is nothing to say. The panic
        overlay is the loudest thing it draws and must never be summoned by an
        empty record."""
        assert cm.daemon_pending_rows() == []
        assert cm.daemon_panic_rows() == []
        assert cm._panic_payload(None) is None


class TestTheSearchBarIsReachable:
    def test_the_mount_binds_the_key_that_opens_the_strip(self):
        """A strip with no key to open it is a row that can never appear.
        `search_rows` was mounted on the daemon while `extra_key_bindings` stayed
        unset, so `/` was bound nowhere and the bar was decoration."""
        mount = cm.build_daemon_mount(None)
        kb = mount.get("extra_key_bindings")
        assert kb is not None, "no key bindings — the search bar cannot be opened"
        keys = {str(getattr(k, "value", k)) for b in kb.bindings for k in b.keys}
        assert "/" in keys, f"the search key is not bound: {sorted(keys)}"

    def test_the_mount_supplies_the_strip_as_well_as_the_key(self):
        assert cm.build_daemon_mount(None).get("search_rows") is not None

    def test_the_bar_is_silent_until_it_is_opened(self):
        from backend.core.ouroboros.battle_test import transcript_hatches as th
        th.reset_search_for_tests()
        provider = cm.build_daemon_mount(None)["search_rows"]
        assert provider() == []


class TestTheLocalEntrance:
    """`LocalRewindClient`'s argument, applied to the hatches: one
    implementation, two entrances — never a second implementation."""

    def test_it_serves_as_both_ui_and_client(self):
        """The installer only ever asks for `flash`, `_narrate_verbose` and
        `send_input`; splitting them into two shims would invent a distinction
        the callee does not make."""
        shim = cm.LocalCockpitClient(send_input=lambda _t: None)
        assert hasattr(shim, "flash") and hasattr(shim, "send_input")
        assert shim._narrate_verbose is False

    def test_send_input_reaches_the_local_sink(self):
        seen = []
        cm.LocalCockpitClient(send_input=seen.append).send_input("/narrate on")
        assert seen == ["/narrate on"]

    def test_narrate_verbose_is_writable(self):
        """The hatch action toggles it, so a read-only property would break the
        binding rather than the binding breaking."""
        shim = cm.LocalCockpitClient(send_input=lambda _t: None)
        shim._narrate_verbose = True
        assert shim._narrate_verbose is True

    def test_a_missing_sink_never_raises(self):
        """A keybinding that can break the REPL it is bound in is worse than an
        unbound key."""
        cm.LocalCockpitClient(send_input=None).send_input("anything")
        cm.LocalCockpitClient(send_input=None).flash("a notice")

    def test_a_raising_sink_never_escapes(self):
        def _boom(_t):
            raise RuntimeError("dispatch died")
        cm.LocalCockpitClient(send_input=_boom).send_input("/verb")

    def test_flash_prefers_an_explicit_sink(self):
        seen = []
        cm.LocalCockpitClient(send_input=None, flash=seen.append).flash("hi")
        assert seen == ["hi"]


class TestTheMountContract:
    def test_every_hook_it_claims_is_a_real_cockpit_hook(self):
        """The mount would otherwise be free to invent names the builder does not
        accept, and the caller's `.get()` would silently pass None for a hook
        that never existed."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import bipartite_layout as bl

        fn = [
            n for n in ast.walk(ast.parse(inspect.getsource(bl).lstrip()))
            if isinstance(n, ast.FunctionDef)
            and n.name == "build_bipartite_application"
        ][0]
        accepted = {a.arg for a in fn.args.args} | {
            a.arg for a in fn.args.kwonlyargs}
        assert set(cm.build_daemon_mount(None)) <= accepted, (
            f"mount claims hooks the builder does not accept: "
            f"{sorted(set(cm.build_daemon_mount(None)) - accepted)}")

    def test_the_mount_never_raises_without_a_repl(self):
        assert isinstance(cm.build_daemon_mount(None), dict)

    def test_width_is_resolved_per_call_not_captured(self, monkeypatch):
        """Every renderer takes a width and the canvas draws with
        `wrap_lines=False`, so a row wrapped to a stale width is clipped rather
        than reflowed. A provider closing over its mount-time width would be
        correct until the first resize."""
        import shutil
        sizes = iter([shutil.os.terminal_size((200, 60)),
                      shutil.os.terminal_size((80, 24))])
        monkeypatch.setattr(shutil, "get_terminal_size",
                            lambda *_a, **_k: next(sizes))
        assert cm._terminal_width() == 200
        assert cm._terminal_width() == 80

    def test_a_degenerate_terminal_still_yields_a_usable_width(self, monkeypatch):
        import shutil
        monkeypatch.setattr(
            shutil, "get_terminal_size",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("no tty")))
        assert cm._terminal_width() >= 20


class TestTheDaemonIsWiredToTheMount:
    def test_serpent_flow_passes_the_mounted_hooks(self):
        """Behaviour would need a live daemon cockpit, so this pins the wiring
        structurally — and pins it by AST rather than substring, because the
        docstrings around this call site name these hooks in prose to explain the
        defect and a text search matches the explanation."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import serpent_flow as sf

        tree = ast.parse(inspect.getsource(sf))
        calls = [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and (getattr(n.func, "id", "") or getattr(n.func, "attr", ""))
            == "run_bipartite_repl"
        ]
        assert calls, "the daemon no longer mounts the bipartite cockpit"
        passed = {kw.arg for c in calls for kw in c.keywords if kw.arg}
        for hook in ("pending_rows", "panic_rows", "queue_rows", "search_rows",
                     "serpent_active", "toolbar", "extra_key_bindings"):
            assert hook in passed, (
                f"the daemon cockpit stopped mounting {hook} — it produces this "
                f"state and would be blind to it again")
