# -*- coding: utf-8 -*-
"""
dmde_auto_gui.py — DMDE 免费版自动恢复 图形界面

功能:
  - 自动检测/选择正在运行的 DMDE 进程 (多开时可指定, 绝不碰其他实例)
  - 选择输出目录, 设置每批文件数/预览模式/忽略断点
  - 实时日志与进度 (当前目录 / 已恢复文件数 / 批次)
  - 一键开始 / 安全停止 (等当前批次完成后停止, 断点已保存)
  - 未以管理员运行时一键提权重启

用法:
  与 DMDE 相同权限运行 (右键"以管理员身份运行"), 或启动后点"以管理员身份重启"。
  先在 DMDE 中打开卷并停在文件面板, 再点"开始恢复"。

依赖: pip install pywinauto   (界面本身用 Python 自带的 tkinter)
"""
import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import dmde_auto_recover as engine

BASE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(BASE, '_gui_settings.json')


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def load_settings():
    try:
        with open(SETTINGS, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


class FolderPicker(tk.Toplevel):
    """纯 tkinter 目录选择器 — 不调用系统文件对话框, 避免外壳枚举导致的卡死。"""

    def __init__(self, master, initial=''):
        super().__init__(master)
        if initial:
            initial = os.path.normpath(initial)
        self.title('选择输出目录 (务必选另一块物理磁盘)')
        self.geometry('620x440')
        self.transient(master)
        self.grab_set()
        self.result = None
        self._full = {}          # treeview item id -> 完整路径

        top = ttk.Frame(self)
        top.pack(fill='x', padx=6, pady=4)
        ttk.Label(top, text='磁盘:').pack(side='left')
        self.drive_cb = ttk.Combobox(top, state='readonly', width=8)
        self.drive_cb.pack(side='left', padx=4)
        self.drive_cb.bind('<<ComboboxSelected>>', lambda e: self._load_drive())
        ttk.Label(top, text='双击文件夹逐层展开; 选中后点下方"确定"').pack(side='left', padx=8)

        self.tree = ttk.Treeview(self, show='tree', height=16)
        self.tree.pack(fill='both', expand=True, padx=6, pady=4)
        self.tree.bind('<<TreeviewOpen>>', self._on_open)

        bottom = ttk.Frame(self)
        bottom.pack(fill='x', padx=6, pady=4)
        self.cur_var = tk.StringVar()
        ttk.Label(bottom, text='已选:').pack(side='left')
        tk.Label(bottom, textvariable=self.cur_var, fg='#0055cc').pack(side='left', padx=4)
        ttk.Button(bottom, text='确定', command=self._ok).pack(side='right')
        ttk.Button(bottom, text='取消', command=self.destroy).pack(side='right', padx=6)

        # 磁盘列表
        drives = []
        try:
            buf = ctypes.create_unicode_buffer(512)
            n = ctypes.windll.kernel32.GetLogicalDriveStringsW(511, buf)
            drives = [d for d in buf[:n].split('\x00') if d]
        except Exception:
            pass
        self.drive_cb['values'] = drives
        init_drive = None
        if initial:
            for d in drives:
                if initial.upper().startswith(d.upper()):
                    init_drive = d
                    break
        if init_drive is None and drives:
            init_drive = drives[0]
        if init_drive:
            self.drive_cb.set(init_drive)
            self._load_drive()
            self._expand_to(initial)

        self.protocol('WM_DELETE_WINDOW', self.destroy)
        if initial:
            initial = os.path.normpath(initial)
        self.wait_window()

    def _load_drive(self):
        self.tree.delete(*self.tree.get_children())
        self._full.clear()
        drive = self.drive_cb.get()
        if not drive:
            return
        iid = self._add_dir('', '', drive)
        self.tree.item(iid, open=True)
        self._expand(iid)

    def _add_dir(self, parent_iid, parent_path, name):
        full = name if not parent_path else os.path.join(parent_path, name)
        iid = self.tree.insert(parent_iid, 'end', text=name, open=False,
                               values=(full,))
        self._full[iid] = full
        # 放一个占位子节点, 让展开箭头出现
        ph = self.tree.insert(iid, 'end', text='...', values=('~',))
        self._full[ph] = '~'
        return iid

    def _on_open(self, event):
        self._expand(self.tree.focus())

    def _expand(self, iid):
        full = self._full.get(iid)
        if full is None or full == '~':
            return
        for ch in self.tree.get_children(iid):
            if self._full.get(ch) == '~':
                self.tree.delete(ch)
        if self.tree.get_children(iid):
            return   # 已展开过
        try:
            with os.scandir(full) as it:
                dirs = sorted((e for e in it if e.is_dir(follow_symlinks=False)),
                              key=lambda e: e.name.lower())
            for e in dirs:
                self._add_dir(iid, full, e.name)
        except PermissionError:
            self.tree.insert(iid, 'end', text='<无权限>', values=('~',))
        except OSError as e:
            self.tree.insert(iid, 'end', text='<错误: %s>' % e, values=('~',))
        if not self.tree.get_children(iid):
            self.tree.insert(iid, 'end', text='<空目录>', values=('~',))

    def _expand_to(self, path):
        """尽力展开并选中初始路径 (逐层)。"""
        if not path:
            return
        path = os.path.normpath(path)
        drive = os.path.splitdrive(path)[0] + os.sep
        rel = path[len(drive):]
        parts = [p for p in rel.split(os.sep) if p]
        # 找到盘符节点
        iid = None
        for ch in self.tree.get_children(''):
            if (self._full.get(ch) or '').upper() == drive.upper():
                iid = ch
                break
        if iid is None:
            return
        self.tree.item(iid, open=True)
        self._expand(iid)
        cur = drive
        for p in parts:
            self._expand(iid)
            self.tree.item(iid, open=True)
            nxt = None
            for ch in self.tree.get_children(iid):
                if (self._full.get(ch) or '').lower() == os.path.join(cur, p).lower():
                    nxt = ch
                    break
            if nxt is None:
                break
            iid = nxt
            cur = os.path.join(cur, p)
            self.tree.item(iid, open=True)
            self._expand(iid)
        self.tree.see(iid)
        self.tree.selection_set(iid)
        self.tree.focus(iid)

    def _ok(self):
        sel = self.tree.selection()
        if sel and self._full.get(sel[0]) not in (None, '~'):
            self.result = self._full[sel[0]]
        self.destroy()


class FolderSelectDialog(tk.Toplevel):
    """从 DMDE 当前面板读取文件夹列表, 多选要恢复的分支。"""

    def __init__(self, master, fetch):
        super().__init__(master)
        self.title('选择要恢复的文件夹')
        self.geometry('560x480')
        self.transient(master)
        self.grab_set()
        self.result = None
        self._fetch = fetch
        self._q = queue.Queue()

        self.ctx_label = ttk.Label(self, text='勾选要恢复的文件夹 (读取自 DMDE 当前面板):')
        self.ctx_label.pack(anchor='w', padx=8, pady=(8, 2))
        frame = ttk.Frame(self)
        frame.pack(fill='both', expand=True, padx=8)
        self.listbox = tk.Listbox(frame, selectmode='extended',
                                  font=('Microsoft YaHei UI', 9))
        sb = ttk.Scrollbar(frame, orient='vertical', command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=sb.set)
        self.listbox.pack(side='left', fill='both', expand=True)
        sb.pack(side='left', fill='y')

        self.status = ttk.Label(self, text='正在从 DMDE 读取面板文件夹列表...')
        self.status.pack(anchor='w', padx=8)
        ttk.Label(self, text='提示: 如需选择更深层目录里的文件夹, '
                             '请先在 DMDE 中进入该目录后点"刷新"',
                  foreground='#a05000', wraplength=520, justify='left').pack(anchor='w', padx=8)

        btns = ttk.Frame(self)
        btns.pack(fill='x', padx=8, pady=6)
        ttk.Button(btns, text='全选', command=self._sel_all).pack(side='left')
        ttk.Button(btns, text='全不选', command=self._sel_none).pack(side='left', padx=6)
        ttk.Button(btns, text='刷新', command=self._start_fetch).pack(side='left')
        ttk.Button(btns, text='确定', command=self._ok).pack(side='right')
        ttk.Button(btns, text='取消', command=self.destroy).pack(side='right', padx=6)

        self.after(80, self._poll)
        self._start_fetch()
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        self.wait_window()

    def _start_fetch(self):
        self.listbox.delete(0, 'end')
        self.status.configure(text='正在从 DMDE 读取面板文件夹列表...')

        def work():
            try:
                self._q.put(('ok', self._fetch()))
            except Exception as e:
                self._q.put(('err', str(e)))
        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, data = self._q.get_nowait()
                if kind == 'ok':
                    dirs, cf = data
                    for name in dirs:
                        self.listbox.insert('end', name)
                    self.ctx_label.configure(
                        text='读取层级: %s — 选择的文件夹必须是这一层级里的' % cf)
                    self.status.configure(text='共 %d 个文件夹, 按住 Ctrl/Shift 多选后点确定' % len(dirs))
                else:
                    self.status.configure(text='读取失败: %s' % data)
        except queue.Empty:
            pass
        self.after(120, self._poll)

    def _sel_all(self):
        self.listbox.selection_set(0, 'end')

    def _sel_none(self):
        self.listbox.selection_clear(0, 'end')

    def _ok(self):
        self.result = [self.listbox.get(i) for i in self.listbox.curselection()]
        if not self.result:
            messagebox.showinfo('提示', '未选中任何文件夹 (可不选, 恢复全部)')
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('freedmde — DMDE 免费版批量自动恢复')
        self.geometry('860x660')
        self.minsize(760, 560)
        self.q = queue.Queue()
        self.worker = None
        self.cancel_event = threading.Event()
        self.progress = {}
        self.result = None          # ('done'|'stopped'|'error', msg)
        self._pids = {}
        self._build()
        self._load_settings()
        self.refresh_pids()
        self.after(120, self._poll)

    # ---------------- 界面 ----------------
    def _build(self):
        pad = dict(padx=8, pady=4)
        top = ttk.Frame(self)
        top.pack(fill='x', **pad)

        admin = is_admin()
        self.banner = tk.Label(
            top,
            text=('✔ 已以管理员身份运行' if admin
                  else '✘ 未以管理员运行 — 自动化需要与 DMDE 相同权限, 请点右侧按钮'),
            fg=('green' if admin else '#c00000'),
            font=('Microsoft YaHei UI', 10, 'bold'))
        self.banner.pack(side='left')
        if not admin:
            ttk.Button(top, text='以管理员身份重启',
                       command=self._relaunch_admin).pack(side='right')

        row1 = ttk.Frame(self)
        row1.pack(fill='x', **pad)
        ttk.Label(row1, text='DMDE 进程:').pack(side='left')
        self.pid_var = tk.StringVar(value='自动检测')
        self.pid_combo = ttk.Combobox(row1, textvariable=self.pid_var,
                                      state='readonly', width=52)
        self.pid_combo.pack(side='left', padx=6)
        ttk.Button(row1, text='刷新', width=6,
                   command=self.refresh_pids).pack(side='left')

        row2 = ttk.Frame(self)
        row2.pack(fill='x', **pad)
        ttk.Label(row2, text='输出目录:').pack(side='left')
        self.out_var = tk.StringVar()
        ttk.Entry(row2, textvariable=self.out_var).pack(
            side='left', fill='x', expand=True, padx=6)
        ttk.Button(row2, text='浏览...', command=self._browse).pack(side='left')

        row3 = ttk.Frame(self)
        row3.pack(fill='x', **pad)
        ttk.Label(row3, text='每批文件数:').pack(side='left')
        self.chunk_var = tk.IntVar(value=3900)
        ttk.Spinbox(row3, from_=50, to=4000, increment=50, width=7,
                    textvariable=self.chunk_var).pack(side='left', padx=(4, 14))
        self.preview_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text='预览模式 (只统计不恢复)',
                        variable=self.preview_var).pack(side='left', padx=(0, 14))
        self.fresh_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text='忽略断点重来',
                        variable=self.fresh_var).pack(side='left', padx=(0, 14))
        self.debug_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text='调试输出',
                        variable=self.debug_var).pack(side='left')
        self.train_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row3, text='试跑', variable=self.train_var).pack(side='left', padx=(14, 2))
        self.train_batches = tk.IntVar(value=3)
        ttk.Spinbox(row3, from_=1, to=100, width=4,
                    textvariable=self.train_batches).pack(side='left')
        ttk.Label(row3, text='批后自动停').pack(side='left', padx=(2, 0))
        self.goto_root_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row3, text='开始前回到 $Root',
                        variable=self.goto_root_var).pack(side='left', padx=(14, 0))
        ttk.Label(row3, text='起始目录(可选):').pack(side='left', padx=(14, 2))
        self.root_var = tk.StringVar()
        ttk.Entry(row3, textvariable=self.root_var, width=14).pack(side='left')

        row3b = ttk.Frame(self)
        row3b.pack(fill='x', **pad)
        ttk.Button(row3b, text='选择要恢复的文件夹...',
                   command=self._pick_folders).pack(side='left')
        ttk.Label(row3b, text='已限定:').pack(side='left', padx=(10, 2))
        self.only_var = tk.StringVar()
        tk.Label(row3b, textvariable=self.only_var, fg='#0055cc').pack(side='left')
        ttk.Button(row3b, text='清除', width=5,
                   command=lambda: self.only_var.set('')).pack(side='left', padx=6)
        ttk.Label(row3b, text='(留空 = 恢复全部)').pack(side='left', padx=4)

        row4 = ttk.Frame(self)
        row4.pack(fill='x', **pad)
        self.start_btn = ttk.Button(row4, text='▶ 开始恢复', command=self.start)
        self.start_btn.pack(side='left')
        self.stop_btn = ttk.Button(row4, text='■ 停止', command=self.stop,
                                   state='disabled')
        self.stop_btn.pack(side='left', padx=8)
        self.status_var = tk.StringVar(value='待机 — 请先在 DMDE 中打开卷并停在文件面板')
        tk.Label(row4, textvariable=self.status_var, fg='#0055cc',
                 font=('Microsoft YaHei UI', 9, 'bold')).pack(side='left', padx=14)

        self.log_text = tk.Text(self, height=20, state='disabled',
                                font=('Consolas', 9), bg='#111', fg='#ddd')
        self.log_text.pack(fill='both', expand=True, **pad)

        row5 = ttk.Frame(self)
        row5.pack(fill='x', **pad)
        ttk.Button(row5, text='打开输出目录', command=self._open_out).pack(side='left')
        ttk.Button(row5, text='打开日志文件', command=self._open_log).pack(side='left', padx=8)
        tk.Label(row5, text='⚠ 运行期间请勿移动鼠标/键盘 (自动化在模拟真人操作);'
                            ' 中止可直接关闭 DMDE 窗口, 断点已保存。',
                 fg='#a05000').pack(side='right')

    # ---------------- 行为 ----------------
    def _log(self, msg, tag=None):
        self.log_text.configure(state='normal')
        self.log_text.insert('end', msg + '\n', tag)
        self.log_text.see('end')
        self.log_text.configure(state='disabled')

    def _browse(self):
        d = FolderPicker(self, initial=self.out_var.get().strip())
        if d.result:
            self.out_var.set(d.result)

    def _open_out(self):
        out = self.out_var.get().strip()
        if out and os.path.isdir(out):
            os.startfile(out)

    def _open_log(self):
        out = self.out_var.get().strip()
        p = os.path.join(out, '_dmde_auto.log') if out else ''
        if p and os.path.exists(p):
            os.startfile(p)
        else:
            messagebox.showinfo('提示', '日志文件尚未生成。')

    def _relaunch_admin(self):
        params = '"%s"' % os.path.abspath(__file__)
        try:
            ctypes.windll.shell32.ShellExecuteW(None, 'runas', sys.executable,
                                                params, BASE, 1)
            self.destroy()
        except Exception as e:
            messagebox.showerror('失败', '提权重启失败:\n%s' % e)

    def refresh_pids(self):
        try:
            self._pids = engine.list_dmde_pids()
        except Exception as e:
            self._log('检测 DMDE 进程失败: %s' % e)
            self._pids = {}
        values = ['自动检测']
        for pid, title in sorted(self._pids.items()):
            values.append('%d — %s' % (pid, title[:44]))
        self.pid_combo['values'] = values
        if len(self._pids) == 1:
            self.pid_var.set(values[1])
        elif self.pid_var.get() not in values:
            self.pid_var.set('自动检测')
        n = len(self._pids)
        self._log('检测到 %d 个 DMDE 实例%s' % (n, '' if n else ' — 请先以管理员启动 DMDE 并打开卷'))

    def _selected_pid(self):
        v = self.pid_var.get()
        if v == '自动检测':
            return None
        try:
            return int(v.split(' —')[0])
        except Exception:
            return None

    def _pick_folders(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo('提示', '正在恢复中, 无法读取列表')
            return
        pid = self._selected_pid()
        out = self.out_var.get().strip() or BASE

        def fetch():
            ns = argparse.Namespace(out=out, pid=pid, chunk=3900, root=None,
                                    list_only=True, fresh=True, max_batches=0,
                                    goto_root=False, only=None, debug=False)
            r = engine.Recoveror(ns)
            r.pid = engine.find_dmde_pid(pid)
            d = engine.DMDE(r)
            w = d.top()
            if w is not None and d.is_popup(w):
                d.pump(max_rounds=3)
            cf = d.current_folder() or '(当前面板)'
            dirs = d.read_panel_dirs()
            return dirs, cf

        dlg = FolderSelectDialog(self, fetch)
        if dlg.result:
            self.only_var.set(', '.join(dlg.result))
            # 选择的文件夹来自 DMDE 当前面板层级, 开始时不能跳回 $Root
            self.goto_root_var.set(False)
            messagebox.showinfo(
                '已限定 %d 个文件夹' % len(dlg.result),
                '已取消勾选"开始前回到 $Root" (选择来自 DMDE 当前面板层级)。'
                '如果之后想恢复全部, 请点"清除"并重新勾选"开始前回到 $Root"。')

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning('提示', '请先选择输出目录 (务必是另一块物理磁盘)!')
            return
        if not os.path.isabs(out):
            out = os.path.abspath(out)
        try:
            chunk = int(self.chunk_var.get())
        except Exception:
            chunk = 3900
        chunk = max(50, min(4000, chunk))
        only = [x.strip() for x in self.only_var.get().split(',') if x.strip()] or None
        args = argparse.Namespace(
            out=out, pid=self._selected_pid(), chunk=chunk,
            root=(self.root_var.get().strip() or None),
            list_only=bool(self.preview_var.get()),
            fresh=bool(self.fresh_var.get()),
            max_batches=(int(self.train_batches.get()) if self.train_var.get() else 0),
            goto_root=bool(self.goto_root_var.get()),
            only=only,
            debug=bool(self.debug_var.get()))
        try:
            self._save_settings(out, chunk)
        except Exception:
            pass
        if not self.preview_var.get() and not is_admin():
            messagebox.showerror('权限不足',
                                 '未以管理员身份运行, 无法控制 DMDE 窗口。\n'
                                 '请点右上角"以管理员身份重启"。')
            return
        self.cancel_event.clear()
        self.result = None
        self.progress.clear()
        self.start_btn.configure(state='disabled')
        self.stop_btn.configure(state='normal')
        self._log('=' * 62)
        self._log('开始: 输出=%s 每批=%d 预览=%s' % (out, chunk, bool(self.preview_var.get())))
        self.worker = threading.Thread(
            target=self._worker, args=(args,), daemon=True)
        self.worker.start()

    def _worker(self, args):
        try:
            engine.run(args,
                       log_cb=lambda m: self.q.put(m),
                       cancel=lambda: self.cancel_event.is_set(),
                       progress=self.progress)
            self.result = ('done', '全部完成')
        except engine.Cancelled:
            self.result = ('stopped', '已停止 (断点已保存, 重新开始可续传)')
        except SystemExit as e:
            self.result = ('error', str(e))
        except Exception:
            self.result = ('error', traceback.format_exc(limit=6))

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self._log('>> 已请求停止: 将在安全点 (当前批次完成后) 停止, 请稍候...')
            self.stop_btn.configure(state='disabled')

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                self._log(msg)
        except queue.Empty:
            pass
        if self.progress:
            p = self.progress
            self.status_var.set('阶段: %s | 当前: %s | 已恢复文件: %s | 批次: %s' % (
                p.get('phase', '-'), p.get('folder', '-'),
                p.get('files', 0), p.get('batches', 0)))
        if self.worker and not self.worker.is_alive() and \
                not self.start_btn['state'] == 'normal':
            kind, msg = self.result or ('error', '未知状态')
            if kind == 'done':
                self._log('✔ ' + msg)
                self.status_var.set('✔ 全部完成')
                messagebox.showinfo('完成', '恢复全部完成!\n' + self.out_var.get())
            elif kind == 'stopped':
                self._log('■ ' + msg)
                self.status_var.set('■ 已停止 (断点已保存)')
            else:
                self._log('✘ 出错:\n' + msg)
                self.status_var.set('✘ 出错 — 详情见日志')
                messagebox.showerror('出错', msg[:1200])
            self.start_btn.configure(state='normal')
            self.stop_btn.configure(state='disabled')
            self.worker = None
        self.after(150, self._poll)

    # ---------------- 设置 ----------------
    def _load_settings(self):
        s = load_settings()
        if s.get('out'):
            self.out_var.set(s['out'])
        if s.get('chunk'):
            try:
                self.chunk_var.set(int(s['chunk']))
            except Exception:
                pass

    def _save_settings(self, out, chunk):
        with open(SETTINGS, 'w', encoding='utf-8') as f:
            json.dump({'out': out, 'chunk': chunk}, f, ensure_ascii=False)


def main():
    app = App()
    app.mainloop()


if __name__ == '__main__':
    main()
