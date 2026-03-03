"""
Three-Body OTA — FastAPI Firmware Management Server

Handles firmware binary uploads, validates metadata, and publishes
OTA update notifications to the MQTT broker for device consumption.
"""

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any

import aiofiles
import aiofiles.os
import paho.mqtt.client as mqtt
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FIRMWARE_DIR = Path(__file__).parent / "firmware_storage"
FIRMWARE_DIR.mkdir(exist_ok=True)

REGISTRY_PATH = FIRMWARE_DIR / "registry.json"

MQTT_BROKER_HOST = "localhost"
MQTT_BROKER_PORT = 1883
MQTT_TOPIC = "firmware/update"
MQTT_CLIENT_ID = "three-body-backend"

UPLOAD_CHUNK_SIZE = 64 * 1024  # 64 KB streaming chunks

logger = logging.getLogger("three-body-ota")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------

class FirmwareMetadata(BaseModel):
    version: str = Field(..., min_length=1, description="Semantic firmware version")
    file_name: str = Field(..., min_length=1, description="Original .bin filename")
    file_size_bytes: int = Field(..., gt=0, description="Expected file size in bytes")
    sha256_hash: str = Field(..., pattern=r"^[a-fA-F0-9]{64}$", description="SHA-256 hex digest")


# ---------------------------------------------------------------------------
# Registry — file-based JSON persistence
# ---------------------------------------------------------------------------

async def _load_registry() -> dict[str, Any]:
    if not REGISTRY_PATH.exists():
        return {}
    try:
        async with aiofiles.open(REGISTRY_PATH, "r") as f:
            return json.loads(await f.read())
    except (json.JSONDecodeError, OSError):
        return {}


async def _save_registry(registry: dict[str, Any]) -> None:
    async with aiofiles.open(REGISTRY_PATH, "w") as f:
        await f.write(json.dumps(registry, indent=2))


