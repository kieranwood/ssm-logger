# Car logger: record every drive, upload it at home

`car_logger.py` runs on a Raspberry Pi that stays in the car with the OpenPort plugged into it.
It records every drive and, once the car is back in range of your Wi-Fi, uploads the recordings to
`log_server.py` on a computer at home. The recordings are the same CSVs `logger.py` makes, so they
open in the desktop app and the website, and `analyse.py` runs on them.

```
OBD port ── OpenPort 2.0 ── USB ── Raspberry Pi ~~ Wi-Fi ~~ log_server.py (home computer / NAS)
```

What it does, in order:

1. Tries to reach the ECU every few seconds. The ECU only answers with the ignition on.
2. Ignition on: records every supported parameter to `logs/ssm_YYYYMMDD_HHMMSS.csv.part`,
   about 20 rows a second, flushing to the SD card every second.
3. Ignition off (the ECU silent for 15 s): the file becomes `….csv`. If the engine ran for less than
   10 s, for example the key was only turned to ON, the recording is thrown away instead.
4. Every 20 s, if any drives are waiting, it checks whether the server can be reached. Once the car is
   home, it uploads them (gzip-compressed and checked with SHA-256) and moves the local copies to
   `logs/uploaded/`. Drives recorded away from home wait for the next time the car is home.
5. Optionally it powers the Pi off: 2 minutes after the last drive once everything is uploaded,
   15 minutes after it if the server can't be reached, or straight away if the car battery drops
   below 11.8 V.

A brief dropout, or a stall and restart, carries on in the same file. A drive that is cut off by a
power cut is kept: the `.part` file is finished at the next start (at most the last second is lost).

## What to use

**A Raspberry Pi, not an ESP32, for the logging.** The OpenPort is a USB device, and all the code that
talks to it (`openport.py`, `sources.py`, `defs.py`) is Python, so a Pi runs it unchanged. An ESP32
would need USB host support (only the S2/S3 have it) and a rewrite of the driver in C.
Your ESP32 boards are useful for power control, though (option C below).
An ESP32-S3 version that plugs the OpenPort into the S3's own USB port is in progress in
[`esp32/`](../esp32/README.md).

| Pi | Notes |
|---|---|
| **Zero 2 W** (best) | Low power (roughly 0.5–0.7 W idle), small. It needs a micro-USB OTG adapter for the OpenPort. |
| 3 B / 3 B+ / 4 | Work fine. They draw a few times more power, which matters if the Pi stays on with the engine off. |
| Zero W (original) | Works, but it is slow, and it has to run a Raspberry Pi OS new enough for Python 3.11. |
| 5 | Works. It has its own real-time clock, and can wake from off with its power button pads. |

You also need Raspberry Pi OS **Bookworm or newer** (Lite is enough), a decent SD card, and a
12 V → 5 V supply (a 3 A buck converter or a good USB car charger).

Check that your Wi-Fi reaches the car where it parks. One way: park, then run `nmcli dev wifi` on
the Pi and look at the signal.

## Power

The OpenPort is powered by the OBD port (pin 16, which is live all the time). The question is
how the Pi gets power, and what switches it back on after it has powered itself off. A halted Pi
does not start again by itself: its power has to be cut and restored, or GPIO3 pulled to ground.

| Option | Wiring | Draw parked | `shutdown` |
|---|---|---|---|
| **A. Always on** | Pi on constant 12 V (an OBD splitter with a power pigtail, or a fuse tap on a constant circuit) | The Pi's idle draw, all the time | `false` |
| **B. Battery pack that switches the Pi on** | A UPS board or HAT on the Pi, charged from a socket that is only live with the ignition on | None from the car | `true` |
| **C. Power controller** | An ESP32 (or a ready-made car power switch) on constant 12 V switches the Pi's supply | The controller's sleep draw | `true` |

**A: always on.** This is the simplest, and it works as soon as you install. A Zero 2 W idling draws
roughly 50 mA from the 12 V side, so about 1.2 Ah a day, or 8–9 Ah over a week parked. That is a
fair share of a car battery, so leave `low_battery_v` set. If the car sits for weeks, choose B or C.
Note that if the low-battery cut-off ever powers the Pi off, it stays off until its power is cycled.

**B: separate battery.** This uses no car power when parked, and needs no wiring into the car if
your accessory socket is switched with the ignition (check yours). Ignition on: the socket charges
the pack and the Pi runs. Ignition off: the Pi runs from the pack, uploads, and powers off. For the next
drive, the board must switch the Pi on when charging power comes back. Some UPS boards (PiSugar for
the Zero, for example) have an "auto power on" setting for this; check that the board you use does
before buying. Without it, a small circuit that briefly grounds GPIO3 when the socket goes live does
the same job.

