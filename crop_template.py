"""Crop a rectangle out of a saved screenshot into templates/<name>.png.

    python crop_template.py screen.png retry 180 1000 100 30
                            <src>      <name> x   y    w   h

The (x, y, w, h) convention matches the rectangles used in state.py. Grab a
screenshot first with capture_screen.py, read the pixel coordinates of the
button in an image editor, then run this to save the crop.
"""
import os
import sys

from PIL import Image


def main():
    if len(sys.argv) != 7:
        print(__doc__)
        raise SystemExit(2)
    src = sys.argv[1]
    name = sys.argv[2]
    x, y, w, h = map(int, sys.argv[3:7])

    img = Image.open(src)
    crop = img.crop((x, y, x + w, y + h))
    os.makedirs('templates', exist_ok=True)
    out = os.path.join('templates', name + '.png')
    crop.save(out)
    print('saved %s (%dx%d)' % (out, crop.width, crop.height))


if __name__ == '__main__':
    main()
