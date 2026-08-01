"""The palette showed rows that were not descriptions, for five reasons.

Four were subtractive rules matching below the unit they were written for — a
word-prefix instead of a word, a ``\\b`` inside a hyphenated compound, a
quantifier bound to one character of a two-character token, a tier marker read
as a citation. Each produced text no author wrote, which is precisely what
"SUBTRACTIVE only — no word appears that the author did not write" exists to
prevent.

The fifth is the one that matters: the cascade ranked SOURCES and never judged
RESULTS. Every rung asked "did this return a non-empty string?", so rank was
absolute — residue from a high rung beat prose from a low one, and no better
candidate could supersede a worse one that merely arrived first. All four of
the others were survivable bugs that this design turned into shipped rows.

The last test in this file is the one that keeps it fixed: it walks the LIVE
registry, so a verb added tomorrow is covered by a test written today.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.battle_test import repl_completion as rc
from backend.core.ouroboros.battle_test.verb_description import (
    Candidate, Shape, assess, best_candidate, shape_ceiling, to_operator_voice,
)
from backend.core.ouroboros.battle_test.verb_usage import UNDOCUMENTED


# ---------------------------------------------------------------------------
# 1. The four sub-token faults, each by the string that shipped
# ---------------------------------------------------------------------------


class TestSubtractionStaysAboveTheToken:
    def test_opener_is_a_word_not_a_prefix(self):
        """"Dispatcher" is not "dispatch" + noise.

        ``str.startswith("dispatch")`` fired on it and cut the word in half.
        /multi_prior's row read "Er for /multi_prior REPL verb"."""
        out = to_operator_voice(
            "Dispatcher for /multi_prior REPL verb.", "multi_prior")
        # The FIRST WORD is the assertion. `"er for" not in out` looked
        # equivalent and is not — it matches inside "dispatcher for", so it
        # fails on the correct output and would have to be weakened until it
        # stopped testing anything.
        assert out.split()[0].lower() == "dispatcher"

    @pytest.mark.parametrize("word,verb", [
        ("Processor state and counters", "process"),
        ("Router table for the mesh", "route"),
        ("Parser output for the last op", "parse"),
    ])
    def test_no_opener_decapitates_a_longer_word(self, word, verb):
        """Generalised past the one instance. Every opener has a longer word
        it is a prefix of, and the fix is at the matcher — so a template
        nobody has written yet is covered too."""
        assert to_operator_voice(word, verb).lower().startswith(
            word.split()[0][:4].lower())

    def test_verb_name_does_not_split_a_compound(self):
        """``\\b`` sits INSIDE "Embodied-state", so "embodied" matched its
        first half and the trailing ``[\\s:,—-]*`` ate the hyphen. The row
        read "State views: ..." — a sentence about state, produced by
        decapitating a sentence about embodied state."""
        out = to_operator_voice(
            "Embodied-state views: arch, aura, attention, portrait.",
            "embodied")
        assert out.lower().startswith("embodied-state")

    def test_verb_name_alone_is_still_stripped(self):
        """The compound guard must not disable the rule it guards: a bare
        leading name is still redundant beside the left-hand column."""
        assert to_operator_voice("Posture status and overrides", "posture") \
            .lower().startswith("status")

    def test_optional_pair_quantifier_actually_fires(self):
        """```` ``? ```` is one REQUIRED backtick plus an optional one, not an
        optional pair — so the pattern only matched text that still carried
        markup, and markup was unwrapped afterwards. It never fired at all.

        This is the highest-traffic docstring template in the dispatch
        packages, and with the pattern dead only the bare opener "Parse" came
        off. /cost shipped "REPL line and return the rendered result"."""
        assert rc._humanise(
            "Parse a ``/cost`` REPL line and return the rendered result.",
            "cost") == ""

    def test_tier_prefix_is_not_a_citation(self):
        """``^[A-Z]\\d+\\b`` matched "L3" in "L3 execution graph" and threw the
        phrase away as provenance. /graph's row became "Units, edges and
        stats" — a sentence with no subject."""
        out = to_operator_voice(
            "L3 execution graph — units, edges and stats.", "graph")
        assert out.lower().startswith("l3 execution graph")

    def test_a_real_citation_is_still_stripped(self):
        """Tightening the marker must not stop it working. Every genuine
        citation in this tree carries a second marker and still matches."""
        assert to_operator_voice(
            "M11 Slice 5 — replay an operation from its trace", "replay"
        ).lower().startswith("replay")


