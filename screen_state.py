"""
screen_state.py — recognise WHICH screen The Tower is currently showing, using
the same template images as the scenario engine (templates/*.png).

Unlike the older state.py (which compares whole-screen reference captures), this
maps a small set of *signature templates* to a named screen. A screen matches
when any of its templates is found at/above the screen's threshold; screens are
checked in the order listed in screens.json, so overlays (skip, perk, game-over)
take priority over base screens (in_game).

    python screen_state.py            # live: print the current screen every ~1s
    python screen_state.py --once     # print once and exit

Programmatic use (also used by the engine's `whereami` / `if_screen` steps):

    from screen_state import identify_screen
    info = identify_screen(device.capture())
    info['name']        # e.g. 'reward_skip' or 'unknown'
    info['confidence']  # best matching template's score
    info['ranked']      # [(name, conf), ...] every screen, best first
"""
import json
import os
import sys
import time

from image_recognition import find_template

try:
    sys.stdout.reconfigure(encoding='utf-8')      # console may default to cp1251
except (AttributeError, ValueError):
    pass

DEVICE_ID = '127.0.0.1:5555'
SCREENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'screens.json')

_cache = {}


def load_screens(path=SCREENS_FILE, force=False):
    """Load (and cache) the screen definitions from screens.json."""
    if force or path not in _cache:
        with open(path, encoding='utf-8') as f:
            _cache[path] = json.load(f)['screens']
    return _cache[path]


def identify_screen(capture, screens=None):
    """Classify ``capture`` (a PIL image) into a named screen.

    Returns a dict::

        {'name', 'confidence', 'template', 'matched', 'ranked'[, 'closest']}

    ``matched`` is False (and ``name`` == 'unknown') when nothing clears its
    threshold; ``closest`` then names the best-scoring screen for debugging.
    ``ranked`` is every screen's best score, highest first.
    """
    if screens is None:
        screens = load_screens()

    ranked = []
    winner = None
    for sc in screens:
        th = float(sc.get('threshold', 0.85))
        best_conf, best_tpl = 0.0, None
        for tpl in sc.get('templates', []):
            try:
                m = find_template(capture, tpl, threshold=0.0)   # 0.0 => always best
            except Exception:      # noqa: BLE001 — a missing template shouldn't kill detection
                continue
            if m is not None and m.confidence > best_conf:
                best_conf, best_tpl = m.confidence, tpl
        ranked.append((sc['name'], round(best_conf, 3)))
        if winner is None and best_conf >= th:
            winner = {'name': sc['name'], 'confidence': round(best_conf, 3),
                      'template': best_tpl, 'matched': True}

    ranked_sorted = sorted(ranked, key=lambda x: x[1], reverse=True)
    if winner is None:
        top_name, top_conf = ranked_sorted[0] if ranked_sorted else ('unknown', 0.0)
        winner = {'name': 'unknown', 'confidence': top_conf,
                  'template': None, 'matched': False, 'closest': top_name}
    winner['ranked'] = ranked_sorted
    return winner


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    once = '--once' in argv
    verbose = '-v' in argv or '--verbose' in argv

    from android_device import AndroidDevice
    device = AndroidDevice(DEVICE_ID)
    screens = load_screens()

    while True:
        cap = device.capture()
        if cap is None:
            print('capture failed')
            if once:
                return
            time.sleep(2)
            continue
        info = identify_screen(cap, screens)
        if info['matched']:
            line = 'screen: %-13s (%.3f via %s)' % (
                info['name'], info['confidence'], os.path.basename(info['template']))
        else:
            line = 'screen: %-13s (closest %s %.3f)' % (
                'unknown', info.get('closest'), info['confidence'])
        if verbose:
            line += '  | ' + ', '.join('%s=%.2f' % (n, c) for n, c in info['ranked'][:4])
        print(line)
        if once:
            return
        time.sleep(1.0)


if __name__ == '__main__':
    main()
