package com.drussell23.jarvis

import kotlin.test.*
import kotlin.test.Test
import java.util.concurrent.ConcurrentLinkedQueue

class OpsControllerTest {

    private fun fakeSettings() = JarvisSettings().apply {
        endpoint = "http://127.0.0.1:8765"
        enabled = true
        autoReconnect = true
        reconnectMaxBackoffMs = 30_000L
        opIdFilter = ""
    }

    private fun frame(
        type: StreamEventType, opId: String = "op-x", id: String = "e1",
    ): StreamEvent = StreamEvent(
        schemaVersion = "1.0",
        eventId = id, eventType = type, opId = opId,
        timestamp = "t", payload = emptyMap(),
    )

    @Test
    fun taskEventAddsNewOpId() {
        val updates = ConcurrentLinkedQueue<List<String>>()
        val controller = OpsController(
            settings = fakeSettings(),
            onOpsChanged = { updates.add(it) },
        )
        controller.applyStreamEvent(frame(StreamEventType.TASK_CREATED, "op-new"))
        assertEquals(listOf("op-new"), controller.snapshot())
    }

    @Test
    fun taskEventIsIdempotent() {
        val updates = ConcurrentLinkedQueue<List<String>>()
        val controller = OpsController(
            settings = fakeSettings(),
            onOpsChanged = { updates.add(it) },
        )
        controller.applyStreamEvent(frame(StreamEventType.TASK_CREATED, "op-a"))
        controller.applyStreamEvent(frame(StreamEventType.TASK_UPDATED, "op-a"))
        assertEquals(listOf("op-a"), controller.snapshot())
        // Exactly one UI callback fired (the second was a no-op).
        assertEquals(1, updates.size)
    }

    @Test
    fun controlEventDoesNotAddOpId() {
        val controller = OpsController(
            settings = fakeSettings(),
            onOpsChanged = { },
        )
        controller.applyStreamEvent(frame(StreamEventType.HEARTBEAT, "op-hb"))
        assertTrue(controller.snapshot().isEmpty())
    }
}
