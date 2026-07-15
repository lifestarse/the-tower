"""Report which templates are visible on the current device screen.

Like run_state_check.py, but it uses image_recognition instead of the histogram
state classifier. For every templates/*.png it prints the best match confidence
and center, and writes an annotated screenshot to matches.png so you can see
exactly what was located.

    python run_template_demo.py
"""
import cv2
import numpy as np

from android_device import AndroidDevice
from image_recognition import TemplateLibrary, find_template, multi_scale

DEVICE_ID = 'emulator-5554'
THRESHOLD = 0.8


def main():
    lib = TemplateLibrary('templates')
    if len(lib) == 0:
        print('No templates in templates/. Add some button crops first '
              '(see capture_screen.py and templates/README.md).')
        return

    device = AndroidDevice(DEVICE_ID)
    screen = device.capture()
    annotated = cv2.cvtColor(np.array(screen.convert('RGB')), cv2.COLOR_RGB2BGR)

    for name in lib.names():
        m = find_template(screen, lib[name], threshold=THRESHOLD, scales=multi_scale())
        if m:
            print('FOUND  %-20s conf=%.3f center=%s' % (name, m.confidence, m.center))
            x1, y1, x2, y2 = m.box
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), 2)
            cv2.putText(annotated, name, (x1, max(12, y1 - 6)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        else:
            print('  --   %-20s (not found)' % name)

    cv2.imwrite('matches.png', annotated)
    print('annotated screenshot -> matches.png')


if __name__ == '__main__':
    main()
