# Edge Case Test Procedures — Hardware & Firmware Tests

> **Scope:** Tests that require physical ESP32 hardware, WiFi infrastructure,
> or manual intervention. Organised by plan category and priority.
>
> **Device:** ESP32 DEVKIT V1 (4 MB flash, A/B OTA partitions)
> **Toolchain:** ESP-IDF v5.3+, `idf.py`, serial monitor

---

## Pre-requisites (All Tests)

1. ESP32 connected via USB (e.g. COM5 / `/dev/ttyUSB0`).
2. WiFi AP running and credentials configured in `sdkconfig.defaults`.
3. Backend stack running: `docker compose up -d && cd backend-and-dash && uvicorn main:app --host 0.0.0.0 --port 8000`.
4. Serial monitor attached: `idf.py -p COM5 monitor`.
5. v1.0.0 firmware flashed as the baseline.

---

## Category 1 — Power Failure During OTA (P0)

### Test 1.1 — Power loss during firmware download

| Field | Value |
|-------|-------|
| **Pre-condition** | Device running v1.0.0, OTA triggered for v2.0.0 |
| **Risk** | Device bricking if partial write corrupts active partition |

**Steps:**
1. Trigger OTA via MQTT for v2.0.0.
2. Watch serial monitor for download progress lines:
   ```
   I OTA_HANDLER: Downloading... 40% (370000 / 926384 bytes)
   ```
3. At ~50 % progress, **pull the USB cable** (hard power cut).
4. Wait 5 seconds, then reconnect USB and open serial monitor.

**Expected:**
- Device boots into **v1.0.0** (old firmware).
- `esp_ota_set_boot_partition()` was never reached, so bootloader ignores the partially-written inactive partition.
- Serial shows `Firmware Version: 1.0.0`.

**Pass:** Old firmware runs normally; device accepts a fresh OTA trigger.

**Recovery:** If device doesn't boot, re-flash v1.0.0 via serial:
```
idf.py -p COM5 flash
```

---

### Test 1.2 — Power loss after `esp_ota_end` but before reboot

| Field | Value |
|-------|-------|
| **Pre-condition** | Device running v1.0.0 |
| **Requires** | Temporary firmware code modification |

**Steps:**
1. In `firmware/main/ota_handler.c`, add a delay before the reboot:
   ```c
   // After line 186: ESP_LOGI(TAG, "OTA complete — rebooting in 3s");
   ESP_LOGW(TAG, "=== TEST 1.2: 30s window — PULL POWER NOW ===");
   vTaskDelay(pdMS_TO_TICKS(30000));  // 30s window to pull power
   ```
2. Build and flash the modified v1.0.0.
3. Trigger OTA for v2.0.0.
4. When you see the test log message, **pull power** within 30 seconds.
5. Reconnect power and observe boot.

**Expected:**
- Bootloader boots into v2.0.0 (boot partition was set to PENDING_VERIFY before power loss).
- Self-test window starts. If v2.0.0 is valid → commits. If not → rollback after 3 failures.

**Pass:** Device settles on a stable firmware version.

**Cleanup:** Remove the test delay and rebuild v1.0.0.

---

### Test 1.3 — Power loss during self-test window

| Field | Value |
|-------|-------|
| **Pre-condition** | v2.0.0 OTA just completed, device in PENDING_VERIFY |
| **Risk** | NVS reboot counter must persist across hard power loss |

**Steps:**
1. Trigger OTA to v2.0.0 successfully.
2. Watch for: `[SELF-TEST] Waiting up to 15s for MQTT connection...`
3. Within 5 seconds of that log line, **pull USB power**.
4. Reconnect. Watch log for reboot count.
5. Repeat steps 3–4 two more times (total: 3 power pulls).

**Expected:**
- Each reconnect shows incrementing `Reboot count: N/3`.
- After 3rd failure: `Max reboots exceeded — triggering rollback`.
- 4th boot: device runs v1.0.0.

