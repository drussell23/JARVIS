// JARVIS Observability — IntelliJ Platform plugin (Gap #6 Slice 6).
//
// Build with:
//     ./gradlew buildPlugin        # produces build/distributions/*.zip
//     ./gradlew runIde             # launches sandbox IntelliJ
//     ./gradlew test               # runs the Kotlin unit suite
//
// Targets all 2023.2+ JetBrains IDEs (IntelliJ IDEA, PyCharm,
// WebStorm, GoLand, Rider, RubyMine — the same ``.zip`` drops into
// any of them).

plugins {
    id("java")
    id("org.jetbrains.kotlin.jvm") version "1.9.24"
    id("org.jetbrains.intellij") version "1.17.4"
}

group = "com.drussell23"
version = "0.1.0"

repositories {
    mavenCentral()
}

dependencies {
    testImplementation("org.junit.jupiter:junit-jupiter:5.10.2")
    // kotlin.test + JUnit 4 backend — matches the standalone
    // `run_tests.sh` harness so both routes run the same test code.
    testImplementation("org.jetbrains.kotlin:kotlin-test")
    testImplementation("org.jetbrains.kotlin:kotlin-test-junit")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
}

// The IntelliJ Platform Gradle Plugin wires in the platform SDK +
// test runner + sandbox runtime. Pinning IC 2023.2.6 (stable LTS)
// makes the plugin installable on every shipping JetBrains IDE.
intellij {
    version.set("2023.2.6")
    type.set("IC") // IntelliJ Community — broadest surface.
    plugins.set(listOf<String>())
}

tasks {
    withType<org.jetbrains.kotlin.gradle.tasks.KotlinCompile> {
        kotlinOptions {
            jvmTarget = "17"
            freeCompilerArgs = freeCompilerArgs + "-Xjsr305=strict"
        }
    }

    patchPluginXml {
        sinceBuild.set("232")
        untilBuild.set("243.*")
    }

    test {
        // JUnit 4 runner picks up kotlin.test + kotlin-test-junit
        // bindings — same tests run under `./gradlew test` and the
        // standalone `bash run_tests.sh` harness.
        useJUnit()
    }

    // Disable the verification tasks that require a live IDE
    // download during plain-gradle unit-test runs. Developers can
    // still `runIde` / `buildPlugin` — those downloads happen on
    // first build. CI picks it up automatically.
    signPlugin {
        // Unsigned by default; operators can add a CLI cert later.
    }
}
