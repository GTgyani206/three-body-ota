#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────
# test_ota.sh — End-to-end OTA test script for Three-Body OTA
#
# Usage:
#   ./test_ota.sh              # Full: flash v1.0.0, OTA to v2.0.0
#   ./test_ota.sh --ota-only   # Skip flash, just build+upload+trigger OTA
#   ./test_ota.sh --version X  # OTA to version X (default: 2.0.0)
# ─────────────────────────────────────────────────────────────────
set -euo pipefail

# ── Defaults ──
BASE_VERSION="1.0.0"
OTA_VERSION="2.0.0"
OTA_ONLY=false
PORT="/dev/ttyUSB0"
BAUD=115200
BACKEND_URL="http://localhost:8000"
ADMIN_TOKEN="replace-with-strong-random-token"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRMWARE_DIR="$SCRIPT_DIR/firmware"

# Temp files for background processes
MQTT_SUB_FILE=$(mktemp /tmp/ota-mqtt-sub.XXXXXX)
SERIAL_LOG=$(mktemp /tmp/ota-serial.XXXXXX)
PIDS_TO_KILL=()

cleanup() {
    # Kill any background processes we started
    for pid in "${PIDS_TO_KILL[@]}"; do
        kill "$pid" 2>/dev/null || true
    done
    # Clean temp files
    rm -f "$MQTT_SUB_FILE" "$SERIAL_LOG" 2>/dev/null || true
    # Restore CMakeLists version
    sed -i "s/PROJECT_VER \".*\"/PROJECT_VER \"${BASE_VERSION}\"/" \
        "$FIRMWARE_DIR/CMakeLists.txt" 2>/dev/null || true
}
trap cleanup EXIT

# ── Colors ──
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ── Parse args ──
while [[ $# -gt 0 ]]; do
    case "$1" in
        --ota-only)   OTA_ONLY=true; shift ;;
        --version)    OTA_VERSION="$2"; shift 2 ;;
        --port)       PORT="$2"; shift 2 ;;
        --base)       BASE_VERSION="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 [--ota-only] [--version X.Y.Z] [--base X.Y.Z] [--port /dev/ttyUSBx]"
            exit 0 ;;
        *) fail "Unknown argument: $1" ;;
    esac
done

# ─────────────────────────────────────────────────────────────────
# Pre-flight checks
# ─────────────────────────────────────────────────────────────────
info "Pre-flight checks..."

command -v idf.py >/dev/null 2>&1 \
    || fail "ESP-IDF not sourced. Run: . \$HOME/esp/esp-idf/export.sh"
docker ps >/dev/null 2>&1 \
    || fail "Docker not running or no sudo access"
[[ -c "$PORT" ]] \
    || fail "Serial port $PORT not found. Is the ESP32 connected?"
command -v mosquitto_sub >/dev/null 2>&1 \
    || fail "mosquitto-clients not installed. Run: sudo apt install mosquitto-clients"

# Kill anything holding the serial port
fuser -k "$PORT" 2>/dev/null || true
sleep 0.5

# WiFi interface + IP
LAPTOP_IP=$(ip -4 addr show wlp1s0 2>/dev/null \
    | grep -oP 'inet \K[0-9.]+' || true)
if [[ -z "$LAPTOP_IP" ]]; then
    LAPTOP_IP=$(ip -4 route get 1 | grep -oP 'src \K[0-9.]+' || true)
fi
[[ -n "$LAPTOP_IP" ]] || fail "Could not detect laptop IP. Check WiFi."

# Verify sdkconfig.defaults IPs match
CONFIGURED_IP=$(grep 'CONFIG_FIRMWARE_SERVER_URL' \
    "$FIRMWARE_DIR/sdkconfig.defaults" | grep -oP '//\K[0-9.]+')
IP_CHANGED=false
if [[ "$CONFIGURED_IP" != "$LAPTOP_IP" ]]; then
    warn "IP mismatch: sdkconfig has $CONFIGURED_IP but laptop is $LAPTOP_IP"
    info "Auto-fixing sdkconfig.defaults..."
    sed -i "s|$CONFIGURED_IP|$LAPTOP_IP|g" "$FIRMWARE_DIR/sdkconfig.defaults"
    ok "Updated sdkconfig.defaults to $LAPTOP_IP"
    IP_CHANGED=true
