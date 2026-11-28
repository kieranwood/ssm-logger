# ssm-esp32: the car logger on an ESP32-S3

This is an ESP32-S3 version of [`car_logger.py`](../car_logger.py) for a YD-ESP32-S3 (N8R2) board. The
OpenPort plugs into the board's own USB port, which acts as a USB host. It is being built in stages:

1. **Connection test** (this firmware). It finds the OpenPort, asks it to identify itself, reads the OBD
   voltage, gets the ECU's ID, and times 100 reads.
2. Logging: drives saved as raw SSM replies to flash, which is about 4 MB an hour.
3. Wi-Fi upload to `log_server.py`, which turns the raw replies into the usual CSV.
4. Deep sleep while parked, waking on the battery voltage.

`main/openport.c` is a C port of the parts of [`openport.py`](../openport.py) that SSM logging needs: the
same commands and the same packet parsing. Like the Python logger, it only reads from the ECU.

## Hardware: YD-ESP32-S3

The board has two USB-C ports:

| Port | Goes to | Use |
|---|---|---|
| **COM** | CH343 USB-serial chip | Flashing and the console: plug your Mac in here |
| **USB** | The S3's own USB | The OpenPort plugs in here (through a USB-C to USB-A adapter) |

**5 V on the USB port.** As a host, the board has to supply 5 V on the USB port, or the OpenPort is never
detected. That depends on the solder jumpers near the ports. According to community notes, `IN-OUT`
bridges the diode between USB power and the 5 V rail, and `USB-OTG` joins the two USB-C ports' power lines.
Check the board's schematic for your revision before soldering. Then measure 5 V on the USB port's
VBUS pin before plugging the OpenPort in.

- **On the bench:** with `USB-OTG` closed, the 5 V from your Mac on COM also reaches the OpenPort.
- **In the car:** power the board from its 5 V pin through a 12 V → 5 V converter. Then **never plug your
  Mac into either port at the same time**: with the jumpers closed, the car supply would back-feed your Mac.

The OpenPort itself is powered from the OBD port, so for this test it must be plugged into the car.
Without the car, it gets as far as the voltage reading.

## Build and flash

This needs ESP-IDF 5.1 or newer (5.3.1 tested for the build). The USB host serial driver is downloaded
automatically on the first build.

```sh
. ~/source/esp-adf/esp-idf/export.sh     # or wherever your ESP-IDF is
cd esp32
idf.py set-target esp32s3                 # first time only
idf.py build
idf.py -p PORT flash monitor              # the COM port; Ctrl-] to quit
```

`PORT` is the COM port's device: `/dev/cu.usbmodem…` or `/dev/cu.wchusbserial…` depending on the
driver. `ls /dev/cu.*` with the board plugged in and unplugged shows which.

If `export.sh` complains about a missing `idf5.3_py3.14_env`, it has picked up a Python newer than your
ESP-IDF install. Point it at the environment you already have:
`export IDF_PYTHON_ENV_PATH=~/.espressif/python_env/idf5.3_py3.13_env` before sourcing it.

## Running the test

1. Flash it with the Mac on **COM**. Watch the monitor.
2. Plug the OpenPort into the car's OBD port, and into the board's **USB** port.
3. Turn the ignition on (the engine can be off, or running).

Every 5 seconds it tries again and prints one line:

| Line | Meaning |
|---|---|
| `RESULT: PASS ECU 7152345007, ari …, 12.45 V, 100/100 reads, 21.3 reads/s, …` | Everything works |
| `RESULT: PARTIAL OpenPort OK …, no ECU reply - is the ignition on?` | USB and the OpenPort work; the ECU didn't answer |
| `RESULT: FAIL usb - nothing found on the USB port …` | No device detected: usually no 5 V on the USB port |
| `RESULT: FAIL usb - could not open the USB device …` | Something was detected but didn't open as a serial port; the `USB device attached:` line above it shows what |

When it does open, it prints the OpenPort's USB descriptors once. If anything goes wrong, paste
those together with the RESULT line.

## Host test

`test/test_openport.c` runs `openport.c` on your Mac against a fake OpenPort. The fake replies the way
`web/fake-ecu.js` does, in random-sized pieces, with junk, damaged packets, a missing ECU and unplugging:

```sh
cd esp32/test && cc -std=c11 -Wall -fsanitize=address,undefined -Istub -o /tmp/test_openport test_openport.c && /tmp/test_openport
```
