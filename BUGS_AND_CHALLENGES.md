# Bugs & Challenges — Three-Body OTA

A living log of significant bugs hit during development and the exact fixes applied.

---

## Bug 1: Flash Size Mismatch — Partitions Wouldn't Fit

**Phase:** 3 (Partition Table)  
**Severity:** 🔴 Build-Blocking

### Symptom
```
FAILED: partition check
partition table (3 app partitions) exceeds flash size (2MB)
```

### Root Cause
ESP-IDF defaults to 2MB flash size. Our custom `partition.csv` defines two 1.5MB OTA slots, which requires 4MB. The default `sdkconfig` shadow config was silently overriding `sdkconfig.defaults`.

### Fix
Added explicit flash size config to `sdkconfig.defaults`:
```ini
CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y
CONFIG_ESPTOOLPY_FLASHSIZE="4MB"
```
Then forced a full rebuild:
```bash
rm -rf build sdkconfig && idf.py build
```

---

## Bug 2: Flash Speed Too High — `Unable to verify flash chip connection`

**Phase:** 4 (First real flash attempt)  
**Severity:** 🔴 Hardware-Blocking

### Symptom
```
A fatal error occurred: Unable to verify flash chip connection
(No serial data received.)
```
This happened when using the default `idf.py flash` command (460800 baud).

### Root Cause
Some ESP32 DEVKIT V1 boards are unstable at 460800 baud during the flash verification phase, particularly with certain USB-UART bridge chips.

### Fix
Use `esptool.py` directly at 115200 baud:
```bash
python -m esptool --chip esp32 -p /dev/ttyUSB0 -b 115200 --before default_reset --after hard_reset write_flash \
  --flash_mode dio --flash_size 4MB --flash_freq 40m \
  0x1000 build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin \
  0xd000 build/ota_data_initial.bin \
  0x10000 build/three_body_ota.bin
```

---

## Bug 3: ESP32 and Laptop on Different Network Subnets

**Phase:** 7 (Integration Testing)  
**Severity:** 🟠 Runtime-Blocking

### Symptom
```
E (17362) esp-tls: [sock=54] select() timeout
E (17362) transport_base: Failed to open a new connection: 32774
E (17362) mqtt_client: Error transport connect
```
ESP32 connects to WiFi successfully but MQTT times out.

### Root Cause
The laptop had two network interfaces:
- `wlp1s0` (WiFi): `172.25.178.107/19` — connected to the "P3" hotspot originally  
- After reconnection: `10.22.88.26/24`

The ESP32 got IP `10.22.88.87` (on the `10.22.88.x` subnet), but the firmware was configured with the stale `172.25.178.107` address. The two subnets are not routable to each other.

### Fix
Always verify both devices are on the same subnet before flashing:
```bash
ip addr show wlp1s0 | grep "inet "
```
Then update `firmware/sdkconfig.defaults`:
```ini
CONFIG_FIRMWARE_SERVER_URL="http://10.22.88.26:8000"
CONFIG_MQTT_BROKER_URL="mqtt://10.22.88.26:1883"
```
And rebuild cleanly:
```bash
rm -rf build sdkconfig && idf.py build
```

---

## Bug 4: MQTT ACL Silently Blocking OTA Trigger

**Phase:** 7 (Integration Testing)  
**Severity:** 🔴 Silent Failure — Hardest to Debug

### Symptom
```bash
docker exec three-body-mqtt mosquitto_pub -h localhost -t "firmware/update" -m '...'
# Exit code: 0 — no error!
```
But ESP32 never receives the message. Broker logs show:
```
New client connected from 127.0.0.1:xxxxx as auto-XXXXXXXX
Client auto-XXXXXXXX disconnected.
```
No acknowledgement of publish.

### Root Cause
The Mosquitto ACL file (`mosquitto/config/aclfile`) restricts write access to `firmware/#` to only the `three-body-backend` user:
```
user three-body-backend
topic write firmware/#
```
Anonymous clients (no `-u` flag) are **silently denied** — `mosquitto_pub` returns exit code 0 anyway, making this nearly invisible.

### Fix
Always publish with `-u three-body-backend`:
```bash
sudo docker exec three-body-mqtt mosquitto_pub \
  -h localhost \
  -u three-body-backend \
  -t "firmware/update" \
  -m '{"version":"2.0.0",...}'
```
Also updated `QUICKSTART.md` Step 7 with this flag.

---

## Bug 5: OTA HTTP Download Timeout at 10 Seconds

**Phase:** 7 (Integration Testing)  
**Severity:** 🔴 OTA-Blocking

### Symptom
```
E (2050931) OTA_HANDLER: HTTP read error
E (2050931) OTA_HANDLER: OTA validation failed — aborting
```
Log appeared ~10 seconds into download. Backend served the file correctly (200 OK, `content-length: 924672`).

