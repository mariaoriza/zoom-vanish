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
  "linux-headers-$(uname -r)" python3-venv libportaudio2 curl

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

echo "==> 5/5 Installing the program as 'zoom-vanish'"
install -m 755 "$HERE/zoom_vanish.py" "$APP_DIR/app/zoom_vanish.py"
cat > "$HOME/.local/bin/zoom-vanish" <<EOF
#!/bin/sh
exec "$VENV/bin/python" "$APP_DIR/app/zoom_vanish.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/zoom-vanish"

echo
echo "Cameras on this computer:"
v4l2-ctl --list-devices || true
echo
echo "Done. Start it with:  $HOME/.local/bin/zoom-vanish"
echo "(Just 'zoom-vanish' works after your next login, once ~/.local/bin is on your PATH.)"
