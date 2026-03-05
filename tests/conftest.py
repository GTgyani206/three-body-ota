"""Shared configuration for integration tests."""

import os

# Default test configuration — override via environment variables
os.environ.setdefault("MQTT_HOST", "localhost")
os.environ.setdefault("MQTT_PORT", "1883")
os.environ.setdefault("API_URL", "http://localhost:8000")
os.environ.setdefault("ADMIN_TOKEN", "dev-token-change-me")
