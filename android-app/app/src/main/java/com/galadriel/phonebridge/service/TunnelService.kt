package com.galadriel.phonebridge.service

import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.IBinder
import androidx.core.app.NotificationCompat
import com.galadriel.phonebridge.BuildConfig
import com.galadriel.phonebridge.MainActivity
import com.galadriel.phonebridge.adb.AdbDiscovery
import com.galadriel.phonebridge.adb.AdbEndpoint
import com.galadriel.phonebridge.model.TunnelStatus
import com.galadriel.phonebridge.security.DeviceIdentity
import com.galadriel.phonebridge.security.TokenStore
import com.galadriel.phonebridge.tunnel.ControlSocket
import java.net.InetAddress
import java.util.concurrent.atomic.AtomicBoolean
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

object TunnelState {
    private val mutableStatus = MutableStateFlow(TunnelStatus())
    val status: StateFlow<TunnelStatus> = mutableStatus.asStateFlow()

    fun update(value: TunnelStatus) {
        mutableStatus.value = value
    }
}

class TunnelService : Service() {
    companion object {
        const val ACTION_START = "com.galadriel.phonebridge.START"
        const val ACTION_STOP = "com.galadriel.phonebridge.STOP"
        const val EXTRA_ENROLLMENT_CODE = "enrollment_code"
        const val EXTRA_MANUAL_HOST = "manual_host"
        const val EXTRA_MANUAL_PORT = "manual_port"
        private const val CHANNEL_ID = "phone_bridge_access"
        private const val NOTIFICATION_ID = 7001
        private const val SESSION_MILLIS = 60 * 60 * 1000L

        fun start(
            context: Context,
            enrollmentCode: String?,
            manualHost: String,
            manualPort: Int,
        ) {
            val intent = Intent(context, TunnelService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_ENROLLMENT_CODE, enrollmentCode)
                .putExtra(EXTRA_MANUAL_HOST, manualHost)
                .putExtra(EXTRA_MANUAL_PORT, manualPort)
            context.startForegroundService(intent)
        }

        fun stop(context: Context) {
            context.startService(
                Intent(context, TunnelService::class.java).setAction(ACTION_STOP),
            )
        }
    }

    private val running = AtomicBoolean(false)
    private var scope = newScope()
    private var controlSocket: ControlSocket? = null
    private var expiryJob: Job? = null

    override fun onCreate() {
        super.onCreate()
        createNotificationChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> stopTunnel("Access stopped")
            ACTION_START -> {
                startForeground(NOTIFICATION_ID, buildNotification())
                if (running.compareAndSet(false, true)) {
                    if (!scope.coroutineContext[Job]!!.isActive) scope = newScope()
                    startTunnel(intent)
                }
            }
        }
        return START_NOT_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        stopTunnel("Access stopped")
        super.onDestroy()
    }

    private fun startTunnel(intent: Intent) {
        val tokenStore = TokenStore(this)
        val enrollmentCode = intent.getStringExtra(EXTRA_ENROLLMENT_CODE)
        val manualHost = intent.getStringExtra(EXTRA_MANUAL_HOST)
            ?.trim()
            .orEmpty()
            .ifBlank { tokenStore.manualHost }
        val manualPort = intent.getIntExtra(EXTRA_MANUAL_PORT, tokenStore.manualPort)
        if (manualPort in 1..65535) {
            tokenStore.manualHost = manualHost
            tokenStore.manualPort = manualPort
        }

        val localExpiresAt = System.currentTimeMillis() + SESSION_MILLIS
        TunnelState.update(
            TunnelStatus(
                enabled = true,
                backendStatus = "Connecting",
                expiresAtMillis = localExpiresAt,
            ),
        )
        scheduleExpiry(localExpiresAt)

        val discovery = AdbDiscovery(this)
        controlSocket = ControlSocket(
            baseUrl = BuildConfig.PHONE_BRIDGE_WS_URL,
            identity = DeviceIdentity(),
            tokenStore = tokenStore,
            enrollmentCode = enrollmentCode,
            endpointProvider = {
                discoverEndpoint(discovery, manualHost, manualPort)
            },
            onStatus = { status ->
                val current = TunnelState.status.value
                TunnelState.update(
                    current.copy(
                        backendStatus = status,
                        agentStatus = if (status == "Agent connected") {
                            "Connected"
                        } else {
                            "Disconnected"
                        },
                    ),
                )
            },
            onAuthenticated = { backendExpiresAt ->
                val expiresAt = minOf(localExpiresAt, backendExpiresAt)
                TunnelState.update(
                    TunnelState.status.value.copy(expiresAtMillis = expiresAt),
                )
                scheduleExpiry(expiresAt)
            },
            onDisconnected = { stopTunnel("Backend disconnected") },
        ).also { it.start() }
    }

    private suspend fun discoverEndpoint(
        discovery: AdbDiscovery,
        manualHost: String,
        manualPort: Int,
    ): AdbEndpoint {
        var failure: Throwable? = null
        repeat(2) { attempt ->
            try {
                return discovery.discover()
            } catch (error: CancellationException) {
                throw error
            } catch (error: Exception) {
                failure = error
                if (attempt == 0) delay(500)
            }
        }
        if (manualPort in 1..65535) {
            return AdbEndpoint(InetAddress.getByName(manualHost), manualPort)
        }
        throw failure ?: IllegalStateException("Wireless ADB discovery failed")
    }

    private fun scheduleExpiry(expiresAt: Long) {
        expiryJob?.cancel()
        expiryJob = scope.launch {
            delay((expiresAt - System.currentTimeMillis()).coerceAtLeast(0))
            stopTunnel("Session expired")
        }
    }

    private fun stopTunnel(reason: String) {
        if (!running.compareAndSet(true, false)) {
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
            return
        }
        expiryJob?.cancel()
        expiryJob = null
        controlSocket?.stop()
        controlSocket = null
        scope.cancel()
        TunnelState.update(TunnelStatus(error = reason))
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun newScope() = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private fun createNotificationChannel() {
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                "Agent phone access",
                NotificationManager.IMPORTANCE_LOW,
            ),
        )
    }

    private fun buildNotification() = NotificationCompat.Builder(this, CHANNEL_ID)
        .setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
        .setContentTitle("Agent currently has ADB access")
        .setContentText("Tap Stop to disconnect")
        .setOngoing(true)
        .setContentIntent(
            PendingIntent.getActivity(
                this,
                0,
                Intent(this, MainActivity::class.java),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            ),
        )
        .addAction(
            android.R.drawable.ic_menu_close_clear_cancel,
            "Stop",
            PendingIntent.getService(
                this,
                1,
                Intent(this, TunnelService::class.java).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            ),
        )
        .build()
}
