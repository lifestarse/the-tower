# The Tower Automation

## Environment
- Python 3.10
- BlueStacks 5, portrait (resolution is auto-detected — tested at 900x1600)
- Android Debug Bridge (from platform-tools_r32.0.0-windows)

### Connecting to BlueStacks
BlueStacks is a *networked* emulator reached over TCP, not a stock AVD:
1. BlueStacks → Settings → **Advanced → Android Debug Bridge** → enable it. It
   shows a port (usually `127.0.0.1:5555`).
2. `adb connect 127.0.0.1:5555`, then confirm with `adb devices`.
3. Set that as `device_id` in `scenarios.json` (already defaulted to
   `127.0.0.1:5555`). `android_device.py` auto-runs `adb connect` for any
   `host:port` device, and never `kill-server`s (which would drop the link).

## Python packages
- pure-python-adb 0.3.0.dev0
- Pillow >= 9.0.1
- opencv-python >= 4.5
- numpy >= 1.21

## ADB ?
- All python automation scripts in this project use Android Debug Bridge(ADB) which is officially provided by Google.
- By using ADB, we can capture screenshot, check running activity, send touch input, etc...

## Prerequisites
- Set up python environment
- Install BlueStacks 5 and set display settings to 'portrait', '720x1280'

# Setup ADB
- There are two ways of running adb on Windows.
1. (LOCAL) Place adb.exe where .py script exists
   1. Download platform tools from [official Android Developer website](https://developer.android.com/studio/releases/platform-tools)
   2. Unzip two files (adb.exe, AdbWinApi.dll) to target path (where you're going to place .py files)
2. (GLOBAL) Set environment variable of your system to run adb.exe globally
   1. Download platform tools from [official Android Developer website](https://developer.android.com/studio/releases/platform-tools)
   2. Unzip two files (adb.exe, AdbWinApi.dll) to your desired path (e.g. 'C:/adb/')
   3. Add your desired path to environment variables' Path

# How to run the script
1. Clone or download the source as ZIP and unzip it.
2. Install necessary packages
```shell
pip install -r requirements.txt
```
3. Run your 'The Tower' app on your BlueStacks 5.
4. Run state checking script to see everything is okay.
```shell
python run_state_check.py
```
5. Run automation script and have fun.
```shell
python run_automation.py
```

## Image recognition (template matching)

The original automation classifies the whole screen into a *state* (`state.py`,
colour histograms on fixed rectangles) and then taps hard-coded coordinates.
That breaks as soon as a button moves or the resolution changes.

`image_recognition.py` adds real template matching (OpenCV `TM_CCOEFF_NORMED`):
give it a small PNG crop of a button and it **locates that button anywhere on
the screenshot** and taps its actual center. It supports multi-scale search (so
templates cropped at one resolution still match at another) and a confidence
threshold.

```python
from android_device import AndroidDevice
from image_recognition import TemplateLibrary, locate_and_tap, multi_scale

device = AndroidDevice('emulator-5554')
lib = TemplateLibrary('templates')            # loads templates/*.png once
screen = device.capture()
if locate_and_tap(device, screen, lib['retry'], scales=multi_scale()):
    print('tapped retry')
```

### Workflow
1. Grab a screenshot: `python capture_screen.py screen.png`
2. Crop a button into `templates/`: `python crop_template.py screen.png retry 180 1000 100 30`
3. See what the matcher finds (writes `matches.png`): `python run_template_demo.py`
4. Run the template-driven loop: `python run_smart_automation.py`

See `templates/README.md` for naming and cropping tips. The matching logic has a
built-in self-test that needs no device or game:

```shell
python image_recognition.py      # -> "image_recognition self-test OK ..."
```

## Scenarios & UI

A **scenario** pairs a template with settings and says "whenever this appears,
tap it". Each scenario has its **own check interval** (default 1s), match
threshold, tap cooldown, and enabled flag. Scenarios live in `scenarios.json`
and every check/tap is printed to the console.

Ships with one scenario: tap the **CLAIM** gem button (`templates/claim.png`)
whenever it shows up.

### Console

```shell
python run_scenarios.py            # runs scenarios.json, logs every action, Ctrl+C to stop
```

Example log:

```
[12:39:26] engine started: 1 active scenario(s): claim gems
[12:39:26] check  claim gems         FOUND conf=1.000 at (556, 282)
[12:39:26]   TAP   claim gems at (556, 282)
```

### UI (tkinter)

```shell
python scenario_ui.py
```

- Table of scenarios — Add / Edit / Duplicate / Remove, reorder with ↑ / ↓.
- Edit dialog: name, template (Browse), check interval, threshold, tap cooldown,
  action (tap / log-only), enabled, multi-scale.
- **Start / Stop** the engine; a live log shows every check and tap.
- **Screenshot** button grabs the device screen to `screen.png` for cropping new
  templates. **Save** writes back to `scenarios.json`.

The engine (`automation_engine.py`) is shared by both and is device-agnostic, so
it can be unit-tested with a fake device — no emulator required.

## Demo Video
To be updated

## Instruction Video
To be updated