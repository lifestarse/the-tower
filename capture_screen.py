"""Save the current device screenshot to a PNG so you can crop button templates.

    python capture_screen.py                 -> screen.png
    python capture_screen.py my_screen.png   -> my_screen.png

Open the PNG in any image editor, crop a tight rectangle around the button you
want to detect, and save it as templates/<name>.png. That <name> is the key you
pass to the TemplateLibrary (e.g. templates/retry.png -> lib['retry']).
"""
import sys

from android_device import AndroidDevice

DEVICE_ID = 'emulator-5554'


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else 'screen.png'
    device = AndroidDevice(DEVICE_ID)
    img = device.capture()
    img.save(out)
    print('saved %s (%dx%d)' % (out, img.width, img.height))


if __name__ == '__main__':
    main()
