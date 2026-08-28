// JARVIS Observability — settings.
//
// The IntelliJ Platform Gradle Plugin's version is declared HERE and nowhere
// else. JetBrains' own guidance for 2.x is that the version declaration belongs
// in the settings script; repeating it in build.gradle.kts is what produces the
// "plugin already on the classpath with a different version" class of Gradle
// compatibility error. build.gradle.kts therefore applies the plugin with no
// version of its own.
//
// Tooling versions ARE literals, deliberately. gradle.properties holds the
// values that describe the PRODUCT (which platform, which build range, which
// JDK); the versions of the tools that read that file have to be fixed before
// anything can read anything.

import org.jetbrains.intellij.platform.gradle.extensions.intellijPlatform

plugins {
    id("org.jetbrains.intellij.platform.settings") version "2.18.1"
}

rootProject.name = "jarvis-observability"

dependencyResolutionManagement {
    // PREFER_SETTINGS, not FAIL_ON_PROJECT_REPOS: the stricter mode turns a
    // stray project-level `repositories {}` — in this build or in any future
    // subproject — into a hard build failure rather than an override notice.
    // The repository set is still centralised here; this only decides how
    // loudly a deviation is punished.
    repositoriesMode = RepositoriesMode.PREFER_SETTINGS

    repositories {
        mavenCentral()

        // Resolves the IntelliJ Platform artifacts, the plugin marketplace,
        // and the JetBrains Runtime. `defaultRepositories()` is the supported
        // seam for this — the individual URLs are JetBrains' to change, and
        // hardcoding them here is how a build breaks on someone else's
        // infrastructure migration.
        intellijPlatform {
            defaultRepositories()
        }
    }
}
