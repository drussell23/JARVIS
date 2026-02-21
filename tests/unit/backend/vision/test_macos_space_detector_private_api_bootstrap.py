from __future__ import annotations

import logging


class _FakeObjC:
    @staticmethod
    def loadBundle(_name, _namespace, bundle_path=None):
        _ = bundle_path
        return object()

    @staticmethod
    def loadBundleFunctions(_bundle, namespace, _functions):
        namespace["CGSCopyManagedDisplaySpaces"] = lambda _conn: []
        namespace["CGSMainConnectionID"] = lambda: 77


def _new_detector(module):
    detector = module.MacOSSpaceDetector.__new__(module.MacOSSpaceDetector)
    detector._private_api_available = False
    detector._cgs_copy_managed_display_spaces = None
    detector._cgs_get_active_space = None
    detector.cgs_connection = None
    return detector


def test_private_api_bootstrap_without_objc_msgsend(monkeypatch):
    from backend.vision import macos_space_detector as module

    monkeypatch.setattr(module, "objc", _FakeObjC())

    detector = _new_detector(module)
    detector._init_private_apis()

    assert detector._private_api_available is True
    assert detector.cgs_connection == 77
    assert callable(detector._cgs_copy_managed_display_spaces)


def test_private_api_warning_is_emitted_once(monkeypatch, caplog):
    from backend.vision import macos_space_detector as module

    monkeypatch.setattr(module, "objc", None)
    monkeypatch.setattr(module, "_PRIVATE_API_WARNING_EMITTED", False)

    with caplog.at_level(logging.WARNING):
        detector_a = _new_detector(module)
        detector_a._init_private_apis()

        detector_b = _new_detector(module)
        detector_b._init_private_apis()

    warning_lines = [
        rec.message for rec in caplog.records
        if "Could not load private APIs, using fallback" in rec.message
    ]
    assert len(warning_lines) == 1
