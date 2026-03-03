"""
Three-Body OTA — FastAPI Firmware Management Server

Handles firmware binary uploads, validates metadata, and publishes
OTA update notifications to the MQTT broker for device consumption.
"""

import json
import logging
from pathlib import Path
from contextlib import asynccontextmanager

import paho.mqtt.client as mqtt
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FIRMWARE_DIR = Path(__file__).parent / "firmware_storage"
FIRMWARE_DIR.mkdir(exist_ok=True)

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

    def publish(self, topic: str, payload: str, qos: int = 1) -> bool:
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


@app.post("/upload-firmware/")
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

    # --- 3. Stream-save the .bin to disk -------------------------------------
    dest_path = FIRMWARE_DIR / meta.file_name
    total_bytes = 0
    try:
        with open(dest_path, "wb") as f:
            while True:
                chunk = await firmware.read(UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                f.write(chunk)
                total_bytes += len(chunk)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save firmware file: {exc}")

    if total_bytes != meta.file_size_bytes:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"File size mismatch: expected {meta.file_size_bytes} bytes, received {total_bytes}",
        )

    logger.info("Firmware saved: %s (%d bytes)", dest_path.name, total_bytes)

    # --- 4. Publish metadata to MQTT -----------------------------------------
    mqtt_published = mqtt_publisher.publish(MQTT_TOPIC, meta.model_dump_json(), qos=1)

    return {
        "status": "ok",
        "file_name": meta.file_name,
        "file_size_bytes": total_bytes,
        "version": meta.version,
        "mqtt_published": mqtt_published,
    }
