/* ota_handler.c — HTTP firmware download, SHA-256 validation, and A/B partition flashing */

#include "ota_handler.h"
#include <string.h>
#include "esp_log.h"
#include "esp_system.h"
#include "esp_http_client.h"
#include "esp_ota_ops.h"
#include "mbedtls/sha256.h"

static const char *TAG = "OTA_HANDLER";

#define OTA_BUFF_SIZE 4096

static void bytes_to_hex_string(const uint8_t *hash, char *hex_str)
{
    for (int i = 0; i < 32; i++) {
        sprintf(hex_str + (i * 2), "%02x", hash[i]);
    }
    hex_str[64] = '\0';
}

esp_err_t ota_start_update(const char *version, const char *expected_sha256, int expected_size)
{
    esp_err_t err;
    esp_ota_handle_t update_handle = 0;
    const esp_partition_t *update_partition = NULL;

    ESP_LOGI(TAG, "OTA: v%s, %d bytes, SHA: %.16s...", version, expected_size, expected_sha256);

    /*
     * A/B Partitioning: esp_ota_get_next_update_partition() returns the inactive
     * OTA slot (ota_0 or ota_1). This ensures the running firmware remains intact
     * until the new image is fully validated and the device reboots.
     */
    update_partition = esp_ota_get_next_update_partition(NULL);
    if (update_partition == NULL) {
        ESP_LOGE(TAG, "No OTA partition available");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "Target partition: subtype %d @ 0x%" PRIx32,
             update_partition->subtype, update_partition->address);

    char download_url[256];
    snprintf(download_url, sizeof(download_url), "%s/firmware/%s/download", CONFIG_FIRMWARE_SERVER_URL, version);

    esp_http_client_config_t config = {
        .url = download_url,
        .timeout_ms = 30000,
        .keep_alive_enable = true,
        .buffer_size = 4096,
        .buffer_size_tx = 1024,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        ESP_LOGE(TAG, "HTTP client init failed");
        return ESP_FAIL;
    }

    err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "HTTP open failed: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return ESP_FAIL;
    }

    esp_http_client_fetch_headers(client);
    int status_code = esp_http_client_get_status_code(client);
    if (status_code != 200) {
        ESP_LOGE(TAG, "HTTP %d (expected 200)", status_code);
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return ESP_FAIL;
    }

    err = esp_ota_begin(update_partition, OTA_WITH_SEQUENTIAL_WRITES, &update_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return err;
    }

    /* Incremental SHA-256: computed during download to avoid re-reading flash */
    mbedtls_sha256_context sha_ctx;
    mbedtls_sha256_init(&sha_ctx);
    mbedtls_sha256_starts(&sha_ctx, 0);

    char *ota_write_data = (char *)malloc(OTA_BUFF_SIZE);
    if (ota_write_data == NULL) {
        ESP_LOGE(TAG, "OOM for OTA buffer");
        esp_ota_abort(update_handle);
        return ESP_ERR_NO_MEM;
    }

    int total_read = 0;
    int last_logged_pct = -1;
    int content_length = esp_http_client_get_content_length(client);
    while (1) {
        int data_read = esp_http_client_read(client, ota_write_data, OTA_BUFF_SIZE);
        if (data_read < 0) {
            ESP_LOGE(TAG, "HTTP read error");
            err = ESP_FAIL;
            break;
        } else if (data_read > 0) {
            err = esp_ota_write(update_handle, (const void *)ota_write_data, data_read);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "esp_ota_write failed: %s", esp_err_to_name(err));
                break;
            }
            mbedtls_sha256_update(&sha_ctx, (const unsigned char *)ota_write_data, data_read);
            total_read += data_read;
            /* Log progress every 10% */
            if (content_length > 0) {
                int pct = (total_read * 100) / content_length;
                int pct_bucket = pct / 10;
                if (pct_bucket != last_logged_pct) {
                    last_logged_pct = pct_bucket;
                    ESP_LOGI(TAG, "Downloading... %d%% (%d / %d bytes)", pct, total_read, content_length);
                }
            }
        } else {
            if (esp_http_client_is_complete_data_received(client)) {
                err = ESP_OK;
            } else {
                ESP_LOGE(TAG, "HTTP connection closed prematurely");
                err = ESP_FAIL;
            }
            break;
        }
    }

    free(ota_write_data);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    /*
     * Validation BEFORE esp_ota_end(): SHA-256 must be verified while data is still
     * in the streaming buffer context. esp_ota_end() finalizes the partition and
     * would require re-reading from flash to compute hash if done afterward.
     */
    if (err == ESP_OK) {
        if (total_read != expected_size) {
            ESP_LOGE(TAG, "Size mismatch: expected %d, got %d", expected_size, total_read);
            err = ESP_FAIL;
        } else {
            uint8_t hash_out[32];
            mbedtls_sha256_finish(&sha_ctx, hash_out);

            char calculated_hash_str[65];
            bytes_to_hex_string(hash_out, calculated_hash_str);
            ESP_LOGI(TAG, "Calculated SHA-256: %s", calculated_hash_str);

            if (strcmp(calculated_hash_str, expected_sha256) != 0) {
                ESP_LOGE(TAG, "SHA-256 mismatch — corrupted download");
                err = ESP_FAIL;
            }
        }
    }
    mbedtls_sha256_free(&sha_ctx);

    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OTA validation failed — aborting");
        esp_ota_abort(update_handle);
        return err;
    }

    err = esp_ota_end(update_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_end failed: %s", esp_err_to_name(err));
        return err;
    }

    /*
     * esp_ota_set_boot_partition() updates the otadata partition to point to the
     * new image with state PENDING_VERIFY. On reboot, app_main must call
     * esp_ota_mark_app_valid_cancel_rollback() to commit; otherwise bootloader
     * reverts to the previous slot.
     */
    err = esp_ota_set_boot_partition(update_partition);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_set_boot_partition failed: %s", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "OTA complete — rebooting in 3s");
    vTaskDelay(pdMS_TO_TICKS(3000));
    esp_restart();

    return ESP_OK;
}
