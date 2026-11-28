#!/bin/sh
# Sets up log_server.py as a service on an always-on Linux machine (Ubuntu, Debian, Raspberry Pi OS),
# to receive drives from car_logger.py and the ESP32 logger. Run it from a clone of this repo, as the
# user the server should run as:
#   sh pi/install-server.sh [DIR] [PORT]
# DIR: where drives are saved (default ~/car-logs). PORT: default 8765.
set -eu
cd "$(dirname "$0")/.."
REPO=$(pwd)
ME=$(id -un)
DIR=$(realpath -m "${1:-$HOME/car-logs}")
PORT=${2:-8765}
ENV=/etc/ssm-log-server.env

echo "== Packages (numpy: converting the ESP32's raw drives to CSV)"
sudo apt-get update -q
sudo apt-get install -y python3 python3-numpy

echo "== RomRaider parameter definitions"
[ -f definitions/log_defs.xml ] || python3 fetch_defs.py

echo "== Drives folder: $DIR"
mkdir -p "$DIR"

echo "== Token"
if sudo test -f "$ENV"; then
    echo "keeping the token in $ENV"
else
    TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
    printf 'SSM_UPLOAD_TOKEN=%s\n' "$TOKEN" | sudo tee "$ENV" >/dev/null
    sudo chmod 600 "$ENV"
fi

echo "== Service"
sed "s|@USER@|$ME|g; s|@REPO@|$REPO|g; s|@DIR@|$DIR|g; s|@PORT@|$PORT|g; s|@ENV@|$ENV|g" \
    pi/ssm-log-server.service | sudo tee /etc/systemd/system/ssm-log-server.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable ssm-log-server
sudo systemctl restart ssm-log-server
sleep 1
systemctl --no-pager --lines=3 status ssm-log-server || true

if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
    echo
    echo "ufw is on: allow the logger in from your home network, e.g."
    echo "  sudo ufw allow from 192.168.0.0/16 to any port $PORT proto tcp"
fi

IP=$(hostname -I | awk '{print $1}')
echo
echo "Installed. Logs: journalctl -u ssm-log-server -f    Drives: $DIR"
echo "Give the loggers:"
echo "  url   = http://$IP:$PORT        (reserve this IP for the server in your router)"
echo "  token = $(sudo sed -n 's/^SSM_UPLOAD_TOKEN=//p' "$ENV")"
