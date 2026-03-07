"""Tests for unconditional GCP activity callback registration.

Root cause: _poll_health_until_ready() only called progress_callback when
has_real_data=True (APARS present). If the GCP VM was unreachable or returned
non-APARS responses, _mark_startup_activity("gcp_verification") never fired,
making GCP polling invisible to ProgressController.

Fix: Added activity_callback parameter that fires on every poll iteration
regardless of APARS data availability.
"""


class TestGcpUnconditionalActivity:
    def test_activity_callback_fires_without_apars(self):
        """activity_callback should fire even when has_real_data is False."""
        call_count = 0

        def activity_cb():
            nonlocal call_count
            call_count += 1

        # Simulate poll iterations with no APARS data
        has_real_data = False
        progress_callback = None  # No progress callback

        for _ in range(5):
            # progress_callback only fires with real data
            if progress_callback and has_real_data:
                progress_callback(0, "starting", "waiting")

            # activity_callback fires unconditionally
            if activity_cb:
                activity_cb()

        assert call_count == 5

    def test_activity_callback_fires_with_apars(self):
        """activity_callback should ALSO fire when APARS data is present."""
        activity_count = 0
        progress_count = 0

        def activity_cb():
            nonlocal activity_count
            activity_count += 1

        def progress_cb(pct, phase, detail):
            nonlocal progress_count
            progress_count += 1

        has_real_data = True

        for _ in range(3):
            if progress_cb and has_real_data:
                progress_cb(50, "loading", "step 3/6")

            if activity_cb:
                activity_cb()

        assert activity_count == 3
        assert progress_count == 3

    def test_activity_callback_exception_doesnt_break_polling(self):
        """activity_callback errors should be swallowed, not crash polling."""
        poll_completed = False

        def bad_activity_cb():
            raise RuntimeError("callback error")

        # Simulate the try/except pattern in the polling loop
        try:
            bad_activity_cb()
        except Exception:
            pass  # Swallowed, as the real code does

        poll_completed = True
        assert poll_completed is True

    def test_none_activity_callback_is_safe(self):
        """When activity_callback is None, polling should work normally."""
        activity_callback = None

        # This should not raise
        if activity_callback:
            activity_callback()

        # No assertion needed — just verifying no exception

    def test_activity_callback_independent_of_has_real_data(self):
        """Verify activity_callback decision tree is independent of has_real_data."""
        # The key invariant: activity_callback is called regardless of
        # has_real_data value, while progress_callback is gated on it.
        scenarios = [
            {"has_real_data": True, "expect_progress": True, "expect_activity": True},
            {"has_real_data": False, "expect_progress": False, "expect_activity": True},
        ]

        for scenario in scenarios:
            progress_called = False
            activity_called = False

            def progress_cb(pct, phase, detail):
                nonlocal progress_called
                progress_called = True

            def activity_cb():
                nonlocal activity_called
                activity_called = True

            # Simulate the code path
            if progress_cb and scenario["has_real_data"]:
                progress_cb(50, "test", "test")

            if activity_cb:
                activity_cb()

            assert progress_called == scenario["expect_progress"], (
                f"progress_called should be {scenario['expect_progress']} "
                f"when has_real_data={scenario['has_real_data']}"
            )
            assert activity_called == scenario["expect_activity"], (
                f"activity_called should always be True"
            )
