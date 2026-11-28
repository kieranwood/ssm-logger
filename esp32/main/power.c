// See power.h.
#include "power.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "config.h"
#include "driver/uart.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_attr.h"
#include "esp_log.h"
#include "esp_sleep.h"
#include "wake_rule.h"

static const char *TAG = "power";

#define SENSE_GPIO 4                    // ADC1: readable while Wi-Fi is on (ADC2 isn't)
#define CHECK_EVERY_US (5 * 1000000ULL)
#define DEFAULT_WAKE_MV 13200           // alternator charging: the engine is running
#define LOW_BATTERY_MV 11800            // parked and below this: sleep to save the battery
#define MIN_BELIEVABLE_MV 9000          // below this the divider isn't connected: never sleep
#define MAX_BELIEVABLE_MV 17000
#define DEFAULT_SCALE 5545              // battery mV per 1000 pin mV: a 100k / 22k divider
#define MAGIC 0x55AA0004u

// Kept in RTC memory through deep sleep, so a wake check needs no flash or NVS access.
RTC_DATA_ATTR static uint32_t s_magic;
RTC_DATA_ATTR static uint32_t s_scale;
RTC_DATA_ATTR static int s_wake_mv;
RTC_DATA_ATTR static bool s_enabled;
RTC_DATA_ATTR static int s_min_mv;          // lowest battery reading since going to sleep
RTC_DATA_ATTR static int s_above;

static char s_status[160];

static int read_pin_mv(void)
{
    adc_unit_t unit;
    adc_channel_t chan;
    if (adc_oneshot_io_to_channel(SENSE_GPIO, &unit, &chan) != ESP_OK) return 0;
    adc_oneshot_unit_handle_t u;
    adc_oneshot_unit_init_cfg_t ic = {.unit_id = unit};
    if (adc_oneshot_new_unit(&ic, &u) != ESP_OK) return 0;
    adc_oneshot_chan_cfg_t cc = {.atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_DEFAULT};
    adc_oneshot_config_channel(u, chan, &cc);
    adc_cali_handle_t cal = NULL;
    adc_cali_curve_fitting_config_t cf = {.unit_id = unit, .chan = chan, .atten = ADC_ATTEN_DB_12,
                                          .bitwidth = ADC_BITWIDTH_DEFAULT};
    adc_cali_create_scheme_curve_fitting(&cf, &cal);
    int sum = 0, n = 0;
    for (int i = 0; i < 16; i++) {
        int mv;
        if (cal && adc_oneshot_get_calibrated_result(u, cal, chan, &mv) == ESP_OK) {
            sum += mv;
            n++;
        }
    }
    if (cal) adc_cali_delete_scheme_curve_fitting(cal);
    adc_oneshot_del_unit(u);
    return n ? sum / n : 0;
}

int power_battery_mv(void)
{
    return (int)((int64_t)read_pin_mv() * (s_scale ? s_scale : DEFAULT_SCALE) / 1000);
}

static bool believable(int mv) { return mv >= MIN_BELIEVABLE_MV && mv <= MAX_BELIEVABLE_MV; }

static void sleep_now(void)
{
    esp_sleep_enable_timer_wakeup(CHECK_EVERY_US);
    esp_deep_sleep_start();
}

void power_early(void)
{
    if (esp_sleep_get_wakeup_cause() != ESP_SLEEP_WAKEUP_TIMER || s_magic != MAGIC || !s_enabled) return;
    int mv = power_battery_mv();
    if (!wake_check(mv, s_wake_mv, &s_min_mv, &s_above)) sleep_now();
    printf("\nWoke up: battery %.2f V (lowest asleep %.2f V) - the engine is running\n", mv / 1000.0,
           s_min_mv / 1000.0);
}

static int nvs_int(const char *key, int fallback)
{
    char buf[16];
    return config_get(key, buf, sizeof buf) == ESP_OK && buf[0] ? atoi(buf) : fallback;
}

void power_init(void)
{
    s_enabled = nvs_int("sleep", 0) == 1;
    s_wake_mv = nvs_int("wake_mv", DEFAULT_WAKE_MV);
    s_scale = nvs_int("adc_scale", DEFAULT_SCALE);
    s_magic = MAGIC;
}

void power_calibrate(int obd_mv)
{
    int pin = read_pin_mv();
    if (pin < 500 || !believable(obd_mv)) return;          // no divider, or a strange reading
    uint32_t scale = (uint32_t)((int64_t)obd_mv * 1000 / pin);
    if (abs((int)scale - (int)s_scale) * 100 > (int)s_scale) {   // more than 1% different: save it
        char buf[16];
        snprintf(buf, sizeof buf, "%lu", (unsigned long)scale);
        config_set("adc_scale", buf);
        ESP_LOGI(TAG, "Divider calibrated against the OpenPort: %.2f V reads %.3f V at GPIO%d",
                 obd_mv / 1000.0, pin / 1000.0, SENSE_GPIO);
    }
    s_scale = scale;
}

bool power_sleep_enabled(void) { return s_enabled && believable(power_battery_mv()); }

bool power_battery_low(void)
{
    int mv = power_battery_mv();
    return believable(mv) && mv < LOW_BATTERY_MV;
}

esp_err_t power_set_sleep(bool on, char *why, size_t cap)
{
    int mv = power_battery_mv();
    if (on && !believable(mv)) {
        snprintf(why, cap, "the divider on GPIO%d reads %.2f V - connect it first (without it nothing could "
                           "wake the board)", SENSE_GPIO, mv / 1000.0);
        return ESP_ERR_INVALID_STATE;
    }
    esp_err_t err = config_set("sleep", on ? "1" : "0");
    if (err == ESP_OK) s_enabled = on;
    return err;
}

esp_err_t power_set_wake_mv(int mv)
{
    if (mv < 12500 || mv > 15000) return ESP_ERR_INVALID_ARG;
    char buf[16];
    snprintf(buf, sizeof buf, "%d", mv);
    esp_err_t err = config_set("wake_mv", buf);
    if (err == ESP_OK) s_wake_mv = mv;
    return err;
}

void power_sleep(const char *why)
{
    int mv = power_battery_mv();
    s_min_mv = mv;
    s_above = 0;
    ESP_LOGI(TAG, "Going to sleep (%s): battery %.2f V; waking when the engine runs (above %.1f V, and "
             "%.1f V above the lowest reading)", why, mv / 1000.0, s_wake_mv / 1000.0, WAKE_RISE_MV / 1000.0);
    uart_wait_tx_idle_polling(CONFIG_ESP_CONSOLE_UART_NUM);
    sleep_now();
}

const char *power_status(void)
{
    int mv = power_battery_mv();
    snprintf(s_status, sizeof s_status, "battery %.2f V (GPIO%d divider%s), sleep %s, wakes above %.1f V", mv / 1000.0,
             SENSE_GPIO, believable(mv) ? "" : " not connected", s_enabled ? "on" : "off", s_wake_mv / 1000.0);
    return s_status;
}
