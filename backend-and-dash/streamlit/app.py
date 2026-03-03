import json
import logging
import os
import socket
import ssl
import threading
import uuid
from datetime import datetime, timezone

import pandas as pd
import paho.mqtt.client as mqtt
import streamlit as st

MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
APP_ENV = os.environ.get("ENV", os.environ.get("NODE_ENV", "development")).lower()
_mqtt_port_raw = os.environ.get("MQTT_PORT")
try:
    MQTT_PORT = int(_mqtt_port_raw) if _mqtt_port_raw else 1883
except (TypeError, ValueError):
    logging.warning("Invalid MQTT_PORT=%r; falling back to 1883", _mqtt_port_raw)
    MQTT_PORT = 1883
MQTT_USE_TLS = os.environ.get("MQTT_USE_TLS", "false").lower() in {"1", "true", "yes"}
MQTT_TLS_CA_CERT = os.environ.get(
    "MQTT_TLS_CA_CERT",
    "/certs/ca.crt" if APP_ENV in {"development", "dev", "local", "test"} else "",
) or None
MQTT_TLS_CLIENT_CERT = os.environ.get("MQTT_TLS_CLIENT_CERT")
MQTT_TLS_CLIENT_KEY = os.environ.get("MQTT_TLS_CLIENT_KEY")
MQTT_TOPIC = "device/+/status"


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
        cert_info = ssl._ssl._test_decode_cert(ca_cert_path)
        subject = cert_info.get("subject", ())
        subject_parts = ["=".join(item) for group in subject for item in group]
        subject_joined = " ".join(subject_parts).lower()
        if "threebodyota-dev-ca" in subject_joined or "=dev" in subject_joined:
            raise RuntimeError("Refusing to start in production with development CA certificate")

STATUS_COLOR = {
    "COMMITTED": "🟢",
    "PENDING": "🟡",
    "PENDING_VERIFY": "🟡",
    "DOWNLOADING": "🟡",
    "ROLLED_BACK": "🔴",
    "FAILED": "🔴",
}


class DeviceStatusStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}

    def update(self, device_id: str, payload: dict) -> None:
        status = str(payload.get("status", payload.get("state", "UNKNOWN"))).upper()
        firmware_version = payload.get("firmware_version") or payload.get("version") or "-"
        reboot_count = payload.get("reboot_count")
        now = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._rows[device_id] = {
                "device_id": device_id,
                "firmware_version": firmware_version,
                "status": status,
                "reboot_count": reboot_count if reboot_count is not None else "-",
                "last_update": payload.get("timestamp", now),
                "indicator": STATUS_COLOR.get(status, "⚪"),
            }

    def dataframe(self) -> pd.DataFrame:
        with self._lock:
            rows = list(self._rows.values())
        if not rows:
            return pd.DataFrame(
                columns=["indicator", "device_id", "firmware_version", "status", "reboot_count", "last_update"]
            )
        return pd.DataFrame(rows).sort_values(by="last_update", ascending=False)


@st.cache_resource

def get_store() -> DeviceStatusStore:
    return DeviceStatusStore()


@st.cache_resource

def start_mqtt_listener(_store: DeviceStatusStore):
    unique_client_id = f"three-body-dashboard-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=unique_client_id,
    )

    def on_message(_client, _userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            return

        topic_parts = msg.topic.split("/")
        if len(topic_parts) < 3:
            return

        device_id = topic_parts[1]
        _store.update(device_id, payload)

    client.on_message = on_message

    if MQTT_USE_TLS:
        _validate_tls_ca_for_environment(MQTT_TLS_CA_CERT)
        client.tls_set(
            ca_certs=MQTT_TLS_CA_CERT,
            certfile=MQTT_TLS_CLIENT_CERT,
            keyfile=MQTT_TLS_CLIENT_KEY,
        )

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        client.subscribe(MQTT_TOPIC, qos=1)
        client.loop_start()
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        logging.exception("MQTT listener startup failed")
        st.error(f"MQTT connection failed: {exc}")
        raise

    return client


st.set_page_config(page_title="Three-Body OTA Dashboard", layout="wide")
st.title("Three-Body OTA Device Status")

store = get_store()
try:
    listener = start_mqtt_listener(store)
except (ConnectionRefusedError, TimeoutError, OSError):
    listener = None

st.caption(f"MQTT: {MQTT_HOST}:{MQTT_PORT} | Topic: {MQTT_TOPIC}")

if st.button("Reconnect MQTT"):
    if listener is not None:
        try:
            listener.loop_stop()
            listener.disconnect()
        except Exception:
            pass
    start_mqtt_listener.clear()
    try:
        listener = start_mqtt_listener(store)
    except (ConnectionRefusedError, TimeoutError, OSError):
        listener = None

@st.fragment(run_every="2s")
def render_device_table() -> None:
    frame = store.dataframe()

    if frame.empty:
        st.info("No device status messages received yet.")
    else:
        st.dataframe(
            frame[["indicator", "device_id", "firmware_version", "status", "reboot_count", "last_update"]],
            use_container_width=True,
            hide_index=True,
        )


render_device_table()
