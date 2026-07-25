package com.galadriel.phonebridge

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.TextButton
import androidx.compose.material3.Text
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import kotlinx.coroutines.delay

class MainActivity : ComponentActivity() {
    private val viewModel: AgentAccessViewModel by viewModels()
    private val notificationPermission = registerForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) {}

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                val tunnelStatus by viewModel.status.collectAsState()
                var enrollmentCode by remember { mutableStateOf("") }
                var advanced by remember { mutableStateOf(false) }
                var manualHost by remember {
                    mutableStateOf(viewModel.savedManualHost)
                }
                var manualPort by remember {
                    mutableStateOf(
                        viewModel.savedManualPort.takeIf { it > 0 }?.toString().orEmpty(),
                    )
                }
                var now by remember { mutableStateOf(System.currentTimeMillis()) }
                var formError by remember { mutableStateOf<String?>(null) }
                val wirelessEnabled = remember(tunnelStatus.enabled) {
                    runCatching {
                        Settings.Global.getInt(
                            contentResolver,
                            "adb_wifi_enabled",
                            0,
                        ) == 1
                    }.getOrDefault(false)
                }

                LaunchedEffect(tunnelStatus.enabled) {
                    while (tunnelStatus.enabled) {
                        now = System.currentTimeMillis()
                        delay(1_000)
                    }
                }
                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(24.dp),
                    verticalArrangement = Arrangement.spacedBy(16.dp),
                ) {
                    Text("Phone Agent Bridge", style = MaterialTheme.typography.headlineMedium)
                    Text("Wireless debugging: ${if (wirelessEnabled) "Enabled" else "Disabled"}")
                    TextButton(
                        onClick = {
                            val wirelessDebugging = Intent(
                                "android.settings.WIRELESS_DEBUGGING_SETTINGS",
                            )
                            val intent = if (
                                wirelessDebugging.resolveActivity(packageManager) != null
                            ) {
                                wirelessDebugging
                            } else {
                                Intent(Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS)
                            }
                            startActivity(intent)
                        },
                    ) {
                        Text("Open Wireless Debugging settings")
                    }
                    Text("Backend: ${tunnelStatus.backendStatus}")
                    Text("Agent: ${tunnelStatus.agentStatus}")
                    tunnelStatus.expiresAtMillis?.let { expiresAt ->
                        val remaining = ((expiresAt - now).coerceAtLeast(0) / 1_000)
                        Text("Session expires in: ${remaining / 60}:${(remaining % 60).toString().padStart(2, '0')}")
                    }
                    if (!viewModel.registered && !tunnelStatus.enabled) {
                        OutlinedTextField(
                            value = enrollmentCode,
                            onValueChange = {
                                enrollmentCode = it.filter(Char::isDigit).take(8)
                            },
                            modifier = Modifier.fillMaxWidth(),
                            label = { Text("8-digit enrollment code") },
                            singleLine = true,
                            keyboardOptions = KeyboardOptions(
                                keyboardType = KeyboardType.Number,
                            ),
                        )
                    }
                    if (!tunnelStatus.enabled) {
                        TextButton(onClick = { advanced = !advanced }) {
                            Text(if (advanced) "Hide Advanced" else "Advanced")
                        }
                    }
                    if (advanced && !tunnelStatus.enabled) {
                        OutlinedTextField(
                            value = manualHost,
                            onValueChange = { manualHost = it.trim().take(255) },
                            modifier = Modifier.fillMaxWidth(),
                            label = { Text("Manual ADB host fallback") },
                            singleLine = true,
                        )
                        OutlinedTextField(
                            value = manualPort,
                            onValueChange = {
                                manualPort = it.filter(Char::isDigit).take(5)
                            },
                            modifier = Modifier.fillMaxWidth(),
                            label = { Text("Manual ADB port fallback") },
                            singleLine = true,
                            keyboardOptions = KeyboardOptions(
                                keyboardType = KeyboardType.Number,
                            ),
                        )
                    }
                    Button(
                        modifier = Modifier.fillMaxWidth(),
                        onClick = {
                            if (tunnelStatus.enabled) {
                                viewModel.stop()
                            } else {
                                if (!viewModel.registered && enrollmentCode.length != 8) {
                                    formError = "Enter the 8-digit enrollment code"
                                } else {
                                    formError = null
                                    requestNotificationPermission()
                                    viewModel.enable(
                                        enrollmentCode.takeIf { !viewModel.registered },
                                        manualHost,
                                        manualPort.toIntOrNull() ?: 0,
                                    )
                                }
                            }
                        },
                    ) {
                        Text(
                            if (tunnelStatus.enabled) {
                                "Stop agent access"
                            } else {
                                "Enable agent access"
                            },
                        )
                    }
                    (formError ?: tunnelStatus.error)?.let { Text(it) }
                }
            }
        }
    }

    private fun requestNotificationPermission() {
        if (
            android.os.Build.VERSION.SDK_INT >= 33 &&
            ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS,
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }
}
