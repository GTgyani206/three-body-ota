"""
Security test suite for Three-Body OTA backend.

Covers: upload success, duplicate rejection, size mismatch, hash mismatch,
path traversal, admin auth, and Ed25519 signature verification.
"""

import base64
import hashlib
import json
import os
import tempfile

# ---------------------------------------------------------------------------
# Test keypair setup — MUST happen before importing the app
# ---------------------------------------------------------------------------
from nacl.signing import SigningKey as NaClSigningKey

_test_sk = NaClSigningKey.generate()
_test_vk = _test_sk.verify_key

_pubkey_tmpfile = tempfile.NamedTemporaryFile(mode="w", suffix=".pub", delete=False)
_pubkey_tmpfile.write(base64.b64encode(_test_vk.encode()).decode())
_pubkey_tmpfile.close()

os.environ["ADMIN_TOKEN"] = "test-secret-token-xyz"
os.environ["SIGNING_PUBLIC_KEY_PATH"] = _pubkey_tmpfile.name

# Now safe to import
from main import app, FIRMWARE_DIR, REGISTRY_PATH  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)
ADMIN = {"X-Admin-Token": "test-secret-token-xyz"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sign_canonical(file_name: str, file_size_bytes: int, sha256_hash: str, version: str) -> str:
    """Produce an Ed25519 signature matching the Rust CLI's canonical format."""
    canonical = json.dumps(
        {
            "file_name": file_name,
            "file_size_bytes": file_size_bytes,
            "sha256_hash": sha256_hash,
            "version": version,
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    signed = _test_sk.sign(canonical.encode())
    return base64.b64encode(signed.signature).decode()


def _build_upload(version: str, bin_data: bytes, file_name: str = None, headers: dict = None,
                  meta_overrides: dict = None):
    """Build and POST a firmware upload request."""
    sha = hashlib.sha256(bin_data).hexdigest()
    fname = file_name or f"fw_{version.replace('.', '_')}.bin"
    meta = {
        "version": version,
        "file_name": fname,
        "file_size_bytes": len(bin_data),
        "sha256_hash": sha,
        "signing_alg": "ed25519",
        "signature": _sign_canonical(fname, len(bin_data), sha, version),
    }
    if meta_overrides:
        # Re-sign if overrides change signing-relevant fields
        meta.update(meta_overrides)
    return client.post(
        "/upload-firmware/",
        files={"firmware": (fname, bin_data, "application/octet-stream")},
        data={"metadata": json.dumps(meta)},
        headers=headers if headers is not None else ADMIN,
    )


@pytest.fixture(autouse=True)
def clean_storage():
    """Ensure each test starts and ends with a clean firmware_storage."""
    _cleanup()
    yield
    _cleanup()


def _cleanup():
    for f in FIRMWARE_DIR.glob("*.bin"):
        f.unlink()
    REGISTRY_PATH.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestUploadSuccess:
    def test_valid_upload_returns_201(self):
        resp = _build_upload("10.0.0", b"\xAA" * 512)
        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "ok"
        assert body["sha256_verified"] is True
        assert body["signature_verified"] is True
        assert body["version"] == "10.0.0"
        assert body["storage_name"].startswith("10.0.0_")

    def test_upload_persists_to_registry(self):
        _build_upload("10.1.0", b"\xBB" * 256)
        resp = client.get("/firmware/10.1.0")
        assert resp.status_code == 200
        assert resp.json()["version"] == "10.1.0"
        assert "created_at" in resp.json()


class TestDuplicateVersion:
    def test_duplicate_version_returns_409(self):
        _build_upload("20.0.0", b"\xCC" * 128)
        resp = _build_upload("20.0.0", b"\xCC" * 128)
        assert resp.status_code == 409
        assert "already exists" in resp.json()["detail"]


class TestSizeMismatch:
    def test_size_mismatch_returns_400(self):
        bin_data = b"\xDD" * 512
        sha = hashlib.sha256(bin_data).hexdigest()
        wrong_size = 999
        sig = _sign_canonical("fw.bin", wrong_size, sha, "30.0.0")
        meta = json.dumps({
            "version": "30.0.0", "file_name": "fw.bin",
            "file_size_bytes": wrong_size, "sha256_hash": sha,
            "signing_alg": "ed25519", "signature": sig,
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("fw.bin", bin_data)},
            data={"metadata": meta},
            headers=ADMIN,
        )
        assert resp.status_code == 400
        assert "size mismatch" in resp.json()["detail"].lower()


class TestHashMismatch:
    def test_hash_mismatch_returns_400(self):
        bin_data = b"\xEE" * 512
        bad_hash = "a" * 64
        sig = _sign_canonical("fw.bin", len(bin_data), bad_hash, "31.0.0")
        meta = json.dumps({
            "version": "31.0.0", "file_name": "fw.bin",
            "file_size_bytes": len(bin_data), "sha256_hash": bad_hash,
            "signing_alg": "ed25519", "signature": sig,
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("fw.bin", bin_data)},
            data={"metadata": meta},
            headers=ADMIN,
        )
        assert resp.status_code == 400
        assert "sha-256 mismatch" in resp.json()["detail"].lower()


class TestPathTraversal:
    def test_dotdot_traversal_rejected(self):
        bin_data = b"\xFF" * 64
        sha = hashlib.sha256(bin_data).hexdigest()
        evil_name = "../../../etc/shadow.bin"
        sig = _sign_canonical(evil_name, len(bin_data), sha, "40.0.0")
        meta = json.dumps({
            "version": "40.0.0", "file_name": evil_name,
            "file_size_bytes": len(bin_data), "sha256_hash": sha,
            "signing_alg": "ed25519", "signature": sig,
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("safe.bin", bin_data)},
            data={"metadata": meta},
            headers=ADMIN,
        )
        assert resp.status_code == 400
        assert "path traversal" in resp.json()["detail"].lower()

    def test_backslash_traversal_rejected(self):
        bin_data = b"\xFF" * 64
        sha = hashlib.sha256(bin_data).hexdigest()
        evil_name = "..\\..\\windows\\system.bin"
        sig = _sign_canonical(evil_name, len(bin_data), sha, "41.0.0")
        meta = json.dumps({
            "version": "41.0.0", "file_name": evil_name,
            "file_size_bytes": len(bin_data), "sha256_hash": sha,
            "signing_alg": "ed25519", "signature": sig,
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("safe.bin", bin_data)},
            data={"metadata": meta},
            headers=ADMIN,
        )
        assert resp.status_code == 400
        assert "path traversal" in resp.json()["detail"].lower()


class TestAdminAuth:
    def test_missing_token_returns_401(self):
        resp = _build_upload("50.0.0", b"\x00" * 64, headers={})
        assert resp.status_code == 401
        assert "missing" in resp.json()["detail"].lower()

    def test_wrong_token_returns_403(self):
        resp = _build_upload("51.0.0", b"\x00" * 64, headers={"X-Admin-Token": "wrong"})
        assert resp.status_code == 403
        assert "invalid" in resp.json()["detail"].lower()

    def test_delete_without_token_returns_401(self):
        resp = client.delete("/firmware/1.0.0")
        assert resp.status_code == 401

    def test_read_endpoints_do_not_require_auth(self):
        assert client.get("/health").status_code == 200
        assert client.get("/firmware/").status_code == 200
        assert client.get("/firmware/nonexistent").status_code == 404


class TestSignatureVerification:
    def test_invalid_signature_rejected(self):
        bin_data = b"\xAB" * 256
        sha = hashlib.sha256(bin_data).hexdigest()
        meta = json.dumps({
            "version": "60.0.0", "file_name": "fw.bin",
            "file_size_bytes": len(bin_data), "sha256_hash": sha,
            "signing_alg": "ed25519",
            "signature": base64.b64encode(b"\x00" * 64).decode(),
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("fw.bin", bin_data)},
            data={"metadata": meta},
            headers=ADMIN,
        )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()

    def test_tampered_metadata_rejected(self):
        """Sign for version 1.0.0, but submit as 2.0.0 — signature must fail."""
        bin_data = b"\xCD" * 256
        sha = hashlib.sha256(bin_data).hexdigest()
        sig_for_v1 = _sign_canonical("fw.bin", len(bin_data), sha, "61.0.0")
        meta = json.dumps({
            "version": "62.0.0",  # different version than what was signed
            "file_name": "fw.bin",
            "file_size_bytes": len(bin_data), "sha256_hash": sha,
            "signing_alg": "ed25519", "signature": sig_for_v1,
        })
        resp = client.post(
            "/upload-firmware/",
            files={"firmware": ("fw.bin", bin_data)},
            data={"metadata": meta},
            headers=ADMIN,
        )
        assert resp.status_code == 400
        assert "signature" in resp.json()["detail"].lower()
