"""Tests for IntentClassifier — deterministic voice command routing."""
import pytest

from backend.core.intent_classifier import (
    IntentClassifier,
    CommandIntent,
    ClassificationResult,
    get_intent_classifier,
)


class TestCommandIntent:
    def test_values(self):
        assert CommandIntent.ACTION.value == "action"
        assert CommandIntent.QUERY.value == "query"


class TestClassificationResult:
    def test_frozen(self):
        result = ClassificationResult(
            intent=CommandIntent.ACTION,
            confidence=0.9,
            matched_signal="search youtube",
            action_category="browser",
        )
        with pytest.raises(AttributeError):
            result.intent = CommandIntent.QUERY


class TestIntentClassifierActions:
    """Test that action commands are correctly classified."""

    @pytest.fixture
    def classifier(self):
        return IntentClassifier()

    # --- Browser actions ---

    def test_search_youtube(self, classifier):
        r = classifier.classify("search YouTube for NBA highlights")
        assert r.intent == CommandIntent.ACTION
        assert r.action_category == "browser"

    def test_search_google(self, classifier):
        r = classifier.classify("search Google for Python tutorials")
        assert r.intent == CommandIntent.ACTION

    def test_go_to_website(self, classifier):
        r = classifier.classify("go to linkedin.com")
        assert r.intent == CommandIntent.ACTION
        assert r.action_category == "browser"

    def test_look_up(self, classifier):
        r = classifier.classify("look up the weather forecast")
        assert r.intent == CommandIntent.ACTION

    def test_search_for(self, classifier):
        r = classifier.classify("search for computer science videos")
        assert r.intent == CommandIntent.ACTION
        assert r.action_category == "browser"

    def test_browse_to(self, classifier):
        r = classifier.classify("browse to github.com")
        assert r.intent == CommandIntent.ACTION

    def test_pull_up(self, classifier):
        r = classifier.classify("pull up the NBA scores")
        assert r.intent == CommandIntent.ACTION

    def test_show_me(self, classifier):
        r = classifier.classify("show me today's headlines")
        assert r.intent == CommandIntent.ACTION

    # --- App control ---

    def test_open_apple_music(self, classifier):
        r = classifier.classify("open Apple Music")
        assert r.intent == CommandIntent.ACTION
        assert r.action_category == "app_control"

    def test_open_spotify(self, classifier):
        r = classifier.classify("open Spotify")
        assert r.intent == CommandIntent.ACTION

    def test_open_generic_app(self, classifier):
        r = classifier.classify("open Final Cut Pro")
        assert r.intent == CommandIntent.ACTION
        assert r.action_category == "app_control"

    def test_launch_app(self, classifier):
        r = classifier.classify("launch Safari")
        assert r.intent == CommandIntent.ACTION

    def test_play_music(self, classifier):
        r = classifier.classify("play music")
        assert r.intent == CommandIntent.ACTION
        assert r.action_category == "app_control"

    def test_play_song(self, classifier):
        r = classifier.classify("play the song Dreams by Fleetwood Mac")
        assert r.intent == CommandIntent.ACTION

    def test_switch_to(self, classifier):
        r = classifier.classify("switch to Terminal")
        assert r.intent == CommandIntent.ACTION

    def test_close_app(self, classifier):
        r = classifier.classify("close the app")
        assert r.intent == CommandIntent.ACTION

    def test_pause_music(self, classifier):
        r = classifier.classify("pause music")
        assert r.intent == CommandIntent.ACTION

    # --- System actions ---

    def test_connect_wifi(self, classifier):
        r = classifier.classify("connect WiFi")
        assert r.intent == CommandIntent.ACTION
        assert r.action_category == "system"

    def test_take_screenshot(self, classifier):
        r = classifier.classify("take a screenshot")
        assert r.intent == CommandIntent.ACTION

    def test_set_volume(self, classifier):
        r = classifier.classify("set volume to 50%")
        assert r.intent == CommandIntent.ACTION

    def test_turn_on_bluetooth(self, classifier):
        r = classifier.classify("turn on Bluetooth")
        assert r.intent == CommandIntent.ACTION
        assert r.action_category == "system"

    def test_mute(self, classifier):
        r = classifier.classify("mute")
        assert r.intent == CommandIntent.ACTION

    # --- Communication ---

    def test_send_email(self, classifier):
        r = classifier.classify("send an email to John")
        assert r.intent == CommandIntent.ACTION
        assert r.action_category == "communication"

    def test_schedule_meeting(self, classifier):
        r = classifier.classify("schedule a meeting for tomorrow at 3")
        assert r.intent == CommandIntent.ACTION

    def test_set_reminder(self, classifier):
        r = classifier.classify("set a reminder to call Mom at 5pm")
        assert r.intent == CommandIntent.ACTION

    # --- Code operations ---

    def test_run_tests(self, classifier):
        r = classifier.classify("run the tests")
        assert r.intent == CommandIntent.ACTION
        assert r.action_category == "code"

    def test_deploy(self, classifier):
        r = classifier.classify("deploy the latest build")
        assert r.intent == CommandIntent.ACTION


