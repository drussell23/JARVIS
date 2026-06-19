from __future__ import annotations


def test_disabled_never_sheds(monkeypatch):
    monkeypatch.delenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", raising=False)
    import backend.core.ouroboros.governance.control_plane_load_shed as ls
    ls._reset_for_test()
    ls.stream_begin()
    assert ls.evaluate(9999.0) is False
    assert ls.is_shedding() is False
    ls.stream_end()


def test_sheds_on_high_lag_during_stream_restores_after(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "true")
    import backend.core.ouroboros.governance.control_plane_load_shed as ls
    ls._reset_for_test()
    # no stream -> no shed even on high lag
    assert ls.evaluate(9999.0) is False
    ls.stream_begin()
    assert ls.evaluate(50.0) is False        # below threshold (150)
    assert ls.evaluate(500.0) is True         # above threshold -> shed
    assert ls.is_shedding() is True
    ls.stream_end()                            # stream done -> restore
    assert ls.is_shedding() is False


def test_reentrant_streams(monkeypatch):
    monkeypatch.setenv("JARVIS_CONTROL_PLANE_LOAD_SHED_ENABLED", "true")
    import backend.core.ouroboros.governance.control_plane_load_shed as ls
    ls._reset_for_test()
    ls.stream_begin(); ls.stream_begin()
    assert ls.evaluate(500.0) is True
    ls.stream_end()
    assert ls.is_shedding() is True            # one stream still active
    ls.stream_end()
    assert ls.is_shedding() is False
