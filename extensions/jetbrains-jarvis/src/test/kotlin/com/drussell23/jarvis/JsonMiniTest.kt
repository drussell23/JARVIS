package com.drussell23.jarvis

import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test

class JsonMiniTest {

    @Test
    fun parsesSimpleObject() {
        val out = JsonMini.parse("""{"k":"v","n":1}""") as Map<*, *>
        assertEquals("v", out["k"])
        assertEquals(1L, out["n"])
    }

    @Test
    fun parsesNestedArray() {
        @Suppress("UNCHECKED_CAST")
        val out = JsonMini.parse("""{"xs":[1,2,3]}""") as Map<String, Any?>
        val xs = out["xs"] as List<*>
        assertEquals(3, xs.size)
        assertEquals(1L, xs[0])
    }

    @Test
    fun parsesBooleansAndNull() {
        val out = JsonMini.parse("""{"a":true,"b":false,"c":null}""") as Map<*, *>
        assertEquals(true, out["a"])
        assertEquals(false, out["b"])
        assertNull(out["c"])
    }

    @Test
    fun parsesStringEscapes() {
        val out = JsonMini.parse("""{"s":"line1\nline2\ttab"}""") as Map<*, *>
        assertEquals("line1\nline2\ttab", out["s"])
    }

    @Test
    fun parsesDoubleFromDecimal() {
        val out = JsonMini.parse("""{"x":3.14}""") as Map<*, *>
        assertEquals(3.14, out["x"])
    }

    @Test
    fun rejectsTrailingGarbage() {
        assertThrows(JsonParseException::class.java) {
            JsonMini.parse("""{"k":"v"} extra""")
        }
    }

    @Test
    fun rejectsUnterminatedString() {
        assertThrows(JsonParseException::class.java) {
            JsonMini.parse("""{"k":"no end""")
        }
    }
}
