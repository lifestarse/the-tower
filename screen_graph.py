"""
screen_graph.py — a UI screen-transition GRAPH for The Tower automation.

Where menu_db.py answers "which screen am I on", this answers "how do I GET to
screen X from here". Nodes are screen labels; edges are transitions
("from screen A, doing ACTION lands on screen B"). The engine can then navigate:
give it a target and it BFS-routes there, tapping the buttons on each hop and
re-checking after every step so it self-corrects.

Design (chosen by a design bake-off; see the transition-graph spec):
  * Base = a thin flat graph (transitions.json) + BFS + a single `goto` step.
  * Composite node identity — the decisive robustness fix. classify() layers the
    two recognizers the repo already has: screen_state.identify_screen (precise,
    template-signature) FIRST, then menu_db.identify_menu (whole-frame) as a
    fallback, and any node may declare `anchors` (templates that MUST be found)
    to split look-alike siblings (e.g. the Attack vs Defense upgrade tabs) and
    reject animation-flipped false matches.
  * settle() waits for a stable LABEL, not stable pixels, so it converges on the
    perpetually-animated battlefield (whose anchors keep its label rock-steady).
  * Edges replay by TEMPLATE (re-locate the button live -> resolution robust),
    with a fractional xy fallback. Navigation verifies every hop and re-plans
    from the screen it actually landed on; a bounded back()-recovery ladder
    unwinds when it gets lost. Edges carry observed counts + an `alt` map, so a
    button that sometimes opens an ad self-heals.

Imports stay ppadb-free (menu_db / image_recognition / screen_state / humanize),
so `import screen_graph` never needs a device — AndroidDevice is imported only
inside the CLI main(). This preserves the engine's "importable without ppadb".

CLI:
    python screen_graph.py --record            # interactive recorder (safe)
    python screen_graph.py --goto perk_select  # navigate to a screen
    python screen_graph.py --explore --safe templates/menu_close_x.png ...
    python screen_graph.py --show              # print the graph
    python screen_graph.py --selftest          # verify graph logic (no device)

Engine macro steps (see automation_engine._run_step):
    {"do": "goto", "menu": "home"}
    {"do": "explore", "safe": ["templates/menu_close_x.png"], "budget": 150}
"""
import argparse
import json
import os
import sys
import time
from collections import deque

from image_recognition import find_template, multi_scale, Match
import menu_db
import screen_state
from humanize import human_sleep

try:
    sys.stdout.reconfigure(encoding='utf-8')      # console may default to cp1251
except (AttributeError, ValueError):
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
GRAPH_FILE = os.path.join(HERE, 'transitions.json')
EDGES_DIR = os.path.join(HERE, 'edges')
DEVICE_ID = '127.0.0.1:5556'
APP_PACKAGE = 'com.TechTreeGames.TheTower'
REF_RESOLUTION = [1600, 900]


# --------------------------------------------------------------------------- #
# Graph storage (a single flat transitions.json; button crops in edges/)
# --------------------------------------------------------------------------- #
def _empty_graph():
    return {'version': 1, 'ref_resolution': list(REF_RESOLUTION),
            'nodes': {}, 'edges': []}


def load_graph(path=GRAPH_FILE):
    """Load transitions.json, returning an empty skeleton if it is missing."""
    try:
        g = json.load(open(path, encoding='utf-8'))
    except Exception:  # noqa: BLE001 — missing/corrupt file -> start fresh
        return _empty_graph()
    g.setdefault('version', 1)
    g.setdefault('ref_resolution', list(REF_RESOLUTION))
    g.setdefault('nodes', {})
    g.setdefault('edges', [])
    return g


def save_graph(graph, path=GRAPH_FILE):
    """Atomically write the graph (tmp + os.replace). If the file changed under
    us (a recorder session vs the engine), merge instead of clobbering."""
    if os.path.exists(path):
        try:
            disk = json.load(open(path, encoding='utf-8'))
            graph = _merge_graphs(disk, graph)
        except Exception:  # noqa: BLE001
            pass
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return graph


