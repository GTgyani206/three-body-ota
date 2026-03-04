"""
Three-Body OTA — FastAPI Firmware Management Server (Hardened)

Handles firmware binary uploads with Ed25519 signature verification,
admin-token auth, path-safe storage, and MQTT OTA notifications.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import ssl
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from contextlib import asynccontextmanager
from typing import Any

import aiofiles
import aiofiles.os
import paho.mqtt.client as mqtt
from fastapi import FastAPI, File, Form, UploadFile, HTTPException, Request, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
FIRMWARE_DIR = Path(__file__).parent / "firmware_storage"
FIRMWARE_DIR.mkdir(exist_ok=True)

REGISTRY_PATH = FIRMWARE_DIR / "registry.json"

MQTT_BROKER_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_BROKER_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_TOPIC = "firmware/update"
MQTT_CLIENT_ID = "three-body-backend"
MQTT_USE_TLS = os.environ.get("MQTT_USE_TLS", "false").lower() in {"1", "true", "yes"}
APP_ENV = os.environ.get("ENV", os.environ.get("NODE_ENV", "development")).lower()
MQTT_TLS_CA_CERT = os.environ.get(
    "MQTT_TLS_CA_CERT",
    "/certs/ca.crt" if APP_ENV in {"development", "dev", "local", "test"} else "",
) or None
MQTT_TLS_CLIENT_CERT = os.environ.get("MQTT_TLS_CLIENT_CERT")
MQTT_TLS_CLIENT_KEY = os.environ.get("MQTT_TLS_CLIENT_KEY")

ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-token-change-me")
SIGNING_PUBLIC_KEY_PATH = os.environ.get("SIGNING_PUBLIC_KEY_PATH")

UPLOAD_CHUNK_SIZE = 64 * 1024  # 64 KB streaming chunks
MQTT_FW_CHUNK_SIZE = 4 * 1024

logger = logging.getLogger("three-body-ota")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _is_production_env() -> bool:
    return APP_ENV in {"production", "prod"}


def _validate_tls_ca_for_environment(ca_cert_path: str | None) -> None:
    if not MQTT_USE_TLS:
        return

    if _is_production_env() and not ca_cert_path:
        raise RuntimeError("MQTT TLS requires MQTT_TLS_CA_CERT in production")

    if not ca_cert_path:
        return

    if _is_production_env():
        try:
            cert_info = ssl._ssl._test_decode_cert(ca_cert_path)
            subject = cert_info.get("subject", ())
            subject_parts = ["=".join(item) for group in subject for item in group]
            subject_joined = " ".join(subject_parts).lower()
            if "threebodyota-dev-ca" in subject_joined or "=dev" in subject_joined:
                raise RuntimeError(
                    "Refusing to start in production with development CA certificate"
                )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"Unable to validate MQTT TLS CA certificate '{ca_cert_path}': {exc}"
            ) from exc

# ---------------------------------------------------------------------------
# Ed25519 public key loading (optional — enables signature verification)
# ---------------------------------------------------------------------------
_verify_key = None
if SIGNING_PUBLIC_KEY_PATH:
    try:
        from nacl.signing import VerifyKey

        with open(SIGNING_PUBLIC_KEY_PATH, "r") as _f:
            _pk_bytes = base64.b64decode(_f.read().strip())
        _verify_key = VerifyKey(_pk_bytes)
        logger.info("Ed25519 public key loaded from %s", SIGNING_PUBLIC_KEY_PATH)
    except ImportError:
        logger.error("PyNaCl not installed — signature verification disabled")
    except Exception as exc:
        logger.error("Failed to load signing public key: %s", exc)

# ---------------------------------------------------------------------------
# Pydantic schema (matches CLI output exactly)
# ---------------------------------------------------------------------------

class FirmwareMetadata(BaseModel):
    version: str = Field(..., min_length=1, description="Semantic firmware version")
    file_name: str = Field(..., min_length=1, description="Original .bin filename")
    file_size_bytes: int = Field(..., gt=0, description="Expected file size in bytes")
    sha256_hash: str = Field(..., pattern=r"^[a-fA-F0-9]{64}$", description="SHA-256 hex digest")
    signing_alg: str = Field(..., pattern=r"^ed25519$", description="Signing algorithm")
    signature: str = Field(..., min_length=1, description="Base64-encoded Ed25519 signature")
    key_id: str | None = Field(default=None, description="Optional key identifier")


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def require_admin(request: Request):
    """Verify X-Admin-Token header on mutating endpoints."""
    token = request.headers.get("X-Admin-Token")
    if not token:
        raise HTTPException(status_code=401, detail="Missing X-Admin-Token header")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid admin token")


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

_SAFE_FILENAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*\.bin$")


def _validate_filename(name: str) -> None:
    """Reject filenames with path traversal, separators, or unsafe patterns."""
    if ".." in name or "/" in name or "\\" in name or "\0" in name:
        raise HTTPException(
            status_code=400, detail=f"Path traversal detected in file_name: {name!r}"
        )
    if name != PurePosixPath(name).name:
        raise HTTPException(
            status_code=400, detail=f"file_name contains directory components: {name!r}"
        )
    if not _SAFE_FILENAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file_name (must be alphanumeric basename ending .bin): {name!r}",
        )


def _storage_filename(version: str, sha256_hash: str) -> str:
    """Generate a server-side filename from version + hash prefix.
    Never trusts client-supplied filenames for disk paths."""
    safe_ver = re.sub(r"[^a-zA-Z0-9._-]", "_", version)
    return f"{safe_ver}_{sha256_hash[:12]}.bin"


def _verify_signature(meta: FirmwareMetadata) -> None:
    """Verify Ed25519 signature over canonical payload. No-op if no public key configured."""
    if _verify_key is None:
        return

    # Canonical payload: compact JSON, alphabetical keys — must match Rust CLI exactly
    canonical = json.dumps(
        {
            "file_name": meta.file_name,
            "file_size_bytes": meta.file_size_bytes,
            "sha256_hash": meta.sha256_hash,
            "version": meta.version,
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    try:
        sig_bytes = base64.b64decode(meta.signature)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 in signature field")

    try:
        _verify_key.verify(canonical.encode(), sig_bytes)
    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Ed25519 signature verification failed — metadata may be tampered",
        )


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
        self._client.on_disconnect = self._on_disconnect
        self._connected = False

        if MQTT_USE_TLS:
            try:
                _validate_tls_ca_for_environment(MQTT_TLS_CA_CERT)
                self._client.tls_set(
                    ca_certs=MQTT_TLS_CA_CERT,
                    certfile=MQTT_TLS_CLIENT_CERT,
                    keyfile=MQTT_TLS_CLIENT_KEY,
                )
                logger.info("MQTT TLS enabled")
            except Exception as exc:
                logger.critical("MQTT TLS configuration failed: %s", exc)
                raise RuntimeError("MQTT TLS is required but misconfigured") from exc

    def _on_disconnect(self, _client, _userdata, flags, reason_code, _properties):
        self._connected = False
        logger.warning("MQTT disconnected (reason=%s, flags=%s)", reason_code, flags)

    def connect(self) -> None:
        try:
            self._client.connect(self._host, self._port, keepalive=60)
            self._client.loop_start()
            self._connected = True
            logger.info("MQTT connected to %s:%d", self._host, self._port)
        except Exception as exc:
            self._connected = False
            logger.warning("MQTT connection failed (%s) — continuing without MQTT", exc)

    def _publish_sync(self, topic: str, payload: str, qos: int = 1) -> bool:
        """Blocking publish — called via asyncio.to_thread()."""
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
    version="0.2.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "mqtt_connected": mqtt_publisher._connected,
        "signature_verification": _verify_key is not None,
    }


@app.post("/upload-firmware/", status_code=201, dependencies=[Depends(require_admin)])
async def upload_firmware(
    firmware: UploadFile = File(..., description="Compiled .bin firmware file"),
    metadata: str = Form(..., description="JSON string matching FirmwareMetadata schema"),
):
    # 1. Parse and validate metadata JSON
    try:
        raw = json.loads(metadata)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON in metadata: {exc}")

    try:
        meta = FirmwareMetadata(**raw)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Metadata validation failed: {exc}")

    # 2. Filename safety — reject traversal, enforce safe basename
    _validate_filename(meta.file_name)

    # 3. Validate uploaded file extension
    if not firmware.filename or not firmware.filename.endswith(".bin"):
        raise HTTPException(status_code=400, detail="Uploaded file must have a .bin extension")

    # 4. Ed25519 signature verification (if public key configured)
    _verify_signature(meta)

    # 5. Duplicate version check
    registry = await _load_registry()
    if meta.version in registry:
        raise HTTPException(
            status_code=409, detail=f"Version '{meta.version}' already exists"
        )

    # 6. Stream-save with server-generated filename (never trust client filename)
    storage_name = _storage_filename(meta.version, meta.sha256_hash)
    dest_path = FIRMWARE_DIR / storage_name
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
        raise HTTPException(status_code=500, detail=f"Failed to save firmware: {exc}")

    # 7. Size verification
    if total_bytes != meta.file_size_bytes:
        dest_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"File size mismatch: expected {meta.file_size_bytes}, received {total_bytes}",
        )

    # 8. SHA-256 re-verification
    computed_hash = await _compute_sha256(dest_path)
    if computed_hash != meta.sha256_hash.lower():
        dest_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400,
            detail=f"SHA-256 mismatch: expected {meta.sha256_hash}, computed {computed_hash}",
        )

    logger.info(
        "Firmware verified: %s v%s (%d bytes, sha256=%s)",
        storage_name, meta.version, total_bytes, computed_hash,
    )

    # 9. Persist to registry
    entry = meta.model_dump()
    entry["storage_name"] = storage_name
    entry["created_at"] = datetime.now(timezone.utc).isoformat()
    registry[meta.version] = entry
    await _save_registry(registry)

    # 10. Publish OTA metadata to MQTT (non-blocking)
    chunks = (total_bytes + MQTT_FW_CHUNK_SIZE - 1) // MQTT_FW_CHUNK_SIZE
    mqtt_payload = {
        "version": meta.version,
        "sha256_hash": computed_hash,
        "size": total_bytes,
        "chunks": chunks,
        "file_name": meta.file_name,
    }
    mqtt_published = await mqtt_publisher.publish(
        MQTT_TOPIC,
        json.dumps(mqtt_payload),
        qos=1,
    )

    if not mqtt_published:
        logger.error("Firmware uploaded but MQTT publish failed for version=%s", meta.version)

    return {
        "status": "ok",
        "version": meta.version,
        "file_name": meta.file_name,
        "storage_name": storage_name,
        "file_size_bytes": total_bytes,
        "sha256_verified": True,
        "signature_verified": _verify_key is not None,
        "mqtt_published": mqtt_published,
    }


@app.get("/firmware/")
async def list_firmware():
    registry = await _load_registry()
    return {"count": len(registry), "versions": list(registry.values())}


@app.get("/firmware/versions")
async def list_firmware_versions():
    registry = await _load_registry()
    return {"count": len(registry), "versions": sorted(registry.keys())}


@app.get("/firmware/{version}")
async def get_firmware_metadata(version: str):
    registry = await _load_registry()
    if version not in registry:
        raise HTTPException(status_code=404, detail=f"Version '{version}' not found")
    return registry[version]


@app.get("/firmware/{version}/download")
async def download_firmware(version: str):
    registry = await _load_registry()
    if version not in registry:
        raise HTTPException(status_code=404, detail=f"Version '{version}' not found")

    entry = registry[version]
    storage_name = entry.get("storage_name", entry["file_name"])
    file_path = FIRMWARE_DIR / storage_name

    if not file_path.exists():
        raise HTTPException(
            status_code=404, detail=f"Binary '{storage_name}' missing from storage"
        )

    return FileResponse(
        path=file_path,
        filename=entry["file_name"],
        media_type="application/octet-stream",
    )


@app.delete("/firmware/{version}", dependencies=[Depends(require_admin)])
async def delete_firmware(version: str):
    registry = await _load_registry()
    if version not in registry:
        raise HTTPException(status_code=404, detail=f"Version '{version}' not found")

    entry = registry[version]
    storage_name = entry.get("storage_name", entry["file_name"])
    (FIRMWARE_DIR / storage_name).unlink(missing_ok=True)

    del registry[version]
    await _save_registry(registry)

    logger.info("Firmware deleted: version=%s", version)
    return {"status": "deleted", "version": version}
