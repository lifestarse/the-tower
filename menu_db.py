"""
menu_db.py — whole-screen menu recognition by comparing a screenshot to a
database of reference frames.

Where screen_state.py matches small *signature templates* and the legacy
state.py compares colour histograms on fixed rectangles, this compares the
WHOLE frame: every screen is reduced to a small resolution-independent
grayscale thumbnail (a "fingerprint"), and the current capture is matched to the
closest stored fingerprint by mean-absolute-difference.

The database is just a folder of PNGs (default ``screen_db/``) where **the file
name is the label** — ``perk_select.png`` means the screen "perk_select". No
index file to keep in sync: add, rename or delete PNGs freely.

Learning / test mode (exactly the "capture unseen frames" idea):

    python menu_db.py --learn        # walk the game through its menus; every
                                     # frame that matches nothing in the DB is
                                     # saved as new_XXX.png — rename them after.

Other uses:

    python menu_db.py --identify     # print the current menu once
    python menu_db.py --watch        # print the current menu live (~1s)
    python menu_db.py --list         # list the screens in the DB
    python menu_db.py --selftest     # verify the matcher (no device needed)

Programmatic (also used by the engine's whichmenu / if_menu macro steps):

    from menu_db import identify_menu
    info = identify_menu(device.capture())
    info['name']       # e.g. 'perk_select' or 'unknown'
    info['distance']   # mean-abs-diff to the closest screen (lower = more alike)
    info['matched']    # True if distance <= threshold
"""
import argparse
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
DB_DIR = os.path.join(HERE, 'screen_db')
DEVICE_ID = '127.0.0.1:5556'

THUMB = 64            # canonical thumbnail edge — makes the compare resolution-
#                      independent (a menu looks the same at 720p and 1080p once
#                      shrunk to 64x64).
MATCH_MAD = 10.0     # max mean-abs-difference (0..255) to call two frames the
#                      SAME screen. 0 = identical; different menus are usually
#                      20-60+. Raise it to be more forgiving of animation, lower
#                      it to split near-identical screens.

try:
    sys.stdout.reconfigure(encoding='utf-8')      # console may default to cp1251
except (AttributeError, ValueError):
    pass

# Pillow moved the resampling constants under Image.Resampling in 9.1+.
try:
    _RESAMPLE = Image.Resampling.BILINEAR
except AttributeError:                            # older Pillow
    _RESAMPLE = Image.BILINEAR


# --------------------------------------------------------------------------- #
# Fingerprint + distance
# --------------------------------------------------------------------------- #
def signature(image):
    """Return the resolution-independent fingerprint of a frame: a THUMB x THUMB
    grayscale float32 array. Accepts a PIL image or a path to a PNG."""
    if isinstance(image, str):
        image = Image.open(image)
    g = image.convert('L').resize((THUMB, THUMB), _RESAMPLE)
    return np.asarray(g, dtype=np.float32)


def distance(a, b):
    """Mean absolute difference (0..255) between two signatures. 0 = identical."""
    return float(np.abs(a - b).mean())


# --------------------------------------------------------------------------- #
# Database (a folder of PNGs; file stem = label)
# --------------------------------------------------------------------------- #
def load_db(folder=DB_DIR):
    """Load every PNG in ``folder`` as a list of ``(label, signature, path)``.
    ``label`` is the file name without extension. Unreadable files are skipped.
    """
    db = []
    if not os.path.isdir(folder):
        return db
    for fn in sorted(os.listdir(folder)):
        if not fn.lower().endswith('.png'):
            continue
        path = os.path.join(folder, fn)
        try:
            db.append((os.path.splitext(fn)[0], signature(path), path))
        except Exception:  # noqa: BLE001 — a bad file shouldn't break the DB
            continue
    return db


def _next_name(folder, prefix='new'):
    n = 1
    while os.path.exists(os.path.join(folder, '%s_%03d.png' % (prefix, n))):
        n += 1
    return '%s_%03d.png' % (prefix, n)