**Pass:** Rollback triggers after exactly 3 failures. NVS counter survives hard power cycles.

---

### Test 1.4 — Brownout during flash write

| Field | Value |
|-------|-------|
| **Pre-condition** | Bench power supply required |
| **Equipment** | Adjustable PSU, multimeter |

**Steps:**
1. Power ESP32 from bench PSU at 3.3 V (bypass USB power).
2. Trigger OTA for v2.0.0.
3. At ~50 % download, drop voltage to 2.5 V for 500 ms, then restore to 3.3 V.

**Expected:**
- ESP32 brownout detector triggers a reset.
- Device reboots into v1.0.0 (old firmware).

**Pass:** No manual intervention needed; device recovers automatically.

---

## Category 2 — Network Disruption During Download (P1)

### Test 2.1 — WiFi disconnect mid-download

**Steps:**
1. Trigger OTA for v2.0.0.
2. At ~30 % progress, **turn off the WiFi access point** (or disable the hotspot).
3. Observe serial output.

**Expected:**
```
E OTA_HANDLER: HTTP read error
E OTA_HANDLER: OTA validation failed — aborting
```
Device stays on v1.0.0. Does NOT reboot.

**Pass:** Clean abort, old firmware continues running.

---

### Test 2.2 — Server unreachable (firewall block)

**Steps:**
1. On the server machine, block port 8000:
   - **Windows:** `netsh advfirewall firewall add rule name="block8000" dir=in action=block protocol=tcp localport=8000`
   - **Linux:** `sudo iptables -A INPUT -p tcp --dport 8000 -j DROP`
2. Trigger OTA.
3. Wait 30+ seconds.

**Expected:**
```
E OTA_HANDLER: HTTP open failed: ESP_ERR_HTTP_CONNECT
```
or timeout after 30s.

**Pass:** OTA aborted, device on v1.0.0.

**Cleanup:** Remove firewall rule.

---

### Test 2.3 — Extremely slow network (timeout boundary)

| Field | Value |
|-------|-------|
| **Purpose** | Determine if `timeout_ms=30000` is per-read or total |

**Steps:**
1. Use traffic shaping to limit bandwidth to 10 KB/s:
   - **Linux:** `tc qdisc add dev wlan0 root tbf rate 10kbit burst 10k latency 400ms`
   - **Windows:** Use NetLimiter or similar tool.
2. Trigger OTA (900 KB firmware → ~90 seconds at 10 KB/s).
3. Observe whether download completes or times out.

**Expected:** Download times out at 30s (if per-read timeout) or succeeds (if per-chunk timeout with data flowing). This test determines the actual timeout semantics.

**Pass:** Document the observed behaviour. If timeout, that's correct — 30s is too short for very slow links.

---

### Test 2.4 — Intermittent packet loss (weak WiFi)

**Steps:**
1. Move ESP32 to maximum WiFi range (RSSI below −80 dBm).
2. Verify weak signal: `iw dev wlan0 station dump` shows packet loss.
3. Trigger OTA.

**Expected:** TCP retransmissions slow download. Either:
- Completes within 30s → SHA-256 validates → success.
- Exceeds 30s → clean timeout → abort.

**Pass:** No corruption; OTA either fully succeeds or cleanly aborts.

---

### Test 2.5 — Server sends partial response then dies

**Steps:**
1. Modify the FastAPI download endpoint to abort after 50 %:
   ```python
   # In main.py download_firmware(), replace FileResponse with:
   async def streaming():
       path = FIRMWARE_DIR / storage_name
       sent = 0
       async with aiofiles.open(path, "rb") as f:
           while chunk := await f.read(4096):
               sent += len(chunk)
               if sent > total_bytes // 2:
                   raise ConnectionError("Simulated crash")
               yield chunk
   return StreamingResponse(streaming(), media_type="application/octet-stream")
   ```
2. Trigger OTA.

**Expected:**
```
E OTA_HANDLER: HTTP connection closed prematurely
E OTA_HANDLER: OTA validation failed — aborting
```

