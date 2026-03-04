/* ota_handler.h — OTA download, SHA-256 validation, and A/B partition commit interface */

#ifndef OTA_HANDLER_H
#define OTA_HANDLER_H

#include "esp_err.h"

/**
 * @brief Download firmware, validate SHA-256, flash to inactive partition, and reboot.
 *
 * SHA-256 is computed incrementally during download (before esp_ota_end) to avoid
 * re-reading flash. On success, sets boot partition to PENDING_VERIFY state;
 * self-test in app_main must commit or rollback occurs.
 *
 * @param version         Firmware version string for download URL construction
 * @param expected_sha256 64-char lowercase hex SHA-256 hash for validation
 * @param expected_size   Exact byte count to detect truncated downloads
 * @return ESP_OK on success (device reboots), error code on failure
 */
esp_err_t ota_start_update(const char *version, const char *expected_sha256, int expected_size);

#endif /* OTA_HANDLER_H */
