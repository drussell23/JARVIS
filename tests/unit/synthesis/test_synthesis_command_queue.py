import time
from backend.neural_mesh.synthesis.gap_signal_bus import CapabilityGapEvent
from backend.neural_mesh.synthesis.synthesis_command_queue import (
    SynthesisCommandQueue,
)


def _evt(task_type="vision_action", target_app="xcode", source="primary_fallback"):
    return CapabilityGapEvent(
        goal="open prefs",
        task_type=task_type,
        target_app=target_app,
        source=source,
    )


def test_enqueue_and_dequeue():
    q = SynthesisCommandQueue(ttl_seconds=60)
    q.enqueue(_evt())
    cmd = q.dequeue()
    assert cmd is not None
    assert cmd.event.domain_id == "vision_action:xcode"


def test_expired_not_returned():
    q = SynthesisCommandQueue(ttl_seconds=0)
    q.enqueue(_evt())
    time.sleep(0.01)
    assert q.dequeue() is None


def test_supersession():
    q = SynthesisCommandQueue(ttl_seconds=60)
    e1 = _evt()
    e2 = CapabilityGapEvent(goal="updated goal", task_type="vision_action", target_app="xcode", source="primary_fallback")
    q.enqueue(e1)
    q.enqueue(e2)
    cmd = q.dequeue()
    assert cmd is not None
    assert cmd.event.goal == "updated goal"
    assert q.dequeue() is None


def test_different_domains_coexist():
    q = SynthesisCommandQueue(ttl_seconds=60)
    q.enqueue(_evt(task_type="vision_action"))
    q.enqueue(_evt(task_type="email_compose", target_app="gmail"))
    results = []
    while (cmd := q.dequeue()) is not None:
        results.append(cmd)
    assert len(results) == 2
