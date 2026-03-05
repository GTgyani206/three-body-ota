"""
Integration edge-case tests for Three-Body OTA.

These tests require running infrastructure (Mosquitto + FastAPI backend)
and validate the full MQTT → HTTP → download pipeline.

Pre-requisites:
    docker compose up -d
    cd backend-and-dash && uvicorn main:app --host 0.0.0.0 --port 8000 &

Run:  cd tests && python -m pytest test_integration_edge_cases.py -v
"""

import hashlib
import json
import os
import socket
import time

import pytest

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
API_URL = os.environ.get("API_URL", "http://localhost:8000")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "dev-token-change-me")


def _broker_reachable() -> bool:
    try:
        s = socket.create_connection((MQTT_HOST, MQTT_PORT), timeout=2)
        s.close()
        return True
    except OSError:
        return False


def _api_reachable() -> bool:
    try:
        import requests
        return requests.get(f"{API_URL}/health", timeout=2).status_code == 200
    except Exception:
        return False


needs_infra = pytest.mark.skipif(
    not (_broker_reachable() and _api_reachable()),
    reason="MQTT broker or FastAPI backend not reachable",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _upload_firmware(version: str, data: bytes):
    """Upload firmware via the REST API and return the response."""
    import requests
    sha = hashlib.sha256(data).hexdigest()
    fname = f"fw_{version.replace('.', '_')}.bin"
    meta = {
        "version": version,
        "file_name": fname,
        "file_size_bytes": len(data),
        "sha256_hash": sha,
        "signing_alg": "ed25519",
        "signature": "a" * 88,  # dummy — only valid if signing key not configured
    }
    return requests.post(
        f"{API_URL}/upload-firmware/",
        files={"firmware": (fname, data, "application/octet-stream")},
        data={"metadata": json.dumps(meta)},
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=10,
    )


def _delete_firmware(version: str):
    import requests
    requests.delete(
        f"{API_URL}/firmware/{version}",
        headers={"X-Admin-Token": ADMIN_TOKEN},
        timeout=5,
    )


# ===================================================================
# Category 4 — MQTT Edge Cases (Integration)
# ===================================================================

@needs_infra
class TestMQTTMessageDelivery:
    """4.1–4.7: MQTT message flow edge cases with a real broker."""

    def test_subscriber_receives_ota_trigger(self):
        """Verify the backend publishes to firmware/update on upload."""
        import paho.mqtt.client as mqtt

        received = []

        def on_message(client, userdata, msg):
            received.append(json.loads(msg.payload.decode()))

        sub = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="test-subscriber",
        )
        sub.on_message = on_message
        sub.connect(MQTT_HOST, MQTT_PORT)
        sub.subscribe("firmware/update", qos=1)
        sub.loop_start()

        time.sleep(1)  # ensure subscription is active
        ver = f"int.4.1.{int(time.time())}"
        _upload_firmware(ver, os.urandom(256))
        time.sleep(3)  # wait for delivery

        sub.loop_stop()
        sub.disconnect()
        _delete_firmware(ver)

        versions = [m.get("version") for m in received]
        assert ver in versions, f"Expected {ver} in {versions}"

    def test_malformed_json_does_not_crash_subscriber(self):
        """4.4: Publish garbage to firmware/update — subscriber must survive."""
        import paho.mqtt.client as mqtt

        errors = []

        def on_message(client, userdata, msg):
            try:
                json.loads(msg.payload.decode())
            except json.JSONDecodeError:
                errors.append("bad_json")

        sub = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="test-malformed",
        )
        sub.on_message = on_message
        sub.connect(MQTT_HOST, MQTT_PORT)
        sub.subscribe("firmware/update", qos=1)
        sub.loop_start()
        time.sleep(1)

        pub = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="three-body-backend",
        )
        pub.connect(MQTT_HOST, MQTT_PORT)

        # Send various malformed payloads
        for payload in [b"not json", b"", b"{}", b'{"version":null}',
                        b'{"version":"x"}']:
            pub.publish("firmware/update", payload, qos=1)
        time.sleep(2)

        pub.disconnect()
        sub.loop_stop()
        sub.disconnect()

        assert len(errors) >= 1, "At least 'not json' should trigger parse error"

    def test_large_mqtt_message_dropped_by_broker(self):
        """4.5: Message > max_packet_size (10 KB) should be dropped."""
        import paho.mqtt.client as mqtt

        received = []

        def on_message(client, userdata, msg):
            received.append(msg.payload)

        sub = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="test-large-msg",
        )
        sub.on_message = on_message
        sub.connect(MQTT_HOST, MQTT_PORT)
        sub.subscribe("firmware/update", qos=1)
        sub.loop_start()
        time.sleep(1)

        pub = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="three-body-backend",
        )
        pub.connect(MQTT_HOST, MQTT_PORT)
        big_payload = b"A" * 20_000
        pub.publish("firmware/update", big_payload, qos=1)
        time.sleep(2)

        pub.disconnect()
        sub.loop_stop()
        sub.disconnect()

        large_msgs = [m for m in received if len(m) > 10240]
        assert len(large_msgs) == 0, "Broker should drop messages > 10 KB"

    def test_duplicate_ota_trigger_delivered_twice(self):
        """4.2: Same OTA message published twice — both delivered (QoS 1)."""
        import paho.mqtt.client as mqtt

        received = []

        def on_message(client, userdata, msg):
            received.append(json.loads(msg.payload.decode()))

        sub = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="test-dup-trigger",
        )
        sub.on_message = on_message
        sub.connect(MQTT_HOST, MQTT_PORT)
        sub.subscribe("firmware/update", qos=1)
        sub.loop_start()
        time.sleep(1)

        pub = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="three-body-backend",
        )
        pub.connect(MQTT_HOST, MQTT_PORT)

        trigger = json.dumps({
            "version": "dup.1.0", "sha256_hash": "a" * 64,
            "file_size_bytes": 1024,
        })
        pub.publish("firmware/update", trigger, qos=1)
        pub.publish("firmware/update", trigger, qos=1)
        time.sleep(2)

        pub.disconnect()
        sub.loop_stop()
        sub.disconnect()

        dup_versions = [m["version"] for m in received if m["version"] == "dup.1.0"]
        assert len(dup_versions) >= 2


