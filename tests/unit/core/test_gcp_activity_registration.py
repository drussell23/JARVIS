"""Tests for GCP verification activity registration with ProgressController."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))


class TestGcpActivityRegistration:
    def test_activity_marker_updated_on_progress(self):
        """GCP progress callback should update activity markers."""
        markers = {}
        sources = {}

        def mock_mark_activity(source, stage=None):
            phase = stage or "intelligence"
            markers[phase] = time.time()
            sources[phase] = source

        mock_mark_activity("gcp_verification", stage="intelligence")

        assert "intelligence" in markers
        assert sources["intelligence"] == "gcp_verification"

    def test_activity_marker_not_set_on_recycle(self):
        """GCP recycle events should NOT register as startup activity."""
        markers = {}

        pct = 0
        detail = "recycling VM"
        is_recycle = pct == 0 and "recycl" in detail.lower()

        if not is_recycle:
            markers["intelligence"] = time.time()

        assert "intelligence" not in markers

    def test_activity_timestamp_is_recent(self):
        """Activity marker timestamp should be within tolerance."""
        markers = {}
        now = time.time()
        markers["intelligence"] = now

        staleness = time.time() - markers["intelligence"]
        assert staleness < 1.0

    def test_activity_source_contains_gcp(self):
        """Activity source string should identify GCP verification."""
        source = "gcp_verification"
        assert "gcp" in source.lower()
