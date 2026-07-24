# Phone Agent Bridge (Stage 1)

This debug-only proof keeps the activity open and proxies one backend WebSocket
stream to the phone's local Wireless ADB connect port.

1. Add the backend URL to `~/.gradle/gradle.properties`:

   ```properties
   PHONE_BRIDGE_WS_URL=ws://YOUR_BACKEND_LAN_IP:8765
   ```

2. Start Galadriel with:

   ```bash
   PHONE_BRIDGE_ENABLED=1 python main.py
   ```

3. Pair the backend's ADB client with the phone normally, install the debug APK,
   enter the phone's current Wireless ADB connect port, and tap **Start tunnel**.
4. Run:

   ```bash
   adb connect 127.0.0.1:37000
   adb -s 127.0.0.1:37000 shell echo hello
   ```

The default backend URL (`ws://10.0.2.2:8765`) is suitable only for an Android
emulator. Cleartext WebSockets are allowed only in debug builds.
