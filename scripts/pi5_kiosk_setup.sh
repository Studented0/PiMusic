#!/bin/bash
# PiMusic Pi 5 kiosk setup — run once on the Pi:
#   bash pi5_kiosk_setup.sh
# Points Chromium (fullscreen kiosk) at the PiMusic server on boot.

set -e

SERVER_URL="http://jeanine:5000"

echo "== PiMusic Pi 5 kiosk setup =="

# 1. Chromium (package name differs across releases)
sudo apt-get update
sudo apt-get install -y chromium 2>/dev/null || sudo apt-get install -y chromium-browser
CHROMIUM=$(command -v chromium || command -v chromium-browser)
echo "Chromium: $CHROMIUM"

# 2. Desktop autologin (boots straight into the graphical session)
sudo raspi-config nonint do_boot_behaviour B4

# 3. Never blank the screen
sudo raspi-config nonint do_blanking 1 || true

# 4. Kiosk autostart (labwc = Wayland compositor on current Pi OS desktop)
mkdir -p "$HOME/.config/labwc"
AUTOSTART="$HOME/.config/labwc/autostart"
# Remove any previous pimusic kiosk line, then append
touch "$AUTOSTART"
sed -i '/pimusic-kiosk/d' "$AUTOSTART"
cat >> "$AUTOSTART" <<EOF
$CHROMIUM --kiosk --noerrdialogs --disable-infobars --disable-session-crashed-bubble --autoplay-policy=no-user-gesture-required --check-for-update-interval=31536000 $SERVER_URL & # pimusic-kiosk
EOF
echo "labwc autostart written: $AUTOSTART"

# 5. Fallback for older X11/LXDE sessions (harmless if unused)
mkdir -p "$HOME/.config/lxsession/LXDE-pi"
XAUTO="$HOME/.config/lxsession/LXDE-pi/autostart"
touch "$XAUTO"
sed -i '/pimusic-kiosk/d' "$XAUTO"
echo "@$CHROMIUM --kiosk --noerrdialogs --disable-infobars --autoplay-policy=no-user-gesture-required $SERVER_URL # pimusic-kiosk" >> "$XAUTO"

echo ""
echo "Done. Rebooting into kiosk in 5s (Ctrl+C to cancel)..."
sleep 5
sudo reboot
