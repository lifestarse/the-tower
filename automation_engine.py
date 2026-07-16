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
import threading
import time
from dataclasses import dataclass, asdict, fields, field

import numpy as np

from image_recognition import (find_template, find_all_templates, find_rotated,
                               multi_scale, to_gray)

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
    when: str = ''                 # only act if THIS template IS on screen (gate)
    unless: str = ''               # only act if THIS template is NOT on screen
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


# --------------------------------------------------------------------------- #
# Macro step interpreter — lets a scenario be a small multi-step routine
# (open a menu, scroll, tap every match, go back ...) built from the UI.
# --------------------------------------------------------------------------- #
def _screens_similar(a, b, tol=3.0):
    aa = np.asarray(a.convert('L'), dtype=np.int16)
    bb = np.asarray(b.convert('L'), dtype=np.int16)
    return float(np.abs(aa - bb).mean()) < tol


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
        time.sleep(float(step.get('seconds', 0.5)))
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
                time.sleep(float(step.get('gap', 0.35)))
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
                if s.when:
                    get_tpl(s.when)
                if s.unless:
                    get_tpl(s.unless)
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
                time.sleep(tick)
                continue

            if self.require_foreground:
                try:
                    pkg = device.get_top_activity_package()
                except Exception as e:  # noqa: BLE001
                    self.log('activity check error: %s' % e)
                    time.sleep(1)
                    continue
                if pkg != self.app_package:
                    for s in due:
                        last_check[s.name] = now
                    self.log('waiting: foreground is %s (want %s)' % (pkg, self.app_package))
                    time.sleep(1)
                    continue

            try:
                screen = device.capture()
            except Exception as e:  # noqa: BLE001
                self.log('capture error: %s' % e)
                time.sleep(1)
                continue
            if screen is None:
                self.log('capture returned None')
                time.sleep(1)
                continue

            for s in due:
                last_check[s.name] = now
                if not prepare(s):
                    continue
                scales = (multi_scale(s.scale_min, s.scale_max, s.scale_steps)
                          if s.multi_scale else None)

                # Context gate: skip unless the 'when' template is on screen.
                if s.when:
                    if find_template(screen, get_tpl(s.when),
                                     threshold=s.threshold, scales=scales) is None:
                        self.log('check  %-18s (gate off)' % s.name)
                        continue
                # Negative gate: skip if the 'unless' template IS on screen.
                if s.unless:
                    if find_template(screen, get_tpl(s.unless),
                                     threshold=s.threshold, scales=scales) is not None:
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

            time.sleep(tick)

        self.log('engine stopped')