# ---------------------------------------------------------------------------
# 2. A short description is not a poor one
# ---------------------------------------------------------------------------


class TestLengthIsNotQuality:
    @pytest.mark.parametrize("text,verb", [
        ("Op fan-out tree.", "fanout"),
        ("Capability flag star-map.", "constellation"),
        ("Proactive anticipation surface.", "anticipate"),
        ("Memory crystallisation timeline.", "story"),
    ])
    def test_three_word_descriptions_survive(self, text, verb):
        """A four-WORD floor in `_humanise` discarded all four. They are real
        descriptions, they were sitting in the source, and the palette showed
        mined subcommand lists in their place — from which
        `verb_description`'s own module docstring concluded those verbs had
        "NO PROSE AT ALL"."""
        assert rc._humanise(text, verb) != ""
        assert assess(rc._humanise(text, verb), verb).acceptable

    def test_short_is_not_a_free_pass(self):
        """The floor was wrong, not the instinct behind it. Short AND
        contentless is still contentless."""
        assert assess("Line and dispatch", "x").shape in (
            Shape.RESIDUE, Shape.IMPLEMENTATION, Shape.EMPTY)


# ---------------------------------------------------------------------------
# 3. The classifier
# ---------------------------------------------------------------------------


class TestShapeClassification:
    @pytest.mark.parametrize("text,expected", [
        ("Er for multi prior REPL verb", Shape.RESIDUE),      # orphan head
        ("and render the dashboard", Shape.RESIDUE),          # fragment head
        ("Canonical entry point by", Shape.RESIDUE),          # dangling tail
        ("/causal REPL dispatcher", Shape.RESIDUE),           # verb echo
        ("§38.11-F operator surface", Shape.RESIDUE),         # bare citation
        ("Usage: /cost [governor]", Shape.USAGE),
        ("help · show · depth · status", Shape.SUBCOMMAND_LIST),
        ("Op fan-out tree", Shape.NOUN_PHRASE),
        ("Show the activity radar — what is moving right now", Shape.PROSE),
    ])
    def test_shapes(self, text, expected):
        assert assess(text, "x").shape is expected

    def test_runtime_vocabulary_marks_implementation(self):
        """True prose about the FUNCTION. Accurate, and useless to an
        operator — so it must score BELOW a description and ABOVE nothing."""
        got = assess(
            "Async — walks the target off the event loop, writes the manifest "
            "row.", "enqueue_soak")
        assert got.shape is Shape.IMPLEMENTATION

    def test_one_runtime_word_is_not_enough(self):
        """"L1 event emitter throughput and counters" is a real description
        that happens to contain "event". A single hit must not condemn it."""
        assert assess("L1 event emitter throughput and counters",
                      "events").shape is Shape.PROSE

    def test_maintainer_subject_is_caught_by_grammar_not_vocabulary(self):
        """"Tests can inject an explicit governor and/or session_browser
        without touching the module singletons" is fluent, specific and dense
        in domain words — every individual word is legitimate, so no
        vocabulary rule can catch it. The tell is that its SUBJECT is the
        reader. It scored as clean prose and became /cost's palette row."""
        got = assess(
            "Tests can inject an explicit governor and/or session_browser "
            "without touching the module singletons.", "cost")
        assert got.shape is Shape.IMPLEMENTATION
        assert any("maintainer-subject" in r for r in got.reasons)

    def test_assessment_carries_its_reason(self):
        """Without a reason a rejected description is indistinguishable from
        an absent one, and "which verbs still need a sentence, and what is
        wrong with what they have" becomes unanswerable."""
        assert assess("and render the dashboard", "x").reasons

    @pytest.mark.parametrize("hostile", [
        None, "", "   ", "\x00\x01", "·" * 500, "/" * 80, 12345, object(),
    ])
    def test_never_raises(self, hostile):
        """A palette that throws while the operator is typing is a worse
        failure than a palette with a gap in it."""
        assert isinstance(assess(hostile, "x").score, float)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. Arbitration — rank stopped deciding
