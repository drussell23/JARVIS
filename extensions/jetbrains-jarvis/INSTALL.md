# JARVIS Observability — JetBrains plugin

Read-only IDE-integration sidebar for the JARVIS Ouroboros
governance loop. Companion to the VS Code / Cursor extension and
Sublime Text plugin — all three consume the same
schema-v1.0 HTTP GET + SSE surface.

## Compatibility

| IDE           | Versions       |
|---------------|----------------|
| IntelliJ IDEA | 2023.2 – 2024.3 |
| PyCharm       | 2023.2 – 2024.3 |
| WebStorm      | 2023.2 – 2024.3 |
| GoLand        | 2023.2 – 2024.3 |
| Rider         | 2023.2 – 2024.3 |
| RubyMine      | 2023.2 – 2024.3 |

Same `.zip` drops into any JetBrains IDE in that range via
`Settings → Plugins → ⚙ → Install Plugin from Disk...`.

## Running the agent

The plugin requires a locally running JARVIS EventChannelServer
with the Gap #6 Slice 4 graduated defaults (both flags default
`true`):

    JARVIS_IDE_OBSERVABILITY_ENABLED=true   # default on 2026-04-20
    JARVIS_IDE_STREAM_ENABLED=true          # default on 2026-04-20

The plugin refuses non-loopback endpoints. Default endpoint is
`http://127.0.0.1:8765`.

## Build from source

Requirements:

- **JDK 17** (the IntelliJ Platform Gradle Plugin 1.17.4 does not
  support JDK 25; versions 17 and 21 work).
- **Gradle 8.x** (8.11 tested). Gradle 9 has native-library loading
  issues on recent macOS builds — pin 8.11 until that resolves.
- **Internet**: first-time build downloads the IntelliJ Platform
  SDK (~1.5 GB) into `~/.gradle/caches/`.

From this directory:

    export JAVA_HOME=/opt/homebrew/opt/openjdk@17
    gradle buildPlugin

Output: `build/distributions/jarvis-observability-*.zip`.

## Install the built plugin

1. In any JetBrains IDE 2023.2+, open
   `Settings → Plugins`.
2. Click the gear icon → `Install Plugin from Disk...`.
3. Pick `build/distributions/jarvis-observability-*.zip`.
4. Restart the IDE when prompted.

The `JARVIS Ops` tool window appears on the right. Its first tab
("Ops") shows a live list of op IDs as the agent produces them;
selecting one populates the "Detail" tab with the task projection.

## Running tests

Two options:

**Gradle-free** (pure-Kotlin modules, 35 tests):

    bash run_tests.sh

Uses `kotlinc` + bundled `kotlin-test-junit` JARs + JUnit 4
downloaded to `./.deps/` on first run. No IntelliJ SDK needed.

**Full suite** (including platform-dependent `OpsControllerTest`):

    export JAVA_HOME=/opt/homebrew/opt/openjdk@17
    gradle test

This is the first run's "expensive" path — downloads the IntelliJ
Platform SDK the first time, then runs incrementally on subsequent
invocations.

## Plugin descriptor validation

The IntelliJ Platform Gradle Plugin ships a `verifyPlugin` task:

    gradle verifyPlugin

Validates `plugin.xml` against the target platform version set in
`build.gradle.kts`.

## Developing against a sandbox IDE

    gradle runIde

Launches a sandbox IntelliJ Community IDE with the plugin
preloaded. Useful for manual smoke testing.

## Troubleshooting

- **"Gradle could not start your build"**: usually a JDK mismatch.
  Make sure `JAVA_HOME` points at JDK 17 (not JDK 25 or newer).
- **"libnative-platform.dylib failed to load"**: a known Gradle 9
  bug on macOS aarch64. Pin Gradle 8.11 until resolved.
- **Plugin loads but tool window empty**: check
  `JARVIS.sublime-settings`-equivalent — wait, wrong plugin. For
  JetBrains, open `Settings → Tools → JARVIS Observability` and
  confirm the endpoint + enabled toggle. The endpoint must be
  loopback.

## Authority invariant

This plugin is a read-only consumer. Every HTTP request is `GET`.
The SSE stream is unidirectional (server → client). The plugin
never POSTs, PUTs, DELETEs, or PATCHes anything to the agent —
this mirrors the server-side Manifesto §1 Boundary Principle on
the client side.

Grep-pinned by the test suite:

    $ bash run_tests.sh
    ...
    OK (35 tests)
