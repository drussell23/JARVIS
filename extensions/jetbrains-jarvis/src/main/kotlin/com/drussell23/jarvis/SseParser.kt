package com.drussell23.jarvis

/**
 * Pure-Kotlin SSE frame parser — no dependencies, no I/O.
 *
 * Exported for unit tests. Takes a single raw frame (the text
 * between two ``\n\n`` delimiters) and returns either a validated
 * [StreamEvent] or null if the frame is malformed / schema-
 * mismatched / unknown event type.
 *
 * Mirrors the Sublime and VS Code parsers so wire-compat is
 * enforced across all three clients.
 */

object SseParser {
    fun parse(rawFrame: String): StreamEvent? {
        var eventId: String? = null
        var eventType: String? = null
        val dataParts = mutableListOf<String>()
        for (line in rawFrame.split('\n')) {
            if (line.isEmpty() || line.startsWith(":")) continue
            val colon = line.indexOf(':')
            if (colon < 0) continue
            val field = line.substring(0, colon).trim()
            var value = line.substring(colon + 1)
            if (value.startsWith(" ")) value = value.substring(1)
            when (field) {
                "id" -> eventId = value
                "event" -> eventType = value
                "data" -> dataParts.add(value)
            }
        }
        if (eventId == null || eventType == null || dataParts.isEmpty()) {
            return null
        }
        val type = StreamEventType.fromWire(eventType) ?: return null
        val parsed = try {
            JsonMini.parse(dataParts.joinToString("\n"))
        } catch (_: JsonParseException) {
            return null
        }
        @Suppress("UNCHECKED_CAST")
        val asMap = parsed as? Map<String, Any?> ?: return null
        if (!isSupportedSchema(asMap["schema_version"] as? String)) return null
        val opId = asMap["op_id"] as? String ?: ""
        val ts = asMap["timestamp"] as? String ?: ""
        @Suppress("UNCHECKED_CAST")
        val payload = (asMap["payload"] as? Map<String, Any?>) ?: emptyMap()
        return StreamEvent(
            schemaVersion = SUPPORTED_SCHEMA_VERSION,
            eventId = eventId,
            eventType = type,
            opId = opId,
            timestamp = ts,
            payload = payload,
        )
    }
}