fi

ok "Pre-flight passed (Laptop IP: $LAPTOP_IP)"

# ─────────────────────────────────────────────────────────────────
# Step 1: Ensure infrastructure is running
# ─────────────────────────────────────────────────────────────────
info "Starting infrastructure..."

if ! docker ps --format '{{.Names}}' | grep -q 'three-body-mqtt'; then
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" up -d mosquitto
    info "Waiting for MQTT broker..."
    sleep 5
fi
ok "MQTT broker running"

if curl -s --max-time 2 "$BACKEND_URL/health" | grep -q '"ok"'; then
    ok "Backend already running"
else
    info "Starting backend..."
    cd "$SCRIPT_DIR/backend-and-dash"
    export ADMIN_TOKEN="$ADMIN_TOKEN"
    nohup uvicorn main:app --host 0.0.0.0 --port 8000 \
        > /tmp/three-body-backend.log 2>&1 &
    BACKEND_PID=$!
    PIDS_TO_KILL+=("$BACKEND_PID")
    sleep 3
    curl -s --max-time 3 "$BACKEND_URL/health" | grep -q '"ok"' \
        || fail "Backend failed to start. Check /tmp/three-body-backend.log"
    ok "Backend started (PID $BACKEND_PID)"
fi

cd "$FIRMWARE_DIR"

# ─────────────────────────────────────────────────────────────────
# Step 2: Build and flash base firmware (unless --ota-only)
# ─────────────────────────────────────────────────────────────────
if [[ "$OTA_ONLY" == false ]]; then
    info "Building base firmware v${BASE_VERSION}..."
    sed -i "s/PROJECT_VER \".*\"/PROJECT_VER \"${BASE_VERSION}\"/" CMakeLists.txt

    # Full rebuild if IP changed, otherwise just regenerate sdkconfig
    if [[ "$IP_CHANGED" == true ]]; then
        info "IP changed — doing full rebuild..."
        rm -rf build sdkconfig
    else
        rm -f sdkconfig
    fi

    idf.py build 2>&1 | tail -3

    info "Erasing flash for clean OTA state..."
    esptool.py --chip esp32 --port "$PORT" --baud "$BAUD" \
        erase_flash 2>&1 | tail -2

    info "Flashing v${BASE_VERSION} to ESP32..."
    python -m esptool --chip esp32 -p "$PORT" -b "$BAUD" \
        --before default_reset --after hard_reset write_flash \
        --flash_mode dio --flash_size 4MB --flash_freq 40m \
        0x1000 build/bootloader/bootloader.bin \
        0x8000 build/partition_table/partition-table.bin \
        0xd000 build/ota_data_initial.bin \
        0x10000 build/three_body_ota.bin 2>&1 | grep -E "Wrote|Hash"
    ok "v${BASE_VERSION} flashed"

    # Start persistent MQTT subscriber to catch device coming online
    info "Waiting for device to boot and connect to MQTT..."
    > "$MQTT_SUB_FILE"
    mosquitto_sub -h localhost -p 1883 -t "firmware/status" -t "device/#" \
        >> "$MQTT_SUB_FILE" 2>/dev/null &
    SUB_PID=$!
    PIDS_TO_KILL+=("$SUB_PID")

    CONNECTED=false
    for i in $(seq 1 50); do
        sleep 1
        if grep -q "$BASE_VERSION" "$MQTT_SUB_FILE" 2>/dev/null; then
            CONNECTED=true
            break
        fi
        # Progress every 10s
        if (( i % 10 == 0 )); then
            info "  Waiting for MQTT... (${i}s)"
        fi
    done

    kill "$SUB_PID" 2>/dev/null || true

    if [[ "$CONNECTED" == true ]]; then
        ok "Device online: v${BASE_VERSION} confirmed via MQTT"
    else
        warn "Could not confirm v${BASE_VERSION} via MQTT after 50s"
        info "Continuing anyway..."
    fi
fi

# ─────────────────────────────────────────────────────────────────
# Step 3: Build OTA target firmware
# ─────────────────────────────────────────────────────────────────
info "Building OTA target v${OTA_VERSION}..."
sed -i "s/PROJECT_VER \".*\"/PROJECT_VER \"${OTA_VERSION}\"/" CMakeLists.txt

