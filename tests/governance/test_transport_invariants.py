from __future__ import annotations

import ast

from backend.core.ouroboros.governance.meta.shipped_code_invariants import (
    _validate_realtime_actuation_no_remote_transport,
    _validate_transport_no_hardcoded_endpoints,
)


def test_actuation_importing_transport_is_flagged():
    bad = (
        "from backend.core.ouroboros.governance.transport import DistributedEventBus\n"
        "def click(x, y):\n    pass\n"
    )
    violations = _validate_realtime_actuation_no_remote_transport(ast.parse(bad), bad)
    assert violations
    assert any("transport" in v for v in violations)


def test_actuation_without_transport_import_holds():
    good = "import time\ndef click(x, y):\n    time.sleep(0)\n"
    violations = _validate_realtime_actuation_no_remote_transport(ast.parse(good), good)
    assert violations == ()


def test_hardcoded_ipv4_in_config_is_flagged():
    bad = 'HOST = "10.1.2.3"\nURL = "wss://10.1.2.3:8443/ws"\n'
    violations = _validate_transport_no_hardcoded_endpoints(ast.parse(bad), bad)
    assert violations


def test_env_resolved_config_holds():
    good = 'import os\nhost = os.environ.get("JARVIS_BRAIN_WS_HOST")\n'
    violations = _validate_transport_no_hardcoded_endpoints(ast.parse(good), good)
    assert violations == ()
