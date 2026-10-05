#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Must run as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run with sudo: sudo bash install.sh${NC}"
    exit 1
fi

INSTALL_USER=${SUDO_USER:-pi}
INSTALL_HOME=$(getent passwd "$INSTALL_USER" | cut -d: -f6)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  OptoCamZero Installer${NC}"
echo -e "${GREEN}========================================${NC}"
echo "User:     $INSTALL_USER"
echo "Home:     $INSTALL_HOME"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo -e "${YELLOW}[1/8] Installing system packages...${NC}"
apt-get update -q
apt-get install -y hostapd dnsmasq pigpio python3-pip python3-flask \
    python3-numpy python3-pil python3-picamera2

# ── 2. Python packages (pip) ──────────────────────────────────────────────────
echo -e "${YELLOW}[2/8] Installing Python packages...${NC}"
pip3 install spidev pigpio --break-system-packages 2>/dev/null || \
pip3 install spidev pigpio

# ── 3. Copy scripts ───────────────────────────────────────────────────────────
echo -e "${YELLOW}[3/8] Copying scripts and assets...${NC}"
cp "$SCRIPT_DIR/scripts/optocamzero.py"  "$INSTALL_HOME/optocamzero.py"
cp "$SCRIPT_DIR/scripts/gallery_server.py" "$INSTALL_HOME/gallery_server.py"
cp "$SCRIPT_DIR/scripts/magic_service.py" "$INSTALL_HOME/magic_service.py"
if [ ! -f "$INSTALL_HOME/.env" ]; then
    install -m 0600 "$SCRIPT_DIR/.env.example" "$INSTALL_HOME/.env"
fi

cp "$SCRIPT_DIR/assets/cmunvt.ttf"       "$INSTALL_HOME/cmunvt.ttf"
cp "$SCRIPT_DIR/assets/optocamlogo.svg"  "$INSTALL_HOME/optocamlogo.svg"
cp "$SCRIPT_DIR/assets/splash.raw"       "$INSTALL_HOME/splash.raw"

# Replace hardcoded paths with actual user home
sed -i "s|/home/dkumkum|$INSTALL_HOME|g" "$INSTALL_HOME/optocamzero.py"
sed -i "s|/home/dkumkum|$INSTALL_HOME|g" "$INSTALL_HOME/gallery_server.py"

# Create photos directory
mkdir -p "$INSTALL_HOME/photos"
chown -R "$INSTALL_USER:$INSTALL_USER" \
    "$INSTALL_HOME/optocamzero.py" \
    "$INSTALL_HOME/gallery_server.py" \
    "$INSTALL_HOME/magic_service.py" \
    "$INSTALL_HOME/.env" \
    "$INSTALL_HOME/cmunvt.ttf" \
    "$INSTALL_HOME/optocamlogo.svg" \
    "$INSTALL_HOME/splash.raw" \
    "$INSTALL_HOME/photos"

# ── 4. Service files ──────────────────────────────────────────────────────────
echo -e "${YELLOW}[4/8] Installing systemd services...${NC}"
for svc in camera-auto optocam-hotspot optocam-gallery uap0; do
    cp "$SCRIPT_DIR/services/$svc.service" "/etc/systemd/system/$svc.service"
    sed -i "s|/home/dkumkum|$INSTALL_HOME|g" "/etc/systemd/system/$svc.service"
    sed -i "s|dkumkum|$INSTALL_USER|g"       "/etc/systemd/system/$svc.service"
done

# ── 5. Hotspot config ─────────────────────────────────────────────────────────
echo -e "${YELLOW}[5/8] Configuring hotspot...${NC}"
cp "$SCRIPT_DIR/services/hostapd.conf" "/etc/hostapd/hostapd.conf"
cp "$SCRIPT_DIR/services/dnsmasq-optocam.conf" "/etc/dnsmasq.d/optocam.conf"

# Point hostapd to its config file (masked by default on Pi OS)
systemctl unmask hostapd
sed -i 's|#DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

# Tell NetworkManager to leave uap0 alone
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/optocam-unmanaged.conf << 'EOF'
[keyfile]
unmanaged-devices=interface-name:uap0
EOF

# ── 6. Boot config ────────────────────────────────────────────────────────────
echo -e "${YELLOW}[6/8] Configuring /boot/firmware/config.txt...${NC}"
CONFIG=/boot/firmware/config.txt

# Fix conflicting default settings
sed -i 's/^camera_auto_detect=1/camera_auto_detect=0/' "$CONFIG"
sed -i 's/^display_auto_detect=1/display_auto_detect=0/' "$CONFIG"
sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CONFIG"

# Helper: append line only if not already present
add_if_missing() {
    grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"
}

add_if_missing "dtparam=spi=on"
add_if_missing "dtparam=i2c_arm=off"
add_if_missing "dtoverlay=imx708"
add_if_missing "dtoverlay=st7789,cs=1,dc=9,rst=25,bl=13,width=240,height=240"
add_if_missing "arm_boost=1"
add_if_missing "arm_freq=1200"
add_if_missing "over_voltage=2"
add_if_missing "initial_turbo=30"
add_if_missing "boot_delay=0"
add_if_missing "disable_splash=1"
add_if_missing "auto_initramfs=0"
add_if_missing "dtoverlay=disable-bt"
add_if_missing "dtoverlay=spi1-3cs"
add_if_missing "disable_fw_kms_setup=1"
add_if_missing "disable_overscan=1"

