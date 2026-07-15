"""
Tkinter UI for The Tower scenario automation.

Manage scenarios (each = a template/points + timing + gates), toggle them
individually, start/stop the engine, watch a live colour-coded log, and
import/export the whole config or a single scenario. Scenarios are saved to
scenarios.json and shared with the console runner (run_scenarios.py).

    python scenario_ui.py
"""
import json
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from automation_engine import Engine, Scenario

# --------------------------------------------------------------------------- #
# Palette + ttk styling
# --------------------------------------------------------------------------- #
BG = '#1e2029'
PANEL = '#272a35'
CARD = '#2f333f'
INPUT = '#343846'
TEXT = '#e4e7f1'
MUTED = '#9aa1b6'
ACCENT = '#8b5cf6'
ACCENT_HI = '#a78bfa'
CYAN = '#22d3ee'
GREEN = '#34d399'
RED = '#f87171'
YELLOW = '#fbbf24'
SEL = '#3a3f52'


def _apply_style(root):
    st = ttk.Style(root)
    try:
        st.theme_use('clam')
    except tk.TclError:
        pass
    root.configure(bg=BG)
    st.configure('.', background=BG, foreground=TEXT, fieldbackground=INPUT,
                 bordercolor=SEL, font=('Segoe UI', 10))
    st.configure('TFrame', background=BG)
    st.configure('Card.TFrame', background=PANEL)
    st.configure('TLabel', background=BG, foreground=TEXT)
    st.configure('Card.TLabel', background=PANEL, foreground=TEXT)
    st.configure('Muted.TLabel', background=BG, foreground=MUTED)
    st.configure('Title.TLabel', background=BG, foreground=TEXT,
                 font=('Segoe UI Semibold', 13))
    st.configure('TButton', background=CARD, foreground=TEXT, borderwidth=0,
                 padding=(10, 6), focuscolor=BG)
    st.map('TButton', background=[('active', SEL), ('disabled', PANEL)],
           foreground=[('disabled', MUTED)])
    st.configure('Accent.TButton', background=ACCENT, foreground='white',
                 padding=(16, 7))
    st.map('Accent.TButton', background=[('active', ACCENT_HI), ('disabled', PANEL)],
           foreground=[('disabled', MUTED)])
    for w in ('TEntry', 'TSpinbox', 'TCombobox'):
        st.configure(w, fieldbackground=INPUT, foreground=TEXT, insertcolor=TEXT,
                     bordercolor=SEL, arrowcolor=TEXT, padding=4)
    st.map('TCombobox', fieldbackground=[('readonly', INPUT)])
    st.configure('TCheckbutton', background=BG, foreground=TEXT)
    st.map('TCheckbutton', background=[('active', BG)])
    st.configure('Card.TCheckbutton', background=PANEL, foreground=TEXT)
    st.map('Card.TCheckbutton', background=[('active', PANEL)])
    st.configure('Treeview', background=CARD, fieldbackground=CARD, foreground=TEXT,
                 rowheight=28, borderwidth=0)
    st.map('Treeview', background=[('selected', ACCENT)],
           foreground=[('selected', 'white')])
    st.configure('Treeview.Heading', background=PANEL, foreground=MUTED,
                 font=('Segoe UI Semibold', 10), relief='flat', padding=6)
    st.map('Treeview.Heading', background=[('active', SEL)])
    st.configure('TScrollbar', background=CARD, troughcolor=BG, arrowcolor=MUTED,
                 bordercolor=BG)
    return st