@needs_infra
class TestMQTTACLEnforcement:
    """7.2: Verify ACL blocks unauthorized publishes to firmware/#."""

    def test_anonymous_publish_blocked_by_acl(self):
        """Anonymous clients cannot write to firmware/update."""
        import paho.mqtt.client as mqtt

        received = []

        def on_message(client, userdata, msg):
            received.append(msg.payload)

        sub = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="test-acl-sub",
        )
        sub.on_message = on_message
        sub.connect(MQTT_HOST, MQTT_PORT)
        sub.subscribe("firmware/update", qos=1)
        sub.loop_start()
        time.sleep(1)

        # Publish as anonymous (no username)
        anon = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="anonymous-attacker",
        )
        anon.connect(MQTT_HOST, MQTT_PORT)
        anon.publish("firmware/update", b'{"version":"evil"}', qos=1)
        time.sleep(2)

        anon.disconnect()
        sub.loop_stop()
        sub.disconnect()

        evil_msgs = [m for m in received if b"evil" in m]
        assert len(evil_msgs) == 0, "ACL should block anonymous writes to firmware/#"


# ===================================================================
# Category 6 — Download Edge Cases (Integration)
# ===================================================================

@needs_infra
class TestDownloadEdgeCases:
    """6.x: HTTP download behaviour under adverse conditions."""

    def test_concurrent_downloads_same_version(self):
        """Multiple devices downloading simultaneously."""
        import requests
        from concurrent.futures import ThreadPoolExecutor

        ver = f"int.6.0.{int(time.time())}"
        data = os.urandom(4096)
        resp = _upload_firmware(ver, data)
        if resp.status_code != 201:
            pytest.skip("Upload failed (signing key mismatch?)")

        expected_hash = hashlib.sha256(data).hexdigest()

        def download(_):
            r = requests.get(f"{API_URL}/firmware/{ver}/download", timeout=10)
            return r.status_code, hashlib.sha256(r.content).hexdigest()

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(download, range(5)))

        _delete_firmware(ver)

        for status, h in results:
            assert status == 200
            assert h == expected_hash

    def test_download_returns_correct_content_length(self):
        import requests

        ver = f"int.6.1.{int(time.time())}"
        data = os.urandom(2048)
        resp = _upload_firmware(ver, data)
        if resp.status_code != 201:
            pytest.skip("Upload failed")

        dl = requests.get(f"{API_URL}/firmware/{ver}/download", timeout=10)
        _delete_firmware(ver)

        assert dl.status_code == 200
        assert int(dl.headers.get("content-length", 0)) == len(data)


# ===================================================================
# End-to-End OTA Simulation
# ===================================================================

@needs_infra
class TestEndToEndOTASimulation:
    """Simulates the complete OTA flow from backend to mock device."""

    def test_full_ota_flow_upload_trigger_download_verify(self):
        """Upload → MQTT trigger → download → SHA-256 verify."""
        import paho.mqtt.client as mqtt
        import requests

        ver = f"e2e.{int(time.time())}"
        firmware_data = os.urandom(8192)
        expected_hash = hashlib.sha256(firmware_data).hexdigest()

        # Step 1: Subscribe to OTA trigger (mock device)
        trigger = []

        def on_message(client, userdata, msg):
            trigger.append(json.loads(msg.payload.decode()))

        device = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="mock-esp32",
        )
        device.on_message = on_message
        device.connect(MQTT_HOST, MQTT_PORT)
        device.subscribe("firmware/update", qos=1)
        device.loop_start()
        time.sleep(1)

        # Step 2: Upload firmware
        resp = _upload_firmware(ver, firmware_data)
        if resp.status_code != 201:
            device.loop_stop(); device.disconnect()
            pytest.skip("Upload failed (signing key mismatch?)")

        time.sleep(3)

        # Step 3: Verify MQTT trigger received
        device.loop_stop()
        device.disconnect()

        matching = [t for t in trigger if t.get("version") == ver]
        assert len(matching) >= 1, f"Device never received trigger for {ver}"

        # Step 4: Download firmware (as device would)
        dl = requests.get(f"{API_URL}/firmware/{ver}/download", timeout=10)
        assert dl.status_code == 200

        # Step 5: Verify SHA-256 (as device would)
        actual_hash = hashlib.sha256(dl.content).hexdigest()
        assert actual_hash == expected_hash

        _delete_firmware(ver)