def _merge_graphs(disk, mem):
    """Union two graphs by edge identity (from, action-key, to). Counts take the
    MAX of the two sides (safe against double counting), nodes are unioned with
    the in-memory copy winning. Preserves edges that exist only on one side."""
    out = {'version': mem.get('version', 1),
           'ref_resolution': mem.get('ref_resolution', list(REF_RESOLUTION)),
           'nodes': {}, 'edges': []}
    out['nodes'].update(disk.get('nodes', {}))
    out['nodes'].update(mem.get('nodes', {}))
    by_key = {}
    for src in (disk.get('edges', []), mem.get('edges', [])):
        for e in src:
            k = (e.get('from'), _action_key(e.get('action', {})), e.get('to'))
            if k not in by_key:
                by_key[k] = dict(e)
            else:
                cur = by_key[k]
                cur['count'] = max(float(cur.get('count', 0)), float(e.get('count', 0)))
                alt = dict(cur.get('alt') or {})
                for t, v in (e.get('alt') or {}).items():
                    alt[t] = max(float(alt.get(t, 0)), float(v))
                cur['alt'] = alt
    out['edges'] = list(by_key.values())
    maxid = max((e['id'] for e in out['edges'] if isinstance(e.get('id'), int)),
                default=0)
    for e in out['edges']:
        if not isinstance(e.get('id'), int):
            maxid += 1
            e['id'] = maxid
    return out


def _next_edge_id(graph):
    return max((e['id'] for e in graph['edges'] if isinstance(e.get('id'), int)),
               default=0) + 1


# --------------------------------------------------------------------------- #
# Composite screen identity  (the robustness fix)
# --------------------------------------------------------------------------- #
def _anchors_ok(capture, node):
    """True unless the node declares `anchors` templates that are NOT all on
    screen. This is what forces two look-alike screens apart when asked to."""
    if not node:
        return True
    anchors = node.get('anchors') or []
    if not anchors:
        return True
    th = float(node.get('anchor_threshold', 0.85))
    for tpl in anchors:
        try:
            if find_template(capture, tpl, threshold=th) is None:
                return False
        except Exception:  # noqa: BLE001 — a missing anchor template = fail closed
            return False
    return True


def classify(capture, graph, db=None, screens=None):
    """Composite node identity: screen_state (precise) -> menu_db (whole-frame)
    -> per-node anchors. Returns {name, matched, via, distance, confidence}."""
    nodes = graph.get('nodes', {})

    try:
        ss = screen_state.identify_screen(capture, screens)
    except Exception:  # noqa: BLE001
        ss = {'matched': False, 'name': 'unknown', 'confidence': 0.0}
    if ss.get('matched') and _anchors_ok(capture, nodes.get(ss['name'])):
        return {'name': ss['name'], 'matched': True, 'via': 'screen_state',
                'confidence': ss.get('confidence'), 'distance': None}

    try:
        md = menu_db.identify_menu(capture, db=db)
    except Exception:  # noqa: BLE001
        md = {'matched': False, 'name': 'unknown', 'distance': None, 'closest': None}
    if md.get('matched') and _anchors_ok(capture, nodes.get(md['name'])):
        return {'name': md['name'], 'matched': True, 'via': 'menu_db',
                'distance': md.get('distance'), 'confidence': None}

    return {'name': 'unknown', 'matched': False, 'via': None,
            'distance': md.get('distance'), 'confidence': ss.get('confidence'),
            'closest': md.get('closest') or ss.get('closest')}


def settle(device, graph, db=None, screens=None, *, timeout=2.5, poll=0.15, stable=2):
    """Poll capture()+classify() until the LABEL is the same non-'unknown' value
    `stable` times in a row (or timeout). Label-stability, not pixel-stability,
    is what lets this converge on the animated battlefield. Returns (capture,
    classify_info)."""
    deadline = time.monotonic() + timeout
    last, count = None, 0
    cap = device.capture()
    info = classify(cap, graph, db=db, screens=screens)
    while True:
        lbl = info['name']
        if lbl != 'unknown':
            count = count + 1 if lbl == last else 1
            if count >= stable:
                return cap, info
        else:
            count = 0
        last = lbl
        if time.monotonic() >= deadline:
            return cap, info
        time.sleep(poll)
        cap = device.capture()
        info = classify(cap, graph, db=db, screens=screens)


