"""
claim_all.py — open the Event → Missions list and CLAIM every completed mission,
scrolling top-to-bottom until nothing is left, then return to the game.

Everything is driven by image recognition — no magic screen coordinates:
  * the ★ menu button, the CLAIM buttons and the "Tap To Return To Game" footer
    are all located by template matching (templates/menu_star.png,
    claim_list.png, return_game.png);
  * the reward badge is detected by colour in a box anchored to the *found* star;
  * swipes and the UI-reveal tap are derived from the live screen size.

Plan (recreates the manual flow):
  1. find the ★ menu button (reveal the UI first if it's hidden)
  2. only proceed if the star shows its blue reward badge
  3. open Event / Missions
  4. scroll to the top (the tab caches its position), then sweep down claiming
     every CLAIM; claiming reveals the next tier, so repeat sweeps until one
     claims nothing
  5. tap the "Tap To Return To Game" footer

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
from humanize import human_sleep

DEVICE_ID = '127.0.0.1:5555'

STAR = 'templates/menu_star.png'      # ★ menu button (opens Event / Missions)
CLAIM = 'templates/claim_list.png'    # the word CLAIM inside a mission button
RETURN = 'templates/return_game.png'  # "Tap To Return To Game" footer

MATCH_THRESHOLD = 0.80                # for the menu / return templates
CLAIM_THRESHOLD = 0.85
MAX_SCROLLS = 25                      # per scroll direction, per sweep
MAX_SWEEPS = 8                        # whole top->bottom passes

# The blue "N" reward badge sits up-left of the star. Search a box anchored to
# the matched star centre (not a fixed screen coordinate).
BADGE_DX = (-82, -5)                  # x offsets from star centre
BADGE_DY = (-50, 8)                   # y offsets from star centre
BADGE_MIN_PIXELS = 80


def _similar(a, b, tol=3.0):
    """True if two screenshots are essentially identical (list didn't scroll)."""
    aa = np.asarray(a.convert('L'), dtype=np.int16)
    bb = np.asarray(b.convert('L'), dtype=np.int16)
    return float(np.abs(aa - bb).mean()) < tol


def _badge_present(screen, star_center):
    """True if the star shows its blue reward badge (rewards are claimable)."""
    cx, cy = star_center
    x0, y0 = max(0, cx + BADGE_DX[0]), max(0, cy + BADGE_DY[0])
    x1, y1 = cx + BADGE_DX[1], cy + BADGE_DY[1]
    roi = np.array(screen.convert('RGB'))[y0:y1, x0:x1]
    if roi.size == 0:
        return False
    hsv = cv2.cvtColor(roi, cv2.COLOR_RGB2HSV)
    mask = ((hsv[:, :, 0] >= 100) & (hsv[:, :, 0] <= 140) &
            (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 110))
    return int(mask.sum()) >= BADGE_MIN_PIXELS


def main(logger=print, device_id=DEVICE_ID):
    def log(msg):
        logger('[%s] %s' % (time.strftime('%H:%M:%S'), msg))

    device = AndroidDevice(device_id)
    width, height = device.capture().size

    def swipe(y_from_frac, y_to_frac):
        device.swipe_xy(width // 2, int(height * y_from_frac),
                        width // 2, int(height * y_to_frac), 400)

    def claims_in_view():
        band = (int(height * 0.20), int(height * 0.92))
        hits = find_all_templates(device.capture(), CLAIM, threshold=CLAIM_THRESHOLD)
        return [h for h in hits if band[0] <= h.center[1] <= band[1]]

    # 1. locate the ★ menu button, revealing the UI if it is hidden
    star = find_template(device.capture(), STAR, threshold=MATCH_THRESHOLD)
    if star is None:
        log('menu not visible -> revealing UI')
        device.tap_xy(width // 2, int(height * 0.25))   # tap empty battlefield
        human_sleep(0.8)
        star = find_template(device.capture(), STAR, threshold=MATCH_THRESHOLD)
    if star is None:
        log('could not find the star menu button; aborting')
        return 0

    # 2. gate on the reward badge (anchored to the found star)
    if not _badge_present(device.capture(), star.center):
        log('no reward badge on the star -> nothing to claim')
        return 0

    # 3. open Event / Missions
    log('reward badge present -> opening event')
    device.tap_xy(*star.center)
    human_sleep(1.2)

    # 4. sweep the list top->bottom claiming everything, repeating until empty
    total = 0
    for sweep in range(MAX_SWEEPS):
        for _ in range(MAX_SCROLLS):            # rewind to the very top
            before = device.capture()
            swipe(0.39, 0.78)
            human_sleep(0.5)
            if _similar(before, device.capture()):
                break

        swept = 0
        for _ in range(MAX_SCROLLS):
            for _pass in range(4):              # list refreshes in place
                hits = claims_in_view()
                if not hits:
                    break
                for h in sorted(hits, key=lambda m: m.center[1]):
                    device.tap_xy(*h.center)
                    swept += 1
                    log('CLAIM at %s (conf %.2f)' % (h.center, h.confidence))
                    human_sleep(0.35)
            before = device.capture()
            swipe(0.78, 0.39)
            human_sleep(0.7)
            if _similar(before, device.capture()):
                break                           # bottom of the list

        total += swept
        if swept == 0:
            break
        log('sweep %d: +%d claimed, re-checking from the top' % (sweep + 1, swept))

    log('claimed %d reward(s)' % total)

    # 5. return to the main game screen via the footer button
    ret = find_template(device.capture(), RETURN, threshold=0.7)
    if ret is not None:
        device.tap_xy(*ret.center)
    else:
        device.tap_xy(width // 2, int(height * 0.95))   # fallback: blind-tap the footer
        human_sleep(0.6)
        # Only press Back if the blind tap did NOT already return us to the game
        # (the missions footer is still on screen). Doing both unconditionally can
        # send Back on the live game screen and open the pause menu.
        if find_template(device.capture(), RETURN, threshold=0.6) is not None:
            device.back()
    human_sleep(0.8)
    log('done')
    return total


if __name__ == '__main__':
    main()
