# Changelog

## Unreleased

- Hardened backend MQTT TLS setup to fail fast when TLS is enabled but misconfigured.
- Upgraded Streamlit dependency to 1.54.0.
- Removed unmaintained streamlit-autorefresh dependency and switched dashboard refresh to native `st.fragment(run_every=...)`.
- Added safer Streamlit MQTT startup handling (safe port parsing, unique client IDs, graceful connection failure path).
- Updated Docker Compose healthcheck for Mosquitto to avoid package installs at probe time.
- Replaced hardcoded Compose ADMIN_TOKEN with required environment variable substitution.
- Corrected Mosquitto listener option usage (`allow_anonymous`).
- Scoped firmware write ACL to backend user and switched device firmware read rule to pattern-based ACL.
- Removed committed key/csr/srl certificate artifacts from source control and added ignore rules for cert materials.
