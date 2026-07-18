"""
humanize.py — small random jitter so the automation behaves less like a machine.

A real finger never taps the exact same pixel twice, and a human never pauses
for exactly the same number of milliseconds. These helpers add a little random
variation to taps and sleeps so timing and clicks "wander" like a person's.

Deliberately dependency-free (no ppadb / cv2 / numpy), so any module can import
it — including automation_engine, which must stay importable without a device.

    from humanize import human_sleep, jitter_point
    human_sleep(0.5)                 # sleeps 0.475..0.525s  (0.5 +/- 5%)
    x, y = jitter_point(540, 960)    # e.g. (548, 957) — within +/-15 / +/-8 px
"""
import random
import time

# A tap lands within +/- these many pixels of the target — a fingertip-sized
# wobble around the button centre (buttons are far larger than this, so the tap
# still hits, it just isn't pixel-perfect every time).
TAP_JITTER_X = 15
TAP_JITTER_Y = 8

# A sleep varies by +/- this fraction of its duration (0.05 = +/-5%).
SLEEP_FRAC = 0.05


def human_sleep(seconds, frac=SLEEP_FRAC):
    """``time.sleep(seconds)`` with +/- ``frac`` jitter (default +/-5%).

    The result is always positive (0.95x..1.05x of ``seconds`` at the default),
    so this is a safe drop-in for ``time.sleep``. Non-positive input is a no-op.
    """
    seconds = float(seconds)
    if seconds <= 0:
        return
    time.sleep(seconds + random.uniform(-frac, frac) * seconds)


def jitter_point(x, y, dx=TAP_JITTER_X, dy=TAP_JITTER_Y):
    """Return ``(x, y)`` nudged by a random +/-``dx`` / +/-``dy``, clamped to >=0.

    Note: apply this to *taps* only, never to the scroll-swipe path — the
    upgrade panel scrolls straight down an ~8px safe gutter and any horizontal
    wobble there would start the swipe on a cell (which buys it).
    """
    return (max(0, int(x) + random.randint(-dx, dx)),
            max(0, int(y) + random.randint(-dy, dy)))