# --------------------------------------------------------------------------- #
# Edges: action key, observe/reinforce, pathfinding, replay
# --------------------------------------------------------------------------- #
def _action_key(action):
    """A hashable identity for an action so near-identical taps merge into one
    edge instead of spawning duplicates (fractional taps quantized to a 0.02 grid)."""
    kind = action.get('kind', 'tap')
    if kind == 'template':
        t = action.get('template') or ''
        return (kind, os.path.splitext(os.path.basename(str(t)))[0])
    if kind == 'tap':
        xy = action.get('xy_frac') or [0.0, 0.0]
        return (kind, round(float(xy[0]) / 0.02) * 0.02, round(float(xy[1]) / 0.02) * 0.02)
    if kind == 'swipe':
        return (kind, tuple(round(float(v), 2) for v in (action.get('vector') or [])))
    return (kind,)


def observe_hop(graph, frm, action, to):
    """Add or reinforce the edge (frm, action) -> to. If a matching (frm, action)
    edge exists: bump its count when `to` matches, else record `to` under alt.
    Otherwise append a new edge. Returns the edge."""
    key = _action_key(action)
    for e in graph['edges']:
        if e.get('from') == frm and _action_key(e.get('action', {})) == key:
            if to == e.get('to'):
                e['count'] = float(e.get('count', 0.0)) + 1.0
            else:
                alt = e.setdefault('alt', {})
                alt[to] = float(alt.get(to, 0.0)) + 1.0
            return e
    edge = {'id': _next_edge_id(graph), 'from': frm, 'to': to,
            'action': action, 'count': 1.0, 'alt': {}}
    graph['edges'].append(edge)
    return edge


def _reliability(edge):
    c = float(edge.get('count', 1.0))
    alt = sum(float(v) for v in (edge.get('alt') or {}).values())
    return c / (c + alt) if (c + alt) > 0 else 0.0


def build_adj(edges):
    adj = {}
    for e in edges:
        adj.setdefault(e.get('from'), []).append(e)
    return adj


def find_path(edges, start, goal):
    """BFS for the shortest hop-count path start->goal. Among edges out of a node
    the more reliable (higher count / lower alt) are expanded first, so
    deterministic hops are preferred. Returns the ordered edge list, [] if
    already there, or None if unreachable."""
    if start == goal:
        return []
    adj = build_adj(edges)
    prev = {start: None}
    q = deque([start])
    while q:
        node = q.popleft()
        for e in sorted(adj.get(node, []), key=_reliability, reverse=True):
            nxt = e.get('to')
            if nxt in prev:
                continue
            prev[nxt] = (node, e)
            if nxt == goal:
                path = []
                cur = goal
                while prev[cur] is not None:
                    p, edge = prev[cur]
                    path.append(edge)
                    cur = p
                path.reverse()
                return path
            q.append(nxt)
    return None


def _find_in_roi(cap, tpl, th, scales, roi, width, height):
    """find_template, optionally restricted to a fractional [x0,y0,x1,y1] band
    (to pick the right instance of a button that appears more than once)."""
    if not roi:
        return find_template(cap, tpl, threshold=th, scales=scales)
    x0, y0 = int(width * roi[0]), int(height * roi[1])
    x1, y1 = int(width * roi[2]), int(height * roi[3])
    m = find_template(cap.crop((x0, y0, x1, y1)), tpl, threshold=th, scales=scales)
    if m is None:
        return None
    lx, ly = m.top_left
    return Match(m.confidence, (lx + x0, ly + y0), m.size)   # shift back to full frame


def execute_edge(device, edge, width, height, log):
    """Replay one edge's single action on the device. Taps are jittered (via
    AndroidDevice); swipes are NOT (a horizontal wobble on the upgrade list would
    buy a cell)."""
    action = edge.get('action', {})
    kind = action.get('kind', 'tap')

    if kind == 'back':
        device.back()
        return
    if kind == 'swipe':
        v = action.get('vector', [0.5, 0.75, 0.5, 0.4])
        dur = int(action.get('dur', 400))
        device.swipe_xy(int(width * v[0]), int(height * v[1]),
                        int(width * v[2]), int(height * v[3]), dur)
        return
    if kind == 'template':
        tpl = action.get('template')
        th = float(action.get('threshold', 0.80))
        scales = multi_scale() if action.get('multiscale') else None
        try:
            m = _find_in_roi(device.capture(), tpl, th, scales, action.get('roi'),
                             width, height)
        except Exception as e:  # noqa: BLE001
            log('    goto: template match error (%s)' % e)
            m = None
        if m is not None:
            cx, cy = m.center
            off = action.get('tap_offset') or [0.0, 0.0]
            cx += int(off[0] * m.size[0])
            cy += int(off[1] * m.size[1])
            device.tap_xy(cx, cy)
            return
        log('    goto: button %s not found, falling back to xy_frac'
            % os.path.basename(str(tpl)))
    # kind == 'tap', or template fallback
    xy = action.get('xy_frac')
    if xy:
        device.tap_xy(int(width * xy[0]), int(height * xy[1]))
    else:
        log('    goto: edge %s has no usable tap target' % edge.get('id'))


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #
def _relaunch(device, log):
    try:
        device.device.shell('monkey -p %s -c android.intent.category.LAUNCHER 1'
                            % APP_PACKAGE)
        human_sleep(2.0)
        return True
    except Exception as e:  # noqa: BLE001
        log('  goto: relaunch failed: %s' % e)
        return False


