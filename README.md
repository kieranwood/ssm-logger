# ssm-logger

A live data logger, fault-code reader and fuel-trim analyser for Subaru ECUs that speak
SSM over CAN, using a **Tactrix OpenPort 2.0** cable on macOS (Linux should work but is untested). Written for and tested on a
2012 Forester (FB25, ECU `7152345007`).

![The logger reviewing a recording from a car with a failing front A/F sensor](docs/screenshot.png)

*Reviewing a recording of a P0171 (system too lean) fault on a 2012 Forester. Fuel trim climbs
to about +35% and only drops back after fuel cuts. The front A/F sensor reads flat 14.7 AFR
while the rear O2 sensor reads rich. Replacing the front A/F sensor fixed it.*

## Features

- **Live logging** of every SSM parameter the ECU reports as supported, charted in real
  time and recorded to `logs/ssm_YYYYMMDD_HHMMSS.csv`. Press **M** to drop numbered markers
  (handy while wiggling connectors), **Space** to pause and scroll back.
- **Fault codes**: stored, pending and permanent OBD-II codes, freeze frame, readiness
  monitors, and clearing (standard OBD-II modes 01/02/03/04/07/0A).
- **Analysis**: rule-based checks aimed at fuel-trim and sensor faults - trims against
  coolant temperature and airflow, front A/F vs rear O2 sensor, A/F sensor response to fuel
  cuts, MAF glitches, charging voltage, misfire counts. Each finding links to the time
  ranges it is based on.
- **Log review** of saved recordings, including CSVs from the OpenPort's own SD-card logging.

## Browser version

**[kieranwood.github.io/ssm-logger](https://kieranwood.github.io/ssm-logger/)** - nothing to install.

- **View and analyse recordings** in any modern browser: drop in a CSV from this logger or from
  the OpenPort's SD card. [Open it with a sample recording](https://kieranwood.github.io/ssm-logger/?sample)
  (the failing front A/F sensor log in the screenshot above).
- **Log live from the car** in Chrome or Edge (version 137 or newer) on a desktop computer:
  plug in the OpenPort, turn the ignition on and press **Connect to car**. Charts, markers (M),
  pause (Space), recording to a CSV download in the desktop app's format, analysis and fault
  codes (read and clear) all work as in the desktop app.
- **[Live demo](https://kieranwood.github.io/ssm-logger/?demo)**: replays the sample through an
  emulated OpenPort and ECU, so you can try live mode without a car, in any browser. (Reading
  fault codes in the demo also needs Chrome or Edge 137+.)

It runs this repo's own `recording.py`, `analyse.py`, `defs.py` and `dtc.py` in the browser with
[Pyodide](https://pyodide.org), and talks to the cable with Web Serial, so files and data never
leave your computer. The first visit downloads about 10 MB (Python and numpy), which the browser
then caches. Live mode also downloads RomRaider's `log_defs.xml` from the same pinned commit as
`fetch_defs.py`.

Live logging in the browser is new and has so far only been tested against the emulated ECU -
if it doesn't connect to your car, the desktop app is the fallback (and please open an issue).

To run it locally, serve the repo root and open `/web/`:

```sh
python3 -m http.server 8000    # then open http://localhost:8000/web/
```

`/web/?url=path/to/log.csv` opens a recording straight away, `?sample` the sample and `?demo`
the live demo.

## Requirements

- Python 3.10+ on macOS or Linux
- Tactrix OpenPort 2.0 (shows up as a USB serial device; no J2534 driver needed)
- RomRaider's logger definitions (`log_defs.xml`) for the parameter list, addresses and
  scaling - downloaded by `fetch_defs.py`, see below

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python fetch_defs.py    # downloads RomRaider's log_defs.xml into definitions/
```

`fetch_defs.py` downloads the file from a fixed [RomRaider](https://github.com/RomRaider/RomRaider)
commit and checks its SHA-256. To use a different copy, set `SSM_LOG_DEFS=/path/to/log_defs.xml`
or pass `--defs` to `logger.py`. An existing RomRaider checkout at
`~/source/RomRaider/definitions/log_defs.xml` is also picked up.

## Usage

```sh
.venv/bin/python logger.py                  # connect to the car (ignition on)
.venv/bin/python logger.py --demo           # simulated data, no car needed
.venv/bin/python logger.py --open FILE.csv  # review a saved log

.venv/bin/python dtc.py                     # read codes, freeze frame, readiness
.venv/bin/python dtc.py --clear             # clear codes (asks first)
.venv/bin/python dtc.py --port /dev/ttyACM0  # any command: name the cable if auto-detect fails

.venv/bin/python analyse.py logs/ssm_....csv
.venv/bin/python probe.py [--debug]         # minimal connection test
```

## Platforms

| Platform | Status |
|---|---|
| macOS | Tested with a real car. |
| Linux | Should work, untested on a car. The cable usually appears as `/dev/ttyACM0`. If you get "permission denied", add yourself to the serial group and log in again: `sudo usermod -aG dialout $USER` (`uucp` on Arch). |
| Windows | Not supported for talking to the car: Tactrix's Windows driver normally takes over the cable and expects programs to use its J2534 interface. Demo mode, log review and `analyse.py` work. |

The cable is found by its USB ID (`0403:cc4c`), falling back to `/dev/cu.usbmodem*` (macOS) and
`/dev/ttyACM*` (Linux). `--port` overrides it on `logger.py`, `dtc.py` and `probe.py`.

## Files

| File | Purpose |
|---|---|
| `logger.py` | Qt GUI: live charts, recording, markers, review, fault codes, analysis |
| `sources.py` | ECU data source (SSM over ISO 15765) and a demo simulator |
| `openport.py` | Minimal OpenPort 2.0 driver over its USB serial port |
| `defs.py` | Loads SSM parameter definitions from RomRaider's `log_defs.xml` |
| `fetch_defs.py` | Downloads `log_defs.xml` from RomRaider into `definitions/` |
| `dtc.py` | OBD-II codes, freeze frame and readiness monitors |
| `analyse.py` | Rule-based checks on a recording |
| `recording.py` | Loads logger CSVs and OpenPort SD-card CSVs |
| `probe.py` | Proof-of-concept connection test |
| `web/` | Browser version: viewer, live logging over Web Serial (`openport.js`, `live.js`), demo ECU (`fake-ecu.js`), Python glue (`webapp.py`). Deployed to GitHub Pages by `.github/workflows/pages.yml` |
| `sdcard/logcfg.txt` | OpenPort standalone (SD card) logging config for the 2012 Forester |

## Limitations

- **Subaru only** for live logging: it reads SSM memory addresses, which other makes don't
  answer. The fault-code reader uses standard OBD-II and should work on most cars with
  500 kbit/s CAN and the engine ECU at `0x7E0`/`0x7E8`.
- The analysis thresholds are rules of thumb for a naturally aspirated FB25 with a front
  wideband A/F sensor and a rear narrowband O2 sensor. Findings are hints, not a diagnosis.
- On this ECU, *A/F Correction #3* rests around −50 %, not 0 % (`C3_REST` in `analyse.py`).

## Licence

This project is released under the [MIT License](LICENSE).

The parameter definitions file `log_defs.xml` belongs to the
[RomRaider](https://github.com/RomRaider/RomRaider) project, which is licensed under the GNU
GPL v2 or later. It is not included in this repository; `fetch_defs.py` downloads it from
RomRaider at setup time. Thanks to the RomRaider contributors for their work documenting
Subaru's SSM protocol.

Not affiliated with or endorsed by Subaru, RomRaider or Tactrix. Use at your own risk -
clearing codes or polling the ECU while driving is your responsibility.
