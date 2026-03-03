# Three-Body OTA — Fail-Safe Over-The-Air Firmware Update System

A fail-safe OTA firmware update platform for **ESP32** microcontrollers, featuring a **3-Stage Commit Protocol** with automatic rollback, MQTT-based chunked transport, Ed25519-signed firmware packages, and a cloud management backend.

Built in 48 hours for the SanDisk Hackathon.

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

## Implementation Status

| Component | Status | Description |
|-----------|--------|-------------|
| **Rust CLI Signer** | ✅ Implemented | Ed25519 keygen / sign / verify, SHA-256 hashing, metadata JSON |
| **FastAPI Backend** | ✅ Implemented | Upload, CRUD, auth, path safety, signature verification, MQTT publish |
| **Mosquitto Broker** | ✅ Implemented | Docker Compose config, anonymous local dev, QoS 1 |
| **Security Tests** | ✅ Implemented | 13 pytest tests + 9 Rust unit tests |
| **ESP32 Firmware** | 🔲 Planned | A/B partitions, OTA engine, MQTT client, 3-stage commit |
| **Streamlit Dashboard** | 🔲 Planned | Fleet view, OTA progress monitor, rollback controls |
| **Chunked MQTT Delivery** | 🔲 Planned | Backend splitting .bin into 4KB chunks over MQTT |
| **TLS / Broker ACLs** | 🔲 Planned | Production security for MQTT transport |

---

## Repository Structure (Actual)

```
three-body-ota/
│
├── cli-tool/                       ← Rust CLI signer (Developer 3)
│   ├── src/
│   │   └── main.rs                 ← Ed25519 sign/verify/keygen + SHA-256 hashing
│   ├── Cargo.toml
│   └── Cargo.lock
│
├── backend-and-dash/               ← Python backend (Developer 2)
│   ├── main.py                     ← FastAPI server (upload, CRUD, auth, sig verify)
│   ├── requirements.txt
│   ├── test_security.py            ← pytest security test suite (13 tests)
│   └── firmware_storage/           ← Uploaded .bin files + registry.json
│
├── firmware/                       ← ESP-IDF C project (Developer 1) — not yet started
│
├── mosquitto/
│   └── config/
│       └── mosquitto.conf          ← Anonymous local broker config
│
├── docker-compose.yml              ← Mosquitto broker on port 1883
├── .gitignore
└── README.md
```

### Ownership Rules (Zero Merge Conflicts)

| Directory          | Owner       | Language     |
|--------------------|-------------|--------------|
| `firmware/`        | Developer 1 | C (ESP-IDF)  |
| `backend-and-dash/`| Developer 2 | Python       |
| `cli-tool/`        | Developer 3 | Rust         |

---

## Security Model

### Firmware Signing (Ed25519)

All firmware packages are cryptographically signed before upload. The signing flow:

```
┌──────────────┐     canonical JSON      ┌──────────────────┐
│  .bin file   ├──SHA-256──► ┌──────┐    │ Signed metadata  │
│              │             │ sign ├───►│  .json           │
│  + version   ├──metadata──►└───┬──┘    │  (+ signature)   │
└──────────────┘                 │       └──────────────────┘
                         Ed25519 private key
```

**Canonical payload** (what is signed): compact JSON, alphabetical keys, no whitespace:
```json
{"file_name":"fw.bin","file_size_bytes":1024,"sha256_hash":"ab...","version":"1.0.0"}
```

Both the Rust CLI and Python backend produce identical canonical bytes for the same input, ensuring cross-language signature compatibility.

### Backend Hardening

| Protection | Implementation |
|-----------|---------------|
| **Authentication** | `X-Admin-Token` header required on POST/DELETE. Token from `ADMIN_TOKEN` env var. |
| **Path traversal** | Server generates storage filenames (`{version}_{hash[:12]}.bin`). Metadata `file_name` is validated but never used as a filesystem path. |
| **Integrity** | SHA-256 re-computed after save and compared to metadata. File deleted on mismatch. |
| **Size check** | Actual bytes written compared to declared `file_size_bytes`. File deleted on mismatch. |
| **Signature verify** | Ed25519 signature verified against canonical payload if `SIGNING_PUBLIC_KEY_PATH` is set. |
| **Duplicate guard** | Upload rejected with 409 if version already exists in registry. |

