"""
upgrade_all.py — smart in-battle upgrade buyer for The Tower.

Replaces the old blind "upgrade attack / upgrade defense" scenarios (which just
tapped 6 fixed cells). For each category tab (Attack / Defense / Utility) it:

  * opens the tab *header-aware* — the tabs are toggles, so tapping the tab that
    is already open would CLOSE the panel; we only tap when the OCR'd panel title
    ("ATTACK UPGRADES" ...) is not already the target tab,
  * decides whether each cell is MAXED by the colour of its buy button
    (blue $-button = buyable, grey/brown "Max" = maxed) — far more robust than
    OCR-ing the low-contrast word "Max",
  * OCRs the cell NAME and matches it (fuzzily — the DBs may contain errors) to a
    priority database, skips anything at/below a priority floor,
  * checks affordability (parses the cash balance and each cost) so it does not
    waste taps on upgrades it cannot pay for, and
  * scrolls the whole list top-to-bottom so upgrades past the visible 6 are seen.

Maxed cells are remembered in state/upgrade_state.json for the current run (so we
don't re-OCR them); clear it with --reset or reset_run_memory() on a new run.

Priority DB (battle = cash spent DURING a run):
  C:/Users/user/.gemini/antigravity-ide/brain/<id>/battle_upgrades_db.json

Standalone (safe: --dry plans only, never buys — but it DOES open tabs & scroll):
    python upgrade_all.py --dry --tab attack
    python upgrade_all.py --dry --all
    python upgrade_all.py --all
"""
import argparse
import difflib
import json
import os
import re
import sys
import time

import numpy as np

try:
    sys.stdout.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
DEVICE_ID = '127.0.0.1:5556'
STATE_FILE = os.path.join(HERE, 'state', 'upgrade_state.json')

BATTLE_DB = ('C:/Users/user/.gemini/antigravity-ide/brain/'
             'e315e4f7-1ad5-4972-840a-1c8ac8e6288f/battle_upgrades_db.json')

# --------------------------------------------------------------------------- #
# UI geometry — calibrated against a 900x1600 capture of the in-run panel.
# Boxes are (x0, y0, x1, y1) in base coords; everything is scaled to the live
# capture so it still works at other emulator resolutions.
# --------------------------------------------------------------------------- #
BASE_W, BASE_H = 900, 1600
COL_X = [(12, 442), (458, 888)]                       # left / right cell columns
PANEL_TOP, PANEL_BOT = 1044, 1518                     # cell area (below header..above tabs)
ROW_Y = [(1048, 1182), (1214, 1348), (1380, 1514)]    # legacy fixed rows (unused)
NAME_FRAC = 0.52          # name occupies the left 52% of a cell
BTN_TOP_FRAC = 0.60       # buy button sits in the bottom 40% of a cell
HEADER_BOX = (24, 972, 560, 1040)                     # "ATTACK UPGRADES" title
CASH_BOX = (36, 26, 360, 92)                           # top-left "$15,32M"
WAVE_BOX = (468, 858, 700, 912)                        # "Wave 1182" (right stats box)
TAB_X = {'attack': 112, 'defense': 335, 'utility': 558}
TAB_Y = 1555
# Scroll strictly down the SCREEN CENTRE LINE: the ~8px dark gutter between the
# two cell columns is the only touch-safe corridor (The Tower's buy buttons fire
# on pointer-DOWN — hold-to-buy — so a swipe that starts on a cell BUYS it).
SCROLL_X = BASE_W // 2                    # 450 — dead centre, in the column gutter
SCROLL_TOP, SCROLL_BOT = 1120, 1470      # rewind band (big fling to the top)
SCROLL_MS = 180                          # quick fling (rewind)
# Paging step: a SLOW, short drag advances the list by ~1.4 rows with overlap, so
# no upgrade is skipped between pages (a fast fling over-scrolls and skips cells).
PAGE_FROM, PAGE_TO, PAGE_MS = 1400, 1180, 480

