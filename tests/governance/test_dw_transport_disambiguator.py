from __future__ import annotations

from backend.core.ouroboros.governance.preflight_probe import ProbeOutcome
from backend.core.ouroboros.governance.dw_transport_disambiguator import (
    FailureClass,
    classify_surface_failure,
)


def _fail(msg="", body="", status=0, timeout=False):
    return ProbeOutcome(
        model_id="m", success=False, status_code=status,
        error_body=body, error_message=msg, timeout=timeout,
    )


def test_done_before_content_is_upstream():
    # THE load-bearing case — clean stream, empty completion → upstream.
    oc = _fail(msg="done_before_content", body="done_before_content")
    assert classify_surface_failure(oc) is FailureClass.UPSTREAM


def test_stream_closed_early_is_transport():
    assert classify_surface_failure(_fail(msg="stream_closed_early")) is FailureClass.TRANSPORT


def test_ttft_timeout_is_transport():
    assert classify_surface_failure(_fail(msg="ttft_timeout", timeout=True)) is FailureClass.TRANSPORT


def test_server_disconnected_is_transport():
    assert classify_surface_failure(
        _fail(msg="prober_raised:ServerDisconnectedError:peer closed")
    ) is FailureClass.TRANSPORT


def test_asyncio_timeout_is_transport():
    assert classify_surface_failure(_fail(msg="asyncio.wait_for hit 10s", timeout=True)) is FailureClass.TRANSPORT


def test_success_is_none():
    ok = ProbeOutcome(model_id="m", success=True, status_code=200)
    assert classify_surface_failure(ok) is FailureClass.NONE


def test_5xx_body_without_stream_marker_is_upstream():
    # HTTP 500 with a server body but no transport marker → upstream.
    assert classify_surface_failure(
        _fail(msg="status_500", body="Internal server error", status=500)
    ) is FailureClass.UPSTREAM


def test_transport_prefixed_aiohttp_exception_is_transport():
    # Real dw_heavy_probe.py:790 shape for a mid-stream socket drop —
    # keyed by the "transport:" prefix, not the exception name.
    assert classify_surface_failure(
        _fail(msg="transport:ServerTimeoutError:read timed out")
    ) is FailureClass.TRANSPORT
    assert classify_surface_failure(
        _fail(msg="transport:ClientPayloadError:incomplete read")
    ) is FailureClass.TRANSPORT
    assert classify_surface_failure(
        _fail(msg="transport:ClientOSError:broken pipe")
    ) is FailureClass.TRANSPORT


def test_run_preflight_outer_catch_is_transport():
    # Real preflight_probe.py:428 shape (probe_raised, not prober_raised).
    assert classify_surface_failure(
        _fail(msg="probe_raised:ClientConnectorError:x")
    ) is FailureClass.TRANSPORT


def test_session_acquire_failed_is_transport():
    # Real preflight_probe.py:727 adapter shape.
    assert classify_surface_failure(
        _fail(msg="session_acquire_failed:OSError:no route to host")
    ) is FailureClass.TRANSPORT


def test_none_fields_do_not_raise():
    class _OC:
        success = False
        status_code = None
        error_message = None
        error_body = None

    assert classify_surface_failure(_OC()) is FailureClass.UPSTREAM


def test_done_before_content_beats_5xx_and_transport_substring():
    # Upstream precedence: even with a transport substring + a 5xx status,
    # done_before_content must win (never flush a healthy socket).
    assert classify_surface_failure(
        _fail(msg="done_before_content stream_closed_early", status=500)
    ) is FailureClass.UPSTREAM