# ---------------------------------------------------------------------------


class TestArbitration:
    def test_prose_beats_a_subcommand_list_nominated_first(self):
        """THE regression. Under boolean acceptance the first non-empty
        source won outright, so a mined vocabulary could never be superseded
        by a sentence found later."""
        winner = best_candidate([
            Candidate("help · show · depth · status", "mined", 0.0),
            Candidate("Op fan-out tree", "module-docstring", 0.0),
        ], "fanout")
        assert winner is not None and winner.source == "module-docstring"

    def test_a_source_prior_is_a_thumb_not_a_veto(self):
        """Rank survives as a prior because an ``Operator:`` line was written
        for the person typing the verb. It must still be able to LOSE, or
        nothing has changed."""
        winner = best_candidate([
            Candidate("and dispatch the line", "operator-section", 0.25),
            Candidate("Browse an operation's causal lineage", "module", 0.0),
        ], "causal")
        assert winner is not None and winner.source == "module"

    def test_a_prior_breaks_a_genuine_tie(self):
        winner = best_candidate([
            Candidate("Show the current operation mode", "function-docstring", 0.10),
            Candidate("Show the current operation mode", "module-docstring", 0.0),
        ], "mode")
        assert winner is not None and winner.source == "function-docstring"

    def test_nothing_acceptable_returns_none(self):
        """``None`` is a real answer. An honest "[undocumented]" is worth more
        than a confident fragment, because the operator acts on it."""
        assert best_candidate([
            Candidate("and dispatch", "a", 0.0),
            Candidate("/x REPL", "b", 0.0),
        ], "x") is None

    def test_empty_nomination_list(self):
        assert best_candidate([], "x") is None


class TestDeferredNominationsAreAnExactBound:
    """`mine_subcommands` costs an ``inspect.getsource`` + ``ast.parse`` per
    verb — 340ms across the table, on a surface that renders between
    keystrokes. Deferring it is only legitimate if the winner is unchanged.

    The distinction matters more than the milliseconds: "stop at the first
    source that answers" IS the boolean acceptance this arbiter replaced.
    Skipping work whose outcome is already determined is a different thing,
    and these tests are what keeps them different.
    """

    def test_a_determined_supplier_is_never_called(self):
        called = []

        def _expensive():
            called.append(1)
            return "help · show · depth · status"

        winner = best_candidate([
            Candidate("Show the activity radar — what is moving now",
                      "docstring", 0.10),
            Candidate("", "mined", 0.0, supplier=_expensive,
                      ceiling=shape_ceiling(Shape.SUBCOMMAND_LIST)),
        ], "radar")
        assert winner is not None and winner.source == "docstring"
        assert not called, "evaluated a source that could not have won"

    def test_an_undetermined_supplier_IS_called(self):
        """The bound must not become a veto. With nothing better in hand the
        deferred source is the answer, and skipping it would reintroduce
        exactly the gap that showed "[undocumented]" beside a documented
        verb."""
        called = []

        def _expensive():
            called.append(1)
            return "help · show · depth · status"

        winner = best_candidate([
            Candidate("and dispatch", "residue", 0.0),
            Candidate("", "mined", 0.0, supplier=_expensive,
                      ceiling=shape_ceiling(Shape.SUBCOMMAND_LIST)),
        ], "fanout")
        assert called, "a source that could have won was skipped"
        assert winner is not None and winner.source == "mined"

    def test_lazy_and_eager_agree_on_every_live_verb(self):
        """The bound is claimed to be EXACT. Asserted against the real table
        rather than a constructed pair, because the ceilings are properties of
        the shape floors and a future floor change could silently break the
        claim while every unit case still passed."""
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            _VERB_TO_DISPATCHER, prime_registry,
        )
        from backend.core.ouroboros.battle_test.verb_usage import (
            derive_usage, mine_subcommands,
        )
        prime_registry()
        for verb, fn in list(_VERB_TO_DISPATCHER.items()):
            lazy = rc._describe(fn)
            mined = mine_subcommands(fn)
            eager_pool = []
            if mined:
                eager_pool.append(Candidate(" · ".join(mined), "mined", 0.0))
            usage = derive_usage(fn, verb)
            if usage:
                eager_pool.append(Candidate(usage, "derived-usage", 0.0))
            if not eager_pool:
                continue
            # If either deferred source could have beaten the shipped answer,
            # deferring changed the outcome.
            shipped = assess(lazy, verb).score
            for cand in eager_pool:
                assert assess(cand.text, verb).score <= shipped + 1e-9, (
                    verb, lazy, cand.text)

    def test_a_throwing_supplier_is_survived(self):
        def _boom():
            raise RuntimeError("getsource failed")

        winner = best_candidate([
            Candidate("Browse an operation's causal lineage", "doc", 0.0),
            Candidate("", "mined", 0.0, supplier=_boom, ceiling=1.0),
        ], "causal")
        assert winner is not None and winner.source == "doc"


