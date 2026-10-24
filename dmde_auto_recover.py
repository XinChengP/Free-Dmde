# -*- coding: utf-8 -*-
"""
dmde_auto_recover.py — DMDE 免费版自动恢复脚本（按原相对路径导出）

原理:
  DMDE 免费版每次恢复操作只能处理"活动面板"(当前打开的目录)中最多 4000 个文件,
  但操作次数不限。本脚本通过 GUI 自动化(pywinauto)模拟人工操作:
  逐个目录进入 -> 标记当前目录下的文件(不含子目录) -> Recover -> 自动点击
  "Continue with Free Edition"/完成统计框 -> 记录断点 -> 递归进入子目录。
  目录本身不由 DMDE 恢复, 而是脚本按原相对路径在输出目录创建, 每个目录的文件
  直接恢复到 输出目录\\原相对路径\\ 下, 从而完整还原目录结构。

用法 (必须"以管理员身份"运行, 与 DMDE 相同权限):
  1. 手动在 DMDE 中完成: 选择磁盘 -> 打开卷(或完成扫描并打开重建的文件系统),
     停在文件面板, 让右侧面板显示要恢复的起始目录。
  2. 运行:
       python dmde_auto_recover.py --out D:\\recovered
     可选参数:
       --pid N          指定 DMDE 进程 PID (多开时; 默认自动查找, 多个时报错)
       --chunk N        每次恢复操作的文件数上限, 默认 3900 (官方上限 4000)
       --root NAME      先进入当前面板中名为 NAME 的目录再开始 (默认: 当前面板位置)
       --list-only      只遍历并统计, 不实际恢复 (预览/核对)
       --fresh          忽略断点记录, 从头开始
       --debug          调试输出

依赖: pip install pywinauto
"""
import argparse
import ctypes
import json
import os
import re
import sys
import time
import traceback

from pywinauto import Application, Desktop
from pywinauto.keyboard import send_keys

RE_NAG = re.compile(r'Free Edition|继续免费版|Continue with Free', re.I)
RE_STATS = re.compile(r'Total files recovered|已恢复的文件|recovered', re.I)
RE_SKIP = re.compile(r'^(Skip all|Skip|Ignore|Ignore all|跳过全部|跳过|忽略)$', re.I)
RE_OK = re.compile(r'^&?OK$|^确定$', re.I)
RE_PROGRESS_BTN = re.compile(r'Abort|Pause|中止|暂停|Cancel', re.I)
RE_RD_TITLE = re.compile(r'^(Recover|恢复)$')


class Cancelled(Exception):
    """用户请求停止(在安全点抛出, 断点已保存)。"""


# ---------------------------------------------------------------- 核心

class Recoveror:
    """输出目录/断点状态/日志。"""

    def __init__(self, args):
        self.args = args
        self.out_root = os.path.abspath(args.out)
        os.makedirs(self.out_root, exist_ok=True)
        self.state_path = os.path.join(self.out_root, '_dmde_auto_state.json')
        self.log_path = os.path.join(self.out_root, '_dmde_auto.log')
        self.state = {'folders': {}}
        if not args.fresh and os.path.exists(self.state_path):
            try:
                with open(self.state_path, encoding='utf-8') as f:
                    self.state = json.load(f)
            except Exception:
                pass
        self.logf = open(self.log_path, 'a', encoding='utf-8')
        # GUI 集成: 日志回调 / 停止标志 / 进度字典 / 统计
        self.log_cb = None
        self.cancel = None
        self.progress = {}
        self.stats = {'files': 0, 'batches': 0}

    def log(self, *a):
        msg = '[%s] %s' % (time.strftime('%m-%d %H:%M:%S'), ' '.join(str(x) for x in a))
        print(msg, flush=True)
        try:
            self.logf.write(msg + '\n')
            self.logf.flush()
        except Exception:
            pass
        if self.log_cb:
            try:
                self.log_cb(msg)
            except Exception:
                pass

    def cancelled(self):
        try:
            return bool(self.cancel and self.cancel())
        except Exception:
            return False

    def save_state(self):
        tmp = self.state_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)


def list_dmde_pids():
    """返回 {pid: 窗口标题} — 所有带主窗口的 DMDE 进程。"""
    import win32process
    pids = {}
    for w in Desktop(backend='win32').windows(class_name='WCFrame'):
        try:
            t = w.window_text() or ''
            if 'DMDE' in t:
                _, pid = win32process.GetWindowThreadProcessId(w.handle)
                pids[pid] = t
        except Exception:
            pass
    return pids


def find_dmde_pid(prefer=None):
    """查找正在运行、带主窗口的 DMDE 进程。"""
    pids = list_dmde_pids()
    if prefer:
        if prefer not in pids:
            raise SystemExit('指定的 PID %s 不是带主窗口的 DMDE 进程 (找到: %r)' % (prefer, pids))
        return prefer
    if not pids:
        raise SystemExit('未找到运行中的 DMDE 主窗口。请先以管理员启动 DMDE 并打开卷/文件面板。')
    if len(pids) > 1:
        raise SystemExit('检测到多个 DMDE 进程: %r\n请用 --pid 指定要控制的那一个。' % pids)
    return list(pids)[0]


