"""
Image recognition (template matching) for The Tower automation.

Where `state.py` classifies the *whole* screen with colour histograms on fixed
rectangles, this module *locates* a given button / icon anywhere on the
screenshot and returns its position. Taps therefore keep working even when an
element moves or the emulator resolution differs from the reference images.

Backed by OpenCV template matching (`TM_CCOEFF_NORMED`) with optional
multi-scale search and a confidence threshold.

Quick use:

    from image_recognition import TemplateLibrary, locate_and_tap

    lib = TemplateLibrary('templates')          # loads templates/*.png once
    screen = device.capture()                   # PIL.Image from AndroidDevice
    if locate_and_tap(device, screen, lib['retry']):
        print('tapped retry')

Run `python image_recognition.py` to self-test the matching logic without a
device or the game (it synthesises an image and finds a known patch in it).
"""

from __future__ import annotations

import os
import glob

import numpy as np
import cv2
from PIL import Image


# --------------------------------------------------------------------------- #
# Image coercion
# --------------------------------------------------------------------------- #
def to_gray(image) -> np.ndarray:
    """Coerce a PIL.Image, a file path, or an ndarray into a grayscale ndarray.

    ndarray input is assumed to be RGB (the order produced by
    ``np.array(pil_image)``), matching what AndroidDevice.capture() yields.
    """
    if isinstance(image, str):
        arr = cv2.imread(image, cv2.IMREAD_GRAYSCALE)
        if arr is None:
            raise FileNotFoundError('template not found or unreadable: %s' % image)
        return arr
    if isinstance(image, Image.Image):
        arr = np.array(image.convert('RGB'))
        return cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    if isinstance(image, np.ndarray):
        if image.ndim == 3:
            return cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
        return image
    raise TypeError('unsupported image type: %r' % type(image))


