// JARVIS Observability — IntelliJ Platform plugin (Gap #6 Slice 6).
//
// Build with:
//     ./gradlew buildPlugin        # produces build/distributions/*.zip
//     ./gradlew runIde             # launches sandbox IntelliJ
//     ./gradlew test               # runs the Kotlin unit suite
//
// Migrated 2026-08-27 from the Gradle IntelliJ Plugin 1.x
// (`org.jetbrains.intellij` 1.17.4) to the IntelliJ Platform Gradle
// Plugin 2.x (`org.jetbrains.intellij.platform`).
//
// The 1.x plugin called `org.gradle.api.internal.plugins`
// `.DefaultArtifactPublicationSet` — a Gradle INTERNAL class that Gradle 9
// removed — so it died at plugin application, before any task existed, with
// "Type ... not present" and "Found 0 tasks". 1.x is no longer developed and
// will never support Gradle 9, so pinning an older Gradle would only have
// deferred this. Versions live in gradle.properties; see the note there about
// why nothing in this build was pinned before.

import org.jetbrains.intellij.platform.gradle.TestFrameworkType
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("java")
    id("org.jetbrains.kotlin.jvm") version "2.4.10"
    // Version intentionally absent — declared once in settings.gradle.kts.
    id("org.jetbrains.intellij.platform")
}

group = providers.gradleProperty("pluginGroup").get()
version = providers.gradleProperty("pluginVersion").get()

// Repositories are centralised in settings.gradle.kts
// (dependencyResolutionManagement). Declaring them here would override that.

dependencies {
    intellijPlatform {
        // The platform to compile against, read from gradle.properties so the
        // target is data rather than script. `create(type, version)` takes the
        // type as a value, which keeps "which IDE" configurable instead of
        // hard-selecting a helper like intellijIdeaCommunity().
        create(
            providers.gradleProperty("platformType"),
            providers.gradleProperty("platformVersion"),
        )

        // The platform test fixtures. In 2.x the test framework is an explicit
        // dependency; 1.x wired it in implicitly, which is why the old file
        // never mentioned it.
        testFramework(TestFrameworkType.Platform)
    }

    testImplementation("org.junit.jupiter:junit-jupiter:5.10.2")
    // kotlin.test + JUnit 4 backend — matches the standalone
    // `run_tests.sh` harness so both routes run the same test code.
    testImplementation("org.jetbrains.kotlin:kotlin-test")
    testImplementation("org.jetbrains.kotlin:kotlin-test-junit")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.8.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-core:1.8.1")
}

intellijPlatform {
    pluginConfiguration {
        ideaVersion {
            sinceBuild = providers.gradleProperty("pluginSinceBuild")

            // Empty property => no untilBuild => no artificial upper bound.
            // `orNull` on a blank value keeps the range open rather than
            // emitting an empty attribute the platform would reject.
            untilBuild = providers.gradleProperty("pluginUntilBuild")
                .map { it.trim() }
                .filter { it.isNotEmpty() }
        }
    }
}

// Provision the JDK rather than inherit whatever `java` is on PATH. This is the
// defect that made the failure machine-dependent: the Mac supplied JDK 25, the
// Windows box Temurin 21, and the build had an opinion about neither.
val jdk = providers.gradleProperty("javaVersion").get().toInt()

java {
    toolchain {
        languageVersion = JavaLanguageVersion.of(jdk)
    }
}

kotlin {
    // `compilerOptions`, not `kotlinOptions`: the latter is deprecated as of
    // Kotlin 2.0 and is the other half of why this build could not move to a
    // toolchain that Gradle 9 accepts.
    compilerOptions {
        jvmTarget = JvmTarget.fromTarget(jdk.toString())
        freeCompilerArgs.add("-Xjsr305=strict")
    }
}

tasks {
    test {
        // JUnit 4 runner picks up kotlin.test + kotlin-test-junit
        // bindings — same tests run under `./gradlew test` and the
        // standalone `bash run_tests.sh` harness.
        useJUnit()
    }
}
