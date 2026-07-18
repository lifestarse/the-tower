import subprocess as sp
import io
import re
from ppadb.client import Client as AdbClient
from PIL import Image

from humanize import jitter_point


def _ensure_server_and_connect(device_id):
    # Start (do NOT kill) the adb server. `adb kill-server` drops TCP
    # connections to networked emulators like BlueStacks, so the old
    # kill-server-on-import would sever this very device.
    sp.run(['adb', 'start-server'], stdout=sp.PIPE, stderr=sp.PIPE)
    # Networked emulators (BlueStacks, MEmu, LDPlayer, ...) are reached over
    # TCP as host:port and must be explicitly (re)connected after a server
    # start. A plain "emulator-5554" is auto-discovered, so skip it there.
    if ':' in device_id:
        sp.run(['adb', 'connect', device_id], stdout=sp.PIPE, stderr=sp.PIPE)


class AndroidDevice:
    def __init__(self, device_id):
        _ensure_server_and_connect(device_id)
        adb = AdbClient()
        self.device = adb.device(device_id)
        if self.device is None:
            raise RuntimeError(
                'adb has no device %r. Enable ADB in BlueStacks, then check '
                '`adb connect %s` and `adb devices`.' % (device_id, device_id))

    def capture(self):
        screen = self.device.screencap()
        im_bytes = io.BytesIO(screen)
        im = Image.open(im_bytes)
        return im

    def tap_xy(self, x, y, jitter=True):
        # Human-like wobble by default so every tap lands on a slightly different
        # pixel of the button. Pass jitter=False for a pixel-exact tap.
        if jitter:
            x, y = jitter_point(x, y)
        self.device.input_tap(int(x), int(y))

    def tap_point(self, point, jitter=True):
        self.tap_xy(point[0], point[1], jitter=jitter)

    def swipe_xy(self, x1, y1, x2, y2, duration):
        self.device.input_swipe(x1, y1, x2, y2, duration)

    def swipe_point(self, p1, p2, duration):
        self.device.input_swipe(p1[0], p1[1], p2[0], p2[1], duration)

    def back(self):
        self.device.input_keyevent(4)

    def get_focused_package(self):
        """Package of the currently focused window — i.e. what's actually on
        screen. More reliable than ppadb's get_top_activity() on emulators like
        BlueStacks, whose persistent launcher (com.uncube.launcher3) is
        otherwise misreported as the top activity. Returns None if unknown.
        """
        out = self.device.shell('dumpsys window')
        for key in ('mCurrentFocus', 'mFocusedApp'):
            m = re.search(key + r'=[^\n]*?\s([A-Za-z][\w.]+)/', out)
            if m:
                return m.group(1)
        return None

    def get_top_activity_package(self):
        # Prefer the focused window (correct on BlueStacks); fall back to
        # ppadb's top-activity only if the focus can't be parsed.
        pkg = self.get_focused_package()
        if pkg:
            return pkg
        top = self.device.get_top_activity()
        return top.package if top else None
