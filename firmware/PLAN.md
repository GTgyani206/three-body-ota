# Firmware Development Plan — Phased Milestones

> **Owner:** Devyanshu (Developer 1)  
> **Target:** ESP32 DEVKIT V1 (4MB flash) · ESP-IDF v5.3 · FreeRTOS  
> **OTA Method:** MQTT notification → HTTP download → SHA-256 verify → flash Partition B  

---

## Progress Summary

| Phase | Description | Status |
|-------|-------------|--------|
| 0 | Build & Flash Sanity | ✅ Complete |
| 1 | WiFi Connect | ✅ Complete |
| 2 | MQTT Client | ✅ Complete |
| 3 | Custom Partition Table | ✅ Complete |
| 4 | OTA Download + Flash | ✅ Complete |
| 5 | Self-Test & Rollback | ✅ Complete |
| 6 | Status Reporting | ✅ Complete |
| 7 | Integration Demo | 🔲 Ready to Test |

---

## Current State

| File | Status |
|------|--------|
| `main.c` | Full implementation with OTA, self-test, status reporting |
| `wifi.c` / `wifi.h` | WiFi STA with auto-reconnect |
| `ota_handler.c` / `ota_handler.h` | HTTP download, SHA-256 verify, flash to Partition B |
| `partition.csv` | A/B OTA layout (1.5MB per partition) |
| `Kconfig.projbuild` | WiFi creds + server URLs in menuconfig |
| `sdkconfig.defaults` | 4MB flash, custom partition table |

---

## Phase 0 — Build & Flash Sanity ✅

**Goal:** Confirm toolchain works end-to-end. ESP32 boots, you see serial output.

**Tasks:**
1. Ensure ESP-IDF submodules are fully fetched (`git submodule update --init --recursive`)
2. `cd firmware && idf.py fullclean && idf.py build` — must succeed
3. Flash: `idf.py -p /dev/ttyUSB0 flash monitor`
4. See `"Hello, Three-Body OTA!"` on serial monitor

**Files touched:** None (already written)

**Exit criteria:** Serial monitor shows hello-world output. Build is green.

---

## Phase 1 — WiFi Connect ✅

**Goal:** ESP32 connects to your WiFi network and gets an IP address. Handles reconnects.

**Tasks:**
1. Update `sdkconfig.defaults` with your actual WiFi SSID/password  
2. Write `wifi.h` — expose `wifi_init_sta()` and a "connected" event group bit
3. Write `wifi.c` — WiFi STA mode, event handler for `WIFI_EVENT_STA_DISCONNECTED` (auto-reconnect), `IP_EVENT_STA_GOT_IP`
4. Update `main.c` — call `nvs_flash_init()` + `wifi_init_sta()`, block until IP acquired
5. Build, flash, verify IP address prints on serial monitor

**Key ESP-IDF APIs:**
- `esp_wifi_init()`, `esp_wifi_set_config()`, `esp_wifi_start()`, `esp_wifi_connect()`
- `esp_event_handler_instance_register()` for `WIFI_EVENT` and `IP_EVENT`
- `xEventGroupWaitBits()` to block until connected

**Exit criteria:** Serial monitor shows `"Got IP: 192.168.x.x"`. Reconnects automatically if router reboots.

---

## Phase 2 — MQTT Client ✅

**Goal:** ESP32 connects to Mosquitto broker, subscribes to `firmware/update`, prints received messages.

**Depends on:** Phase 1 (WiFi must be up)

**Tasks:**
1. Add MQTT client code in `main.c` (or a new `mqtt_handler.c` — keep it simple, start in main)
2. Connect to broker at the IP from `sdkconfig.defaults` (`CONFIG_MQTT_BROKER_URL`)
3. Subscribe to topic `firmware/update` with QoS 1
4. On message received → log the raw JSON payload to serial
5. Build, flash, test by manually publishing a message:
   ```bash
   mosquitto_pub -h <broker-ip> -t firmware/update -m '{"version":"0.0.1","test":true}'
   ```
6. Verify ESP32 prints the message