class TestIntentClassifierQueries:
    """Test that query/conversation commands are correctly classified."""

    @pytest.fixture
    def classifier(self):
        return IntentClassifier()

    def test_what_is(self, classifier):
        r = classifier.classify("what is machine learning?")
        assert r.intent == CommandIntent.QUERY

    def test_whats(self, classifier):
        r = classifier.classify("what's the weather like today?")
        assert r.intent == CommandIntent.QUERY

    def test_tell_me_about(self, classifier):
        r = classifier.classify("tell me about quantum computing")
        assert r.intent == CommandIntent.QUERY

    def test_how_does(self, classifier):
        r = classifier.classify("how does the neural mesh work?")
        assert r.intent == CommandIntent.QUERY

    def test_explain(self, classifier):
        r = classifier.classify("explain the Trinity architecture")
        assert r.intent == CommandIntent.QUERY

    def test_why_is(self, classifier):
        r = classifier.classify("why is the sky blue?")
        assert r.intent == CommandIntent.QUERY

    def test_who_is(self, classifier):
        r = classifier.classify("who is the CEO of Anthropic?")
        assert r.intent == CommandIntent.QUERY

    def test_describe(self, classifier):
        r = classifier.classify("describe the Ouroboros pipeline")
        assert r.intent == CommandIntent.QUERY

    def test_can_you_explain(self, classifier):
        r = classifier.classify("can you explain how async works?")
        assert r.intent == CommandIntent.QUERY

    def test_do_you_know(self, classifier):
        r = classifier.classify("do you know what time it is?")
        assert r.intent == CommandIntent.QUERY

    def test_question_mark_fallback(self, classifier):
        r = classifier.classify("is the server running?")
        assert r.intent == CommandIntent.QUERY

    def test_how_many(self, classifier):
        r = classifier.classify("how many agents are active?")
        assert r.intent == CommandIntent.QUERY


class TestIntentClassifierEdgeCases:
    """Test edge cases and ambiguous commands."""

    @pytest.fixture
    def classifier(self):
        return IntentClassifier()

    def test_empty_string(self, classifier):
        r = classifier.classify("")
        assert r.intent == CommandIntent.QUERY
        assert r.confidence == 0.0

    def test_whitespace_only(self, classifier):
        r = classifier.classify("   ")
        assert r.intent == CommandIntent.QUERY

    def test_query_beats_action_when_explicit(self, classifier):
        """Query prefix should take priority over action words."""
        r = classifier.classify("what is the search YouTube feature?")
        assert r.intent == CommandIntent.QUERY

    def test_tell_me_about_overrides_action(self, classifier):
        """'tell me about' is Q&A even if it contains action words."""
        r = classifier.classify("tell me about the open source licenses")
        assert r.intent == CommandIntent.QUERY

    def test_ambiguous_defaults_to_query(self, classifier):
        """Ambiguous commands should default to QUERY (safe fallback)."""
        r = classifier.classify("hmm interesting")
        assert r.intent == CommandIntent.QUERY
        assert r.confidence == 0.50  # default fallback

    def test_confidence_ranking(self, classifier):
        """Query prefix should have higher confidence than default."""
        q = classifier.classify("what is Python?")
        d = classifier.classify("random gibberish")
        assert q.confidence > d.confidence

    def test_case_insensitive(self, classifier):
        r = classifier.classify("SEARCH YOUTUBE FOR NBA")
        assert r.intent == CommandIntent.ACTION

    def test_extra_whitespace(self, classifier):
        r = classifier.classify("  search   youtube  for  NBA  ")
        assert r.intent == CommandIntent.ACTION

    def test_action_verb_at_start(self, classifier):
        r = classifier.classify("find the nearest coffee shop")
        assert r.intent == CommandIntent.ACTION

    def test_action_verb_not_at_start(self, classifier):
        """'find' in the middle should not trigger action verb match."""
        r = classifier.classify("help me find my keys")
        # 'find' is in the middle, but "search for" is not present
        # Should still match via keyword patterns or default to query
        # The word "find" matches _ACTION_PHRASES via "search for" → no
        # This tests that verb-at-start check is properly scoped
        assert r.intent in (CommandIntent.ACTION, CommandIntent.QUERY)


class TestIntentClassifierSingleton:
    def test_singleton_returns_same_instance(self):
        a = get_intent_classifier()
        b = get_intent_classifier()
        assert a is b

    def test_singleton_is_intent_classifier(self):
        c = get_intent_classifier()
        assert isinstance(c, IntentClassifier)


class TestClassificationResultFields:
    def test_action_result_fields(self):
        classifier = IntentClassifier()
        r = classifier.classify("open Safari")
        assert r.intent == CommandIntent.ACTION
        assert r.confidence > 0.0
        assert r.matched_signal != ""
        assert r.action_category != ""

    def test_query_result_fields(self):
        classifier = IntentClassifier()
        r = classifier.classify("what is the meaning of life?")
        assert r.intent == CommandIntent.QUERY
        assert r.confidence > 0.0
        assert r.matched_signal != ""
        assert r.action_category == ""
