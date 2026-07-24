package com.galadriel.phonebridge

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.ui.unit.dp
import com.galadriel.phonebridge.tunnel.ControlSocket

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                var adbPort by remember { mutableStateOf("") }
                var status by remember { mutableStateOf("Disconnected") }
                var controlSocket by remember {
                    mutableStateOf<ControlSocket?>(null)
                }

                DisposableEffect(Unit) {
                    onDispose { controlSocket?.stop() }
                }

                Column(
                    modifier = Modifier
                        .fillMaxSize()
                        .padding(24.dp),
                    verticalArrangement = Arrangement.spacedBy(16.dp),
                ) {
                    Text("Phone Agent Bridge", style = MaterialTheme.typography.headlineMedium)
                    Text("Backend: ${BuildConfig.PHONE_BRIDGE_WS_URL}")
                    Text("Status: $status")
                    OutlinedTextField(
                        value = adbPort,
                        onValueChange = { value ->
                            adbPort = value.filter(Char::isDigit).take(5)
                        },
                        modifier = Modifier.fillMaxWidth(),
                        enabled = controlSocket == null,
                        label = { Text("Wireless ADB connect port") },
                        singleLine = true,
                        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
                    )
                    Button(
                        modifier = Modifier.fillMaxWidth(),
                        onClick = {
                            val running = controlSocket
                            if (running != null) {
                                running.stop()
                                controlSocket = null
                                status = "Disconnected"
                            } else {
                                val port = adbPort.toIntOrNull()
                                if (port == null || port !in 1..65535) {
                                    status = "Enter a valid ADB port"
                                } else {
                                    val socket = ControlSocket(
                                        baseUrl = BuildConfig.PHONE_BRIDGE_WS_URL,
                                        adbPort = port,
                                        onStatus = { newStatus ->
                                            runOnUiThread { status = newStatus }
                                        },
                                    )
                                    controlSocket = socket
                                    socket.start()
                                }
                            }
                        },
                    ) {
                        Text(if (controlSocket == null) "Start tunnel" else "Stop tunnel")
                    }
                    Text(
                        "Stage 1 keeps this activity open. Pair this backend with the phone " +
                            "before starting the tunnel.",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }
    }
}