**Pass:** Device stays on v1.0.0. Size check catches the truncation.

---

## Category 3 — Firmware-Side Corruption Tests (P0)

### Test 3.4 — Valid binary but non-bootable firmware

**Steps:**
1. Create a random 900 KB file: `dd if=/dev/urandom of=garbage.bin bs=1024 count=900`
2. Compute SHA-256: `sha256sum garbage.bin`
3. Upload to backend and trigger OTA with correct hash.

**Expected:**
- Download succeeds, SHA-256 matches.
- `esp_ota_end()` rejects the image (invalid ESP32 app header).
- OTA aborted, device stays on v1.0.0.

**Pass:** `esp_ota_end failed` logged; device stable.

---

### Test 3.5 — Firmware that crashes immediately on boot

**Steps:**
1. Modify `firmware/main/main.c` — add crash before WiFi:
   ```c
   void app_main(void) {
       abort();  // Intentional crash for Test 3.5
   }
   ```
2. Set version to "crash.0.0", build, upload, trigger OTA.

**Expected:**
- Device reboots into crash.0.0, hits `abort()`, reboots.
- Reboot count increments: 1/3, 2/3, 3/3.
- After 3 crashes → rollback to previous version.

**Pass:** Previous version restored after 3 boot failures.

---

### Test 3.6 — Firmware boots but fails MQTT self-test

**Steps:**
1. Set `CONFIG_MQTT_BROKER_URL="mqtt://192.168.255.255:1883"` (unreachable IP).
2. Keep WiFi credentials correct.
3. Set version to "mqttfail.0.0", build, upload, trigger OTA.

**Expected:**
- WiFi connects successfully.
- MQTT connection times out during 15-second self-test window.
- `[SELF-TEST] FAILED — MQTT timed out after 15s` logged.
- Device reboots. After 3 failures → rollback.

**Pass:** Rollback to previous working version.

---

## Category 5 — Rollback & Boot-Loop Scenarios (P0)

### Test 5.1 — Verify 3-failure rollback threshold

**Steps:**
1. Flash v1.0.0 as baseline.
2. OTA to v2.0.0 (broken WiFi SSID, see TESTING.md Step 7).
3. Watch serial output through all 3 reboot cycles.

**Expected log sequence:**
```
Reboot 1:  Reboot count: 1/3 → WiFi fail → Self-test FAILED → reboot
Reboot 2:  Reboot count: 2/3 → WiFi fail → Self-test FAILED → reboot
Reboot 3:  Reboot count: 3/3 → Max reboots exceeded → rollback
Reboot 4:  Boots v1.0.0 (old firmware) ← ROLLBACK COMPLETE
```

**Pass:** Rollback at exactly 3, not 2 or 4.

---

### Test 5.2 — NVS reboot counter survives hard power loss

**Steps:**
1. OTA to broken firmware (wrong SSID).
2. After first boot failure (count=1), **pull USB power** before second reboot.
3. Reconnect power. Check counter is 1 (from log: `Reboot count: 2/3`).
4. Pull power again after count=2.
5. Reconnect. Should see `Reboot count: 3/3` → rollback.

**Pass:** Counter persists across hard power cycles, reaches 3, triggers rollback.

---

### Test 5.3 — Both OTA partitions contain broken firmware

| Field | Value |
|-------|-------|
| **Risk** | ⚠️ POTENTIAL BRICKING — have serial flash recovery ready |

**Steps:**
1. Flash broken firmware A to ota_0 (bad WiFi SSID, v1.0.0).
2. OTA to broken firmware B (bad MQTT URL, v2.0.0).
3. v2.0.0 fails self-test → rolls back to v1.0.0.
4. v1.0.0 also has bad WiFi → can't connect.
5. Observe what happens.

**Expected:** Device alternates between two bad firmwares or enters infinite boot loop. **There is no factory-reset partition to recover to.**

