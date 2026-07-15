"""
Tkinter UI for The Tower scenario automation.

Manage a list of *scenarios* (each = a template image + per-scenario check
interval, threshold, and tap/log action), start/stop the engine, and watch every
action stream into a live log. Scenarios are saved to scenarios.json and shared
with the console runner (run_scenarios.py).

    python scenario_ui.py
"""
import os
import queue
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from automation_engine import Engine, Scenario


class ScenarioDialog(tk.Toplevel):
    """Modal add/edit dialog. Sets self.result to a Scenario, or None on cancel."""

    def __init__(self, master, scenario: Scenario = None):
        super().__init__(master)
        self.title('Scenario')
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
        ttk.Button(bar, text='OK', command=self._ok).pack(side='right', padx=4)
        ttk.Button(bar, text='Cancel', command=self.destroy).pack(side='right')

        self.transient(master)
        self.grab_set()
        self.v_name.set(self.v_name.get())  # focus below
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
            # store relative if inside the project, else absolute
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('The Tower — Scenario Automation')
        self.geometry('820x560')
        self.minsize(720, 480)

        self._log_q = queue.Queue()
        self.engine = Engine(logger=self._enqueue_log)

        self._build_settings()
        self._build_table()
        self._build_controls()
        self._build_log()

        self._refresh_table()
        self.after(120, self._drain_logs)
        self.after(400, self._poll_running)
        self.protocol('WM_DELETE_WINDOW', self._on_close)

    # ----------------------------------------------------------- UI build
    def _build_settings(self):
        f = ttk.Frame(self); f.pack(fill='x', padx=8, pady=(8, 0))
        self.v_device = tk.StringVar(value=self.engine.device_id)
        self.v_package = tk.StringVar(value=self.engine.app_package)
        self.v_fg = tk.BooleanVar(value=self.engine.require_foreground)
        ttk.Label(f, text='Device').pack(side='left')
        ttk.Entry(f, textvariable=self.v_device, width=16).pack(side='left', padx=(4, 12))
        ttk.Label(f, text='App package').pack(side='left')
        ttk.Entry(f, textvariable=self.v_package, width=28).pack(side='left', padx=(4, 12))
        ttk.Checkbutton(f, text='Only when app in foreground', variable=self.v_fg)\
            .pack(side='left')

    def _build_table(self):
        f = ttk.Frame(self); f.pack(fill='both', expand=True, padx=8, pady=8)
        cols = ('on', 'name', 'template', 'interval', 'threshold', 'action')
        self.tree = ttk.Treeview(f, columns=cols, show='headings', height=8)
        heads = {'on': ('On', 40), 'name': ('Name', 180), 'template': ('Template', 240),
                 'interval': ('Every (s)', 80), 'threshold': ('Thresh', 70),
                 'action': ('Action', 70)}
        for c, (text, w) in heads.items():
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, anchor='center' if c != 'name' and c != 'template' else 'w')
        sb = ttk.Scrollbar(f, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')
        self.tree.bind('<Double-1>', lambda e: self._edit())

    def _build_controls(self):
        f = ttk.Frame(self); f.pack(fill='x', padx=8)
        self.btns = {}
        for key, text, cmd in [
            ('add', 'Add', self._add), ('edit', 'Edit', self._edit),
            ('dup', 'Duplicate', self._duplicate), ('del', 'Remove', self._remove),
            ('up', '↑', self._move_up), ('down', '↓', self._move_down),
        ]:
            b = ttk.Button(f, text=text, command=cmd, width=9 if len(text) > 2 else 3)
            b.pack(side='left', padx=2); self.btns[key] = b

        self.btn_start = ttk.Button(f, text='▶ Start', command=self._start)
        self.btn_stop = ttk.Button(f, text='■ Stop', command=self._stop, state='disabled')
        self.btn_save = ttk.Button(f, text='Save', command=self._save)
        self.btn_shot = ttk.Button(f, text='Screenshot', command=self._screenshot)
        self.btn_shot.pack(side='right', padx=2)
        self.btn_save.pack(side='right', padx=2)
        self.btn_stop.pack(side='right', padx=2)
        self.btn_start.pack(side='right', padx=2)

    def _build_log(self):
        f = ttk.Frame(self); f.pack(fill='both', expand=True, padx=8, pady=8)
        ttk.Label(f, text='Log').pack(anchor='w')
        self.log = tk.Text(f, height=10, wrap='none', state='disabled',
                           background='#111', foreground='#ddd', font=('Consolas', 9))
        sb = ttk.Scrollbar(f, orient='vertical', command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.pack(side='left', fill='both', expand=True)
        sb.pack(side='right', fill='y')

    # ----------------------------------------------------------- table sync
    def _refresh_table(self):
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self.engine.scenarios):
            if s.template:
                what = os.path.basename(s.template)
            else:
                what = '%d pt%s' % (len(s.points), '' if len(s.points) == 1 else 's')
                if s.when:
                    what += ' · when ' + os.path.splitext(os.path.basename(s.when))[0]
                elif s.unless:
                    what += ' · unless ' + os.path.splitext(os.path.basename(s.unless))[0]
            self.tree.insert('', 'end', iid=str(i), values=(
                '✓' if s.enabled else '·', s.name, what,
                '%g' % s.interval, '%.2f' % s.threshold, s.action))

    def _selected_index(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    # ----------------------------------------------------------- actions
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
            self.engine.scenarios[i] = dlg.result; self._refresh_table()
            self.tree.selection_set(str(i))

    def _duplicate(self):
        i = self._selected_index()
        if i is None:
            return
        src = self.engine.scenarios[i]
        copy = Scenario.from_dict(src.to_dict()); copy.name = src.name + ' copy'
        self.engine.scenarios.insert(i + 1, copy); self._refresh_table()

    def _remove(self):
        i = self._selected_index()
        if i is None:
            return
        if messagebox.askyesno('Remove', 'Remove scenario %r?'
                               % self.engine.scenarios[i].name):
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

    def _pull_settings(self):
        self.engine.device_id = self.v_device.get().strip() or 'emulator-5554'
        self.engine.app_package = self.v_package.get().strip()
        self.engine.require_foreground = self.v_fg.get()

    def _save(self):
        self._pull_settings()
        self.engine.save()
        self._log('saved %s' % self.engine.config_path)

    def _start(self):
        if self.engine.is_running():
            return
        self._pull_settings()
        if not any(s.enabled for s in self.engine.scenarios):
            messagebox.showinfo('Nothing enabled', 'Enable at least one scenario first.')
            return
        self.engine.start()
        self._set_running(True)

    def _stop(self):
        self.engine.stop()

    def _screenshot(self):
        """Grab the device screen to screen.png so you can crop new templates."""
        try:
            from android_device import AndroidDevice
            dev = AndroidDevice(self.v_device.get().strip() or 'emulator-5554')
            img = dev.capture()
            img.save('screen.png')
            self._log('saved screen.png (%dx%d) — crop buttons from it into templates/'
                      % (img.width, img.height))
        except Exception as e:  # noqa: BLE001
            self._log('screenshot failed: %s' % e)
            messagebox.showerror('Screenshot failed', str(e))

    # ----------------------------------------------------------- log plumbing
    def _enqueue_log(self, msg):
        self._log_q.put(msg)

    def _log(self, msg):
        self._enqueue_log('[ui] %s' % msg)

    def _drain_logs(self):
        try:
            while True:
                msg = self._log_q.get_nowait()
                self.log.configure(state='normal')
                self.log.insert('end', msg + '\n')
                self.log.see('end')
                self.log.configure(state='disabled')
        except queue.Empty:
            pass
        self.after(120, self._drain_logs)

    def _set_running(self, running):
        self.btn_start.configure(state='disabled' if running else 'normal')
        self.btn_stop.configure(state='normal' if running else 'disabled')
        for b in self.btns.values():
            b.configure(state='disabled' if running else 'normal')
        self.btn_save.configure(state='disabled' if running else 'normal')

    def _poll_running(self):
        # keep buttons in sync if the engine thread exits on its own (e.g. error)
        if not self.engine.is_running() and str(self.btn_stop['state']) == 'normal':
            self._set_running(False)
        self.after(400, self._poll_running)

    def _on_close(self):
        self.engine.stop()
        self.destroy()


if __name__ == '__main__':
    App().mainloop()