**C: power controller.** The ESP32 sits on constant power in deep sleep and wakes every few seconds
to measure the battery voltage through a resistor divider. Above about 13.2 V the alternator is
charging, so the engine is running, and it switches the Pi on through a MOSFET or relay. After the
Pi has halted, it switches the Pi off again. To tell it when, add `dtoverlay=gpio-poweroff,gpiopin=26`
to the Pi's `/boot/firmware/config.txt`: GPIO26 then goes high only once the Pi has fully halted. Read
it with a level-safe input. A bare ESP32 module draws microamps in deep sleep, but dev boards draw more
(their USB chip and regulator stay powered), typically a few mA: measure yours. Ready-made "car power
supply" boards for the Pi do the same job using an ignition-switched wire from a fuse tap.

Whichever you choose, avoid cutting the Pi's power while it is running: that risks corrupting the SD
card. With B and C, the logger powers the Pi off cleanly first.

## Setup

### On the home server

On an always-on Linux machine (Ubuntu, Debian, a NAS running one), from a copy of this repo:

```sh
sh pi/install-server.sh                  # or: sh pi/install-server.sh /path/to/drives 8765
```

That installs `python3-numpy`, which converting the ESP32's raw drives needs, and downloads the
RomRaider definitions. It then makes a token, keeps it in `/etc/ssm-log-server.env` (root-only),
installs `log_server.py` as the `ssm-log-server` service, and prints the `url` and `token` to give
the loggers. Drives go to `~/car-logs` by default: CSVs at the top, and the ESP32's raw files in
`raw/`. Watch it with `journalctl -u ssm-log-server -f`. If `ufw` is on, the script prints the rule to
let the loggers in.

Reserve the server's IP in your router, so the address the loggers were given keeps working.

Without the installer, on any computer with Python 3.10 or newer:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'    # make a token
python3 log_server.py --dir ~/car-logs --token THE_TOKEN          # port 8765 by default
```

The server is plain HTTP with a shared token, which is fine on a home network. Don't expose it to the
internet.

### On the Pi

1. Flash Raspberry Pi OS Lite with Raspberry Pi Imager. In its settings, set your home Wi-Fi, a user
   and SSH. The Pi rejoins that network whenever it is in range.
2. Copy this repo to the Pi (`git clone` or `scp`), then run:

   ```sh
   cd ssm-logger
   sh pi/install.sh
   ```

   This installs `python3-serial` and `python3-numpy`, adds you to the `dialout` group, downloads
   the RomRaider definitions, allows `systemctl poweroff` without a password for your user, and starts
   the `ssm-car-logger` service.
3. Edit `car_logger.toml`: the server `url` and `token`, and `shutdown = true` if you chose power
   option B or C. Then run `sudo systemctl restart ssm-car-logger`.
4. Watch it with `journalctl -u ssm-car-logger -f`.

### Testing before it goes in the car

- On any computer, `python3 car_logger.py --demo --config car_logger.toml` simulates a 60 s drive
  every 90 s and uploads them, so it tests the server, the token and the Wi-Fi. It never powers off.
- In the car, plug in the OpenPort and turn the ignition on. The journal should show
  `Ignition on (ECU …): recording …`, then `Saved …` 15 s after you switch off, and `Uploaded …` if
  the server can be reached.
- `python3 car_logger.py --upload-now` uploads whatever is waiting and exits.

## Things to know

- **Clock.** Most Pis have no real-time clock; Raspberry Pi OS saves the time at shutdown and restores
  it at boot. If the Pi has been off, a drive recorded before it gets back online can be named a little
  early, though the times inside the file are always right relative to its start. For exact names, add
  a DS3231 RTC module (`dtoverlay=i2c-rtc,ds3231`). The Pi 5 has its own.
- **Size.** A recording is roughly 40–50 MB an hour, and about a tenth of that when sent over Wi-Fi.
  Uploaded copies stay on the Pi for 30 days (`keep_uploaded_days`), or are deleted sooner, oldest
  first, if free space falls below 500 MB.
- **The OpenPort** draws a little from the OBD port whenever it is plugged in, whatever the Pi is doing.
- **Polling** is read-only, the same SSM reads `logger.py` makes, but it runs on every drive.
  Unplug it if you notice anything odd.
- **Linux + OpenPort** is untested on a real car (see the main README). If the cable isn't found, set
  `[ecu] port = "/dev/ttyACM0"`.
