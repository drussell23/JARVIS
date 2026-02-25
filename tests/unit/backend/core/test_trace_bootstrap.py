"""Tests for TraceBootstrap singleton initialization."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestTraceBootstrap(unittest.TestCase):
    def setUp(self):
        from backend.core.trace_bootstrap import _reset
        _reset()

    def tearDown(self):
        from backend.core.trace_bootstrap import _reset
        _reset()

    def test_initialize_creates_all_components(self):
        from backend.core.trace_bootstrap import (
            initialize, get_lifecycle_emitter, get_span_recorder,
            get_envelope_factory,
        )
        with tempfile.TemporaryDirectory() as tmp:
            result = initialize(
                trace_dir=Path(tmp),
                boot_id="test-boot",
                runtime_epoch_id="test-epoch",
            )
            assert result is True
            assert get_lifecycle_emitter() is not None
            assert get_span_recorder() is not None
            assert get_envelope_factory() is not None

    def test_double_initialize_is_idempotent(self):
        from backend.core.trace_bootstrap import initialize, get_lifecycle_emitter
        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="b1", runtime_epoch_id="e1")
            emitter1 = get_lifecycle_emitter()
            initialize(trace_dir=Path(tmp), boot_id="b2", runtime_epoch_id="e2")
            emitter2 = get_lifecycle_emitter()
            assert emitter1 is emitter2  # Same instance

    def test_getters_return_none_before_init(self):
        from backend.core.trace_bootstrap import (
            get_lifecycle_emitter, get_span_recorder, get_envelope_factory,
        )
        assert get_lifecycle_emitter() is None
        assert get_span_recorder() is None
        assert get_envelope_factory() is None

    def test_shutdown_closes_emitter(self):
        from backend.core.trace_bootstrap import initialize, shutdown, get_lifecycle_emitter
        with tempfile.TemporaryDirectory() as tmp:
            initialize(trace_dir=Path(tmp), boot_id="b", runtime_epoch_id="e")
            emitter = get_lifecycle_emitter()
            shutdown()
            assert emitter._closed is True

    def test_env_var_driven_config(self):
        from backend.core.trace_bootstrap import initialize, get_envelope_factory
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {
                "JARVIS_TRACE_DIR": tmp,
                "JARVIS_BOOT_ID": "env-boot",
                "JARVIS_RUNTIME_EPOCH_ID": "env-epoch",
            }):
                initialize()
                factory = get_envelope_factory()
                assert factory is not None
                # runtime_epoch_id has a public property
                assert factory.runtime_epoch_id == "env-epoch"
                # boot_id is stored as _boot_id (private via __slots__)
                assert factory._boot_id == "env-boot"


if __name__ == "__main__":
    unittest.main()