**Pass:** Document the exact behaviour. This test identifies whether a factory partition is needed.

**Recovery:** Serial flash a working firmware:
```bash
idf.py -p COM5 erase_flash
idf.py -p COM5 flash
```

---

### Test 5.4 — Successful boot resets reboot counter

**Steps:**
1. OTA to v2.0.0 (working firmware).
2. Verify `[SELF-TEST] Firmware 2.0.0 COMMITTED ✓` in log.
3. Manually reboot ESP32 (press reset button).
4. Check that `Reboot count` is 0 or not logged (not in PENDING_VERIFY state).

**Pass:** Counter reset after successful commit.

---

### Test 5.5 — Self-test passes at the 15-second boundary

**Steps:**
1. Add artificial delay to MQTT broker (e.g., Mosquitto auth plugin with 14s sleep).
   Alternatively, use network latency injection:
   ```
   tc qdisc add dev wlan0 root netem delay 13000ms
   ```
2. Trigger OTA.
3. MQTT should connect at ~14 seconds (within the 15s window).

**Expected:** Self-test passes just in time → firmware committed.

**Variant:** Increase delay to 16s → self-test should fail and reboot.

**Pass:** Exact boundary behaviour documented at 14s (pass) vs 16s (fail).

---

## Category 8 — Resource Exhaustion (P3)

### Test 8.1 — Low heap memory during OTA

**Steps:**
1. Modify `app_main()` to allocate large buffers before OTA:
   ```c
   // Before mqtt_app_start()
   void *hog = malloc(200 * 1024);  // Reserve 200 KB of the 320 KB heap
   ESP_LOGW(TAG, "Test 8.1: Free heap after hog: %lu", (unsigned long)esp_get_free_heap_size());
   ```
2. Trigger OTA.

**Expected:** Either:
- `ESP_ERR_NO_MEM` from HTTP client or OTA buffer allocation → clean abort.
- Or OTA succeeds if remaining heap is sufficient.

**Pass:** No crash; clean failure message if heap too low.

**Cleanup:** Remove the memory hog code.

---

### Test 8.2 — Flash wear (100 OTA cycles)

**Steps:**
1. Create a script that loops 100 times:
   ```bash
   for i in $(seq 1 100); do
     # Alternate between v1.0.0 and v2.0.0
     if [ $((i % 2)) -eq 0 ]; then VER="1.0.0"; else VER="2.0.0"; fi
     mosquitto_pub -h $HOST -u three-body-backend -t firmware/update \
       -m "{\"version\":\"$VER\",\"sha256_hash\":\"$HASH\",\"file_size_bytes\":$SIZE}"
     sleep 60  # Wait for OTA + reboot + self-test + settle
   done
   ```
2. Monitor for flash errors in serial output.

**Expected:** All 100 cycles succeed. ESP32 flash is rated for ~100K erase cycles.

**Pass:** No flash errors, no degradation in write speed.

---

### Test 8.3 — OTA during active firmware operation

**Steps:**
1. Ensure the firmware's main loop is actively publishing heartbeats.
2. Trigger OTA while heartbeats are being sent.

**Expected:** OTA runs on dedicated FreeRTOS task (`ota_worker`, 8 KB stack). Main loop continues on the default task. Both share CPU time.

**Pass:** OTA completes. Heartbeats may slow down but device doesn't crash.

---

### Test 8.4 — NVS partition full

**Steps:**
1. Before OTA, fill NVS with dummy entries:
   ```c
   nvs_handle_t h;
   nvs_open("fill_test", NVS_READWRITE, &h);
   for (int i = 0; i < 500; i++) {
       char key[16];
       snprintf(key, sizeof(key), "k%d", i);
       nvs_set_str(h, key, "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx");
   }
   nvs_commit(h);
   nvs_close(h);
   ```
2. Trigger OTA to broken firmware (force self-test failure).
3. Observe whether `set_reboot_count()` succeeds or fails.