# --------------------------------------------------------------------------- #
# Add / edit dialog
# --------------------------------------------------------------------------- #
class ScenarioDialog(tk.Toplevel):
    """Modal add/edit dialog. Sets self.result to a Scenario, or None on cancel."""

    def __init__(self, master, scenario: Scenario = None):
        super().__init__(master)
        self.title('Scenario')
        self.configure(bg=BG)
        self.resizable(False, False)
        self.result = None
        s = scenario or Scenario(name='', template='')

        self.v_name = tk.StringVar(value=s.name)
        self.v_template = tk.StringVar(value=s.template)
        self.v_when = tk.StringVar(value=s.when)
        self.v_unless = tk.StringVar(value=s.unless)
        self.v_points = tk.StringVar(value=self._points_to_str(s.points))
        self.v_enabled = tk.BooleanVar(value=s.enabled)
        self.v_threshold = tk.DoubleVar(value=s.threshold)
        self.v_interval = tk.DoubleVar(value=s.interval)
        self.v_cooldown = tk.DoubleVar(value=s.cooldown)
        self.v_multiscale = tk.BooleanVar(value=s.multi_scale)
        self.v_action = tk.StringVar(value=s.action)
        self.v_rotate = tk.IntVar(value=s.rotate)
        self._roi = list(s.roi)          # preserved as-is (edit in scenarios.json)
        self._downscale = s.downscale

        pad = {'padx': 8, 'pady': 4}
        row = 0

        def label(text):
            ttk.Label(self, text=text).grid(row=row, column=0, sticky='w', **pad)

        label('Name'); ttk.Entry(self, textvariable=self.v_name, width=32)\
            .grid(row=row, column=1, columnspan=2, sticky='we', **pad); row += 1

        label('Template')
        ttk.Entry(self, textvariable=self.v_template, width=26)\
            .grid(row=row, column=1, sticky='we', **pad)
        ttk.Button(self, text='Browse…', command=lambda: self._browse(self.v_template))\
            .grid(row=row, column=2, **pad); row += 1

        label('When visible (gate)')
        ttk.Entry(self, textvariable=self.v_when, width=26)\
            .grid(row=row, column=1, sticky='we', **pad)
        ttk.Button(self, text='Browse…', command=lambda: self._browse(self.v_when))\
            .grid(row=row, column=2, **pad); row += 1

        label('Unless visible')
        ttk.Entry(self, textvariable=self.v_unless, width=26)\
            .grid(row=row, column=1, sticky='we', **pad)
        ttk.Button(self, text='Browse…', command=lambda: self._browse(self.v_unless))\
            .grid(row=row, column=2, **pad); row += 1

        label('Tap points  x,y; x,y')
        ttk.Entry(self, textvariable=self.v_points, width=26)\
            .grid(row=row, column=1, columnspan=2, sticky='we', **pad); row += 1

        label('Check interval (s)')
        ttk.Spinbox(self, textvariable=self.v_interval, from_=0.1, to=3600, increment=0.5,
                    width=10).grid(row=row, column=1, sticky='w', **pad); row += 1

        label('Match threshold')
        ttk.Spinbox(self, textvariable=self.v_threshold, from_=0.1, to=1.0, increment=0.01,
                    width=10).grid(row=row, column=1, sticky='w', **pad); row += 1

        label('Tap cooldown (s)')
        ttk.Spinbox(self, textvariable=self.v_cooldown, from_=0.0, to=3600, increment=0.5,
                    width=10).grid(row=row, column=1, sticky='w', **pad); row += 1

        label('Action')
        ttk.Combobox(self, textvariable=self.v_action, values=['tap', 'none'],
                     state='readonly', width=8).grid(row=row, column=1, sticky='w', **pad); row += 1

        label('Rotate step° (0=off)')
        ttk.Spinbox(self, textvariable=self.v_rotate, from_=0, to=180, increment=5,
                    width=10).grid(row=row, column=1, sticky='w', **pad); row += 1

        ttk.Checkbutton(self, text='Enabled', variable=self.v_enabled)\
            .grid(row=row, column=1, sticky='w', **pad)
        ttk.Checkbutton(self, text='Multi-scale search', variable=self.v_multiscale)\
            .grid(row=row, column=2, sticky='w', **pad); row += 1

        bar = ttk.Frame(self); bar.grid(row=row, column=0, columnspan=3, sticky='e', **pad)
        ttk.Button(bar, text='OK', style='Accent.TButton', command=self._ok).pack(side='right', padx=4)
        ttk.Button(bar, text='Cancel', command=self.destroy).pack(side='right')

        self.transient(master)
        self.grab_set()
        self.wait_visibility()
        self.focus()

    @staticmethod
    def _points_to_str(points):
        return '; '.join('%d,%d' % (int(x), int(y)) for (x, y) in (points or []))

    @staticmethod
    def _parse_points(text):
        points = []
        for chunk in text.replace('\n', ';').split(';'):
            chunk = chunk.strip()
            if not chunk:
                continue
            x, y = chunk.split(',')
            points.append([int(x), int(y)])
        return points

    def _browse(self, var):
        path = filedialog.askopenfilename(
            title='Choose a template image',
            initialdir=os.path.abspath('templates'),
            filetypes=[('PNG images', '*.png'), ('All files', '*.*')])
        if path:
            try:
                rel = os.path.relpath(path, os.getcwd())
                var.set(rel if not rel.startswith('..') else path)
            except ValueError:
                var.set(path)

    def _ok(self):
        name = self.v_name.get().strip()
        template = self.v_template.get().strip()
        if not name:
            messagebox.showerror('Invalid', 'Name is required.', parent=self); return
        try:
            points = self._parse_points(self.v_points.get())
        except ValueError:
            messagebox.showerror('Invalid', 'Tap points must be "x,y; x,y".', parent=self); return
        if not template and not points:
            messagebox.showerror(
                'Invalid', 'Give a Template to locate, or Tap points to tap.',
                parent=self); return
        try:
            self.result = Scenario(
                name=name, template=template, enabled=self.v_enabled.get(),
                threshold=float(self.v_threshold.get()), interval=float(self.v_interval.get()),
                cooldown=float(self.v_cooldown.get()), multi_scale=self.v_multiscale.get(),
                action=self.v_action.get(),
                when=self.v_when.get().strip(), unless=self.v_unless.get().strip(),
                points=points, rotate=int(self.v_rotate.get()),
                downscale=self._downscale, roi=self._roi)
        except (tk.TclError, ValueError) as e:
            messagebox.showerror('Invalid', 'Numeric field error: %s' % e, parent=self); return
        self.destroy()


