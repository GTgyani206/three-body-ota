/*
 * ota_handler.h — OTA Download and Commit routines
 */

#ifndef OTA_HANDLER_H
#define OTA_HANDLER_H

#include "esp_err.h"

/**
 * @brief Starts the OTA update process
 *
 * Downloads the binary from the backend via HTTP, verifies its SHA-256
 * checksum iteratively, writes to the next OTA partition, and configures
 * the bootloader to trial-boot into it.
 *
 * @param version The new firmware version (e.g. "1.0.1") to fetch.
 * @param expected_sha256 The hex string of the expected SHA-256 hash.
 * @param expected_size Expected file size in bytes to prevent overflow.
 *
 * @return ESP_OK if download, verification, and partition setup succeeded.
 */
esp_err_t ota_start_update(const char *version, const char *expected_sha256, int expected_size);

#endif /* OTA_HANDLER_H */