def _recover(device, graph, db, screens, log, stop, tries=2):
    """Unwind an unknown/lost screen with a bounded back() ladder, re-classifying
    after each. Returns True if it lands on a known screen."""
    for _ in range(tries):
        if stop is not None and stop.is_set():
            return False
        device.back()
        human_sleep(0.6)
        _cap, info = settle(device, graph, db=db, screens=screens)
        if info['name'] != 'unknown':
            log('  goto: recovered to %r via back()' % info['name'])
            return True
    return False


def goto(device, target, log=print, *, graph=None, db=None, screens=None,
         max_hops=8, allow_relaunch=False, stop=None):
    """Navigate to `target`. Each iteration: settle -> classify -> if there, done;
    else BFS a path from the ACTUAL current screen, fire the first edge, settle,
    reinforce the observed hop, and loop (re-planning from wherever it landed).
    Guards: max_hops cap and a repeated-screen loop-breaker. Returns True on
    arrival."""
    if graph is None:
        graph = load_graph()
    edges = graph['edges']
    seen = {}
    for _hop in range(max_hops):
        if stop is not None and stop.is_set():
            return False
        cap, info = settle(device, graph, db=db, screens=screens)
        cur = info['name']
        if cur == target:
            return True
        if cur == 'unknown':
            if _recover(device, graph, db, screens, log, stop):
                continue
            if allow_relaunch and _relaunch(device, log):
                continue
            log('  goto: lost on an unknown screen (closest %s), aborting'
                % info.get('closest'))
            return False
        seen[cur] = seen.get(cur, 0) + 1
        if seen[cur] >= 3:
            log('  goto: stuck on %r (seen 3x), aborting' % cur)
            return False
        path = find_path(edges, cur, target)
        if not path:
            log('  goto: no known path %s -> %s' % (cur, target))
            return False
        edge = path[0]
        w, h = cap.size
        log('  goto: %s --%s--> %s' % (cur, edge['action'].get('kind'), edge['to']))
        execute_edge(device, edge, w, h, log)
        human_sleep(float(edge.get('settle', 0.4)))
        _cap2, info2 = settle(device, graph, db=db, screens=screens)
        got = info2['name']
        observe_hop(graph, cur, edge['action'], got)      # navigation reinforces
        if got != edge['to']:
            log('  goto: landed on %r (expected %r) — replanning' % (got, edge['to']))
    log('  goto: gave up after %d hops (target %r)' % (max_hops, target))
    return False


# --------------------------------------------------------------------------- #
# Interactive recorder
# --------------------------------------------------------------------------- #
def _next_crop_path():
    os.makedirs(EDGES_DIR, exist_ok=True)
    n = 1
    while os.path.exists(os.path.join(EDGES_DIR, 'e%03d.png' % n)):
        n += 1
    return os.path.join(EDGES_DIR, 'e%03d.png' % n), 'edges/e%03d.png' % n


def _tap_action(cap_before, px, py, w, h, pending_crop):
    """Build a tap edge action. If a crop is armed, harvest the button box into
    edges/ and key a TEMPLATE edge to it (with the tap point as xy fallback);
    otherwise a plain fractional-xy tap."""
    xy = [round(px / w, 4), round(py / h, 4)]
    box = pending_crop.get('box')
    if box:
        bx, by, bw, bh = box
        _abs, rel = _next_crop_path()
        cap_before.convert('RGB').crop((bx, by, bx + bw, by + bh)).save(_abs)
        return {'kind': 'template', 'template': rel, 'threshold': 0.80,
                'multiscale': True, 'xy_frac': xy}
    return {'kind': 'tap', 'xy_frac': xy}