**Key ESP-IDF APIs:**
- `esp_mqtt_client_init()`, `esp_mqtt_client_start()`
- `MQTT_EVENT_CONNECTED`, `MQTT_EVENT_DATA` handlers
- `esp_mqtt_client_subscribe()`

**Exit criteria:** ESP32 receives and prints MQTT messages from the broker.

---

## Phase 3 — Custom Partition Table ✅

**Goal:** Set up A/B OTA partition layout on flash.

**Tasks:**
1. Create `partition.csv` with this layout:
   ```
   # Name,    Type, SubType, Offset,  Size
   nvs,       data, nvs,     0x9000,  0x4000
   otadata,   data, ota,     0xd000,  0x2000
   phy_init,  data, phy,     0xf000,  0x1000
   ota_0,     app,  ota_0,   0x10000, 0x180000
   ota_1,     app,  ota_1,   0x190000,0x180000
   ```
   (Each OTA partition = 1.5 MB — fits comfortably in 4MB flash)
2. Add to `sdkconfig.defaults`:
   ```
   CONFIG_PARTITION_TABLE_CUSTOM=y
   CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partition.csv"
   ```
3. `idf.py fullclean && idf.py build` — partition table must compile
4. Flash with `idf.py -p /dev/ttyUSB0 flash` (this writes the new partition table)
5. Verify with `idf.py partition-table` or check boot log for partition info

**Exit criteria:** Boot log shows `ota_0` and `ota_1` partitions. Device boots from `ota_0`.

---

## Phase 4 — OTA Download + Flash (Core) ✅

**Goal:** On MQTT trigger, download firmware via HTTP, verify SHA-256, write to Partition B, reboot.

**Depends on:** Phase 2 (MQTT) + Phase 3 (partitions)

**Tasks:**
1. Write `ota_handler.h` — expose `ota_start_update(const char *version, const char *sha256_hash, int file_size)`
2. Write `ota_handler.c`:
   - Build download URL: `http://<server-ip>:8000/firmware/{version}/download`
   - Use `esp_http_client` to download the `.bin`
   - Open OTA handle: `esp_ota_begin()` on the next update partition
   - Write chunks: `esp_ota_write()` in a loop
   - Compute SHA-256 on-the-fly using `mbedtls_sha256`
   - After download completes: compare SHA-256 with expected hash
   - If match: `esp_ota_end()` + `esp_ota_set_boot_partition()` + `esp_restart()`
   - If mismatch: `esp_ota_abort()`, log error, stay on current partition
3. In `main.c` MQTT handler: parse the JSON from `firmware/update`, extract `version` + `sha256_hash` + `file_size_bytes`, call `ota_start_update()`
4. Build, flash base firmware (v0.0.1)
5. **Test the happy path:**
   - Upload a v0.0.2 firmware to the backend
   - Backend publishes to MQTT
   - ESP32 receives, downloads, verifies, flashes, reboots into v0.0.2

**Key ESP-IDF APIs:**
- `esp_ota_get_next_update_partition()`
- `esp_ota_begin()`, `esp_ota_write()`, `esp_ota_end()`
- `esp_ota_set_boot_partition()`
- `esp_http_client_init()`, `esp_http_client_perform()` or open/read/close
- `mbedtls_sha256_starts()`, `mbedtls_sha256_update()`, `mbedtls_sha256_finish()`

**Exit criteria:** ESP32 OTA updates from v0.0.1 → v0.0.2 by downloading from the FastAPI backend. SHA-256 verified.

---

## Phase 5 — Self-Test, Commit & Rollback ✅

**Goal:** After rebooting into new firmware, run self-tests. Commit if all pass, otherwise let bootloader rollback automatically.

**Depends on:** Phase 4

**Tasks:**
1. On every boot in `main.c`:
   - Check `esp_ota_get_state()` — if `ESP_OTA_IMG_PENDING_VERIFY`:
     - Run self-test: WiFi connects? MQTT connects? Basic sanity?
     - **PASS →** call `esp_ota_mark_app_valid_cancel_rollback()`, log "COMMITTED"
     - **FAIL →** do nothing, log "SELF-TEST FAILED — will rollback on next reboot"
   - If already committed, proceed normally