async def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 of a file using async I/O in 64 KB chunks."""
    hasher = hashlib.sha256()
    async with aiofiles.open(path, "rb") as f:
        while True:
            chunk = await f.read(UPLOAD_CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


# ---------------------------------------------------------------------------
# MQTT helper
# ---------------------------------------------------------------------------

class MQTTPublisher:
    """Thin wrapper around paho-mqtt with graceful failure handling."""

    def __init__(self, host: str, port: int, client_id: str) -> None:
        self._host = host
        self._port = port
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        self._connected = False

    def connect(self) -> None:
        try:
            self._client.connect(self._host, self._port, keepalive=60)
            self._client.loop_start()
            self._connected = True
            logger.info("MQTT connected to %s:%d", self._host, self._port)
        except Exception as exc:
            self._connected = False
            logger.warning("MQTT connection failed (%s) — server will continue without MQTT", exc)

    def _publish_sync(self, topic: str, payload: str, qos: int = 1) -> bool:
        """Blocking publish — intended to be called via asyncio.to_thread()."""
        if not self._connected:
            logger.warning("MQTT not connected — skipping publish to '%s'", topic)
            return False
        try:
            info = self._client.publish(topic, payload, qos=qos)
            info.wait_for_publish(timeout=5.0)
            logger.info("MQTT published to '%s' (mid=%s)", topic, info.mid)
            return True
        except Exception as exc:
            logger.error("MQTT publish failed: %s", exc)
            return False

    async def publish(self, topic: str, payload: str, qos: int = 1) -> bool:
        return await asyncio.to_thread(self._publish_sync, topic, payload, qos)

    def disconnect(self) -> None:
        if self._connected:
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
            logger.info("MQTT disconnected")


mqtt_publisher = MQTTPublisher(MQTT_BROKER_HOST, MQTT_BROKER_PORT, MQTT_CLIENT_ID)

# ---------------------------------------------------------------------------
# Application lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    mqtt_publisher.connect()
    yield
    mqtt_publisher.disconnect()


app = FastAPI(
    title="Three-Body OTA Backend",
    description="Firmware upload & MQTT OTA trigger service",
    version="0.1.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    return {"status": "ok", "mqtt_connected": mqtt_publisher._connected}


@app.post("/upload-firmware/", status_code=201)
async def upload_firmware(
    firmware: UploadFile = File(..., description="Compiled .bin firmware file"),
    metadata: str = Form(..., description="JSON string matching FirmwareMetadata schema"),
):
    # --- 1. Validate metadata JSON -------------------------------------------
    try:
        raw = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in metadata: {exc}")

    try:
        meta = FirmwareMetadata(**raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Metadata validation failed: {exc}")

    # --- 2. Validate file extension ------------------------------------------
    if not firmware.filename or not firmware.filename.endswith(".bin"):
        raise HTTPException(status_code=400, detail="Firmware file must have a .bin extension")

    # --- 3. Duplicate version check ------------------------------------------
    registry = await _load_registry()
    if meta.version in registry:
        raise HTTPException(
            status_code=409,
            detail=f"Firmware version '{meta.version}' already exists in registry",
        )

    # --- 4. Async stream-save the .bin to disk --------------------------------
    dest_path = FIRMWARE_DIR / meta.file_name
    total_bytes = 0
    try:
        async with aiofiles.open(dest_path, "wb") as f:
            while True:
                chunk = await firmware.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                await f.write(chunk)
                total_bytes += len(chunk)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save firmware file: {exc}")

    if total_bytes != meta.file_size_bytes:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"File size mismatch: expected {meta.file_size_bytes} bytes, received {total_bytes}",
        )

    # --- 5. SHA-256 re-verification ------------------------------------------
    computed_hash = await _compute_sha256(dest_path)
    if computed_hash != meta.sha256_hash.lower():
        dest_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"SHA-256 mismatch: expected {meta.sha256_hash}, computed {computed_hash}",
        )

    logger.info("Firmware verified & saved: %s (%d bytes, sha256=%s)", dest_path.name, total_bytes, computed_hash)

    # --- 6. Persist to registry ----------------------------------------------
    registry[meta.version] = meta.model_dump()
    await _save_registry(registry)

    # --- 7. Publish metadata to MQTT (non-blocking) --------------------------
    mqtt_published = await mqtt_publisher.publish(MQTT_TOPIC, meta.model_dump_json(), qos=1)

    return {
        "status": "ok",
        "file_name": meta.file_name,
        "file_size_bytes": total_bytes,
        "version": meta.version,
        "sha256_verified": True,
        "mqtt_published": mqtt_published,
    }


@app.get("/firmware/")
async def list_firmware():
    registry = await _load_registry()
    return {"count": len(registry), "versions": list(registry.values())}


@app.get("/firmware/{version}")
async def get_firmware_metadata(version: str):
    registry = await _load_registry()
    if version not in registry:
        raise HTTPException(status_code=404, detail=f"Firmware version '{version}' not found")
    return registry[version]


@app.get("/firmware/{version}/download")
async def download_firmware(version: str):
    registry = await _load_registry()
    if version not in registry:
        raise HTTPException(status_code=404, detail=f"Firmware version '{version}' not found")

    file_name = registry[version]["file_name"]
    file_path = FIRMWARE_DIR / file_name

    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Binary file '{file_name}' missing from storage")

    return FileResponse(
        path=file_path,
        filename=file_name,
        media_type="application/octet-stream",
    )


@app.delete("/firmware/{version}")
async def delete_firmware(version: str):
    registry = await _load_registry()
    if version not in registry:
        raise HTTPException(status_code=404, detail=f"Firmware version '{version}' not found")

    file_name = registry[version]["file_name"]
    file_path = FIRMWARE_DIR / file_name
    file_path.unlink(missing_ok=True)

    del registry[version]
    await _save_registry(registry)

    logger.info("Firmware deleted: version=%s file=%s", version, file_name)
    return {"status": "deleted", "version": version}
