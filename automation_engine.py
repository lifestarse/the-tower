"""
Scenario-driven automation engine for The Tower.

A *scenario* pairs a template image with settings: how often to look for it
(`interval`, seconds — configurable per scenario), the match `threshold`, and
what to do when it is found (`tap` or log-only). The engine captures the screen
on a fast tick and, for every enabled scenario whose interval has elapsed, runs
template matching and acts — logging every check and tap.

Shared by run_scenarios.py (console) and scenario_ui.py (tkinter UI).

The engine is device-agnostic: it obtains a device from `device_factory`,
which by default lazily builds an android_device.AndroidDevice. Tests (and the
UI, before a device exists) can pass their own factory, so importing this module
never requires ppadb or a live emulator.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, asdict, fields, field

import numpy as np

from image_recognition import (find_template, find_all_templates, find_rotated,
                               multi_scale, to_gray)
from humanize import human_sleep

DEFAULT_CONFIG = 'scenarios.json'


@dataclass
class Scenario:
    name: str
    template: str = ''             # PNG crop to locate + tap, e.g. templates/claim.png
    enabled: bool = True
    threshold: float = 0.80        # min match confidence (0..1)
    interval: float = 1.0          # seconds between checks — per scenario
    cooldown: float = 0.5          # min seconds between taps after a hit
    multi_scale: bool = True       # search several template scales
    scale_min: float = 0.8         # multi-scale: smallest template scale to try
    scale_max: float = 1.2         # multi-scale: largest scale to try (up to ~10x)
    scale_steps: int = 9           # multi-scale: number of scales between min & max
    action: str = 'tap'            # 'tap' | 'none' (log only)
    # --- optional: context gate + fixed-point tapping (for menu buttons) ---
    when: str = ''                 # gate: act only if this template IS on screen.
    #                                May also be a list (OR: any one on screen is
    #                                enough) and each entry may be a
    #                                {"template":.., "threshold":..} dict.
    unless: str = ''               # negative gate: skip if this template IS on
    #                                screen. Same list/dict forms as `when`
    #                                (OR: skip if ANY listed template is present).
    points: list = field(default_factory=list)  # fixed [x,y] taps (device px);
    #                                if set, tap these instead of a matched center
    # --- optional: rotation-invariant matching (for spinning items) ---
    rotate: int = 0                # if >0, match template at every `rotate`° step
    downscale: float = 1.0         # shrink screen+template for speed (rotate only)
    roi: list = field(default_factory=list)  # [x0,y0,x1,y1] search box (rotate)
    # --- optional: multi-step macro (open menu -> scroll -> claim -> back) ---
    steps: list = field(default_factory=list)  # if set, run these in order once

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        known = {f.name for f in fields(Scenario)}
        return Scenario(**{k: v for k, v in d.items() if k in known})


def _default_device_factory(device_id):
    from android_device import AndroidDevice
    return AndroidDevice(device_id)


def _gate_entries(value, default_threshold):
    """Normalise a ``when`` / ``unless`` value into ``[(path, threshold), ...]``.

    Accepts, for backward compatibility and OR-gates:
      * ``''``               -> no gate (empty list)
      * ``'templates/x.png'``-> single template at ``default_threshold``
      * ``{'template': ..., 'threshold': ...}`` -> single, own threshold
      * a list mixing the two forms -> OR gate (the caller treats a hit on ANY
        entry as the gate being satisfied)
    """
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append((it['template'],
                        float(it.get('threshold', default_threshold))))
        else:
            out.append((it, default_threshold))
    return out


# --------------------------------------------------------------------------- #
# Macro step interpreter — lets a scenario be a small multi-step routine
# (open a menu, scroll, tap every match, go back ...) built from the UI.
# --------------------------------------------------------------------------- #
def _screens_similar(a, b, tol=3.0):
    aa = np.asarray(a.convert('L'), dtype=np.int16)
    bb = np.asarray(b.convert('L'), dtype=np.int16)
    return float(np.abs(aa - bb).mean()) < tol


# --------------------------------------------------------------------------- #
# Perk selection (OCR the "Choose a New Perk" screen and pick a good one)
# --------------------------------------------------------------------------- #
# Custom perk priority — higher = picked sooner. Keyed by a lowercase substring
# of the perk text (as OCR reads it). Perks not listed use DEFAULT_PRIORITY.
PERK_PRIORITY = {
    'perk wave requirement': 5,
    'defense percent': 4,
    'max health': 4,
    'free upgrade': 4,
    'interest': 2,
    'health regen': 2,
}
DEFAULT_PRIORITY = 3          # any good/UW perk not in PERK_PRIORITY


def _merge_perk_lines(ocr_lines):
    """Group OCR text lines into perk cards by vertical proximity.

    ``ocr_lines`` is ``[(top, bottom, text), ...]``. A card's wrapped text
    arrives as separate OCR lines ("... and lifesteal" / "-90%"), and such a
    fragment alone can read as a clean no-downside perk — so cards must be
    scored whole, never per line. Lines inside one card sit tightly stacked;
    the margin between cards is far taller than a text line. Split wherever
    the vertical gap exceeds ~0.8 of the median line height.
    Returns ``[(y_center, text), ...]`` per card, top to bottom.
    """
    if not ocr_lines:
        return []
    ocr_lines = sorted(ocr_lines)
    heights = sorted(b - t for (t, b, _) in ocr_lines)
    gap_limit = max(6, int(heights[len(heights) // 2] * 0.8))
    cards = []
    for top, bot, txt in ocr_lines:
        if cards and top - cards[-1][1] <= gap_limit:
            cards[-1][1] = max(cards[-1][1], bot)
            cards[-1][2].append(txt)
        else:
            cards.append([top, bot, [txt]])
    return [((top + bot) // 2, ' '.join(parts)) for top, bot, parts in cards]


def perk_badness(text, avoid=('enemy_damage', 'tower_hp', 'lifesteal')):
    """Which avoided categories a perk's text triggers (empty set = fine).
    All trade-off downsides read as "..., but ...". We only flag the ones the
    user hates: enemy damage UP, tower max health DOWN, tower lifesteal DOWN."""
    t = text.lower().replace('×', 'x')                  # normalise x
    hits = set()
    if 'enemy_damage' in avoid and re.search(r'enem\w*\s+damage\s*x', t):
        hits.add('enemy_damage')                             # "Enemies Damage x2.5", "Ranged Enemies Damage x3"
    if ('tower_hp' in avoid and 'max health -' in t
            and not re.search(r'(enem|boss|ranged)\w*\s+max health', t)):
        hits.add('tower_hp')                                 # only the TOWER's own "Max Health -70%" (not enemy/boss)
    if 'lifesteal' in avoid and (re.search(r'lifesteal\s*-', t)
                                 or 'and lifesteal' in t or 'life absorption -' in t):
        hits.add('lifesteal')                                # "... Lifesteal -90%"
    return hits


def perk_score(text, avoid=('enemy_damage', 'tower_hp', 'lifesteal')):
    """Higher = better. Precedence matches the code below:
      0 = a trade-off hitting an AVOIDED category (never pick unless forced);
      else, if the text matches a PERK_PRIORITY key -> that priority, EVEN when the
        perk also carries a harmless downside — a listed strong perk outranks a
        plain one (e.g. "Free Upgrade Chance ..., but Coins -20%" still scores 4);
      else 1 for any remaining harmless trade-off (kept below plain perks);
      else DEFAULT_PRIORITY. The pick_perk step takes the highest score."""
    if perk_badness(text, avoid):
        return 0
    t = text.lower()
    for key, pri in PERK_PRIORITY.items():
        if key in t:
            return pri
    is_tradeoff = ('but' in t.split() or ' but ' in t or ' но ' in t)
    return 1 if is_tradeoff else DEFAULT_PRIORITY


def run_steps(device, steps, log, width, height, stop=None):
    """Run a macro step list once. Returns the number of taps performed.

    Step shapes (each a dict with a "do" key):
      {"do":"tap","template":"path","threshold":0.8,"all":false,"rotate":0,
       "downscale":1.0,"roi":[],"band":[y0f,y1f],"gap":0.35}
      {"do":"tap_points","points":[[x,y], ...]}
      {"do":"swipe","vector":[x0f,y0f,x1f,y1f],"dur":400}   # fractions of screen
      {"do":"wait","seconds":0.5}
      {"do":"back"}
      {"do":"repeat","steps":[...],"max":25,"until":"stable"|"no_tap"}
    """
    taps = 0
    for step in steps:
        if stop is not None and stop.is_set():
            break
        taps += _run_step(device, step, log, width, height, stop)
    return taps


def _run_step(device, step, log, width, height, stop):
    do = (step.get('do') or '').lower()

    if do == 'wait':
        human_sleep(float(step.get('seconds', 0.5)))
        return 0

    if do == 'back':
        device.back()
        return 0

    if do == 'swipe':
        v = step.get('vector', [0.5, 0.75, 0.5, 0.4])
        dur = int(step.get('dur', 400))
        device.swipe_xy(int(width * v[0]), int(height * v[1]),
                        int(width * v[2]), int(height * v[3]), dur)
        return 0

    if do == 'tap_points':
        pts = step.get('points', [])
        for (x, y) in pts:
            device.tap_xy(int(x), int(y))
        return len(pts)

    if do == 'tap':
        tpl = step.get('template')
        if not tpl:
            return 0
        th = float(step.get('threshold', 0.8))
        screen = device.capture()
        if step.get('all'):
            hits = find_all_templates(screen, tpl, threshold=th)
            band = step.get('band')
            if band:
                y0, y1 = int(height * band[0]), int(height * band[1])
                hits = [h for h in hits if y0 <= h.center[1] <= y1]
            for h in sorted(hits, key=lambda m: m.center[1]):
                device.tap_xy(*h.center)
                human_sleep(float(step.get('gap', 0.35)))
            if hits:
                log('  macro: tapped %dx %s' % (len(hits), os.path.basename(tpl)))
            return len(hits)
        if int(step.get('rotate', 0)):
            m = find_rotated(screen, tpl, step=int(step['rotate']), threshold=th,
                             downscale=float(step.get('downscale', 1.0)),
                             roi=(tuple(step['roi']) if step.get('roi') else None))
        else:
            m = find_template(screen, tpl, threshold=th)
        if m is not None:
            device.tap_xy(*m.center)
            log('  macro: tapped %s' % os.path.basename(tpl))
            return 1
        return 0

    if do == 'pick_perk':
        try:
            import winocr
        except ImportError:
            log('  macro: pick_perk needs winocr (pip install winocr) — Windows only')
            return 0
        avoid = tuple(step.get('avoid', ['enemy_damage', 'tower_hp', 'lifesteal']))
        screen = device.capture()
        try:
            res = winocr.recognize_pil_sync(screen.convert('RGB'), step.get('lang', 'en'))
        except Exception as e:  # noqa: BLE001
            log('  macro: pick_perk OCR failed: %s' % e)
            return 0
        lines = []
        for ln in res.get('lines', []):
            ws = ln.get('words') or []
            if not ws:
                continue
            top = min(w['bounding_rect']['y'] for w in ws)
            bot = max(w['bounding_rect']['y'] + w['bounding_rect']['height'] for w in ws)
            lines.append((top, bot, ln['text']))
        header_y = None
        footer_y = height
        for top, bot, txt in lines:
            tl = txt.lower()
            yc = (top + bot) // 2
            if 'choose' in tl and 'perk' in tl:
                header_y = yc
            elif 'selected perk' in tl and yc > (header_y or 0):
                footer_y = min(footer_y, yc)
        if header_y is None:
            log('  macro: pick_perk — perk screen not open (no "Choose a New Perk")')
            return 0
        body = [(top, bot, txt) for (top, bot, txt) in lines
                if header_y < (top + bot) // 2 < footer_y]
        # Score whole cards, and only clusters that contain actual words — a
        # letterless cluster is OCR junk, not a perk.
        choices = [(yc, txt) for (yc, txt) in _merge_perk_lines(body)
                   if len(txt.strip()) >= 4 and re.search(r'[a-zа-яё]', txt.lower())]
        if not choices:
            log('  macro: pick_perk — screen open but no choices read')
            return 0
        scored = sorted(choices, key=lambda c: (perk_score(c[1], avoid), -c[0]), reverse=True)
        best_yc, best_txt = scored[0]
        best_score = perk_score(best_txt, avoid)
        x = int(width * float(step.get('x_frac', 0.5)))
        if best_score == 0:
            log('  macro: pick_perk — ALL choices are bad, taking least-bad: %r' % best_txt)
        else:
            skipped = [t for (_, t) in choices if t != best_txt]
            log('  macro: pick_perk -> %r%s'
                % (best_txt, (' (skipped %s)' % ', '.join('%r' % s for s in skipped)) if skipped else ''))
        device.tap_xy(x, int(best_yc))
        return 1

    if do == 'if':
        # Run inner steps only if a template is present (or absent, present:false).
        tpl = step.get('template')
        th = float(step.get('threshold', 0.8))
        found = bool(tpl) and find_template(device.capture(), tpl, threshold=th) is not None
        if found == bool(step.get('present', True)):
            return run_steps(device, step.get('steps', []), log, width, height, stop)
        return 0

    if do == 'whereami':
        # Log which named screen (screens.json) is currently showing.
        try:
            from screen_state import identify_screen
            info = identify_screen(device.capture())
            if info['matched']:
                log('  macro: screen = %s (%.3f)' % (info['name'], info['confidence']))
            else:
                log('  macro: screen = unknown (closest %s %.3f)'
                    % (info.get('closest'), info['confidence']))
        except Exception as e:  # noqa: BLE001
            log('  macro: whereami failed: %s' % e)
        return 0

    if do == 'upgrade_all':
        # Smart in-battle upgrade buyer (see upgrade_all.py): reads every cell
        # across the Attack/Defense/Utility tabs, skips maxed ones (button
        # colour), and buys buyable ones by cash-priority within budget.
        try:
            from upgrade_all import run_upgrade_all, CATEGORIES
        except Exception as e:  # noqa: BLE001
            log('  macro: upgrade_all unavailable (%s)' % e)
            return 0
        cats = tuple(step.get('cats') or CATEGORIES)
        try:
            return run_upgrade_all(device, log, cats=cats,
                                   floor=int(step.get('floor', 1)),
                                   verbose=bool(step.get('verbose', False)))
        except Exception as e:  # noqa: BLE001
            log('  macro: upgrade_all failed: %s' % e)
            return 0

    if do == 'if_screen':
        # Run inner steps only when the current screen is (or, present:false,
        # is NOT) the named screen from screens.json.
        want = step.get('screen')
        try:
            from screen_state import identify_screen
            info = identify_screen(device.capture())
        except Exception as e:  # noqa: BLE001
            log('  macro: if_screen failed: %s' % e)
            return 0
        on_it = (info['name'] == want)
        if on_it == bool(step.get('present', True)):
            return run_steps(device, step.get('steps', []), log, width, height, stop)
        return 0

    if do == 'whichmenu':
        # Log which DB screen (menu_db / screen_db) the whole frame matches.
        try:
            from menu_db import identify_menu
            info = identify_menu(device.capture(),
                                 threshold=float(step.get('threshold', 10.0)))
            if info['matched']:
                log('  macro: menu = %s (dist %s)' % (info['name'], info['distance']))
            else:
                log('  macro: menu = unknown (closest %s %s)'
                    % (info.get('closest'), info['distance']))
        except Exception as e:  # noqa: BLE001
            log('  macro: whichmenu failed: %s' % e)
        return 0

    if do == 'if_menu':
        # Run inner steps only when the whole frame matches (or, present:false,
        # does NOT match) the named screen from the menu_db (screen_db/*.png).
        want = step.get('menu')
        try:
            from menu_db import identify_menu
            info = identify_menu(device.capture(),
                                 threshold=float(step.get('threshold', 10.0)))
        except Exception as e:  # noqa: BLE001
            log('  macro: if_menu failed: %s' % e)
            return 0
        on_it = (info['name'] == want)
        if on_it == bool(step.get('present', True)):
            return run_steps(device, step.get('steps', []), log, width, height, stop)
        return 0

    if do == 'goto':
        # Navigate to a named screen via the transition graph (screen_graph.py /
        # transitions.json): BFS a path from wherever we are, tap each hop and
        # re-check after every step. Lets a macro prepend "get to screen X".
        try:
            from screen_graph import goto as _goto, load_graph
        except Exception as e:  # noqa: BLE001
            log('  macro: goto unavailable (%s)' % e)
            return 0
        want = step.get('menu') or step.get('screen')
        if not want:
            log('  macro: goto needs a "menu" target')
            return 0
        try:
            ok = _goto(device, want, log, graph=load_graph(),
                       max_hops=int(step.get('max_hops', 8)),
                       allow_relaunch=bool(step.get('relaunch', False)), stop=stop)
            return 1 if ok else 0
        except Exception as e:  # noqa: BLE001
            log('  macro: goto failed: %s' % e)
            return 0

    if do == 'explore':
        # Optional bounded auto-crawler that seeds the transition graph by tapping
        # ONLY an allowlist of safe (non-destructive) templates. Needs "safe".
        try:
            from screen_graph import explore as _explore, load_graph
        except Exception as e:  # noqa: BLE001
            log('  macro: explore unavailable (%s)' % e)
            return 0
        safe = step.get('safe') or []
        if not safe:
            log('  macro: explore needs a "safe" template allowlist — skipped')
            return 0
        try:
            return _explore(device, load_graph(), log, safe=tuple(safe),
                            budget=int(step.get('budget', 150)), stop=stop)
        except Exception as e:  # noqa: BLE001
            log('  macro: explore failed: %s' % e)
            return 0

    if do == 'repeat':
        inner = step.get('steps', [])
        max_iter = int(step.get('max', 20))
        until = step.get('until') or 'stable'   # 'stable' | 'no_tap' | {'gone': tpl}
        gone = until.get('gone') if isinstance(until, dict) else None
        gone_th = float(until.get('threshold', 0.8)) if isinstance(until, dict) else 0.8
        total = 0
        for _ in range(max_iter):
            if stop is not None and stop.is_set():
                break
            before = device.capture() if until == 'stable' else None
            got = run_steps(device, inner, log, width, height, stop)
            total += got
            if gone is not None:
                if find_template(device.capture(), gone, threshold=gone_th) is None:
                    break
            elif until == 'no_tap' and got == 0:
                break
            elif until == 'stable' and before is not None \
                    and _screens_similar(before, device.capture()):
                break
        return total

    log('  macro: unknown step %r' % do)
    return 0


class Engine:
    def __init__(self, config_path=DEFAULT_CONFIG, logger=print, device_factory=None):
        self.config_path = config_path
        self.logger = logger
        self.device_factory = device_factory or _default_device_factory

        self.device_id = 'emulator-5554'
        self.app_package = 'com.TechTreeGames.TheTower'
        self.require_foreground = True
        self.scenarios = []
        self.stats = {}                # scenario name -> times it acted this run

        self._thread = None
        self._stop = threading.Event()

        self.load()

    # --------------------------------------------------------------- logging
    def log(self, msg):
        self.logger('[%s] %s' % (time.strftime('%H:%M:%S'), msg))

    # --------------------------------------------------------------- config
    def to_dict(self):
        return {
            'device_id': self.device_id,
            'app_package': self.app_package,
            'require_foreground': self.require_foreground,
            'scenarios': [s.to_dict() for s in self.scenarios],
        }

    def apply_dict(self, data):
        self.device_id = data.get('device_id', self.device_id)
        self.app_package = data.get('app_package', self.app_package)
        self.require_foreground = data.get('require_foreground', self.require_foreground)
        self.scenarios = [Scenario.from_dict(s) for s in data.get('scenarios', [])]

    def load(self, path=None):
        path = path or self.config_path
        if not os.path.exists(path):
            self.scenarios = []
            return
        with open(path, 'r', encoding='utf-8') as f:
            self.apply_dict(json.load(f))

    def save(self, path=None):
        path = path or self.config_path
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    # --------------------------------------------------------------- control
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running():
            return
        self.stats = {}
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    # --------------------------------------------------------------- loop
    def _run(self):
        try:
            device = self.device_factory(self.device_id)
        except Exception as e:  # noqa: BLE001 - surface any init failure to the user
            self.log('ERROR: cannot open device %r: %s' % (self.device_id, e))
            self.log('       install pure-python-adb and make sure adb sees the '
                     'emulator (adb devices).')
            return

        # Grayscale template cache — lazy, so scenarios can be enabled mid-run.
        cache = {}
        warned = set()

        def get_tpl(path):
            if path not in cache:
                cache[path] = to_gray(path)
            return cache[path]

        def prepare(s):
            """Load a scenario's templates on demand; False if it can't act."""
            try:
                if s.template:
                    get_tpl(s.template)
                for _p, _th in _gate_entries(s.when, s.threshold):
                    get_tpl(_p)
                for _p, _th in _gate_entries(s.unless, s.threshold):
                    get_tpl(_p)
            except Exception as e:  # noqa: BLE001
                if s.name not in warned:
                    self.log('WARN: scenario %r: cannot load template (%s)' % (s.name, e))
                    warned.add(s.name)
                return False
            return bool(s.template or s.points or s.steps)

        last_check = {}
        last_tap = {}
        tick = 0.1

        def tapped(name):
            last_tap[name] = time.monotonic()
            self.stats[name] = self.stats.get(name, 0) + 1

        self.log('engine started (%d scenario(s) configured)' % len(self.scenarios))

        while not self._stop.is_set():
            now = time.monotonic()
            # Re-read `enabled` every tick so the UI can toggle scenarios live.
            due = [s for s in self.scenarios
                   if s.enabled and now - last_check.get(s.name, 0.0) >= s.interval]
            if not due:
                human_sleep(tick)
                continue

            if self.require_foreground:
                try:
                    pkg = device.get_top_activity_package()
                except Exception as e:  # noqa: BLE001
                    self.log('activity check error: %s' % e)
                    human_sleep(1)
                    continue
                if pkg != self.app_package:
                    for s in due:
                        last_check[s.name] = now
                    self.log('waiting: foreground is %s (want %s)' % (pkg, self.app_package))
                    human_sleep(1)
                    continue

            try:
                screen = device.capture()
            except Exception as e:  # noqa: BLE001
                self.log('capture error: %s' % e)
                human_sleep(1)
                continue
            if screen is None:
                self.log('capture returned None')
                human_sleep(1)
                continue

            for s in due:
                last_check[s.name] = now
                if not prepare(s):
                    continue
                scales = (multi_scale(s.scale_min, s.scale_max, s.scale_steps)
                          if s.multi_scale else None)

                # Context gate: skip unless AT LEAST ONE 'when' template is on
                # screen (OR over the list). Each entry may carry its own threshold.
                when_entries = _gate_entries(s.when, s.threshold)
                if when_entries:
                    if not any(find_template(screen, get_tpl(p), threshold=th,
                                             scales=scales) is not None
                               for p, th in when_entries):
                        self.log('check  %-18s (gate off)' % s.name)
                        continue
                # Negative gate: skip if ANY 'unless' template IS on screen (OR).
                unless_entries = _gate_entries(s.unless, s.threshold)
                if unless_entries:
                    if any(find_template(screen, get_tpl(p), threshold=th,
                                         scales=scales) is not None
                           for p, th in unless_entries):
                        self.log('check  %-18s (already present)' % s.name)
                        continue

                if s.steps:
                    # Multi-step macro: run the whole routine once.
                    if now - last_tap.get(s.name, 0.0) < s.cooldown:
                        continue
                    w, h = screen.size
                    self.log('macro  %-18s running %d step(s)' % (s.name, len(s.steps)))
                    n = run_steps(device, s.steps, self.log, w, h, self._stop)
                    self.log('macro  %-18s done (%d tap(s))' % (s.name, n))
                    if n:
                        tapped(s.name)
                    else:
                        last_tap[s.name] = now
                    continue

                if s.points:
                    # Fixed-point tapping (menu buttons at known positions).
                    if now - last_tap.get(s.name, 0.0) < s.cooldown:
                        continue
                    if s.action == 'tap':
                        for (x, y) in s.points:
                            device.tap_xy(int(x), int(y))
                        self.log('  TAP   %-16s %d point(s)' % (s.name, len(s.points)))
                        tapped(s.name)
                    else:
                        last_tap[s.name] = now
                    continue

                if s.rotate:
                    m = find_rotated(screen, get_tpl(s.template),
                                     step=s.rotate, threshold=s.threshold,
                                     scales=scales, downscale=(s.downscale or 1.0),
                                     roi=(tuple(s.roi) if s.roi else None))
                else:
                    m = find_template(screen, get_tpl(s.template),
                                      threshold=s.threshold, scales=scales)
                if m is None:
                    self.log('check  %-18s no match' % s.name)
                    continue
                self.log('check  %-18s FOUND conf=%.3f at %s'
                         % (s.name, m.confidence, m.center))
                if s.action == 'tap':
                    if now - last_tap.get(s.name, 0.0) < s.cooldown:
                        self.log('  skip tap (%s on cooldown)' % s.name)
                        continue
                    device.tap_point(m.center)
                    self.log('  TAP   %s at %s' % (s.name, m.center))
                    tapped(s.name)

            human_sleep(tick)

        self.log('engine stopped')
