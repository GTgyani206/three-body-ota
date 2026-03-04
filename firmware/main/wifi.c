/*
 * wifi.c — WiFi STA driver for Three-Body OTA
 *
 * Connects to the AP defined in sdkconfig (menuconfig).
 * Auto-reconnects on disconnect up to WIFI_MAXIMUM_RETRY times.
 */

#include "wifi.h"
#include <string.h>
#include "esp_wifi.h"
#include "esp_log.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "nvs_flash.h"

static const char *TAG = "WIFI";

/* FreeRTOS event group to signal WiFi events to app_main */
static EventGroupHandle_t s_wifi_event_group;

/* Retry counter */
static int s_retry_num = 0;

/* ------------------------------------------------------------------ */
/* Event handler — runs on the default event loop task                 */
/* ------------------------------------------------------------------ */
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                               int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        /* WiFi driver started — initiate first connection attempt */
        esp_wifi_connect();

    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (s_retry_num < WIFI_MAXIMUM_RETRY) {
            esp_wifi_connect();
            s_retry_num++;
            ESP_LOGW(TAG, "Disconnected. Retrying connection (%d/%d)...",
                     s_retry_num, WIFI_MAXIMUM_RETRY);
        } else {
            ESP_LOGE(TAG, "Failed to connect after %d attempts", WIFI_MAXIMUM_RETRY);
            xEventGroupSetBits(s_wifi_event_group, WIFI_FAIL_BIT);
        }

    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "Got IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

/* ------------------------------------------------------------------ */
/* Public API                                                          */
/* ------------------------------------------------------------------ */
esp_err_t wifi_init_sta(void)
{
    s_wifi_event_group = xEventGroupCreate();

    /* Initialize the TCP/IP stack and create default WiFi STA netif */
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    /* WiFi driver init with default config */
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    /* Register event handlers */
    esp_event_handler_instance_t instance_any_id;
    esp_event_handler_instance_t instance_got_ip;

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        WIFI_EVENT, ESP_EVENT_ANY_ID,
        &wifi_event_handler, NULL, &instance_any_id));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(
        IP_EVENT, IP_EVENT_STA_GOT_IP,
        &wifi_event_handler, NULL, &instance_got_ip));

    /* Configure STA with SSID + password from sdkconfig */
    wifi_config_t wifi_config = {
        .sta = {
            .ssid      = CONFIG_ESP_WIFI_SSID,
            .password  = CONFIG_ESP_WIFI_PASSWORD,
            .threshold.authmode = WIFI_AUTH_WPA2_PSK,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "wifi_init_sta finished. Waiting for connection...");

    /* Block until we get an IP or exhaust retries */
    EventBits_t bits = xEventGroupWaitBits(
        s_wifi_event_group,
        WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE,   /* don't clear bits on exit */
        pdFALSE,   /* wait for ANY bit, not all */
        portMAX_DELAY);

    if (bits & WIFI_CONNECTED_BIT) {
        ESP_LOGI(TAG, "Connected to AP  SSID: %s", CONFIG_ESP_WIFI_SSID);
        return ESP_OK;
    } else if (bits & WIFI_FAIL_BIT) {
        ESP_LOGE(TAG, "Failed to connect to SSID: %s", CONFIG_ESP_WIFI_SSID);
        return ESP_FAIL;
    }

    ESP_LOGE(TAG, "Unexpected WiFi event");
    return ESP_FAIL;
}
