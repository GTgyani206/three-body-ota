# Three-Body OTA — Complete Test Summary

**Date:** March 5, 2026
**System:** ESP32 DEVKIT V1 (4 MB flash) ↔ Mosquitto MQTT ↔ FastAPI Backend
**Total Tests:** 80 (53 automated, 8 integration, 27 manual hardware)

---

## Test Results Overview

| Suite | File | Tests | Status |
|-------|------|------:|--------|
| Backend Security | `backend-and-dash/test_security.py` | 13 | ✅ 13 passed |
| Backend Edge Cases | `backend-and-dash/test_edge_cases.py` | 40 | ✅ 40 passed |
| Integration (MQTT + HTTP) | `tests/test_integration_edge_cases.py` | 8 | ⏸️ Requires infrastructure |
| Hardware / Firmware | `firmware/TEST_EDGE_CASES.md` | 27 | 📋 Manual procedures |

### How to Run

```bash
# Automated backend tests (no external dependencies)
cd backend-and-dash && python -m pytest test_security.py test_edge_cases.py -v

# Integration tests (requires: docker compose up -d && uvicorn main:app)
cd tests && pip install -r requirements.txt && python -m pytest test_integration_edge_cases.py -v

# Hardware tests — follow step-by-step procedures in firmware/TEST_EDGE_CASES.md
```

---

## 1 · Backend Security Tests (`test_security.py`) — 13 tests ✅

| # | Test | Category | Status |
|---|------|----------|--------|
| 1 | `TestUploadSuccess::test_valid_upload_returns_201` | Upload | ✅ PASS |
| 2 | `TestUploadSuccess::test_upload_persists_to_registry` | Upload | ✅ PASS |
| 3 | `TestDuplicateVersion::test_duplicate_version_returns_409` | Upload | ✅ PASS |
| 4 | `TestSizeMismatch::test_size_mismatch_returns_400` | Validation | ✅ PASS |
| 5 | `TestHashMismatch::test_hash_mismatch_returns_400` | Validation | ✅ PASS |
| 6 | `TestPathTraversal::test_dotdot_traversal_rejected` | Security | ✅ PASS |
| 7 | `TestPathTraversal::test_backslash_traversal_rejected` | Security | ✅ PASS |
| 8 | `TestAdminAuth::test_missing_token_returns_401` | Auth | ✅ PASS |
| 9 | `TestAdminAuth::test_wrong_token_returns_403` | Auth | ✅ PASS |
| 10 | `TestAdminAuth::test_delete_without_token_returns_401` | Auth | ✅ PASS |
| 11 | `TestAdminAuth::test_read_endpoints_do_not_require_auth` | Auth | ✅ PASS |
| 12 | `TestSignatureVerification::test_invalid_signature_rejected` | Crypto | ✅ PASS |
| 13 | `TestSignatureVerification::test_tampered_metadata_rejected` | Crypto | ✅ PASS |

---

## 2 · Backend Edge Case Tests (`test_edge_cases.py`) — 40 tests ✅

### Category 3 — Firmware Corruption & Validation (6 tests)

| # | Test | Plan ID | Status | Finding |
|---|------|---------|--------|---------|
| 14 | `TestCorruptedBinaryOnServer::test_download_serves_corrupted_file_unchanged` | 3.1 | ✅ PASS | Backend does NOT re-verify SHA-256 on download |
| 15 | `TestCorruptedBinaryOnServer::test_metadata_retains_original_hash_after_corruption` | 3.1 | ✅ PASS | Registry shows stale hash after disk corruption |
| 16 | `TestTruncatedFile::test_truncated_file_served_with_wrong_size` | 3.2 | ✅ PASS | Truncated file served; device size check catches it |
| 17 | `TestOversizedFirmware::test_backend_accepts_firmware_exceeding_partition_size` | 3.3 | ✅ PASS | No server-side size limit (device rejects at flash) |
| 18 | `TestZeroByteFirmware::test_zero_size_metadata_rejected_by_pydantic` | 3.7 | ✅ PASS | Pydantic `gt=0` rejects zero-size metadata |
| 19 | `TestZeroByteFirmware::test_empty_file_with_nonzero_size_gives_mismatch` | 3.7 | ✅ PASS | Empty file + non-zero size → 400 size mismatch |

