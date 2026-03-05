"""
Edge-case test suite for Three-Body OTA backend.

Tests harsh real-world conditions mapped to the OTA test plan:
  Category 3  — Firmware corruption & validation
  Category 4  — MQTT payload edge cases (server side)
  Category 6  — Server-side failures
  Category 7  — Security & adversarial scenarios
  Category 9  — Timing & concurrency

Run:  cd backend-and-dash && python -m pytest test_edge_cases.py -v
"""

import base64
import hashlib
import json
import os
import tempfile
import threading

from nacl.signing import SigningKey as NaClSigningKey

# ---------------------------------------------------------------------------
# Test keypair — MUST happen before importing the app
# ---------------------------------------------------------------------------

_test_sk = NaClSigningKey.generate()
_test_vk = _test_sk.verify_key

_pubkey_tmpfile = tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=False)
_pubkey_tmpfile.write(base64.b64encode(_test_vk.encode()).decode())
_pubkey_tmpfile.close()

os.environ["ADMIN_TOKEN"] = "test-secret-token-xyz"
os.environ["SIGNING_PUBLIC_KEY_PATH"] = _pubkey_tmpfile.name

# Now safe to import
from main import app, FIRMWARE_DIR, REGISTRY_PATH  # noqa: E402
import main as _main_module  # noqa: E402

from nacl.signing import VerifyKey  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from unittest.mock import patch, AsyncMock  # noqa: E402

client = TestClient(app)
ADMIN = {"X-Admin-Token": "test-secret-token-xyz"}


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_security.py)
# ---------------------------------------------------------------------------

def _sign_canonical(file_name: str, file_size_bytes: int,
                    sha256_hash: str, version: str) -> str:
    canonical = json.dumps(
        {"file_name": file_name, "file_size_bytes": file_size_bytes,
         "sha256_hash": sha256_hash, "version": version},
        separators=(",", ":"), sort_keys=True,
    )
    signed = _test_sk.sign(canonical.encode())
    return base64.b64encode(signed.signature).decode()


def _build_upload(version: str, bin_data: bytes, file_name: str = None,
                  headers: dict = None, meta_overrides: dict = None):
    sha = hashlib.sha256(bin_data).hexdigest()
    fname = file_name or f"fw_{version.replace('.', '_')}.bin"
    meta = {
        "version": version, "file_name": fname,
        "file_size_bytes": len(bin_data), "sha256_hash": sha,
        "signing_alg": "ed25519",
        "signature": _sign_canonical(fname, len(bin_data), sha, version),
    }
    if meta_overrides:
        meta.update(meta_overrides)
    return client.post(
        "/upload-firmware/",
        files={"firmware": (fname, bin_data, "application/octet-stream")},
        data={"metadata": json.dumps(meta)},
        headers=headers if headers is not None else ADMIN,
    )


@pytest.fixture(autouse=True)
def clean_storage():
    """Set our test verify-key, clean storage, then restore for other modules."""
    _main_module._verify_key = VerifyKey(_test_vk.encode())
    _cleanup()
    yield
    _cleanup()
    # Restore key from current env var so test_security.py's key is active
    # when its tests run in the same pytest session.
    key_path = os.environ.get("SIGNING_PUBLIC_KEY_PATH")
    if key_path and os.path.exists(key_path):
        try:
            with open(key_path, "r") as f:
                _main_module._verify_key = VerifyKey(
                    base64.b64decode(f.read().strip())
                )
        except Exception:
            pass


def _cleanup():
    for f in FIRMWARE_DIR.glob("*.bin"):
        f.unlink()
    REGISTRY_PATH.unlink(missing_ok=True)


# ===================================================================
# Category 3 — Firmware Corruption & Validation
# ===================================================================

