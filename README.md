# Three-Body OTA — Fail-Safe Over-The-Air Firmware Update System

A production-grade OTA firmware update platform for **ESP32** microcontrollers, featuring a **3-Stage Commit Protocol** with automatic rollback, MQTT-based chunked transport, and a cloud management backend.

Built in 48 hours.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLOUD / SERVER                           │
│                                                                 │
│  ┌──────────────────────┐       ┌────────────────────────────┐  │
│  │  FastAPI Backend      │       │  Streamlit Dashboard       │  │
│  │  - Upload .bin        │       │  - Device fleet view       │  │
│  │  - Trigger OTA        │       │  - OTA progress monitor    │  │
│  │  - Track device state │       │  - Rollback controls       │  │
│  └──────────┬───────────┘       └────────────────────────────┘  │
│             │                                                   │
└─────────────┼───────────────────────────────────────────────────┘
              │ Publish firmware chunks + metadata
              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     TRANSPORT (MQTT)                             │
│                                                                 │
│  Eclipse Mosquitto Broker — QoS 1 — Port 1883                  │
│                                                                 │
│  Topics:                                                        │
│    ota/{device_id}/chunk     → 4 KB firmware data packets       │
│    ota/{device_id}/command   → start / abort / commit signals   │
│    ota/{device_id}/status    → device reports progress & state  │
│    ota/{device_id}/metadata  → firmware size, SHA-256, version  │
│                                                                 │
└─────────────┬───────────────────────────────────────────────────┘
              │ Subscribe & receive
              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     DEVICE LAYER (ESP32)                         │
│                                                                 │
│  ┌────────────┐  ┌────────────┐  ┌───────────────────────────┐  │
│  │ Partition A │  │ Partition B │  │  3-Stage Commit Engine    │  │
│  │ (active)   │  │ (staging)  │  │  Stage 1: DOWNLOADING     │  │
│  └────────────┘  └────────────┘  │  Stage 2: PENDING_VERIFY  │  │
│                                  │  Stage 3: COMMITTED        │  │
│  FreeRTOS  ·  ESP-IDF Bootloader │  (or AUTO_ROLLBACK)       │  │
│                                  └───────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3-Stage Commit Protocol

The core safety mechanism guaranteeing that a bad firmware **never bricks the device**.

### Stage 1 — `DOWNLOADING`
The device subscribes to its MQTT chunk topic and writes incoming 4 KB packets to the **inactive (B) partition**. A running SHA-256 hash is computed on-the-fly. If the hash doesn't match the metadata, the download is discarded and the device stays on partition A.

### Stage 2 — `PENDING_VERIFY`
After a successful download and hash verification, the bootloader is instructed to **trial-boot into partition B** on next restart. The device reboots. It now has a limited window (configurable, default 30 seconds) to:
- Initialize all peripherals
- Pass self-test health checks
- Report `BOOT_OK` over MQTT

If any check fails **or** the timer expires without confirmation, the ESP32 bootloader **automatically rolls back** to partition A. No server intervention required.

### Stage 3 — `COMMITTED`
Once the device confirms health, it calls `esp_ota_mark_app_valid_cancel_rollback()`, permanently committing partition B as the new active partition. It publishes a `COMMITTED` status to the server.

```
  Download OK?──No──► Discard, stay on A
       │
      Yes
       ▼
  Trial Boot B
       │
  Health OK within timeout?──No──► Auto-Rollback to A
       │
      Yes
       ▼
  COMMIT B as active ✓
```

---

## Repository Structure

```
three-body-ota/
│
├── firmware/              ← ESP-IDF C project (Device Layer)
│   ├── main/              ← Application source (app_main, OTA engine, MQTT client)
│   ├── components/        ← Reusable ESP-IDF components
│   ├── partitions.csv     ← Custom A/B partition table
│   └── CMakeLists.txt
│
├── backend-and-dash/      ← Python server (Cloud Layer)
│   ├── api/               ← FastAPI application (upload, trigger, device registry)
│   ├── dashboard/         ← Streamlit dashboard
│   ├── requirements.txt
│   └── Dockerfile
│
├── cli-tool/              ← Rust CLI (Tooling Layer)
│   ├── src/
│   │   └── main.rs        ← SHA-256 hashing, metadata JSON generation
│   └── Cargo.toml
│
├── mosquitto/             ← Broker configuration
│   └── config/
│       └── mosquitto.conf
│
├── docker-compose.yml     ← Spins up Mosquitto broker
└── README.md              ← You are here
```

### Ownership Rules (Zero Merge Conflicts)

| Directory          | Owner       | Language     | Touches MQTT Topics        |
|--------------------|-------------|--------------|----------------------------|
| `firmware/`        | Developer 1 | C (ESP-IDF)  | Subscribe: chunk, command, metadata · Publish: status |
| `backend-and-dash/`| Developer 2 | Python       | Publish: chunk, command, metadata · Subscribe: status |
| `cli-tool/`        | Developer 3 | Rust         | None (offline tooling)     |

No directory is shared. No file is co-owned. Conflicts are structurally impossible.

---

## Quick Start

### Prerequisites
- Docker & Docker Compose
- Git

### 1. Clone & Start the Broker
```bash
git clone <repo-url> && cd three-body-ota
docker compose up -d
```

### 2. Verify Mosquitto is Running
```bash
docker compose ps
# Should show mosquitto running on 0.0.0.0:1883
```

### 3. Test MQTT (optional, requires mosquitto-clients)
```bash
# Terminal 1 — Subscribe
mosquitto_sub -h localhost -t "test/hello"

# Terminal 2 — Publish
mosquitto_pub -h localhost -t "test/hello" -m "Three Body OTA is alive"
```

---

## Tech Stack

| Layer     | Technology                        | Why                                              |
|-----------|-----------------------------------|--------------------------------------------------|
| Device    | ESP-IDF 5.x, FreeRTOS, C         | Native OTA APIs, dual-partition bootloader        |
| Transport | Eclipse Mosquitto, MQTT v3.1.1    | Lightweight, QoS 1 guaranteed delivery            |
| Backend   | FastAPI, Pydantic, uvicorn        | Async Python, auto-generated OpenAPI docs         |
| Dashboard | Streamlit                         | Rapid prototyping, real-time data display         |
| CLI Tool  | Rust, sha2 crate, serde_json      | Fast hashing, single-binary distribution          |
| Infra     | Docker Compose                    | One-command local environment                     |

---

## License

MIT
