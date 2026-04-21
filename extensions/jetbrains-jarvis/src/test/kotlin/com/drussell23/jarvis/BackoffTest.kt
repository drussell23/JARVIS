package com.drussell23.jarvis

import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test

class BackoffTest {

    @Test
    fun zeroFailuresIsZero() {
        assertEquals(0L, Backoff.compute(0, 30_000L) { 0.5 })
    }

    @Test
    fun firstFailureUsesBaseTimesJitter() {
        // failures=1 → raw=500; jitter=0.5 → 250
        assertEquals(250L, Backoff.compute(1, 30_000L) { 0.5 })
    }

    @Test
    fun backoffMonotoneNonDecreasing() {
        val a = Backoff.compute(2, 30_000L) { 0.5 }
        val b = Backoff.compute(3, 30_000L) { 0.5 }
        assertTrue(b >= a, "b=$b a=$a")
    }

    @Test
    fun backoffRespectsMaxCap() {
        val capped = Backoff.compute(20, 1_000L) { 1.0 }
        assertTrue(capped <= 1_000L, "capped=$capped")
    }

    @Test
    fun jitterClampedToUnitInterval() {
        // Jitter > 1 should be clamped to 1.
        val out = Backoff.compute(1, 30_000L) { 99.0 }
        assertEquals(500L, out)
    }
}
