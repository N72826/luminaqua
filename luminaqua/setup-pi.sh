#!/bin/bash
# Install LuminAqua on a Raspberry Pi (Raspberry Pi OS Bookworm/Trixie). Safe to run again to update.
#   cd luminaqua && bash setup-pi.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$APP_DIR"

echo "== Updating the OS and installing Bluetooth + Python"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::=--force-confold full-upgrade
sudo DEBIAN_FRONTEND=noninteractive apt-get -y install python3-venv bluez rfkill

echo "== Making sure Bluetooth is switched on"
sudo rfkill unblock bluetooth || true
bluetoothctl power on || true

echo "== Python environment"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "== Service (starts on boot)"
sed -e "s#^User=pi#User=$USER#" -e "s#/home/pi/luminaqua#$APP_DIR#g" luminaqua.service \
  | sudo tee /etc/systemd/system/luminaqua.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable luminaqua
sudo systemctl restart luminaqua
sleep 5
systemctl is-active luminaqua
echo "== Done. Open http://$(hostname).local (or http://$(hostname -I | awk '{print $1}')) and press '+ Add light'."