# --------------------------------------------------------------------------- #
# Main window
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('The Tower — Scenario Automation')
        self.geometry('920x700')
        self.minsize(840, 620)
        _apply_style(self)

        self._log_q = queue.Queue()
        self.engine = Engine(logger=self._enqueue_log)
        self.autoscroll = tk.BooleanVar(value=True)
        self._running_state = False

        self._build_menu()
        self._build_header()
        self._build_settings()
        self._build_table()
        self._build_log()
        self._build_statusbar()

        self._refresh_table()
        self._set_running(False)
        self.after(120, self._drain_logs)
        self.after(600, self._tick)
        self.protocol('WM_DELETE_WINDOW', self._on_close)
        self.bind('<Control-s>', lambda e: self._save())

    # ------------------------------------------------------------- build
    def _menu(self, parent):
        return tk.Menu(parent, tearoff=0, bg=PANEL, fg=TEXT,
                       activebackground=ACCENT, activeforeground='white',
                       bd=0, relief='flat')

    def _build_menu(self):
        bar = self._menu(self)
        filem = self._menu(bar)
        filem.add_command(label='Import config…', command=self._import_config)
        filem.add_command(label='Export config…', command=self._export_config)
        filem.add_separator()
        filem.add_command(label='Save', accelerator='Ctrl+S', command=self._save)
        filem.add_command(label='Reload from disk', command=self._reload)
        filem.add_separator()
        filem.add_command(label='Exit', command=self._on_close)
        bar.add_cascade(label='File', menu=filem)

        sc = self._menu(bar)
        sc.add_command(label='Add…', command=self._add)
        sc.add_command(label='Edit…', command=self._edit)
        sc.add_command(label='Duplicate', command=self._duplicate)
        sc.add_command(label='Remove', command=self._remove)
        sc.add_separator()
        sc.add_command(label='Enable all', command=lambda: self._set_all(True))
        sc.add_command(label='Disable all', command=lambda: self._set_all(False))
        sc.add_separator()
        sc.add_command(label='Import scenario…', command=self._import_scenario)
        sc.add_command(label='Export selected…', command=self._export_scenario)
        sc.add_command(label='Test selected', command=self._test_selected)
        bar.add_cascade(label='Scenario', menu=sc)

        helpm = self._menu(bar)
        helpm.add_command(label='About', command=self._about)
        bar.add_cascade(label='Help', menu=helpm)
        self.config(menu=bar)

    def _build_header(self):
        f = ttk.Frame(self); f.pack(fill='x', padx=14, pady=(12, 4))
        ttk.Label(f, text='⛨  The Tower Automation', style='Title.TLabel').pack(side='left')
        self.btn_start = ttk.Button(f, text='▶  Start', style='Accent.TButton', command=self._start)
        self.btn_stop = ttk.Button(f, text='■  Stop', command=self._stop)
        self.btn_stop.pack(side='right')
        self.btn_start.pack(side='right', padx=(0, 8))
        self.status_lbl = ttk.Label(f, text='Stopped', style='Muted.TLabel')
        self.status_lbl.pack(side='right', padx=(10, 14))
        self.dot = tk.Canvas(f, width=13, height=13, bg=BG, highlightthickness=0)
        self._dot = self.dot.create_oval(2, 2, 12, 12, fill=RED, outline='')
        self.dot.pack(side='right')

    def _build_settings(self):
        c = ttk.Frame(self, style='Card.TFrame'); c.pack(fill='x', padx=14, pady=6)
        self.v_device = tk.StringVar(value=self.engine.device_id)
        self.v_package = tk.StringVar(value=self.engine.app_package)
        self.v_fg = tk.BooleanVar(value=self.engine.require_foreground)
        ttk.Label(c, text='Device', style='Card.TLabel').pack(side='left', padx=(12, 4), pady=10)
        ttk.Entry(c, textvariable=self.v_device, width=16).pack(side='left', padx=(0, 12))
        ttk.Label(c, text='App package', style='Card.TLabel').pack(side='left', padx=(0, 4))
        ttk.Entry(c, textvariable=self.v_package, width=26).pack(side='left', padx=(0, 12))
        ttk.Checkbutton(c, text='Only when app in foreground', variable=self.v_fg,
                        style='Card.TCheckbutton').pack(side='left')
        ttk.Button(c, text='📸  Screenshot', command=self._screenshot).pack(side='right', padx=12)

    def _build_table(self):
        tb = ttk.Frame(self); tb.pack(fill='x', padx=14, pady=(8, 0))
        ttk.Label(tb, text='Scenarios', style='Title.TLabel').pack(side='left')
        for text, cmd in [('Test', self._test_selected),
                          ('Disable all', lambda: self._set_all(False)),
                          ('Enable all', lambda: self._set_all(True)),
                          ('▼', self._move_down), ('▲', self._move_up),
                          ('Remove', self._remove), ('Duplicate', self._duplicate),
                          ('Edit', self._edit), ('＋ Add', self._add)]:
            ttk.Button(tb, text=text, command=cmd).pack(side='right', padx=3)

        f = ttk.Frame(self, style='Card.TFrame'); f.pack(fill='both', expand=True, padx=14, pady=6)
        cols = ('on', 'name', 'target', 'every', 'thresh', 'action', 'fires')
        self.tree = ttk.Treeview(f, columns=cols, show='headings', height=8, selectmode='browse')
        heads = {'on': ('On', 46), 'name': ('Name', 190), 'target': ('Target', 250),
                 'every': ('Every s', 72), 'thresh': ('Thresh', 66),
                 'action': ('Action', 64), 'fires': ('Fires', 60)}
        for col, (t, w) in heads.items():
            self.tree.heading(col, text=t)
            anchor = 'w' if col in ('name', 'target') else 'center'
            self.tree.column(col, width=w, anchor=anchor, stretch=(col == 'target'))
        self.tree.tag_configure('off', foreground=MUTED)
        sb = ttk.Scrollbar(f, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True, padx=(6, 0), pady=6)
        sb.pack(side='right', fill='y', pady=6)
        self.tree.bind('<Button-1>', self._on_click)
        self.tree.bind('<Double-1>', lambda e: self._edit())
        self.tree.bind('<space>', lambda e: self._toggle_selected())

    def _build_log(self):
        head = ttk.Frame(self); head.pack(fill='x', padx=14, pady=(8, 0))
        ttk.Label(head, text='Log', style='Title.TLabel').pack(side='left')
        ttk.Checkbutton(head, text='Autoscroll', variable=self.autoscroll).pack(side='right')
        ttk.Button(head, text='Clear', command=self._clear_log).pack(side='right', padx=6)

        f = ttk.Frame(self, style='Card.TFrame'); f.pack(fill='both', expand=True, padx=14, pady=6)
        self.log = tk.Text(f, height=9, wrap='none', state='disabled', bg='#15171e',
                           fg=TEXT, insertbackground=TEXT, relief='flat',
                           font=('Consolas', 9), padx=8, pady=6)
        sb = ttk.Scrollbar(f, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.log.tag_configure('tap', foreground=GREEN)
        self.log.tag_configure('err', foreground=RED)
        self.log.tag_configure('warn', foreground=YELLOW)
        self.log.tag_configure('ui', foreground=CYAN)

    def _build_statusbar(self):
        self.statusbar = ttk.Label(self, text='', style='Muted.TLabel', anchor='w')
        self.statusbar.pack(fill='x', padx=16, pady=(0, 10))

    # ------------------------------------------------------------- table
    @staticmethod
    def _target_text(s):
        if s.template:
            t = os.path.basename(s.template)
            return t + (' ↻%d°' % s.rotate if s.rotate else '')
        parts = ['%d pt%s' % (len(s.points), '' if len(s.points) == 1 else 's')]
        if s.when:
            parts.append('when ' + os.path.splitext(os.path.basename(s.when))[0])
        if s.unless:
            parts.append('unless ' + os.path.splitext(os.path.basename(s.unless))[0])
        return ' · '.join(parts)

    def _refresh_table(self):
        sel = self._selected_index()
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.engine.scenarios):
            self.tree.insert('', 'end', iid=str(i), tags=() if s.enabled else ('off',),
                             values=('☑' if s.enabled else '☐', s.name, self._target_text(s),
                                     '%g' % s.interval, '%.2f' % s.threshold, s.action,
                                     self.engine.stats.get(s.name, 0)))
        if sel is not None and sel < len(self.engine.scenarios):
            self.tree.selection_set(str(sel))
        self._update_counts()

    def _selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _on_click(self, e):
        if self.tree.identify('region', e.x, e.y) != 'cell':
            return
        if self.tree.identify_column(e.x) == '#1':
            row = self.tree.identify_row(e.y)
            if row:
                self._toggle_index(int(row))
                return 'break'

    def _toggle_selected(self):
        i = self._selected_index()
        if i is not None:
            self._toggle_index(i)

    def _toggle_index(self, i):
        s = self.engine.scenarios[i]
        s.enabled = not s.enabled
        self._log('%s: %s' % (s.name, 'enabled' if s.enabled else 'disabled'))
        self._refresh_table()
        self.tree.selection_set(str(i))

    def _set_all(self, value):
        for s in self.engine.scenarios:
            s.enabled = value
        self._log('all scenarios %s' % ('enabled' if value else 'disabled'))
        self._refresh_table()

    # ------------------------------------------------------------- edit
    def _add(self):
        dlg = ScenarioDialog(self); self.wait_window(dlg)
        if dlg.result:
            self.engine.scenarios.append(dlg.result); self._refresh_table()

    def _edit(self):
        i = self._selected_index()
        if i is None:
            return
        dlg = ScenarioDialog(self, self.engine.scenarios[i]); self.wait_window(dlg)
        if dlg.result:
            self.engine.scenarios[i] = dlg.result
            self._refresh_table(); self.tree.selection_set(str(i))

    def _duplicate(self):
        i = self._selected_index()
        if i is None:
            return
        copy = Scenario.from_dict(self.engine.scenarios[i].to_dict())
        copy.name += ' copy'
        self.engine.scenarios.insert(i + 1, copy); self._refresh_table()

    def _remove(self):
        i = self._selected_index()
        if i is None:
            return
        if messagebox.askyesno('Remove', 'Remove scenario %r?' % self.engine.scenarios[i].name):
            del self.engine.scenarios[i]; self._refresh_table()

    def _move_up(self):
        i = self._selected_index()
        if i is None or i == 0:
            return
        sc = self.engine.scenarios
        sc[i - 1], sc[i] = sc[i], sc[i - 1]
        self._refresh_table(); self.tree.selection_set(str(i - 1))

    def _move_down(self):
        i = self._selected_index()
        if i is None or i >= len(self.engine.scenarios) - 1:
            return
        sc = self.engine.scenarios
        sc[i + 1], sc[i] = sc[i], sc[i + 1]
        self._refresh_table(); self.tree.selection_set(str(i + 1))

    # ------------------------------------------------------------- config I/O
    def _pull_settings(self):
        self.engine.device_id = self.v_device.get().strip() or 'emulator-5554'
        self.engine.app_package = self.v_package.get().strip()
        self.engine.require_foreground = self.v_fg.get()

    def _sync_settings_vars(self):
        self.v_device.set(self.engine.device_id)
        self.v_package.set(self.engine.app_package)
        self.v_fg.set(self.engine.require_foreground)

    def _save(self):
        self._pull_settings(); self.engine.save()
        self._log('saved %s' % self.engine.config_path)

    def _reload(self):
        self.engine.load(); self._sync_settings_vars(); self._refresh_table()
        self._log('reloaded %s' % self.engine.config_path)

    def _export_config(self):
        p = filedialog.asksaveasfilename(defaultextension='.json', initialfile='scenarios.json',
                                         filetypes=[('JSON', '*.json')])
        if not p:
            return
        self._pull_settings()
        try:
            self.engine.save(p); self._log('exported config → %s' % p)
        except OSError as e:
            messagebox.showerror('Export failed', str(e))

    def _import_config(self):
        p = filedialog.askopenfilename(filetypes=[('JSON', '*.json'), ('All files', '*.*')])
        if not p:
            return
        if not messagebox.askyesno('Import config',
                                   'Replace all scenarios and settings with\n%s ?' % p):
            return
        try:
            with open(p, encoding='utf-8') as f:
                self.engine.apply_dict(json.load(f))
        except (OSError, ValueError, KeyError) as e:
            messagebox.showerror('Import failed', str(e)); return
        self._sync_settings_vars(); self._refresh_table()
        self._log('imported config ← %s' % p)

    @staticmethod
    def _safe_name(name):
        return ''.join(c if c.isalnum() or c in '-_ ' else '_' for c in name).strip() or 'scenario'

    def _export_scenario(self):
        i = self._selected_index()
        if i is None:
            messagebox.showinfo('Export scenario', 'Select a scenario first.'); return
        s = self.engine.scenarios[i]
        p = filedialog.asksaveasfilename(defaultextension='.json',
                                         initialfile=self._safe_name(s.name) + '.json',
                                         filetypes=[('JSON', '*.json')])
        if not p:
            return
        try:
            with open(p, 'w', encoding='utf-8') as f:
                json.dump(s.to_dict(), f, indent=2, ensure_ascii=False)
            self._log('exported scenario %r → %s' % (s.name, p))
        except OSError as e:
            messagebox.showerror('Export failed', str(e))

    def _import_scenario(self):
        p = filedialog.askopenfilename(filetypes=[('JSON', '*.json'), ('All files', '*.*')])
        if not p:
            return
        try:
            with open(p, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            messagebox.showerror('Import failed', str(e)); return
        items = data if isinstance(data, list) else data.get('scenarios', [data])
        added = 0
        for d in items:
            if isinstance(d, dict) and d.get('name'):
                self.engine.scenarios.append(Scenario.from_dict(d)); added += 1
        self._refresh_table()
        self._log('imported %d scenario(s) ← %s' % (added, p))

    # ------------------------------------------------------------- run / test
    def _start(self):
        if self.engine.is_running():
            return
        self._pull_settings()
        if not any(s.enabled for s in self.engine.scenarios):
            messagebox.showinfo('Nothing enabled', 'Enable at least one scenario first.'); return
        self.engine.start(); self._set_running(True); self._refresh_table()

    def _stop(self):
        self.engine.stop()

    def _test_selected(self):
        i = self._selected_index()
        if i is None:
            messagebox.showinfo('Test', 'Select a scenario first.'); return
        s = self.engine.scenarios[i]
        self._log('testing %r …' % s.name)
        threading.Thread(target=self._run_test, args=(s,), daemon=True).start()

    def _run_test(self, s):
        try:
            from android_device import AndroidDevice
            from image_recognition import find_template, find_rotated, multi_scale
            dev = AndroidDevice(self.v_device.get().strip() or self.engine.device_id)
            screen = dev.capture()
        except Exception as e:  # noqa: BLE001
            self._log('test: device error: %s' % e); return
        try:
            if not s.template and s.points:
                gate = 'always'
                if s.when:
                    g = find_template(screen, s.when, threshold=s.threshold)
                    gate = 'when-gate %s' % ('MET' if g else 'not met')
                elif s.unless:
                    g = find_template(screen, s.unless, threshold=s.threshold)
                    gate = 'unless-gate %s' % ('BLOCKS' if g else 'clear')
                self._log('test %r: %s → would tap %d point(s)' % (s.name, gate, len(s.points)))
                return
            if s.rotate:
                m = find_rotated(screen, s.template, step=s.rotate, threshold=0.0,
                                 downscale=(s.downscale or 1.0),
                                 roi=(tuple(s.roi) if s.roi else None))
            else:
                m = find_template(screen, s.template, threshold=0.0,
                                  scales=(multi_scale() if s.multi_scale else None))
            hit = bool(m and m.confidence >= s.threshold)
            self._log('test %r: best=%.3f at %s (need %.2f) → %s'
                      % (s.name, m.confidence if m else -1, m.center if m else None,
                         s.threshold, 'HIT' if hit else 'no match'))
        except Exception as e:  # noqa: BLE001
            self._log('test error: %s' % e)

    def _screenshot(self):
        threading.Thread(target=self._grab, daemon=True).start()

    def _grab(self):
        try:
            from android_device import AndroidDevice
            dev = AndroidDevice(self.v_device.get().strip() or self.engine.device_id)
            img = dev.capture(); img.save('screen.png')
            self._log('saved screen.png (%dx%d) — crop buttons into templates/'
                      % (img.width, img.height))
        except Exception as e:  # noqa: BLE001
            self._log('screenshot failed: %s' % e)

    # ------------------------------------------------------------- log / status
    def _enqueue_log(self, msg):
        self._log_q.put(msg)

    def _log(self, msg):
        self._enqueue_log('[ui] %s' % msg)

    def _clear_log(self):
        self.log.configure(state='normal'); self.log.delete('1.0', 'end')
        self.log.configure(state='disabled')

    def _drain_logs(self):
        try:
            while True:
                msg = self._log_q.get_nowait()
                tag = ''
                if 'TAP' in msg:
                    tag = 'tap'
                elif 'ERROR' in msg or 'Traceback' in msg:
                    tag = 'err'
                elif 'WARN' in msg:
                    tag = 'warn'
                elif 'engine started' in msg or 'engine stopped' in msg:
                    tag = 'ui'
                self.log.configure(state='normal')
                self.log.insert('end', msg + '\n', tag)
                if self.autoscroll.get():
                    self.log.see('end')
                self.log.configure(state='disabled')
        except queue.Empty:
            pass
        self.after(120, self._drain_logs)

    def _update_counts(self):
        n = len(self.engine.scenarios)
        en = sum(1 for s in self.engine.scenarios if s.enabled)
        taps = sum(self.engine.stats.values())
        self.statusbar.configure(
            text='%d of %d scenarios enabled   ·   %d taps this run   ·   %s'
                 % (en, n, taps, 'running' if self.engine.is_running() else 'stopped'))

    def _tick(self):
        # keep fire counters + running state live without rebuilding the table
        for i, s in enumerate(self.engine.scenarios):
            iid = str(i)
            if self.tree.exists(iid):
                self.tree.set(iid, 'fires', self.engine.stats.get(s.name, 0))
        running = self.engine.is_running()
        if running != self._running_state:
            self._set_running(running)
        self._update_counts()
        self.after(600, self._tick)

    def _set_running(self, running):
        self._running_state = running
        self.dot.itemconfig(self._dot, fill=GREEN if running else RED)
        self.status_lbl.configure(text='Running' if running else 'Stopped')
        self.btn_start.configure(state='disabled' if running else 'normal')
        self.btn_stop.configure(state='normal' if running else 'disabled')

    def _about(self):
        messagebox.showinfo(
            'About',
            'The Tower — Scenario Automation\n\n'
            'Scenarios locate templates (optionally rotated) or tap fixed points, '
            'gated by what is on screen. Toggle each one with the On column; '
            'import/export configs or single scenarios from the menus.')

    def _on_close(self):
        self.engine.stop()
        self.destroy()


if __name__ == '__main__':
    App().mainloop()
