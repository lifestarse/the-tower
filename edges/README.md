# edges — button crops for transition-graph edges

Each `e<NNN>.png` here is a cropped picture of the button that triggers one
graph edge (see `screen_graph.py` / `transitions.json`). At navigation time the
button is re-located live with `find_template`, so a template-keyed edge keeps
working when the button moves or the emulator resolution changes (a fractional
`xy_frac` is stored as a fallback).

These files are produced automatically by the recorder's `crop` command
(`python screen_graph.py --record`). You normally never touch them by hand.
