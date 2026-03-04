idf_component_register(
    SRCS
        "main.c"
        "wifi.c"
        "ota_handler.c"
    INCLUDE_DIRS "."
    REQUIRES
        nvs_flash
        esp_wifi
        esp_event
        esp_netif
        esp_http_client
        esp_https_ota
        app_update
        mqtt
        mbedtls
        log
        json
)

