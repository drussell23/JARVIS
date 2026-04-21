package com.drussell23.jarvis

import kotlin.random.Random

/**
 * Full-jitter exponential backoff — identical math to the Sublime
 * and VS Code consumers. Takes the consecutive-failure count and
 * returns the next sleep duration in milliseconds.
 *
 *   raw = base * 2 ^ (failures - 1)
 *   capped = min(raw, maxMs)
 *   result = floor(random() * capped)
 *
 * Exported from its own module so the unit test can exercise the
 * math without booting a real consumer.
 */
object Backoff {
    const val BASE_MS: Long = 500L

    fun compute(
        consecutiveFailures: Int,
        maxMs: Long,
        jitter: () -> Double = { Random.nextDouble() },
    ): Long {
        if (consecutiveFailures <= 0) return 0L
        val raw = BASE_MS shl (consecutiveFailures - 1).coerceAtMost(62)
        val capped = if (raw < 0 || raw > maxMs) maxMs else raw
        val j = jitter().coerceIn(0.0, 1.0)
        return (capped * j).toLong()
    }
}
