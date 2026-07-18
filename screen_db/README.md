# screen_db — whole-screen menu database

Each `*.png` here is one reference screen. **The file name (without `.png`) is the
label** the matcher returns — e.g. `perk_select.png` → menu `perk_select`.

## Build it (learning / test mode)

```
python menu_db.py --learn
```

Walk the game through its menus while this runs. Every frame that matches nothing
already in this folder is saved as `new_001.png`, `new_002.png`, … Then just
**rename** those files to meaningful labels (`main_menu.png`, `game_over.png`, …).
Delete any accidental duplicates or transition frames.

## Use it

```
python menu_db.py --identify     # print the current menu once
python menu_db.py --watch        # print the current menu live
python menu_db.py --list         # list every screen in this folder
```

In the scenario engine, macro steps `whichmenu` and `if_menu` use this DB:

```json
{"do": "if_menu", "menu": "perk_select", "steps": [ {"do": "pick_perk"} ]}
{"do": "whichmenu"}
```

## Tuning

Matching compares a 64×64 grayscale thumbnail by mean-absolute-difference
(0 = identical). Two frames count as the same screen when the distance is
≤ `--threshold` (default `10`, see `MATCH_MAD` in `menu_db.py`). Same screen with
ticking numbers is usually 0–3; different menus 20–60+. Raise the threshold to
tolerate more animation, lower it to split near-identical screens.

Notes:
- The thumbnail is resolution-independent, so a DB built at one emulator size
  still matches at another.
- Pure high-frequency detail is averaged away by the downscale — recognition
  keys on the large-scale layout/colours of a screen, which is what separates
  menus anyway.
