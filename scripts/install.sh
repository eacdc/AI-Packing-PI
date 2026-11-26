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

echo "== restart / shut down from the touchscreen (Settings)"
SUDOERS=/etc/sudoers.d/book-inspector
echo "$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl reboot, /usr/bin/systemctl poweroff" | sudo tee "$SUDOERS.tmp" > /dev/null
sudo chmod 440 "$SUDOERS.tmp"
if sudo visudo -cf "$SUDOERS.tmp" > /dev/null; then sudo mv "$SUDOERS.tmp" "$SUDOERS"; else sudo rm -f "$SUDOERS.tmp"; fi

echo "== touchscreen kiosk (Chromium full screen on login)"
sudo raspi-config nonint do_blanking 1 || true   # keep the screen on
# Newer Raspberry Pi OS names the browser "chromium", older "chromium-browser".
BROWSER="$(command -v chromium-browser || command -v chromium || echo chromium)"
KIOSK="sh -c \"sleep 6; $BROWSER --kiosk --noerrdialogs --disable-infobars --incognito --password-store=basic http://localhost:8080\""
# Re-running install.sh replaces an old kiosk line instead of adding a second one.
mkdir -p ~/.config/labwc
touch ~/.config/labwc/autostart
sed -i '/localhost:8080/d' ~/.config/labwc/autostart
echo "$KIOSK &" >> ~/.config/labwc/autostart
if [ -f ~/.config/wayfire.ini ]; then
  sed -i '/localhost:8080/d' ~/.config/wayfire.ini
  if grep -q '^\[autostart\]' ~/.config/wayfire.ini; then
    sed -i "/^\[autostart\]/a book_inspector = $KIOSK" ~/.config/wayfire.ini
  else
    printf '\n[autostart]\nbook_inspector = %s\n' "$KIOSK" >> ~/.config/wayfire.ini
  fi
fi

echo "Done. Reboot, then the screen opens the station by itself."
