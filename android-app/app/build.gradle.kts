plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val debugPhoneBridgeWsUrl = providers.gradleProperty("PHONE_BRIDGE_WS_URL")
    .orElse("ws://10.0.2.2:8765")
    .get()
val releasePhoneBridgeWsUrl = providers.gradleProperty("PHONE_BRIDGE_WSS_URL")
    .orElse("wss://configure-me.invalid")
    .get()

android {
    namespace = "com.galadriel.phonebridge"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.galadriel.phonebridge"
        minSdk = 30
        targetSdk = 34
        versionCode = 2
        versionName = "1.0"

    }

    buildFeatures {
        buildConfig = true
        compose = true
    }

    composeOptions {
        kotlinCompilerExtensionVersion = "1.5.8"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildTypes {
        debug {
            buildConfigField(
                "String",
                "PHONE_BRIDGE_WS_URL",
                "\"${debugPhoneBridgeWsUrl.replace("\"", "\\\"")}\"",
            )
        }
        release {
            require(releasePhoneBridgeWsUrl.startsWith("wss://")) {
                "PHONE_BRIDGE_WSS_URL must use wss://"
            }
            buildConfigField(
                "String",
                "PHONE_BRIDGE_WS_URL",
                "\"${releasePhoneBridgeWsUrl.replace("\"", "\\\"")}\"",
            )
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2024.02.02")
    implementation(composeBom)
    androidTestImplementation(composeBom)

    implementation("androidx.activity:activity-compose:1.8.2")
    implementation("androidx.core:core-ktx:1.12.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.7.0")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    debugImplementation("androidx.compose.ui:ui-tooling")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
    testImplementation("junit:junit:4.13.2")
}
