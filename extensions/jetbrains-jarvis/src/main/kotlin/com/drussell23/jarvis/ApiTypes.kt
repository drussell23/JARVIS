package com.drussell23.jarvis

/**
 * Wire-type mirrors of the JARVIS observability schema v1.0.
 *
 * Shapes exactly match the server-side payloads emitted by
 * `backend/core/ouroboros/governance/ide_observability.py` and
 * `ide_observability_stream.py`. Clients feature-detect on
 * `schemaVersion` and must never assume fields outside this
 * contract.
 *
 * Authority invariant: these are read-only shapes. The plugin
 * never constructs a payload and POSTs it back to the agent.
 */

const val SUPPORTED_SCHEMA_VERSION: String = "1.0"

enum class TaskState(val wire: String) {
    PENDING("pending"),
    IN_PROGRESS("in_progress"),
    COMPLETED("completed"),
    CANCELLED("cancelled");

    companion object {
        fun fromWire(value: String?): TaskState? =
            entries.firstOrNull { it.wire == value }
    }
}

data class TaskProjection(
    val taskId: String,
    val state: TaskState,
    val title: String,
    val body: String,
    val sequence: Int,
    val cancelReason: String,
)

data class TaskDetail(
    val schemaVersion: String,
    val opId: String,
    val closed: Boolean,
    val activeTaskId: String?,
    val tasks: List<TaskProjection>,
    val boardSize: Int,
)

data class HealthResponse(
    val schemaVersion: String,
    val enabled: Boolean,
    val apiVersion: String,
    val surface: String,
)

data class TaskListResponse(
    val schemaVersion: String,
    val opIds: List<String>,
    val count: Int,
)

/**
 * Frozen event-type vocabulary. Matches the Slice 2 broker's
 * [_VALID_EVENT_TYPES] set.
 */
enum class StreamEventType(val wire: String) {
    TASK_CREATED("task_created"),
    TASK_STARTED("task_started"),
    TASK_UPDATED("task_updated"),
    TASK_COMPLETED("task_completed"),
    TASK_CANCELLED("task_cancelled"),
    BOARD_CLOSED("board_closed"),
    HEARTBEAT("heartbeat"),
    STREAM_LAG("stream_lag"),
    REPLAY_START("replay_start"),
    REPLAY_END("replay_end");

    val isTaskEvent: Boolean
        get() = when (this) {
            TASK_CREATED, TASK_STARTED, TASK_UPDATED,
            TASK_COMPLETED, TASK_CANCELLED, BOARD_CLOSED -> true
            else -> false
        }

    val isControlEvent: Boolean
        get() = !isTaskEvent

    companion object {
        fun fromWire(value: String?): StreamEventType? =
            entries.firstOrNull { it.wire == value }
    }
}

data class StreamEvent(
    val schemaVersion: String,
    val eventId: String,
    val eventType: StreamEventType,
    val opId: String,
    val timestamp: String,
    val payload: Map<String, Any?>,
)

class ObservabilityException(
    message: String,
    val status: Int = -1,
    val reasonCode: String = "",
) : RuntimeException(message)

class SchemaMismatchException(val received: String) : RuntimeException(
    "schema_version mismatch: expected $SUPPORTED_SCHEMA_VERSION, got $received"
)

// --- Validation helpers ----------------------------------------------------

private val OP_ID_REGEX = Regex("^[A-Za-z0-9_\\-]{1,128}$")

fun validateOpId(opId: String) {
    if (!OP_ID_REGEX.matches(opId)) {
        throw ObservabilityException(
            "malformed op_id: $opId",
            status = 400,
            reasonCode = "client.malformed_op_id",
        )
    }
}

fun isSupportedSchema(schemaVersion: String?): Boolean =
    schemaVersion == SUPPORTED_SCHEMA_VERSION
