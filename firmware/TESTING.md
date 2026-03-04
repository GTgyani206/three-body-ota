# Phase 7 — Integration Testing Checklist

## Prerequisites

Before testing, ensure you have:
- [ ] ESP32 connected via USB (`/dev/ttyUSB0`)
- [ ] WiFi credentials configured in `sdkconfig.defaults`
- [ ] Your laptop's LAN IP noted (e.g., `192.168.1.100`)
- [ ] Server URLs updated in `sdkconfig.defaults` to point to your laptop

---

## Step 1: Start Infrastructure

### 1.1 Start Mosquitto Broker
```bash
cd ~/esp/three-body-ota
docker compose up -d
```

Verify it's running:
```bash
docker ps | grep mosquitto
# Should show: eclipse-mosquitto running on port 1883
```

### 1.2 Start FastAPI Backend
```bash
cd ~/esp/three-body-ota/backend-and-dash
pip install -r requirements.txt  # if not done
export ADMIN_TOKEN="test-token"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Verify backend is up:
```bash
curl http://localhost:8000/health
# Should return: {"status": "healthy", ...}
```

---

## Step 2: Flash Base Firmware (v1)

### 2.1 Set Firmware Version
Edit `firmware/CMakeLists.txt`:
```cmake
cmake_minimum_required(VERSION 3.16)
set(PROJECT_VER "1.0.0")  # Add this line
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(three_body_ota)
```

### 2.2 Build and Flash
```bash
cd ~/esp/three-body-ota/firmware
idf.py fullclean
idf.py build
idf.py -p /dev/ttyUSB0 flash monitor
```

### 2.3 Verify Base Firmware
You should see:
```
I MAIN: === Three-Body OTA Firmware ===
I MAIN: Device ID: FC:E8:C0:E1:7E:54
I MAIN: Firmware Version: 1.0.0
I WIFI: Got IP: 192.168.x.x
I MAIN: MQTT_EVENT_CONNECTED to broker
I MAIN: Subscribed to firmware/update
```

---

## Step 3: Test MQTT Subscription

From another terminal, publish a test message:
```bash
mosquitto_pub -h localhost -t firmware/update -m '{"test": "hello"}'
```

ESP32 serial should show:
```
I MAIN: MQTT_EVENT_DATA
I MAIN: TOPIC=firmware/update
E MAIN: Missing/invalid fields in OTA JSON message
```

This confirms MQTT is working!

---

## Step 4: Build v2 Firmware for OTA

### 4.1 Change Version
Edit `firmware/CMakeLists.txt`:
```cmake
set(PROJECT_VER "2.0.0")  # Change from 1.0.0
```

### 4.2 Build (Don't Flash!)
```bash
cd ~/esp/three-body-ota/firmware
idf.py build
```

The binary is at: `build/three_body_ota.bin`

### 4.3 Generate Signing Metadata (if using Rust CLI)
```bash
cd ~/esp/three-body-ota/cli-tool
cargo run -- sign \
  --file ../firmware/build/three_body_ota.bin \
  --version 2.0.0 \
  --key firmware_key.secret \
  --output metadata_v2.json
```

Or manually compute SHA-256:
```bash
sha256sum ~/esp/three-body-ota/firmware/build/three_body_ota.bin
# Copy the hash
```

---

## Step 5: Upload v2 to Backend

### Option A: With Rust CLI (Signed)
```bash
curl -X POST http://localhost:8000/upload-firmware/ \
  -H "X-Admin-Token: test-token" \
  -F "firmware=@../firmware/build/three_body_ota.bin" \
  -F "metadata=$(cat metadata_v2.json)"
```

### Option B: Manual Upload (Unsigned)
If not using signature verification:
```bash
# Get file size
stat --printf="%s" ~/esp/three-body-ota/firmware/build/three_body_ota.bin

# Get SHA-256
sha256sum ~/esp/three-body-ota/firmware/build/three_body_ota.bin

# Upload
curl -X POST http://localhost:8000/upload-firmware/ \
  -H "X-Admin-Token: test-token" \
  -F "firmware=@../firmware/build/three_body_ota.bin" \
  -F 'metadata={"version":"2.0.0","file_name":"firmware_v2.bin","file_size_bytes":925488,"sha256_hash":"<paste-hash-here>","signing_alg":"ed25519","signature":"dummy"}'
