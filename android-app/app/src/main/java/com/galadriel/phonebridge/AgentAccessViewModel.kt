package com.galadriel.phonebridge

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import com.galadriel.phonebridge.security.TokenStore
import com.galadriel.phonebridge.service.TunnelService
import com.galadriel.phonebridge.service.TunnelState

class AgentAccessViewModel(application: Application) : AndroidViewModel(application) {
    private val tokenStore = TokenStore(application)

    val status = TunnelState.status
    val registered: Boolean
        get() = tokenStore.deviceId != null
    val savedManualHost: String
        get() = tokenStore.manualHost
    val savedManualPort: Int
        get() = tokenStore.manualPort

    fun enable(enrollmentCode: String?, manualHost: String, manualPort: Int) {
        TunnelService.start(
            getApplication(),
            enrollmentCode,
            manualHost,
            manualPort,
        )
    }

    fun stop() {
        TunnelService.stop(getApplication())
    }
}
