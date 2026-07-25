package com.galadriel.phonebridge.security

import android.content.Context

class TokenStore(context: Context) {
    private val preferences = context.getSharedPreferences(
        "phone_bridge_registration",
        Context.MODE_PRIVATE,
    )

    var deviceId: String?
        get() = preferences.getString("device_id", null)
        set(value) {
            preferences.edit().putString("device_id", value).apply()
        }

    var manualHost: String
        get() = preferences.getString("manual_host", "127.0.0.1") ?: "127.0.0.1"
        set(value) {
            preferences.edit().putString("manual_host", value).apply()
        }

    var manualPort: Int
        get() = preferences.getInt("manual_port", 0)
        set(value) {
            preferences.edit().putInt("manual_port", value).apply()
        }
}
