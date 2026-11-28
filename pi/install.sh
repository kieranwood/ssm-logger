#!/bin/sh
# Sets up car_logger.py on a Raspberry Pi (Raspberry Pi OS Bookworm or newer, for Python 3.11+).
# Run it from a clone of this repo, as the user the logger should run as:
#   sh pi/install.sh
set -eu
cd "$(dirname "$0")/.."
REPO=$(pwd)
ME=$(id -un)

echo "== Packages"
sudo apt-get update -q
sudo apt-get install -y python3-serial python3-numpy

echo "== Serial port access (the OpenPort)"
sudo usermod -aG dialout "$ME"

echo "== RomRaider parameter definitions"
[ -f definitions/log_defs.xml ] || python3 fetch_defs.py

echo "== Settings"
if [ ! -f car_logger.toml ]; then
    cp pi/car_logger.toml.example car_logger.toml
    chmod 600 car_logger.toml
    NEW_SETTINGS=1
fi

echo "== Allow the logger to power the Pi off"
RULE=/etc/sudoers.d/ssm-car-logger
echo "$ME ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff" > /tmp/ssm-car-logger.sudoers
sudo visudo -cf /tmp/ssm-car-logger.sudoers >/dev/null
sudo install -m 440 /tmp/ssm-car-logger.sudoers "$RULE"
rm /tmp/ssm-car-logger.sudoers

echo "== Service"
sed "s|@USER@|$ME|g; s|@REPO@|$REPO|g" pi/ssm-car-logger.service \
    | sudo tee /etc/systemd/system/ssm-car-logger.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable ssm-car-logger
sudo systemctl restart ssm-car-logger

echo
echo "Installed. Logs: journalctl -u ssm-car-logger -f"
if [ "${NEW_SETTINGS:-}" ]; then
    echo "Now edit $REPO/car_logger.toml (server url and token), then: sudo systemctl restart ssm-car-logger"
fi