PRIORITY_FLOOR = 1        # never auto-buy cash_priority <= this (e.g. Range=0)
# Hard blocklist — never bought regardless of DB priority (the DBs can be wrong;
# these are useless cash buys per the user). Matched by normalised name_en.
NEVER_BUY = ('Health Regen', 'Defense Absolute')
# Affordability: only buy when the cost is at most this fraction of current cash,
# so cash is spread over many cheap upgrades instead of dumped into one very
# expensive one (which passively maxes out anyway). 1.0 = "anything affordable".
SPEND_FRAC = 1.0
CATEGORIES = ('attack', 'defense', 'utility')
_SUFFIX = {'k': 1e3, 'm': 1e6, 'b': 1e9, 't': 1e12, 'q': 1e15}


# --------------------------------------------------------------------------- #
# Money / name helpers
# --------------------------------------------------------------------------- #
def parse_money(s):
    """'$ 6,53K' -> 6530.0 ; '12,00% $ 312,66M' -> 312660000.0 ; None if absent.
    The Tower uses comma as the decimal separator (e.g. 15,32M)."""
    if not s:
        return None
    matches = re.findall(r'([0-9]+(?:[.,][0-9]+)?)\s*([kmbtqKMBTQ])?', s)
    # prefer the token that followed a '$'
    dollar = re.search(r'\$\s*([0-9]+(?:[.,][0-9]+)?)\s*([kmbtqKMBTQ])?', s)
    if dollar:
        num, suf = dollar.group(1), dollar.group(2)
    elif matches:
        num, suf = matches[-1]
    else:
        return None
    try:
        val = float(num.replace(',', '.'))
    except ValueError:
        return None
    return val * _SUFFIX.get((suf or '').lower(), 1.0)


def _money(v):
    if v is None:
        return '?'
    for suf, div in (('B', 1e9), ('M', 1e6), ('K', 1e3)):
        if v >= div:
            return '%.1f%s' % (v / div, suf)
    return '%.0f' % v


def _norm(s):
    return re.sub(r'[^a-z0-9%/]', '', (s or '').lower())


def _clean_name(ocr):
    """Keep the leading alphabetic part of a whole-cell OCR, dropping the value,
    cost and the 'Max' button label (name is drawn top-left, value/cost/Max to
    the right / below)."""
    if not ocr:
        return ''
    out = []
    for tok in ocr.replace('\n', ' ').split():
        if re.search(r'[0-9$%]', tok) or tok.lower().strip('.,') == 'max':
            break
        out.append(tok)
    return ' '.join(out).strip()


# --------------------------------------------------------------------------- #
# Priority database
# --------------------------------------------------------------------------- #
class Priorities:
    """Maps an OCR'd cell name to (name_en, priority). Tolerant of DB/OCR errors."""

    def __init__(self, db_path=BATTLE_DB):
        self.by_cat = {c: {} for c in CATEGORIES}
        self.flat = {}
        try:
            root = json.load(open(db_path, encoding='utf-8'))['battle_upgrades']
            self.load_error = None
        except Exception as e:  # noqa: BLE001
            root, self.load_error = {}, str(e)
        for cat in CATEGORIES:
            for it in root.get(cat, []) or []:
                name = it.get('name_en') or ''
                prio = int(it.get('cash_priority') or 0)
                key = _norm(name)
                if key:
                    self.by_cat[cat][key] = (name, prio)
                    self.flat.setdefault(key, (name, prio, cat))

    def match(self, cell_text, category=None):
        """Match a whole-cell OCR string to (name_en, priority, matched_bool).

        Matching is restricted to the CURRENT TAB's category (each tab only shows
        its own upgrades) — this stops a partial OCR like 'Health Level Skip'
        (really utility 'Enemy Health Level Skip') from matching defense 'Health'.
        Within the category it takes the LONGEST DB name that is a substring of
        the normalised cell text (robust to value/cost/'Max' noise, any order),
        then falls back to fuzzy matching for OCR misspellings."""
        key = _norm(cell_text)
        if not key:
            return (cell_text, 0, False)
        pool = self.by_cat.get(category)
        if not pool:                       # no/unknown category — search everything
            pool = {k: v[:2] for k, v in self.flat.items()}
        best = None                        # (name_len, name, prio)
        for dbnorm, (name, prio) in pool.items():
            if dbnorm and dbnorm in key and (best is None or len(dbnorm) > best[0]):
                best = (len(dbnorm), name, prio)
        if best:
            return (best[1], best[2], True)
        alpha = re.sub(r'[^a-z]', '', key)
        hit = difflib.get_close_matches(alpha, list(pool), n=1, cutoff=0.72)
        if hit:
            v = pool[hit[0]]
            return (v[0], v[1], True)
        return (cell_text, 0, False)


