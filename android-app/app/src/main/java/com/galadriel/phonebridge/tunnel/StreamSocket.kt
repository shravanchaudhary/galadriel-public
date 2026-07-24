package com.galadriel.phonebridge.tunnel

import java.io.IOException
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.channels.Channel
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString

class StreamSocket(
    client: OkHttpClient,
    baseUrl: String,
    streamId: String,
) {
    private val opened = CompletableDeferred<Unit>()
    private val incoming = Channel<ByteArray>(capacity = 16)
    private val websocket: WebSocket

    init {
        val request = Request.Builder()
            .url("${baseUrl.trimEnd('/')}/phone/stream/$streamId")
            .build()
        websocket = client.newWebSocket(request, Listener())
    }

    suspend fun awaitOpen() {
        opened.await()
    }

    fun send(bytes: ByteArray) {
        if (!websocket.send(bytes.toByteString())) {
            throw IOException("Stream WebSocket send queue is closed")
        }
    }

    suspend fun receive(): ByteArray? = incoming.receiveCatching().getOrNull()

    fun close() {
        websocket.close(1000, "ADB stream closed")
        incoming.close()
    }

    private inner class Listener : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            opened.complete(Unit)
        }

        override fun onMessage(webSocket: WebSocket, bytes: ByteString) {
            if (incoming.trySend(bytes.toByteArray()).isFailure) {
                webSocket.cancel()
            }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            webSocket.close(code, reason)
        }

        override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
            incoming.close()
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            opened.completeExceptionally(t)
            incoming.close(t)
        }
    }
}
