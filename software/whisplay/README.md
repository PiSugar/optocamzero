# Whisplay HAT + PiSugar 3 port

This port targets the following hardware combination:

- Raspberry Pi Zero 2 W
- Whisplay HAT (240×280 LCD and one button)
- PiSugar 3
- Raspberry Pi Camera Module 3 (`imx708`, autofocus)

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

## Controls

| Context | Whisplay click | Whisplay hold | PiSugar double-click | PiSugar hold | PiSugar click (daemon) |
| --- | --- | --- | --- | --- | --- |
| Preview | Take photo / record GIF | Toggle Photo/GIF | Next filter | Open gallery | Home |
| GIF recording | Cancel recording | — | — | — | Home |
| Gallery | Close gallery | — | Next item | Delete / confirm delete | Home |

In daemon mode, PiSugar single-click remains owned by `whisplay-daemon` as the
system Home gesture. In standalone mode there is no desktop, so stop/restart the
camera with `systemctl`. Optocam only subscribes to the TCP event stream: it
does not read or change `button_shell`, `button_enable`, or `anti_mistouch`, so
existing PiSugar custom actions remain untouched.

## Screen and LED feedback

The complete 240×240 application view is rotated 90 degrees clockwise by
default. Set `OPTOCAM_DISPLAY_ROTATION` to `0`, `90`, `180`, or `270` to change
it. The top-centre battery icon follows the Whisplay desktop design: green at
70% or higher, amber at 35–69%, and red below 35%. It is refreshed from PiSugar
every five seconds and appears in preview, recording and gallery views.

The Whisplay RGB LED gives immediate feedback without delaying the shutter:

- green: camera ready or file saved
- white: shutter accepted
- blue/cyan: filter change or gallery navigation
- purple: Photo/GIF mode change
- red: GIF recording, deletion, or an error
- amber: waiting for delete confirmation

The original joystick-only controls for white-balance selection and hotspot
transfer mode are intentionally not mapped: there are not enough unambiguous
gestures, and delaying the single-click shutter to detect click sequences makes
the camera feel unresponsive. Captured files remain available over SSH/SFTP or
directly from the `photos` directory.

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
