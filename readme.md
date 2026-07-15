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

A themed control panel:

- **Scenario table** with a live **On** column — click it (or press Space) to
  enable/disable a scenario, even while the engine is running. Columns show the
  target, interval, threshold, action, and a live **Fires** counter.
- Add / Edit / Duplicate / Remove, reorder with ▲ / ▼, **Enable all / Disable all**.
- **Edit dialog** covers every field: template (Browse), `when`/`unless` gates,
  tap points, interval, threshold, cooldown, action, rotate step, enabled,
  multi-scale.
- **Test** the selected scenario — captures one frame and reports the match
  confidence / gate state without tapping.
- **Import / Export** from the menus: the whole config, or a single selected
  scenario (`File` and `Scenario` menus). Plus Save (Ctrl+S) and Reload.
- **Start / Stop** with a status light; a colour-coded live log (taps green,
  warnings/errors highlighted) with **Clear** and **Autoscroll**.
- **Screenshot** grabs the device screen to `screen.png` for cropping templates.

### Macro scenarios (multi-step routines)

A scenario can be a small **multi-step macro** instead of a single reactive tap —
open a menu, scroll, tap every match, go back — all built from the UI via the
**Steps (JSON)** field in the edit dialog. Steps run in order once each time the
scenario fires. Step vocabulary:

| step | what it does |
|------|--------------|
| `{"do":"tap","template":"…","threshold":0.8,"all":false,"band":[y0,y1],"rotate":0}` | locate a template and tap it (best match, or **every** match with `"all"`; `band` limits to a vertical fraction of the screen) |
| `{"do":"tap_points","points":[[x,y]]}` | tap fixed device points |
| `{"do":"swipe","vector":[x0,y0,x1,y1],"dur":400}` | swipe/scroll — coords are **fractions** of the screen (0–1) |
| `{"do":"wait","seconds":0.5}` / `{"do":"back"}` | pause / press Back |
| `{"do":"repeat","steps":[…],"max":25,"until":"stable"}` | repeat inner steps until the screen stops changing (`"stable"`), until a pass taps nothing (`"no_tap"`), or until a template is gone (`{"gone":"…","threshold":0.65}`) |

The shipped **`claim all (event)`** scenario (disabled by default) is exactly this:
open the ★ menu, scroll to the top, sweep down tapping every **CLAIM**, repeat
sweeps until nothing is left, then tap **"Tap To Return To Game"** — all by image
recognition. Enable it (with `reveal ui` on, so the panel is up) and set an
interval. `claim_all.py` remains as a standalone equivalent that also colour-gates
on the star's reward badge.

The engine (`automation_engine.py`) is device-agnostic, so both reactive
scenarios and macros can be unit-tested with a fake device — no emulator required.

### Advanced scenarios (menu buttons)

Beyond "find a template and tap it", a scenario can drive fixed on-screen menu
buttons, gated by what's visible:

- `points`: a list of fixed `[x, y]` taps (device pixels) — for buttons at known
  positions. If set, the scenario taps these instead of a matched center.
- `when`: only act if this template **is** on screen (context gate).
- `unless`: only act if this template is **not** on screen (negative gate).
- `rotate`: if `> 0`, match the template at every `rotate`° step (0..360) so it
  finds items that **spin** — e.g. the orbiting diamond pickup. `downscale`
  (e.g. `0.5`) and `roi` `[x0,y0,x1,y1]` keep this fast.

The shipped `scenarios.json` uses these to auto-play a run at 900x1600:

| scenario          | what it does                                                        |
|-------------------|---------------------------------------------------------------------|
| `claim gems`      | taps the 💎 CLAIM button whenever it appears                        |
| `collect diamond` | `rotate`s the diamond template to find the spinning orbit pickup and taps it |
| `reveal ui`       | taps an empty spot to bring the upgrade panel up (`unless` UI shown) |
| `upgrade attack`  | `when` ATTACK panel is up, taps the 2×3 upgrade grid                 |
| `upgrade defense` | `when` DEFENSE panel is up, taps the 2×3 upgrade grid                |
| `switch to defense` / `switch to attack` | tap the tab to cycle Attack↔Defense so both get bought |

Set the panel's buy amount to **Max** in-game so each tap buys everything
affordable. The tap coordinates are tuned for a **900x1600** portrait screen; if
your emulator differs, re-grab templates with the UI's **Screenshot** button and
adjust the `points` (editable in the Add/Edit dialog).

## Demo Video
To be updated

## Instruction Video
To be updated