### Category 4 — MQTT Payload Edge Cases (2 tests)

| # | Test | Plan ID | Status | Finding |
|---|------|---------|--------|---------|
| 20 | `TestMQTTPayloadFormat::test_mqtt_payload_contains_required_fields` | 4.4 | ✅ PASS | ⚠️ Backend publishes `"size"` but firmware expects `"file_size_bytes"` |
| 21 | `TestMQTTPayloadFormat::test_mqtt_payload_well_under_10kb_limit` | 4.5 | ✅ PASS | Payload well under Mosquitto 10 KB limit |

### Category 6 — Server-Side Failures (10 tests)

| # | Test | Plan ID | Status | Finding |
|---|------|---------|--------|---------|
| 22 | `TestDownloadAfterDeletion::test_download_404_after_deletion` | 6.2 | ✅ PASS | Clean 404 after version deleted |
| 23 | `TestDownloadAfterDeletion::test_download_404_when_file_missing_from_disk` | 6.2 | ✅ PASS | Registry exists but .bin missing → 404 |
| 24 | `TestDownloadAfterDeletion::test_metadata_available_even_when_file_missing` | 6.2 | ✅ PASS | Metadata endpoint works without binary |
| 25 | `TestRegistryCorruption::test_corrupted_json_returns_empty_list` | 6.5 | ✅ PASS | Corrupted JSON → empty list (graceful) |
| 26 | `TestRegistryCorruption::test_truncated_json_returns_empty_list` | 6.5 | ✅ PASS | Truncated JSON → empty list (graceful) |
| 27 | `TestRegistryCorruption::test_upload_recovers_corrupted_registry` | 6.5 | ✅ PASS | New upload overwrites corrupted registry |
| 28 | `TestRegistryCorruption::test_empty_registry_file_handled` | 6.5 | ✅ PASS | Empty file → empty list (graceful) |
| 29 | `TestServerErrorResponses::test_download_nonexistent_version` | 6.3 | ✅ PASS | 404 for missing version |
| 30 | `TestServerErrorResponses::test_metadata_nonexistent_version` | 6.3 | ✅ PASS | 404 for missing metadata |
| 31 | `TestServerErrorResponses::test_delete_nonexistent_version` | 6.3 | ✅ PASS | 404 for delete on missing version |

### Category 7 — Security & Adversarial (10 tests)

| # | Test | Plan ID | Status | Finding |
|---|------|---------|--------|---------|
| 32 | `TestVersionDowngrade::test_older_version_upload_accepted` | 7.3 | ✅ PASS | ⚠️ No version ordering — downgrade possible |
| 33 | `TestVersionDowngrade::test_both_versions_listed` | 7.3 | ✅ PASS | Old & new versions coexist in registry |
| 34 | `TestReplayPrevention::test_duplicate_version_rejected` | 7.4 | ✅ PASS | 409 on duplicate version |
| 35 | `TestReplayPrevention::test_duplicate_with_different_binary_still_rejected` | 7.4 | ✅ PASS | Different binary same version → 409 |
| 36 | `TestVersionStringEdgeCases::test_encoded_traversal_in_download_url` | 7.5 | ✅ PASS | URL-encoded traversal → 404 |
| 37 | `TestVersionStringEdgeCases::test_null_byte_in_version` | 7.5 | ✅ PASS | Null byte in URL → 404 |
| 38 | `TestVersionStringEdgeCases::test_special_chars_in_version_sanitised_on_disk` | 7.5 | ✅ PASS | `+`, `@` stripped from storage filename |
| 39 | `TestVersionStringEdgeCases::test_version_with_slashes_rejected_via_filename` | 7.5 | ✅ PASS | `../../etc` version → 400 (filename validation) |
| 40 | `TestVersionStringEdgeCases::test_moderately_long_version_string` | 7.5 | ✅ PASS | 50-char version accepted |
| 41 | `TestVersionStringEdgeCases::test_extremely_long_version_causes_server_error` | 7.5 | ✅ PASS | ⚠️ 200+ char version → 500 (OS path length) |
| 42 | `TestVersionStringEdgeCases::test_unicode_version_sanitised_on_disk` | 7.5 | ✅ PASS | Unicode chars sanitised in storage name |
| 43 | `TestVersionStringEdgeCases::test_malicious_storage_name_in_tampered_registry` | 7.5 | ✅ PASS | Traversal in registry → 404 (file not found) |