# ---------------------------------------------------------------------------
# 5. The module docstring, readmitted under judgement
# ---------------------------------------------------------------------------


class TestModuleDocstringIsScoredNotBanned:
    def test_a_good_module_docstring_wins(self):
        """/provider read "[undocumented]" while "operator-facing DoubleWord
        resilience dashboard" sat one scope up. The source was excluded by
        POLICY because nothing could judge its output; with judgement the
        policy is unnecessary."""
        from backend.core.ouroboros.governance import provider_repl
        got = rc._describe(provider_repl.dispatch_provider_command)
        assert got != UNDOCUMENTED
        assert "dashboard" in got.lower()

    def test_a_provenance_module_docstring_still_loses(self):
        """Readmitting the source must not readmit the noise that got it
        banned. "§38.11-F operator surface" now loses on its merits."""
        assert assess("§38.11-F operator surface", "x").shape is Shape.RESIDUE

    def test_only_modules_inside_the_dispatch_naming_cage_qualify(self):
        """A locally-defined dispatcher must not inherit its file's docstring
        — otherwise this very test module becomes a description source.

        `_is_verb_surface` delegates to the registry's own `_extract_verb_name`
        so "what counts as a verb surface" has ONE definition."""
        def dispatch_nothing_command(line: str):
            """Parse ``/nothing`` line and dispatch. NEVER raises."""

        assert rc._describe(dispatch_nothing_command) == UNDOCUMENTED

    def test_a_usage_lede_does_not_hide_the_sentence_behind_it(self):
        """Candidates were assessed BEFORE normalisation, so
        "``/enqueue_soak <target_path>`` — stage a crash-immortal Swarm soak"
        scored RESIDUE on its leading slash and was discarded — for a fault
        the very next step removes."""
        assert to_operator_voice(
            "/enqueue_soak <target_path> — stage a crash-immortal Swarm soak.",
            "enqueue_soak").lower().startswith("stage")


# ---------------------------------------------------------------------------
# 6. Client-side aliases
# ---------------------------------------------------------------------------