def record(device, graph, log=print, *, db=None, screens=None):
    """Interactive REPL recorder (safe: only taps you type). See module docstring
    / --record help for commands."""
    if db is None:
        db = menu_db.load_db()
    if screens is None:
        try:
            screens = screen_state.load_screens()
        except Exception:  # noqa: BLE001
            screens = None
    pending_crop = {'box': None}
    last_edge = [None]

    _cap, info = settle(device, graph, db=db, screens=screens)
    before = info['name']
    log('recorder on %r. commands: where | x fx fy | p px py | t tpl | '
        'crop px py w h | swipe x0 y0 x1 y1 [dur] | back | roi x0 y0 x1 y1 | '
        'anchor tpl | name label | undo | save | quit' % before)

    while True:
        try:
            line = input('[%s] > ' % before).strip()
        except EOFError:
            break
        if not line:
            continue
        parts = line.split()
        cmd = parts[0].lower()

        if cmd in ('quit', 'q'):
            break
        if cmd == 'save':
            save_graph(graph)
            log('  saved (%d edges)' % len(graph['edges']))
            continue
        if cmd == 'where':
            _cap, info = settle(device, graph, db=db, screens=screens)
            before = info['name']
            log('  %s' % info)
            continue
        if cmd == 'anchor' and len(parts) >= 2:
            node = graph['nodes'].setdefault(before, {})
            node.setdefault('anchors', []).append(parts[1])
            log('  anchor %s -> node %r' % (parts[1], before))
            continue
        if cmd == 'name' and len(parts) >= 2:
            path = menu_db.add_screen(device.capture(), name=parts[1])
            db = menu_db.load_db()
            before = parts[1]
            log('  minted screen %s (label %r)' % (os.path.basename(path), parts[1]))
            continue
        if cmd == 'undo':
            if last_edge[0] in graph['edges']:
                graph['edges'].remove(last_edge[0])
                log('  removed last edge')
            last_edge[0] = None
            continue
        if cmd == 'roi' and len(parts) >= 5 and last_edge[0]:
            last_edge[0]['action']['roi'] = [float(x) for x in parts[1:5]]
            log('  roi attached to last edge')
            continue
        if cmd == 'crop' and len(parts) >= 5:
            pending_crop['box'] = tuple(int(float(x)) for x in parts[1:5])
            log('  crop armed (px py w h) — next tap keys a template edge')
            continue

        # ---- transition-causing actions ----
        cap_before = device.capture()
        w, h = cap_before.size
        action = None
        if cmd == 'x' and len(parts) >= 3:
            px, py = int(w * float(parts[1])), int(h * float(parts[2]))
            action = _tap_action(cap_before, px, py, w, h, pending_crop)
            device.tap_xy(px, py)
        elif cmd == 'p' and len(parts) >= 3:
            px, py = int(parts[1]), int(parts[2])
            action = _tap_action(cap_before, px, py, w, h, pending_crop)
            device.tap_xy(px, py)
        elif cmd == 't' and len(parts) >= 2:
            m = find_template(cap_before, parts[1], threshold=0.8, scales=multi_scale())
            if m is None:
                log('  template %s not on screen' % parts[1])
                continue
            action = {'kind': 'template', 'template': parts[1], 'threshold': 0.8,
                      'multiscale': True}
            device.tap_point(m.center)
        elif cmd == 'back':
            action = {'kind': 'back'}
            device.back()
        elif cmd == 'swipe' and len(parts) >= 5:
            v = [float(x) for x in parts[1:5]]
            dur = int(parts[5]) if len(parts) >= 6 else 400
            action = {'kind': 'swipe', 'vector': v, 'dur': dur}
            device.swipe_xy(int(w * v[0]), int(h * v[1]), int(w * v[2]), int(h * v[3]), dur)
        else:
            log('  ? unknown command')
            continue

        pending_crop['box'] = None
        _cap2, info2 = settle(device, graph, db=db, screens=screens)
        after = info2['name']
        if after == before:
            log('  no transition (%r): nothing recorded' % after)
        else:
            e = observe_hop(graph, before, action, after)
            last_edge[0] = e
            k = e['action'].get('template') or e['action'].get('kind')
            log('  %s -> %s  (edge %s, key %s, count %g)'
                % (before, after, e['id'], k, e.get('count', 1)))
            before = after

    save_graph(graph)
    log('recorder saved — %d edges total' % len(graph['edges']))