### Category 9 — Timing & Concurrency (2 tests)

| # | Test | Plan ID | Status | Finding |
|---|------|---------|--------|---------|
| 44 | `TestConcurrentUploads::test_parallel_different_versions_both_succeed` | 9.1 | ✅ PASS | Parallel uploads of different versions work |
| 45 | `TestConcurrentUploads::test_parallel_same_version_one_wins` | 9.1 | ✅ PASS | Same version → one 201, rest 409 |

### MQTT Publish Failure (2 tests)

| # | Test | Plan ID | Status | Finding |
|---|------|---------|--------|---------|
| 46 | `TestMQTTPublishFailure::test_upload_succeeds_when_mqtt_returns_false` | — | ✅ PASS | Upload succeeds even if MQTT is down |
| 47 | `TestMQTTPublishFailure::test_upload_response_includes_mqtt_status` | — | ✅ PASS | Response `mqtt_published` field reflects status |

### Input Validation Boundaries (6 tests)

| # | Test | Plan ID | Status | Finding |
|---|------|---------|--------|---------|
| 48 | `TestEdgeCaseMetadata::test_non_bin_extension_rejected` | — | ✅ PASS | `.exe` file → 400 |
| 49 | `TestEdgeCaseMetadata::test_missing_required_metadata_fields` | — | ✅ PASS | Partial metadata → 422 |
| 50 | `TestEdgeCaseMetadata::test_invalid_sha256_format` | — | ✅ PASS | Short SHA → 422 (Pydantic regex) |
| 51 | `TestEdgeCaseMetadata::test_metadata_not_json` | — | ✅ PASS | Non-JSON string → 400 |
| 52 | `TestEdgeCaseMetadata::test_empty_version_rejected` | — | ✅ PASS | Empty string → 422 (min_length=1) |
| 53 | `TestEdgeCaseMetadata::test_wrong_signing_algorithm_rejected` | — | ✅ PASS | `rsa2048` → 422 (pattern: `^ed25519$`) |

---

## 3 · Integration Tests (`test_integration_edge_cases.py`) — 8 tests ⏸️

> **Requires:** `docker compose up -d` + `uvicorn main:app --host 0.0.0.0 --port 8000`
> Tests are auto-skipped if infrastructure is not available.

### Category 4 — MQTT Edge Cases (4 tests)

| # | Test | Plan ID | Status |
|---|------|---------|--------|
| 54 | `TestMQTTMessageDelivery::test_subscriber_receives_ota_trigger` | 4.1 | ⏸️ PENDING |
| 55 | `TestMQTTMessageDelivery::test_malformed_json_does_not_crash_subscriber` | 4.4 | ⏸️ PENDING |
| 56 | `TestMQTTMessageDelivery::test_large_mqtt_message_dropped_by_broker` | 4.5 | ⏸️ PENDING |
| 57 | `TestMQTTMessageDelivery::test_duplicate_ota_trigger_delivered_twice` | 4.2 | ⏸️ PENDING |

### Category 7 — MQTT ACL Security (1 test)