### Root Cause
`esp_http_client_config_t.timeout_ms` was set to `10000` (10 seconds). Downloading ~900KB over WiFi at typical embedded speeds takes 15-25 seconds, so the connection was timing out mid-transfer.

### Fix
In `firmware/main/ota_handler.c`:
```c
// Before:
.timeout_ms = 10000,

// After:
.timeout_ms = 30000,
```
Also added explicit buffer size config for more stable transfers:
```c
.buffer_size = 4096,
.buffer_size_tx = 1024,
```

---

## Bug 6: Empty `$SHA256` / `$SIZE` Shell Variables in MQTT Publish

**Phase:** 7 (Integration Testing)  
**Severity:** 🟠 Silent Failure

### Symptom
OTA MQTT message published, broker confirms delivery, ESP32 receives it, but:
```
E (XXX) MAIN: Malformed OTA JSON: missing version/sha256_hash/file_size_bytes
```

### Root Cause
`$SHA256` and `$SIZE` were set in one terminal session but the `mosquitto_pub` command was run in a different terminal (or a new shell spawned by `sudo`). Variables don't cross terminal sessions.

### Fix
Always set variables and publish in a single command chain:
```bash
SHA256=$(sha256sum build/three_body_ota.bin | cut -d' ' -f1)
SIZE=$(stat --format=%s build/three_body_ota.bin)
sudo docker exec three-body-mqtt mosquitto_pub \
  -h localhost \
  -u three-body-backend \
  -t "firmware/update" \
  -m "{\"version\":\"X.Y.Z\",\"sha256_hash\":\"${SHA256}\",\"file_size_bytes\":${SIZE}}"
```

---

## Bug 7: Backend Auto-Publish Race Condition

**Phase:** 7 (Integration Testing)  
**Severity:** 🟡 Timing Issue

### Symptom
After `POST /upload-firmware/` returns `"mqtt_published": true`, the ESP32 never receives the OTA command.

### Root Cause
The backend publishes to MQTT immediately on upload. If the ESP32 rebooted recently and hasn't reconnected to MQTT yet, it misses the QoS 0 publish. The firmware subscribes ~8-12 seconds after boot.

### Workaround
After upload, wait for the ESP32's heartbeat to confirm MQTT is connected, then manually trigger:
```bash
sudo docker exec three-body-mqtt mosquitto_pub \
  -h localhost \
  -u three-body-backend \
  -t "firmware/update" \
  -m "{\"version\":\"X.Y.Z\",\"sha256_hash\":\"${SHA256}\",\"file_size_bytes\":${SIZE}}"
```
**Long-term fix:** Backend should use QoS 1 with retained messages so the device receives it on reconnect.

---

## Challenge 1: `sdkconfig` Caching Overrides `sdkconfig.defaults`

**Severity:** 🟠 Confusing Build Behavior

When `sdkconfig` already exists (from a previous build), ESP-IDF ignores `sdkconfig.defaults`. Changing `.defaults` alone has no effect.

**Solution:** Always do:
```bash
rm -rf build sdkconfig && idf.py build
```
whenever `sdkconfig.defaults` changes.

---

## Challenge 2: Port Busy When Flashing After Monitoring

**Severity:** 🟡 Minor Annoyance

```
[Errno 11] Could not exclusively lock port /dev/ttyUSB0
```
Happens when the serial monitor is still attached.

**Solution:**
```bash
sudo fuser -k /dev/ttyUSB0
```

---

## Challenge 3: Mosquitto ACL Blocks Anonymous Reads Too

The ESP32's MQTT client connects anonymously. It needs to read from `firmware/#` to receive OTA commands. This is explicitly allowed by the ACL pattern:
```
pattern read firmware/#
```
But anonymous *writes* to `firmware/#` are blocked (which is intentional security).

---

## Summary Table

| # | Bug | Trigger | Fix |
|---|-----|---------|-----|
| 1 | Partitions don't fit flash | Custom A/B layout > 2MB default | `CONFIG_ESPTOOLPY_FLASHSIZE_4MB=y` in defaults |
| 2 | Flash fails at 460800 baud | Default `idf.py flash` speed | Use `esptool.py -b 115200` directly |
| 3 | Wrong IP subnet | Stale IP in sdkconfig.defaults | Always verify with `ip addr show` + clean rebuild |
| 4 | MQTT publish silently dropped | Missing `-u` flag / broker ACL | Add `-u three-body-backend` to all `mosquitto_pub` calls |
| 5 | OTA download timeout mid-transfer | 10s timeout too short for 900KB | Increased `timeout_ms` to 30000 |
| 6 | Empty shell variables in publish | Variables not exported to new shell | Set + use variables in one command chain |
| 7 | Backend auto-publish missed by device | Device not yet subscribed | Manually re-trigger after heartbeat confirms MQTT connected |

---

**Team Three Body Problem** — SanDisk Hackathon 2026
