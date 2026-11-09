# Caihong Toolbox

English | [简体中文](README.zh-CN.md)

Caihong Toolbox is the graphical settings app for the OnePlus Pad Pro
(`caihong`) running Caihong Linux. It brings device settings and hardware
checks into one touch-friendly window, and follows the local Plasma theme,
font size and accent color.

It is installed by default in the
[Caihong Linux rootfs](https://github.com/Osilvfe/caihong-linux-rootfs).

## Features

### Device overview

- Model, kernel, battery level and temperature, adapter state, keyboard and pen.
- Unknown readings stay empty instead of being guessed.

### Charging and battery

- Switch between normal charging and bypass power, which stops battery charging
  while keeping the adapter connected.
- Set experimental charge start and stop thresholds for the current session.
- View firmware-reported health, cycle count, capacity, and estimated input and
  battery power.
- See the detected charging protocol (USB PD, PPS, VOOC/SVOOC where known) and
  PPS current limits.

### Performance and temperature

- CPU usage, per-core current and maximum frequency, governor, GPU frequency,
  load average and uptime.
- Memory, swap and root filesystem usage.
- Highest temperature for CPU, GPU, memory, NPU, video, camera, battery, power
  management and modem groups. The full zone list is available in diagnostics.

### Keyboard and touchpad

- Choose standard F1-F12 keys or the printed media functions for the top row.
- Bluetooth F4 compatibility is enabled automatically and has no separate switch.
- Enable or disable the touchpad when needed.

### Pen

- Turn writing feedback on or off and choose the original tool and intensity.
- Test short vibration and the pen-body touch area.
- Observe pen-body double-tap and slide events for diagnostics. Double-tap
  behavior can be set to count only, vibrate, or toggle writing feedback.

### Hardware tests

- Display color card and drawing canvas for touch and pen input.
- Keyboard and touchpad, speaker, microphone, camera, audio/video nodes,
  network and Bluetooth, battery, storage, and individual sensors.
- Every test starts and stops manually; nothing keeps running in the background.

### Services and diagnostics

- Review and enable or disable Caihong services and timers.
- Pen and keyboard entries stay hidden when those accessories are not present.
- Copy the current diagnostic snapshot when asking for support.

## Opening

Open **Caihong Toolbox** from the application menu, or run:

```sh
caihong-toolbox
```

The window runs as your normal user. Charging, keyboard, pen and service
changes go through a small privileged backend and ask for administrator
approval only when needed.

To preview the interface without touching hardware:

```sh
caihong-toolbox --demo
```

## Installation

On Caihong Linux the app is already installed. For a manual Arch Linux ARM
installation:

```sh
sudo pacman -S --needed pyside6 python-gobject polkit
sudo make install
sudo systemctl daemon-reload
sudo systemctl reload dbus
```

Do not restart the system D-Bus. The backend starts on demand and is not
enabled as a boot service.

## Good to know

- Changes are applied only after you press an action button and approve the
  administrator prompt.
- The app does not flash kernels, modules or boot images.
- Charge thresholds are experimental and apply only to the current session.
- Tests become view-only when the matching kernel interface or accessory is
  missing.
- Microphone recordings are temporary and deleted when the test is closed.
- Diagnostic data can contain device addresses; review it before sharing.

## Development

Python 3.11+, PySide6 6.8+ and PyGObject are required. Tests also need pytest.

```sh
make check
python3 -m caihong_toolbox --demo
```