# --------------------------------------------------------------------------- #
# Screen reading
# --------------------------------------------------------------------------- #
# Resolution/aspect independence: the whole UI is sized by SCREEN WIDTH, and
# each element is anchored to the edge it hugs in the game — the top bar (cash)
# to the TOP, the upgrade panel / tabs / grid to the BOTTOM. So we never trust a
# raw pixel: x = base_x * (w/900); a bottom element's y is measured up from the
# live bottom edge, a top element's y down from the top — both scaled by width.
# This is correct for 720x1280, 1080x1920, 1080x2400, etc., not just 900x1600.
def _lx(bx, w):
    return int(bx * w / BASE_W)


def _ly(by, w, h, anchor='bottom'):
    s = w / BASE_W
    return int(by * s) if anchor == 'top' else int(h - (BASE_H - by) * s)


def _sbox(box, w, h, anchor='bottom'):
    return (_lx(box[0], w), _ly(box[1], w, h, anchor),
            _lx(box[2], w), _ly(box[3], w, h, anchor))


def _spt(x, y, w, h, anchor='bottom'):
    return (_lx(x, w), _ly(y, w, h, anchor))


def _is_blue_button(rgb_region):
    """True if the buy button is the blue '$cost' (buyable), False for grey 'Max'.
    Uses the FRACTION of strongly-blue pixels (not the mean) so it still fires
    when the sampled box is slightly misaligned or partly covers cost text —
    the mean-based test produced false 'maxed' readings."""
    if rgb_region.size == 0:
        return False
    px = rgb_region.reshape(-1, 3).astype(np.int16)
    R, G, B = px[:, 0], px[:, 1], px[:, 2]
    blue = (B > 105) & (B > R + 22) & (B > G + 12)
    return float(blue.mean()) > 0.12


def _ocr(pil_img):
    import winocr
    res = winocr.recognize_pil_sync(pil_img.convert('RGB'), 'en')
    return (res.get('text') or '').strip()


def detect_rows(capture):
    """Find the FULLY-visible cell rows in the current panel from a brightness
    profile (cell fills are brighter than the dark gaps between them). This keeps
    OCR + taps aligned at ANY scroll offset — fixed rows caught half-cells and
    produced partial names like 'Chance' for 'Critical Chance'. Returns [(y0,y1)]
    in live pixels, partial (cut-off) rows excluded."""
    arr = np.asarray(capture.convert('L'), dtype=np.float32)
    H, W = arr.shape
    y_top, y_bot = _ly(PANEL_TOP, W, H), _ly(PANEL_BOT, W, H)
    prof = arr[y_top:y_bot, _lx(20, W):_lx(880, W)].mean(1)
    lo, mid = float(prof.min()), float(np.median(prof))
    thr = lo + 0.30 * (mid - lo)                 # between gap floor and cell fill
    mask = prof > thr
    bands, s = [], None
    for i, m in enumerate(mask):
        if m and s is None:
            s = i
        elif not m and s is not None:
            bands.append((s, i)); s = None
    if s is not None:
        bands.append((s, len(mask)))
    min_full = _lx(120, W)                        # full cell ~166px; drop slivers
    return [(y_top + a, y_top + b) for a, b in bands if (b - a) >= min_full]


