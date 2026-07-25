package com.galadriel.phonebridge.tunnel

import com.galadriel.phonebridge.adb.AdbEndpoint
import com.galadriel.phonebridge.security.DeviceIdentity
import com.galadriel.phonebridge.security.TokenStore
import java.time.Instant
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject

class ControlSocket(
    private val baseUrl: String,
    private val identity: DeviceIdentity,
    private val tokenStore: TokenStore,
    private val enrollmentCode: String?,
    private val endpointProvider: suspend () -> AdbEndpoint,
    private val onStatus: (String) -> Unit,
    private val onAuthenticated: (Long) -> Unit,
    private val onDisconnected: () -> Unit,
) {
    private val running = AtomicBoolean(false)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val client = OkHttpClient.Builder()
        .pingInterval(30, TimeUnit.SECONDS)
        .build()
    private var websocket: WebSocket? = null
    private var activeProxy: Job? = null

    fun start() {
        if (!running.compareAndSet(false, true)) return
        onStatus("Connecting to backend")
        val request = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/phone/control")
            .build()
        websocket = client.newWebSocket(request, Listener())
    }

    fun stop() {
        if (!running.compareAndSet(true, false)) return
        websocket?.close(1000, "User stopped tunnel")
        websocket = null
        activeProxy?.cancel()
        scope.cancel()
        client.dispatcher.executorService.shutdown()
        client.connectionPool.evictAll()
        onStatus("Disconnected")
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            if (!running.get()) {
                webSocket.close(1000, "Tunnel stopped")
                return
            }
            onStatus("Authenticating")
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val message = runCatching { JSONObject(text) }.getOrNull() ?: return
            when (message.optString("type")) {
                "auth_challenge" -> authenticate(webSocket, message)
                "authenticated" -> {
                    val deviceId = message.optString("device_id")
                    val expiresAt = runCatching {
                        Instant.parse(message.getString("expires_at")).toEpochMilli()
                    }.getOrNull() ?: return
                    if (deviceId.isNotBlank()) tokenStore.deviceId = deviceId
                    onAuthenticated(expiresAt)
                    onStatus("Backend connected; waiting for agent")
                }
                "open_stream" -> openStream(message)
            }
        }

        private fun authenticate(webSocket: WebSocket, message: JSONObject) {
            val nonce = message.optString("nonce")
            if (nonce.isBlank()) return
            val signature = identity.signChallenge(nonce)
            val deviceId = tokenStore.deviceId
            val response = if (deviceId == null) {
                val code = enrollmentCode
                if (code.isNullOrBlank()) {
                    onStatus("Enrollment code required")
                    webSocket.close(1008, "Enrollment code required")
                    return
                }
                JSONObject()
                    .put("type", "enroll")
                    .put("code", code)
                    .put("public_key", identity.publicKey())
                    .put("signature", signature)
            } else {
                JSONObject()
                    .put("type", "authenticate")
                    .put("device_id", deviceId)
                    .put("signature", signature)
            }
            webSocket.send(response.toString())
        }

        private fun openStream(message: JSONObject) {
            val streamId = message.optString("stream_id")
            val streamToken = message.optString("stream_token")
            if (
                streamId.isBlank() ||
                streamToken.isBlank() ||
                activeProxy?.isActive == true
            ) return
            activeProxy = scope.launch {
                onStatus("Agent connected")
                try {
                    val endpoint = endpointProvider()
                    TcpProxy(
                        client = client,
                        baseUrl = baseUrl,
                        streamId = streamId,
                        streamToken = streamToken,
                        endpoint = endpoint,
                    ).run()
                } catch (error: Exception) {
                    if (running.get()) {
                        onStatus("Tunnel error: ${error.message ?: error.javaClass.simpleName}")
                    }
                } finally {
                    if (running.get()) {
                        onStatus("Backend connected; waiting for agent")
                    }
                }
            }
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            if (running.get()) {
                onStatus("Backend disconnected")
                onDisconnected()
            }
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (running.get()) {
                onStatus("Backend error: ${t.message ?: t.javaClass.simpleName}")
                onDisconnected()
            }
        }
    }
}