# --------------------------------------------------------------------------- #
# Optional bounded auto-explorer (accelerator — needs a safe-template allowlist)
# --------------------------------------------------------------------------- #
def explore(device, graph, log=print, *, safe, db=None, screens=None,
            budget=150, max_nodes=40, stop=None):
    """Bounded crawler: from each reachable node, tap ONLY the allowlisted `safe`
    templates, record where each leads, and backtrack with back(). Never taps
    anything outside `safe` (keep prestige/buy/watch-ad/claim OUT of it). Returns
    taps performed."""
    if not safe:
        log('  explore: refusing to run without a safe-template allowlist')
        return 0
    if db is None:
        db = menu_db.load_db()
    taps, visited = 0, set()
    _cap, info = settle(device, graph, db=db, screens=screens)
    frontier = [info['name']]
    while frontier and taps < budget and len(visited) < max_nodes:
        if stop is not None and stop.is_set():
            break
        node = frontier.pop()
        if node in visited or node == 'unknown':
            continue
        visited.add(node)
        if not goto(device, node, log, graph=graph, db=db, screens=screens, stop=stop):
            continue
        for tpl in safe:
            if stop is not None and stop.is_set():
                break
            try:
                if device.get_top_activity_package() != APP_PACKAGE:
                    log('  explore: left the app — stopping')
                    save_graph(graph)
                    return taps
            except Exception:  # noqa: BLE001
                pass
            cap = device.capture()
            m = find_template(cap, tpl, threshold=0.8, scales=multi_scale())
            if m is None:
                continue
            device.tap_point(m.center)
            taps += 1
            _c, info2 = settle(device, graph, db=db, screens=screens)
            dest = info2['name']
            if dest == 'unknown':
                menu_db.add_screen(device.capture())
                db = menu_db.load_db()
                _c, info3 = settle(device, graph, db=db, screens=screens)
                dest = info3['name']
            observe_hop(graph, node, {'kind': 'template', 'template': tpl,
                                      'threshold': 0.8, 'multiscale': True}, dest)
            if dest not in visited and dest != 'unknown':
                frontier.append(dest)
            device.back()
            settle(device, graph, db=db, screens=screens)
        save_graph(graph)
    log('  explore: %d tap(s), %d node(s) visited' % (taps, len(visited)))
    return taps


def relabel(graph, old, new):
    """Rename a node everywhere (edges from/to + the nodes map). Use after
    renaming a minted screen_db PNG to a meaningful label."""
    for e in graph['edges']:
        if e.get('from') == old:
            e['from'] = new
        if e.get('to') == old:
            e['to'] = new
    if old in graph.get('nodes', {}):
        graph['nodes'][new] = graph['nodes'].pop(old)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _show(graph):
    edges = graph['edges']
    nodes = set()
    for e in edges:
        nodes.add(e.get('from'))
        nodes.add(e.get('to'))
    print('graph: %d node(s), %d edge(s)' % (len(nodes), len(edges)))
    for e in sorted(edges, key=lambda x: (x.get('from') or '', x.get('to') or '')):
        a = e.get('action', {})
        key = a.get('template') or a.get('kind')
        print('  %-18s --%-8s--> %-18s  [%s]  count=%g%s'
              % (e.get('from'), a.get('kind'), e.get('to'), key, e.get('count', 1),
                 (' alt=%s' % e['alt']) if e.get('alt') else ''))
    if graph.get('nodes'):
        print('node refinements:')
        for name, meta in graph['nodes'].items():
            print('  %-18s %s' % (name, meta))


