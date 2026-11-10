#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Codex Auto-Retry V2

V3 在 V2 基础上做系统清理：
  - 配置集中到 app_config.py，重试冷却恒定 2 秒，无指数退避、无累计状态；
  - 保留最近一轮进展判定、两种恢复方式、自动贴底、发送验证和 fail-closed 点击；
  - 修复前台激活时的 Win32 线程 ID 调用，移除重复入口和过时冷却接口；
  - 脚本、日志、互斥锁、单实例检查全部限定 V3，不影响原版或 V2。
"""


import os
import sys
import time
import re
import datetime
import statistics
import ctypes
import math
import argparse
import logging
from logging.handlers import RotatingFileHandler
from ctypes import wintypes
import psutil
from PIL import Image
import numpy as np
import cv2
import win32ui

# ==================== DPI 感知（必须在任何窗口 API 之前） ====================
try:
    dpi_api = ctypes.windll.user32.SetProcessDpiAwarenessContext
    dpi_api.argtypes = [wintypes.HANDLE]
    dpi_api.restype = wintypes.BOOL
    if not dpi_api(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 旧系统: PER_MONITOR_V1
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:
    print("[ERROR] 未检测到 rapidocr_onnxruntime，请先运行: pip install rapidocr_onnxruntime")
    sys.exit(1)

from app_config import CONFIG, MAX_ALLOWED_COOLDOWN

# ==================== Win32 API 封装 ====================
u32 = ctypes.WinDLL("user32", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
g32 = ctypes.WinDLL("gdi32", use_last_error=True)

# HWND/HDC/HBITMAP 在 64 位 Windows 上不能使用 ctypes 默认的 32 位返回值。
ULONG_PTR = ctypes.c_size_t


def bind_api(library, name, args, result):
    function = getattr(library, name)
    function.argtypes = args
    function.restype = result
    return function


bind_api(u32, "GetForegroundWindow", [], wintypes.HWND)
bind_api(u32, "GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL)
bind_api(u32, "GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD)
bind_api(u32, "GetWindowTextLengthW", [wintypes.HWND], ctypes.c_int)
bind_api(u32, "GetWindowTextW", [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int)
bind_api(u32, "IsWindowVisible", [wintypes.HWND], wintypes.BOOL)
bind_api(u32, "IsIconic", [wintypes.HWND], wintypes.BOOL)
bind_api(u32, "GetDC", [wintypes.HWND], wintypes.HDC)
bind_api(u32, "ReleaseDC", [wintypes.HWND, wintypes.HDC], ctypes.c_int)
bind_api(u32, "GetCursorPos", [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL)
bind_api(u32, "SetCursorPos", [ctypes.c_int, ctypes.c_int], wintypes.BOOL)
bind_api(u32, "GetAsyncKeyState", [ctypes.c_int], ctypes.c_short)
bind_api(u32, "AttachThreadInput", [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL)
bind_api(u32, "SetForegroundWindow", [wintypes.HWND], wintypes.BOOL)
bind_api(u32, "BringWindowToTop", [wintypes.HWND], wintypes.BOOL)
bind_api(u32, "MapVirtualKeyW", [wintypes.UINT, wintypes.UINT], wintypes.UINT)
bind_api(u32, "keybd_event", [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ULONG_PTR], None)
bind_api(u32, "mouse_event", [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ULONG_PTR], None)
bind_api(g32, "CreateCompatibleDC", [wintypes.HDC], wintypes.HDC)
bind_api(g32, "CreateCompatibleBitmap", [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP)
bind_api(g32, "SelectObject", [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE)
bind_api(g32, "DeleteObject", [wintypes.HANDLE], wintypes.BOOL)
bind_api(g32, "DeleteDC", [wintypes.HDC], wintypes.BOOL)
bind_api(g32, "BitBlt", [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD], wintypes.BOOL)

u32.WindowFromPoint.argtypes = [wintypes.POINT]
u32.WindowFromPoint.restype = wintypes.HWND
u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
u32.GetAncestor.restype = wintypes.HWND
u32.GetDpiForWindow.argtypes = [wintypes.HWND]
u32.GetDpiForWindow.restype = wintypes.UINT

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
bind_api(u32, "EnumWindows", [WNDENUMPROC, wintypes.LPARAM], wintypes.BOOL)
bind_api(k32, "SetConsoleTitleW", [wintypes.LPCWSTR], wintypes.BOOL)

VK_CONTROL = 0x11
VK_V = 0x56
VK_RETURN = 0x0D
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_SHIFT = 0x10
VK_LWIN = 0x5B
VK_RWIN = 0x5C
KEYEVENTF_UNICODE = 0x0004


RECT = wintypes.RECT


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", INPUTUNION)]


bind_api(u32, "SendInput", [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int], wintypes.UINT)
bind_api(k32, "CreateMutexW", [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE)
bind_api(k32, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
bind_api(k32, "GetCurrentThreadId", [], wintypes.DWORD)


def user_input_active():
    return any(u32.GetAsyncKeyState(key) & 0x8000
               for key in (VK_LBUTTON, VK_RBUTTON, VK_CONTROL, VK_SHIFT, VK_MENU, VK_LWIN, VK_RWIN))


def get_window_rect(hwnd):
    rect = RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (0, 0, 0, 0)
    return (rect.left, rect.top, rect.right, rect.bottom)


def get_dpi_scale(hwnd):
    """窗口当前 DPI 缩放比例（150% 返回 1.5）"""
    try:
        dpi = u32.GetDpiForWindow(hwnd)
        if dpi:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0


def root_window_at(x, y):
    """屏幕坐标 (x, y) 处最顶层窗口所属的根窗口句柄"""
    h = u32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    if not h:
        return 0
    return u32.GetAncestor(h, 2) or h  # GA_ROOT


def get_foreground_root():
    h = u32.GetForegroundWindow()
    if not h:
        return 0
    return u32.GetAncestor(h, 2) or h


def find_target_windows():
    """查找所有 Codex / ChatGPT 窗口"""
    targets = []

    def worker(hwnd, lParam):
        if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
            return True
        left, top, right, bottom = get_window_rect(hwnd)
        w, h = right - left, bottom - top
        if w < 500 or h < 400:
            return True

        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            pname = psutil.Process(pid.value).name().lower()
        except Exception:
            pname = ''
        for ex in CONFIG["EXCLUDE_PROCESSES"]:
            if ex in pname:
                return True

        length = u32.GetWindowTextLengthW(hwnd)
        title = ''
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            u32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value

        # 不按标题误选浏览器或文档，只允许明确的原生客户端进程。
        is_match = pname in {p.lower() for p in CONFIG["TARGET_PROCESSES"]}
        if is_match:
            targets.append({"hwnd": hwnd, "pid": pid.value, "process": pname,
                            "title": title, "rect": (left, top, right, bottom)})
        return True

    cb = WNDENUMPROC(worker)
    u32.EnumWindows(cb, 0)
    return targets


def clip_to_virtual_desktop(rect):
    """虚拟桌面可包含负坐标，不能只使用主屏的宽高。"""
    left, top = u32.GetSystemMetrics(76), u32.GetSystemMetrics(77)
    right = left + u32.GetSystemMetrics(78)
    bottom = top + u32.GetSystemMetrics(79)
    return (max(left, rect[0]), max(top, rect[1]), min(right, rect[2]), min(bottom, rect[3]))


def capture_window_gdi(rect):
    """截取物理矩形；所有 GDI 资源在失败路径也会释放。"""
    cl, ct, cr, cb = clip_to_virtual_desktop(rect)
    w, h = cr - cl, cb - ct
    if w <= 0 or h <= 0:
        return None
    hdc_screen = u32.GetDC(0)
    hdc_mem = hbm = previous = None
    try:
        if not hdc_screen:
            return None
        hdc_mem = g32.CreateCompatibleDC(hdc_screen)
        if not hdc_mem:
            return None
        hbm = g32.CreateCompatibleBitmap(hdc_screen, w, h)
        if not hbm:
            return None
        previous = g32.SelectObject(hdc_mem, hbm)
        if not previous or previous == ctypes.c_void_p(-1).value:
            return None
        if not g32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, cl, ct, 0x00CC0020 | 0x40000000):
            return None
        bmp = win32ui.CreateBitmapFromHandle(hbm)
        info = bmp.GetInfo()
        data = bmp.GetBitmapBits(True)
        return Image.frombuffer('RGB', (info['bmWidth'], info['bmHeight']), data, 'raw', 'BGRX', 0, 1).copy()
    finally:
        if previous and previous != ctypes.c_void_p(-1).value:
            g32.SelectObject(hdc_mem, previous)
        if hbm:
            g32.DeleteObject(hbm)
        if hdc_mem:
            g32.DeleteDC(hdc_mem)
        if hdc_screen:
            u32.ReleaseDC(0, hdc_screen)


def capture_window_snapshot(hwnd):
    """只截取最新画面，不执行 OCR；坐标与实际裁剪后的图像严格一致。"""
    if not u32.IsWindowVisible(hwnd) or u32.IsIconic(hwnd):
        return None
    rect = get_window_rect(hwnd)
    clipped = clip_to_virtual_desktop(rect)
    scale = get_dpi_scale(hwnd)
    # 最大化窗口的透明边框允许少量裁剪；大幅离屏时不估算输入框。
    if any(abs(a - b) > 12 * scale for a, b in zip(rect, clipped)):
        return None
    img = capture_window_gdi(clipped)
    if img is None:
        return None
    return clipped, np.array(img), scale


def force_activate_window(hwnd):
    """请求前台焦点并验证；不用 Alt 脉冲，始终解除线程输入附加。"""
    attached = False
    cur_tid = target_tid = 0
    try:
        if user_input_active():
            return False
        cur_tid = k32.GetCurrentThreadId()
        target_tid_ref = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(target_tid_ref))
        target_tid = target_tid_ref.value
        if target_tid and target_tid != cur_tid:
            attached = bool(u32.AttachThreadInput(cur_tid, target_tid, True))
        u32.BringWindowToTop(hwnd)
        u32.SetForegroundWindow(hwnd)
        time.sleep(0.15)
        return get_foreground_root() == hwnd
    except Exception:
        return False
    finally:
        if attached:
            u32.AttachThreadInput(cur_tid, target_tid, False)


def press_key(vk, is_down=True):
    """使用经典的 Windows 原生 keybd_event 发送键盘事件，带驱动硬件扫描码，百分之百避免 64 位结构体尺寸错误"""
    flags = 0 if is_down else KEYEVENTF_KEYUP
    scan = u32.MapVirtualKeyW(vk, 0)
    u32.keybd_event(vk, scan, flags, 0)


def send_unicode_text(text):
    """Unicode SendInput 不读写剪贴板（文字、图片、文件均保留）。"""
    encoded = text.encode("utf-16-le")
    events = []
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset:offset + 2], "little")
        for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
            event = INPUT(type=1)
            event.ki = KEYBDINPUT(0, unit, flags, 0, 0)
            events.append(event)
    if not events:
        return False
    batch = (INPUT * len(events))(*events)
    return u32.SendInput(len(batch), batch, ctypes.sizeof(INPUT)) == len(batch)


def send_enter_key():
    """回车键在异常路径也释放，避免按键保持按下。"""
    press_key(VK_RETURN, True)
    try:
        time.sleep(0.06)
    finally:
        press_key(VK_RETURN, False)


def click_screen_point(x, y):
    if not u32.SetCursorPos(int(x), int(y)):
        return False
    time.sleep(0.05)
    u32.mouse_event(0x0002, 0, 0, 0, 0)
    try:
        time.sleep(0.06)
    finally:
        u32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.05)
    return True


def clean_text(text):
    return re.sub(r'[\s\r\n\[\]\(\)\{\}，。！？\.,!?:\-_\'\"]', '', text)


def squash(text):
    """小写并去掉所有空白，用于关键词匹配"""
    return re.sub(r'\s+', '', text.lower())


# ==================== 核心监控类 ====================
class CodexAutoRetryApp:
    def __init__(self, dry_run=False, logger=None):
        self.ocr = RapidOCR()
        self.dry_run = dry_run
        self.logger = logger
        self.mode1_count = 0
        self.mode2_count = 0
        self.last_action_time = {}
        self.start_time = time.monotonic()
        self.last_sig = {}
        self.last_sig_time = {}
        self.scroll_count = 0
        self.last_scroll_check = {}
        self.scroll_stall_until = {}
        self.preferred_hwnd = None

        self.pencil_template = None
        tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pencil_template.png")
        if os.path.exists(tpl_path):
            try:
                self.pencil_template = cv2.imread(tpl_path)
            except Exception:
                pass

    def log(self, tag, message, color="37"):
        now = datetime.datetime.now().strftime("%H:%M:%S")
        line = f"[{now}] [{tag}] {message}"
        if self.logger is not None:
            self.logger.info(line)
        try:
            print(f"\033[{color}m{line}\033[0m", flush=True)
        except Exception:
            try:
                print(line, flush=True)
            except Exception:
                pass

    @staticmethod
    def window_exposure(hwnd, rect):
        """采样窗口内部点，估算未被其他窗口遮挡的比例。"""
        left, top, right, bottom = rect
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            return 0
        visible = 0
        for iy in range(1, 6):
            y = int(top + height * iy / 6.0)
            for ix in range(1, 6):
                x = int(left + width * ix / 6.0)
                if root_window_at(x, y) == hwnd:
                    visible += 1
        return visible

    def select_target_window(self, windows):
        """只保留前台窗口；否则沿用上次选择；首次按实际可见度挑选。"""
        if not windows:
            return None
        by_hwnd = {w["hwnd"]: w for w in windows}
        foreground = get_foreground_root()
        if foreground in by_hwnd:
            chosen = by_hwnd[foreground]
        elif self.preferred_hwnd in by_hwnd:
            chosen = by_hwnd[self.preferred_hwnd]
        else:
            scored = []
            for w in windows:
                exposure = self.window_exposure(w["hwnd"], w["rect"])
                left, top, right, bottom = w["rect"]
                area = max(0, right - left) * max(0, bottom - top)
                scored.append((exposure, area, w))
            exposed = [item for item in scored if item[0] > 0]
            if not exposed:
                return None
            _, _, chosen = max(exposed, key=lambda item: (item[0], item[1]))

        changed = self.preferred_hwnd != chosen["hwnd"]
        self.preferred_hwnd = chosen["hwnd"]
        if changed:
            self.log("TARGET", f"监控窗口: {chosen['title']} ({chosen['process']}, hwnd={chosen['hwnd']})", color="36")
        return chosen

    # ---------- 取一帧最新画面：每次都重新读窗口位置 ----------
    def fresh_frame(self, hwnd):
        """重新读取窗口位置 -> 截图 -> OCR。返回 (rect, img_np, ocr_results, scale) 或 None"""
        snap = capture_window_snapshot(hwnd)
        if snap is None:
            return None
        rect, img_np, scale = snap
        # 不把被遮挡窗口上的其他应用文字当成目标应用 OCR。
        if self.window_exposure(hwnd, rect) < 20:
            return None
        try:
            res, _ = self.ocr(img_np)
        except Exception:
            return None
        return rect, img_np, (res or []), scale

    # ---------- 滚动条检测：右侧细滑块 + DPI 自适应贴底容差 ----------
    @staticmethod
    def detect_scroll_position(img_np, scale=1.0):
        """返回 bottom / not_bottom / unknown；无法可靠识别时绝不滚动。"""
        h, w = img_np.shape[:2]
        if w < 100 or h < 160:
            return {"status": "unknown", "reason": "window_too_small"}

        gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)
        rgb = img_np[:, :, :3].astype(np.int16)
        chroma = rgb.max(axis=2) - rgb.min(axis=2)
        x1 = max(0, w - int(round(28 * scale)))
        x2 = max(x1 + 1, w - max(3, int(round(3 * scale))))
        y1 = min(h - 2, max(0, int(round(65 * scale))))
        y2 = h - max(2, int(round(2 * scale)))
        min_thumb = max(28, int(round(24 * scale)))
        candidates = []
        light_theme = float(np.median(gray)) >= 140.0

        for x in range(x1, min(w, x2)):
            col = gray[y1:y2, x]
            neutral = chroma[y1:y2, x] < 45
            if len(col) < min_thumb:
                continue
            if light_theme:
                # 浅色主题取高亮轨道作背景，兼容占比很大的滑块。
                background = float(np.percentile(col, 95))
                mask = (col < background - 18.0) & neutral
            else:
                background = float(np.percentile(col, 20))
                mask = (col > max(42.0, background + 14.0)) & neutral
            padded = np.concatenate(([0], mask.astype(np.int8), [0]))
            changes = np.diff(padded)
            starts = np.flatnonzero(changes == 1)
            ends = np.flatnonzero(changes == -1)
            for start, end in zip(starts, ends):
                if end - start >= min_thumb:
                    candidates.append({
                        "x": x, "top": y1 + int(start), "bottom": y1 + int(end),
                        "height": int(end - start),
                    })

        if not candidates:
            return {"status": "unknown", "reason": "scrollbar_not_found"}

        # 同一滑块会在相邻列产生多个候选；取最右侧一组中的最长段。
        rightmost = max(c["x"] for c in candidates)
        nearby = [c for c in candidates
                  if c["x"] >= rightmost - max(4, int(round(3 * scale)))]
        if len({c["x"] for c in nearby}) < 2:
            return {"status": "unknown", "reason": "scrollbar_too_thin"}
        thumb = max(nearby, key=lambda c: c["height"])
        gap = h - thumb["bottom"]
        tolerance = max(18.0, CONFIG["SCROLL_BOTTOM_TOLERANCE_DIP"] * scale, h * 0.025)
        return {
            "status": "bottom" if gap <= tolerance else "not_bottom",
            "gap": gap, "tolerance": tolerance, "thumb": thumb,
        }

    @staticmethod
    def render_fingerprint(img_np):
        """压缩会话区画面，用于判断三点加载/渲染是否已稳定。"""
        h, w = img_np.shape[:2]
        crop = img_np[int(h * 0.08):int(h * 0.88),
                      int(w * 0.04):int(w * 0.93)]
        if crop.size == 0:
            crop = img_np
        gray = cv2.cvtColor(crop, cv2.COLOR_RGB2GRAY)
        return cv2.resize(gray, (180, 120), interpolation=cv2.INTER_AREA).astype(np.float32)

    def wait_for_render(self, hwnd):
        """短等待 + 连续稳定帧；持续生成时最多等待有限时长。"""
        time.sleep(CONFIG["SCROLL_MIN_WAIT"])
        snap = capture_window_snapshot(hwnd)
        if snap is None:
            return False
        previous = self.render_fingerprint(snap[1])
        stable = 0
        deadline = time.monotonic() + CONFIG["SCROLL_STABLE_TIMEOUT"]
        while time.monotonic() < deadline:
            time.sleep(CONFIG["SCROLL_STABLE_INTERVAL"])
            snap = capture_window_snapshot(hwnd)
            if snap is None:
                return False
            current = self.render_fingerprint(snap[1])
            difference = float(np.mean(np.abs(current - previous)))
            stable = stable + 1 if difference <= CONFIG["SCROLL_STABLE_THRESHOLD"] else 0
            previous = current
            if stable >= CONFIG["SCROLL_STABLE_FRAMES"]:
                return True
        return False

    def scroll_window_down(self, hwnd, rect):
        """在会话正文上发送滚轮事件，不激活窗口、不点击、不移动滚动条。"""
        if self.dry_run or user_input_active():
            return False
        left, top, right, bottom = rect
        x = int(left + (right - left) * 0.80)
        y = int(top + (bottom - top) * 0.55)
        root = root_window_at(x, y)
        if root != hwnd:
            return False

        old = wintypes.POINT()
        u32.GetCursorPos(ctypes.byref(old))
        try:
            u32.SetCursorPos(x, y)
            time.sleep(0.04)
            for _ in range(CONFIG["SCROLL_WHEEL_NOTCHES"]):
                u32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, -WHEEL_DELTA, 0)
                time.sleep(0.04)
        finally:
            if CONFIG["RESTORE_MOUSE_POS"]:
                u32.SetCursorPos(old.x, old.y)
        self.scroll_count += 1
        return True

    def prepare_conversation_view(self, hwnd, fr, st):
        """若未贴底则分段下滚，等待加载稳定后重新 OCR，杜绝分析旧画面。"""
        if self.dry_run or not CONFIG["AUTO_SCROLL_ENABLED"] or st["busy"]:
            return fr, st
        now = time.monotonic()
        if now < self.scroll_stall_until.get(hwnd, 0.0):
            return fr, st
        if now - self.last_scroll_check.get(hwnd, 0.0) < CONFIG["SCROLL_CHECK_INTERVAL"]:
            return fr, st
        self.last_scroll_check[hwnd] = now
        if st["edit_open"] or st["user_draft"]:
            return fr, st

        position = self.detect_scroll_position(fr[1], fr[3])
        if position["status"] != "not_bottom":
            if position["status"] == "bottom" and st["loading_more"]:
                self.log("RENDER", "已贴底但末尾仍在加载，等待稳定后重新识别", color="36")
                self.wait_for_render(hwnd)
                latest = self.fresh_frame(hwnd)
                if latest is None:
                    return None, None
                rect, img_np, res, scale = latest
                return latest, self.analyze_frame(res, rect[2] - rect[0], rect[3] - rect[1], scale)
            return fr, st
        self.log("SCROLL", f"会话未贴底（滑块底距 {position['gap']}px），自动下滚并等待完整渲染", color="36")

        current = fr
        scrolled = False
        for _ in range(CONFIG["MAX_SCROLL_STEPS_PER_CYCLE"]):
            position = self.detect_scroll_position(current[1], current[3])
            if position["status"] == "bottom":
                self.scroll_stall_until.pop(hwnd, None)
                break
            before_thumb = position.get("thumb")
            if position["status"] != "not_bottom" or not self.scroll_window_down(hwnd, current[0]):
                break
            scrolled = True
            self.wait_for_render(hwnd)
            snap = capture_window_snapshot(hwnd)
            if snap is None:
                return None, None
            current = (snap[0], snap[1], [], snap[2])

            # 轮子发出后滑块完全不动，说明检测对象不是真正可滚动的会话区域。
            after = self.detect_scroll_position(current[1], current[3])
            after_thumb = after.get("thumb")
            if after["status"] == "not_bottom" and before_thumb and after_thumb:
                unchanged = all(abs(before_thumb[k] - after_thumb[k]) <= 1
                                for k in ("x", "top", "bottom", "height"))
                if unchanged:
                    self.scroll_stall_until[hwnd] = time.monotonic() + CONFIG["SCROLL_STALL_COOLDOWN"]
                    self.log("SCROLL", f"滚动后位置未变化，暂停自动贴底 {CONFIG['SCROLL_STALL_COOLDOWN']:.0f}s（避免误判非滚动条元素）", color="33")
                    break

        if not scrolled:
            return fr, st
        latest = self.fresh_frame(hwnd)
        if latest is None:
            return None, None
        rect, img_np, res, scale = latest
        new_st = self.analyze_frame(res, rect[2] - rect[0], rect[3] - rect[1], scale)
        position = self.detect_scroll_position(img_np, scale)
        if position["status"] == "not_bottom":
            self.log("SCROLL", "本轮尚未完全贴底，将在下个检查周期继续", color="33")
        return latest, new_st

    @staticmethod
    def is_status_line(text):
        """连接/等待/耗时是状态，不是已经完成的工作。"""
        compact = re.sub(r'\s+', '', text).lower()
        return bool(re.fullmatch(
            r'正在(?:重新连接|重连|连接|重试|思考|等待)(?:\d+/\d+)?[.…·]*'
            r'|(?:重新连接|重连|连接)(?:\d+/\d+)[.…·]*'
            r'|(?:reconnecting|retrying|connecting|thinking)(?:\d+/\d+)?[.…·]*'
            r'|已(?:处理|思考|用时|耗时)\d+(?:分(?:钟)?(?:\d+秒)?|秒|分钟)'
            r'|(?:thought|worked|working)for\d+(?:m(?:\d+s)?|s|minutes?|seconds?)'
            r'|(?:重试|连接)中[.…·]*', compact))

    @staticmethod
    def is_activity_card_line(text):
        """卡片附属按钮和变更统计不单独作为回答进展。"""
        compact = re.sub(r'\s+', '', text)
        if re.fullmatch(r'已(?:编辑|创建|修改|删除|更新|重命名|新建)[\w./\\: -]*\.\w{1,10}', compact):
            return True
        if re.fullmatch(r'[+\-−]\d+(?:[+\-−]\d+)?', compact):
            return True
        if compact in {"撤销", "查看变更", "查看差异", "打开文件", "全部接受", "全部拒绝", "接受", "拒绝",
                       "复制", "分享", "反馈", "取消", "发送", "编辑", "Copy", "Undo", "Viewchanges"}:
            return True
        return bool(re.fullmatch(r'(?:已)?(?:编辑|修改)了文件(?:运行了命令)?|(?:已)?(?:运行|执行)了命令', compact))

    @staticmethod
    def is_completed_activity(text):
        compact = re.sub(r'\s+', '', text)
        return bool(re.match(r'^已(?:运行|执行|编辑|创建|修改|删除|更新|重命名|新建|读取|搜索|检查|测试)', compact)
                    or re.match(r'^(?:Ran|Executed|Edited|Created|Updated|Read|Searched|Tested)\b', text))

    @staticmethod
    def is_model_anchor(text):
        compact = squash(text)
        return bool(re.fullmatch(r'(?:gpt-?[\d.]+(?:-?[\w.]+)*|[\d.]*sol|opus[\d.]*|auto|ultra)'
                                 r'(?:ultra|high|medium|low|max)?', compact))

    # ---------- 画面分析（纯函数，坐标均为相对窗口左上角） ----------
    def analyze_frame(self, ocr_results, w, h, scale):
        """先定位输入区，再按最近一轮的用户消息划分进展。"""
        st = {
            "busy": False, "has_error": False, "loading_more": False,
            "reconnect_loop": False, "edit_open": False, "edit_send": None,
            "action": None, "error": None, "target_prompt": None, "sig": None,
            "input_pos": None, "send_pos": None, "toolbar_y": None,
            "pending_input": False, "user_draft": False, "composer_visible": False,
            "progress": "unknown", "reason": "未发现会话末尾错误", "progress_evidence": [],
            "newer_prompt": False, "error_cta": [],
        }
        items = []
        for box, text, score in ocr_results:
            if score < 0.5 or not text.strip() or len(box) != 4:
                continue
            xs, ys = zip(*box)
            items.append({"text": text, "score": float(score),
                          "cx": sum(xs) / 4.0, "cy": sum(ys) / 4.0,
                          "left": min(xs), "right": max(xs), "top": min(ys), "bottom": max(ys)})
        items.sort(key=lambda o: (o["cy"], o["left"]))
        reliable = [o for o in items if o["score"] >= CONFIG["MIN_OCR_SCORE"]]
        placeholders = [o for o in reliable if o["cy"] > h * 0.70
                        and ("一起工作" in squash(o["text"]) or "和codex" in squash(o["text"]))]
        placeholder = max(placeholders, key=lambda o: o["cy"], default=None)
        anchors_full = [o for o in reliable if o["cy"] > h * 0.80
                        and squash(o["text"]) in {"完全访问", "fullaccess", "默认权限", "defaultpermissions"}]
        anchors_model = [o for o in reliable if o["cy"] > h * 0.80 and self.is_model_anchor(o["text"])]
        anchors = anchors_full + anchors_model
        if anchors:
            # 仅使用最底下一行，避免模型名出现在历史正文中拉偏中位数。
            bottom_y = max(o["cy"] for o in anchors)
            anchors = [o for o in anchors if bottom_y - o["cy"] < 25 * scale]
            anchors_model = [o for o in anchors_model if o in anchors]
            anchors_full = [o for o in anchors_full if o in anchors]
            st["toolbar_y"] = statistics.median(o["cy"] for o in anchors)
        toolbar_y = st["toolbar_y"]
        if placeholder:
            st["input_pos"] = (min(w - 15 * scale, placeholder["cx"] + 30 * scale), placeholder["cy"])
        elif anchors:
            st["input_pos"] = ((anchors_full[0]["cx"] + 40 * scale) if anchors_full else w * 0.35,
                               toolbar_y - CONFIG["INPUT_ABOVE_TOOLBAR_DIP"] * scale)
        st["composer_visible"] = st["input_pos"] is not None
        if anchors_model:
            sx = max(o["right"] for o in anchors_model) + CONFIG["SEND_RIGHT_OF_MODEL_DIP"] * scale
            if 0 < sx < w and 0 < toolbar_y < h:
                st["send_pos"] = (sx, toolbar_y)
        input_y = st["input_pos"][1] if st["input_pos"] else None
        input_top = input_y - 45 * scale if input_y is not None else h
        if input_y is not None:
            region_bottom = toolbar_y - 15 * scale if toolbar_y is not None else h - 15 * scale
            region = [o for o in items if input_top <= o["cy"] <= region_bottom
                      and o not in anchors and o not in placeholders]
            draft_text = "".join(o["text"].strip() for o in region)
            # 不去掉标点：用户草稿“继续！”不是我们写入的“继续”。
            st["pending_input"] = bool(draft_text) and squash(draft_text) == squash(CONFIG["AUTO_SEND_TEXT"])
            st["user_draft"] = bool(draft_text) and not st["pending_input"]
        content = [o for o in items if 60 * scale < o["cy"] < input_top and o not in anchors]
        cancel = [o for o in reliable if clean_text(o["text"]) in {"取消", "Cancel"} and o["cy"] < input_top]
        send = [o for o in reliable if clean_text(o["text"]) in {"发送", "Send", "保存", "保存并提交", "Save"} and o["cy"] < input_top]
        for button in reversed(send):
            if any(abs(button["cy"] - c["cy"]) < 30 * scale
                   and 0 < button["cx"] - c["cx"] < 400 * scale for c in cancel):
                st["edit_open"] = True
                st["edit_send"] = (button["cx"], button["cy"])
                break
        if not st["edit_open"] and cancel:
            # 主会话区内出现「取消」按钮，说明编辑框已展开（发送键可能为无文字图标）
            st["edit_open"] = True
        errors = [o for o in content if o["score"] >= CONFIG["MIN_OCR_SCORE"]
                  and any(k in squash(o["text"]) for k in CONFIG["ERROR_KEYWORDS"])]
        busy = [o for o in content if len(clean_text(o["text"])) <= 24
                and any(squash(k) in squash(o["text"]) for k in CONFIG["BUSY_KEYWORDS"])]
        st["busy"] = bool(busy)
        st["has_error"] = bool(errors)
        if not errors:
            st["loading_more"] = any(o["cy"] > h * 0.55 and w * 0.20 < o["cx"] < w * 0.80
                                     and re.fullmatch(r'(?:\.{2,}|。{2,}|·{2,}|•{2,}|…+)', squash(o["text"]))
                                     for o in content)
            return st
        last_line = max(errors, key=lambda o: o["cy"])
        error_lines = [last_line]
        for previous in reversed([o for o in errors if o is not last_line]):
            if error_lines[0]["top"] - previous["bottom"] > 50 * scale:
                break
            error_lines.insert(0, previous)
        latest = dict(last_line)
        latest.update(text=" ".join(o["text"] for o in error_lines),
                      top=min(o["top"] for o in error_lines), bottom=max(o["bottom"] for o in error_lines))
        st["error"] = latest
        # 残留的“正在思考”不代表当前还在工作；错误之后的忙碌才阻止操作。
        st["busy"] = any(o["cy"] > latest["bottom"] for o in busy)
        st["loading_more"] = any(o["cy"] > latest["bottom"] and w * 0.20 < o["cx"] < w * 0.80
                                 and re.fullmatch(r'(?:\.{2,}|。{2,}|·{2,}|•{2,}|…+)', squash(o["text"]))
                                 for o in content)
        commands = {squash(clean_text(t)) for t in CONFIG["USER_PROMPT_KEYWORDS"]}

        # 报错卡片自带的「继续/重试」按钮贴着错误块右下角；它是操作按钮，不是用户新消息。
        # 若把它当成 prompt，"错误之后已有新消息"会永久成立，导致再也不重试。
        cta_ids = set()
        for err in errors:
            for o in reliable:
                if o in errors or id(o) in cta_ids:
                    continue
                if squash(clean_text(o["text"])) not in commands:
                    continue
                if not (err["top"] - 10 * scale <= o["cy"] <= err["bottom"] + 110 * scale):
                    continue
                # 靠右对齐的用户消息气泡绝对不是错误卡片的内部 CTA 按钮
                if o["cx"] > w * 0.58:
                    continue
                if o["left"] < err["left"] + 0.30 * w:
                    continue
                cta_ids.add(id(o))
        st["error_cta"] = [(o["cx"], o["cy"]) for o in reliable if id(o) in cta_ids]

        def is_prompt(o):
            text = o["text"]
            if o["score"] < CONFIG["MIN_OCR_SCORE"] or o in errors or self.is_status_line(text):
                return False
            if id(o) in cta_ids:
                return False
            if self.is_activity_card_line(text) or self.is_completed_activity(text):
                return False
            explicit = squash(clean_text(text)) in commands
            # 用户发出的明确指令（如「继续/重试」）只要位于右半区 (cx > w * 0.50) 必须作为用户气泡
            if explicit and o["cx"] > w * 0.50:
                return True
            return (not explicit and o["cx"] > w * 0.55 and len(clean_text(text)) > 1
                    and not any(k in squash(text) for k in CONFIG["ERROR_KEYWORDS"])
                    and not any(squash(k) in squash(text) for k in CONFIG["BUSY_KEYWORDS"]))

        prompts = [o for o in content if is_prompt(o)]
        # 允许气泡稍微与错误块重叠（如右侧气泡与左侧错误块在同一水平高度附近）
        preceding = [o for o in prompts if o["top"] < latest["bottom"]]
        target = max(preceding, key=lambda o: o["cy"], default=None)
        st["target_prompt"] = target
        turn_start = target["bottom"] if target else 60 * scale
        turn = [o for o in content if turn_start < o["cy"] < latest["top"] and o not in prompts and o not in errors]
        st["reconnect_loop"] = any(any(k in squash(o["text"]) for k in CONFIG["RECONNECT_KEYWORDS"])
                                   for o in turn)

        def substantive(o):
            text = o["text"]
            if self.is_status_line(text) or squash(clean_text(text)) in commands:
                return False
            if self.is_completed_activity(text):
                return True
            if self.is_activity_card_line(text):
                return False
            return len(clean_text(text)) > 3 and not re.fullmatch(r'[.。·•…]+', squash(text))

        evidence = [o for o in turn if substantive(o) and o["score"] >= CONFIG["MIN_OCR_SCORE"]]
        uncertain = [o for o in turn if substantive(o) and o["score"] < CONFIG["MIN_OCR_SCORE"]]
        st["progress_evidence"] = [o["text"][:120] for o in evidence[:3]]
        if target is not None and not evidence and not uncertain:
            st["progress"] = "none"
            st["action"] = "SIT_2"
            st["reason"] = "最近用户消息之后只有重连/等待状态和错误，直接补发继续"
        elif evidence:
            st["progress"] = "yes" if target else "unknown"
            st["action"] = "SIT_1"
            st["reason"] = "最近一轮已有执行/回答进展" if target else "最近用户消息不可见，不冒险编辑历史消息，改为继续"
        else:
            st["action"] = "SIT_1"
            st["reason"] = "最近用户消息不可见或 OCR 证据不足，不冒险编辑，采用继续"
        # 报错之后已经有新的消息/回答，旧错误不能触发再一次发送。
        # 仅仅又出现一个「继续/重试」不算新一轮内容：它多半是报错卡片按钮，
        # 即使是用户手动再催一次，重复发送同样的指令也无害。
        st["newer_prompt"] = any(o["top"] > latest["bottom"]
                                 and squash(clean_text(o["text"])) not in commands
                                 for o in prompts)
        after = [o for o in content if o["top"] > latest["bottom"] + 8 * scale
                 and o not in prompts and substantive(o)]
        if st["newer_prompt"] or after:
            st["action"] = None
            st["reason"] = "错误之后已有新的用户消息或回答，等待新一轮结果，不重试旧错误"
        if st["edit_open"]:
            st["action"] = None
            st["reason"] = "编辑框已打开；不自动提交用户手动编辑的内容"
        st["sig"] = (squash(latest["text"])[:240], squash(target["text"])[:160] if target else "")
        return st

    # ---------- 铅笔图标：多尺度模板匹配（含掩膜兜底）+ 可选几何兜底 ----------
    def find_pencil(self, img_np, prompt, scale, allow_fallback=True):
        """在最近消息气泡的右下角找铅笔图标；找不到返回 None，由调用方兜底。"""
        h, w = img_np.shape[:2]
        x1 = max(0, int(prompt["right"] - 110 * scale))
        x2 = min(w, int(prompt["right"] + 55 * scale))
        y1 = max(0, int(prompt["bottom"] - 12 * scale))
        y2 = min(h, int(prompt["bottom"] + CONFIG["PENCIL_BELOW_BUBBLE_DIP"] * 3 * scale))
        if self.pencil_template is not None and x2 - x1 > 12 and y2 - y1 > 12:
            roi = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)[y1:y2, x1:x2]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            tpl_gray = cv2.cvtColor(self.pencil_template, cv2.COLOR_BGR2GRAY)
            best = (0.0, None, "模板")
            factors = (0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.35, 1.5, 1.7, 2.0)
            for factor in factors:
                t = cv2.resize(tpl_gray, None, fx=factor, fy=factor,
                               interpolation=cv2.INTER_CUBIC if factor > 1 else cv2.INTER_AREA)
                if t.shape[0] >= gray.shape[0] or t.shape[1] >= gray.shape[1]:
                    continue
                r = cv2.matchTemplate(gray, t, cv2.TM_CCOEFF_NORMED)
                _, mv, _, ml = cv2.minMaxLoc(r)
                if mv > best[0]:
                    best = (mv, (x1 + ml[0] + t.shape[1] // 2, y1 + ml[1] + t.shape[0] // 2), "模板")
            threshold = float(CONFIG.get("PENCIL_MATCH_THRESHOLD", 0.65))
            if best[0] >= threshold:
                return best[1], f"模板匹配 {best[0]:.2f}"
            # 图标在深色底上很暗、纹理少，相关系数会被压低；再用二值掩膜相关兜底。
            roi_mask = (gray > float(np.percentile(gray, 90)) - 6.0).astype(np.float32)
            masked = (0.0, None)
            for factor in (0.45, 0.55, 0.7, 0.85, 1.0, 1.2, 1.5):
                t = cv2.resize(tpl_gray, None, fx=factor, fy=factor,
                               interpolation=cv2.INTER_LINEAR)
                if t.shape[0] >= roi_mask.shape[0] or t.shape[1] >= roi_mask.shape[1]:
                    continue
                t_mask = (t > float(np.percentile(t, 85))).astype(np.float32)
                if t_mask.sum() < 8:
                    continue
                r = cv2.matchTemplate(roi_mask, t_mask, cv2.TM_CCORR_NORMED)
                _, mv, _, ml = cv2.minMaxLoc(r)
                if mv > masked[0]:
                    masked = (mv, (x1 + ml[0] + t_mask.shape[1] // 2, y1 + ml[1] + t_mask.shape[0] // 2))
            masked_threshold = float(CONFIG.get("PENCIL_MATCH_THRESHOLD_MASKED", 0.58))
            if masked[1] is not None and masked[0] >= masked_threshold:
                return masked[1], f"掩膜匹配 {masked[0]:.2f}"
        if allow_fallback and CONFIG["ALLOW_GEOMETRIC_PENCIL_FALLBACK"]:
            # 几何定位：铅笔位于气泡右下角偏右（Copy图标在左，铅笔在右）
            px = int(prompt["right"] - 4 * scale)
            py = int(prompt["bottom"] + CONFIG["PENCIL_BELOW_BUBBLE_DIP"] * scale)
            return (px, py), "几何兜底(气泡右下偏右角)"
        return None, "铅笔模板未可靠匹配"

    # ---------- 点击和键盘发送均 fail-closed，不接受未知命中点 ----------
    def safe_click(self, hwnd, abs_x, abs_y, what):
        if self.dry_run or user_input_active() or get_foreground_root() != hwnd:
            return False
        left, top, right, bottom = get_window_rect(hwnd)
        if not (left <= abs_x < right and top <= abs_y < bottom):
            return False
        if root_window_at(abs_x, abs_y) != hwnd:
            self.log("ABORT", f"「{what}」命中点不属于目标窗口，不使用旧坐标或强制穿透", color="31")
            return False
        return click_screen_point(abs_x, abs_y)

    @staticmethod
    def can_retry(st):
        return bool(st["action"] and not st["busy"] and not st["loading_more"]
                    and not st["edit_open"] and not st["user_draft"] and not st["pending_input"])

    def current_state(self, hwnd):
        frame = self.fresh_frame(hwnd)
        if frame is None:
            return None, None
        rect, _, results, scale = frame
        return frame, self.analyze_frame(results, rect[2] - rect[0], rect[3] - rect[1], scale)

    def verify_submission(self, hwnd, edited=False):
        """检查界面确实退出待发送状态；缺帧不宣称发送已验证。"""
        frame, st = self.current_state(hwnd)
        if frame is None:
            self.log("UNVERIFIED", "发送后无法获取有效画面，暂不重复发送", color="33")
            return False
        if st["edit_open"] or st["pending_input"] or st["user_draft"]:
            return False
        if st["busy"] or st["newer_prompt"]:
            return True
        if edited:
            # 本次确实点击了已验证的编辑发送按钮，随后编辑框关闭。
            return st["composer_visible"] or st["target_prompt"] is not None
        return st["composer_visible"]  # 输入框有可靠锚点，且原“继续”已被消费

    # ---------- 情形二：优先编辑重发；铅笔不可用则退回输入框发送「继续」 ----------
    def do_situation2(self, hwnd, expected_sig=None):
        if self.dry_run or not force_activate_window(hwnd):
            return False
        try:
            frame, st = self.current_state(hwnd)
            if frame is None or not self.can_retry(st) or st["action"] != "SIT_2":
                return False
            expected_sig = st["sig"] if expected_sig is None else expected_sig
            if st["sig"] != expected_sig:
                return False
            # 默认优先点击铅笔重新编辑发送；若未开启则直接退回输入框
            if not CONFIG["SIT2_PREFER_EDIT_RESEND"] and st["input_pos"] is not None:
                self.log("SIT2", "无进展：直接在输入框发送继续（不依赖铅笔图标）", color="36")
                return self.do_situation1(hwnd, expected_sig, require_action=None)
            if st["target_prompt"] is None:
                return self.fallback_to_composer(hwnd, expected_sig, "最近消息不可定位")

            target = st["target_prompt"]
            scale = frame[3]

            # 1. 先尝试在当前截图中匹配铅笔（如果光标之前已悬停或处于显现状态）
            point, how = self.find_pencil(frame[1], target, scale, allow_fallback=False)

            # 2. 若未直接匹配到，将光标悬停至气泡下方区域（避免遮挡气泡文字导致 OCR 丢失）
            if point is None:
                hx = frame[0][0] + target["right"] - 4 * scale
                hy = frame[0][1] + target["bottom"] + CONFIG["PENCIL_BELOW_BUBBLE_DIP"] * scale
                if user_input_active() or root_window_at(hx, hy) != hwnd:
                    return self.fallback_to_composer(hwnd, expected_sig, "无法悬停最近消息")
                u32.SetCursorPos(int(hx), int(hy))
                time.sleep(0.30)
                # 悬停后重新获取截图并匹配铅笔（允许几何兜底）
                snap = capture_window_snapshot(hwnd)
                if snap is not None:
                    point, how = self.find_pencil(snap[1], target, scale, allow_fallback=True)
                else:
                    point, how = self.find_pencil(frame[1], target, scale, allow_fallback=True)

            # 3. 若模板未找到且不允许几何兜底，退回输入框
            if point is None:
                self.log("PENCIL", "未可靠匹配铅笔图标，改用输入框发送继续", color="33")
                return self.fallback_to_composer(hwnd, expected_sig, "铅笔图标不可用")

            px, py = point
            self.log("LOCATE", f"最近消息铅笔 -> ({int(px)}, {int(py)})，{how}", color="36")
            if not self.safe_click(hwnd, frame[0][0] + px, frame[0][1] + py, "铅笔"):
                return self.fallback_to_composer(hwnd, expected_sig, "铅笔点击未生效")

            # 4. 等待编辑框展开并进行校验
            time.sleep(0.55)
            frame, edited = self.current_state(hwnd)
            if frame is None or not edited["edit_open"] or edited["busy"] or edited["loading_more"]:
                self.log("PENCIL", "点击铅笔后未验证到编辑框，改用输入框发送继续", color="33")
                return self.fallback_to_composer(hwnd, expected_sig, "编辑框未出现")
            if edited["user_draft"] or edited["pending_input"]:
                return False

            # 5. 提交编辑内容
            if edited["edit_send"]:
                sx, sy = edited["edit_send"]
                if not self.safe_click(hwnd, frame[0][0] + sx, frame[0][1] + sy, "本次编辑框发送"):
                    return False
            else:
                # 新版编辑框只有图标发送键，没有「发送」文字；Enter 与点击等效。
                self.log("LOCATE", "编辑框无文字发送按钮，改用 Enter 提交", color="36")
                if user_input_active() or get_foreground_root() != hwnd:
                    return False
                send_enter_key()

            # 6. 验证提交结果
            time.sleep(CONFIG["SEND_VERIFY_WAIT"])
            if self.verify_submission(hwnd, edited=True):
                return True
            self.log("UNVERIFIED", "编辑重发后未验证到界面已提交，本轮不再补发", color="33")
            return False
        except Exception as exc:
            self.log("ERROR", f"编辑重发失败: {exc}", color="31")
            return False

    def fallback_to_composer(self, hwnd, expected_sig, why):
        """情形二的安全退路：不依赖铅笔，直接在输入框发送「继续」。"""
        if not CONFIG["SIT2_FALLBACK_TO_COMPOSER"]:
            self.log("ABORT", f"{why}；已禁用输入框兜底，本次不发送", color="33")
            return False
        self.log("FALLBACK", f"{why}；改为在输入框发送「{CONFIG['AUTO_SEND_TEXT']}」", color="33")
        return self.do_situation1(hwnd, expected_sig, require_action=None)

    # ---------- 情形一：仅向空输入框写入并验证“继续” ----------
    def do_situation1(self, hwnd, expected_sig=None, require_action="SIT_1"):
        if self.dry_run or not force_activate_window(hwnd):
            return False
        try:
            frame, st = self.current_state(hwnd)
            if frame is None or not self.can_retry(st):
                return False
            if require_action and st["action"] != require_action:
                return False
            expected_sig = st["sig"] if expected_sig is None else expected_sig
            if st["sig"] != expected_sig or st["input_pos"] is None:
                self.log("ABORT", "状态变化或输入框无可靠锚点，不盲点", color="31")
                return False
            ix, iy = st["input_pos"]
            rect = frame[0]
            self.log("LOCATE", f"输入框 -> 窗口内 ({int(ix)}, {int(iy)})，DPI {frame[3]:.2f}", color="36")
            if not self.safe_click(hwnd, rect[0] + ix, rect[1] + iy, "输入框"):
                return False
            time.sleep(0.15)
            # 聚焦可能展开草稿；写入前再检查一次，不能仅依赖点击前的画面。
            focused_frame, focused = self.current_state(hwnd)
            if focused_frame is None or not self.can_retry(focused) or focused["sig"] != expected_sig:
                return False
            if user_input_active() or get_foreground_root() != hwnd:
                return False
            if not send_unicode_text(CONFIG["AUTO_SEND_TEXT"]):
                self.log("ABORT", "Unicode 输入未全部成功，不发送；剪贴板未修改", color="31")
                return False
            time.sleep(0.25)
            frame, typed = self.current_state(hwnd)
            if frame is None or not typed["pending_input"] or typed["user_draft"] or typed["edit_open"]:
                self.log("ABORT", "未验证到输入框中唯一的继续文本，不按 Enter", color="31")
                return False
            if typed["busy"] or typed["loading_more"] or typed["sig"] != expected_sig:
                return False
            if require_action and typed["action"] != require_action:
                return False
            if user_input_active() or get_foreground_root() != hwnd:
                return False
            send_enter_key()
            time.sleep(CONFIG["SEND_VERIFY_WAIT"])
            frame, after_enter = self.current_state(hwnd)
            if frame is None:
                self.log("UNVERIFIED", "Enter 后缺少有效画面，不补点箭头、不宣称完成", color="33")
                return False
            if not after_enter["pending_input"]:
                return (not after_enter["user_draft"] and not after_enter["edit_open"]
                        and (after_enter["busy"] or after_enter["newer_prompt"] or after_enter["composer_visible"]))
            # 仅当我们写入的文本仍待发送且仍为同一轮时，允许一次箭头补发。
            if (after_enter["busy"] or after_enter["loading_more"] or after_enter["user_draft"]
                    or after_enter["edit_open"] or after_enter["sig"] != expected_sig or not after_enter["send_pos"]):
                return False
            sx, sy = after_enter["send_pos"]
            if not self.safe_click(hwnd, frame[0][0] + sx, frame[0][1] + sy, "发送箭头"):
                return False
            time.sleep(CONFIG["SEND_VERIFY_WAIT"])
            return self.verify_submission(hwnd)
        except Exception as exc:
            self.log("ERROR", f"执行继续失败: {exc}", color="31")
            return False

    # ---------- 单轮扫描：失败也记短防抖，但不再等待 45 秒 ----------
    def scan_and_retry(self):
        all_windows = find_target_windows()
        live = {w["hwnd"] for w in all_windows}
        for mapping in (self.last_action_time, self.last_sig, self.last_sig_time,
                        self.last_scroll_check, self.scroll_stall_until):
            for hwnd in set(mapping) - live:
                mapping.pop(hwnd, None)
        if CONFIG["TARGET_ACTIVE_WINDOW_ONLY"]:
            chosen = self.select_target_window(all_windows)
            windows = [chosen] if chosen else []
        else:
            windows = all_windows
        for win in windows:
            hwnd = win["hwnd"]
            frame, st = self.current_state(hwnd)
            if frame is None:
                continue
            frame, st = self.prepare_conversation_view(hwnd, frame, st)
            if frame is None or not self.can_retry(st) or user_input_active():
                continue
            now = time.monotonic()
            if now - self.last_action_time.get(hwnd, -math.inf) < CONFIG["POST_ACTION_COOLDOWN"]:
                continue
            if st["sig"] == self.last_sig.get(hwnd) and now - self.last_sig_time.get(hwnd, -math.inf) < CONFIG["SAME_ERROR_GUARD"]:
                continue
            label = {"none": "情形二(无进展)", "yes": "情形一(有进展)", "unknown": "进展不确定(保守继续)"}[st["progress"]]
            self.log("DETECTED", f"{label}：{st['reason']} | {st['error']['text'][:100]}", color="33")
            if self.dry_run:
                self.log("DRY_RUN", f"仅检测，不滚动/激活/点击/输入：{st['action']}", color="36")
                self.last_sig[hwnd], self.last_sig_time[hwnd] = st["sig"], now
                continue
            expected_sig = st["sig"]
            wait = CONFIG["RETRY_COOLDOWN"]
            self.log("PRE_WAIT", f"固定冷却 {wait:.1f}s，随后重新取帧确认；无递增退避", color="33")
            time.sleep(wait)
            frame, st = self.current_state(hwnd)
            if frame is None:
                continue
            frame, st = self.prepare_conversation_view(hwnd, frame, st)
            if frame is None or not self.can_retry(st) or st["sig"] != expected_sig or user_input_active():
                self.log("SKIP", "冷却期间状态/最近一轮已变化或存在草稿，取消", color="36")
                continue
            point = wintypes.POINT()
            have_mouse = bool(u32.GetCursorPos(ctypes.byref(point)))
            action = st["action"]
            try:
                ok = (self.do_situation2(hwnd, expected_sig) if action == "SIT_2"
                      else self.do_situation1(hwnd, expected_sig))
            finally:
                if CONFIG["RESTORE_MOUSE_POS"] and have_mouse:
                    u32.SetCursorPos(point.x, point.y)
                finished = time.monotonic()
                self.last_sig[hwnd] = expected_sig
                self.last_sig_time[hwnd] = finished
                self.last_action_time[hwnd] = finished
            if ok:
                if action == "SIT_2":
                    self.mode2_count += 1
                else:
                    self.mode1_count += 1
                self.log("ACTION:" + action, f"已发送并验证界面 | 继续 {self.mode1_count} 次 / 编辑重发 {self.mode2_count} 次", color="32")
            else:
                self.log("NOT_SENT", "操作中止或未能验证；仅短防抖，不冒险重复提交", color="33")
            return ok
        return False

    def run(self, once=False):
        for stream in (sys.stdout, sys.stderr):
            if stream is not None and hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        k32.SetConsoleTitleW("Codex Auto-Retry V2 (Read-only)" if self.dry_run else "Codex Auto-Retry V2 (Active)")
        self.log("START", "Codex Auto-Retry V2：最近一轮判定 + 固定冷却 + 发送验证", color="36")
        self.log("CONFIG", f"固定冷却 {CONFIG['RETRY_COOLDOWN']:.1f}s（无退避；校验上限 {MAX_ALLOWED_COOLDOWN:g}s）| 防抖 {CONFIG['POST_ACTION_COOLDOWN']:.1f}s | 轮询 {CONFIG['POLL_INTERVAL']}s", color="36")
        self.log("CONFIG", f"目标 {', '.join(CONFIG['TARGET_PROCESSES'])} | 自动贴底 {CONFIG['AUTO_SCROLL_ENABLED']} | 只读 {self.dry_run}", color="36")
        last_status_time = time.monotonic()
        while True:
            try:
                self.scan_and_retry()
                if once:
                    return
                now = time.monotonic()
                if now - last_status_time > 60:
                    up = int((now - self.start_time) / 60)
                    self.log("STATUS", f"运行 {up}m | 继续 {self.mode1_count} 次 | 编辑重发 {self.mode2_count} 次 | 滚动 {self.scroll_count} 次", color="36")
                    last_status_time = now
                time.sleep(CONFIG["POLL_INTERVAL"])
            except KeyboardInterrupt:
                self.log("STOP", "已退出。", color="36")
                return
            except Exception as exc:
                self.log("WARN", f"运行异常: {exc}，下周期再检测", color="31")
                if self.logger is not None:
                    self.logger.exception("scan failed")
                if once:
                    raise
                time.sleep(CONFIG["POLL_INTERVAL"])


MONITOR_SCRIPT_NAMES = {"codex_auto_retry.py", "codex_auto_retry_v2.py", "codex_auto_retry_v3.py"}


def python_script_argument(argv):
    """只识别 Python 实际执行的脚本，不匹配 -c 文本、测试代码或标题。"""
    args = iter(argv[1:])
    for arg in args:
        if arg in {"-c", "-m"}:
            return None
        if arg in {"-W", "-X"}:
            next(args, None)
        elif arg == "--":
            return next(args, None)
        elif not arg.startswith("-"):
            return arg
    return None


def monitor_processes(script_path=None):
    own_pid = os.getpid()
    wanted = os.path.normcase(os.path.realpath(script_path)) if script_path else None
    found = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = process.info
            argv = info["cmdline"] or []
            if info["pid"] == own_pid or (info["name"] or "").lower() not in {"python.exe", "pythonw.exe"}:
                continue
            if any(flag in argv for flag in ("--stop", "--check", "--dry-run")):
                continue
            argument = python_script_argument(argv)
            if argument is None or os.path.basename(argument).lower() not in MONITOR_SCRIPT_NAMES:
                continue
            if wanted:
                actual = argument if os.path.isabs(argument) else os.path.join(process.cwd(), argument)
                if os.path.normcase(os.path.realpath(actual)) != wanted:
                    continue
            found.append(process)
        except (psutil.Error, OSError):
            continue
    return found


def acquire_single_instance():
    """互斥锁防止 V3 双开，并阻止与旧版监控同时运行。"""
    ctypes.set_last_error(0)
    handle = k32.CreateMutexW(None, False, r"Local\CodexAutoRetryV2")
    if not handle:
        raise OSError(ctypes.get_last_error(), "无法建立单实例锁")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        k32.CloseHandle(handle)
        raise RuntimeError("V2 已在运行，不启动第二个监控")
    existing = monitor_processes()
    if existing:
        k32.CloseHandle(handle)
        raise RuntimeError(f"已存在原版、V2 或其他 V2 监控（PID {', '.join(str(p.pid) for p in existing)}）；先停止再启动")
    return handle


def create_logger():
    directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(directory, exist_ok=True)
    logger = logging.getLogger("codex-auto-retry-v2")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = RotatingFileHandler(os.path.join(directory, "auto-retry.log"), maxBytes=1_000_000,
                                      backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def main(argv=None):
    parser = argparse.ArgumentParser(description="Codex 自动重试 V3：固定 2 秒冷却，最近一轮进展判定")
    parser.add_argument("--dry-run", action="store_true", help="只检测，不滚动/激活/输入/点击")
    parser.add_argument("--once", action="store_true", help="仅检测一轮后退出（可与 --dry-run 组合）")
    parser.add_argument("--check", action="store_true", help="检查依赖、OCR 初始化和配置，不操作窗口")
    parser.add_argument("--stop", action="store_true", help="仅停止正在执行本目录 V2 脚本的监控")
    args = parser.parse_args(argv)
    CONFIG.validate()
    if args.stop:
        targets = monitor_processes(os.path.abspath(__file__))
        for process in targets:
            try:
                process.terminate()
                process.wait(timeout=3)
            except psutil.NoSuchProcess:
                pass
            except psutil.TimeoutExpired:
                process.kill()
            except psutil.AccessDenied:
                print(f"无法停止 PID {process.pid}：权限不足", file=sys.stderr)
                return 1
        print(f"已停止本目录 V2 监控 {len(targets)} 个；未操作原版或其他副本。")
        return 0
    if args.check:
        RapidOCR()
        size = ctypes.sizeof(INPUT)
        expected = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        if size != expected:
            print(f"INPUT 结构大小错误：{size} != {expected}", file=sys.stderr)
            return 1
        template = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pencil_template.png")
        if cv2.imread(template) is None and not CONFIG["ALLOW_GEOMETRIC_PENCIL_FALLBACK"]:
            print("铅笔模板不可读，无法可靠执行无进展编辑重发", file=sys.stderr)
            return 1
        print(f"依赖与 OCR 初始化通过；INPUT={size} bytes；固定冷却={CONFIG['RETRY_COOLDOWN']:.1f}s；校验上限={MAX_ALLOWED_COOLDOWN:g}s。")
        return 0
    logger = create_logger()
    handle = None
    try:
        if not args.dry_run:
            handle = acquire_single_instance()
        CodexAutoRetryApp(dry_run=args.dry_run, logger=logger).run(once=args.once)
        return 0
    except Exception as exc:
        logger.exception("启动/运行失败: %s", exc)
        if sys.stderr is not None:
            print(f"[ERROR] {exc}", file=sys.stderr)
        return 1
    finally:
        if handle:
            k32.CloseHandle(handle)


if __name__ == "__main__":
    raise SystemExit(main())
