# Whisplay HAT + PiSugar 3 port

This port targets the following hardware combination:

- Raspberry Pi Zero 2 W
- Whisplay HAT (240×280 LCD and one button)
- PiSugar 3
- Raspberry Pi Camera Module 3 (`imx708`, autofocus)
- Compatible microSD card and the Camera Module ribbon cable for Pi Zero

It supports both Whisplay deployment models:

- **daemon**: uses the daemon's shared RGB565 framebuffer, LED, backlight and
  button APIs. Optocam never opens the Whisplay SPI/GPIO devices.
- **standalone**: used on dedicated-camera images without the daemon. Optocam
  owns the HAT through Whisplay's official `WhisplayBoard` runtime.

Both modes listen for custom-button events on PiSugar's persistent TCP stream at
`127.0.0.1:8423`; battery level continues to use the local PiSugar socket.

## Install

Clone this repository on the Pi. For the normal Whisplay desktop image, run:

```sh
cd optocamzero
sudo bash software/whisplay/install.sh --mode daemon
```

This registers **Optocam Zero**, installs `optocamzero.service`, and launches it
through `whisplay-daemon` at every boot. PiSugar single-click returns to the
desktop. To open it again without rebooting, select Optocam on the desktop and
hold the HAT button, or run:

```sh
sudo systemctl start optocamzero.service
```

This version requires a Whisplay daemon that supports
`exit_gesture=none`. This disables quadruple-click exit for Optocam so Whisplay
click, double-click, and hold remain available to the camera. PiSugar
single-click remains Home.

For a dedicated camera without `whisplay-daemon`, run:

```sh
sudo bash software/whisplay/install.sh --mode standalone
```

Standalone installation copies Whisplay's `runtime/whisplay.py`, disables the
daemon to prevent SPI/GPIO contention, and starts Optocam directly at boot.
Running the installer again with `--mode daemon` switches back and re-enables
the daemon.

No boot overlays are changed. Photos and GIFs are stored under
`~/optocamzero-whisplay/photos` in both modes.

The installer also enables a web gallery on port 80. Open
`http://<raspberry-pi-address>/` (for the test device,
`http://192.168.100.155/`) to browse, download, or delete captures. The gallery
runs independently of the camera UI and remains available in both startup
modes.

## Controls

| Context | Whisplay click | Whisplay double-click | Whisplay hold | PiSugar double-click | PiSugar hold, then release | PiSugar click |
| --- | --- | --- | --- | --- | --- | --- |
| Preview | Take photo / record GIF | Next white balance | Cycle Photo/GIF/Moment¹ | Next filter | Open gallery | Home (daemon) / preview on-off (standalone) |
| GIF recording | Cancel recording | — | — | — | — | Home (daemon) |
| Moment recording | Hold to record, release to save | — | — | — | — | Home (daemon) |
| Gallery | Close gallery | Previous item | — | Next item | Delete / confirm delete | Home (daemon) |

¹ Moment mode appears only when the ALSA capture device identifies itself as
`whisplay-sound`. After the photo is taken, hold the Whisplay button to record
up to 10 seconds. Recordings shorter than one second are discarded. The photo
and WAV file share the same capture number and are managed as a pair.

In daemon mode, PiSugar single-click remains owned by `whisplay-daemon` as the
system Home gesture. In standalone mode there is no desktop, so stop/restart the
camera with `systemctl`. Optocam only subscribes to the TCP event stream: it
does not read or change `button_shell`, `button_enable`, or `anti_mistouch`, so
existing PiSugar custom actions remain untouched. PiSugar reports a long press
only after the button is released, so its gallery or deletion action also runs
on release. Whisplay single-click waits for a 350 ms double-click window before
firing the shutter, preventing an AWB double-click from taking a photo first.

## Screen and LED feedback

The camera preview fills the complete 240×280 panel and is rotated 90 degrees
clockwise by default. Set `OPTOCAM_DISPLAY_ROTATION` to `0`, `90`, `180`, or
`270` to change it. Battery level is refreshed from PiSugar every five seconds
and rendered as smaller shadowed white text such as `BAT 45` above, and
left-aligned with, the ISO value in the lower-left preview HUD.

The Whisplay RGB LED gives immediate feedback without delaying the shutter:

- green: camera ready or file saved
- white: shutter accepted
- blue/cyan: filter change or gallery navigation
- purple: capture mode change
- red: GIF recording, deletion, or an error
- amber: waiting for delete confirmation

PHOTO and MOMENT captures also play a short shutter sound through the detected
`whisplay-sound` card. GIF recording remains silent.

Filter and white-balance selection wrap around, while Whisplay/PiSugar provide
previous/next gallery navigation, so every functional choice remains reachable.
The original hotspot-mode and splash-screen gestures are intentionally omitted:
the web gallery is always available, and the splash trigger is cosmetic.
Moments show a speaker badge and play once when selected. Web downloads
and deletions keep each JPG/WAV pair together.

## Diagnostics

In daemon mode, the daemon captures app output in:

```sh
tail -f ~/.whisplay-daemon/daemon-app.log
```

Check Camera Module 3 detection with:

```sh
rpicam-hello --list-cameras
```

The result should contain `imx708`. Check the selected startup mode with:

```sh
systemctl --no-pager --full status optocamzero.service
```
