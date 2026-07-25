package com.galadriel.phonebridge.adb

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.net.wifi.WifiManager
import java.net.InetAddress
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withTimeout

data class AdbEndpoint(
    val host: InetAddress,
    val port: Int,
)

class AdbDiscovery(context: Context) {
    companion object {
        private const val SERVICE_TYPE = "_adb-tls-connect._tcp."
        private const val DISCOVERY_TIMEOUT_MILLIS = 10_000L
    }

    private val nsdManager = context.getSystemService(NsdManager::class.java)
    private val wifiManager = context.applicationContext.getSystemService(
        WifiManager::class.java,
    )

    suspend fun discover(): AdbEndpoint = withTimeout(DISCOVERY_TIMEOUT_MILLIS) {
        val multicastLock = wifiManager.createMulticastLock("phone-bridge-adb-discovery")
        multicastLock.setReferenceCounted(false)
        multicastLock.acquire()
        try {
            discoverOnce()
        } finally {
            if (multicastLock.isHeld) multicastLock.release()
        }
    }

    private suspend fun discoverOnce(): AdbEndpoint = suspendCancellableCoroutine { continuation ->
        val finished = AtomicBoolean(false)
        lateinit var listener: NsdManager.DiscoveryListener

        fun stopDiscovery() {
            if (finished.compareAndSet(false, true)) {
                runCatching { nsdManager.stopServiceDiscovery(listener) }
            }
        }

        listener = object : NsdManager.DiscoveryListener {
            override fun onDiscoveryStarted(serviceType: String) = Unit

            override fun onServiceFound(serviceInfo: NsdServiceInfo) {
                if (finished.get()) return
                nsdManager.resolveService(
                    serviceInfo,
                    object : NsdManager.ResolveListener {
                        override fun onResolveFailed(
                            serviceInfo: NsdServiceInfo,
                            errorCode: Int,
                        ) = Unit

                        override fun onServiceResolved(serviceInfo: NsdServiceInfo) {
                            if (continuation.isActive) {
                                stopDiscovery()
                                continuation.resume(
                                    AdbEndpoint(serviceInfo.host, serviceInfo.port),
                                )
                            }
                        }
                    },
                )
            }

            override fun onServiceLost(serviceInfo: NsdServiceInfo) = Unit

            override fun onDiscoveryStopped(serviceType: String) = Unit

            override fun onStartDiscoveryFailed(serviceType: String, errorCode: Int) {
                stopDiscovery()
                if (continuation.isActive) {
                    continuation.resumeWithException(
                        IllegalStateException("ADB discovery failed: $errorCode"),
                    )
                }
            }

            override fun onStopDiscoveryFailed(serviceType: String, errorCode: Int) = Unit
        }

        continuation.invokeOnCancellation { stopDiscovery() }
        nsdManager.discoverServices(
            SERVICE_TYPE,
            NsdManager.PROTOCOL_DNS_SD,
            listener,
        )
    }
}
