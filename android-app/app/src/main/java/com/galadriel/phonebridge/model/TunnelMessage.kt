package com.galadriel.phonebridge.model

data class OpenStream(
    val streamId: String,
    val streamToken: String,
)

data class TunnelStatus(
    val enabled: Boolean = false,
    val backendStatus: String = "Disconnected",
    val agentStatus: String = "Disconnected",
    val expiresAtMillis: Long? = null,
    val error: String? = null,
)