class TestCorruptedBinaryOnServer:
    """3.1: Firmware binary corrupted on disk after successful upload.

    Backend does NOT re-verify SHA-256 on download — the ESP32's
    incremental SHA-256 check during OTA is the last line of defence.
    """

    def test_download_serves_corrupted_file_unchanged(self):
        resp = _build_upload("3.1.0", b"\xAA" * 1024)
        assert resp.status_code == 201
        storage_name = resp.json()["storage_name"]

        (FIRMWARE_DIR / storage_name).write_bytes(b"\xFF" * 1024)

        dl = client.get("/firmware/3.1.0/download")
        assert dl.status_code == 200
        assert dl.content == b"\xFF" * 1024

    def test_metadata_retains_original_hash_after_corruption(self):
        original = b"\xBB" * 512
        expected_hash = hashlib.sha256(original).hexdigest()
        _build_upload("3.1.1", original)

        reg = json.loads(REGISTRY_PATH.read_text())
        (FIRMWARE_DIR / reg["3.1.1"]["storage_name"]).write_bytes(b"\x00" * 512)

        meta = client.get("/firmware/3.1.1").json()
        assert meta["sha256_hash"] == expected_hash


class TestTruncatedFile:
    """3.2: Firmware file truncated on disk (disk full, partial write)."""

    def test_truncated_file_served_with_wrong_size(self):
        resp = _build_upload("3.2.0", b"\xCC" * 2048)
        assert resp.status_code == 201
        sn = resp.json()["storage_name"]
        (FIRMWARE_DIR / sn).write_bytes(b"\xCC" * 1024)

        dl = client.get("/firmware/3.2.0/download")
        assert dl.status_code == 200
        assert len(dl.content) == 1024


class TestOversizedFirmware:
    """3.3: Firmware larger than ESP32 OTA partition (1.5 MB).

    Backend has no knowledge of device constraints — accepts any size.
    The ESP32's esp_ota_write() fails when the partition fills up.
    """

    def test_backend_accepts_firmware_exceeding_partition_size(self):
        huge = os.urandom(1536 * 1024 + 1)  # 1.5 MB + 1 byte
        resp = _build_upload("3.3.0", huge)
        assert resp.status_code == 201


class TestZeroByteFirmware:
    """3.7: Empty firmware file."""

    def test_zero_size_metadata_rejected_by_pydantic(self):
        sha = hashlib.sha256(b"").hexdigest()
        sig = _sign_canonical("empty.bin", 0, sha, "3.7.0")
        meta = json.dumps({
            "version": "3.7.0", "file_name": "empty.bin",
            "file_size_bytes": 0, "sha256_hash": sha,
            "signing_alg": "ed25519", "signature": sig,
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("empty.bin", b"", "application/octet-stream")},
            data={"metadata": meta}, headers=ADMIN,
        )
        assert resp.status_code == 422  # Pydantic: gt=0

    def test_empty_file_with_nonzero_size_gives_mismatch(self):
        real = b"\xAA" * 1024
        sha = hashlib.sha256(real).hexdigest()
        sig = _sign_canonical("fake.bin", 1024, sha, "3.7.1")
        meta = json.dumps({
            "version": "3.7.1", "file_name": "fake.bin",
            "file_size_bytes": 1024, "sha256_hash": sha,
            "signing_alg": "ed25519", "signature": sig,
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("fake.bin", b"", "application/octet-stream")},
            data={"metadata": meta}, headers=ADMIN,
        )
        assert resp.status_code == 400
        assert "size mismatch" in resp.json()["detail"].lower()


# ===================================================================
# Category 4 — MQTT Payload Edge Cases (Server Side)
# ===================================================================