| # | Test | Plan ID | Status |
|---|------|---------|--------|
| 58 | `TestMQTTACLEnforcement::test_anonymous_publish_blocked_by_acl` | 7.2 | ⏸️ PENDING |

### Category 6 — Download Edge Cases (2 tests)

| # | Test | Plan ID | Status |
|---|------|---------|--------|
| 59 | `TestDownloadEdgeCases::test_concurrent_downloads_same_version` | 6.1 | ⏸️ PENDING |
| 60 | `TestDownloadEdgeCases::test_download_returns_correct_content_length` | 6.1 | ⏸️ PENDING |

### End-to-End Simulation (1 test)

| # | Test | Plan ID | Status |
|---|------|---------|--------|
| 61 | `TestEndToEndOTASimulation::test_full_ota_flow_upload_trigger_download_verify` | E2E | ⏸️ PENDING |

---

## 4 · Hardware / Firmware Tests (`firmware/TEST_EDGE_CASES.md`) — 27 tests 📋

### Category 1 — Power Failure During OTA (P0) — 4 tests

| # | Test | Plan ID | Risk | Status |
|---|------|---------|------|--------|
| 62 | Power loss during firmware download (before `esp_ota_end`) | 1.1 | Bricking | 📋 MANUAL |
| 63 | Power loss after `esp_ota_end` but before reboot | 1.2 | Bricking | 📋 MANUAL |
| 64 | Power loss during self-test window (first 15s) | 1.3 | Boot loop | 📋 MANUAL |
| 65 | Brownout during flash write (voltage sag) | 1.4 | Bricking | 📋 MANUAL |

### Category 2 — Network Disruption During Download (P1) — 5 tests

| # | Test | Plan ID | Risk | Status |
|---|------|---------|------|--------|
| 66 | WiFi disconnect mid-download | 2.1 | Stuck on old FW | 📋 MANUAL |
| 67 | Server unreachable (firewall block) | 2.2 | Stuck on old FW | 📋 MANUAL |
| 68 | Extremely slow network (timeout boundary test) | 2.3 | Timeout semantics | 📋 MANUAL |
| 69 | Intermittent packet loss (weak WiFi signal) | 2.4 | Corruption risk | 📋 MANUAL |
| 70 | Server sends partial response then dies | 2.5 | Stuck on old FW | 📋 MANUAL |

### Category 3 — Firmware-Side Corruption (P0) — 3 tests

| # | Test | Plan ID | Risk | Status |
|---|------|---------|------|--------|
| 71 | Valid binary but non-bootable firmware (garbage header) | 3.4 | Boot failure | 📋 MANUAL |
| 72 | Firmware that crashes immediately on boot (`abort()`) | 3.5 | Boot loop | 📋 MANUAL |
| 73 | Firmware boots but fails MQTT self-test (bad broker URL) | 3.6 | Rollback | 📋 MANUAL |

### Category 5 — Rollback & Boot-Loop Scenarios (P0) — 5 tests

| # | Test | Plan ID | Risk | Status |
|---|------|---------|------|--------|
| 74 | Verify 3-failure rollback threshold (exact count) | 5.1 | Boot loop | 📋 MANUAL |
| 75 | NVS reboot counter survives hard power loss | 5.2 | Counter reset | 📋 MANUAL |
| 76 | Both OTA partitions contain broken firmware | 5.3 | ⚠️ BRICKING | 📋 MANUAL |
| 77 | Successful boot resets reboot counter to 0 | 5.4 | Counter leak | 📋 MANUAL |
| 78 | Self-test passes at the 15-second boundary | 5.5 | Timing | 📋 MANUAL |

### Category 8 — Resource Exhaustion (P3) — 4 tests