def cells_in_view(capture):
    """Yield (col, cell_box, btn_box, tap_pt) for every fully-visible cell, using
    dynamically detected rows × the two fixed columns."""
    w, h = capture.size
    for (y0, y1) in detect_rows(capture):
        ch = y1 - y0
        for c, (cx0, cx1) in enumerate(COL_X):
            cell = (_lx(cx0 + 4, w), y0 + 3, _lx(cx1 - 4, w), y1 - 3)
            btn = (_lx(int(cx0 + (cx1-cx0)*NAME_FRAC) + 4, w),
                   int(y0 + ch*BTN_TOP_FRAC), _lx(cx1 - 8, w), y1 - 4)
            tap = (_lx((cx0+cx1)//2, w), (y0+y1)//2)
            yield (c, cell, btn, tap)


def read_cash(capture):
    try:
        box = _sbox(CASH_BOX, capture.size[0], capture.size[1], anchor='top')
        return parse_money(_ocr(capture.crop(box)))
    except Exception:  # noqa: BLE001
        return None


def read_wave(capture):
    """OCR the current wave number (used to detect a new run -> cache reset)."""
    try:
        t = _ocr(capture.crop(_sbox(WAVE_BOX, *capture.size)))
    except Exception:  # noqa: BLE001
        return None
    m = re.search(r'(\d{1,6})', t.replace(' ', ''))
    return int(m.group(1)) if m else None


def read_header(capture):
    """OCR the panel title -> 'attack'|'defense'|'utility'|None."""
    try:
        t = _ocr(capture.crop(_sbox(HEADER_BOX, *capture.size))).lower()
    except Exception:  # noqa: BLE001
        return None
    for cat in CATEGORIES:
        if cat in t:
            return cat
    return None


def read_panel(capture):
    """Read every fully-visible cell. Returns list of dicts:
        {upgradable(bool), name(str), cost(float|None), tap_pt}.
    `upgradable` from the buy-button COLOUR (blue $-button vs grey Max);
    `name` from OCR of the whole cell; `cost` from OCR of the button ($ only)."""
    arr = np.asarray(capture.convert('RGB'))
    out = []
    for c, cellbox, btnbox, tap in cells_in_view(capture):
        breg = arr[btnbox[1]:btnbox[3], btnbox[0]:btnbox[2]]
        upgradable = _is_blue_button(breg)
        name, cost = '', None
        if upgradable:            # only OCR the FEW buyable cells — big speedup
            try:
                name = _ocr(capture.crop(cellbox)).replace('\n', ' ').strip()
            except Exception:  # noqa: BLE001
                name = ''
            try:
                cost = parse_money(_ocr(capture.crop(btnbox)))
            except Exception:  # noqa: BLE001
                cost = None
        out.append({'upgradable': upgradable, 'name': name, 'cost': cost,
                    'tap_pt': tap})
    return out


# --------------------------------------------------------------------------- #
# Run-scoped maxed memory
# --------------------------------------------------------------------------- #
def _blank_state():
    """Per-run temporary DB: the actionable BUYABLE upgrade names per tab (blue
    $-button, matched, not blacklisted, above the floor) + whether the tab has
    been fully scanned this run, plus the wave it was built at. Maxed/locked
    upgrades simply never enter `buyable`."""
    return {'wave': None, 'tabs': {c: {'buyable': [], 'scanned': False}
                                   for c in CATEGORIES}}


def load_state():
    try:
        st = json.load(open(STATE_FILE, encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return _blank_state()
    if 'tabs' not in st:
        st = _blank_state()
    for c in CATEGORIES:
        st.setdefault('tabs', {}).setdefault(c, {'buyable': [], 'scanned': False})
        st['tabs'][c].setdefault('buyable', [])
        st['tabs'][c].setdefault('scanned', False)
    st.setdefault('wave', None)
    return st


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def reset_run_memory():
    save_state(_blank_state())


def tab_buyable(state, category, prio):
    """Cached buyable upgrade names for a tab and their best priority.
    ([], -1) when nothing is buyable."""
    names = list(state.get('tabs', {}).get(category, {}).get('buyable', []))
    if not names:
        return [], -1
    return names, max(prio.match(n, category)[1] for n in names)


# --------------------------------------------------------------------------- #
# The upgrader
# --------------------------------------------------------------------------- #
def _swipe(device, w, h, y_from, y_to, ms=SCROLL_MS):
    """Scroll the list straight down the centre gutter (x identical at both ends
    so the whole path stays in the touch-safe corridor). A slower swipe = less
    fling momentum = a smaller, controlled scroll."""
    cx = _lx(SCROLL_X, w)
    device.swipe_xy(cx, _ly(y_from, w, h), cx, _ly(y_to, w, h), ms)


def _panel_moved(a_gray, b_gray, w, h, tol=2.0):
    """True if the list actually scrolled between two frames. Compares ONLY the
    left NAME strip of the cells — upgrade names are static text, so they change
    only when the list moves, whereas the cost/level numbers on the right animate
    every frame (and a whole-panel diff mistook that for scrolling, so rewind
    span 6-7 useless swipes and the bottom was never detected)."""
    yt, yb = _ly(PANEL_TOP, w, h), _ly(PANEL_BOT, w, h)
    x0, x1 = _lx(12, w), _lx(235, w)          # left-column name text
    return float(np.abs(a_gray[yt:yb, x0:x1] - b_gray[yt:yb, x0:x1]).mean()) >= tol


def _similar(a, b, tol=3.0):
    aa = np.asarray(a.convert('L'), dtype=np.int16)
    bb = np.asarray(b.convert('L'), dtype=np.int16)
    return float(np.abs(aa - bb).mean()) < tol


def open_tab(device, category, log):
    """Ensure the panel is open on `category` WITHOUT toggling it shut.
    Returns True on success."""
    cap = device.capture()
    w, h = cap.size
    if read_header(cap) == category:
        return True                       # already open on this tab — don't tap!
    device.tap_xy(*_spt(TAB_X[category], TAB_Y, w, h))
    time.sleep(0.35)
    if read_header(device.capture()) == category:
        return True
    # one retry (maybe it was open on this very tab and we just closed it)
    device.tap_xy(*_spt(TAB_X[category], TAB_Y, w, h))
    time.sleep(0.35)
    ok = read_header(device.capture()) == category
    if not ok:
        log('  upgrade: could not open %s tab' % category)
    return ok


def process_tab(device, prio, category, log, dry=False, floor=PRIORITY_FLOOR,
                state=None, cash=None, max_pages=14, verbose=False):
    if not open_tab(device, category, log):
        return 0, []
    w, h = device.capture().size
    state = state or load_state()
    never = {_norm(n) for n in NEVER_BUY}
    expected = set(state['tabs'][category].get('buyable', []))  # from cache
    remaining = set(expected)          # cached buyable we still need to find
    found = set()                      # buyable found this scan → rebuilds cache
    seen = set()
    plan = []
    bought = 0

    # Blind rewind to the top: 3 big flings, NO screenshots. adb screencap is the
    # slowest thing here (~200-400ms), so we do not capture/compare each step.
    for _ in range(3):
        _swipe(device, w, h, 1080, 1520, 140)
        time.sleep(0.05)
    time.sleep(0.1)

    prev = None                        # previous page's grayscale (bottom detection)
    for _page in range(max_pages):
        cap = device.capture()
        gray = np.asarray(cap.convert('L'), dtype=np.int16)
        if prev is not None and not _panel_moved(prev, gray, w, h):
            break                      # list didn't move since last scroll -> bottom
        prev = gray
        cells = read_panel(cap)        # names only OCR'd for BUYABLE (blue) cells
        if verbose:
            for cl in cells:
                if cl['upgradable']:
                    log('    [%s] BUYABLE cost=%s %r'
                        % (category, _money(cl['cost']), cl['name']))

        view_buy = []
        for cell in cells:
            if not cell['upgradable']:
                continue               # maxed — skipped, never OCR'd
            name_en, p, ok = prio.match(cell['name'], category)
            if not ok or _norm(name_en) in never or p <= floor:
                continue               # unmatched / blacklisted / junk priority
            found.add(name_en)
            remaining.discard(name_en)
            key = _norm(name_en)
            if key in seen:
                continue
            seen.add(key)
            view_buy.append((p, name_en, cell))

        for p, name_en, cell in sorted(view_buy, key=lambda t: t[0], reverse=True):
            cost = cell.get('cost')
            if cash is not None and cost is not None and cost > cash * SPEND_FRAC:
                plan.append({'name': name_en, 'priority': p,
                             'action': 'skip($%s)' % _money(cost)})
                continue
            plan.append({'name': name_en, 'priority': p,
                         'action': 'would-buy' if dry else 'buy'})
            if not dry:
                device.tap_xy(*cell['tap_pt'])
                bought += 1
                time.sleep(0.12)

        # Early stop (cache optimization): once every buyable the cache expects
        # has been found, the rest of the list is maxed — stop scrolling now.
        if expected and not remaining:
            break
        # Otherwise scroll one page (blind); the bottom is detected at the top of
        # the next iteration when the page no longer moves.
        _swipe(device, w, h, PAGE_FROM, PAGE_TO, PAGE_MS)
        time.sleep(0.06)

    state['tabs'][category] = {'buyable': sorted(found), 'scanned': True}
    return bought, plan


def read_cash_stable(device, n=2):
    """Median of a few quick cash reads — smooths transient OCR misreads (e.g. a
    frame where the '$' line is mid-animation and OCRs as '1')."""
    vals = []
    for _ in range(n):
        c = read_cash(device.capture())
        if c is not None:
            vals.append(c)
        time.sleep(0.08)
    if not vals:
        return None
    vals.sort()
    return vals[len(vals) // 2]


def run_upgrade_all(device, log=print, cats=CATEGORIES, dry=False,
                    floor=PRIORITY_FLOOR, verbose=False):
    prio = Priorities()
    if prio.load_error:
        log('  upgrade: WARNING priority DB not loaded (%s)' % prio.load_error)
    state = load_state()

    # New-run detection: the wave counter resets (drops) each run → the temp DB
    # of available/maxed upgrades is stale, so rebuild it from scratch.
    wave = read_wave(device.capture())
    prev = state.get('wave')
    if wave is not None and prev is not None and wave < prev:
        log('  upgrade: new run (wave %s < %s) — cache invalidated' % (wave, prev))
        state = _blank_state()
    if wave is not None:
        state['wave'] = wave

    cash = read_cash_stable(device)
    log('  upgrade: cash=%s wave=%s' % (_money(cash), wave))

    # Order tabs so the one holding the highest-priority buyable upgrade goes
    # FIRST — this is what puts Health (defense, p95) ahead of Damage (attack,
    # p75) instead of buying tab-by-tab. Uncached tabs sort after cached ones but
    # before fully-maxed ones, so they still get scanned on the first pass.
    def order_key(cat):
        if not state['tabs'].get(cat, {}).get('scanned'):
            return (1, 0)                       # not scanned yet — scan, default order
        names, best = tab_buyable(state, cat, prio)
        return (2, 0) if not names else (0, -best)   # fully-maxed last, else by prio
    total = 0
    for cat in sorted(cats, key=order_key):
        info = state['tabs'][cat]
        names, best = tab_buyable(state, cat, prio)
        if info.get('scanned') and not names:
            log('  upgrade[%s]: nothing buyable (cached) — skipped, no scroll' % cat)
            continue
        bought, plan = process_tab(device, prio, cat, log, dry=dry, floor=floor,
                                   state=state, cash=cash, verbose=verbose)
        total += bought
        shown = ', '.join('%s(p%d,%s)' % (p['name'], p['priority'], p['action'])
                          for p in plan[:10])
        log('  upgrade[%s]: %d bought | %s' % (cat, bought, shown or '—'))
    if not dry:
        save_state(state)
    return total


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--device', default=DEVICE_ID)
    ap.add_argument('--tab', choices=CATEGORIES)
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--dry', action='store_true', help='plan only, never buy')
    ap.add_argument('--reset', action='store_true', help='clear maxed memory')
    ap.add_argument('--floor', type=int, default=PRIORITY_FLOOR)
    ap.add_argument('--verbose', action='store_true', help='log every cell read')
    args = ap.parse_args(argv)

    if args.reset:
        reset_run_memory()
        print('maxed memory cleared')
    from android_device import AndroidDevice
    device = AndroidDevice(args.device)
    cats = CATEGORIES if (args.all or not args.tab) else (args.tab,)
    n = run_upgrade_all(device, print, cats=cats, dry=args.dry, floor=args.floor,
                        verbose=args.verbose)
    print('done — %d %s' % (n, 'planned buys' if args.dry else 'taps'))


if __name__ == '__main__':
    main()
