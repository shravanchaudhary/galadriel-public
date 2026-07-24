package com.galadriel.phonebridge.tunnel

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
    private val adbPort: Int,
    private val onStatus: (String) -> Unit,
) {
    private val running = AtomicBoolean(false)
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val client = OkHttpClient()
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
            webSocket.send(
                JSONObject()
                    .put("type", "hello")
                    .put("adb_available", true)
                    .toString(),
            )
            onStatus("Backend connected; waiting for agent")
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            val message = runCatching { JSONObject(text) }.getOrNull() ?: return
            if (message.optString("type") != "open_stream") return
            val streamId = message.optString("stream_id")
            if (streamId.isBlank() || activeProxy?.isActive == true) return

            activeProxy = scope.launch {
                onStatus("Agent connected")
                try {
                    TcpProxy(
                        client = client,
                        baseUrl = baseUrl,
                        streamId = streamId,
                        adbPort = adbPort,
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
            if (running.get()) onStatus("Backend disconnected")
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            if (running.get()) {
                onStatus("Backend error: ${t.message ?: t.javaClass.simpleName}")
            }
        }
    }
}
