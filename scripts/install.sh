#!/bin/bash
# One-time setup on Raspberry Pi OS Bookworm (64-bit).
# Conveyor Pi:  bash scripts/install.sh
# Packing Pi:   bash scripts/install.sh packing
set -e
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_USER="$(whoami)"
STATION="${1:-conveyor}"
if [ "$STATION" = "packing" ]; then CONFIG_FILE="config.packing.yaml"; else CONFIG_FILE="config.yaml"; fi

echo "== packages"
sudo apt update
sudo apt install -y python3-picamera2 python3-opencv python3-gpiozero python3-lgpio python3-yaml python3-venv

echo "== python env (uses the system picamera2 / opencv)"
python3 -m venv --system-site-packages "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install fastapi "uvicorn>=0.29" pymssql zxing-cpp

if [ "$STATION" = "conveyor" ]; then
  echo "== camera overlay for OV9281"
  CONFIG=/boot/firmware/config.txt
  if ! grep -q "^dtoverlay=ov9281" "$CONFIG"; then
    sudo sed -i 's/^camera_auto_detect=1/camera_auto_detect=0/' "$CONFIG"
    echo "dtoverlay=ov9281" | sudo tee -a "$CONFIG" > /dev/null
    echo "   added dtoverlay=ov9281 (reboot needed)"
  fi
fi  # Camera Module 3 at packing is detected automatically

echo "== ERP login file"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "   edit $APP_DIR/.env and put the read-only ERP login in it"
fi

echo "== service (starts on boot)"
sudo tee /etc/systemd/system/book-inspector.service > /dev/null <<EOF
[Unit]
Description=Book Inspector $STATION station
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m app --config $APP_DIR/$CONFIG_FILE
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable book-inspector

echo "== touchscreen kiosk (Chromium full screen on login)"
sudo raspi-config nonint do_blanking 1 || true   # keep the screen on
KIOSK='sh -c "sleep 6; (chromium-browser || chromium) --kiosk --noerrdialogs --disable-infobars --incognito http://localhost:8080"'
mkdir -p ~/.config/labwc
grep -q "localhost:8080" ~/.config/labwc/autostart 2>/dev/null || echo "$KIOSK &" >> ~/.config/labwc/autostart
if [ -f ~/.config/wayfire.ini ] && ! grep -q "localhost:8080" ~/.config/wayfire.ini; then
  printf '\n[autostart]\nbook_inspector = %s\n' "$KIOSK" >> ~/.config/wayfire.ini
fi

echo "Done. Reboot, then the screen opens the station by itself."
