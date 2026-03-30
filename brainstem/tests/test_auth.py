from brainstem.auth import BrainstemAuth


def test_canonicalize_matches_spec():
    auth = BrainstemAuth(device_id="watch-ultra2-derek", device_secret="a" * 64)
    payload = {
        "command_id": "cmd-001", "device_id": "watch-ultra2-derek",
        "device_type": "watch", "text": "refactor the auth module",
        "priority": "realtime", "response_mode": "stream",
        "timestamp": "2026-03-29T18:45:00Z",
    }
    canonical = auth.canonicalize(payload)
    assert canonical == (
        "command_id=cmd-001&device_id=watch-ultra2-derek&device_type=watch&"
        "priority=realtime&response_mode=stream&text=refactor the auth module&"
        "timestamp=2026-03-29T18:45:00Z"
    )


def test_canonicalize_includes_intent_hint():
    auth = BrainstemAuth(device_id="mac-m1", device_secret="a" * 64)
    payload = {
        "command_id": "cmd-001", "device_id": "mac-m1", "device_type": "mac",
        "text": "scan", "intent_hint": "ouroboros_scan",
        "priority": "background", "response_mode": "notify",
        "timestamp": "2026-03-29T18:45:00Z",
    }
    canonical = auth.canonicalize(payload)
    assert "intent_hint=ouroboros_scan" in canonical
    parts = canonical.split("&")
    keys = [p.split("=")[0] for p in parts]
    assert keys.index("intent_hint") == 3


def test_canonicalize_includes_sorted_context():
    auth = BrainstemAuth(device_id="mac-m1", device_secret="a" * 64)
    payload = {
        "command_id": "cmd-001", "device_id": "mac-m1", "device_type": "mac",
        "text": "hello", "priority": "realtime", "response_mode": "stream",
        "timestamp": "2026-03-29T18:45:00Z",
        "context": {"location": "office", "battery_level": 72},
    }
    canonical = auth.canonicalize(payload)
    assert 'context={"battery_level": 72, "location": "office"}' in canonical


def test_sign_produces_64_char_hex():
    auth = BrainstemAuth(device_id="mac-m1", device_secret="a" * 64)
    payload = {
        "command_id": "cmd-001", "device_id": "mac-m1", "device_type": "mac",
        "text": "hello", "priority": "realtime", "response_mode": "stream",
        "timestamp": "2026-03-29T18:45:00Z",
    }
    sig = auth.sign(payload)
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)


def test_sign_is_deterministic():
    auth = BrainstemAuth(device_id="mac-m1", device_secret="a" * 64)
    payload = {
        "command_id": "cmd-001", "device_id": "mac-m1", "device_type": "mac",
        "text": "hello", "priority": "realtime", "response_mode": "stream",
        "timestamp": "2026-03-29T18:45:00Z",
    }
    assert auth.sign(payload) == auth.sign(payload)


def test_sign_changes_with_different_text():
    auth = BrainstemAuth(device_id="mac-m1", device_secret="a" * 64)
    base = {
        "command_id": "cmd-001", "device_id": "mac-m1", "device_type": "mac",
        "priority": "realtime", "response_mode": "stream",
        "timestamp": "2026-03-29T18:45:00Z",
    }
    sig_a = auth.sign({**base, "text": "hello"})
    sig_b = auth.sign({**base, "text": "goodbye"})
    assert sig_a != sig_b
