"""
claim_all.py — open the Event → Missions list and CLAIM every completed mission,
scrolling down until the bottom, then return to the game.

Recreates this manual plan from the in-game side menu:
  1. make sure the game UI is shown (reveal the panel if it's hidden)
  2. tap the ★ menu button  ->  Event / Missions list
  3. tap every CLAIM button in view, scroll down, repeat until the bottom
  4. tap "Tap To Return To Game"

Tuned for BlueStacks portrait 900x1600. Uses image recognition for the CLAIM
buttons (so it claims whatever is completable) and fixed taps for the menu
buttons. Logs every action.

    python claim_all.py
"""
import sys
import time

import numpy as np

try:
    sys.stdout.reconfigure(encoding='utf-8')      # console may default to cp1251
except (AttributeError, ValueError):
    pass

from android_device import AndroidDevice
from image_recognition import find_template, find_all_templates

DEVICE_ID = '127.0.0.1:5555'

UI_SHOWN = 'templates/ui_shown.png'   # panel marker -> side menu (★) is visible
CLAIM = 'templates/claim_list.png'    # the word CLAIM inside a mission button

STAR = (843, 404)        # ★ menu button -> Event / Missions
RETURN = (450, 1515)     # "Tap To Return To Game" footer
REVEAL = (450, 400)      # empty battlefield -> toggles the UI on
SWIPE = (450, 1250, 450, 620, 400)    # scroll the list down one page
LIST_TOP, LIST_BOTTOM = 330, 1450     # y-band of the scrollable list
CLAIM_THRESHOLD = 0.85
MAX_SCROLLS = 25


def _similar(a, b, tol=3.0):
    """True if two screenshots are essentially identical (list at the bottom)."""
    aa = np.asarray(a.convert('L'), dtype=np.int16)
    bb = np.asarray(b.convert('L'), dtype=np.int16)
    return float(np.abs(aa - bb).mean()) < tol


def _claims_in_view(device):
    hits = find_all_templates(device.capture(), CLAIM, threshold=CLAIM_THRESHOLD)
    return [h for h in hits if LIST_TOP <= h.center[1] <= LIST_BOTTOM]


def main(logger=print, device_id=DEVICE_ID):
    def log(msg):
        logger('[%s] %s' % (time.strftime('%H:%M:%S'), msg))

    device = AndroidDevice(device_id)

    # 1. ensure the UI (and thus the star menu) is on screen
    if find_template(device.capture(), UI_SHOWN, threshold=0.8) is None:
        log('UI hidden -> revealing panel')
        device.tap_xy(*REVEAL)
        time.sleep(0.8)
    if find_template(device.capture(), UI_SHOWN, threshold=0.8) is None:
        log('WARN: UI panel not detected; continuing anyway')

    # 2. open Event / Missions
    log('opening Event (star menu button)')
    device.tap_xy(*STAR)
    time.sleep(1.2)

    # 3. claim everything, scrolling down until the list stops moving
    total = 0
    for _ in range(MAX_SCROLLS):
        # claim all currently visible (several passes: the list refreshes in place)
        for _pass in range(4):
            hits = _claims_in_view(device)
            if not hits:
                break
            for h in sorted(hits, key=lambda m: m.center[1]):
                device.tap_xy(h.center[0], h.center[1])
                total += 1
                log('CLAIM at %s (conf %.2f)' % (h.center, h.confidence))
                time.sleep(0.35)

        before = device.capture()
        device.swipe_xy(*SWIPE)
        time.sleep(0.7)
        if _similar(before, device.capture()):
            log('reached the bottom of the list')
            break

    log('claimed %d reward(s)' % total)

    # 4. back to the game
    device.tap_xy(*RETURN)
    time.sleep(0.6)
    log('done')


if __name__ == '__main__':
    main()
