# Three-Body OTA — Quickstart Guide

Get the entire OTA system running in under 10 minutes.

---

## Prerequisites

| Tool | Version | Install Command |
|------|---------|-----------------|
| ESP-IDF | v5.3+ | [Installation Guide](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/get-started/) |
| Python | 3.10+ | `sudo apt install python3 python3-pip` |
| Docker | 20.10+ | `sudo apt install docker.io docker-compose` |
| Rust | 1.70+ | `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \| sh` |

**Hardware:** ESP32 DEVKIT V1 (4MB flash) connected via USB

---

## Step 1: Clone the Repository

```bash
git clone https://github.com/GTgyani206/three-body-ota.git
cd three-body-ota
```

---

## Step 2: Configure WiFi & Server IP

Find your laptop's IP address:
```bash
ip addr show | grep "inet " | grep -v 127.0.0.1
```

Edit the firmware configuration:
```bash
nano firmware/sdkconfig.defaults
```

Update these lines with your values:
```ini
CONFIG_ESP_WIFI_SSID="YOUR_WIFI_NAME"
CONFIG_ESP_WIFI_PASSWORD="YOUR_WIFI_PASSWORD"
CONFIG_FIRMWARE_SERVER_URL="http://YOUR_IP:8000"
CONFIG_MQTT_BROKER_URL="mqtt://YOUR_IP:1883"
```

---

## Step 3: Start Infrastructure

### Terminal 1 — MQTT Broker
```bash
docker compose up -d
docker compose logs -f mosquitto
```

### Terminal 2 — FastAPI Backend
```bash
cd backend-and-dash
pip install -r requirements.txt
export ADMIN_TOKEN="your-secret-token"
uvicorn main:app --host 0.0.0.0 --port 8000
```

Verify backend is running:
```bash
curl http://localhost:8000/health
# Expected: {"status": "ok"}
```

---

## Step 4: Build & Flash Firmware v1.0.0

```bash
cd firmware

# Set version
sed -i 's/PROJECT_VER ".*"/PROJECT_VER "1.0.0"/' CMakeLists.txt

# Clean build with your config
rm -rf build sdkconfig
idf.py build

# Flash to ESP32
idf.py -p /dev/ttyUSB0 flash

# Monitor serial output
idf.py -p /dev/ttyUSB0 monitor
```

**Expected output:**
```
I (XXX) app_init: App version:      1.0.0
I (XXX) WIFI: Got IP: 192.168.x.x
I (XXX) MAIN: MQTT_EVENT_CONNECTED to broker
I (XXX) MAIN: Subscribed to firmware/update
I (XXX) MAIN: Firmware running normally. Waiting for OTA commands...
```

Press `Ctrl+]` to exit monitor.

---

## Step 5: Build v2.0.0 for OTA Update

```bash
cd firmware

# Change version
sed -i 's/PROJECT_VER "1.0.0"/PROJECT_VER "2.0.0"/' CMakeLists.txt

# Rebuild (don't flash!)
rm -rf build
idf.py build

# Get SHA256 and size
sha256sum build/three_body_ota.bin
stat --format=%s build/three_body_ota.bin
```

Note down the SHA256 hash and file size.

---

## Step 6: Upload Firmware to Backend

```bash
# Replace SHA256 and SIZE with values from Step 5
SHA256="your_sha256_hash_here"
SIZE=926320  # Your actual size

curl -X POST "http://localhost:8000/upload-firmware/" \
  -H "X-Admin-Token: your-secret-token" \
  -F "firmware=@build/three_body_ota.bin" \
  -F "metadata={\"version\":\"2.0.0\",\"file_name\":\"three_body_ota.bin\",\"file_size_bytes\":${SIZE},\"sha256_hash\":\"${SHA256}\",\"signing_alg\":\"ed25519\",\"signature\":\"dev\"}"
```

Verify upload:
```bash
curl http://localhost:8000/firmware/
# Should list version 2.0.0
```

---

## Step 7: Trigger OTA Update

```bash
# Replace SHA256 and SIZE with your values
docker exec three-body-mqtt mosquitto_pub \
  -h localhost \
  -t "firmware/update" \
  -m '{"version":"2.0.0","sha256_hash":"YOUR_SHA256","file_size_bytes":YOUR_SIZE}'
```

