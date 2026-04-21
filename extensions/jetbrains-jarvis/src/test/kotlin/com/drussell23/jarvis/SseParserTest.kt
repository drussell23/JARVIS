package com.drussell23.jarvis

import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test

class SseParserTest {

    private fun frameText(id: String, type: String, opId: String = "op-x"): String {
        val payload = """{
          "schema_version": "1.0",
          "event_id": "$id",
          "event_type": "$type",
          "op_id": "$opId",
          "timestamp": "t",
          "payload": {}
        }""".replace("\n", "")
        return "id: $id\nevent: $type\ndata: $payload"
    }

    @Test
    fun parsesWellFormedFrame() {
        val ev = SseParser.parse(frameText("e1", "task_created"))
        assertNotNull(ev)
        assertEquals("e1", ev!!.eventId)
        assertEquals(StreamEventType.TASK_CREATED, ev.eventType)
    }

    @Test
    fun ignoresCommentLines() {
        val raw = ": keepalive\n" + frameText("e1", "task_started")
        val ev = SseParser.parse(raw)
        assertNotNull(ev)
    }

    @Test
    fun rejectsSchemaMismatch() {
        val raw = frameText("e1", "task_created").replace("1.0", "9.9")
        assertNull(SseParser.parse(raw))
    }

    @Test
    fun rejectsUnknownEventType() {
        val raw = "id: e1\nevent: bogus\ndata: " +
            """{"schema_version":"1.0","event_id":"e1","event_type":"bogus","op_id":"x","timestamp":"t","payload":{}}"""
        assertNull(SseParser.parse(raw))
    }

    @Test
    fun rejectsMissingData() {
        assertNull(SseParser.parse("id: e1\nevent: task_created\n"))
    }

    @Test
    fun taskEventClassification() {
        for (t in listOf(
            StreamEventType.TASK_CREATED,
            StreamEventType.TASK_STARTED,
            StreamEventType.TASK_UPDATED,
            StreamEventType.TASK_COMPLETED,
            StreamEventType.TASK_CANCELLED,
            StreamEventType.BOARD_CLOSED,
        )) {
            assertTrue(t.isTaskEvent, "expected task event: $t")
            assertFalse(t.isControlEvent)
        }
        for (t in listOf(
            StreamEventType.HEARTBEAT,
            StreamEventType.STREAM_LAG,
            StreamEventType.REPLAY_START,
            StreamEventType.REPLAY_END,
        )) {
            assertFalse(t.isTaskEvent, "expected control event: $t")
            assertTrue(t.isControlEvent)
        }
    }

    @Test
    fun isSupportedSchemaOnlyMatchesOneDotZero() {
        assertTrue(isSupportedSchema("1.0"))
        assertFalse(isSupportedSchema("2.0"))
        assertFalse(isSupportedSchema(null))
    }
}
