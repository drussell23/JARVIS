"""One authority for "how much voice", and the ways it must not fail.

`/narrate` pushed four `os.environ` writes at a hardcoded list of producers.
Two of the four keys had zero readers anywhere in the repository, and the
Moltbook agora — thirteen personas with an autonomous reaction engine — read
none of them, so `/narrate off` silenced two voices and left the loudest one
talking.

These tests pin the inverted design: producers register and PULL, and the
failure modes of a dial (muting the unknown, freezing itself, editing the
archive) are each held shut.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from backend.core.ouroboros.ui import narrative_density as nd

_LEGACY_FLAGS = (
    "JARVIS_NARRATIVE_INTENT_ENABLED",
    "JARVIS_TOOL_PREAMBLE_FALLBACK_ENABLED",
    "JARVIS_NARRATIVE_THINKING_VERBOSE",
    "JARVIS_MOLTBOOK_CONVERSE_ENABLED",
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """No test may read the developer's real environment.

    A dial whose resolution depends on ambient env is exactly the thing
    under test; letting it leak in would make these assertions describe
    the machine rather than the code.
    """
    import os
    for flag in (*_LEGACY_FLAGS, nd.DENSITY_ENV_VAR):
        monkeypatch.delenv(flag, raising=False)
    yield
    # `monkeypatch.delenv` on a key that was ABSENT records nothing to
    # restore, and `set_density` then writes the key directly — so without
    # this the dial leaks out of the suite. It did: it left density=off in
    # the process and a later file's `intent_master_flag_default_on`
    # failed, correctly, on a value this file had set.
    for flag in (*_LEGACY_FLAGS, nd.DENSITY_ENV_VAR):
        os.environ.pop(flag, None)
    for name in ("t.alarm", "t.legacy", "t.presence", "t.typo",
                 "t.thresh", "t.reason"):
        nd.default_registry()._voices.pop(name, None)  # noqa: SLF001


class TestTheLadder:
    def test_it_is_ordinal(self):
        """Thresholds must compare. An unordered enum would push the
        comparison back into every producer — how four hardcoded flags
        happened."""
        assert nd.Density.OFF < nd.Density.PREAMBLES < nd.Density.ON \
            < nd.Density.VERBOSE

    @pytest.mark.parametrize("raw,expect", [
        ("off", nd.Density.OFF), ("VERBOSE", nd.Density.VERBOSE),
        (2, nd.Density.ON), ("3", nd.Density.VERBOSE),
        (nd.Density.OFF, nd.Density.OFF),
    ])
    def test_it_parses_what_operators_actually_type(self, raw, expect):
        assert nd.coerce_density(raw) is expect

    def test_junk_resolves_to_the_DEFAULT_not_to_silence(self):
        """A typo in a config must not mute the organism. Silence is
        indistinguishable from a healthy quiet system, so the operator
        would get no signal that their value was junk."""
        assert nd.coerce_density("lowd") is nd.DEFAULT_DENSITY
        assert nd.coerce_density(None) is nd.DEFAULT_DENSITY
        assert nd.coerce_density("") is nd.DEFAULT_DENSITY
        assert nd.DEFAULT_DENSITY is not nd.Density.OFF

    def test_out_of_range_ints_clamp(self):
        assert nd.coerce_density(99) is nd.Density.VERBOSE
        assert nd.coerce_density(-5) is nd.Density.OFF


class TestTheFourRules:
    def test_1_an_UNREGISTERED_voice_is_heard(self):
        """Fail OPEN. A dial that mutes what it does not recognise silently
        deletes the output of any subsystem that forgot to register — the
        very failure being fixed, reintroduced from the other side."""
        nd.set_density("off")
        v = nd.permits("some.voice.nobody.declared")
        assert v.heard is True
        assert v.reason == "unregistered"

    def test_2_an_EXEMPT_voice_outranks_even_an_explicit_flag(self, monkeypatch):
        """A ⚔ is REVIEW contesting GENERATE: the system reporting that its
        own components disagree. No verbosity preference outranks that."""
        reg = nd.default_registry()
        reg.register("t.alarm", nd.Density.VERBOSE, exempt=True,
                     legacy_flag="JARVIS_T_ALARM")
        monkeypatch.setenv("JARVIS_T_ALARM", "false")
        nd.set_density("off")
        assert nd.permits("t.alarm").heard is True

    def test_3_explicit_presence_beats_the_dial_BOTH_ways(self, monkeypatch):
        reg = nd.default_registry()
        reg.register("t.legacy", nd.Density.ON, legacy_flag="JARVIS_T_LEGACY")
        nd.set_density("off")
        monkeypatch.setenv("JARVIS_T_LEGACY", "true")
        assert nd.permits("t.legacy").heard is True
        nd.set_density("verbose")
        monkeypatch.setenv("JARVIS_T_LEGACY", "false")
        assert nd.permits("t.legacy").heard is False

    def test_3_PRESENCE_is_the_test_not_truthiness(self, monkeypatch):
        """`FLAG=false` is an operator decision; `FLAG` unset is the absence
        of one. A truthiness test cannot tell them apart, which is why the
        old verb's own writes made every producer look operator-set."""
        reg = nd.default_registry()
        reg.register("t.presence", nd.Density.ON,
                     legacy_flag="JARVIS_T_PRESENCE")
        nd.set_density("verbose")
        assert nd.permits("t.presence").reason.startswith("density:")
        monkeypatch.setenv("JARVIS_T_PRESENCE", "false")
        assert nd.permits("t.presence").reason.startswith("explicit:")

    def test_3_an_unparseable_explicit_falls_through_to_the_dial(
            self, monkeypatch):
        """An operator typo should not pin a voice on or off forever."""
        reg = nd.default_registry()
        reg.register("t.typo", nd.Density.ON, legacy_flag="JARVIS_T_TYPO")
        monkeypatch.setenv("JARVIS_T_TYPO", "yes-please")
        nd.set_density("off")
        assert nd.permits("t.typo").heard is False
        nd.set_density("on")
        assert nd.permits("t.typo").heard is True

    def test_4_threshold(self):
        reg = nd.default_registry()
        reg.register("t.thresh", nd.Density.ON)
        nd.set_density("preambles")
        assert nd.permits("t.thresh").heard is False
        nd.set_density("on")
        assert nd.permits("t.thresh").heard is True

    def test_a_verdict_always_carries_a_reason(self):
        """Never a bare bool. The defect being fixed is a dial that could
        not explain itself."""
        reg = nd.default_registry()
        reg.register("t.reason", nd.Density.VERBOSE)
        nd.set_density("off")
        assert nd.permits("t.reason").reason