| # | Test | Plan ID | Risk | Status |
|---|------|---------|------|--------|
| 79 | Low heap memory during OTA (200 KB pre-allocated) | 8.1 | OOM crash | 📋 MANUAL |
| 80 | Flash wear — 100 consecutive OTA cycles | 8.2 | Flash degradation | 📋 MANUAL |
| 81 | OTA during active firmware operation (task contention) | 8.3 | CPU starvation | 📋 MANUAL |
| 82 | NVS partition full (counter write fails) | 8.4 | Rollback broken | 📋 MANUAL |

### Category 9 — Timing & Concurrency, Device-Side (P3) — 3 tests

| # | Test | Plan ID | Risk | Status |
|---|------|---------|------|--------|
| 83 | OTA trigger while previous OTA is downloading | 9.2 | Corruption | 📋 MANUAL |
| 84 | MQTT reconnect storm after broker restart | 9.3 | Thundering herd | 📋 MANUAL |
| 85 | WiFi AP restart (brief 5s outage) | 9.4 | Disconnect | 📋 MANUAL |

### Category 10 — Environmental & Hardware (P3) — 3 tests

| # | Test | Plan ID | Risk | Status |
|---|------|---------|------|--------|
| 86 | Cold boot after 24+ hours powered off | 10.1 | NVS data loss | 📋 MANUAL |
| 87 | Rapid power cycling (relay chatter, 500 ms cycles) | 10.2 | Flash corruption | 📋 MANUAL |
| 88 | Serial monitor attached during OTA | 10.3 | UART interference | 📋 MANUAL |

---

## Key Findings & Security Issues

| Severity | Finding | Test | Impact |
|----------|---------|------|--------|
| 🔴 Critical | Backend MQTT payload uses `"size"` but firmware expects `"file_size_bytes"` | #20 | Auto-publish on upload **never triggers OTA** on device |
| 🔴 Critical | No factory-reset partition — both slots broken = **potential brick** | #76 | Unrecoverable without serial flash |
| 🟠 High | No version downgrade protection — older version can be uploaded | #32 | Attacker/mistake can roll back all devices |
| 🟠 High | No TLS on MQTT or HTTP — SHA-256 is the only integrity defence | Plan 7.1 | MITM can serve malicious firmware if they also control MQTT |
| 🟡 Medium | Very long version strings (200+ chars) cause server 500 | #41 | DoS via crafted version string on Windows |
| 🟡 Medium | Backend doesn't re-verify SHA-256 on download (disk corruption undetected) | #14 | Relies entirely on device-side hash check |
| 🟢 Low | Registry corruption handled gracefully (empty fallback) | #25–28 | No crash, but all version history lost |

---

## Test Coverage by Plan Category

| Category | Plan Tests | Automated | Integration | Manual | Total |
|----------|-----------|----------:|------------:|-------:|------:|
| 1 · Power Failure | 4 | — | — | 4 | 4 |
| 2 · Network Disruption | 6 | — | — | 5 | 5 |
| 3 · Corruption & Validation | 7 | 6 | — | 3 | 9 |
| 4 · MQTT Edge Cases | 7 | 2 | 4 | — | 6 |
| 5 · Rollback & Boot-Loop | 5 | — | — | 5 | 5 |
| 6 · Server-Side Failures | 5 | 10 | 2 | — | 12 |
| 7 · Security & Adversarial | 6 | 10 | 1 | — | 11 |
| 8 · Resource Exhaustion | 4 | — | — | 4 | 4 |
| 9 · Timing & Concurrency | 4 | 2 | — | 3 | 5 |
| 10 · Environmental | 4 | — | — | 3 | 3 |
| Input Validation | — | 6 | — | — | 6 |
| Auth & Crypto (pre-existing) | — | 13 | — | — | 13 |
| End-to-End | — | — | 1 | — | 1 |
| MQTT Graceful Degradation | — | 2 | — | — | 2 |
| **Totals** | **52** | **53** | **8** | **27** | **88** |

---

*Generated from the Three-Body OTA edge-case test plan. Run `python -m pytest -v` in `backend-and-dash/` to execute automated tests.*