---

## Metadata Schema Contract

The canonical schema used by **both** the Rust CLI (producer) and FastAPI backend (consumer):

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `version` | string | ✅ | Semantic version (e.g. "1.2.3") |
| `file_name` | string | ✅ | Original .bin filename (informational; not used for storage) |
| `file_size_bytes` | integer | ✅ | Exact file size in bytes |
| `sha256_hash` | string | ✅ | 64-char lowercase hex SHA-256 digest |
| `signing_alg` | string | ✅ | Always `"ed25519"` |
| `signature` | string | ✅ | Base64-encoded 64-byte Ed25519 signature |
| `key_id` | string | ❌ | Optional key identifier |
| `storage_name` | string | — | Server-generated (added by backend on save) |
| `created_at` | string | — | ISO 8601 timestamp (added by backend on save) |

---

## Quick Start

### Prerequisites
- Docker & Docker Compose
- Rust toolchain (for CLI)
- Python 3.12+ (for backend)

### 1. Start the MQTT Broker
```bash
cd three-body-ota
docker compose up -d
```

### 2. Generate Signing Keys
```bash
cd cli-tool
cargo run -- keygen --output firmware_key
# Creates: firmware_key.secret (private), firmware_key.pub (public)
```

### 3. Sign a Firmware Binary
```bash
cargo run -- sign \
  --file path/to/firmware.bin \
  --version 1.0.0 \
  --key firmware_key.secret \
  --key-id my-key-1 \
  --output metadata.json
```

### 4. Start the Backend
```bash
cd backend-and-dash
pip install -r requirements.txt
export ADMIN_TOKEN="your-secret-token"
export SIGNING_PUBLIC_KEY_PATH="../cli-tool/firmware_key.pub"
uvicorn main:app --reload
```

### 5. Upload Signed Firmware
```bash
curl -X POST http://localhost:8000/upload-firmware/ \
  -H "X-Admin-Token: your-secret-token" \
  -F "firmware=@path/to/firmware.bin" \
  -F "metadata=$(cat metadata.json)"
```

### 6. Verify (CLI)
```bash
cd cli-tool
cargo run -- verify \
  --metadata metadata.json \
  --pubkey firmware_key.pub \
  --file path/to/firmware.bin
```

---

## Running Tests

### Rust CLI (9 unit tests)
```bash
cd cli-tool
cargo test
```

### Python Backend (13 security tests)
```bash
cd backend-and-dash
pip install -r requirements.txt
pytest test_security.py -v
```

---

## Tech Stack

| Layer     | Technology                              | Why                                              |
|-----------|-----------------------------------------|--------------------------------------------------|
| Device    | ESP-IDF 5.x, FreeRTOS, C               | Native OTA APIs, dual-partition bootloader        |
| Transport | Eclipse Mosquitto, MQTT v3.1.1          | Lightweight, QoS 1 guaranteed delivery            |
| Backend   | FastAPI, Pydantic, uvicorn, PyNaCl      | Async Python, auto-generated OpenAPI docs         |
| Dashboard | Streamlit                               | Rapid prototyping, real-time data display         |
| CLI Tool  | Rust, ed25519-dalek, sha2, clap         | Ed25519 signing, fast hashing, single binary      |
| Infra     | Docker Compose                          | One-command local environment                     |

---

## Known Limitations

- **No TLS on MQTT** — broker runs plain TCP on 1883. Production would need TLS + ACLs.
- **File-based registry** — `registry.json` is not safe for concurrent writes. Fine for hackathon; production would use a proper database.
- **No firmware chunking** — backend publishes metadata only; 4KB chunk streaming to devices is planned.
- **Single admin token** — no user management or RBAC. Suitable for local development only.

---

## License

MIT