**Expected:** If NVS is full, `nvs_set_u32` returns `ESP_ERR_NVS_NOT_ENOUGH_SPACE`. Reboot counter cannot increment → rollback mechanism is compromised.

**Pass:** Document behaviour. If counter can't write, recommend reserved NVS space for OTA data.

---

## Category 9 — Timing & Concurrency (P3, Device-Side)

### Test 9.2 — OTA trigger while previous OTA is downloading

**Steps:**
1. Trigger OTA for v2.0.0.
2. While download is in progress (~50 %), publish another trigger for v3.0.0:
   ```bash
   mosquitto_pub -h $HOST -u three-body-backend -t firmware/update \
     -m '{"version":"3.0.0","sha256_hash":"...","file_size_bytes":...}'
   ```

**Expected:**
```
W MAIN: OTA already in progress — command dropped
```
OTA queue depth is 1; second command is dropped via `xQueueSend(..., 0)`.

**Pass:** Only one OTA runs. No corruption from overlapping writes.

---

### Test 9.3 — MQTT reconnect storm after broker restart

**Steps:**
1. Connect 3+ ESP32 devices to the same broker.
2. Restart Mosquitto: `docker restart three-body-mqtt`.
3. Watch all devices' serial output.

**Expected:** All devices detect disconnection, reconnect when broker is back. If a retained OTA message exists, all may start downloading simultaneously.

**Pass:** All devices recover MQTT connection. Backend handles concurrent downloads without crashing.

---

### Test 9.4 — WiFi AP restart (brief outage)

**Steps:**
1. Device running normally, MQTT connected.
2. Turn off WiFi AP for 5 seconds, then turn back on.

**Expected:**
- WiFi event handler detects disconnection.
- Retry logic reconnects (up to 10 attempts).
- MQTT reconnects automatically.

**Pass:** Device recovers without reboot. Heartbeats resume.

---

## Category 10 — Environmental & Hardware (P3)

### Test 10.1 — Cold boot after extended power-off

**Steps:**
1. After successful OTA to v2.0.0, unplug device for 24+ hours.
2. Reconnect and observe boot.

**Expected:** NVS data intact (flash-based). Device boots v2.0.0. Counter is 0.

**Pass:** No data loss from extended power-off.

---

### Test 10.2 — Rapid power cycling (relay chatter)

**Steps:**
1. Use a relay or manual USB plug to power-cycle every 500 ms for 30 seconds.
2. Then leave powered on and observe boot.

**Expected:** ESP32 may not fully boot during rapid cycles. Once power stabilises, device boots normally. No flash corruption (no OTA was in progress).

**Pass:** Clean boot after power stabilises.

---

### Test 10.3 — Serial monitor attached during OTA

**Steps:**
1. Run `idf.py -p COM5 monitor` (UART attached).
2. Trigger OTA.

**Expected:** UART output doesn't interfere with OTA. Full download log visible.

**Pass:** OTA completes successfully with monitor attached.

---

## Results Template

| Test ID | Date | Result | Notes |
|---------|------|--------|-------|
| 1.1     |      |        |       |
| 1.2     |      |        |       |
| 1.3     |      |        |       |
| 1.4     |      |        |       |
| 2.1     |      |        |       |
| 2.2     |      |        |       |
| 2.3     |      |        |       |
| 2.4     |      |        |       |
| 2.5     |      |        |       |
| 3.4     |      |        |       |
| 3.5     |      |        |       |
| 3.6     |      |        |       |
| 5.1     |      |        |       |
| 5.2     |      |        |       |
| 5.3     |      |        |       |
| 5.4     |      |        |       |
| 5.5     |      |        |       |
| 8.1     |      |        |       |
| 8.2     |      |        |       |
| 8.3     |      |        |       |
| 8.4     |      |        |       |
| 9.2     |      |        |       |
| 9.3     |      |        |       |
| 9.4     |      |        |       |
| 10.1    |      |        |       |
| 10.2    |      |        |       |
| 10.3    |      |        |       |
