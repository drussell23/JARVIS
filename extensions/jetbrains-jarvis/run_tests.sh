#!/usr/bin/env bash
# Standalone test runner for the pure-Kotlin modules.
#
# Bypasses Gradle + the IntelliJ Platform SDK download (~GB) and runs
# the 35 unit tests that don't depend on the IDE platform. Uses
# kotlin.test + kotlin-test-junit (JUnit 4 binding) shipped with the
# `kotlin` Homebrew formula.
#
# Requirements (local dev):
#   brew install kotlin            # provides kotlinc + bundled jars
#   /opt/homebrew/opt/openjdk      # any JDK 17+ available to `java`
#   junit 4.13 + hamcrest-core 1.3 downloaded under ./.deps/
#     (or set DEPS_DIR env var to point elsewhere)
#
# For full platform tests (OpsController, tool window, settings),
# use Gradle:
#     ./gradlew test
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

DEPS_DIR="${DEPS_DIR:-$SCRIPT_DIR/.deps}"
KTLIB="${KTLIB:-/opt/homebrew/opt/kotlin/libexec/lib}"
JAVA="${JAVA:-/opt/homebrew/opt/openjdk/bin/java}"
KOTLINC="${KOTLINC:-kotlinc}"

if [[ ! -f "$KTLIB/kotlin-stdlib.jar" ]]; then
  echo "error: cannot find kotlin libs at $KTLIB" >&2
  echo "       set KTLIB or install via 'brew install kotlin'" >&2
  exit 2
fi

mkdir -p "$DEPS_DIR"

fetch() {
  local url="$1" out="$2"
  if [[ ! -f "$DEPS_DIR/$out" ]]; then
    echo "fetching $out"
    curl -sL -o "$DEPS_DIR/$out" "$url"
  fi
}
fetch https://repo1.maven.org/maven2/junit/junit/4.13.2/junit-4.13.2.jar junit-4.13.2.jar
fetch https://repo1.maven.org/maven2/org/hamcrest/hamcrest-core/1.3/hamcrest-core-1.3.jar hamcrest-core-1.3.jar

JUNIT_CP="$DEPS_DIR/junit-4.13.2.jar:$DEPS_DIR/hamcrest-core-1.3.jar"
COROUTINES_JAR="$KTLIB/kotlinx-coroutines-core-jvm.jar"
OUT="$SCRIPT_DIR/build/test-out"
rm -rf "$OUT" && mkdir -p "$OUT"

echo "==> compiling main sources"
"$KOTLINC" -cp "$COROUTINES_JAR" -d "$OUT" \
  src/main/kotlin/com/drussell23/jarvis/ApiTypes.kt \
  src/main/kotlin/com/drussell23/jarvis/JsonMini.kt \
  src/main/kotlin/com/drussell23/jarvis/SseParser.kt \
  src/main/kotlin/com/drussell23/jarvis/Backoff.kt \
  src/main/kotlin/com/drussell23/jarvis/OpDetailRenderer.kt

echo "==> compiling test sources"
"$KOTLINC" -cp "$OUT:$KTLIB/kotlin-test.jar:$KTLIB/kotlin-test-junit.jar:$JUNIT_CP" -d "$OUT" \
  src/test/kotlin/com/drussell23/jarvis/ApiTypesTest.kt \
  src/test/kotlin/com/drussell23/jarvis/JsonMiniTest.kt \
  src/test/kotlin/com/drussell23/jarvis/SseParserTest.kt \
  src/test/kotlin/com/drussell23/jarvis/BackoffTest.kt \
  src/test/kotlin/com/drussell23/jarvis/OpDetailRendererTest.kt

echo "==> running JUnit 4 suite"
RUN_CP="$OUT:$KTLIB/kotlin-stdlib.jar:$KTLIB/kotlin-test.jar:$KTLIB/kotlin-test-junit.jar:$COROUTINES_JAR:$JUNIT_CP"
"$JAVA" -cp "$RUN_CP" org.junit.runner.JUnitCore \
  com.drussell23.jarvis.ApiTypesTest \
  com.drussell23.jarvis.JsonMiniTest \
  com.drussell23.jarvis.SseParserTest \
  com.drussell23.jarvis.BackoffTest \
  com.drussell23.jarvis.OpDetailRendererTest
