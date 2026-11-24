# ssm-logger

A live data logger, fault-code reader and fuel-trim analyser for Subaru ECUs that speak
SSM over CAN, using a **Tactrix OpenPort 2.0** cable on macOS. Written for and tested on a
2012 Forester (FB25, ECU `7152345007`).

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

## Requirements

- macOS with Python 3.10+
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

.venv/bin/python analyse.py logs/ssm_....csv
.venv/bin/python probe.py [--debug]         # minimal connection test
```

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
