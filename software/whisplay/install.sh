#!/bin/bash
set -euo pipefail

MODE=daemon
usage() {
    echo "Usage: sudo bash software/whisplay/install.sh [--mode daemon|standalone]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --mode)
            MODE=${2:-}
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 2
            ;;
    esac
done
if [ "$MODE" != daemon ] && [ "$MODE" != standalone ]; then
    usage
    exit 2
fi

if [ "${EUID}" -ne 0 ]; then
    echo "Run this installer with sudo: sudo bash software/whisplay/install.sh"
    exit 1
fi

INSTALL_USER=${SUDO_USER:-pi}
INSTALL_HOME=$(getent passwd "$INSTALL_USER" | cut -d: -f6)
INSTALL_GROUP=$(id -gn "$INSTALL_USER")
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PYTHON_SOURCE="$SCRIPT_DIR/../python-legacy"
APP_HOME="$INSTALL_HOME/optocamzero-whisplay"
WHISPLAY_RUNTIME_SOURCE=${WHISPLAY_RUNTIME_SOURCE:-"$INSTALL_HOME/Whisplay/runtime/whisplay.py"}

if [ -z "$INSTALL_HOME" ] || [ ! -d "$INSTALL_HOME" ]; then
    echo "Cannot resolve the home directory for user: $INSTALL_USER"
    exit 1
fi
if ! command -v rpicam-hello >/dev/null; then
    echo "rpicam-apps is required before installing Optocam Zero."
    exit 1
fi

echo "Installing camera dependencies..."
apt-get update -q
apt-get install -y --no-install-recommends \
    python3-picamera2 python3-pil python3-numpy python3-libgpiod python3-spidev \
    python3-flask alsa-utils

echo "Installing Optocam Zero to $APP_HOME..."
install -d -m 0755 "$APP_HOME" "$APP_HOME/photos"
install -m 0755 "$PYTHON_SOURCE/scripts/optocamzero.py" "$APP_HOME/optocamzero.py"
install -m 0755 "$PYTHON_SOURCE/scripts/gallery_server.py" "$APP_HOME/gallery_server.py"
install -m 0644 "$PYTHON_SOURCE/scripts/magic_service.py" "$APP_HOME/magic_service.py"
install -m 0644 "$PYTHON_SOURCE/scripts/whisplay_adapter.py" "$APP_HOME/whisplay_adapter.py"
install -m 0644 "$PYTHON_SOURCE/assets/cmunvt.ttf" "$APP_HOME/cmunvt.ttf"
install -m 0644 "$PYTHON_SOURCE/assets/splash.raw" "$APP_HOME/splash.raw"
install -m 0644 "$PYTHON_SOURCE/assets/optocamlogo.svg" "$APP_HOME/optocamlogo.svg"
install -m 0755 "$SCRIPT_DIR/run_optocamzero.sh" "$APP_HOME/run_optocamzero.sh"
install -m 0755 "$SCRIPT_DIR/register_app.py" "$APP_HOME/register_app.py"
install -m 0755 "$SCRIPT_DIR/autostart.py" "$APP_HOME/autostart.py"
if [ ! -f "$APP_HOME/.env" ]; then
    install -m 0600 "$SCRIPT_DIR/.env.example" "$APP_HOME/.env"
fi
if [ -f "$WHISPLAY_RUNTIME_SOURCE" ]; then
    install -m 0644 "$WHISPLAY_RUNTIME_SOURCE" "$APP_HOME/whisplay.py"
elif [ "$MODE" = standalone ]; then
    echo "Standalone mode requires Whisplay runtime/whisplay.py."
    echo "Set WHISPLAY_RUNTIME_SOURCE to its path and run the installer again."
    exit 1
fi
chown -R "$INSTALL_USER:$INSTALL_GROUP" "$APP_HOME"

# Stop a previously installed variant before replacing its unit file.
systemctl disable --now optocamzero.service 2>/dev/null || true
systemctl disable --now optocam-gallery.service 2>/dev/null || true

SERVICE_TEMPLATE="$SCRIPT_DIR/services/optocamzero-$MODE.service.in"
sed \
    -e "s|@USER@|$INSTALL_USER|g" \
    -e "s|@GROUP@|$INSTALL_GROUP|g" \
    -e "s|@USER_HOME@|$INSTALL_HOME|g" \
    -e "s|@APP_HOME@|$APP_HOME|g" \
    "$SERVICE_TEMPLATE" > /etc/systemd/system/optocamzero.service

sed \
    -e "s|@USER@|$INSTALL_USER|g" \
    -e "s|@GROUP@|$INSTALL_GROUP|g" \
    -e "s|@USER_HOME@|$INSTALL_HOME|g" \
    -e "s|@APP_HOME@|$APP_HOME|g" \
    "$SCRIPT_DIR/services/optocam-gallery.service.in" \
    > /etc/systemd/system/optocam-gallery.service

if [ "$MODE" = daemon ]; then
    if ! systemctl cat whisplay-daemon.service >/dev/null 2>&1; then
        echo "Daemon mode requires whisplay-daemon.service."
        exit 1
    fi
    systemctl enable --now whisplay-daemon.service
    wait_count=0
    while [ ! -S /tmp/whisplay-daemon.sock ] && [ "$wait_count" -lt 30 ]; do
        sleep 1
        wait_count=$((wait_count + 1))
    done
    if [ ! -S /tmp/whisplay-daemon.sock ]; then
        echo "Timed out waiting for whisplay-daemon."
        exit 1
    fi
    echo "Registering with whisplay-daemon..."
    runuser -u "$INSTALL_USER" -- /usr/bin/python3 "$APP_HOME/register_app.py" --home "$APP_HOME"
else
    # Direct hardware ownership and the daemon are mutually exclusive.
    systemctl disable --now whisplay-daemon.service 2>/dev/null || true
fi

systemctl daemon-reload
systemctl enable --now optocam-gallery.service
systemctl enable --now optocamzero.service

echo
echo "Optocam Zero is installed in $MODE mode and enabled at boot."
if [ "$MODE" = daemon ]; then
    echo "PiSugar single-click returns to the Whisplay desktop."
else
    echo "whisplay-daemon is disabled; Optocam owns the HAT directly."
fi
echo "Photos are stored in: $APP_HOME/photos"
echo "Web gallery: http://$(hostname -I | awk '{print $1}')/"
