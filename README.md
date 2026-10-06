# Whisplay Optocam Zero

Whisplay Optocam Zero is an open-source, pocket-sized DIY digital camera for
the Raspberry Pi Zero family.

This repository was originally forked from
[Doruk Kumkumoğlu's Optocam Zero](https://github.com/dorukkumkumoglu/optocamzero)
and is now developed as an independent continuation. It preserves the original
3D-printable hardware design and Buildroot firmware while adding a Raspberry Pi
OS port for the Whisplay HAT, PiSugar 3 Air, and Raspberry Pi Camera Module 3.

Inspired by the Kodak Charmera and other toy cameras, the project emphasizes a
playful, intuitive shooting experience. The enclosure is fully printable and
the electronics use readily available, off-the-shelf components.

![Whisplay Optocam Zero finished build](assets/whisplay-optocamzero.webp)

## Features
- Photo, animated GIF, Moment audio, and configurable Magic capture modes.
- Autofocus still capture with eight built-in filters and up to five custom presets.
- Full-screen Whisplay preview with HUD, battery level, and RGB LED feedback.
- On-device gallery plus a mobile- and desktop-friendly web gallery.
- Raspberry Pi OS support in both `whisplay-daemon` and standalone modes.
- Dedicated 3D-printable enclosure for the Whisplay HAT and PiSugar 3 Air.


<br>

## Specs
- **Computer:** Raspberry Pi Zero 2 W.
- **Display:** Whisplay HAT with a 240×280 LCD, RGB LED, and camera button.
- **Power and controls:** PiSugar 3 Air with battery monitoring, custom button,
  and power-button Home control in daemon mode.
- **Camera:** Raspberry Pi Camera Module 3 (`imx708`) with continuous autofocus.
- **Still images:** 2592×2592 JPEG with background saving.
- **Animated GIFs:** 640×640 capture stream.
- **Audio:** up to 10-second Moment recordings when `whisplay-sound` is available.
- **Software:** Raspberry Pi OS, with daemon and standalone deployment modes.
- **Storage and access:** captures stored on microSD and available through the
  on-device gallery or web gallery on port 80.
- **Enclosure:** [PiSugar 3 Air + Whisplay Optocam printable files](https://github.com/PiSugar/suit-cases/tree/main/pisugar3air-whisplay-optocam).

<br>

## Whisplay HAT + PiSugar 3 Air Port

The Whisplay port runs on Raspberry Pi OS and targets a different hardware
combination. It does **not** use the original 240×240 LCD, joystick,
shutter electronics, removable 14500 battery, or Buildroot image described in
the standard build guide.

### Hardware requirements

- Raspberry Pi Zero 2 W.
- Whisplay HAT with its 240×280 LCD, RGB LED, and single button.
- PiSugar 3 Air power board with its programmable button.
- Raspberry Pi Camera Module 3 (`imx708`) with autofocus.
- A compatible microSD card and the correct camera ribbon cable for Pi Zero.
- [3D-printable Whisplay + PiSugar 3 Air enclosure files](https://github.com/PiSugar/suit-cases/tree/main/pisugar3air-whisplay-optocam).

The normal Whisplay desktop image can run the camera through
`whisplay-daemon`. A dedicated installation without the daemon is also
supported; in that mode Optocam owns the Whisplay display and button directly.
See the [Whisplay installation guide](software/whisplay/README.md) for both
installation modes.

### Interaction changes

The Whisplay HAT has one camera button. On PiSugar 3 Air, the custom button
handles filter and gallery gestures, while the power button returns to the
Whisplay desktop in daemon mode. The original joystick controls are remapped as
follows:

| Context | Whisplay click | Whisplay double-click | Whisplay hold | PiSugar custom double-click | PiSugar custom hold, then release | PiSugar custom click | PiSugar power button |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Camera preview | Take a photo or start GIF capture | Select next white balance | Cycle Photo/GIF/Moment¹ | Select next filter | Open on-device gallery | — (daemon) / toggle preview (standalone) | Home (daemon) |
| GIF recording | Cancel recording | — | — | — | — | — | Home (daemon) |
| Moment recording | Hold to record, release to save | — | — | — | — | — | Home (daemon) |
| Gallery | Return to camera preview | Show previous item | — | Show next item | Delete / confirm deletion | — | Home (daemon) |

¹ Moment mode is available on Whisplay hardware only when the detected ALSA
card is `whisplay-sound`. Recordings are capped at 10 seconds and discarded
when shorter than one second.

PiSugar custom-button long-press events are reported only after the button is released, so
the corresponding gallery action occurs on release. Optocam listens for
custom-button double-click and long-press events through the persistent TCP service
on `127.0.0.1:8423`; it does not inject commands into `button_shell` or change
the user's PiSugar button configuration.

Whisplay single-click is resolved after a short 350 ms double-click window so
that a double-click never takes an unintended photo. The daemon's Whisplay
quadruple-click exit gesture is disabled for Optocam. In daemon mode, the
PiSugar power button is the Home action.

Other differences from the original interface:

- The camera preview fills the complete 240×280 screen and is rotated 90°
  clockwise by default.
- Battery level is shown in smaller shadowed white text, for example `BAT 45`,
  above the ISO value in the lower-left preview HUD.
- The Whisplay RGB LED acknowledges shutter, save, filter, mode, gallery,
  recording, deletion, and error events.
- PHOTO and MOMENT captures play a short shutter sound through `whisplay-sound`.
- Filter, white-balance and gallery lists use next/previous gestures that wrap
  around, so every item remains reachable without a joystick.
- The original hotspot-mode and splash-screen gestures are not mapped. Captures
  are instead always available from the web gallery on port 80, for example
  `http://<raspberry-pi-address>/`.
- Moments carry a speaker badge in both galleries. Opening one plays its
  audio once; downloading or deleting it includes the paired JPG and WAV.

<br>

## Sample Photos


<img src="assets/Optocamzero_45.jpg" width="49%"/> <img src="assets/Optocamzero_166.jpg" width="49%"/>
<img src="assets/Optocamzero_332.jpg" width="49%"/> <img src="assets/Optocamzero_333.jpg" width="49%"/>
<img src="assets/Optocamzero_200.jpg" width="49%"/> <img src="assets/Optocamzero_69.jpg" width="49%"/>
<img src="assets/Optocamzero_120.jpg" width="49%"/> <img src="assets/Optocamzero_133.jpg" width="49%"/>


<br>


## Build the Camera

The Whisplay HAT and PiSugar 3 Air version uses its own
[3D-printable enclosure](https://github.com/PiSugar/suit-cases/tree/main/pisugar3air-whisplay-optocam)
and the [Whisplay installation guide](software/whisplay/README.md).

The files under [hardware](hardware/) belong to the upstream **original
Optocam Zero design**. They are retained for reference and are not the hardware
design for the Whisplay HAT + PiSugar 3 Air version.

<br>

## Original Optocam Zero Hardware (Upstream)

> **Important:** Every item below is for the original Optocam Zero hardware,
> not the Whisplay HAT + PiSugar 3 Air build.

- [Original bill of materials](hardware/BOM.md).
- [Original build guide](hardware/optocamzero-build-guide.pdf) (PDF).
- [Original Bambu Studio project files](hardware/print-ready/) for transparent PETG or PETG / PETG-CF.
- [Original STL files](hardware/stls/) for camera parts.
- [Original CAD model](hardware/cad/optocamzero_V1.0.step) for customization.


<br>

## Software

See the [software](software/) folder for: 

- Optocam Zero code and installation guide.
- Camera controls information.