class DMDE:
    def __init__(self, r):
        self.r = r
        self.debug = False
        self.dir_images = set()
        self.file_images = set()
        self.app = Application(backend='win32').connect(process=r.pid)
        self.main = self.app.window(title_re='DMDE .*', class_name='WCFrame')

    # ---------------- 窗口/对话框 ----------------
    def top(self):
        try:
            return self.app.top_window()
        except Exception:
            return None

    def is_popup(self, w):
        try:
            return (w.class_name() == '#32770' and w.is_visible()
                    and (w.window_text() or '') != (self.main.window_text() or ''))
        except Exception:
            return False

    def static_texts(self, w):
        out = []
        try:
            for s in w.descendants(control_type='Static'):
                t = s.window_text()
                if t:
                    out.append(t)
        except Exception:
            pass
        return out

    def buttons(self, w):
        btns = {}
        try:
            for b in w.descendants(class_name='Button'):
                t = b.window_text()
                if t and b.is_enabled() and b.is_visible():
                    btns.setdefault(t, b)
        except Exception:
            pass
        return btns

    def looks_like_progress(self, w):
        try:
            for c in w.descendants():
                cn = c.class_name() or ''
                if 'progress' in cn.lower():
                    return True
            for t in self.buttons(w):
                if RE_PROGRESS_BTN.fullmatch(t):
                    return True
        except Exception:
            pass
        return False

    def _is_radio(self, b):
        try:
            import win32gui
            return (win32gui.GetWindowLong(b.handle, 0xFFF0) & 0xF) in (0x4, 0x9)
        except Exception:
            return False

    def pump(self, max_rounds=50, timeout_s=None):
        """处理 DMDE 各类弹出对话框。返回统计文本(若有)。"""
        stats = None
        t0 = time.time()
        rounds = 0
        last_handle = None
        same_count = 0
        while rounds < max_rounds:
            rounds += 1
            if timeout_s and time.time() - t0 > timeout_s:
                break
            w = self.top()
            if w is None or not self.is_popup(w):
                break
            try:
                h = w.handle
            except Exception:
                h = None
            same_count = same_count + 1 if h == last_handle else 0
            last_handle = h
            title = ''
            try:
                title = w.window_text() or ''
            except Exception:
                pass
            btns = self.buttons(w)
            texts = self.static_texts(w)
            self.r.log('弹窗: %r 文本=%r 按钮=%r' % (title, texts[:5], list(btns)[:6]))

            # 0) 恢复对话框本体: 只能由 run_recovery 处理, pump 绝不能动它
            try:
                if RE_RD_TITLE.fullmatch(w.window_text() or ''):
                    self.r.log('  -> 恢复对话框等待处理...')
                    time.sleep(0.8)
                    continue
            except Exception:
                pass
            hit = False
            # 1) 免费版激活提示
            for t, b in btns.items():
                if RE_NAG.search(t):
                    b.click_input()
                    self.r.log('  -> 点击: %r' % t)
                    time.sleep(0.6)
                    hit = True
                    break
            if hit:
                continue
            # 2) 完成统计框
            if RE_STATS.search(' '.join(texts)):
                stats = ' '.join(texts)
                self.r.log('  -> 恢复完成: %s' % stats[:150])
                for t, b in btns.items():
                    if RE_OK.fullmatch(t):
                        b.click_input()
                        time.sleep(0.6)
                        break
                continue
            # 3) 恢复进度窗 -> 等待, 绝不能关
            if self.looks_like_progress(w):
                self.r.log('  -> 恢复进行中, 等待...')
                time.sleep(1.5)
                continue
            # 4) 重名/错误对话框: 单选框选择 + OK 确认
            choice = None
            for pref in ('Skip all', 'Ignore all', '全部跳过', '跳过全部', 'Skip', 'Ignore', '跳过', '忽略'):
                if pref in btns:
                    choice = (pref, btns[pref])
                    break
            if choice:
                name, b = choice
                if self._is_radio(b):
                    b.click_input()
                    time.sleep(0.4)
                    self.r.log('  -> 选中单选项 %r' % name)
                else:
                    b.click_input()
                    self.r.log('  -> 点击: %r' % name)
                # 验证对话框是否已关闭; 未关则说明是单选框, 需补点 OK
                time.sleep(0.5)
                w2 = self.top()
                still = (w2 is not None and self.is_popup(w2))
                try:
                    still = still and w2.handle == h
                except Exception:
                    pass
                if still:
                    okb = btns.get('&OK') or btns.get('OK') or btns.get('确定')
                    if okb is not None:
                        okb.click_input()
                        self.r.log('  -> 补点 OK 确认')
                time.sleep(0.6)
                continue
            # 5) Yes/No 确认框 (免费版: "Process only files in the active panel?")
            #    答 Yes = 只恢复活动面板文件(≤4000), 正是本脚本的逐目录流程
            for t in ('&Yes', 'Yes', '&是', '是'):
                if t in btns:
                    btns[t].click_input()
                    self.r.log('  -> 点击 Yes')
                    time.sleep(0.6)
                    hit = True
                    break
            if hit:
                continue
            # 6) 未知对话框: 先截图存档 (自绘文字读不到, 便于人工排查), 再尝试 OK/ESC
            self.snap_dialog(w, 'unknown')
            if any(RE_OK.fullmatch(t) for t in btns):
                key = [t for t in btns if RE_OK.fullmatch(t)][0]
                btns[key].click_input()
                self.r.log('  -> 点击 OK (未知弹窗, 请检查日志!)')
                time.sleep(0.5)
                continue
            send_keys('{ESC}')
            self.r.log('  -> ESC (未知弹窗, 请检查日志!)')
            time.sleep(0.5)
        return stats

    def snap_dialog(self, w, tag):
        """把对话框区域截图存到输出目录, 便于人工排查自绘文字的弹窗。"""
        try:
            from PIL import ImageGrab
            r = w.rectangle()
            box = (max(0, r.left - 8), max(0, r.top - 8), r.right + 8, r.bottom + 8)
            img = ImageGrab.grab(bbox=box)
            self.snap_n = getattr(self, 'snap_n', 0) + 1
            p = os.path.join(self.r.out_root, '_dmde_popup_%s_%d.png' % (tag, self.snap_n))
            img.save(p)
            self.r.log('  -> 已截图: %s' % p)
        except Exception:
            pass

    def wait_no_popup(self, timeout=20):
        t0 = time.time()
        while time.time() - t0 < timeout:
            w = self.top()
            if w is None or not self.is_popup(w):
                return True
            self.pump(max_rounds=5)
            time.sleep(0.5)
        return False

    # ---------------- 面板 ----------------
    def lists(self):
        """识别左树/右文件列表 — 按相对主窗口的位置比例, 与窗口摆放位置无关。"""
        left = right = None
        try:
            mrect = self.main.rectangle()
            mw = max(mrect.width(), 1)
        except Exception:
            return None, None
        for c in self.main.descendants(class_name='SysListView32'):
            try:
                if not c.is_visible() or c.item_count() == 0:
                    continue
                rc = c.rectangle()
                rel_left = (rc.left - mrect.left) / mw
                rel_width = (rc.right - rc.left) / mw
                if rel_left < 0.30 and rel_width < 0.45:
                    if left is None:
                        left = c
                elif rel_left >= 0.30:
                    if right is None or c.item_count() >= right.item_count():
                        right = c
            except Exception:
                pass
        return left, right

    def _fast_rows(self, right, n):
        """ctypes + VirtualAllocEx 直读 ListView (name/state/image/尺寸列)。

        比 pywinauto 逐项包装快 5-10 倍; 任何异常时抛出, 调用方回退常规读取。"""
        import ctypes
        from ctypes import wintypes

        LVM_GETITEMW = 0x104B
        LVIF_TEXT, LVIF_IMAGE, LVIF_STATE = 0x001, 0x002, 0x008

        class LVITEMW(ctypes.Structure):
            _fields_ = [('mask', wintypes.UINT), ('iItem', wintypes.INT),
                        ('iSubItem', wintypes.INT), ('state', wintypes.UINT),
                        ('stateMask', wintypes.UINT), ('pszText', ctypes.c_void_p),
                        ('cchTextMax', wintypes.INT), ('iImage', wintypes.INT),
                        ('lParam', ctypes.c_void_p), ('iIndent', wintypes.INT)]

        user32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        hproc = k32.OpenProcess(0x0038, False, self.r.pid)   # VM_OP|VM_READ|VM_WRITE
        if not hproc:
            raise OSError('OpenProcess failed')
        BUF = 8192
        base = None
        try:
            base = k32.VirtualAllocEx(hproc, None, BUF, 0x3000, 0x04)
            if not base:
                raise OSError('VirtualAllocEx failed')
            text_off = 1024        # 名称文本缓冲(宽字符)在远程块内的偏移
            size_off = 4096        # 尺寸列文本偏移

            def read_item(i, sub, text_ptr):
                it = LVITEMW()
                it.mask = LVIF_TEXT | LVIF_IMAGE | LVIF_STATE
                it.iItem = i
                it.iSubItem = sub
                it.stateMask = 0xF000
                it.pszText = ctypes.c_void_p(text_ptr)
                it.cchTextMax = 380
                raw = ctypes.string_at(ctypes.byref(it), ctypes.sizeof(it))
                wrote = ctypes.c_size_t()
                if not k32.WriteProcessMemory(hproc, base, raw, len(raw),
                                              ctypes.byref(wrote)):
                    raise OSError('WriteProcessMemory failed')
                user32.SendMessageW(right.handle, LVM_GETITEMW, 0, base)
                filled = LVITEMW()
                rb = ctypes.create_string_buffer(ctypes.sizeof(filled))
                rr = ctypes.c_size_t()
                k32.ReadProcessMemory(hproc, base, rb, ctypes.sizeof(filled),
                                      ctypes.byref(rr))
                ctypes.memmove(ctypes.byref(filled), rb, ctypes.sizeof(filled))
                got = ctypes.create_string_buffer(2 * 380)
                rd = ctypes.c_size_t()
                k32.ReadProcessMemory(hproc, ctypes.c_void_p(text_ptr), got,
                                      2 * 380, ctypes.byref(rd))
                txt = got.raw[:rd.value].decode('utf-16-le', errors='replace')
                txt = txt.split(chr(0), 1)[0]
                return txt, filled.state, filled.iImage

            attr_off = 6144       # 属性列文本偏移
            out = []
            for i in range(n):
                name, state, img = read_item(i, 0, base + text_off)
                v1 = read_item(i, 1, base + size_off)[0]
                v4 = read_item(i, 4, base + attr_off)[0]
                out.append((i, name, state, img, [v1, '', '', v4]))
            return out
        finally:
            if base:
                try:
                    k32.VirtualFreeEx(hproc, base, 0, 0x8000)
                except Exception:
                    pass
            try:
                k32.CloseHandle(hproc)
            except Exception:
                pass

    def rows(self):
        _, right = self.lists()
        if right is None:
            raise RuntimeError('找不到文件面板列表 (请确认已打开卷且文件面板可见)')
        n = right.item_count()
        if n > 0:
            try:
                fast = self._fast_rows(right, n)
                return fast, right
            except Exception:
                if not getattr(self, '_fast_warned', False):
                    self._fast_warned = True
                    self.r.log('快速读取不可用, 回退常规读取: %s' % traceback.format_exc(limit=1))
        out = []
        for i in range(n):
            it = right.item(i)
            img = None
            try:
                img = it['image']
            except Exception:
                pass
            v1 = v4 = ''
            try:
                v1 = right.item(i, 1).text()
            except Exception:
                pass
            try:
                v4 = right.item(i, 4).text()
            except Exception:
                pass
            subs = [v1, '', '', v4]
            if self.debug:
                for s in range(1, 5):
                    try:
                        subs[s - 1] = right.item(i, s).text()
                    except Exception:
                        subs[s - 1] = None
            out.append((i, it['text'], it['state'] or 0, img, subs))
        return out, right

    def sub_text(self, right, idx, col):
        try:
            return right.item(idx, col).text() or ''
        except Exception:
            return ''

    def is_dir_row(self, right, idx, name, img, v1=None):
        if name.startswith('[..]') or name.startswith('$'):
            return True
        if v1 is None:
            v1 = self.sub_text(right, idx, 1)   # 尺寸列: 目录显示 [directory]
        if 'directory' in v1 or '目录' in v1:
            return True
        v4 = self.sub_text(right, idx, 4)      # 属性列: D 开头 = 目录
        if v4.startswith('D'):
            return True
        if re.search(r'\d', v1):
            return False
        if img is not None:
            if img in self.dir_images:
                return True
            if img in self.file_images:
                return False
        # 仍无法判断: 有扩展名按文件, 否则按目录
        if '.' in name:
            return False
        return True

    def calibrate_images(self, rows, right):
        """用明确的行学习图标索引, 辅助后续判断。"""
        for idx, name, st, img, _s in rows:
            if name.startswith('[..]') or name.startswith('$'):
                continue
            v1 = self.sub_text(right, idx, 1)
            if 'directory' in v1 or '目录' in v1:
                self.dir_images.add(img)
            elif re.search(r'\d', v1):
                self.file_images.add(img)

    def read_panel_dirs(self):
        """只读: 返回当前面板中的目录名列表 (供 GUI 的目录选择器使用)。"""
        rows, right = self.rows()
        dirs = []
        for idx, name, st, img, _s in rows:
            if name.startswith('[..]') or not name.strip():
                continue
            if self.is_dir_row(right, idx, name, img):
                dirs.append(name)
        return dirs

    def wait_stable(self, timeout=15):
        """等待面板行数稳定 (行数快查, 稳定后才做一次完整读取)。"""
        t0 = time.time()
        _, right = self.lists()
        if right is None:
            raise RuntimeError('找不到文件面板列表 (请确认已打开卷且文件面板可见)')
        last = -1
        while time.time() - t0 < timeout:
            n = right.item_count()
            if n == last and n > 0:
                return self.rows()
            last = n
            time.sleep(0.25)
        raise RuntimeError('面板列表为空或不稳定')

    def current_folder(self):
        left, _ = self.lists()
        if left is None:
            return None
        try:
            for i in range(left.item_count()):
                it = left.item(i)
                if (it['state'] or 0) & 0x2:
                    return it['text']
        except Exception:
            pass
        return None

    # ---------------- 标记 ----------------
    def unmark_all(self):
        for _ in range(6):
            rows, right = self.rows()
            marked = [idx for idx, name, st, img, _s in rows if st & 0xc000]
            if not marked:
                return
            for idx in marked:
                try:
                    right.item(idx).click_input()
                    time.sleep(0.12)
                    send_keys('{INSERT}')
                    time.sleep(0.15)
                except Exception:
                    pass

    def mark_file_rows(self, file_idxs, start, count):
        """标记第 start..start+count 个文件行。

        策略: 按 15 行一块做 shift+点击区间标记 (锚点与终点都保证可见),
        每块校验, 缺的用"点击+INSERT"补; 最后取消区间内误标的目录行。"""
        rows, right = self.rows()
        target = file_idxs[start:start + count]
        if not target:
            return []
        STEP = 15
        a = 0
        while a < len(target):
            if self.r.cancelled():
                raise Cancelled('用户停止 (标记中断)')
            b = min(a + STEP, len(target)) - 1   # 含
            lo, hi = target[a], target[b]
            try:
                right.item(lo).click_input()      # 锚点(普通点击=仅选中)
                time.sleep(0.15)
                last_item = right.item(hi)
                try:
                    last_item.ensure_visible()
                except Exception:
                    pass
                time.sleep(0.15)
                send_keys('{VK_SHIFT down}')
                time.sleep(0.12)
                try:
                    last_item.click_input()
                finally:
                    send_keys('{VK_SHIFT up}')
                time.sleep(0.35)
            except Exception:
                self.r.log('区间标记异常(%d-%d): %s' % (lo, hi, traceback.format_exc(limit=1)))
            # 本块校验补标 (只读本块行, 避免整表读取)
            tgt_set = set(target[a:b + 1])
            missing = []
            for i in target[a:b + 1]:
                try:
                    if not (right.item(i)['state'] or 0) & 0xc000:
                        missing.append(i)
                except Exception:
                    missing.append(i)
            if missing:
                self.r.log('块 %d-%d 缺 %d 行, 逐行补标' % (lo, hi, len(missing)))
                for idx in missing:
                    right.item(idx).click_input()
                    time.sleep(0.15)
                    send_keys('{INSERT}')
                    time.sleep(0.2)
            # 取消混入区间的目录行 (标记了但不属于 target 的行)
            try:
                for i in range(lo, hi + 1):
                    if i in tgt_set:
                        continue
                    st = right.item(i)['state'] or 0
                    if st & 0xc000:
                        right.item(i).click_input()
                        time.sleep(0.15)
                        send_keys('{INSERT}')
                        time.sleep(0.2)
                        self.r.log('已取消第 %d 行(非目标)的标记' % i)
            except Exception:
                pass
            a = b + 1
        rows, right = self.rows()
        marked_files = []
        for i in target:
            try:
                if (right.item(i)['state'] or 0) & 0xc000:
                    marked_files.append(i)
            except Exception:
                pass
        self.r.log('标记完成: 目标 %d, 实际 %d' % (len(target), len(marked_files)))
        return marked_files

    # ---------------- 恢复 ----------------
    def open_recover_dialog(self):
        last_err = '未尝试'
        for attempt in range(3):
            self.pump(max_rounds=4)   # 先清掉遗留弹窗
            rec = None
            for b in self.main.descendants(class_name='Button'):
                t = b.window_text() or ''
                if ('Recover' in t or t.startswith('恢复'))                         and b.is_visible() and b.is_enabled():
                    rec = b
                    break
            if rec is None:
                time.sleep(1.5)
                last_err = 'Recover 按钮不可见/不可用'
                continue
            rec.click_input()
            t0 = time.time()
            while time.time() - t0 < 12:
                w = self.top()
                if w is not None and w.class_name() == '#32770':
                    if RE_RD_TITLE.fullmatch(w.window_text() or ''):
                        return w
                    self.pump(max_rounds=3)
                time.sleep(0.4)
            last_err = '点击后面板对话框未出现'
        raise RuntimeError('恢复对话框未出现 (%s)' % last_err)

    def find_recover_dlg(self, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            w = self.top()
            if w is not None and w.class_name() == '#32770' \
                    and RE_RD_TITLE.fullmatch(w.window_text() or ''):
                return w
            time.sleep(0.5)
        raise RuntimeError("恢复对话框未出现")

    def set_destination(self, rd, dest):
        # "Edit"(1033) 是开关: 仅当路径编辑框不可用时才点它
        pe = None
        for _ in range(3):
            try:
                cand = rd.child_window(control_id=529, class_name='Edit')
                if cand.exists(timeout=2) and cand.is_enabled():
                    pe = cand
                    break
            except Exception:
                pass
            try:
                rd.child_window(control_id=1033, class_name='Button').click_input()
            except Exception:
                pass
            time.sleep(0.6)
            rd = self.find_recover_dlg(8)
        if pe is None:
            raise RuntimeError('恢复对话框的目标路径编辑框不可用')
        pe.click_input()
        send_keys('^a{DEL}')
        time.sleep(0.1)
        send_keys(dest)
        time.sleep(0.2)
        got = pe.window_text() or ''
        self.r.log('目标路径: %r' % got)
        if got.rstrip('\\') != dest.rstrip('\\'):
            self.r.log('警告: 路径回读不一致! 回读=%r 期望=%r' % (got, dest))
            raise RuntimeError('目标路径设置失败')

    def ensure_deleted_category(self, dlg):
        """若本批标记的文件含已删除项(属性 x), 点击"已删除"类别标签 (会话内一次)。"""
        if not getattr(self, 'need_deleted', False):
            return
        if getattr(self, '_deleted_clicked', False):
            return
        try:
            for b in dlg.descendants(class_name='Button'):
                t = b.window_text() or ''
                if t in ('已删除', 'deleted'):
                    b.click_input()
                    self._deleted_clicked = True
                    self.r.log('  -> 已点击类别"已删除" (恢复已删除文件)')
                    time.sleep(0.4)
                    return
            self.r.log('  未找到"已删除"类别控件')
        except Exception as e:
            self.r.log('  点击"已删除"失败 (忽略): %s' % e)

    def _row_marked(self, right, i):
        """用 SendMessageTimeout 读第 i 行的标记位 (恢复期间 DMDE 忙也能安全调用)。"""
        import ctypes
        user32 = ctypes.windll.user32
        LVM_GETITEMSTATE = 0x102C
        SMTO_ABORTIFHUNG = 0x0002
        res = ctypes.c_uint(0)
        ok = user32.SendMessageTimeoutW(right.handle, LVM_GETITEMSTATE, i,
                                        0xF000, SMTO_ABORTIFHUNG, 800,
                                        ctypes.byref(res))
        if not ok:
            return None
        return bool(res.value & 0xC000)

    def run_recovery(self, dest, target_idxs=None, right=None):
        rd = self.open_recover_dialog()
        self.set_destination(rd, dest)
        self.ensure_deleted_category(rd)
        rd.child_window(control_id=1, class_name='Button').click_input()
        self.r.log('恢复已启动...')
        stats = None
        t0 = time.time()
        last_min = -1
        stop_note = False
        quiet_done = 0
        while time.time() - t0 < 7200:
            if self.r.cancelled() and not stop_note:
                stop_note = True
                self.r.log('已请求停止: 等待当前批次恢复完成后停止 (请勿关闭 DMDE)...')
            w = self.top()
            # Yes 之后 DMDE 带受限选择重新弹出 Recover 对话框 -> 再点 Recover 才真正执行
            try:
                if w is not None and w.class_name() == '#32770' \
                        and RE_RD_TITLE.fullmatch(w.window_text() or ''):
                    # 重新弹出的恢复对话框: 按钮可能短暂禁用/窗口可能重建,
                    # 每次尝试都重新抓取 top window, 避免旧引用空转
                    clicked = False
                    for _ in range(20):
                        try:
                            w2 = self.top()
                            self.ensure_deleted_category(w2)
                            b = w2.child_window(control_id=1, class_name='Button')
                            if b.exists(timeout=0.5) and b.is_enabled() and b.is_visible():
                                b.click_input()
                                clicked = True
                                break
                        except Exception:
                            pass
                        time.sleep(0.3)
                    self.r.log('  -> 重新点击 Recover (受限面板恢复)' if clicked
                               else '  -> Recover 按钮暂不可用, 稍后重试')
                    time.sleep(0.6)
                    continue
            except Exception:
                pass
            if w is None or not self.is_popup(w):
                # 无弹窗: 判定恢复是否完成
                # 可靠信号: 目标行标记已被 DMDE 清空 (恢复完成后自动清标记)
                if target_idxs and right is not None:
                    try:
                        sample = target_idxs[:10]
                        sts = [self._row_marked(right, i) for i in sample]
                        sts = [x for x in sts if x is not None]
                        if sts and not any(sts):
                            self.r.log('  -> 标记已清空, 恢复完成 (%.1f 秒)' % (time.time() - t0))
                            break
                    except Exception:
                        pass
                # 兜底: 连续无弹窗 (~1.6 秒) 视为完成
                quiet_done += 1
                if quiet_done >= 4:
                    break
                time.sleep(0.4)
                continue
            quiet_done = 0
            st = self.pump(max_rounds=10, timeout_s=60)
            if st:
                stats = st
            time.sleep(0.3)
            mn = int(time.time() - t0) // 60
            if mn > last_min and mn > 0:
                last_min = mn
                self.r.log('  ...恢复中 (%d 分钟)' % mn)
        # 吞掉完成统计框 (若有)
        time.sleep(0.8)
        try:
            w = self.top()
            if w is not None and self.is_popup(w):
                self.pump(max_rounds=4)
        except Exception:
            pass
        return stats

    # ---------------- 遍历 ----------------
    def dblclick_row(self, right, idx, expect_name):
        """双击行进入目录, 验证确实进入 (行集/行数变化为准)。"""
        before_rows, _ = self.rows()
        before_names = set(n for _, n, _, _, _ in before_rows)
        before_count = len(before_rows)
        for attempt in range(3):
            try:
                right.item(idx).click_input(double=True)
            except Exception:
                pass
            time.sleep(1.0)
            self.pump(max_rounds=3)
            rows, right2 = self.rows()
            names = set(n for _, n, _, _, _ in rows)
            if right2.item_count() != before_count or names != before_names:
                return True
            cf = self.current_folder()
            if cf and expect_name and expect_name in cf:
                return True
        cf = self.current_folder()
        raise RuntimeError('无法确认已进入目录 %r (行数 %d -> %d)' % (
            expect_name, before_count, len(rows)))

    def goto_parent(self):
        rows, right = self.rows()
        up = None
        for idx, name, st, img, _s in rows:
            if name.startswith('[..]'):
                up = idx
                break
        if up is None:
            raise RuntimeError('找不到 [..] 行')
        right.item(up).click_input(double=True)
        time.sleep(1.0)
        self.pump(max_rounds=3)
        rows, right = self.rows()
        return rows, right

    def subtree_done(self, relpath):
        """目录及其所有已记录子目录是否全部完成 (基于断点状态, 不打开 DMDE)。"""
        r = self.r
        st = r.state['folders'].get(relpath)
        if not st or not st.get('done'):
            return False
        kids = st.get('children')
        if kids is None:
            return False   # 旧版断点无子目录清单, 保险起见进入检查
        return all(self.subtree_done(k) for k in kids)

    def tree_nav(self, name):
        """在左侧目录树中点击名为 name 的节点跳转 (快于 进入+返回)。"""
        left, _ = self.lists()
        if left is None:
            return False
        idx = None
        for i in range(left.item_count()):
            it = left.item(i)
            if (it['text'] or '') == name:
                idx = i
                break
        if idx is None:
            return False
        before_rows, _ = self.rows()
        before_names = set(n for _, n, _, _, _ in before_rows)
        before_count = len(before_rows)
        try:
            left.item(idx).ensure_visible()
            left.item(idx).click_input()
        except Exception:
            return False
        time.sleep(0.7)
        self.pump(max_rounds=2)
        rows, right = self.rows()
        names = set(n for _, n, _, _, _ in rows)
        cf = self.current_folder()
        if (cf and name in cf) or right.item_count() != before_count or names != before_names:
            return True
        try:
            left.item(idx).ensure_visible()
            left.item(idx).click_input()
        except Exception:
            return False
        time.sleep(1.0)
        self.pump(max_rounds=2)
        rows, right = self.rows()
        names = set(n for _, n, _, _, _ in rows)
        cf = self.current_folder()
        return bool((cf and name in cf) or right.item_count() != before_count or names != before_names)

    def _count_direct_files(self, dest):
        try:
            return sum(1 for e in os.scandir(dest) if e.is_file())
        except Exception:
            return 0

    def _recover_folder_files(self, dest, fstate, relpath):
        """分批标记并恢复当前面板中的文件 (断点推进在此处)。每批前重新分类面板行。"""
        r = self.r
        chunk = r.args.chunk
        while True:
            rows, right = self.rows()
            file_rows = []
            need_del = False
            for idx, name, st, img, subs in rows:
                if name.startswith('[..]') or not name.strip():
                    continue
                if not self.is_dir_row(right, idx, name, img, v1=(subs[0] if subs else None)):
                    file_rows.append(idx)
                    if 'x' in (subs[3] or '').lower():
                        need_del = True
            self.need_deleted = need_del
            done_n = fstate.get('rows_done', 0)
            if done_n >= len(file_rows):
                break
            if r.cancelled():
                raise Cancelled('用户停止 (断点已保存)')
            mb = getattr(r.args, 'max_batches', 0) or 0
            if mb and r.stats['batches'] >= mb:
                raise Cancelled('训练模式: 已完成 %d 批, 自动停止 (断点已保存, 取消限制可继续)' % mb)
            start = done_n
            if start > 0:
                self.unmark_all()   # 只有续传到一半才可能有残留标记
            cnt = min(chunk, len(file_rows) - start)
            marked = self.mark_file_rows(file_rows, start, cnt)
            if not marked:
                raise RuntimeError('第 %d 批标记失败 (目录 %r)' % (start // chunk + 1, relpath))
            if len(marked) != cnt:
                raise RuntimeError(
                    '标记数不符: 目标 %d 实际 %d (目录 %r)。'
                    '运行期间可能有其他窗口/人为操作干扰了鼠标键盘, 请重试。'
                    % (cnt, len(marked), relpath))
            r.log('已标记 %d 个文件, 开始第 %d 批恢复' % (len(marked), start // chunk + 1))
            r.progress.update({'phase': '恢复中', 'folder': relpath or '(起始目录)',
                               'batch_files': len(marked)})
            stats = self.run_recovery(dest, target_idxs=marked, right=right)
            fstate['rows_done'] = start + len(marked)
            r.stats['files'] += len(marked)
            r.stats['batches'] += 1
            r.progress.update({'files': r.stats['files'], 'batches': r.stats['batches'],
                               'folder': relpath or '(起始目录)', 'phase': '已恢复一批'})
            r.save_state()
            r.log('第 %d 批完成: %s' % (start // chunk + 1, (stats or '')[:120]))

    def process_folder(self, relpath, depth=0, start=True):
        r = self.r
        # 循环/超深保护: 损坏盘的重建视图可能出现虚拟环路 (objects/*/*/*...)
        if not hasattr(self, '_visited'):
            self._visited = set()
        if relpath:
            if relpath in self._visited:
                r.log('跳过(已访问过, 疑似重建视图虚拟环路): %s' % relpath[:100])
                return
            self._visited.add(relpath)
            if len(relpath) > 160 or depth > 22:
                r.log('跳过(路径过深/过长, 疑似重建视图伪循环): %s' % relpath[:100])
                return
        fstate = r.state['folders'].setdefault(relpath, {'done': False, 'rows_done': 0})
        rows, right = self.wait_stable()
        file_rows, dir_names = [], []
        for idx, name, st, img, subs in rows:
            if name.startswith('[..]') or not name.strip():
                continue
            if self.is_dir_row(right, idx, name, img, v1=(subs[0] if subs else None)):
                dir_names.append(name)
            else:
                file_rows.append(idx)

        # --only: 限定只恢复选中的文件夹分支 (仅对起始目录生效)
        filtered = False
        only = getattr(r.args, 'only', None)
        if start and only:
            before = len(dir_names)
            dir_names = [d for d in dir_names if d in only]
            file_rows = []
            filtered = True
            r.log('限定恢复文件夹: %r (面板子目录 %d -> 选中 %d, 起始目录下的文件不恢复)'
                  % (only, before, len(dir_names)))

        if fstate.get('done'):
            r.log('文件已恢复过, 跳过标记: %s (子目录 %d)' % (relpath or '(起始目录)', len(dir_names)))
        else:
            # 本目录文件是否含已删除项 (属性列 x) -> 需要勾选"已删除"类别
            self.need_deleted = any('x' in (subs[3] or '').lower()
                                    for idx, name, st, img, subs in rows
                                    if not name.startswith('[..]') and idx in file_rows)
            dest = os.path.join(r.out_root, relpath) if relpath else r.out_root
            t0f = time.time()
            r.log('目录 %r: 文件 %d, 子目录 %d' % (relpath or '(起始目录)', len(file_rows), len(dir_names)))

            if file_rows and not r.args.list_only:
                os.makedirs(dest, exist_ok=True)
                self._recover_folder_files(dest, fstate, relpath)
                # 自愈: 磁盘直连文件数 < 面板文件数 => 有批次曾被中止, 重做一次
                direct = self._count_direct_files(dest)
                if direct < len(file_rows) and not fstate.get('retried'):
                    fstate['retried'] = True
                    fstate['rows_done'] = 0
                    r.save_state()
                    r.log('警告: 磁盘文件数(%d) < 面板文件数(%d), 自动重做本目录 (重名文件自动跳过)'
                          % (direct, len(file_rows)))
                    self._recover_folder_files(dest, fstate, relpath)
            elif file_rows:
                r.log('  (list-only) 将恢复 %d 个文件 -> %s' % (len(file_rows), dest))
                for d0 in dir_names:
                    r.log('  (list-only) 子目录: %s' % d0)

        os.makedirs(os.path.join(r.out_root, relpath) if relpath else r.out_root, exist_ok=True)
        if file_rows and not r.args.list_only and not fstate.get('done'):
            r.log('  本目录标记+恢复耗时 %.1f 秒' % (time.time() - t0f))
        tree_ok = True   # 左侧树导航可用; 失败一次后回退为面板双击导航
        for name in dir_names:
            sub = name if not relpath else relpath + os.sep + name
            sub_dest = os.path.join(r.out_root, sub)
            if len(sub_dest) > 200:
                r.log('跳过(输出路径过长, 疑似伪循环): %s' % sub[:100])
                continue
            os.makedirs(sub_dest, exist_ok=True)
            if r.args.list_only:
                r.log('(list-only) 子目录: %s' % sub)
                continue
            if self.subtree_done(sub):
                r.log('跳过(子树已完成): %s' % sub)
                continue
            # 祖先同名保护: 重建视图可能出现 004/004/004 式伪循环
            ancestors = set(relpath.split(os.sep)) if relpath else set()
            if name in ancestors:
                r.log('跳过(与上级目录同名, 疑似重建视图伪循环): %s' % sub)
                continue
            try:
                if tree_ok and self.tree_nav(name):
                    self.process_folder(sub, depth + 1, start=False)
                    continue
                if tree_ok:
                    tree_ok = False
                    r.log('树导航不可用, 回退为面板双击导航')
                # 树点击可能已进入 name 但验证失败: 此时 current_folder 显示 name
                cf = self.current_folder()
                if cf and name in cf:
                    self.process_folder(sub, depth + 1, start=False)
                    rows, right = self.goto_parent()
                    names = set(n for _, n, _, _, _ in rows)
                    for _ in range(2):
                        if name in names:
                            break
                        self.pump(max_rounds=2)
                        rows, right = self.goto_parent()
                        names = set(n for _, n, _, _, _ in rows)
                    continue
                idx = None
                for attempt in range(5):
                    rows, right = self.rows()
                    for i2, n2, st2, im2, _s2 in rows:
                        if n2 == name:
                            idx = i2
                            break
                    if idx is not None:
                        break
                    # 行不见了: 可能虚拟视图在后台重建, 等待后重读
                    self.r.log('子目录行 %r 暂未找到 (第 %d 次), 等待视图重建...' % (name, attempt + 1))
                    time.sleep(3)
                    self.pump(max_rounds=3)
                if idx is None:
                    r.log('警告: 找不到子目录行 %r, 跳过' % name)
                    continue
                self.dblclick_row(right, idx, name)
                self.process_folder(sub, depth + 1, start=False)
                rows, right = self.goto_parent()
                names = set(n for _, n, _, _, _ in rows)
                for _ in range(3):
                    if name in names:
                        break
                    self.pump(max_rounds=3)
                    rows, right = self.goto_parent()
                    names = set(n for _, n, _, _, _ in rows)
                if name not in names:
                    raise RuntimeError('返回上级失败: %s <- %s' % (relpath, sub))
            except Cancelled:
                raise
            except SystemExit:
                raise
            except Exception as e:
                r.log('警告: 处理子目录 %r 时出错, 跳过: %s' % (name, e))
                # 尝试回到本目录, 保证后续兄弟分支继续
                try:
                    cf = self.current_folder()
                    if cf and name in cf:
                        pass   # 已在本目录内(未进入成功), 继续
                    else:
                        rows, right = self.goto_parent()
                except Exception:
                    pass
                raise RuntimeError('返回上级失败: %s <- %s' % (relpath, sub))
        if filtered:
            fstate['children'] = dir_names
            r.save_state()
            r.log('限定范围的分支已完成: %s (起始目录保持未完成, 之后不限范围运行会补齐其余分支)' % (relpath or '(起始目录)'))
        else:
            fstate['done'] = True
            fstate['children'] = dir_names
            r.save_state()
            r.log('目录完成: %s' % (relpath or '(起始目录)'))


def run(args, log_cb=None, cancel=None, progress=None):
    """执行恢复。args 为 argparse.Namespace; log_cb/cancel/progress 供 GUI 集成。"""
    r = Recoveror(args)
    r.log_cb = log_cb
    r.cancel = cancel
    if progress is not None:
        r.progress = progress
    only = getattr(args, 'only', None)
    if isinstance(only, str):
        only = [x.strip() for x in only.split(',') if x.strip()] or None
    args.only = only
    pid = find_dmde_pid(getattr(args, 'pid', None))
    r.pid = pid
    r.log('连接 DMDE PID=%s -> 输出 %s%s' % (pid, r.out_root,
          ' (list-only)' if getattr(args, 'list_only', False) else ''))
    r.progress.update({'phase': '已连接 DMDE', 'files': 0, 'batches': 0})
    d = DMDE(r)
    d.debug = getattr(args, 'debug', False)

    # 前置检查
    w = d.top()
    if w is not None and d.is_popup(w):
        texts = ' '.join(d.static_texts(w))
        if 'parameters' in texts or '参数' in texts:
            raise SystemExit('DMDE 有文件系统参数对话框待处理, 请先手动选择参数后重试。')
        if not d.wait_no_popup(180):
            raise SystemExit('DMDE 有未处理的弹窗, 请先手动处理。')

    rows, right = d.wait_stable()
    if args.debug:
        for idx, name, st, img, subs in rows[:10]:
            d.r.log('row %d %r st=%x img=%s cols=%r' % (idx, name, st, img, subs))
    # 运行期图标标定: 已知目录行(带$前缀的虚拟目录除外) -> 收集 img
    # 简化: 通过"尺寸列含数字=文件"为主, 图标作为辅助; 此处只记录分布
    imgs = {}
    for idx, name, st, img, _s in rows:
        imgs.setdefault(img, 0)
        imgs[img] += 1
    d.r.log('行图标分布: %r' % imgs)
    d.calibrate_images(rows, right)

    # 回到 $Root, 使续传起点确定 (面板可能停在子目录)
    if getattr(args, 'goto_root', False) and not args.root:
        ok = False
        try:
            left, _ = d.lists()
            if left is not None:
                node = None
                for i in range(left.item_count()):
                    it = left.item(i)
                    if (it['text'] or '').startswith('$Root'):
                        node = it
                        break
                if node is not None:
                    for attempt in range(3):
                        try:
                            node.ensure_visible()
                            node.click_input(double=True)
                        except Exception:
                            pass
                        time.sleep(1.5)
                        d.pump(max_rounds=2)
                        cf = d.current_folder()
                        if cf and '$root' in cf.lower():
                            ok = True
                            break
        except Exception as e:
            r.log('回到 $Root 异常: %s' % e)
        if ok:
            rows, right = d.wait_stable()
            r.log('已回到 $Root (%d 行)' % len(rows))
        else:
            raise SystemExit('无法自动回到 $Root (树点击无效)。'
                             '请手动在 DMDE 左侧树双击 $Root, 再点开始。')

    if args.root:
        idx = None
        for i2, n2, st2, im2, _s2 in rows:
            if n2 == args.root:
                idx = i2
                break
        if idx is None:
            raise SystemExit('当前面板中找不到目录 %r' % args.root)
        d.dblclick_row(right, idx, args.root)

    d.process_folder('')
    r.log('全部完成。输出目录: %s' % r.out_root)
    r.progress.update({'phase': '全部完成'})
    return r


def main():
    ap = argparse.ArgumentParser(description='DMDE 免费版自动恢复 (按原相对路径导出)')
    ap.add_argument('--out', required=True, help='输出目录 (必须与源盘/镜像不同的物理磁盘)')
    ap.add_argument('--pid', type=int, default=None, help='DMDE 进程 PID (多开时指定)')
    ap.add_argument('--chunk', type=int, default=3900, help='每次恢复的文件数上限 (默认 3900)')
    ap.add_argument('--root', default=None, help='先进入当前面板中名为 NAME 的目录')
    ap.add_argument('--list-only', action='store_true', help='只遍历统计, 不恢复')
    ap.add_argument('--fresh', action='store_true', help='忽略断点, 重新开始')
    ap.add_argument('--max-batches', type=int, default=0, help='训练模式: 恢复 N 批后自动停止 (0=不限)')
    ap.add_argument('--goto-root', action='store_true', help='开始前先回到 $Root (续传时建议)')
    ap.add_argument('--only', default=None, help='限定只恢复这些文件夹 (逗号分隔, 对起始目录生效)')
    ap.add_argument('--debug', action='store_true', help='调试输出')
    run(ap.parse_args())


if __name__ == '__main__':
    try:
        main()
    except Cancelled:
        print('\n已停止, 断点已保存, 重新运行可续传。', file=sys.stderr)
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        print('\n出错终止。断点已保存, 排除问题后重新运行即可续传。', file=sys.stderr)
        sys.exit(1)
