#!/bin/sh
# VoyagerInterface installer for Debian 12. Run from this folder:  sh install.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
echo "Installing packages (asks for your password) ..."
sudo apt-get update
sudo apt-get install -y python3 python3-tk
echo "Installing program to /opt/voyagerinterface ..."
sudo install -d /opt/voyagerinterface
sudo install -m 0755 "$HERE/voyagerinterface.py" /opt/voyagerinterface/voyagerinterface.py
echo "Installing USB rule ..."
sudo install -m 0644 "$HERE/99-voyagerinterface.rules" /etc/udev/rules.d/99-voyagerinterface.rules
getent group plugdev >/dev/null || sudo groupadd plugdev
RELOGIN=no
if ! id -nG "$USER" | tr ' ' '\n' | grep -qx plugdev; then
  sudo usermod -aG plugdev "$USER"
  RELOGIN=yes
fi
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=hidraw --action=change || true
# a calculator that is already connected: give access now, no reconnect needed
for d in /sys/class/hidraw/hidraw*; do
  [ -e "$d/device/uevent" ] || continue
  if grep -qi '^HID_ID=0003:000003F0:00001341' "$d/device/uevent"; then
    dev="/dev/$(basename "$d")"
    sudo chgrp plugdev "$dev"; sudo chmod 0660 "$dev"
    command -v setfacl >/dev/null && sudo setfacl -m "u:$USER:rw" "$dev" || true
    echo "Calculator already connected ($dev): access given."
  fi
done
mkdir -p "$HOME/VoyagerInterface Backups"
echo "Adding desktop launcher ..."
DESK="[Desktop Entry]
Type=Application
Name=VoyagerInterface
Comment=Back up and restore HP 12C/15C/16C CE memory over USB (unofficial)
Exec=python3 /opt/voyagerinterface/voyagerinterface.py
Icon=media-floppy
Terminal=false
Categories=Utility;"
mkdir -p "$HOME/.local/share/applications"
printf '%s\n' "$DESK" > "$HOME/.local/share/applications/voyagerinterface.desktop"
if [ -d "$HOME/Desktop" ]; then
  printf '%s\n' "$DESK" > "$HOME/Desktop/voyagerinterface.desktop"
  chmod +x "$HOME/Desktop/voyagerinterface.desktop"
  command -v gio >/dev/null && gio set "$HOME/Desktop/voyagerinterface.desktop" metadata::trusted true 2>/dev/null || true
fi
echo
echo "Done."
if [ "$RELOGIN" = yes ]; then
  echo "IMPORTANT: you were added to the 'plugdev' group. Log out and log in once"
  echo "(or restart the virtual machine) so that it works every time."
fi
echo "Start VoyagerInterface from the desktop icon or Applications menu > Accessories/Utility."
