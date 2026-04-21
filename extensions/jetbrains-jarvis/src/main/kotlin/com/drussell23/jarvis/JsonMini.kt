package com.drussell23.jarvis

/**
 * Minimal pure-Kotlin JSON parser — no third-party deps.
 *
 * The IntelliJ Platform ships with the Gson/Kotlinx libraries, but
 * we avoid declaring a runtime dependency that can conflict with a
 * user's IDE configuration. This parser covers the strict subset
 * we need: objects, arrays, strings, numbers (as Double),
 * true/false/null.
 *
 * For outbound JSON we never write — the plugin is read-only, so
 * there is no corresponding writer.
 */

class JsonParseException(message: String) : RuntimeException(message)

object JsonMini {
    fun parse(input: String): Any? {
        val p = Parser(input)
        p.skipWhitespace()
        val out = p.readValue()
        p.skipWhitespace()
        if (!p.atEnd) {
            throw JsonParseException("trailing data at offset ${p.pos}")
        }
        return out
    }

    private class Parser(private val src: String) {
        var pos: Int = 0
        val atEnd: Boolean get() = pos >= src.length

        fun peek(): Char = src[pos]

        fun skipWhitespace() {
            while (!atEnd && src[pos].isWhitespace()) pos++
        }

        fun readValue(): Any? {
            skipWhitespace()
            if (atEnd) throw JsonParseException("unexpected EOF")
            return when (peek()) {
                '{' -> readObject()
                '[' -> readArray()
                '"' -> readString()
                't', 'f' -> readBoolean()
                'n' -> readNull()
                else -> readNumber()
            }
        }

        private fun readObject(): Map<String, Any?> {
            expect('{')
            skipWhitespace()
            val out = LinkedHashMap<String, Any?>()
            if (!atEnd && peek() == '}') { pos++; return out }
            while (!atEnd) {
                skipWhitespace()
                val key = readString()
                skipWhitespace()
                expect(':')
                skipWhitespace()
                out[key] = readValue()
                skipWhitespace()
                if (atEnd) throw JsonParseException("unterminated object")
                when (peek()) {
                    ',' -> pos++
                    '}' -> { pos++; return out }
                    else -> throw JsonParseException("expected , or } at offset $pos")
                }
            }
            throw JsonParseException("unterminated object")
        }

        private fun readArray(): List<Any?> {
            expect('[')
            skipWhitespace()
            val out = ArrayList<Any?>()
            if (!atEnd && peek() == ']') { pos++; return out }
            while (!atEnd) {
                skipWhitespace()
                out.add(readValue())
                skipWhitespace()
                if (atEnd) throw JsonParseException("unterminated array")
                when (peek()) {
                    ',' -> pos++
                    ']' -> { pos++; return out }
                    else -> throw JsonParseException("expected , or ] at offset $pos")
                }
            }
            throw JsonParseException("unterminated array")
        }

        private fun readString(): String {
            expect('"')
            val sb = StringBuilder()
            while (!atEnd) {
                val c = src[pos]
                if (c == '"') { pos++; return sb.toString() }
                if (c == '\\') {
                    pos++
                    if (atEnd) throw JsonParseException("dangling escape")
                    when (val esc = src[pos]) {
                        '"', '\\', '/' -> sb.append(esc)
                        'n' -> sb.append('\n')
                        'r' -> sb.append('\r')
                        't' -> sb.append('\t')
                        'b' -> sb.append('\b')
                        'f' -> sb.append('\u000C')
                        'u' -> {
                            if (pos + 4 >= src.length) {
                                throw JsonParseException("short \\u escape")
                            }
                            val hex = src.substring(pos + 1, pos + 5)
                            sb.append(hex.toInt(16).toChar())
                            pos += 4
                        }
                        else -> throw JsonParseException("bad escape \\$esc")
                    }
                    pos++
                } else {
                    sb.append(c); pos++
                }
            }
            throw JsonParseException("unterminated string")
        }

        private fun readBoolean(): Boolean {
            if (src.regionMatches(pos, "true", 0, 4)) {
                pos += 4; return true
            }
            if (src.regionMatches(pos, "false", 0, 5)) {
                pos += 5; return false
            }
            throw JsonParseException("expected boolean at offset $pos")
        }

        private fun readNull(): Any? {
            if (src.regionMatches(pos, "null", 0, 4)) {
                pos += 4; return null
            }
            throw JsonParseException("expected null at offset $pos")
        }

        private fun readNumber(): Any {
            val start = pos
            if (!atEnd && peek() == '-') pos++
            while (!atEnd && (peek().isDigit() || peek() in ".eE+-")) pos++
            val lit = src.substring(start, pos)
            // Integer fast path.
            if ('.' !in lit && 'e' !in lit && 'E' !in lit) {
                lit.toLongOrNull()?.let { return it }
            }
            return lit.toDoubleOrNull()
                ?: throw JsonParseException("bad number '$lit'")
        }

        private fun expect(c: Char) {
            if (atEnd || src[pos] != c) {
                throw JsonParseException("expected $c at offset $pos")
            }
            pos++
        }
    }
}
