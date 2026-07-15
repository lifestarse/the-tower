# Templates

Button / icon crops used by `image_recognition.py` (template matching). Each
file here is a small PNG of one on-screen element; the matcher locates it in the
live screenshot and taps its center, so taps keep working even when the element
moves or the emulator resolution differs from the reference.

## Naming

`templates/<name>.png` is served by `TemplateLibrary` under `<name>`:

```python
lib = TemplateLibrary('templates')
lib['retry']        # -> templates/retry.png
```

`run_smart_automation.py` looks for these names (all optional):

| name             | what to crop                                  |
|------------------|-----------------------------------------------|
| `claim`          | a claim / collect button (rewards, gems, ...) |
| `retry`          | the retry button on the game-over screen      |
| `upgrade_damage` | an in-run "damage" upgrade button             |
| `upgrade_speed`  | an in-run "attack speed" upgrade button       |

Add as many of your own as you like and reference them from your scripts.

## How to make a template

1. Get a screenshot from the running game:
   ```shell
   python capture_screen.py screen.png
   ```
2. Open `screen.png`, find the pixel box of the button (x, y, width, height).
3. Crop it into this folder:
   ```shell
   python crop_template.py screen.png retry 180 1000 100 30
   ```
4. Check what the matcher sees:
   ```shell
   python run_template_demo.py      # prints confidences, writes matches.png
   ```

## Tips

- Crop **tight** around the distinctive part of the button. Avoid backgrounds
  that change (progress bars, animated glows, counters).
- Keep it grayscale-distinguishable — matching runs on luminance.
- If a real button is missed, lower the threshold a little; if you get false
  hits, raise it. Defaults: 0.8 (demo), 0.82 (automation).
- If the template was cropped at a different resolution than the live screen,
  pass `scales=multi_scale()` (the demo and automation already do).
