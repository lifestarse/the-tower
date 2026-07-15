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

from image_recognition import find_template, find_rotated, multi_scale, to_gray

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

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d):
        known = {f.name for f in fields(Scenario)}
        return Scenario(**{k: v for k, v in d.items() if k in known})


def _default_device_factory(device_id):
    from android_device import AndroidDevice
    return AndroidDevice(device_id)


class Engine:
    def __init__(self, config_path=DEFAULT_CONFIG, logger=print, device_factory=None):
        self.config_path = config_path
        self.logger = logger
        self.device_factory = device_factory or _default_device_factory

        self.device_id = 'emulator-5554'
        self.app_package = 'com.TechTreeGames.TheTower'
        self.require_foreground = True
        self.scenarios = []

        self._thread = None
        self._stop = threading.Event()

        self.load()

    # --------------------------------------------------------------- logging
    def log(self, msg):
        self.logger('[%s] %s' % (time.strftime('%H:%M:%S'), msg))

    # --------------------------------------------------------------- config
    def load(self):
        if not os.path.exists(self.config_path):
            self.scenarios = []
            return
        with open(self.config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.device_id = data.get('device_id', self.device_id)
        self.app_package = data.get('app_package', self.app_package)
        self.require_foreground = data.get('require_foreground', True)
        self.scenarios = [Scenario.from_dict(s) for s in data.get('scenarios', [])]

    def save(self):
        data = {
            'device_id': self.device_id,
            'app_package': self.app_package,
            'require_foreground': self.require_foreground,
            'scenarios': [s.to_dict() for s in self.scenarios],
        }
        with open(self.config_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # --------------------------------------------------------------- control
    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.is_running():
            return
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

        # Preload + cache every referenced template (main + gate) as grayscale once.
        cache = {}

        def get_tpl(path):
            if path not in cache:
                cache[path] = to_gray(path)
            return cache[path]

        enabled = []
        for s in self.scenarios:
            if not s.enabled:
                continue
            try:
                if s.template:
                    get_tpl(s.template)
                if s.when:
                    get_tpl(s.when)
                if s.unless:
                    get_tpl(s.unless)
            except Exception as e:  # noqa: BLE001
                self.log('WARN: scenario %r: cannot load template (%s)' % (s.name, e))
                continue
            if s.template or s.points:
                enabled.append(s)
            else:
                self.log('WARN: scenario %r has neither a template nor points' % s.name)

        self.log('engine started: %d active scenario(s): %s'
                 % (len(enabled), ', '.join(s.name for s in enabled) or '<none>'))
        if not enabled:
            self.log('nothing to do — enable a scenario with a template or points.')
            return

        last_check = {s.name: 0.0 for s in enabled}
        last_tap = {s.name: 0.0 for s in enabled}
        tick = 0.1

        while not self._stop.is_set():
            now = time.monotonic()
            due = [s for s in enabled if now - last_check[s.name] >= s.interval]
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
                scales = multi_scale() if s.multi_scale else None

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

                if s.points:
                    # Fixed-point tapping (menu buttons at known positions).
                    if now - last_tap[s.name] < s.cooldown:
                        continue
                    if s.action == 'tap':
                        for (x, y) in s.points:
                            device.tap_xy(int(x), int(y))
                        self.log('  TAP   %-16s %d point(s)' % (s.name, len(s.points)))
                    last_tap[s.name] = now
                    continue

                if s.rotate:
                    m = find_rotated(screen, get_tpl(s.template),
                                     step=s.rotate, threshold=s.threshold,
                                     downscale=(s.downscale or 1.0),
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
                    if now - last_tap[s.name] < s.cooldown:
                        self.log('  skip tap (%s on cooldown)' % s.name)
                        continue
                    device.tap_point(m.center)
                    last_tap[s.name] = time.monotonic()
                    self.log('  TAP   %s at %s' % (s.name, m.center))

            time.sleep(tick)

        self.log('engine stopped')
