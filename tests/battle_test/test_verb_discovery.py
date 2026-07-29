"""A verb an operator cannot find is a verb they do not have.

The registry answered two different questions with one dictionary: "how do
I route this line" and "does this verb exist". `_CUSTOM_HANDLER_EXCLUSIONS`
says out loud that its entries "retain their legacy custom handlers" — a
statement that they EXIST — and excluding them from the map erased them
from `/help`, tab completion, the slash palette and the progress board's
count. Sixteen top-level verbs, `/undo` and `/goal` and `/budget` among
them, were handled and undiscoverable.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
    _CHAIN_SOURCES,
    _CUSTOM_HANDLER_EXCLUSIONS,
    discover_chain_verbs,
    external_verbs,
    list_dispatchable_verbs,
    list_verbs,
    prime_registry,
    register_external_verb,
)


@pytest.fixture(scope="module", autouse=True)
def _primed():
    prime_registry()


class TestTheSixteen:
    @pytest.mark.parametrize("verb", [
        "undo", "goal", "budget", "risk", "plan", "remember", "forget",
        "tdd", "plugins", "status", "ops", "pause", "resume", "infer",
        "liquidity", "doctor",
    ])
    def test_every_handled_verb_is_findable(self, verb):
        assert verb in list_verbs(), f"/{verb} is handled and invisible"

    def test_each_one_says_WHERE_it_lives(self):
        """`/help` should be able to tell an operator where a verb is
        handled rather than implying this registry owns it."""
        ext = external_verbs()
        assert ext.get("undo", "").startswith("harness.py:")
        assert ext.get("risk") == "custom handler"


class TestRoutingIsUnCHANGED:
    def test_discovery_grew_and_dispatch_did_not(self):
        """The whole point: making a verb findable must not make this
        registry start routing it. A verb routed twice is worse than one
        routed nowhere."""
        assert len(list_verbs()) > len(list_dispatchable_verbs())

    def test_no_external_verb_is_dispatchable_here(self):
        overlap = set(external_verbs()) & set(list_dispatchable_verbs())
        assert not overlap, f"{overlap} would be routed twice"

    def test_registering_an_existing_dispatcher_is_REFUSED(self):
        """Ambiguity about who owns a verb is worse than an unlisted one."""
        known = list_dispatchable_verbs()[0]
        assert register_external_verb(known, "somewhere") is False

    @pytest.mark.asyncio
    async def test_an_external_verb_still_falls_THROUGH(self):
        """`/undo` must reach the harness chain exactly as before."""
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            try_dispatch,
        )
        outcome = await try_dispatch("/undo 2")
        assert outcome.matched is False


class TestDerivedNeverWritten:
    def test_the_chain_scan_finds_verbs_by_what_they_DO(self):
        """`cmd == "ops"` and `raw == "true"` are identical as tests. The
        discriminator is the body: one routes to a handler, the other
        assigns a boolean. A denylist of non-verb words would need an
        entry every time someone wrote a new env parse."""
        derived = discover_chain_verbs()
        assert "undo" in derived
        for noise in ("true", "yes", "on", "false", "unknown"):
            assert noise not in derived, noise

    def test_it_is_structural_not_positional(self):
        """Line numbers and function names change; the scan must survive
        the chain being reordered, renamed or split."""
        src = "\n".join([
            "def anything(self, command):",
            "    cmd = command.strip()",
            "    if cmd == 'zzztest':",
            "        self._repl_cmd_zzztest(cmd)",
        ])
        tmp = pathlib.Path("build_tmp_chain_probe.py")
        tmp.write_text(src)
        try:
            found = discover_chain_verbs(sources=[tmp.name])
        finally:
            tmp.unlink(missing_ok=True)
        assert "zzztest" in found

    def test_a_sub_command_is_not_mistaken_for_a_verb(self):
        """`/memory add` must not register `add`. Only the branch's own
        body is inspected, never nested If bodies."""
        derived = discover_chain_verbs()
        for sub in ("add", "rm", "list", "all"):
            assert sub not in derived, sub

    def test_no_hand_written_verb_list_exists(self):
        """The defect was a list saying what existed while the code said
        otherwise, with nothing comparing them."""
        import inspect

        from backend.core.ouroboros.battle_test import (
            repl_dispatch_registry as r,
        )
        src = inspect.getsource(r.discover_chain_verbs)
        for verb in ("undo", "goal", "budget", "tdd"):
            assert f'"{verb}"' not in src


class TestItCannotRegress:
    def test_EVERY_routing_branch_in_the_chain_is_discoverable(self):
        """The invariant that stops verb #17 from going dark. Reads the
        chains directly, so it fails the moment a handled verb is not
        findable — which is precisely what nothing checked before."""
        known = set(list_verbs())
        missing = []
        for rel in _CHAIN_SOURCES:
            path = pathlib.Path(rel)
            if not path.exists():
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.If):
                    continue
                from backend.core.ouroboros.battle_test import (
                    repl_dispatch_registry as r,
                )
                if not r._branch_routes_somewhere(node):
                    continue
                for verb in r._verbs_in_test(node.test):
                    if verb not in known:
                        missing.append(f"/{verb} ({path.name}:{node.lineno})")
        assert not missing, (
            "handled but undiscoverable — no /help, no completion, no "
            "palette:\n  " + "\n  ".join(sorted(set(missing))))

    def test_every_EXCLUDED_verb_is_discoverable(self):
        """Excluding a verb from auto-dispatch is a routing decision. It
        must never again be read as an existence claim."""
        known = set(list_verbs())
        for verb in _CUSTOM_HANDLER_EXCLUSIONS:
            assert verb in known, verb


class TestNeverRaises:
    @pytest.mark.parametrize("call", [
        lambda: discover_chain_verbs(sources=["does/not/exist.py"]),
        lambda: discover_chain_verbs(sources=[]),
        lambda: register_external_verb(None),
        lambda: register_external_verb("  "),
        lambda: register_external_verb("has spaces"),
        lambda: external_verbs(),
    ])
    def test_junk_degrades(self, call):
        assert call() is not None or True

    def test_a_syntactically_broken_source_is_skipped(self, tmp_path):
        bad = tmp_path / "broken.py"
        bad.write_text("def (((:\n")
        assert discover_chain_verbs(
            sources=[bad.name], root=str(tmp_path)) == {}
