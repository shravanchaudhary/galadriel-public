package com.galadriel.phonebridge.tunnel

import java.net.InetSocketAddress
import java.net.Socket
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.launch
import kotlinx.coroutines.selects.select
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient

class TcpProxy(
    private val client: OkHttpClient,
    private val baseUrl: String,
    private val streamId: String,
    private val adbPort: Int,
) {
    suspend fun run() = withContext(Dispatchers.IO) {
        Socket().use { adbSocket ->
            adbSocket.tcpNoDelay = true
            adbSocket.connect(InetSocketAddress("127.0.0.1", adbPort), 5_000)

            val stream = StreamSocket(client, baseUrl, streamId)
            try {
                stream.awaitOpen()
                coroutineScope {
                    val adbToBackend = launch {
                        val buffer = ByteArray(64 * 1024)
                        val input = adbSocket.getInputStream()
                        while (true) {
                            val count = input.read(buffer)
                            if (count < 0) break
                            stream.send(buffer.copyOf(count))
                        }
                    }
                    val backendToAdb = launch {
                        val output = adbSocket.getOutputStream()
                        while (true) {
                            val bytes = stream.receive() ?: break
                            output.write(bytes)
                            output.flush()
                        }
                    }

                    select {
                        adbToBackend.onJoin { }
                        backendToAdb.onJoin { }
                    }
                    adbSocket.close()
                    stream.close()
                    adbToBackend.cancelAndJoin()
                    backendToAdb.cancelAndJoin()
                }
            } finally {
                stream.close()
            }
        }
    }
}
