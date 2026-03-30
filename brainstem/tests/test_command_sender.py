from brainstem.command_sender import CommandSender
from brainstem.auth import BrainstemAuth
from brainstem.config import BrainstemConfig

def test_build_payload_has_required_fields():
    config = BrainstemConfig(vercel_url="https://jarvis.vercel.app", device_id="mac-m1-derek", device_secret="a" * 64)
    auth = BrainstemAuth(device_id=config.device_id, device_secret=config.device_secret)
    sender = CommandSender(config=config, auth=auth)
    payload = sender.build_payload(text="refactor auth", priority="realtime", response_mode="stream")
    assert payload["device_id"] == "mac-m1-derek"
    assert payload["device_type"] == "mac"
    assert payload["text"] == "refactor auth"
    assert "command_id" in payload
    assert "timestamp" in payload
    assert "signature" in payload
    assert len(payload["signature"]) == 64

def test_build_payload_includes_intent_hint():
    config = BrainstemConfig(vercel_url="https://jarvis.vercel.app", device_id="mac-m1-derek", device_secret="a" * 64)
    auth = BrainstemAuth(device_id=config.device_id, device_secret=config.device_secret)
    sender = CommandSender(config=config, auth=auth)
    payload = sender.build_payload(text="scan repos", priority="background", response_mode="notify", intent_hint="ouroboros_scan")
    assert payload["intent_hint"] == "ouroboros_scan"

def test_build_payload_includes_context():
    config = BrainstemConfig(vercel_url="https://jarvis.vercel.app", device_id="mac-m1-derek", device_secret="a" * 64)
    auth = BrainstemAuth(device_id=config.device_id, device_secret=config.device_secret)
    sender = CommandSender(config=config, auth=auth)
    payload = sender.build_payload(text="hello", priority="realtime", response_mode="stream", context={"active_app": "VSCode"})
    assert payload["context"]["active_app"] == "VSCode"