class TestTheDialWritesExactlyOneKey:
    def test_set_density_touches_only_its_own_key(self, monkeypatch):
        nd.set_density("off")
        import os
        assert os.environ[nd.DENSITY_ENV_VAR] == "off"
        for flag in _LEGACY_FLAGS:
            assert flag not in os.environ, (
                f"{flag} written by the dial — this is the self-shadowing "
                "bug: once written, every later read looks operator-set"
            )

    def test_the_verb_no_longer_shotguns_producer_flags(self):
        """Structural, AST-based: no `os.environ[...] = ...` inside
        `_handle_narrate`.

        Checked as an ASSIGNMENT node rather than by searching the source
        for flag names — the docstring legitimately names those flags while
        explaining why it must not write them, and a substring search cannot
        tell an explanation from the offence.
        """
        src = pathlib.Path(
            "backend/core/ouroboros/battle_test/serpent_flow.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_handle_narrate")
        writes = [
            t for node in ast.walk(fn)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Subscript)
            and isinstance(t.value, ast.Attribute)
            and t.value.attr == "environ"
        ]
        assert not writes, f"{len(writes)} os.environ writes in _handle_narrate"


class TestTheRoster:
    def test_it_reports_every_registered_voice(self):
        nd.ensure_discovered()
        names = {r.voice.name for r in nd.roster()}
        assert "moltbook.banter" in names
        assert "narrative.intent" in names

    def test_the_two_voice_families_share_one_ladder(self):
        """The point of the exercise: Moltbook and the narrative channel
        answer to the SAME dial."""
        nd.ensure_discovered()
        nd.set_density("off")
        heard = {r.voice.name for r in nd.roster() if r.verdict.heard}
        assert "moltbook.banter" not in heard
        assert "narrative.intent" not in heard
        assert "moltbook.conflict" in heard      # alarm survives

        nd.set_density("verbose")
        heard = {r.voice.name for r in nd.roster() if r.verdict.heard}
        assert {"moltbook.banter", "narrative.thinking"} <= heard

    def test_snapshot_is_transport_safe(self):
        nd.set_density("on")
        snap = nd.snapshot()
        assert snap["density"] == "on"
        assert isinstance(snap["audible"], list)
        import json
        json.dumps(snap)                      # must survive the bridge

    def test_discovery_is_idempotent(self):
        first = nd.ensure_discovered()
        assert nd.ensure_discovered() == first