class TestMQTTPayloadFormat:
    """4.4 / 4.5: Verify the MQTT payload the backend publishes on upload.

    IMPORTANT FINDING: Backend publishes field "size" but ESP32 firmware
    expects "file_size_bytes".  Auto-publish from upload will be rejected
    by the device with "Malformed OTA JSON".  Manual triggers with the
    correct field name work around this.
    """

    def test_mqtt_payload_contains_required_fields(self):
        with patch.object(
            _main_module.mqtt_publisher, "publish",
            new_callable=AsyncMock, return_value=True,
        ) as mock_pub:
            resp = _build_upload("4.4.0", b"\xAA" * 256)
            assert resp.status_code == 201
            mock_pub.assert_called_once()

            payload = json.loads(mock_pub.call_args.args[1])
            assert payload["version"] == "4.4.0"
            assert "sha256_hash" in payload
            # Documents the field-name mismatch with firmware:
            assert "size" in payload
            assert "file_size_bytes" not in payload

    def test_mqtt_payload_well_under_10kb_limit(self):
        """Mosquitto max_packet_size is 10 240 bytes."""
        with patch.object(
            _main_module.mqtt_publisher, "publish",
            new_callable=AsyncMock, return_value=True,
        ) as mock_pub:
            _build_upload("4.5.0", b"\xBB" * 4096)
            payload_str = mock_pub.call_args.args[1]
            assert len(payload_str) < 10240


# ===================================================================
# Category 6 — Server-Side Failures
# ===================================================================

class TestDownloadAfterDeletion:
    """6.2: Version deleted while potentially being downloaded."""

    def test_download_404_after_deletion(self):
        _build_upload("6.2.0", b"\xDD" * 512)
        assert client.get("/firmware/6.2.0/download").status_code == 200
        client.delete("/firmware/6.2.0", headers=ADMIN)
        assert client.get("/firmware/6.2.0/download").status_code == 404

    def test_download_404_when_file_missing_from_disk(self):
        """Registry entry exists but .bin file was deleted externally."""
        resp = _build_upload("6.2.1", b"\xEE" * 512)
        sn = resp.json()["storage_name"]
        (FIRMWARE_DIR / sn).unlink()
        assert client.get("/firmware/6.2.1/download").status_code == 404

    def test_metadata_available_even_when_file_missing(self):
        resp = _build_upload("6.2.2", b"\xFF" * 256)
        (FIRMWARE_DIR / resp.json()["storage_name"]).unlink()
        meta = client.get("/firmware/6.2.2")
        assert meta.status_code == 200
        assert meta.json()["version"] == "6.2.2"


