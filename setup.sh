#!/usr/bin/env bash
# Zoom Vanish setup for Debian 13.
# Run from the folder that contains zoom_vanish.py, as your normal user:
#   bash setup.sh
# It asks for your password (sudo) only for the system packages and the
# virtual camera configuration.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/zoom-vanish"
VENV="$APP_DIR/venv"
VIDEO_NR=10

if [ "$(id -u)" -eq 0 ]; then
  echo "Run this as your normal user, not with sudo. It calls sudo itself when needed."
  exit 1
fi
if [ ! -f "$HERE/zoom_vanish.py" ]; then
  echo "zoom_vanish.py not found next to setup.sh."
  exit 1
fi

echo "==> 1/5 Installing system packages"
sudo apt update
sudo apt install -y v4l2loopback-dkms v4l2loopback-utils v4l-utils \
  "linux-headers-$(uname -r)" python3-venv libportaudio2 curl libnotify-bin

echo "==> 2/5 Configuring the virtual camera (/dev/video$VIDEO_NR, named \"Vanish Cam\")"
echo "options v4l2loopback devices=1 video_nr=$VIDEO_NR card_label=\"Vanish Cam\" exclusive_caps=1" \
  | sudo tee /etc/modprobe.d/zoom-vanish.conf >/dev/null
echo "v4l2loopback" | sudo tee /etc/modules-load.d/zoom-vanish.conf >/dev/null

echo "==> 3/5 Loading the virtual camera module"
if lsmod | grep -q '^v4l2loopback'; then
  echo "    v4l2loopback is already loaded (maybe by OBS). Leaving it as is."
  echo "    If /dev/video$VIDEO_NR is missing, reboot and it will load with the settings above."
elif ! sudo modprobe v4l2loopback; then
  echo
  echo "The module did not load. The most common reason is Secure Boot."
  echo "Check with:  mokutil --sb-state"
  echo "If it says 'SecureBoot enabled', register the DKMS signing key once:"
  echo "  sudo mokutil --import /var/lib/dkms/mok.pub"
  echo "  (choose a one-time password), reboot, pick 'Enroll MOK' on the blue"
  echo "  screen, enter that password, then run this script again."
  exit 1
fi

echo "==> 4/5 Creating a Python environment and installing libraries (a few minutes)"
mkdir -p "$APP_DIR/app" "$APP_DIR/models" "$HOME/.local/bin"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
# mediapipe (Google's hand tracking, for wave detection) brings its own OpenCV.
"$VENV/bin/pip" uninstall --quiet -y opencv-python-headless 2>/dev/null || true
"$VENV/bin/pip" install --quiet numpy pyvirtualcam sounddevice mediapipe

echo "    Downloading Google's hand gesture model (a few MB)"
MODEL_URL="https://storage.googleapis.com/mediapipe-models/gesture_recognizer/gesture_recognizer/float16/latest/gesture_recognizer.task"
if curl -fsSL "$MODEL_URL" -o "$APP_DIR/models/gesture_recognizer.task.part"; then
  mv "$APP_DIR/models/gesture_recognizer.task.part" "$APP_DIR/models/gesture_recognizer.task"
  echo "    model sha256: $(sha256sum "$APP_DIR/models/gesture_recognizer.task" | cut -d' ' -f1)"
else
  echo "    Could not download the hand model. Everything except wave detection will work."
  echo "    Run setup.sh again later to retry."
fi

echo "==> 5/5 Installing the program, menu entry and shortcuts"
install -m 755 "$HERE/zoom_vanish.py" "$APP_DIR/app/zoom_vanish.py"
cat > "$HOME/.local/bin/zoom-vanish" <<EOF
#!/bin/sh
exec "$VENV/bin/python" "$APP_DIR/app/zoom_vanish.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/zoom-vanish"
chmod 700 "$APP_DIR"

# Menu entry: starts the virtual camera together with Zoom, stops when Zoom quits.
mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/zoom-vanish.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Zoom (Vanish)
Comment=Zoom with the Vanish Cam: wave goodbye or press Ctrl+Alt+V to disappear
Exec=$HOME/.local/bin/zoom-vanish --with-zoom --wave
Icon=Zoom
Terminal=false
Categories=Network;VideoConference;
EOF

# Keyboard shortcuts (GNOME or XFCE): Ctrl+Alt+V vanish/appear, Ctrl+Alt+C capture room.
MK=org.gnome.settings-daemon.plugins.media-keys
add_shortcut() {  # id, name, command, binding
  local path="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/$1/"
  local list
  list="$(gsettings get $MK custom-keybindings)"
  if [[ "$list" != *"$path"* ]]; then
    list="$(python3 -c 'import ast, sys
l = ast.literal_eval(sys.argv[1].replace("@as ", ""))
l.append(sys.argv[2])
print(l)' "$list" "$path")"
    gsettings set $MK custom-keybindings "$list"
  fi
  gsettings set "$MK.custom-keybinding:$path" name "$2"
  gsettings set "$MK.custom-keybinding:$path" command "$3"
  gsettings set "$MK.custom-keybinding:$path" binding "$4"
}
if command -v gsettings >/dev/null && gsettings list-schemas 2>/dev/null | grep -qx "$MK"; then
  add_shortcut zoom-vanish-toggle "Zoom Vanish: vanish / appear" \
    "$HOME/.local/bin/zoom-vanish --toggle" "<Control><Alt>v"
  add_shortcut zoom-vanish-capture "Zoom Vanish: capture room" \
    "$HOME/.local/bin/zoom-vanish --capture" "<Control><Alt>c"
  echo "    Shortcuts set: Ctrl+Alt+V (vanish/appear), Ctrl+Alt+C (capture room)"
elif command -v xfconf-query >/dev/null; then
  # XFCE: <Primary> means Ctrl. Set the value, creating the entry if needed.
  for pair in "v|--toggle" "c|--capture"; do
    key="/commands/custom/<Primary><Alt>${pair%%|*}"
    cmd="$HOME/.local/bin/zoom-vanish ${pair##*|}"
    xfconf-query -c xfce4-keyboard-shortcuts -p "$key" -s "$cmd" 2>/dev/null \
      || xfconf-query -c xfce4-keyboard-shortcuts -p "$key" -n -t string -s "$cmd"
  done
  echo "    Shortcuts set (XFCE): Ctrl+Alt+V (vanish/appear), Ctrl+Alt+C (capture room)"
else
  echo "    Not GNOME or XFCE, so set these shortcuts by hand in your desktop's keyboard settings:"
  echo "      $HOME/.local/bin/zoom-vanish --toggle    (vanish/appear)"
  echo "      $HOME/.local/bin/zoom-vanish --capture   (capture room)"
fi

echo
echo "Cameras on this computer:"
v4l2-ctl --list-devices || true
echo
echo "Done. Open \"Zoom (Vanish)\" from your app menu."
echo "The first time, choose Vanish Cam in Zoom: Settings > Video > Camera."
