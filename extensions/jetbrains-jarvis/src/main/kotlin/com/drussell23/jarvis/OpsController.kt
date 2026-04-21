package com.drussell23.jarvis

import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import java.util.Collections
import java.util.concurrent.atomic.AtomicReference

/**
 * Bridge between the UI (tool window / actions) and the
 * [ObservabilityClient] + [StreamConsumer]. Owns the consumer
 * lifecycle, fans stream events back to the UI via
 * [onOpsChanged], and exposes a suspend-free API to IntelliJ
 * actions that must run on the EDT.
 *
 * Thread safety:
 *   * [opIds] is a copy-on-write list (mutations through assign).
 *   * UI callback [onOpsChanged] is invoked on whatever thread
 *     fired the change — the factory is expected to invokeLater.
 *
 * Lifecycle:
 *   * [start] — launches the SSE consumer + immediate refresh.
 *   * [stop] — stops the consumer and drops cached state.
 */
class OpsController(
    private val settings: JarvisSettings,
    private val onOpsChanged: (List<String>) -> Unit,
    private val clientFactory: (String) -> ObservabilityClient = { ep ->
        ObservabilityClient(ep)
    },
    private val streamFactory: (String, String?, Boolean, Long) -> StreamConsumer =
        { ep, filter, reconnect, maxBackoff ->
            StreamConsumer(
                endpoint = ep,
                opIdFilter = if (filter.isNullOrEmpty()) null else filter,
                autoReconnect = reconnect,
                reconnectMaxBackoffMs = maxBackoff,
            )
        },
) {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val consumerRef = AtomicReference<StreamConsumer?>(null)
    private var opIds: List<String> = Collections.emptyList()
    private val lock = Any()

    fun isRunning(): Boolean = consumerRef.get() != null

    fun snapshot(): List<String> = synchronized(lock) { opIds }

    fun start() {
        if (consumerRef.get() != null) return
        val consumer = streamFactory(
            settings.endpoint,
            settings.opIdFilter,
            settings.autoReconnect,
            settings.reconnectMaxBackoffMs,
        )
        consumer.onEvent(::applyStreamEvent)
        consumerRef.set(consumer)
        consumer.start()
        scope.launch { refreshAsync() }
    }

    fun stop() {
        val c = consumerRef.getAndSet(null) ?: return
        scope.launch { c.stop() }
    }

    /**
     * Force a re-fetch of the task list. Called from the Refresh
     * action AND from [applyStreamEvent] on a `stream_lag` frame.
     */
    fun refresh() {
        scope.launch { refreshAsync() }
    }

    private suspend fun refreshAsync() {
        try {
            val client = clientFactory(settings.endpoint)
            val list = client.taskList()
            updateOpIds(list.opIds)
        } catch (_: ObservabilityException) {
            // silent — caller surfaces via logger elsewhere
        } catch (_: SchemaMismatchException) {
            // ditto
        }
    }

    /**
     * Open a dedicated detail view for [opId]. Delegated to the
     * renderer so the controller itself stays UI-free.
     */
    fun openOp(opId: String) {
        try {
            validateOpId(opId)
        } catch (_: ObservabilityException) {
            return
        }
        scope.launch {
            try {
                val client = clientFactory(settings.endpoint)
                val detail = client.taskDetail(opId)
                OpDetailRenderer.render(detail)
            } catch (_: ObservabilityException) {
                // ignore — action provides user feedback elsewhere
            } catch (_: SchemaMismatchException) {
                // ditto
            }
        }
    }

    /**
     * Apply an incoming SSE event to the cached op list.
     * Exported (non-private) so the graduation test suite can
     * drive it with synthetic events.
     */
    fun applyStreamEvent(event: StreamEvent) {
        if (event.eventType.isControlEvent) {
            if (event.eventType == StreamEventType.STREAM_LAG) {
                refresh()
            }
            return
        }
        if (event.eventType.isTaskEvent) {
            synchronized(lock) {
                if (event.opId !in opIds) {
                    opIds = opIds + event.opId
                    onOpsChanged(opIds)
                }
            }
        }
    }

    private fun updateOpIds(newList: List<String>) {
        val snapshot: List<String>
        synchronized(lock) {
            opIds = newList
            snapshot = opIds
        }
        onOpsChanged(snapshot)
    }
}

/**
 * Placeholder — real implementation would open a tool-window
 * content tab or an editor preview. Exposed as an object so
 * tests can swap it for a capture-on-call double.
 */
object OpDetailRenderer {
    @Volatile
    var sink: (TaskDetail) -> Unit = { /* plugin.xml wiring */ }

    fun render(detail: TaskDetail) {
        sink(detail)
    }
}
