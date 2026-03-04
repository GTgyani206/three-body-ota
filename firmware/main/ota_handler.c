/*
 * ota_handler.c — Handles OTA Firmware Download & Flashing
 *
 * Downloads binary via HTTP, writes to Partition B, computes SHA-256 on the fly.
 * On success, sets next boot partition and reboots.
 */

#include "ota_handler.h"
#include <string.h>
#include "esp_log.h"
#include "esp_system.h"
#include "esp_http_client.h"
#include "esp_ota_ops.h"
#include "mbedtls/sha256.h"

static const char *TAG = "OTA_HANDLER";

#define OTA_BUFF_SIZE 4096

/* Utility to convert mbedtls hash bytes to a lowercase hex string */
static void bytes_to_hex_string(const uint8_t *hash, char *hex_str)
{
    for (int i = 0; i < 32; i++) {
        sprintf(hex_str + (i * 2), "%02x", hash[i]);
    }
    hex_str[64] = 0;
}

esp_err_t ota_start_update(const char *version, const char *expected_sha256, int expected_size)
{
    esp_err_t err;
    esp_ota_handle_t update_handle = 0;
    const esp_partition_t *update_partition = NULL;

    ESP_LOGI(TAG, "Starting OTA for version: %s", version);
    ESP_LOGI(TAG, "Expected File Size: %d bytes", expected_size);
    ESP_LOGI(TAG, "Expected SHA-256: %s", expected_sha256);

    /* 1. Identify the partition to write to (Partition B if running A, etc) */
    update_partition = esp_ota_get_next_update_partition(NULL);
    if (update_partition == NULL) {
        ESP_LOGE(TAG, "No OTA partition found!");
        return ESP_FAIL;
    }
    ESP_LOGI(TAG, "Writing to partition subtype %d at offset 0x%" PRIx32,
             update_partition->subtype, update_partition->address);

    /* 2. Setup HTTP Client */
    char download_url[256];
    snprintf(download_url, sizeof(download_url), "%s/firmware/%s/download", CONFIG_FIRMWARE_SERVER_URL, version);
    ESP_LOGI(TAG, "Download URL: %s", download_url);

    esp_http_client_config_t config = {
        .url = download_url,
        .timeout_ms = 10000,
        .keep_alive_enable = true,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (client == NULL) {
        ESP_LOGE(TAG, "Failed to initialize HTTP client");
        return ESP_FAIL;
    }

    err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open HTTP connection: %s", esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return ESP_FAIL;
    }
    
    esp_http_client_fetch_headers(client);
    int status_code = esp_http_client_get_status_code(client);
    if (status_code != 200) {
        ESP_LOGE(TAG, "Invalid HTTP Status Code: %d", status_code);
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return ESP_FAIL;
    }

    /* 3. Begin OTA partition writing */
    err = esp_ota_begin(update_partition, OTA_WITH_SEQUENTIAL_WRITES, &update_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_begin failed: %s", esp_err_to_name(err));
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return err;
    }

    /* Setup SHA-256 for running calculation */
    mbedtls_sha256_context sha_ctx;
    mbedtls_sha256_init(&sha_ctx);
    mbedtls_sha256_starts(&sha_ctx, 0); // 0 for SHA-256, 1 for SHA-224

    char *ota_write_data = (char *)malloc(OTA_BUFF_SIZE);
    if (ota_write_data == NULL) {
        ESP_LOGE(TAG, "Failed to allocate memory for OTA buffer");
        esp_ota_abort(update_handle);
        return ESP_ERR_NO_MEM;
    }

    /* 4. Stream data from HTTP to OTA partition */
    int total_read = 0;
    while (1) {
        int data_read = esp_http_client_read(client, ota_write_data, OTA_BUFF_SIZE);
        if (data_read < 0) {
            ESP_LOGE(TAG, "Error: SSL data read error");
            err = ESP_FAIL;
            break;
        } else if (data_read > 0) {
            err = esp_ota_write(update_handle, (const void *)ota_write_data, data_read);
            if (err != ESP_OK) {
                ESP_LOGE(TAG, "Error: esp_ota_write failed (%s)!", esp_err_to_name(err));
                break;
            }
            mbedtls_sha256_update(&sha_ctx, (const unsigned char *)ota_write_data, data_read);
            total_read += data_read;
            ESP_LOGD(TAG, "Downloaded %d of %d bytes", total_read, expected_size);
        } else if (data_read == 0) {
            /* Connection closed = all data received */
            if (esp_http_client_is_complete_data_received(client)) {
                ESP_LOGI(TAG, "Connection closed, all data received");
                err = ESP_OK;
            } else {
                ESP_LOGE(TAG, "Error: HTTP connection closed prematurely");
                err = ESP_FAIL;
            }
            break;
        }
    }

    free(ota_write_data);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    /* 5. Validation Check: Size & SHA-256 */
    if (err == ESP_OK) {
        if (total_read != expected_size) {
            ESP_LOGE(TAG, "Size mismatch! Expected: %d, Got: %d", expected_size, total_read);
            err = ESP_FAIL;
        } else {
            uint8_t hash_out[32];
            mbedtls_sha256_finish(&sha_ctx, hash_out);
            
            char calculated_hash_str[65];
            bytes_to_hex_string(hash_out, calculated_hash_str);
            ESP_LOGI(TAG, "Calculated SHA-256: %s", calculated_hash_str);

            if (strcmp(calculated_hash_str, expected_sha256) != 0) {
                ESP_LOGE(TAG, "SHA-256 MISMATCH! Firmware corrupted during download.");
                err = ESP_FAIL;
            }
        }
    }
    mbedtls_sha256_free(&sha_ctx);

    /* 6. End Write & Setup Bootloader */
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "OTA Upload failed. Aborting.");
        esp_ota_abort(update_handle);
        return err;
    }

    err = esp_ota_end(update_handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_end failed (%s)!", esp_err_to_name(err));
        return err;
    }

    err = esp_ota_set_boot_partition(update_partition);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_ota_set_boot_partition failed (%s)!", esp_err_to_name(err));
        return err;
    }

    ESP_LOGI(TAG, "OTA Success! Setting boot partition to B and restarting in 3 seconds...");
    vTaskDelay(pdMS_TO_TICKS(3000));
    esp_restart();

    return ESP_OK;
}
