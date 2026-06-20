"""Dynamic Transport Router — payload-size RT-vs-BATCH selection tests."""
from __future__ import annotations

import types
import pytest

from backend.core.ouroboros.governance import payload_transport_heuristic as h


def _ctx(targets=(), complexity="trivial"):
    return types.SimpleNamespace(target_files=tuple(targets), task_complexity=complexity)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED", "JARVIS_DW_BATCH_PAYLOAD_LINE_THRESHOLD",
              "JARVIS_DW_BATCH_PAYLOAD_FILE_THRESHOLD", "JARVIS_PROJECT_ROOT"):
        monkeypatch.delenv(k, raising=False)
    # neutralize the line-count probe unless a test sets it
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.providers._max_target_line_count",
        lambda targets, root: 0, raising=False,
    )
    yield


def test_disabled_by_default():
    assert h.dynamic_transport_enabled() is False


def test_localized_single_small_file_streams(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED", "true")
    assert h.should_batch_by_payload(_ctx(("requirements.txt",), "trivial")) is False


def test_multi_file_batches(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED", "true")
    assert h.should_batch_by_payload(_ctx(("a.py", "b.py", "c.py"), "moderate")) is True


def test_large_file_batches(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED", "true")
    monkeypatch.setattr(
        "backend.core.ouroboros.governance.providers._max_target_line_count",
        lambda targets, root: 1200, raising=False,
    )
    assert h.should_batch_by_payload(_ctx(("big.py",), "moderate")) is True


def test_heavy_complexity_batches(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED", "true")
    assert h.should_batch_by_payload(_ctx(("new_file.py",), "heavy")) is True


def test_thresholds_env_tunable(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DW_BATCH_PAYLOAD_FILE_THRESHOLD", "2")
    assert h.should_batch_by_payload(_ctx(("a.py", "b.py"), "moderate")) is True


def test_fail_soft_to_rt_on_bad_context(monkeypatch):
    monkeypatch.setenv("JARVIS_DW_DYNAMIC_TRANSPORT_ENABLED", "true")
    bad = types.SimpleNamespace()  # no target_files attr
    assert h.should_batch_by_payload(bad) is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