class TestMoltbookIsGatedAtBothSeams:
    def test_surfacing_is_gated_but_the_ARCHIVE_IS_NOT(self):
        """A display preference has no business editing the society's
        memory. `_notify_subscribers` gates the feed; the row is still
        written, so `/moltbook` shows an unbroken history and turning the
        dial back up does not reveal a gap."""
        from backend.core.ouroboros.governance import moltbook
        src = pathlib.Path(moltbook.__file__).read_text()
        tree = ast.parse(src)
        notify = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef)
                      and n.name == "_notify_subscribers")
        assert any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") == "audible"
            for n in ast.walk(notify)
        ), "the surfacing seam does not consult the dial"
        # ...and the storage path must NOT.
        store = next(n for n in ast.walk(tree)
                     if isinstance(n, ast.ClassDef)
                     and n.name == "MoltbookStore")
        assert not any(
            isinstance(n, ast.Call) and getattr(n.func, "id", "") == "audible"
            for n in ast.walk(store)
        ), "the STORE consults the dial — density is editing the archive"

    def test_banter_generation_refuses_before_the_dice(self):
        """Refusing at the renderer would mean the reaction was already
        formulated — a model call spent and a resident's cooldown consumed
        — to produce something the operator asked not to see. The existing
        posture gate is checked first for exactly this reason."""
        from backend.core.ouroboros.governance import moltbook
        fn = next(n for n in ast.walk(ast.parse(
            pathlib.Path(moltbook.__file__).read_text()))
            if isinstance(n, ast.AsyncFunctionDef)
            and n.name == "_maybe_converse")
        body = ast.unparse(fn)
        gate = body.index("moltbook.banter")
        dice = body.index("sha256")
        assert gate < dice, "density is checked after the dice are rolled"

    def test_voice_is_derived_from_what_a_post_already_is(self):
        from backend.core.ouroboros.governance.moltbook import voice_for
        assert voice_for({"kind": "musing", "body": "⚔ contested"}) \
            == "moltbook.conflict"
        assert voice_for({"kind": "musing", "reply_to": "p1"}) \
            == "moltbook.banter"
        assert voice_for({"kind": "status", "body": "all green"}) \
            == "moltbook.post"


class TestItNeverRaises:
    @pytest.mark.parametrize("bad", [None, 0, object(), b"x", [], {}])
    def test_permits_survives_anything(self, bad):
        assert isinstance(nd.permits(bad), nd.Verdict)  # type: ignore[arg-type]

    def test_a_broken_registry_degrades_to_audible(self, monkeypatch):
        """A dial that cannot answer must never swallow the organism's
        voice."""
        def _boom(*_a, **_k):
            raise RuntimeError("registry gone")
        monkeypatch.setattr(nd._REGISTRY, "get", _boom)
        v = nd.permits("anything")
        assert v.heard is True
        assert v.reason == "degraded"