```

Verify upload:
```bash
curl http://localhost:8000/firmware/
# Should list version 2.0.0
```

---

## Step 6: Trigger OTA (Happy Path)

The backend automatically publishes to MQTT when firmware is uploaded.
If it didn't, manually trigger:

```bash
mosquitto_pub -h localhost -t firmware/update -m '{
  "version": "2.0.0",
  "file_name": "firmware_v2.bin",
  "file_size_bytes": 925488,
  "sha256_hash": "<paste-sha256-here>"
}'
```

### Expected ESP32 Behavior:
```
I MAIN: OTA Trigger Received!
I MAIN:  - Version: 2.0.0
I OTA_HANDLER: Starting OTA for version: 2.0.0
I OTA_HANDLER: Download URL: http://192.168.x.x:8000/firmware/2.0.0/download
I OTA_HANDLER: Downloaded X of Y bytes
I OTA_HANDLER: Calculated SHA-256: abc123...
I OTA_HANDLER: OTA Success! Rebooting in 3 seconds...

<reboot>

W MAIN: Running in PENDING_VERIFY state — self-test required
I MAIN: Reboot count: 1 / 3
I WIFI: Got IP: 192.168.x.x
I MAIN: MQTT_EVENT_CONNECTED to broker
I MAIN: ========================================
I MAIN:   SELF-TEST PASSED — COMMITTING OTA
I MAIN: ========================================
I MAIN: Firmware Version: 2.0.0
```

**Verify version changed from 1.0.0 to 2.0.0!**

---

## Step 7: Test Rollback (Broken Firmware)

### 7.1 Create Intentionally Broken Firmware
Edit `sdkconfig.defaults` with a WRONG WiFi SSID:
```
CONFIG_ESP_WIFI_SSID="WRONG_NETWORK_NAME"
```

Set version to 3.0.0 in CMakeLists.txt.

### 7.2 Build Broken Firmware
```bash
cd ~/esp/three-body-ota/firmware
idf.py build
```

### 7.3 Upload v3 (Broken) to Backend
Same process as Step 5, but with version 3.0.0.

### 7.4 Trigger OTA

### 7.5 Expected Rollback Behavior
```
I OTA_HANDLER: OTA Success! Rebooting in 3 seconds...

<reboot into v3.0.0>

W MAIN: Running in PENDING_VERIFY state — self-test required
I MAIN: Reboot count: 1 / 3
W WIFI: Disconnected. Retrying connection (1/10)...
W WIFI: Disconnected. Retrying connection (2/10)...
...
E WIFI: Failed to connect after 10 attempts
E MAIN: WiFi connection failed
E MAIN: Self-test FAILED (WiFi). Rebooting...

<reboot — count 2>
<same failure>

<reboot — count 3>
E MAIN: Max reboot count reached! Triggering rollback...

<bootloader rolls back to v2.0.0>

I MAIN: Firmware Version: 2.0.0  ← Back to working version!
```

### 7.6 Restore Correct WiFi
Don't forget to fix `sdkconfig.defaults` back to correct credentials!

---

## Step 8: Verify Status on MQTT

Subscribe to status topics in a separate terminal:
```bash
mosquitto_sub -h localhost -t "firmware/status" -t "device/+/status" -v
```

You should see:
- `firmware/status` messages on commit
- `device/{MAC}/status` heartbeats every 30 seconds

---

## Success Criteria

- [x] ESP32 connects to WiFi and MQTT
- [x] ESP32 receives OTA trigger via MQTT
- [x] ESP32 downloads firmware via HTTP
- [x] ESP32 verifies SHA-256 hash
- [x] ESP32 reboots into new firmware
- [x] Self-test passes → firmware is committed
- [x] Broken firmware triggers automatic rollback
- [x] Device recovers to last known-good version
- [x] Status messages appear on MQTT topics

---

## Troubleshooting

### "Connection refused" on HTTP download
- Check `CONFIG_FIRMWARE_SERVER_URL` in sdkconfig
- Ensure backend is running with `--host 0.0.0.0`
- Check firewall: `sudo ufw allow 8000`

### SHA-256 mismatch
- Ensure you're using the correct hash from the built binary
- Don't rebuild after generating the hash

### MQTT won't connect
- Check `CONFIG_MQTT_BROKER_URL` in sdkconfig
- Ensure Mosquitto is running: `docker compose ps`
- Test: `mosquitto_pub -h <ip> -t test -m "hello"`

### Rollback not happening
- Ensure `otadata` partition was flashed (should happen on first flash)
- Check boot log for partition info
- Verify `esp_ota_mark_app_invalid_rollback_and_reboot()` is being called
