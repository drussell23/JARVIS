package com.drussell23.jarvis

import kotlin.test.*
import kotlin.test.Test

class ApiTypesTest {

    @Test
    fun schemaVersionConstant() {
        assertEquals("1.0", SUPPORTED_SCHEMA_VERSION)
    }

    @Test
    fun taskStateFromWire() {
        assertEquals(TaskState.PENDING, TaskState.fromWire("pending"))
        assertEquals(TaskState.IN_PROGRESS, TaskState.fromWire("in_progress"))
        assertEquals(TaskState.COMPLETED, TaskState.fromWire("completed"))
        assertEquals(TaskState.CANCELLED, TaskState.fromWire("cancelled"))
        assertNull(TaskState.fromWire("bogus"))
        assertNull(TaskState.fromWire(null))
    }

    @Test
    fun streamEventTypeFromWire() {
        assertEquals(StreamEventType.TASK_CREATED, StreamEventType.fromWire("task_created"))
        assertEquals(StreamEventType.HEARTBEAT, StreamEventType.fromWire("heartbeat"))
        assertNull(StreamEventType.fromWire("not_real"))
    }

    @Test
    fun validateOpIdAcceptsValid() {
        validateOpId("op-abc")
        validateOpId("op_123")
        validateOpId("A".repeat(128))
    }

    @Test
    fun validateOpIdRejectsMalformed() {
        assertFailsWith<ObservabilityException> {
            validateOpId("bad space")
        }
        assertFailsWith<ObservabilityException> {
            validateOpId("")
        }
        assertFailsWith<ObservabilityException> {
            validateOpId("A".repeat(129))
        }
    }

    @Test
    fun observabilityExceptionCarriesStatusAndReason() {
        val exc = ObservabilityException("oops", status = 403, reasonCode = "x.y")
        assertEquals(403, exc.status)
        assertEquals("x.y", exc.reasonCode)
    }

    @Test
    fun schemaMismatchCarriesReceived() {
        val exc = SchemaMismatchException("9.9")
        assertEquals("9.9", exc.received)
    }
}
