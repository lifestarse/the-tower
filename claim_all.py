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
import cv2

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
SCROLL_DOWN = (450, 1250, 450, 620, 400)   # reveal items further down the list
SCROLL_UP = (450, 620, 450, 1250, 400)     # reveal items further up (toward top)
LIST_TOP, LIST_BOTTOM = 330, 1450     # y-band of the scrollable list
CLAIM_THRESHOLD = 0.85
MAX_SCROLLS = 25
MAX_SWEEPS = 8

# The blue "N" badge at the star's top-left means there are rewards to claim.
BADGE_ROI = (775, 360, 850, 415)      # region that holds the badge (menu open)
BADGE_MIN_PIXELS = 80                 # blue-purple pixels needed to call it present


def _similar(a, b, tol=3.0):
    """True if two screenshots are essentially identical (list at the bottom)."""
    aa = np.asarray(a.convert('L'), dtype=np.int16)
    bb = np.asarray(b.convert('L'), dtype=np.int16)
    return float(np.abs(aa - bb).mean()) < tol


def _claims_in_view(device):
    hits = find_all_templates(device.capture(), CLAIM, threshold=CLAIM_THRESHOLD)
    return [h for h in hits if LIST_TOP <= h.center[1] <= LIST_BOTTOM]


def _badge_present(image):
    """True if the star shows its blue reward badge (rewards are claimable)."""
    x0, y0, x1, y1 = BADGE_ROI
    roi = np.array(image.convert('RGB'))[y0:y1, x0:x1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
    mask = ((hsv[:, :, 0] >= 100) & (hsv[:, :, 0] <= 140) &
            (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 110))
    return int(mask.sum()) >= BADGE_MIN_PIXELS


def _scroll_to_top(device):
    """The list caches its scroll position, so start from the very top."""
    for _ in range(MAX_SCROLLS):
        before = device.capture()
        device.swipe_xy(*SCROLL_UP)
        time.sleep(0.5)
        if _similar(before, device.capture()):
            return


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

    # 2. gate on the star's reward badge — no blue badge means nothing to claim
    if not _badge_present(device.capture()):
        log('no reward badge on the star — nothing to claim')
        return 0

    # 3. open Event / Missions
    log('reward badge present -> opening Event (star menu button)')
    device.tap_xy(*STAR)
    time.sleep(1.2)

    # 3-4. Sweep the list top -> bottom claiming everything. Claiming a mission
    # reveals its next tier (which may itself be completed), so repeat whole
    # sweeps until one claims nothing.
    total = 0
    for sweep in range(MAX_SWEEPS):
        _scroll_to_top(device)          # the tab remembers its scroll position
        swept = 0
        for _ in range(MAX_SCROLLS):
            for _pass in range(4):      # list refreshes in place after each claim
                hits = _claims_in_view(device)
                if not hits:
                    break
                for h in sorted(hits, key=lambda m: m.center[1]):
                    device.tap_xy(h.center[0], h.center[1])
                    swept += 1
                    log('CLAIM at %s (conf %.2f)' % (h.center, h.confidence))
                    time.sleep(0.35)
            before = device.capture()
            device.swipe_xy(*SCROLL_DOWN)
            time.sleep(0.7)
            if _similar(before, device.capture()):
                break               # bottom of the list
        total += swept
        if swept == 0:
            break
        log('sweep %d: +%d claimed, re-checking from the top' % (sweep + 1, swept))

    log('claimed %d reward(s)' % total)

    # 5. back to the main game screen
    device.tap_xy(*RETURN)
    time.sleep(0.8)
    if find_template(device.capture(), UI_SHOWN, threshold=0.8) is None:
        device.back()          # fallback if still on an overlay
        time.sleep(0.6)
    log('done')
    return total


if __name__ == '__main__':
    main()
