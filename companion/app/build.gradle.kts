plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.androidagent.companion"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.androidagent.companion"
        // 26 keeps the service APIs used here available while still
        // covering effectively every device in use.
        minSdk = 26
        targetSdk = 34
        versionCode = 1
        versionName = "0.1"
    }

    buildTypes {
        // Debug only, deliberately: this is sideloaded by its owner, never
        // published, and a debug signature keeps installs upgradeable
        // without a keystore in CI.
        getByName("debug") { isMinifyEnabled = false }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
}