def add_screen(capture, folder=DB_DIR, name=None, prefix='new'):
    """Save ``capture`` (a PIL image) into the DB folder as a new screen and
    return its path. Auto-names ``new_001.png`` etc. if ``name`` is omitted."""
    os.makedirs(folder, exist_ok=True)
    fn = name or _next_name(folder, prefix)
    if not fn.lower().endswith('.png'):
        fn += '.png'
    path = os.path.join(folder, fn)
    capture.convert('RGB').save(path)
    return path


# --------------------------------------------------------------------------- #
# Identify
# --------------------------------------------------------------------------- #
def identify_menu(capture, db=None, folder=DB_DIR, threshold=MATCH_MAD):
    """Classify ``capture`` into a named screen from the DB.

    Returns ``{'name', 'distance', 'matched', 'closest', 'ranked'}``. ``name`` is
    the closest label when its distance is <= ``threshold``, else ``'unknown'``
    (``closest`` still names the nearest). ``ranked`` is the 5 nearest screens.
    """
    if db is None:
        db = load_db(folder)
    if not db:
        return {'name': 'unknown', 'distance': None, 'matched': False,
                'closest': None, 'ranked': []}
    sig = signature(capture)
    scored = sorted((distance(sig, s), label) for label, s, _ in db)
    best_d, best_label = scored[0]
    matched = best_d <= threshold
    return {'name': best_label if matched else 'unknown',
            'distance': round(best_d, 2), 'matched': matched,
            'closest': best_label,
            'ranked': [(lbl, round(d, 2)) for d, lbl in scored[:5]]}


# --------------------------------------------------------------------------- #
# Learning / test mode
# --------------------------------------------------------------------------- #
def learn(device, folder=DB_DIR, threshold=MATCH_MAD, interval=1.0,
          max_iter=None, log=print):
    """Capture repeatedly; whenever a frame matches nothing in the DB (best
    distance > ``threshold``) save it as a NEW screen. Walk the game through its
    menus while this runs and the DB grows one entry per distinct screen. Rename
    the saved ``new_*.png`` to meaningful labels afterwards.

    Runs until ``max_iter`` frames (None = until Ctrl+C).
    """
    os.makedirs(folder, exist_ok=True)
    db = load_db(folder)
    log('learn mode: %d screen(s) in DB, threshold=%.1f MAD (Ctrl+C to stop)'
        % (len(db), threshold))
    i = 0
    try:
        while max_iter is None or i < max_iter:
            i += 1
            cap = device.capture()
            if cap is None:
                log('  capture failed')
                time.sleep(interval)
                continue
            sig = signature(cap)
            best_d, best_label = min(
                ((distance(sig, s), lbl) for lbl, s, _ in db),
                default=(None, None))
            if best_d is not None and best_d <= threshold:
                log('  known: %-18s (dist %.2f)' % (best_label, best_d))
            else:
                path = add_screen(cap, folder)
                db.append((os.path.splitext(os.path.basename(path))[0], sig, path))
                near = ('' if best_d is None
                        else ' (closest %s @ %.2f)' % (best_label, best_d))
                log('  NEW screen -> %s%s' % (os.path.basename(path), near))
            time.sleep(interval)
    except KeyboardInterrupt:
        log('stopped — %d screen(s) in DB' % len(db))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser(description='Whole-screen menu database + learner')
    ap.add_argument('--device', default=DEVICE_ID)
    ap.add_argument('--dir', default=DB_DIR, help='database folder (PNGs)')
    ap.add_argument('--threshold', type=float, default=MATCH_MAD,
                    help='max mean-abs-diff to count as the same screen')
    ap.add_argument('--interval', type=float, default=1.0)
    ap.add_argument('--learn', action='store_true',
                    help='test mode: save frames not already in the DB')
    ap.add_argument('--identify', action='store_true', help='print current menu once')
    ap.add_argument('--watch', action='store_true', help='print current menu live')
    ap.add_argument('--list', action='store_true', help='list screens in the DB')
    ap.add_argument('--selftest', action='store_true', help='verify matcher (no device)')
    args = ap.parse_args(argv)

    if args.selftest:
        _selftest()
        return

    if args.list:
        db = load_db(args.dir)
        print('%d screen(s) in %s:' % (len(db), args.dir))
        for label, _sig, path in db:
            print('  %-22s %s' % (label, os.path.basename(path)))
        return

    from android_device import AndroidDevice
    device = AndroidDevice(args.device)

    if args.learn:
        learn(device, args.dir, args.threshold, args.interval)
        return

    db = load_db(args.dir)
    while True:
        info = identify_menu(device.capture(), db=db, threshold=args.threshold)
        if info['matched']:
            print('menu: %-18s (dist %s)' % (info['name'], info['distance']))
        else:
            print('menu: %-18s (closest %s %s)'
                  % ('unknown', info.get('closest'), info['distance']))
        if args.identify or not args.watch:
            return
        time.sleep(args.interval)