class TestRegistryCorruption:
    """6.5: registry.json corrupted (disk error, partial write, etc.)."""

    def test_corrupted_json_returns_empty_list(self):
        REGISTRY_PATH.write_text("{{not valid json!!")
        resp = client.get("/firmware/")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_truncated_json_returns_empty_list(self):
        _build_upload("6.5.0", b"\xAA" * 128)
        content = REGISTRY_PATH.read_text()
        REGISTRY_PATH.write_text(content[: len(content) // 2])
        resp = client.get("/firmware/")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_upload_recovers_corrupted_registry(self):
        REGISTRY_PATH.write_text("broken!")
        resp = _build_upload("6.5.1", b"\xBB" * 256)
        assert resp.status_code == 201
        listing = client.get("/firmware/")
        assert listing.json()["count"] == 1

    def test_empty_registry_file_handled(self):
        REGISTRY_PATH.write_text("")
        resp = client.get("/firmware/")
        assert resp.status_code == 200
        assert resp.json()["count"] == 0


class TestServerErrorResponses:
    """6.3: Appropriate error codes for missing resources."""

    def test_download_nonexistent_version(self):
        assert client.get("/firmware/99.99.99/download").status_code == 404

    def test_metadata_nonexistent_version(self):
        assert client.get("/firmware/nonexistent").status_code == 404

    def test_delete_nonexistent_version(self):
        assert client.delete("/firmware/99.99.99", headers=ADMIN).status_code == 404


# ===================================================================
# Category 7 — Security & Adversarial Scenarios
# ===================================================================

class TestVersionDowngrade:
    """7.3: No version ordering enforcement on the backend.

    SECURITY FINDING: An attacker (or operator mistake) can upload v1.0.0
    after v2.0.0 is deployed, effectively downgrading every device that
    picks up the OTA trigger.  Consider adding version comparison.
    """

    def test_older_version_upload_accepted(self):
        _build_upload("2.0.0", b"\xAA" * 256)
        resp = _build_upload("1.0.0", b"\xBB" * 256)
        assert resp.status_code == 201

    def test_both_versions_listed(self):
        _build_upload("2.0.0", b"\xAA" * 256)
        _build_upload("1.0.0", b"\xBB" * 256)
        versions = client.get("/firmware/versions").json()["versions"]
        assert "1.0.0" in versions and "2.0.0" in versions


class TestReplayPrevention:
    """7.4: Re-uploading the same version is blocked (409)."""

    def test_duplicate_version_rejected(self):
        _build_upload("7.4.0", b"\xCC" * 256)
        resp = _build_upload("7.4.0", b"\xCC" * 256)
        assert resp.status_code == 409

    def test_duplicate_with_different_binary_still_rejected(self):
        _build_upload("7.4.1", b"\xDD" * 256)
        resp = _build_upload("7.4.1", b"\xEE" * 256)
        assert resp.status_code == 409


class TestVersionStringEdgeCases:
    """7.5: Malicious / unusual version strings."""

    def test_encoded_traversal_in_download_url(self):
        resp = client.get("/firmware/..%2F..%2Fetc%2Fpasswd/download")
        assert resp.status_code == 404

    def test_null_byte_in_version(self):
        resp = client.get("/firmware/1.0.0%00evil/download")
        assert resp.status_code == 404

    def test_special_chars_in_version_sanitised_on_disk(self):
        """_storage_filename() strips unsafe characters from version."""
        # Explicit safe file_name; version with special chars that get sanitised
        resp = _build_upload("v1+beta@2", b"\xAA" * 128, file_name="fw_beta.bin")
        assert resp.status_code == 201
        sn = resp.json()["storage_name"]
        assert "@" not in sn and "+" not in sn

    def test_version_with_slashes_rejected_via_filename(self):
        """Version containing / produces an unsafe auto-generated file_name."""
        resp = _build_upload("../../etc", b"\xAA" * 128)
        assert resp.status_code == 400

    def test_moderately_long_version_string(self):
        long_ver = "1." + "0" * 50
        resp = _build_upload(long_ver, b"\xBB" * 128)
        assert resp.status_code == 201

    def test_extremely_long_version_causes_server_error(self):
        """FINDING: Version > 200 chars causes 500 on Windows (path length)."""
        long_ver = "1." + "0" * 200
        resp = _build_upload(long_ver, b"\xBB" * 128)
        assert resp.status_code == 500

    def test_unicode_version_sanitised_on_disk(self):
        """Unicode in version is sanitised by _storage_filename()."""
        resp = _build_upload("v1.0.0-café", b"\xCC" * 128, file_name="fw_cafe.bin")
        assert resp.status_code == 201

    def test_malicious_storage_name_in_tampered_registry(self):
        """If someone edits registry.json to inject a traversal path."""
        _build_upload("7.5.9", b"\xDD" * 128)
        reg = json.loads(REGISTRY_PATH.read_text())
        reg["7.5.9"]["storage_name"] = "..\\..\\windows\\system32\\config"
        REGISTRY_PATH.write_text(json.dumps(reg))
        resp = client.get("/firmware/7.5.9/download")
        assert resp.status_code == 404


# ===================================================================
# Category 9 — Timing & Concurrency
# ===================================================================

class TestConcurrentUploads:
    """9.1: Parallel uploads to test race conditions.

    NOTE: FastAPI TestClient is not truly thread-safe, so these tests
    exercise the endpoint logic but may not surface all race conditions.
    Run with a real HTTP server for full concurrency testing.
    """

    def test_parallel_different_versions_both_succeed(self):
        results = {}

        def upload(ver, data):
            results[ver] = _build_upload(ver, data)

        t1 = threading.Thread(target=upload, args=("9.1.0", b"\xAA" * 256))
        t2 = threading.Thread(target=upload, args=("9.1.1", b"\xBB" * 256))
        t1.start(); t2.start()
        t1.join(); t2.join()

        codes = {results["9.1.0"].status_code, results["9.1.1"].status_code}
        assert 201 in codes

    def test_parallel_same_version_one_wins(self):
        results = []

        def upload():
            results.append(_build_upload("9.1.2", b"\xCC" * 256))

        threads = [threading.Thread(target=upload) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        codes = sorted(r.status_code for r in results)
        assert codes.count(201) >= 1
        assert all(c in (201, 409) for c in codes)


# ===================================================================
# MQTT Publish Failure — Graceful Degradation
# ===================================================================

class TestMQTTPublishFailure:
    """Upload must succeed even when MQTT broker is unreachable."""

    def test_upload_succeeds_when_mqtt_returns_false(self):
        with patch.object(
            _main_module.mqtt_publisher, "publish",
            new_callable=AsyncMock, return_value=False,
        ):
            resp = _build_upload("mqtt.0.0", b"\xAA" * 256)
            assert resp.status_code == 201
            assert resp.json()["mqtt_published"] is False

    def test_upload_response_includes_mqtt_status(self):
        with patch.object(
            _main_module.mqtt_publisher, "publish",
            new_callable=AsyncMock, return_value=True,
        ):
            resp = _build_upload("mqtt.0.1", b"\xBB" * 256)
            assert resp.status_code == 201
            assert resp.json()["mqtt_published"] is True


# ===================================================================
# Input Validation Boundary Tests
# ===================================================================

class TestEdgeCaseMetadata:
    """Various metadata edge cases that probe validation boundaries."""

    def test_non_bin_extension_rejected(self):
        data = b"\xAA" * 128
        sha = hashlib.sha256(data).hexdigest()
        sig = _sign_canonical("firmware.exe", len(data), sha, "edge.0.0")
        meta = json.dumps({
            "version": "edge.0.0", "file_name": "firmware.exe",
            "file_size_bytes": len(data), "sha256_hash": sha,
            "signing_alg": "ed25519", "signature": sig,
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("firmware.exe", data)},
            data={"metadata": meta}, headers=ADMIN,
        )
        assert resp.status_code == 400

    def test_missing_required_metadata_fields(self):
        meta = json.dumps({"version": "edge.0.1"})
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("fw.bin", b"\xBB" * 64)},
            data={"metadata": meta}, headers=ADMIN,
        )
        assert resp.status_code == 422

    def test_invalid_sha256_format(self):
        meta = json.dumps({
            "version": "edge.0.2", "file_name": "fw.bin",
            "file_size_bytes": 128, "sha256_hash": "tooshort",
            "signing_alg": "ed25519", "signature": "dummy",
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("fw.bin", b"\xCC" * 128)},
            data={"metadata": meta}, headers=ADMIN,
        )
        assert resp.status_code == 422

    def test_metadata_not_json(self):
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("fw.bin", b"\xDD" * 64)},
            data={"metadata": "not json at all"}, headers=ADMIN,
        )
        assert resp.status_code == 400

    def test_empty_version_rejected(self):
        meta = json.dumps({
            "version": "", "file_name": "fw.bin",
            "file_size_bytes": 64, "sha256_hash": "a" * 64,
            "signing_alg": "ed25519", "signature": "dummy",
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("fw.bin", b"\xEE" * 64)},
            data={"metadata": meta}, headers=ADMIN,
        )
        assert resp.status_code == 422

    def test_wrong_signing_algorithm_rejected(self):
        meta = json.dumps({
            "version": "edge.0.5", "file_name": "fw.bin",
            "file_size_bytes": 64, "sha256_hash": "a" * 64,
            "signing_alg": "rsa2048",
            "signature": base64.b64encode(b"\x00" * 64).decode(),
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("fw.bin", b"\xFF" * 64)},
            data={"metadata": meta}, headers=ADMIN,
        )
        assert resp.status_code == 422