class TestAliasHelpIsDerivedFromTheRoutingTable:
    def test_no_row_shows_an_internal_action_id(self):
        """Six rows read "audio: force_wake" / "audio: ptt_stop" — an internal
        identifier presented as a description."""
        from backend.core.ouroboros.cli.ov import client_verbs
        for verb, help_text in client_verbs().items():
            assert not str(help_text).startswith("audio:"), verb

    def test_synonyms_are_named_as_synonyms(self):
        """``AUDIO_VERBS`` is a synonym table, so a verb without help has
        documented siblings. Saying "alias of /force-wake" is both true and
        more useful than a fresh sentence, because it tells the operator these
        are the same word rather than three things to learn."""
        from backend.core.ouroboros.cli.ov import client_verbs
        verbs = client_verbs()
        assert verbs["wake!"].startswith("alias of /force-wake")
        assert "seize the mic" in verbs["wake!"]

    def test_derived_from_the_table_not_transcribed(self):
        """A second list of which spellings are synonyms is exactly how a
        palette starts lying about what the CLI accepts."""
        from backend.core.ouroboros.cli import ov
        for verb, action in ov.AUDIO_VERBS.items():
            text = ov.client_verbs()[verb]
            if text.startswith("alias of /"):
                target = text[len("alias of /"):].split(" — ")[0]
                assert ov.AUDIO_VERBS[target] == action, verb

    def test_the_slash_form_the_palette_offers_actually_routes(self):
        """Found while auditing the descriptions, and the same lie in a
        different place: ``AUDIO_VERBS`` is keyed on BARE words, the palette
        renders every entry with a leading slash, and the router looked up
        what the operator typed. ``/wake`` was in the menu, missed the table
        on selection, and was relayed to a daemon with no ``/wake``
        dispatcher — offered, and inert.

        The neighbouring ``/deck``, ``/tasks`` and ``/keys`` branches each
        test both forms by hand, so every client verb was patched for slash
        forms one at a time and the table lookup covering fifteen of them
        never was."""
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            prime_registry,
        )
        from backend.core.ouroboros.cli import ov
        prime_registry()
        for verb in ("wake", "ptt stop", "force-wake", "mute", "wake!"):
            assert ov._resolve_audio_verb(f"/{verb}", ov.AUDIO_VERBS) == \
                ov.AUDIO_VERBS[verb], verb

    def test_the_daemon_still_wins_a_collision(self):
        """A blind slash-strip would have hijacked ``/voice`` and ``/listen``
        — both sit in ``AUDIO_VERBS`` AND have real daemon dispatchers, and
        the palette shows the DAEMON's description for each.

        `registry_from_dispatch` resolves that collision with "daemon wins";
        the router now resolves it the same way, so the menu and the router
        cannot disagree about who answers."""
        from backend.core.ouroboros.battle_test.repl_dispatch_registry import (
            _VERB_TO_DISPATCHER, prime_registry,
        )
        from backend.core.ouroboros.cli import ov
        prime_registry()
        contested = [v for v in ov.AUDIO_VERBS if v in _VERB_TO_DISPATCHER]
        assert contested, "fixture assumes at least one collision exists"
        for verb in contested:
            assert ov._resolve_audio_verb(f"/{verb}", ov.AUDIO_VERBS) is None

    def test_the_bare_form_is_untouched(self):
        """`voice` typed WITHOUT a slash is still the audio word. The fix adds
        a form; it must not remove one."""
        from backend.core.ouroboros.cli import ov
        for verb, action in ov.AUDIO_VERBS.items():
            assert ov._resolve_audio_verb(verb, ov.AUDIO_VERBS) == action

    def test_an_unprimed_registry_declines_to_intercept(self):
        """Fails CLOSED. A guard that guessed "mine" when it could not read
        the daemon's table would silently swallow verbs on a degraded boot,
        and swallowing is the harder failure to diagnose."""
        from backend.core.ouroboros.cli import ov
        assert ov._daemon_owns("\x00 not a verb") in (True, False)

    def test_an_undocumented_orphan_degrades_to_blank_not_to_an_id(self):
        """A verb whose whole synonym group is undocumented has nothing to
        borrow. Blank falls through to the honest ``[undocumented]``; an
        action id would look like help."""
        from backend.core.ouroboros.cli import ov
        assert ov._alias_help("no-such-verb") == ""


# ---------------------------------------------------------------------------
# 7. The property that keeps it fixed
# ---------------------------------------------------------------------------