---

## Step 8: Watch the Magic 🎉

In your serial monitor (`idf.py monitor`), you'll see:

```
I (XXX) MQTT: Received update notification
I (XXX) OTA_HANDLER: Starting OTA to version 2.0.0
I (XXX) OTA_HANDLER: HTTP connected, downloading...
I (XXX) OTA_HANDLER: Written 926320 bytes
I (XXX) OTA_HANDLER: SHA-256 verified successfully!
I (XXX) OTA_HANDLER: OTA Success! Rebooting in 3 seconds...

--- DEVICE REBOOTS ---

I (XXX) app_init: App version:      2.0.0     ← SUCCESS!
I (XXX) WIFI: Got IP: 192.168.x.x
I (XXX) MAIN: MQTT_EVENT_CONNECTED
I (XXX) MAIN: Firmware COMMITTED — Normal operation
```

---

## Quick Reference

### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| POST | `/upload-firmware/` | Upload new firmware (requires X-Admin-Token) |
| GET | `/firmware/` | List all versions |
| GET | `/firmware/{version}` | Get version metadata |
| GET | `/firmware/{version}/download` | Download binary |
| DELETE | `/firmware/{version}` | Delete version (requires X-Admin-Token) |

### MQTT Topics

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `firmware/update` | Server → Device | Trigger OTA with version, SHA256, size |
| `firmware/status` | Device → Server | Device status reports |
| `device/{MAC}/status` | Device → Server | Per-device heartbeats |

### Partition Layout

```
0x001000  bootloader      (24 KB)
0x008000  partition-table (4 KB)
0x009000  nvs             (16 KB)
0x00d000  otadata         (8 KB)
0x00f000  phy_init        (4 KB)
0x010000  ota_0           (1.5 MB)  ← Partition A
0x190000  ota_1           (1.5 MB)  ← Partition B
```

---

## Troubleshooting

### WiFi Connection Failed
- Verify SSID/password in `sdkconfig.defaults`
- Ensure 2.4GHz WiFi (ESP32 doesn't support 5GHz)
- Check router allows new connections

### MQTT Connection Failed
- Verify laptop IP in `sdkconfig.defaults`
- Check `docker compose ps` shows mosquitto running
- Ensure ESP32 and laptop are on same network

### Flash Failed
```bash
# Try slower baud rate
python -m esptool --chip esp32 -p /dev/ttyUSB0 -b 115200 \
  write_flash --flash_mode dio --flash_size 4MB \
  0x1000 build/bootloader/bootloader.bin \
  0x8000 build/partition_table/partition-table.bin \
  0xd000 build/ota_data_initial.bin \
  0x10000 build/three_body_ota.bin
```

### OTA Download Failed
- Check backend is running: `curl http://YOUR_IP:8000/health`
- Verify firmware was uploaded: `curl http://YOUR_IP:8000/firmware/`
- Check SHA256 matches exactly

### Automatic Rollback Triggered
This is the safety system working! The device rolled back because:
- WiFi didn't connect within 15 seconds
- MQTT didn't connect within 15 seconds
- Check your WiFi credentials are correct in the new firmware

---

## Testing Rollback

To verify the rollback mechanism works:

1. Build v3.0.0 with **wrong WiFi credentials**:
```bash
# Temporarily change WiFi to invalid
sed -i 's/CONFIG_ESP_WIFI_SSID=.*/CONFIG_ESP_WIFI_SSID="WRONG_SSID"/' sdkconfig.defaults
sed -i 's/PROJECT_VER "2.0.0"/PROJECT_VER "3.0.0"/' CMakeLists.txt
rm -rf build sdkconfig && idf.py build
```

2. Upload and trigger OTA as before

3. Watch serial output — device will:
   - Download and flash v3.0.0
   - Reboot into v3.0.0
   - Fail to connect WiFi
   - Timeout after 15 seconds
   - **Automatically rollback to v2.0.0**

---

## Next Steps

- 📊 **Streamlit Dashboard**: `cd backend-and-dash/streamlit && streamlit run app.py`
- 🔐 **Production Security**: Enable MQTT TLS and ACLs
- 🔑 **Firmware Signing**: Use the CLI tool for Ed25519 signatures

---

**Team Three Body Problem** — SanDisk Hackathon 2026
