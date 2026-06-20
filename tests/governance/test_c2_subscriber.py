"""Tests for the C2 telemetry subscriber's pure projection logic (no network)."""
from __future__ import annotations
import importlib.util
import os

# Load the standalone script as a module (it lives in scripts/, not a package).
_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "scripts",
                     "jarvis_c2_subscriber.py")
_spec = importlib.util.spec_from_file_location("jarvis_c2_subscriber", _PATH)
c2 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(c2)  # type: ignore[union-attr]


def test_parse_sse_block_event_and_data():
    et, data = c2.parse_sse_block('event: fleet_calibrated\ndata: {"model_id": "x", "ast_pass_rate": 1.0}')
    assert et == "fleet_calibrated" and data["ast_pass_rate"] == 1.0


def test_parse_sse_block_heartbeat_and_malformed():
    assert c2.parse_sse_block(": heartbeat") == (None, None)
    et, data = c2.parse_sse_block("event: x\ndata: {not json")
    assert et == "x" and data is None


def test_dashboard_counts_state_applied():
    d = c2.C2Dashboard()
    line = d.ingest("operation_terminal", {"state": "applied", "op_id": "op-1"})
    assert "state=applied" in line and d.applied == 1


def test_dashboard_counts_advisor_block():
    d = c2.C2Dashboard()
    line = d.ingest("operation_terminal",
                    {"state": "blocked", "terminal_reason_code": "advisor_blocked", "op_id": "o"})
    assert "advisor BLOCKED" in line and d.advisor_blocks == 1


def test_dashboard_tracks_fleet_ewma():
    d = c2.C2Dashboard()
    d.ingest("fleet_calibrated",
             {"model_id": "deepseek/DeepSeek-V4-Pro", "valid_tok_per_s": 90.0, "ast_pass_rate": 1.0})
    assert d.ewma["DeepSeek-V4-Pro"] == 90.0
    assert "applied=" in d.render_status() and "EWMA[" in d.render_status()


def test_dashboard_ignores_irrelevant_events():
    d = c2.C2Dashboard()
    assert d.ingest("heartbeat", {"x": 1}) is None
    assert d.ingest("task_updated", {"x": 1}) is None
    assert d.ingest(None, None) is None


def test_dashboard_recent_is_bounded():
    d = c2.C2Dashboard(recent_cap=5)
    for i in range(20):
        d.ingest("operation_terminal", {"state": "applied", "op_id": f"o{i}"})
    assert len(d.recent) == 5 and d.applied == 20
