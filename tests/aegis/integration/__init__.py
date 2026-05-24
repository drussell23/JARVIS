"""Aegis integration test suite — gated by ``@pytest.mark.aegis_integration``.

Tests in this directory exercise the full credential-confiscation
spine in production-shape code: real preflight, real subprocess, real
provider modules (Slice 2B-ii+), real Aegis daemon, stub upstream.

Run locally:
    pytest -m aegis_integration tests/aegis/integration/

Default ``pytest tests/aegis/`` SKIPS this directory so iteration stays
fast. CI runs the integration suite as a separate stage.

Slice 2B-i ships the directory + marker registration. Provider-rewire
integration tests land in Slice 2B-ii.
"""
