/* wifi.h — WiFi STA mode initialization with blocking connect and auto-reconnect */

#ifndef WIFI_H
#define WIFI_H

#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
#define WIFI_MAXIMUM_RETRY 10

/**
 * @brief Initialize WiFi STA and block until IP obtained or max retries exceeded.
 * @return ESP_OK on successful connection, ESP_FAIL otherwise
 */
esp_err_t wifi_init_sta(void);

#endif /* WIFI_H */
