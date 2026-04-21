package com.drussell23.jarvis

import java.net.HttpURLConnection
import java.net.URI
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets

/**
 * HTTP GET client for the Slice 1 observability surface.
 *
 * Uses Java's stock [HttpURLConnection] so the plugin has no
 * runtime HTTP dependency beyond the JDK the IntelliJ Platform
 * already ships. Every request is GET. Schema-version mismatch
 * raises [SchemaMismatchException]; non-2xx raises
 * [ObservabilityException] with the stable reason_code from the
 * server payload.
 */
class ObservabilityClient(
    private val endpoint: String,
    private val timeoutMs: Int = 10_000,
    private val connectionFactory: (String) -> HttpURLConnection = { url ->
        (URI(url).toURL().openConnection() as HttpURLConnection)
    },
) {
    init {
        validateLoopback(endpoint)
    }

    fun health(): HealthResponse {
        val m = get("/observability/health")
        return HealthResponse(
            schemaVersion = m["schema_version"] as? String ?: "",
            enabled = m["enabled"] as? Boolean ?: false,
            apiVersion = m["api_version"] as? String ?: "",
            surface = m["surface"] as? String ?: "",
        )
    }

    @Suppress("UNCHECKED_CAST")
    fun taskList(): TaskListResponse {
        val m = get("/observability/tasks")
        val ids = (m["op_ids"] as? List<Any?>)?.filterIsInstance<String>() ?: emptyList()
        return TaskListResponse(
            schemaVersion = m["schema_version"] as? String ?: "",
            opIds = ids,
            count = (m["count"] as? Long)?.toInt() ?: ids.size,
        )
    }

    @Suppress("UNCHECKED_CAST")
    fun taskDetail(opId: String): TaskDetail {
        validateOpId(opId)
        val m = get("/observability/tasks/" + URLEncoder.encode(opId, StandardCharsets.UTF_8))
        val rawTasks = (m["tasks"] as? List<Any?>) ?: emptyList()
        val tasks = rawTasks.mapNotNull { raw ->
            val t = raw as? Map<String, Any?> ?: return@mapNotNull null
            val state = TaskState.fromWire(t["state"] as? String) ?: return@mapNotNull null
            TaskProjection(
                taskId = t["task_id"] as? String ?: return@mapNotNull null,
                state = state,
                title = t["title"] as? String ?: "",
                body = t["body"] as? String ?: "",
                sequence = (t["sequence"] as? Long)?.toInt() ?: 0,
                cancelReason = t["cancel_reason"] as? String ?: "",
            )
        }
        return TaskDetail(
            schemaVersion = m["schema_version"] as? String ?: "",
            opId = m["op_id"] as? String ?: opId,
            closed = m["closed"] as? Boolean ?: false,
            activeTaskId = m["active_task_id"] as? String,
            tasks = tasks,
            boardSize = (m["board_size"] as? Long)?.toInt() ?: tasks.size,
        )
    }

    @Suppress("UNCHECKED_CAST")
    private fun get(path: String): Map<String, Any?> {
        val base = endpoint.trimEnd('/')
        val conn = connectionFactory("$base$path")
        conn.requestMethod = "GET"
        conn.connectTimeout = timeoutMs
        conn.readTimeout = timeoutMs
        conn.setRequestProperty("Accept", "application/json")
        conn.useCaches = false
        return try {
            val status = conn.responseCode
            val body = (if (status in 200..299) conn.inputStream else conn.errorStream)
                ?.use { it.readBytes() } ?: ByteArray(0)
            if (status != 200) {
                val reason = extractReasonCode(body)
                throw ObservabilityException(
                    "$path returned $status", status = status, reasonCode = reason,
                )
            }
            val parsed = try {
                JsonMini.parse(body.toString(StandardCharsets.UTF_8))
            } catch (_: JsonParseException) {
                throw ObservabilityException(
                    "invalid JSON from $path", status = status,
                    reasonCode = "client.invalid_json",
                )
            }
            val map = parsed as? Map<String, Any?> ?: throw ObservabilityException(
                "expected JSON object from $path", status = status,
                reasonCode = "client.unexpected_shape",
            )
            if (!isSupportedSchema(map["schema_version"] as? String)) {
                throw SchemaMismatchException(map["schema_version"] as? String ?: "")
            }
            map
        } finally {
            conn.disconnect()
        }
    }

    private fun extractReasonCode(body: ByteArray): String {
        if (body.isEmpty()) return ""
        return try {
            val m = JsonMini.parse(body.toString(StandardCharsets.UTF_8)) as? Map<*, *>
            m?.get("reason_code") as? String ?: ""
        } catch (_: Exception) {
            ""
        }
    }

    companion object {
        fun validateLoopback(endpoint: String) {
            val host = try {
                URL(endpoint).host
            } catch (_: Exception) {
                throw ObservabilityException(
                    "malformed endpoint URL: $endpoint",
                    reasonCode = "client.bad_endpoint",
                )
            }
            val acceptable = setOf("127.0.0.1", "::1", "localhost")
            if (host !in acceptable) {
                throw ObservabilityException(
                    "endpoint must be loopback (127.0.0.1/::1/localhost); got $host",
                    reasonCode = "client.non_loopback",
                )
            }
        }
    }
}