# Incremental build — only version string changes
idf.py build 2>&1 | tail -3

SHA256=$(sha256sum build/three_body_ota.bin | cut -d' ' -f1)
SIZE=$(stat --format=%s build/three_body_ota.bin)
ok "v${OTA_VERSION} built (${SIZE} bytes, SHA: ${SHA256:0:16}...)"

# ─────────────────────────────────────────────────────────────────
# Step 4: Upload to backend
# ─────────────────────────────────────────────────────────────────
info "Uploading v${OTA_VERSION} to backend..."

# Delete any stale version first
curl -s -X DELETE "$BACKEND_URL/firmware/$OTA_VERSION" \
    -H "X-Admin-Token: $ADMIN_TOKEN" >/dev/null 2>&1 || true

UPLOAD_RESULT=$(curl -s -X POST "$BACKEND_URL/upload-firmware/" \
    -H "X-Admin-Token: $ADMIN_TOKEN" \
    -F "firmware=@build/three_body_ota.bin;type=application/octet-stream" \
    -F "metadata={\"version\":\"${OTA_VERSION}\",\"file_size_bytes\":${SIZE},\"sha256_hash\":\"${SHA256}\"}")

if echo "$UPLOAD_RESULT" | grep -q '"status":"ok"'; then
    ok "Upload successful"
else
    echo "$UPLOAD_RESULT"
    fail "Upload failed"
fi

# ─────────────────────────────────────────────────────────────────
# Step 5: Start MQTT watcher, then trigger OTA
# ─────────────────────────────────────────────────────────────────

# Persistent MQTT subscriber — started BEFORE trigger so we never miss a message
> "$MQTT_SUB_FILE"
mosquitto_sub -h localhost -p 1883 -t "firmware/status" -t "device/#" \
    >> "$MQTT_SUB_FILE" 2>/dev/null &
VERIFY_PID=$!
PIDS_TO_KILL+=("$VERIFY_PID")
sleep 1

info "Triggering OTA update to v${OTA_VERSION}..."
docker exec three-body-mqtt mosquitto_pub \
    -h localhost \
    -u three-body-backend \
    -t "firmware/update" \
    -m "{\"version\":\"${OTA_VERSION}\",\"sha256_hash\":\"${SHA256}\",\"file_size_bytes\":${SIZE}}"
ok "OTA trigger sent"

# ─────────────────────────────────────────────────────────────────
# Step 6: Wait for OTA + reboot + verify
# ─────────────────────────────────────────────────────────────────
info "Waiting for OTA download, reboot, and self-test (up to 120s)..."
info "(download → SHA-256 verify → reboot → WiFi → MQTT → self-test → COMMIT)"

OTA_SUCCESS=false
TIMEOUT=120

for i in $(seq 1 $TIMEOUT); do
    sleep 1

    if grep -q "\"firmware_version\":\"${OTA_VERSION}\"" "$MQTT_SUB_FILE" 2>/dev/null; then
        if grep "\"firmware_version\":\"${OTA_VERSION}\"" "$MQTT_SUB_FILE" \
            | grep -qE '"status":"(COMMITTED|RUNNING)"'; then
            OTA_SUCCESS=true
            break
        fi
    fi

    if (( i % 15 == 0 )); then
        info "  Still waiting... (${i}s / ${TIMEOUT}s)"
    fi
done

kill "$VERIFY_PID" 2>/dev/null || true

echo ""
echo "═══════════════════════════════════════════════════════════"
if [[ "$OTA_SUCCESS" == true ]]; then
    echo -e "${GREEN}  ✅ OTA UPDATE SUCCESSFUL: v${BASE_VERSION} → v${OTA_VERSION}${NC}"
    echo -e "${GREEN}  Device confirmed running v${OTA_VERSION} via MQTT${NC}"
else
    echo -e "${RED}  ❌ OTA verification timed out after ${TIMEOUT}s${NC}"
    echo -e "${YELLOW}  Captured MQTT messages:${NC}"
    cat "$MQTT_SUB_FILE" 2>/dev/null | tail -5
    echo ""
    echo -e "${YELLOW}  Check: idf.py -p $PORT monitor${NC}"
fi
echo "═══════════════════════════════════════════════════════════"
echo ""