# --------------------------------------------------------------------------- #
# Match result
# --------------------------------------------------------------------------- #
class Match:
    """A single template hit inside a screenshot."""

    __slots__ = ('confidence', 'top_left', 'size')

    def __init__(self, confidence: float, top_left, size):
        self.confidence = float(confidence)
        self.top_left = (int(top_left[0]), int(top_left[1]))   # (x, y)
        self.size = (int(size[0]), int(size[1]))               # (w, h)

    @property
    def center(self):
        x, y = self.top_left
        w, h = self.size
        return (x + w // 2, y + h // 2)

    @property
    def box(self):
        """(x1, y1, x2, y2) — usable directly with PIL's Image.crop()."""
        x, y = self.top_left
        w, h = self.size
        return (x, y, x + w, y + h)

    def __repr__(self):
        return 'Match(conf=%.3f, center=%s, size=%s)' % (
            self.confidence, self.center, self.size)


# --------------------------------------------------------------------------- #
# Core matching
# --------------------------------------------------------------------------- #
def find_template(screen, template, threshold: float = 0.8, scales=None):
    """Return the best :class:`Match` for ``template`` in ``screen``, or None.

    Parameters
    ----------
    screen, template : PIL.Image | path | ndarray
    threshold : float
        Minimum normalised correlation (0..1) to accept. 0.8 is a sane start;
        raise it if you get false hits, lower it if a real button is missed.
    scales : iterable of float, optional
        Template scale factors to try, for when the screenshot resolution does
        not match the resolution the template was cropped at. Use
        ``multi_scale()`` for a reasonable spread. Defaults to ``[1.0]``.
    """
    screen_g = to_gray(screen)
    template_g = to_gray(template)
    sh, sw = screen_g.shape[:2]

    best = None
    for scale in (scales if scales is not None else (1.0,)):
        if scale == 1.0:
            t = template_g
        else:
            t = cv2.resize(template_g, None, fx=scale, fy=scale,
                           interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
        th, tw = t.shape[:2]
        if th > sh or tw > sw or th < 4 or tw < 4:
            continue  # template bigger than screen (or degenerate) — skip
        res = cv2.matchTemplate(screen_g, t, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        if best is None or max_val > best.confidence:
            best = Match(max_val, max_loc, (tw, th))

    if best is not None and best.confidence >= threshold:
        return best
    return None


def find_all_templates(screen, template, threshold: float = 0.8):
    """Return every non-overlapping :class:`Match` at/above ``threshold``.

    Useful for counting or acting on repeated elements (e.g. several identical
    upgrade buttons). Sorted by confidence, highest first.
    """
    screen_g = to_gray(screen)
    template_g = to_gray(template)
    th, tw = template_g.shape[:2]
    res = cv2.matchTemplate(screen_g, template_g, cv2.TM_CCOEFF_NORMED)

    ys, xs = np.where(res >= threshold)
    raw = [Match(res[y, x], (x, y), (tw, th)) for y, x in zip(ys, xs)]
    raw.sort(key=lambda m: m.confidence, reverse=True)

    # Greedy non-maximum suppression: keep a hit only if its center is not
    # already covered by a stronger, earlier hit.
    kept = []
    for m in raw:
        cx, cy = m.center
        if all(abs(cx - k.center[0]) > tw // 2 or abs(cy - k.center[1]) > th // 2
               for k in kept):
            kept.append(m)
    return kept


def multi_scale(low: float = 0.8, high: float = 1.2, steps: int = 9):
    """A spread of scale factors for :func:`find_template`'s ``scales`` arg."""
    return [round(float(s), 4) for s in np.linspace(low, high, steps)]


def _rotate_gray(gray: np.ndarray, angle: float) -> np.ndarray:
    """Rotate a grayscale image by ``angle`` degrees, expanding the canvas so
    no corner is clipped (empty area filled black)."""
    h, w = gray.shape[:2]
    cx, cy = w / 2.0, h / 2.0
    m = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw, nh = int(h * sin + w * cos), int(h * cos + w * sin)
    m[0, 2] += nw / 2.0 - cx
    m[1, 2] += nh / 2.0 - cy
    return cv2.warpAffine(gray, m, (nw, nh), flags=cv2.INTER_LINEAR, borderValue=0)


def find_rotated(screen, template, step: int = 15, threshold: float = 0.7,
                 scales=None, downscale: float = 1.0, roi=None):
    """Like :func:`find_template`, but tries the template at every rotation
    (0, step, 2*step, ... < 360). For items that spin, such as the orbiting
    diamond pickup, whose orientation changes as they move.

    ``downscale`` (<1) shrinks screen and template before matching for speed —
    0.5 is ~4x faster and usually keeps enough detail. ``roi`` is an optional
    ``(x0, y0, x1, y1)`` box to restrict (and speed up) the search.

    Returns the best :class:`Match` (in full-resolution screen coordinates) at or
    above ``threshold``, else None. Costlier than find_template (≈ 360/step
    matches), so keep the template small and the interval modest.
    """
    screen_g = to_gray(screen)
    ox, oy = 0, 0
    if roi:
        x0, y0, x1, y1 = roi
        ox, oy = x0, y0
        screen_g = screen_g[y0:y1, x0:x1]
    base = to_gray(template)

    ds = float(downscale)
    if ds != 1.0:
        screen_g = cv2.resize(screen_g, None, fx=ds, fy=ds, interpolation=cv2.INTER_AREA)
        base = cv2.resize(base, None, fx=ds, fy=ds, interpolation=cv2.INTER_AREA)

    sh, sw = screen_g.shape[:2]
    best = None
    for angle in range(0, 360, max(1, int(step))):
        t0 = base if angle == 0 else _rotate_gray(base, angle)
        for scale in (scales if scales is not None else (1.0,)):
            t = t0 if scale == 1.0 else cv2.resize(
                t0, None, fx=scale, fy=scale,
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
            th, tw = t.shape[:2]
            if th > sh or tw > sw or th < 4 or tw < 4:
                continue
            res = cv2.matchTemplate(screen_g, t, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if best is None or max_val > best[0]:
                best = (max_val, max_loc, (tw, th))

    if best is None or best[0] < threshold:
        return None
    inv = 1.0 / ds
    (mv, (lx, ly), (tw, th)) = best
    return Match(mv, (ox + lx * inv, oy + ly * inv), (tw * inv, th * inv))


# --------------------------------------------------------------------------- #
# Device integration
# --------------------------------------------------------------------------- #
def locate_and_tap(device, screen, template, threshold: float = 0.8, scales=None):
    """Find ``template`` in ``screen`` and tap its center on ``device``.

    ``device`` is an :class:`android_device.AndroidDevice`. Returns the
    :class:`Match` that was tapped, or None if nothing matched.
    """
    m = find_template(screen, template, threshold=threshold, scales=scales)
    if m is not None:
        device.tap_point(m.center)
    return m


# --------------------------------------------------------------------------- #
# Template library
# --------------------------------------------------------------------------- #
class TemplateLibrary:
    """Loads every ``*.png`` in a directory once and serves them by stem name.

        lib = TemplateLibrary('templates')
        lib['retry']          # grayscale ndarray for templates/retry.png
        list(lib.names())     # -> ['retry', 'upgrade_damage', ...]
    """

    def __init__(self, directory: str = 'templates'):
        self.directory = directory
        self._templates = {}
        for path in sorted(glob.glob(os.path.join(directory, '*.png'))):
            name = os.path.splitext(os.path.basename(path))[0]
            self._templates[name] = to_gray(path)

    def names(self):
        return self._templates.keys()

    def __contains__(self, name):
        return name in self._templates

    def __getitem__(self, name):
        try:
            return self._templates[name]
        except KeyError:
            raise KeyError(
                "no template %r in %r (have: %s)"
                % (name, self.directory, ', '.join(self.names()) or '<none>'))

    def __len__(self):
        return len(self._templates)


# --------------------------------------------------------------------------- #
# Self-test (no device / game required)
# --------------------------------------------------------------------------- #
def _selftest():
    rng = np.random.default_rng(0)
    screen = rng.integers(0, 255, size=(1280, 720, 3), dtype=np.uint8)

    # Carve a distinctive patch out of the screen and treat it as the template.
    tx, ty, tw, th = 300, 900, 80, 40
    patch = screen[ty:ty + th, tx:tx + tw].copy()
    template = Image.fromarray(patch, mode='RGB')

    m = find_template(screen, template, threshold=0.9)
    assert m is not None, 'template not found'
    assert m.center == (tx + tw // 2, ty + th // 2), \
        'center mismatch: %s vs %s' % (m.center, (tx + tw // 2, ty + th // 2))

    # Scaled screen: template must still be found via multi_scale search.
    small = cv2.resize(screen, None, fx=0.85, fy=0.85, interpolation=cv2.INTER_AREA)
    m2 = find_template(small, template, threshold=0.7, scales=multi_scale())
    assert m2 is not None, 'scaled template not found'

    # find_all_templates on a screen with two copies of the patch.
    twin = screen.copy()
    twin[100:100 + th, 200:200 + tw] = patch
    hits = find_all_templates(twin, template, threshold=0.95)
    assert len(hits) >= 2, 'expected >=2 hits, got %d' % len(hits)

    print('image_recognition self-test OK '
          '(best=%.3f, scaled=%.3f, hits=%d)'
          % (m.confidence, m2.confidence, len(hits)))


if __name__ == '__main__':
    _selftest()