# NOTE: do NOT remove vc4-kms-v3d (it ships in the base Pi OS config.txt). It sets
# the CMA pool to 256MB; without it the pool drops to 64MB and full-resolution
# capture fails with "Cannot allocate memory" — preview still works, which hides the
# bug. The SPI display is script-driven and doesn't need KMS, but the camera's
# capture DMA buffers are allocated from the CMA pool that vc4-kms-v3d reserves.

# OPTIONAL (~1s faster boot): overclock the SD bus 50->100MHz. Speeds every
# Linux-phase read (numpy import, module loading). Verified stable on the dev unit's
# A2 card, but it is card/board-specific — uncomment, then check `dmesg | grep mmc`
# for errors/CRC after a reboot before relying on it.
# add_if_missing "dtparam=sd_overclock=100"

# Boot parameters
CMDLINE=/boot/firmware/cmdline.txt
if ! grep -q "spidev.bufsiz" "$CMDLINE"; then
    sed -i 's/$/ spidev.bufsiz=131072/' "$CMDLINE"
fi
if ! grep -q "quiet" "$CMDLINE"; then
    sed -i 's/$/ quiet loglevel=3 logo.nologo vt.global_cursor_default=0/' "$CMDLINE"
fi
if ! grep -q "consoleblank" "$CMDLINE"; then
    sed -i 's/$/ consoleblank=0/' "$CMDLINE"
fi

# ── 6b. Boot-speed system tuning ──────────────────────────────────────────────
echo -e "${YELLOW}[6b/8] Applying boot-speed optimizations...${NC}"

# Disable the USB host controller — WiFi is SDIO and nothing else uses USB, so the
# dwc_otg init (~0.9s of kernel time) is pure overhead. No stock overlay exists, so
# compile a tiny one that marks the USB node disabled. (Pi Zero 2 W / BCM2710.)
if ! command -v dtc >/dev/null 2>&1; then
    apt-get install -y device-tree-compiler
fi
cat > /tmp/disable-usb.dts << 'DTS'
/dts-v1/;
/plugin/;
/ {
    fragment@0 {
        target-path = "/soc/usb@7e980000";
        __overlay__ {
            status = "disabled";
        };
    };
};
DTS
if dtc -@ -I dts -O dtb -o /boot/firmware/overlays/disable-usb.dtbo /tmp/disable-usb.dts 2>/dev/null; then
    add_if_missing "dtoverlay=disable-usb"
fi

# Load the camera stack at sysinit instead of waiting for udev coldplug (~7.5s), so
# the camera is probed by the time the early-starting script opens it. modprobe
# pulls in dependencies; module names absent on a given kernel are simply skipped.
cat > /etc/modules-load.d/optocam-camera.conf << 'EOF'
i2c-bcm2835
bcm2835-isp
bcm2835-unicam-legacy
dw9807-vcm
imx708
EOF

# Blacklist the bcm2835 audio module (audio is off anyway). NOTE: the HDMI-audio
# snd_soc modules get pulled in as hard DEPENDENCIES of vc4-kms-v3d and load
# regardless of this blacklist (and vc4-kms-v3d must stay — see CMA note above), so
# the saving is small. It costs nothing, so we keep it to avoid the snd_bcm2835 load.
cat > /etc/modprobe.d/optocam-blacklist.conf << 'EOF'
blacklist snd_bcm2835
blacklist snd_soc_hdmi_codec
blacklist snd_soc_core
blacklist snd_pcm
blacklist snd_pcm_dmaengine
blacklist snd_compress
blacklist snd_timer
blacklist snd
EOF

# Defer the /boot/firmware (FAT) mount so its fsck+mount stops gating sysinit
# (~0.8s). The firmware already read config.txt before Linux; nothing at runtime
# needs it mounted, and x-systemd.automount mounts it on demand (e.g. apt updates).
if ! grep -q 'x-systemd.automount' /etc/fstab; then
    sed -i -E 's#^(PARTUUID=\S+\s+/boot/firmware\s+vfat\s+)defaults(\s+)[0-9](\s+)[0-9]#\1defaults,nofail,x-systemd.automount\20\30#' /etc/fstab
fi

# ── 7. Enable services ────────────────────────────────────────────────────────
echo -e "${YELLOW}[7/8] Enabling services...${NC}"
systemctl daemon-reload
systemctl enable pigpiod
systemctl enable uap0
systemctl enable camera-auto
systemctl disable optocam-hotspot 2>/dev/null || true
systemctl disable optocam-gallery 2>/dev/null || true
systemctl disable hostapd 2>/dev/null || true
systemctl disable dnsmasq 2>/dev/null || true
systemctl disable ModemManager 2>/dev/null || true
systemctl disable NetworkManager-wait-online.service 2>/dev/null || true
systemctl disable avahi-daemon 2>/dev/null || true
systemctl disable e2scrub_reap 2>/dev/null || true
systemctl disable dphys-swapfile 2>/dev/null || true
systemctl disable bluetooth 2>/dev/null || true
systemctl disable hciuart 2>/dev/null || true
systemctl disable triggerhappy 2>/dev/null || true

# ── 8. Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo "Camera will start automatically on next boot."
echo "Hotspot: connect to 'Optocam Zero' — password: 0026opto"
echo "Gallery: open 192.168.4.1 in a browser while connected to the hotspot"
echo ""
echo -e "${YELLOW}Rebooting in 5 seconds... (Ctrl+C to cancel)${NC}"
sleep 5
# Flush config.txt / fstab writes to disk before rebooting. With the deferred
# /boot/firmware automount, an unsynced config.txt write can fail to persist
# across the reboot, so force it explicitly.
sync
reboot
