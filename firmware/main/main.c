/*
 * main.c — Entry point for Three-Body OTA firmware
 *
 * Phase 6: Full OTA with status reporting to MQTT dashboard
 */

#include <stdio.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_mac.h"
#include "esp_timer.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "mqtt_client.h"
#include "cJSON.h"
#include "esp_ota_ops.h"
#include "esp_app_format.h"

#include "wifi.h"
#include "ota_handler.h"

static const char *TAG = "MAIN";

/* Global MQTT client handle for status publishing */
static esp_mqtt_client_handle_t mqtt_client = NULL;

/* Tracking for self-test: did MQTT successfully connect? */
static volatile bool mqtt_connected = false;

/* Device identification */
static char device_id[18] = {0};  /* MAC address as string */
static const char *firmware_version = "unknown";

/* Boot timestamp for uptime calculation */
static int64_t boot_time_us = 0;

/* NVS namespace and key for the reboot counter */
#define NVS_NAMESPACE "ota_data"
#define NVS_KEY_REBOOT_CNT "reboot_cnt"
#define MAX_REBOOT_COUNT 3

/* Status topics */
#define TOPIC_FIRMWARE_STATUS "firmware/status"
#define TOPIC_DEVICE_STATUS_FMT "device/%s/status"

/* ------------------------------------------------------------------ */
/* Status Publishing Helpers                                          */
/* ------------------------------------------------------------------ */
static void get_device_mac(void)
{
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(device_id, sizeof(device_id), "%02X:%02X:%02X:%02X:%02X:%02X",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

static void publish_status(const char *status)
{
    if (!mqtt_client || !mqtt_connected) {
        ESP_LOGW(TAG, "Cannot publish status — MQTT not connected");
        return;
    }

    int64_t uptime_us = esp_timer_get_time() - boot_time_us;
    int uptime_sec = (int)(uptime_us / 1000000);

    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "device_id", device_id);
    cJSON_AddStringToObject(root, "firmware_version", firmware_version);
    cJSON_AddStringToObject(root, "status", status);
    cJSON_AddNumberToObject(root, "uptime_seconds", uptime_sec);
    cJSON_AddNumberToObject(root, "free_heap", esp_get_free_heap_size());

    char *json_str = cJSON_PrintUnformatted(root);
    if (json_str) {
        esp_mqtt_client_publish(mqtt_client, TOPIC_FIRMWARE_STATUS, json_str, 0, 1, 0);
        ESP_LOGI(TAG, "Published status: %s", json_str);
        free(json_str);
    }
    cJSON_Delete(root);
}

static void publish_heartbeat(void)
{
    if (!mqtt_client || !mqtt_connected) return;

    int64_t uptime_us = esp_timer_get_time() - boot_time_us;
    int uptime_sec = (int)(uptime_us / 1000000);

    cJSON *root = cJSON_CreateObject();
    cJSON_AddStringToObject(root, "device_id", device_id);
    cJSON_AddStringToObject(root, "firmware_version", firmware_version);
    cJSON_AddNumberToObject(root, "uptime_seconds", uptime_sec);
    cJSON_AddNumberToObject(root, "free_heap", esp_get_free_heap_size());

    char *json_str = cJSON_PrintUnformatted(root);
    if (json_str) {
        char topic[64];
        snprintf(topic, sizeof(topic), TOPIC_DEVICE_STATUS_FMT, device_id);
        esp_mqtt_client_publish(mqtt_client, topic, json_str, 0, 0, 0);  /* QoS 0 for heartbeat */
        ESP_LOGD(TAG, "Heartbeat: %s", json_str);
        free(json_str);
    }
    cJSON_Delete(root);
}

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
            mqtt_connected = true;  /* Mark MQTT as connected for self-test */
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
    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    
    /* Register event handler */
    esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    
    /* Start the client */
    esp_mqtt_client_start(mqtt_client);
}

/* ------------------------------------------------------------------ */
/* Reboot Counter Helpers (NVS)                                       */
/* ------------------------------------------------------------------ */
static uint32_t get_reboot_count(void)
{
    nvs_handle_t handle;
    uint32_t count = 0;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &handle) == ESP_OK) {
        nvs_get_u32(handle, NVS_KEY_REBOOT_CNT, &count);
        nvs_close(handle);
    }
    return count;
}

static void set_reboot_count(uint32_t count)
{
    nvs_handle_t handle;
    if (nvs_open(NVS_NAMESPACE, NVS_READWRITE, &handle) == ESP_OK) {
        nvs_set_u32(handle, NVS_KEY_REBOOT_CNT, count);
        nvs_commit(handle);
        nvs_close(handle);
    }
}

