# Three-Body OTA — ESP32 Firmware

Fail-safe OTA firmware for ESP32 with automatic rollback on boot failure.

## Features

- **A/B Partition Layout** — Dual OTA partitions for safe updates
- **MQTT-Triggered Updates** — Subscribe to `firmware/update` for OTA notifications
- **HTTP Download** — Fetches firmware binary from FastAPI backend
- **SHA-256 Verification** — Validates firmware integrity before flashing
- **3-Stage Commit Protocol** — Download → Trial Boot → Commit
- **Auto-Rollback** — Fails to connect WiFi/MQTT? Bootloader reverts automatically
- **Boot Loop Detection** — NVS counter triggers rollback after 3 failed boots
- **Status Reporting** — Publishes device status and heartbeats to MQTT

## Requirements

- ESP32 DEVKIT V1 (or compatible, 4MB flash minimum)
- ESP-IDF v5.3+
- USB cable for flashing

## Quick Start

### 1. Configure WiFi and Server

Edit `sdkconfig.defaults` or run `idf.py menuconfig`:

```bash
# sdkconfig.defaults
CONFIG_ESP_WIFI_SSID="YourWiFiName"
CONFIG_ESP_WIFI_PASSWORD="YourWiFiPassword"
CONFIG_FIRMWARE_SERVER_URL="http://192.168.1.100:8000"
CONFIG_MQTT_BROKER_URL="mqtt://192.168.1.100:1883"
```

### 2. Build

```bash
cd firmware
source ~/esp/esp-idf/export.sh  # or your ESP-IDF path
idf.py fullclean
idf.py build
```

### 3. Flash

```bash
idf.py -p /dev/ttyUSB0 flash monitor
```

You should see:
```
I (xxx) MAIN: === Three-Body OTA Firmware ===
I (xxx) MAIN: Device ID: FC:E8:C0:E1:7E:54
I (xxx) MAIN: Firmware Version: 0.1.0
I (xxx) WIFI: Got IP: 192.168.1.xxx
I (xxx) MAIN: MQTT_EVENT_CONNECTED to broker
```

## Project Structure

```
firmware/
├── main/
│   ├── main.c              # Entry point, MQTT handler, self-test, status reporting
│   ├── wifi.c              # WiFi STA driver with auto-reconnect
│   ├── wifi.h
│   ├── ota_handler.c       # HTTP download, SHA-256 verify, flash to Partition B
│   ├── ota_handler.h
│   ├── Kconfig.projbuild   # Menuconfig options for WiFi/Server
│   └── CMakeLists.txt
├── partition.csv           # A/B OTA partition layout
├── sdkconfig.defaults      # Default build configuration
├── CMakeLists.txt
├── PLAN.md                 # Development phases and progress
└── README.md               # This file
```

## Partition Layout

| Name | Type | Offset | Size |
|------|------|--------|------|
| nvs | data | 0x9000 | 16 KB |
| otadata | data | 0xd000 | 8 KB |
| phy_init | data | 0xf000 | 4 KB |
| ota_0 | app | 0x10000 | 1.5 MB |
| ota_1 | app | 0x190000 | 1.5 MB |

## MQTT Topics

### Subscribe

| Topic | Description |
|-------|-------------|
| `firmware/update` | OTA trigger with version, SHA-256, and file size |

### Publish

| Topic | Description |
|-------|-------------|
| `firmware/status` | Device status after commit/rollback |
| `device/{MAC}/status` | Periodic heartbeat (every 30s) |

### OTA Trigger Message Format

```json
{
  "version": "1.0.0",
  "file_name": "firmware.bin",
  "file_size_bytes": 925488,
  "sha256_hash": "a1b2c3d4..."
}
```

## OTA Flow

```
1. ESP32 receives MQTT message on "firmware/update"
2. Parses JSON, extracts version, sha256_hash, file_size_bytes
3. Downloads binary from: GET /firmware/{version}/download
4. Computes SHA-256 while streaming to Partition B
5. Verifies hash matches expected value
6. Sets boot partition to B, reboots

After Reboot:
7. Firmware is in PENDING_VERIFY state
8. Runs self-test: WiFi connects? MQTT connects?
9. PASS → esp_ota_mark_app_valid_cancel_rollback() → COMMITTED
10. FAIL → Restart → After 3 fails, bootloader rolls back to Partition A
```

## Self-Test & Rollback

The firmware uses ESP32's native bootloader rollback mechanism:

- **NVS Reboot Counter**: Incremented on every boot in `PENDING_VERIFY` state
- **Max Retries**: 3 (configurable via `MAX_REBOOT_COUNT`)
- **Self-Test Window**: 15 seconds for MQTT connection
- **Commit**: `esp_ota_mark_app_valid_cancel_rollback()` on success
- **Forced Rollback**: `esp_ota_mark_app_invalid_rollback_and_reboot()` at max retries

## Testing Rollback

1. Flash working firmware (v1)
2. Create broken firmware (v2) — e.g., wrong WiFi SSID baked in
3. Upload v2 to backend, trigger OTA
4. ESP32 downloads v2, reboots
5. WiFi fails in v2 → reboots → after 3 tries → rolls back to v1
6. Device recovers without manual intervention

## Firmware Version

The firmware version is embedded at build time via ESP-IDF's app description.
To set a custom version, add to `CMakeLists.txt`:

```cmake
set(PROJECT_VER "1.0.0")
```

Or create a `version.txt` file in the project root.

## Troubleshooting

### Build Fails with Missing Submodules
```bash
cd ~/esp/esp-idf
git submodule update --init --recursive
```

### WiFi Won't Connect
- Check `CONFIG_ESP_WIFI_SSID` and `CONFIG_ESP_WIFI_PASSWORD` in sdkconfig
- Run `idf.py menuconfig` → Three-Body OTA Configuration → WiFi

### MQTT Won't Connect
- Verify broker is running: `docker compose up -d` (from repo root)
- Check `CONFIG_MQTT_BROKER_URL` points to correct IP
- Test with: `mosquitto_pub -h <ip> -t test -m "hello"`

### OTA Download Fails
- Ensure backend is running: `uvicorn main:app --host 0.0.0.0`
- Check `CONFIG_FIRMWARE_SERVER_URL` is correct
- Verify firmware is uploaded to backend

## License

MIT
