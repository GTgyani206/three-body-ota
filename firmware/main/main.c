/*
 * main.c — Entry point for Three-Body OTA firmware
 *
 * Phase 4: NVS init → WiFi connect → MQTT subscribe → Parse JSON → Run OTA
 */

#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "nvs_flash.h"
#include "mqtt_client.h"
#include "cJSON.h"

#include "wifi.h"
#include "ota_handler.h"

static const char *TAG = "MAIN";

/* ------------------------------------------------------------------ */
/* MQTT Event Handler                                                 */
/* ------------------------------------------------------------------ */
static void mqtt_event_handler(void *handler_args, esp_event_base_t base, int32_t event_id, void *event_data)
{
    esp_mqtt_event_handle_t event = event_data;
    esp_mqtt_client_handle_t client = event->client;

    switch ((esp_mqtt_event_id_t)event_id) {
        case MQTT_EVENT_CONNECTED:
            ESP_LOGI(TAG, "MQTT_EVENT_CONNECTED to broker");
            int msg_id = esp_mqtt_client_subscribe(client, "firmware/update", 1);
            ESP_LOGI(TAG, "Subscribed to firmware/update, msg_id=%d", msg_id);
            break;

        case MQTT_EVENT_DISCONNECTED:
            ESP_LOGI(TAG, "MQTT_EVENT_DISCONNECTED");
            break;

        case MQTT_EVENT_SUBSCRIBED:
            ESP_LOGI(TAG, "MQTT_EVENT_SUBSCRIBED, msg_id=%d", event->msg_id);
            break;

        case MQTT_EVENT_DATA:
            ESP_LOGI(TAG, "MQTT_EVENT_DATA");
            ESP_LOGI(TAG, "TOPIC=%.*s", event->topic_len, event->topic);
            
            /* Null-terminate the incoming data for cJSON parser */
            char *json_str = malloc(event->data_len + 1);
            if (!json_str) {
                ESP_LOGE(TAG, "Failed to allocate memory for MQTT payload");
                break;
            }
            memcpy(json_str, event->data, event->data_len);
            json_str[event->data_len] = '\0';
            
            /* Parse the JSON payload */
            cJSON *root = cJSON_Parse(json_str);
            if (root == NULL) {
                ESP_LOGE(TAG, "Failed to parse MQTT JSON payload");
                free(json_str);
                break;
            }

            cJSON *version = cJSON_GetObjectItem(root, "version");
            cJSON *sha_hash = cJSON_GetObjectItem(root, "sha256_hash");
            cJSON *file_size = cJSON_GetObjectItem(root, "file_size_bytes");

            if (cJSON_IsString(version) && cJSON_IsString(sha_hash) && cJSON_IsNumber(file_size)) {
                ESP_LOGI(TAG, "OTA Trigger Received!");
                ESP_LOGI(TAG, " - Version: %s", version->valuestring);
                ESP_LOGI(TAG, " - Size: %d", file_size->valueint);
                ESP_LOGI(TAG, " - SHA-256: %s", sha_hash->valuestring);
                
                /* Start the actual download and flash process! */
                ota_start_update(version->valuestring, sha_hash->valuestring, file_size->valueint);
            } else {
                ESP_LOGE(TAG, "Missing/invalid fields in OTA JSON message");
            }

            cJSON_Delete(root);
            free(json_str);
            break;

        case MQTT_EVENT_ERROR:
            ESP_LOGE(TAG, "MQTT_EVENT_ERROR");
            break;

        default:
            ESP_LOGI(TAG, "Other MQTT event id:%d", event->event_id);
            break;
    }
}

/* ------------------------------------------------------------------ */
/* MQTT Initialization                                                */
/* ------------------------------------------------------------------ */
static void mqtt_app_start(void)
{
    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = CONFIG_MQTT_BROKER_URL,
    };

    ESP_LOGI(TAG, "Starting MQTT Client. Broker URL: %s", CONFIG_MQTT_BROKER_URL);
    esp_mqtt_client_handle_t client = esp_mqtt_client_init(&mqtt_cfg);
    
    /* Register event handler */
    esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    
    /* Start the client */
    esp_mqtt_client_start(client);
}

/* ------------------------------------------------------------------ */
/* Application Entry Point                                            */
/* ------------------------------------------------------------------ */
void app_main(void)
{
    ESP_LOGI(TAG, "=== Three-Body OTA Firmware ===");

    /* ── 1. Initialize NVS (required by WiFi driver) ── */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS partition truncated — erasing and reinitializing");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_LOGI(TAG, "NVS initialized");

    /* ── 2. Connect to WiFi (blocks until connected or fails) ── */
    esp_err_t wifi_ret = wifi_init_sta();
    if (wifi_ret != ESP_OK) {
        ESP_LOGE(TAG, "WiFi connection failed — halting");
        /* In later phases this will trigger rollback logic */
        return;
    }
    ESP_LOGI(TAG, "WiFi connected successfully");

    /* ── 3. Start MQTT Client ── */
    mqtt_app_start();

    /* ── 4. Heartbeat loop ── */
    int count = 0;
    while (1) {
        ESP_LOGI(TAG, "Alive and running. Count: %d", count++);
        vTaskDelay(pdMS_TO_TICKS(5000));
    }
}