def main(argv=None):
    ap = argparse.ArgumentParser(description='UI screen-transition graph')
    ap.add_argument('--device', default=DEVICE_ID)
    ap.add_argument('--graph', default=GRAPH_FILE)
    ap.add_argument('--goto', metavar='SCREEN', help='navigate to a screen label')
    ap.add_argument('--record', action='store_true', help='interactive recorder')
    ap.add_argument('--explore', action='store_true', help='bounded auto-crawler')
    ap.add_argument('--safe', nargs='*', default=[], help='explore: safe template allowlist')
    ap.add_argument('--show', action='store_true', help='print the graph')
    ap.add_argument('--max-hops', type=int, default=8)
    ap.add_argument('--relaunch', action='store_true', help='allow app relaunch on recovery')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args(argv)

    if args.selftest:
        _selftest()
        return

    graph = load_graph(args.graph)
    if args.show:
        _show(graph)
        return

    from android_device import AndroidDevice
    device = AndroidDevice(args.device)
    db = menu_db.load_db()
    try:
        screens = screen_state.load_screens()
    except Exception:  # noqa: BLE001
        screens = None

    if args.record:
        record(device, graph, print, db=db, screens=screens)
        return
    if args.explore:
        if not args.safe:
            print('refusing to explore without --safe <templates...> allowlist')
            return
        explore(device, graph, print, safe=args.safe, db=db, screens=screens)
        return
    if args.goto:
        try:
            if device.get_top_activity_package() != APP_PACKAGE:
                print('The Tower is not in the foreground; aborting.')
                return
        except Exception:  # noqa: BLE001
            pass
        ok = goto(device, args.goto, print, graph=graph, db=db, screens=screens,
                  max_hops=args.max_hops, allow_relaunch=args.relaunch)
        save_graph(graph)
        print('arrived at %r' % args.goto if ok else 'failed to reach %r' % args.goto)
        return
    ap.print_help()


# --------------------------------------------------------------------------- #
# Self-test (graph logic only — no device / images required)
# --------------------------------------------------------------------------- #
def _selftest():
    import tempfile
    g = _empty_graph()
    observe_hop(g, 'home', {'kind': 'tap', 'xy_frac': [0.5, 0.9]}, 'settings')
    observe_hop(g, 'settings', {'kind': 'tap', 'xy_frac': [0.3, 0.2]}, 'audio')
    observe_hop(g, 'home', {'kind': 'template', 'template': 'edges/e001.png'}, 'perk')
    assert len(g['edges']) == 3, g

    p = find_path(g['edges'], 'home', 'audio')
    assert p and [e['to'] for e in p] == ['settings', 'audio'], p
    assert find_path(g['edges'], 'home', 'home') == []
    assert find_path(g['edges'], 'audio', 'home') is None

    # reinforcement + alt (nondeterministic outcome)
    observe_hop(g, 'home', {'kind': 'tap', 'xy_frac': [0.5, 0.9]}, 'settings')
    e = next(e for e in g['edges'] if e['from'] == 'home' and e['to'] == 'settings')
    assert e['count'] == 2.0, e
    observe_hop(g, 'home', {'kind': 'tap', 'xy_frac': [0.5, 0.9]}, 'ad_popup')
    assert e['alt'].get('ad_popup') == 1.0, e
    assert _reliability(e) == 2.0 / 3.0, _reliability(e)

    # near-identical taps merge (0.02 grid)
    assert (_action_key({'kind': 'tap', 'xy_frac': [0.501, 0.9]})
            == _action_key({'kind': 'tap', 'xy_frac': [0.5, 0.9]}))

    # reliability tiebreak in BFS: a flaky direct edge vs the reliable one
    observe_hop(g, 'home', {'kind': 'template', 'template': 'edges/e009.png'}, 'audio')
    flaky = next(e for e in g['edges'] if e['from'] == 'home' and e['to'] == 'audio')
    flaky['count'] = 1.0
    flaky['alt'] = {'home': 9.0}                     # very unreliable
    p2 = find_path(g['edges'], 'home', 'audio')      # 1-hop flaky vs 2-hop reliable
    assert p2 is not None

    # save/load round-trip + atomic merge
    path = os.path.join(tempfile.mkdtemp(prefix='sg_'), 'transitions.json')
    save_graph(g, path)
    g2 = load_graph(path)
    assert len(g2['edges']) == len(g['edges']), (len(g2['edges']), len(g['edges']))
    observe_hop(g2, 'audio', {'kind': 'back'}, 'settings')
    save_graph(g2, path)                             # exercises the merge path
    g3 = load_graph(path)
    assert any(e['action'].get('kind') == 'back' for e in g3['edges'])

    # relabel rewrites everywhere
    relabel(g, 'audio', 'sound')
    assert not any('audio' in (e.get('from'), e.get('to')) for e in g['edges'])

    print('screen_graph self-test OK — %d edges, home->audio = %s'
          % (len(g['edges']), ' -> '.join(['home'] + [e['to'] for e in p])))


if __name__ == '__main__':
    main()