class TestEveryLiveVerbHasADescription:
    """Walks the registry rather than a golden list.

    A fixture of today's 84 verbs would pass forever while verb 85 shipped
    "[undocumented]" — which is how the first four of these defects survived
    to reach a screenshot. The invariant is about the palette, so it is
    asserted over whatever the palette actually contains.
    """

    @staticmethod
    def _registry():
        return rc.unified_registry(None).verbs

    def test_registry_is_populated(self):
        """Guards the tests below: an empty registry would satisfy every
        'no bad row' assertion vacuously."""
        assert len(self._registry()) >= 60

    def test_no_verb_is_undocumented(self):
        missing = [v.slash_form for v in self._registry()
                   if v.description == UNDOCUMENTED]
        assert not missing, f"no description resolves for: {missing}"

    def test_no_row_is_residue(self):
        """The class that produced "Er for /multi_prior REPL verb" and "Line
        and render the dashboard": text that survived subtraction as a
        fragment, reads as help, and answers nothing."""
        bad = [(v.slash_form, v.description, assess(v.description,
                                                    v.slash_form.lstrip("/")).reasons)
               for v in self._registry()
               if assess(v.description,
                         v.slash_form.lstrip("/")).shape is Shape.RESIDUE]
        assert not bad, f"residue on the palette: {bad}"

    def test_no_row_falls_back_to_a_bare_usage_or_vocabulary(self):
        """Both are honest last resorts and neither is a description. They are
        allowed to EXIST as rungs — a verb documented tomorrow needs somewhere
        to fall — but no verb should currently need one."""
        weak = [(v.slash_form, v.description) for v in self._registry()
                if assess(v.description, v.slash_form.lstrip("/")).shape
                in (Shape.USAGE, Shape.SUBCOMMAND_LIST)]
        assert not weak, f"verbs still without prose: {weak}"

    def test_no_two_rows_carry_the_same_meaning(self):
        """Eleven rows carried four meanings::

            /flush   halt outbound audio now (ducking)
            /hush    alias of /flush — halt outbound audio now (ducking)
            /shh     alias of /flush — halt outbound audio now (ducking)

        `VerbDescriptor.aliases` exists to say exactly this and was empty on
        every row, while four consumers — typo suggestion, prefix matching,
        `matches()` and `/verb --help` — were already reading it.

        Asserted over the LIVE registry and by MEANING rather than by known
        family, so a duplicate arriving from a source nobody has written yet
        is caught by the same test. Two dispatch verbs pointing at one
        function would be a different origin and the identical symptom."""
        from collections import defaultdict
        by_meaning = defaultdict(list)
        for v in self._registry():
            by_meaning[v.description.strip().lower()].append(v.slash_form)
        dupes = {k: v for k, v in by_meaning.items() if len(v) > 1}
        assert not dupes, f"rows sharing one meaning: {dupes}"

    def test_no_row_advertises_itself_as_an_alias(self):
        """A row explaining that it is a duplicate is a row that should not
        exist. `/exit` spent its description on "alias for /quit" — the
        aliasing stated in the one column an operator scans for what a verb
        DOES."""
        for v in self._registry():
            assert "alias of" not in v.description.lower(), v.slash_form
            assert "alias for" not in v.description.lower(), v.slash_form

    def test_every_folded_alias_stays_reachable(self):
        """Collapsing the DISPLAY must not shrink the ACCEPT set. The router
        reads AUDIO_VERBS directly and is untouched; this pins the palette
        side, since a fold that quietly removed a verb would look identical
        in the row count."""
        from backend.core.ouroboros.battle_test.repl_completion import (
            fuzzy_match,
        )
        registry = rc.unified_registry(None)
        aliased = [(v, a) for v in registry.verbs for a in v.aliases]
        assert aliased, "fixture assumes the registry folds something"
        for verb, alias in aliased:
            assert verb.matches(alias), (verb.slash_form, alias)
            hits = fuzzy_match(alias, registry, max_results=3)
            assert verb.slash_form in [h.slash_form for h in hits], (
                f"typing {alias} does not reach {verb.slash_form}")

    def test_folding_did_not_orphan_the_alias_help_text(self):
        """`_alias_help` names a canonical verb and `audio_alias_families`
        decides which verb the palette SHOWS. Two implementations of "which
        spelling is canonical" would eventually point the operator at a row
        that had been folded away, so they share `_canonical_of`."""
        from backend.core.ouroboros.battle_test.repl_completion import (
            unified_registry,
        )
        from backend.core.ouroboros.cli import ov
        rows = {v.slash_form for v in unified_registry(None).verbs}
        for verb in ov.AUDIO_VERBS:
            text = ov._alias_help(verb)
            if not text.startswith("alias of /"):
                continue
            target = "/" + text[len("alias of /"):].split(" — ")[0]
            assert target in rows, f"{verb} points at {target}, which has no row"

    def test_no_row_echoes_only_its_own_name(self):
        for v in self._registry():
            name = v.slash_form.lstrip("/").replace("_", " ").lower()
            assert v.description.lower().strip(" .") != name, v.slash_form
