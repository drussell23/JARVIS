package com.drussell23.jarvis

import com.intellij.openapi.components.PersistentStateComponent
import com.intellij.openapi.components.Service
import com.intellij.openapi.components.State
import com.intellij.openapi.components.Storage
import com.intellij.openapi.components.service
import com.intellij.openapi.options.Configurable
import javax.swing.JComponent
import javax.swing.JPanel

/**
 * Persistent settings for the JARVIS observability plugin.
 *
 * Stored in `jarvis-observability.xml` under the IDE config dir.
 * Mirrors the Sublime + VS Code plugins' option surfaces.
 */
@Service(Service.Level.APP)
@State(
    name = "JarvisObservabilitySettings",
    storages = [Storage("jarvis-observability.xml")]
)
class JarvisSettings : PersistentStateComponent<JarvisSettings.State> {

    data class State(
        var endpoint: String = "http://127.0.0.1:8765",
        var enabled: Boolean = true,
        var autoReconnect: Boolean = true,
        var reconnectMaxBackoffMs: Long = 30_000L,
        var opIdFilter: String = "",
    )

    private var inner: State = State()

    override fun getState(): State = inner

    override fun loadState(state: State) {
        inner = state
    }

    var endpoint: String
        get() = inner.endpoint
        set(v) { inner.endpoint = v }

    var enabled: Boolean
        get() = inner.enabled
        set(v) { inner.enabled = v }

    var autoReconnect: Boolean
        get() = inner.autoReconnect
        set(v) { inner.autoReconnect = v }

    var reconnectMaxBackoffMs: Long
        get() = inner.reconnectMaxBackoffMs
        set(v) { inner.reconnectMaxBackoffMs = v }

    var opIdFilter: String
        get() = inner.opIdFilter
        set(v) { inner.opIdFilter = v }

    companion object {
        fun getInstance(): JarvisSettings = service()
    }
}

/**
 * Minimal IntelliJ Settings page. Expands over time; for Slice 6
 * we ship the endpoint field + enabled toggle and leave the
 * advanced knobs as keyboard-edited XML (ops who care know where
 * to look).
 */
class JarvisSettingsConfigurable : Configurable {
    private var panel: JPanel? = null

    override fun getDisplayName(): String = "JARVIS Observability"

    override fun createComponent(): JComponent {
        // A real-world panel would use a com.intellij.ui.dsl.builder
        // FormBuilder; we keep the scaffold minimal here because the
        // critical authority invariants are in the non-UI modules.
        val p = JPanel()
        panel = p
        return p
    }

    override fun isModified(): Boolean = false

    override fun apply() {
        // no-op until the form is wired
    }
}
