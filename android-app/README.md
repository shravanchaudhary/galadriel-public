# Phone Agent Bridge

The Android companion creates an authenticated, user-authorized tunnel from the
agent's localhost ADB listener to Wireless ADB on the phone. Access runs as a
visible foreground service and expires after 60 minutes.

## One-time setup

1. Enable Developer Options and Wireless Debugging.
2. Pair the backend ADB identity with **Pair device with pairing code**:

   ```bash
   adb pair PHONE_IP:PAIRING_PORT
   ```

3. Open Tower's `/phone-bridge` page and generate an 8-digit enrollment code.
4. Open the app, enter the code, and tap **Enable agent access**. The app creates
   a non-exportable P-256 identity in Android Keystore. The enrollment code is
   single-use and expires after 10 minutes.

The app discovers `_adb-tls-connect._tcp.` for every new stream. If discovery is
unavailable, set a host and current Wireless ADB connect port under **Advanced**.

## Local debug

Set the backend URL in `~/.gradle/gradle.properties`:

```properties
PHONE_BRIDGE_WS_URL=ws://YOUR_BACKEND_LAN_IP:8765
```

Start the backend and build:

```bash
PHONE_BRIDGE_ENABLED=1 PHONE_TOOLS_ENABLED=1 python main.py
./gradlew assembleDebug
```

After enabling access in the app:

```bash
adb connect 127.0.0.1:37000
adb -s 127.0.0.1:37000 shell echo hello
```

The default backend URL (`ws://10.0.2.2:8765`) is suitable only for an Android
emulator. Cleartext WebSockets are allowed only in debug builds.

## Production release

Build with the tenant's WSS host:

```bash
./gradlew assembleRelease \
  -PPHONE_BRIDGE_WSS_URL=wss://TENANT.replika.clodexa.com
```

Production routes `/phone/*` through the HTTPS ALB to container port `8765`.
Port `37000` remains localhost-only. The backend ADB key and enrolled device
records persist on tenant storage; tunnel payloads, challenges, and stream
tokens are never persisted.

The user can stop access from the app or permanent notification. Revoking a
device from `/phone-bridge` closes its control session and requires a new
enrollment code before it can reconnect. If Wireless ADB trust is revoked in
Android settings, pair the persistent backend identity again.
