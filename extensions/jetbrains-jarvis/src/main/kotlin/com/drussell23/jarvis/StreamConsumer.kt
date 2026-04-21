package com.drussell23.jarvis

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.HttpURLConnection
import java.net.URI
import java.net.URLEncoder
import java.nio.charset.StandardCharsets

enum class StreamState { DISCONNECTED, CONNECTING, CONNECTED, RECONNECTING, ERROR, CLOSED }

typealias StreamListener = (StreamEvent) -> Unit
typealias StreamStateListener = (StreamState) -> Unit

/**
 * Coroutine-driven SSE consumer.
 *
 * Mirrors the Sublime + VS Code lifecycle: start() returns
 * immediately; a supervisor-scoped launch runs the reconnect loop.
 * stop() cancels the job and suspends until it terminates.
 *
 * Listeners are invoked on [Dispatchers.IO]; callers that need to
 * touch the IntelliJ EDT must hop via
 * `ApplicationManager.getApplication().invokeLater { }`.
 */
class StreamConsumer(
    private val endpoint: String,
    private val opIdFilter: String? = null,
    private val autoReconnect: Boolean = true,
    private val reconnectMaxBackoffMs: Long = 30_000L,
    private val logger: (String) -> Unit = { },
    private val connectionFactory: (String) -> HttpURLConnection = { url ->
        (URI(url).toURL().openConnection() as HttpURLConnection)
    },
    private val jitter: () -> Double = { kotlin.random.Random.nextDouble() },
    private val sleepMs: suspend (Long) -> Unit = { ms ->
        if (ms > 0L) delay(ms) else Unit
    },
) {
    init {
        ObservabilityClient.validateLoopback(endpoint)
        opIdFilter?.let(::validateOpId)
    }

    private val listeners = mutableListOf<StreamListener>()
    private val stateListeners = mutableListOf<StreamStateListener>()
    private val lock = Any()
    private var job: Job? = null
    private var scope: CoroutineScope? = null

    @Volatile private var state: StreamState = StreamState.DISCONNECTED
    @Volatile private var lastEventId: String? = null
    @Volatile private var consecutiveFailures: Int = 0

    fun getState(): StreamState = state

    fun onEvent(l: StreamListener): () -> Unit {
        synchronized(lock) { listeners.add(l) }
        return { synchronized(lock) { listeners.remove(l) } }
    }

    fun onState(l: StreamStateListener): () -> Unit {
        synchronized(lock) { stateListeners.add(l) }
        return { synchronized(lock) { stateListeners.remove(l) } }
    }

    fun start() {
        synchronized(lock) {
            if (job != null && job!!.isActive) return
            val cs = CoroutineScope(SupervisorJob() + Dispatchers.IO)
            scope = cs
            job = cs.launch { runLoop() }
        }
    }

    suspend fun stop() {
        transition(StreamState.CLOSED)
        val j = synchronized(lock) { job }
        j?.cancel()
        try {
            j?.join()
        } catch (_: CancellationException) {
            // expected
        }
        synchronized(lock) { job = null; scope = null }
    }

    private fun transition(next: StreamState) {
        if (state == next) return
        state = next
        val snap = synchronized(lock) { stateListeners.toList() }
        for (l in snap) {
            try {
                l(next)
            } catch (exc: Exception) {
                logger("[stream] state listener threw: ${exc.message}")
            }
        }
    }

    private fun dispatch(event: StreamEvent) {
        val snap = synchronized(lock) { listeners.toList() }
        for (l in snap) {
            try {
                l(event)
            } catch (exc: Exception) {
                logger("[stream] listener threw for ${event.eventType}: ${exc.message}")
            }
        }
    }

    private suspend fun runLoop() {
        while (scope?.isActive == true) {
            try {
                transition(
                    if (consecutiveFailures == 0) StreamState.CONNECTING
                    else StreamState.RECONNECTING,
                )
                connectAndStream()
                consecutiveFailures = 0
            } catch (_: CancellationException) {
                return
            } catch (exc: Exception) {
                if (scope?.isActive != true) return
                consecutiveFailures += 1
                logger("[stream] dropped: ${exc.message} (failures=$consecutiveFailures)")
                transition(StreamState.ERROR)
                if (!autoReconnect) return
            }
            if (!autoReconnect || scope?.isActive != true) return
            sleepMs(Backoff.compute(consecutiveFailures, reconnectMaxBackoffMs, jitter))
        }
    }

    private suspend fun connectAndStream() = withContext(Dispatchers.IO) {
        val base = endpoint.trimEnd('/')
        val path = buildString {
            append("/observability/stream")
            if (!opIdFilter.isNullOrEmpty()) {
                append("?op_id=")
                append(URLEncoder.encode(opIdFilter, StandardCharsets.UTF_8))
            }
        }
        val conn = connectionFactory("$base$path")
        try {
            conn.requestMethod = "GET"
            conn.setRequestProperty("Accept", "text/event-stream")
            conn.setRequestProperty("Cache-Control", "no-store")
            lastEventId?.let { conn.setRequestProperty("Last-Event-ID", it) }
            conn.useCaches = false
            val status = conn.responseCode
            if (status != 200) {
                throw ObservabilityException(
                    "stream returned $status", status = status,
                )
            }
            transition(StreamState.CONNECTED)
            consumeBody(conn)
        } finally {
            try { conn.disconnect() } catch (_: Exception) { /* swallow */ }
        }
    }

    private suspend fun consumeBody(conn: HttpURLConnection) {
        val reader = BufferedReader(
            InputStreamReader(conn.inputStream, StandardCharsets.UTF_8)
        )
        val buf = StringBuilder()
        reader.use { r ->
            val chunk = CharArray(4096)
            while (scope?.isActive == true) {
                val n = r.read(chunk)
                if (n < 0) return
                buf.append(chunk, 0, n)
                while (true) {
                    val idx = buf.indexOf("\n\n")
                    if (idx < 0) break
                    val raw = buf.substring(0, idx)
                    buf.delete(0, idx + 2)
                    val parsed = SseParser.parse(raw) ?: continue
                    lastEventId = parsed.eventId
                    dispatch(parsed)
                }
            }
        }
    }
}
