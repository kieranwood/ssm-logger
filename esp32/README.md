# ssm-esp32: the car logger on an ESP32-S3

This is an ESP32-S3 version of [`car_logger.py`](../car_logger.py) for a YD-ESP32-S3 (N8R2) board. The
OpenPort plugs into the board's own USB port, which acts as a USB host. It is being built in stages:

1. ~~Connection test~~: passed in the car (ECU 7152345007, 100/100 reads). The code is in commit 39f2986.
2. **Logging** (this firmware): every drive is recorded to flash as raw SSM replies.
3. Wi-Fi upload to `log_server.py`, which turns the raw replies into the usual CSV.
4. Deep sleep while parked, waking on the battery voltage.

`main/openport.c` is a C port of the parts of [`openport.py`](../openport.py) that SSM logging needs: the
same commands and the same packet parsing. Like the Python logger, it only reads from the ECU.

## What it does

It behaves like `car_logger.py`:

- **Waiting:** it tries the ECU every couple of seconds. The ECU only answers with the ignition on.
- **Recording:** with the ignition on, it records every parameter the ECU supports to
  `drive_NNNN.part` on the board's flash. It works out the parameters from the ECU's init reply, the
  same way `defs.py` does, using a table generated from `log_defs.xml` when the firmware is built.
- **Saving:** once the ECU has been silent for 15 s, the file becomes `drive_NNNN.raw`. If the engine
  ran for less than 10 s, the drive is thrown away instead.
- **Dropouts and power cuts:** a dropout or a stall carries on in the same file. A file cut off by a
  power cut is finished at the next start, losing at most the last second.

The 6.4 MB of storage holds about an hour and a half of driving. When it fills up, the drive in
progress is ended and saved, and no more are recorded until drives are fetched off the board.

The `.raw` format is described in [`main/drive_log.h`](main/drive_log.h). [`../ssmraw.py`](../ssmraw.py)
converts it to the logger's CSV with `defs.py`, giving the same file the desktop logger would have
recorded.

## Hardware: YD-ESP32-S3

The board has two USB-C ports:

| Port | Goes to | Use |
|---|---|---|
| **COM** | CH343 USB-serial chip | Flashing, the console, fetching drives: plug your Mac in here |
| **USB** | The S3's own USB | The OpenPort plugs in here (through a USB-C to USB-A adapter) |

**5 V on the USB port.** As a host, the board has to supply 5 V on the USB port, or the OpenPort is never
detected. On this board, closing the `USB-OTG` solder jumper does it. That jumper joins the two USB-C
ports' power lines; `IN-OUT` bridges the diode between USB power and the 5 V pin.

- **On the bench:** with `USB-OTG` closed, the 5 V from your Mac on COM also reaches the OpenPort.
- **In the car on its own:** power the board from its 5 V pin through a 12 V → 5 V converter. Then
  **never plug your Mac into either port at the same time**: the car supply would back-feed your Mac.

The OpenPort itself is powered from the OBD port, so it only reaches the ECU when it's plugged into the car.

## Build and flash

You need ESP-IDF 5.1 or newer (built with 5.3.1), plus RomRaider's definitions: run `../fetch_defs.py`
once, because the build generates the parameter table from `../definitions/log_defs.xml`. The USB
host serial and LittleFS components download automatically on the first build.

```sh
. ~/source/esp-adf/esp-idf/export.sh     # or wherever your ESP-IDF is
cd esp32
idf.py set-target esp32s3                 # first time only
idf.py build
idf.py -p PORT flash monitor              # the COM port; Ctrl-] to quit
```

`PORT` is the COM port's device, for example `/dev/cu.wchusbserial…` (`ls /dev/cu.*` shows it). The
console runs at **921600 baud**: `idf.py monitor` picks that up by itself, but other terminals need to
be told. The first lines after a reset are always garbled, because the chip's boot ROM talks at 115200.

If `export.sh` complains about a missing `idf5.3_py3.14_env`, it has picked up a Python newer than your
ESP-IDF install. Point it at the environment you already have:
`export IDF_PYTHON_ENV_PATH=~/.espressif/python_env/idf5.3_py3.13_env` before sourcing it.

Flashing this over stage 1 changes the partition table. The first boot formats the log storage, and
prints a harmless `Corrupted dir pair` warning while it does.

## Fetching drives

```sh
.venv/bin/python esp32/fetch_drives.py --port PORT --status   # what it's doing, and what's stored
.venv/bin/python esp32/fetch_drives.py --port PORT            # copy and convert every drive
.venv/bin/python esp32/fetch_drives.py --port PORT --delete   # ...and delete them from the board
```

Drives are copied to `logs/esp32/drive_NNNN.raw` and checked against the board's CRC-32. They are then
converted to `logs/ssm_drive_NNNN.csv`, which opens in the desktop app and the website. An hour's drive
takes about a minute to copy.

**Opening the COM port resets the board.** This happens with both macOS drivers, because the
auto-reset circuit catches a glitch while the port opens. So fetch drives while the car is parked. If a
reset does land during a drive, the drive so far is kept as its own file and recording carries on in
a new one.

The console commands, if you want them by hand: `ls`, `get NAME`, `rm NAME`, `df`, `status`.

## Host tests

The firmware's logic also runs on your Mac, against a fake OpenPort with a fake ECU behind it
(`test/fake_openport.inc`). The fake replies the way `web/fake-ecu.js` does, in random-sized pieces with
junk mixed in:

```sh
cd esp32/test
# the OpenPort driver: damaged packets, a missing ECU, unplugging
cc -std=c11 -Wall -fsanitize=address,undefined -Istub -o /tmp/test_openport test_openport.c && /tmp/test_openport
# recording drives, then checking the files with ssmraw.py
rm -rf /tmp/ssmdrives && mkdir -p /tmp/ssmgen /tmp/ssmdrives
python3 ../gen_params.py ../../definitions/log_defs.xml /tmp/ssmgen/ssm_params.h
cc -std=c11 -Wall -fsanitize=address,undefined -Istub -I/tmp/ssmgen -o /tmp/test_drive_log test_drive_log.c ../main/drive_log.c
/tmp/test_drive_log /tmp/ssmdrives && ../../.venv/bin/python test_ssmraw.py /tmp/ssmdrives
```

The recording test runs a 36 s drive, a key-on-only session (discarded), a drive with a 5 s dropout,
and a power cut. `test_ssmraw.py` then checks that the firmware read exactly the addresses `defs.py`
would, and that the CSVs carry the fake ECU's values.
