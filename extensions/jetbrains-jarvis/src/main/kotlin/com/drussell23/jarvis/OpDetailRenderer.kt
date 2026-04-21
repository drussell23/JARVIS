package com.drussell23.jarvis

/**
 * Pure HTML renderer for [TaskDetail] — the Swing UI side in
 * [OpDetailPanel] embeds this output in a `JEditorPane`.
 *
 * Extracted as a top-level object so the graduation test suite can
 * exercise the layout without booting the IntelliJ Platform. The
 * matching VS Code + Sublime renderers use the same class-name
 * conventions so a shared stylesheet / screenshot diff is feasible
 * in a future slice.
 *
 * Security: no user input is interpolated without HTML escaping.
 * The only script execution surface in Swing's HTML view is
 * `javascript:` URLs which we never emit. Still, the `escapeHtml`
 * helper is a cheap belt-and-suspenders.
 */
object OpDetailRenderer {

    /**
     * Swap-in point for the tool window wiring. The
     * [OpDetailPanel] installs a closure here that updates its
     * `JEditorPane`; tests install a capture double.
     */
    @Volatile
    var sink: (TaskDetail) -> Unit = { /* default no-op */ }

    fun render(detail: TaskDetail) {
        sink(detail)
    }

    fun renderToHtml(detail: TaskDetail): String {
        val closedBadge = if (detail.closed) {
            "<span class=\"badge closed\">CLOSED</span>"
        } else {
            "<span class=\"badge open\">LIVE</span>"
        }
        val active = detail.activeTaskId?.let {
            " &middot; active: <code>${escapeHtml(it)}</code>"
        } ?: ""

        val body = StringBuilder()
        body.append("<html><head><style>\n")
        body.append(
            """
            body { font-family: sans-serif; }
            h1   { font-size: 12pt; margin: 0 0 6px 0; }
            .meta { color: #888; font-size: 9pt; }
            .badge { padding: 1px 8px; border-radius: 6px; font-size: 8pt;
                     font-weight: bold; margin-left: 6px; }
            .badge.open   { background: #3b7; color: #000; }
            .badge.closed { background: #c44; color: #fff; }
            table { border-collapse: collapse; margin-top: 10px; width: 100%; }
            th, td { padding: 4px 8px; text-align: left; font-size: 10pt; }
            th { border-bottom: 1px solid #666; }
            tr.state-in_progress td { background: rgba(60, 120, 200, 0.15); }
            tr.state-completed   td { background: rgba(60, 170, 90, 0.15); }
            tr.state-cancelled   td { background: rgba(190, 60, 60, 0.15); }
            .chip { padding: 1px 6px; border-radius: 4px; font-size: 8pt;
                    font-weight: bold; text-transform: uppercase; }
            .chip.pending     { background: #d8b442; color: #000; }
            .chip.in_progress { background: #3b7cc3; color: #fff; }
            .chip.completed   { background: #3bab5a; color: #000; }
            .chip.cancelled   { background: #c4545a; color: #fff; }
            .cancel-reason { font-style: italic; color: #c88; }
            """.trimIndent()
        )
        body.append("</style></head><body>\n")
        body.append("<h1>${escapeHtml(detail.opId)} $closedBadge</h1>\n")
        body.append(
            "<div class=\"meta\">${detail.boardSize} task" +
                (if (detail.boardSize == 1) "" else "s") + active + "</div>\n"
        )

        if (detail.tasks.isEmpty()) {
            body.append("<p><em>No tasks yet.</em></p>\n")
        } else {
            body.append("<table>\n")
            body.append(
                "<tr><th>#</th><th>ID</th><th>State</th>" +
                    "<th>Title</th><th>Body</th></tr>\n"
            )
            for (task in detail.tasks) {
                val stateClass = "state-${task.state.wire}"
                val chip =
                    "<span class=\"chip ${task.state.wire}\">" +
                        "${task.state.wire}</span>"
                body.append("<tr class=\"$stateClass\">")
                body.append("<td>${task.sequence}</td>")
                body.append("<td><code>${escapeHtml(task.taskId)}</code></td>")
                body.append("<td>$chip</td>")
                body.append("<td>${escapeHtml(task.title)}</td>")
                body.append("<td>${escapeHtml(task.body)}</td>")
                body.append("</tr>\n")
                if (task.cancelReason.isNotEmpty()) {
                    body.append(
                        "<tr class=\"$stateClass\"><td></td>" +
                            "<td colspan=\"4\" class=\"cancel-reason\">" +
                            "reason: ${escapeHtml(task.cancelReason)}" +
                            "</td></tr>\n"
                    )
                }
            }
            body.append("</table>\n")
        }

        body.append("</body></html>")
        return body.toString()
    }

    fun escapeHtml(s: String): String = buildString(s.length) {
        for (c in s) {
            when (c) {
                '&' -> append("&amp;")
                '<' -> append("&lt;")
                '>' -> append("&gt;")
                '"' -> append("&quot;")
                '\'' -> append("&#39;")
                else -> append(c)
            }
        }
    }
}