2. Add **boot loop detection** via NVS:
   - On every boot: read `reboot_counter` from NVS, increment, write back
   - If counter ≥ 3 and state is `PENDING_VERIFY`: actively trigger rollback
   - After successful commit: reset counter to 0
3. Build two firmware versions:
   - v1 (good) — normal firmware
   - v2 (deliberately broken) — e.g., wrong WiFi SSID so self-test fails
4. **Test rollback path:**
   - Flash v1 (committed, working)
   - OTA update to v2 (broken)
   - ESP32 reboots into v2, self-test fails, boots back into v1

**Key ESP-IDF APIs:**
- `esp_ota_get_state_partition()`
- `esp_ota_mark_app_valid_cancel_rollback()`
- `nvs_get_u32()` / `nvs_set_u32()` for reboot counter

**Exit criteria:** Deliberately broken firmware triggers automatic rollback. Device recovers to the last known-good version without human intervention.

---

## Phase 6 — Status Reporting via MQTT ✅

**Goal:** ESP32 publishes its status back to the broker so the dashboard can display it.

**Depends on:** Phase 5

**Tasks:**
1. After boot + commit/rollback decision, publish to `firmware/status`:
   ```json
   {
     "device_id": "<mac-address>",
     "firmware_version": "1.0.0",
     "status": "COMMITTED",
     "uptime_seconds": 42,
     "free_heap": 180000
   }
   ```
2. Publish periodic heartbeat every 30 seconds to `device/<mac>/status`
3. On OTA start/progress/complete, publish progress updates

**Exit criteria:** Dashboard (Kalpesh) can see the device, its version, and update status.

---

## Phase 7 — End-to-End Integration & Demo

**Goal:** Full demo flow with all three team members' components working together.

**Tasks:**
1. Start Mosquitto broker (docker-compose up)
2. Start FastAPI backend
3. ESP32 running v1 firmware, connected, heartbeat visible on dashboard
4. Upload v2 firmware via backend → MQTT notification → ESP32 downloads → flashes → reboots → self-test → commits → dashboard shows v2
5. Upload v3 firmware (deliberately broken) → same flow → rollback → dashboard shows v1 (or v2) again
6. Record demo video

**Exit criteria:** The demo video shows both the happy path and the rollback path working.

---

## Dependency Graph

```
Phase 0 (Build Sanity)
   │
   ▼
Phase 1 (WiFi) ──────────────────┐
   │                              │
   ▼                              ▼
Phase 2 (MQTT)              Phase 3 (Partition Table)
   │                              │
   └──────────┬───────────────────┘
              ▼
        Phase 4 (OTA Core)
              │
              ▼
        Phase 5 (Self-Test + Rollback)
              │
              ▼
        Phase 6 (Status Reporting)
              │
              ▼
        Phase 7 (Integration Demo)
```

---

## Backend Contract (What Gyanendra Built)

Your firmware only needs to interact with two things:

### 1. MQTT Topic: `firmware/update` (Subscribe)

When a new firmware is uploaded, the backend publishes this JSON:

```json
{
  "version": "1.0.0",
  "file_name": "firmware.bin",
  "file_size_bytes": 123456,
  "sha256_hash": "abcdef1234567890...",
  "signature": "base64...",
  "signing_alg": "ed25519",
  "key_id": "my-key-1"
}
```

You need: `version`, `sha256_hash`, `file_size_bytes`. The rest is informational.

### 2. HTTP Download: `GET /firmware/{version}/download`

Returns the raw `.bin` file as `application/octet-stream`.

Example: `http://192.168.1.100:8000/firmware/1.0.0/download`

Use `esp_http_client` to download this in chunks and pipe into `esp_ota_write()`.

---

## Quick Reference — Files You'll Create/Edit

| Phase | Files |
|-------|-------|
| 0 | (none) |
| 1 | `wifi.c`, `wifi.h`, `main.c`, `sdkconfig.defaults` |
| 2 | `main.c` (add MQTT) |
| 3 | `partition.csv`, `sdkconfig.defaults` |
| 4 | `ota_handler.c`, `ota_handler.h`, `main.c` |
| 5 | `main.c` (self-test + NVS counter) |
| 6 | `main.c` (status publish) |
| 7 | (testing + demo only) |