# --------------------------------------------------------------------------- #
# Self-test (no device / game required)
# --------------------------------------------------------------------------- #
def _selftest():
    import tempfile
    rng = np.random.default_rng(0)

    def frame(seed):
        # Model a MENU, not white noise: a few big coloured bands (low-frequency
        # layout that survives the 64x64 downscale) plus light noise on top.
        # Pure high-frequency noise averages to flat grey when shrunk and would
        # be indistinguishable — real screens have large structured regions.
        r = np.random.default_rng(seed)
        img = np.zeros((1600, 900, 3), dtype=np.uint8)
        n = 8
        for i in range(n):
            y0, y1 = i * 1600 // n, (i + 1) * 1600 // n
            img[y0:y1, :, :] = r.integers(0, 255, size=3, dtype=np.uint16).astype(np.uint8)
        img = np.clip(img.astype(np.int16) + r.integers(-10, 10, img.shape),
                      0, 255).astype(np.uint8)
        return Image.fromarray(img, 'RGB')

    tmp = tempfile.mkdtemp(prefix='menudb_')
    a = frame(1); b = frame(2); c = frame(3)
    add_screen(a, tmp, 'menu_a')
    add_screen(b, tmp, 'menu_b')
    add_screen(c, tmp, 'menu_c')
    db = load_db(tmp)
    assert len(db) == 3, 'expected 3 screens, got %d' % len(db)

    # Exact frame -> its own label, distance ~0.
    r = identify_menu(b, db=db)
    assert r['name'] == 'menu_b' and r['distance'] < 1.0, r

    # Same frame + mild noise -> still matches menu_b.
    bn = np.asarray(b.convert('RGB'), dtype=np.int16)
    bn = np.clip(bn + rng.integers(-8, 8, bn.shape), 0, 255).astype(np.uint8)
    r2 = identify_menu(Image.fromarray(bn, 'RGB'), db=db, threshold=MATCH_MAD)
    assert r2['name'] == 'menu_b' and r2['matched'], r2

    # A brand-new distinct frame -> unknown (nearest still reported).
    r3 = identify_menu(frame(99), db=db, threshold=MATCH_MAD)
    assert not r3['matched'] and r3['name'] == 'unknown' and r3['closest'], r3

    # add-if-unseen (the learn rule): unseen frame gets saved and then matches.
    before = len(load_db(tmp))
    novel = frame(42)
    if not identify_menu(novel, folder=tmp)['matched']:
        add_screen(novel, tmp)
    assert len(load_db(tmp)) == before + 1
    assert identify_menu(novel, folder=tmp)['matched']

    print('menu_db self-test OK — exact=%.2f noisy=%.2f unknown-closest=%s@%.1f'
          % (r['distance'], r2['distance'], r3['closest'], r3['distance']))


if __name__ == '__main__':
    main()
