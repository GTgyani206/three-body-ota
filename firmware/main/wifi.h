/*
 * wifi.h — WiFi STA mode driver for Three-Body OTA
 *
 * Provides wifi_init_sta() which blocks until an IP is obtained.
 * Auto-reconnects on disconnect.
 */

#ifndef WIFI_H
#define WIFI_H

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

/* Event group bits — set by the WiFi event handler */
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

/* Maximum consecutive reconnect attempts before giving up and setting FAIL bit */
#define WIFI_MAXIMUM_RETRY 10

/**
 * @brief  Initialize WiFi in Station mode and block until connected.
 *
 * - Creates the default event loop and netif
 * - Configures STA with SSID/password from sdkconfig
 * - Starts WiFi and waits for IP or failure
 *
 * @return ESP_OK on successful connection, ESP_FAIL otherwise.
 */
esp_err_t wifi_init_sta(void);

#endif /* WIFI_H */
