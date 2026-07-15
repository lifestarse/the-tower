"""Template-matching automation loop for The Tower.

The image-recognition counterpart of run_automation.py. Instead of tapping fixed
coordinates, it *locates* buttons on screen and taps where they actually are, so
it survives moved elements and resolution changes.

Drop button crops into templates/ (see templates/README.md). The names below are
all optional -- any that have no matching templates/<name>.png are skipped:

    claim            - a generic claim / collect button (rewards, gems, ...)
    retry            - the retry button on the game-over screen
    upgrade_damage   - an in-run 'damage' upgrade button
    upgrade_speed    - an in-run 'attack speed' upgrade button

    python run_smart_automation.py
"""
import time

from android_device import AndroidDevice
from image_recognition import TemplateLibrary, locate_and_tap, multi_scale

DEVICE_ID = 'emulator-5554'
APP_PACKAGE = 'com.TechTreeGames.TheTower'
THRESHOLD = 0.82

# Buttons we try to tap each tick, in priority order.
ACTIONS = ['claim', 'retry', 'upgrade_damage', 'upgrade_speed']


def main():
    lib = TemplateLibrary('templates')
    print('loaded templates:', ', '.join(lib.names()) or '<none>')
    if len(lib) == 0:
        print('Add button crops to templates/ before running (capture_screen.py).')
        return

    device = AndroidDevice(DEVICE_ID)
    scales = multi_scale()

    while True:
        screen = device.capture()
        if screen is None:
            print('capture failed')
            time.sleep(2)
            continue

        if device.get_top_activity_package() != APP_PACKAGE:
            print('The Tower is not in the foreground')
            time.sleep(3)
            continue

        acted = False
        for name in ACTIONS:
            if name not in lib:
                continue
            m = locate_and_tap(device, screen, lib[name],
                               threshold=THRESHOLD, scales=scales)
            if m:
                print('tapped %-16s conf=%.3f at %s' % (name, m.confidence, m.center))
                acted = True
                time.sleep(0.4)

        if not acted:
            print('nothing to tap')
        time.sleep(1.5)


if __name__ == '__main__':
    main()
