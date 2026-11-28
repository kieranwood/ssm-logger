#include "config.h"

#include <string.h>

#include "nvs.h"
#include "nvs_flash.h"

#define NS "ssmlog"

esp_err_t config_init(void)
{
    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        err = nvs_flash_init();
    }
    return err;
}

static void get(nvs_handle_t h, const char *key, char *out, size_t cap)
{
    size_t len = cap;
    if (nvs_get_str(h, key, out, &len) != ESP_OK) out[0] = 0;
}

void config_load(logger_config_t *c)
{
    memset(c, 0, sizeof *c);
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) return;
    get(h, "ssid", c->ssid, sizeof c->ssid);
    get(h, "pass", c->pass, sizeof c->pass);
    get(h, "url", c->url, sizeof c->url);
    get(h, "token", c->token, sizeof c->token);
    nvs_close(h);
    size_t n = strlen(c->url);
    while (n && c->url[n - 1] == '/') c->url[--n] = 0;
}

esp_err_t config_get(const char *key, char *out, size_t cap)
{
    out[0] = 0;
    nvs_handle_t h;
    esp_err_t err = nvs_open(NS, NVS_READONLY, &h);
    if (err != ESP_OK) return err;
    get(h, key, out, cap);
    nvs_close(h);
    return ESP_OK;
}

esp_err_t config_set(const char *key, const char *value)
{
    static const struct { const char *key; size_t cap; } keys[] = {
        {"ssid", sizeof ((logger_config_t *)0)->ssid}, {"pass", sizeof ((logger_config_t *)0)->pass},
        {"url", sizeof ((logger_config_t *)0)->url}, {"token", sizeof ((logger_config_t *)0)->token},
        {"sleep", 4}, {"wake_mv", 8}, {"adc_scale", 12},
    };
    size_t cap = 0;
    for (size_t i = 0; i < sizeof keys / sizeof *keys; i++) {
        if (strcmp(key, keys[i].key) == 0) cap = keys[i].cap;
    }
    if (!cap) return ESP_ERR_NOT_FOUND;
    if (strlen(value) >= cap) return ESP_ERR_INVALID_SIZE;
    nvs_handle_t h;
    esp_err_t err = nvs_open(NS, NVS_READWRITE, &h);
    if (err != ESP_OK) return err;
    err = value[0] ? nvs_set_str(h, key, value) : nvs_erase_key(h, key);
    if (err == ESP_ERR_NVS_NOT_FOUND) err = ESP_OK;
    if (err == ESP_OK) err = nvs_commit(h);
    nvs_close(h);
    return err;
}