/* ------------------------------------------------------------------ */
/* Application Entry Point                                            */
/* ------------------------------------------------------------------ */
void app_main(void)
{
    /* Record boot time for uptime calculation */
    boot_time_us = esp_timer_get_time();
    
    /* Get device MAC address */
    get_device_mac();
    
    /* Get firmware version from app description */
    const esp_app_desc_t *app_desc = esp_app_get_description();
    if (app_desc) {
        firmware_version = app_desc->version;
    }

    ESP_LOGI(TAG, "=== Three-Body OTA Firmware ===");
    ESP_LOGI(TAG, "Device ID: %s", device_id);
    ESP_LOGI(TAG, "Firmware Version: %s", firmware_version);

    /* ── 1. Initialize NVS (required by WiFi driver + reboot counter) ── */
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES ||
        ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_LOGW(TAG, "NVS partition truncated — erasing and reinitializing");
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    ESP_LOGI(TAG, "NVS initialized");

    /* ── 2. Check OTA state and handle boot loop detection ── */
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t ota_state;
    bool pending_verify = false;
    
    if (esp_ota_get_state_partition(running, &ota_state) == ESP_OK) {
        if (ota_state == ESP_OTA_IMG_PENDING_VERIFY) {
            pending_verify = true;
            ESP_LOGW(TAG, "Running in PENDING_VERIFY state — self-test required");
            
            /* Increment reboot counter */
            uint32_t reboot_count = get_reboot_count() + 1;
            set_reboot_count(reboot_count);
            ESP_LOGI(TAG, "Reboot count: %lu / %d", (unsigned long)reboot_count, MAX_REBOOT_COUNT);
            
            if (reboot_count >= MAX_REBOOT_COUNT) {
                ESP_LOGE(TAG, "Max reboot count reached! Triggering rollback...");
                esp_ota_mark_app_invalid_rollback_and_reboot();
                /* Does not return */
            }
        } else {
            ESP_LOGI(TAG, "OTA state: %d (not pending verify)", ota_state);
        }
    }

    /* ── 3. Connect to WiFi (blocks until connected or fails) ── */
    esp_err_t wifi_ret = wifi_init_sta();
    if (wifi_ret != ESP_OK) {
        ESP_LOGE(TAG, "WiFi connection failed");
        if (pending_verify) {
            ESP_LOGE(TAG, "Self-test FAILED (WiFi). Rebooting to trigger rollback...");
            vTaskDelay(pdMS_TO_TICKS(1000));
            esp_restart();
        }
        return;
    }
    ESP_LOGI(TAG, "WiFi connected successfully");

    /* ── 4. Start MQTT Client ── */
    mqtt_app_start();

    /* ── 5. Wait for MQTT connection (self-test window) ── */
    if (pending_verify) {
        ESP_LOGI(TAG, "Waiting for MQTT connection (self-test)...");
        int wait_count = 0;
        while (!mqtt_connected && wait_count < 30) {  /* 15 second timeout */
            vTaskDelay(pdMS_TO_TICKS(500));
            wait_count++;
        }
        
        if (mqtt_connected) {
            /* SELF-TEST PASSED: Commit the firmware! */
            ESP_LOGI(TAG, "========================================");
            ESP_LOGI(TAG, "  SELF-TEST PASSED — COMMITTING OTA");
            ESP_LOGI(TAG, "========================================");
            esp_ota_mark_app_valid_cancel_rollback();
            set_reboot_count(0);  /* Reset counter on successful commit */
            publish_status("COMMITTED");
        } else {
            ESP_LOGE(TAG, "Self-test FAILED (MQTT timeout). Rebooting to trigger rollback...");
            vTaskDelay(pdMS_TO_TICKS(1000));
            esp_restart();
        }
    }

    /* ── 6. Publish initial status (for already-committed firmware) ── */
    if (!pending_verify && mqtt_connected) {
        vTaskDelay(pdMS_TO_TICKS(1000));  /* Brief delay for MQTT to stabilize */
        publish_status("RUNNING");
    }

    /* ── 7. Heartbeat loop ── */
    ESP_LOGI(TAG, "Firmware running normally. Waiting for OTA commands...");
    int heartbeat_count = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(30000));  /* 30 second heartbeat interval */
        heartbeat_count++;
        
        ESP_LOGI(TAG, "Heartbeat #%d | Heap: %lu bytes", heartbeat_count, (unsigned long)esp_get_free_heap_size());
        publish_heartbeat();
    }
}
