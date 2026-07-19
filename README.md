# Optocam Zero

Optocam Zero is a Raspberry Pi Zero based compact digital camera made using off the shelf components. 

I designed Optocam Zero to have a very compact, carry everywhere and have fun sort of camera. As I was inspired by Kodak charmera and similar toy cameras, I wanted it to be feel playful, enjoyable and be intuitive to use. I also aimed to make it relatively easy to build so that others can also build one and have fun with it. That's why all the case parts are easily printable and for the electronics it uses off the shelf components that are easy to find.

![exploded view](assets/DSCG1663.jpg)

## Features
- Very compact and easy to carry in your pocket.
- Intuitive and simple camera interface and controls.
- Uses autofocus camera module.
- 8 photo filters included.
- Easy and fast image transfer through custom hotspot interface. Optimized both for mobile and desktop.
- Screen dimming when inactive to preserve battery.
- USB-C charging. Device can be used while charging.
- Interchangable battery.
- Off the shelf/ common components for the electronics.
- Fully 3D printed case parts (apart from fasteners).
- 3D printable TPU protective sleeve and lanyard design is available.
- **NEW** - GIF recording and playback.

<br>

## Specs
- **UPDATE** - Now boots in 5 seconds with the buildroot image.
- **UPDATE** - Consistent 25-30 fps camera preview on the screen.
- 2592x2592px Jpeg image capture. Image saves in the background while preview stays active.
- 240x240px 1.4 inch lcd display.
- Uses 14500 type li-ion battery.
- 70–80 minutes of use per charge.
- Dimensions: 51×71×18mm (excluding camera and screen bump)
  
<br>

## Whisplay HAT + PiSugar 3 Version

This repository also contains a Raspberry Pi OS port for a different hardware
combination. This version does **not** use the original 240×240 LCD, joystick,
shutter electronics, removable 14500 battery, or Buildroot image described in
the standard build guide.

### Hardware requirements

- Raspberry Pi Zero 2 W.
- Whisplay HAT with its 240×280 LCD, RGB LED, and single button.
- PiSugar 3 power board with its programmable button.
- Raspberry Pi Camera Module 3 (`imx708`) with autofocus.
- A compatible microSD card and the correct camera ribbon cable for Pi Zero.

The normal Whisplay desktop image can run the camera through
`whisplay-daemon`. A dedicated installation without the daemon is also
supported; in that mode Optocam owns the Whisplay display and button directly.
See the [Whisplay installation guide](software/whisplay/README.md) for both
installation modes.

### Interaction changes

The Whisplay HAT has only one camera button, while in daemon mode the PiSugar
button's single click remains the desktop Home action. The original joystick
controls are therefore remapped as follows:

| Context | Whisplay click | Whisplay double-click | Whisplay hold | PiSugar double-click | PiSugar hold, then release | PiSugar click |
| --- | --- | --- | --- | --- | --- | --- |
| Camera preview | Take a photo or start GIF capture | Select next white balance | Switch Photo/GIF mode | Select next filter | Open on-device gallery | Home (daemon) / toggle preview (standalone) |
| GIF recording | Cancel recording | — | — | — | — | Home (daemon) |
| Gallery | Return to camera preview | Show previous item | — | Show next item | Delete / confirm deletion | Home (daemon) |

PiSugar long-press events are reported only after the button is released, so
the corresponding gallery action occurs on release. Optocam listens for
PiSugar double-click and long-press events through the persistent TCP service
on `127.0.0.1:8423`; it does not inject commands into `button_shell` or change
the user's PiSugar button configuration.

Whisplay single-click is resolved after a short 350 ms double-click window so
that a double-click never takes an unintended photo. The daemon's Whisplay
quadruple-click exit gesture is disabled for Optocam; in daemon mode PiSugar
single-click is the only Home action.

Other differences from the original interface:

- The camera preview fills the complete 240×280 screen and is rotated 90°
  clockwise by default.
- Battery level is shown in smaller shadowed white text, for example `BAT 45`,
  above the ISO value in the lower-left preview HUD.
- The Whisplay RGB LED acknowledges shutter, save, filter, mode, gallery,
  recording, deletion, and error events.
- Filter, white-balance and gallery lists use next/previous gestures that wrap
  around, so every item remains reachable without a joystick.
- The original hotspot-mode and splash-screen gestures are not mapped. Captures
  are instead always available from the web gallery on port 80, for example
  `http://<raspberry-pi-address>/`.

<br>

## Sample Photos


<img src="assets/Optocamzero_45.jpg" width="49%"/> <img src="assets/Optocamzero_166.jpg" width="49%"/>
<img src="assets/Optocamzero_332.jpg" width="49%"/> <img src="assets/Optocamzero_333.jpg" width="49%"/>
<img src="assets/Optocamzero_200.jpg" width="49%"/> <img src="assets/Optocamzero_69.jpg" width="49%"/>
<img src="assets/Optocamzero_120.jpg" width="49%"/> <img src="assets/Optocamzero_133.jpg" width="49%"/>


<br>


## How to Build Optocam Zero?

Everything you need to build an Optocam Zero yourself is included in this repo.

All the 3d print files, Required parts list, and detailed step by step build guide can be found under the [hardware](hardware/) folder.

If you're considering building one, check the [BOM](hardware/BOM.md) to get an idea of the cost for required tools and parts.
Also, have a look at the [build guide](hardware/optocamzero-build-guide.pdf), it will give you a clear idea of what the build involves.

<br>

## Hardware

See the [hardware](hardware/) folder for:

- [Bill of materials](hardware/BOM.md).
- [Build guide](hardware/optocamzero-build-guide.pdf) (PDF).
- [Bambu Studio project files](hardware/print-ready/) ready to print for transparent PETG or PETG / PETG-CF.
- [Individual .stls](hardware/stls/) for camera parts.
- [CAD file](hardware/cad/optocamzero_V1.0.step) for customization.


<br>

## Software

See the [software](software/) folder for: 

- Optocam Zero code and installation guide.
- Camera controls information.
