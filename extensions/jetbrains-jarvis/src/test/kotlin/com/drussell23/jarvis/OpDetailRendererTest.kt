package com.drussell23.jarvis

import org.junit.jupiter.api.Assertions.*
import org.junit.jupiter.api.Test

class OpDetailRendererTest {

    private fun detail(
        closed: Boolean = false,
        activeTaskId: String? = null,
        tasks: List<TaskProjection> = emptyList(),
    ): TaskDetail = TaskDetail(
        schemaVersion = "1.0",
        opId = "op-xyz",
        closed = closed,
        activeTaskId = activeTaskId,
        tasks = tasks,
        boardSize = tasks.size,
    )

    private fun task(
        id: String, state: TaskState, title: String = "t",
        body: String = "", seq: Int = 1, cancel: String = "",
    ): TaskProjection = TaskProjection(
        taskId = id, state = state, title = title, body = body,
        sequence = seq, cancelReason = cancel,
    )

    @Test
    fun rendersOpIdAndLiveBadge() {
        val html = OpDetailRenderer.renderToHtml(detail())
        assertTrue("op-xyz" in html)
        assertTrue("LIVE" in html)
        assertFalse("CLOSED" in html)
    }

    @Test
    fun rendersClosedBadgeWhenClosed() {
        val html = OpDetailRenderer.renderToHtml(detail(closed = true))
        assertTrue("CLOSED" in html)
    }

    @Test
    fun rendersAllFourTaskStates() {
        val html = OpDetailRenderer.renderToHtml(
            detail(
                tasks = listOf(
                    task("t-1", TaskState.PENDING, title = "a", seq = 1),
                    task("t-2", TaskState.IN_PROGRESS, title = "b", seq = 2),
                    task("t-3", TaskState.COMPLETED, title = "c", seq = 3),
                    task("t-4", TaskState.CANCELLED, title = "d", seq = 4, cancel = "bye"),
                )
            )
        )
        for (wire in listOf("pending", "in_progress", "completed", "cancelled")) {
            assertTrue(wire in html, "expected state '$wire' in rendered HTML")
        }
        assertTrue("reason: bye" in html, "cancel reason must render")
    }

    @Test
    fun rendersActiveTaskWhenPresent() {
        val html = OpDetailRenderer.renderToHtml(
            detail(activeTaskId = "task-active-01")
        )
        assertTrue("task-active-01" in html)
        assertTrue("active:" in html)
    }

    @Test
    fun rendersEmptyStateWhenNoTasks() {
        val html = OpDetailRenderer.renderToHtml(detail())
        assertTrue("No tasks yet" in html)
    }

    @Test
    fun escapeHtmlCoversAllFiveEntities() {
        assertEquals(
            "&lt;&gt;&amp;&quot;&#39;",
            OpDetailRenderer.escapeHtml("<>&\"'"),
        )
    }

    @Test
    fun renderToHtmlEscapesInjectionInTitle() {
        val html = OpDetailRenderer.renderToHtml(
            detail(
                tasks = listOf(
                    task("t-1", TaskState.PENDING, title = "<script>alert(1)</script>"),
                )
            )
        )
        // Raw script must not appear in the output.
        assertFalse("<script>alert(1)</script>" in html)
        // Escaped form must.
        assertTrue("&lt;script&gt;alert(1)&lt;/script&gt;" in html)
    }

    @Test
    fun renderToHtmlEscapesInjectionInCancelReason() {
        val html = OpDetailRenderer.renderToHtml(
            detail(
                tasks = listOf(
                    task(
                        "t-1", TaskState.CANCELLED,
                        title = "ok", cancel = "<b>bad</b>",
                    )
                )
            )
        )
        assertFalse("<b>bad</b>" in html)
        assertTrue("&lt;b&gt;bad&lt;/b&gt;" in html)
    }

    @Test
    fun rendererSinkSwappableForTests() {
        var captured: TaskDetail? = null
        val originalSink = OpDetailRenderer.sink
        try {
            OpDetailRenderer.sink = { captured = it }
            val d = detail()
            OpDetailRenderer.render(d)
            assertNotNull(captured)
            assertEquals("op-xyz", captured!!.opId)
        } finally {
            OpDetailRenderer.sink = originalSink
        }
    }
}
