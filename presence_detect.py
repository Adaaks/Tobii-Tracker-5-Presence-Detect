"""Presence Detect - lock your PC and/or black out your screens when you walk away.

Uses the Tobii Eye Tracker 5 to sense whether you're *sitting there* (head and eye
tracking), not where you're looking - so reading your other monitors never triggers
it. Keyboard and mouse use also count as being there. Two independent features:

  * Lock: locks Windows after you've been away for a while.
  * Black out: turns your screens black (great for OLED), and wakes them when you return.

    pythonw presence_detect.py               # open the app (lives in the tray)
    pythonw presence_detect.py --background  # start hidden in the tray (used at login)
    python presence_detect.py --test         # console readout of what it senses, does nothing

Shortcuts: Ctrl+Alt+B black out now, Ctrl+Alt+End quit.
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import traceback
import winreg

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # real pixel coordinates on every monitor
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

import tkinter as tk  # noqa: E402

from tobii_stream import PRESENCE_PRESENT, TobiiStream  # noqa: E402

user32, kernel32, gdi32 = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.gdi32
shell32, dwmapi, winmm = ctypes.windll.shell32, ctypes.windll.dwmapi, ctypes.windll.winmm

APP_NAME = "Presence Detect"
FROZEN = getattr(sys, "frozen", False)  # running as the packaged PresenceDetect.exe
# The .exe keeps its settings in %APPDATA% so it works from anywhere; the script keeps them beside itself.
APPDATA = os.environ.get("APPDATA") or os.path.expanduser("~")
DATA_DIR = (os.path.join(APPDATA, "PresenceDetect") if FROZEN
            else os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)
LOG_FILE = os.path.join(DATA_DIR, "presence_detect.log")
SETTINGS_FILE = os.path.join(DATA_DIR, "presence_detect_settings.json")
ICON_FILE = os.path.join(DATA_DIR, "presence_detect.ico")
# settings from before the app was renamed (Presence Blackout) are picked up automatically
OLD_SETTINGS_FILES = [os.path.join(DATA_DIR, "presence_blackout_settings.json"),
                      os.path.join(APPDATA, "PresenceBlackout", "presence_blackout_settings.json")]

TICK_MS = 100
SEEN_GRACE_S = 1.0     # a head/gaze sample keeps you "present" for this long
WAKE_CONFIRM_S = 0.3   # tracker must see you this long to wake (ignores one-frame glitches)
RETRY_TRACKER_S = 5    # retry connecting if the tracker / Tobii service isn't available
INPUT_WAKE_HOLD_S = 3  # keys/mouse not counted: after they wake the screens, wait before blacking out again
CONNECT_GRACE_S = 2    # right after (re)connecting, assume you're here until the first data arrives
REBUILD_AFTER_S = 5    # a lost connection that hasn't come back by itself is rebuilt from scratch
LOCK_CHECK_S = 1.0     # how often to check whether Windows is locked
NO_TRACKER_AWAY_S = 300  # tracker unplugged: fall back to a normal screensaver (keyboard/mouse idle)
REPEAT_OFF_S = 15      # re-send "displays off" this often while away (in case something woke them)

BLACKOUT_PRESETS = [(0, "Instantly"), (2, "2 s"), (4, "4 s"), (6, "6 s"), (8, "8 s"), (10, "10 s"),
                    (15, "15 s"), (30, "30 s"), (60, "1 min"), (300, "5 min"), (600, "10 min")]
LOCK_PRESETS = [(15, "15 s"), (30, "30 s"), (60, "1 min"), (120, "2 min"), (300, "5 min"),
                (600, "10 min"), (900, "15 min"), (1800, "30 min"), (3600, "1 hour")]
FADE_PRESETS = [(0.5, "0.5 s"), (1, "1 s"), (2, "2 s"), (3, "3 s"), (5, "5 s")]
DIM_OVER_PRESETS = [(1, "1 s"), (2, "2 s"), (3, "3 s"), (5, "5 s")]
DIM_LEVEL_PRESETS = [(0.3, "30%"), (0.5, "50%"), (0.7, "70%"), (0.85, "85%")]
DIM_HOLD_PRESETS = [(3, "3 s"), (5, "5 s"), (10, "10 s"), (20, "20 s")]
FINAL_PRESETS = [(0, "Instant"), (0.5, "0.5 s"), (1, "1 s"), (2, "2 s")]
OFF_AFTER_PRESETS = [(0, "Never"), (60, "1 min"), (300, "5 min"), (900, "15 min"), (1800, "30 min")]

DEFAULTS = {
    "blackout": True,           # feature: black out the screens when away
    "away": 0,                  # seconds away before blacking out
    "paused": False,            # tray "Pause": both features on hold
    "transition": "instant",    # instant | fade | dim
    "fade_s": 1.0,              # fade: length of the fade
    "dim_s": 2.0,               # dim first: how long the dim takes
    "dim_level": 0.7,           #   ...how dark the dim gets (0-1)
    "dim_hold_s": 5,            #   ...how long it stays dimmed
    "final_s": 1.0,             #   ...then the fade to full black
    "style": "overlay",         # overlay (black windows) | off (monitor power saving)
    "excluded": [],             # monitors left alone
    "displays_off_after": 0,    # while black, also power the displays off after this long (0 = never)
    "wake_on_presence": True,   # waking: tracker seeing you counts, not just keyboard/mouse
    "use_head": True,           # what counts as you being here: head (face) tracking,
    "use_eyes": True,           #   eye tracking,
    "use_input": True,          #   and keyboard/mouse use (at least one stays on)
    "lock": False,              # feature: lock Windows when away
    "lock_after": 300,
    "lock_black": True,         # displays off on the lock screen while away
    "startup": True,
}


def duration_text(s, short=False):
    if 0 < s < 10 and s != int(s):  # fractions for short fades, e.g. 0.5 s
        return f"{s:g} s" if short else f"{s:g} seconds"
    s = int(round(s))
    if s < 60:
        return f"{s} s" if short else f"{s} second{'s' if s != 1 else ''}"
    if s % 3600 == 0:
        h = s // 3600
        return f"{h} h" if short else f"{h} hour{'s' if h != 1 else ''}"
    if s % 60 == 0:
        m = s // 60
        return f"{m} min" if short else f"{m} minute{'s' if m != 1 else ''}"
    return f"{s // 60} min {s % 60} s"


def log(msg):
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > 1_000_000:
            os.replace(LOG_FILE, LOG_FILE + ".old")
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


def load_settings():
    for path in [SETTINGS_FILE] + OLD_SETTINGS_FILES:
        try:
            with open(path) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            continue
        if "blackout" not in saved and "paused" in saved:
            # old versions used "paused" as the black-out on/off switch
            saved["blackout"], saved["paused"] = not saved["paused"], False
        return {**DEFAULTS, **saved}
    return dict(DEFAULTS)


def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump({k: settings[k] for k in DEFAULTS}, f, indent=1)
    except OSError:
        pass


# ---------------------------------------------------------------- Windows helpers

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


kernel32.GetTickCount.restype = wt.DWORD


def seconds_since_input():
    li = LASTINPUTINFO()
    li.cbSize = ctypes.sizeof(li)
    user32.GetLastInputInfo(ctypes.byref(li))
    return ((kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT),
                ("dwFlags", wt.DWORD), ("szDevice", ctypes.c_wchar * 32)]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("DeviceName", ctypes.c_wchar * 32), ("DeviceString", ctypes.c_wchar * 128),
                ("StateFlags", wt.DWORD), ("DeviceID", ctypes.c_wchar * 128), ("DeviceKey", ctypes.c_wchar * 128)]


def monitors():
    """Monitors left to right: dicts with id (device name), x, y, w, h, primary, name."""
    out = []

    def cb(hmon, _dc, _rect, _lp):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(mi)
        user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        r = mi.rcMonitor
        dd = DISPLAY_DEVICEW()
        dd.cb = ctypes.sizeof(dd)
        name = dd.DeviceString if user32.EnumDisplayDevicesW(mi.szDevice, 0, ctypes.byref(dd), 0) else ""
        out.append({"id": mi.szDevice, "x": r.left, "y": r.top, "w": r.right - r.left, "h": r.bottom - r.top,
                    "primary": bool(mi.dwFlags & 1), "name": name})
        return 1

    proto = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumDisplayMonitors(None, None, proto(cb), None)
    return sorted(out, key=lambda m: (m["x"], m["y"]))


user32.OpenInputDesktop.restype = wt.HANDLE
user32.SwitchDesktop.argtypes = [wt.HANDLE]
user32.CloseDesktop.argtypes = [wt.HANDLE]


def session_locked():
    h = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not h:
        return True  # the secure (lock screen) desktop is the input desktop
    ok = user32.SwitchDesktop(h)
    user32.CloseDesktop(h)
    return not ok


user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]


def displays_off():
    user32.PostMessageW(wt.HWND(0xFFFF), 0x0112, 0xF170, 2)  # broadcast WM_SYSCOMMAND SC_MONITORPOWER off


def displays_on(locked):
    kernel32.SetThreadExecutionState(0x00000002)  # ES_DISPLAY_REQUIRED: wakes the displays
    user32.PostMessageW(wt.HWND(0xFFFF), 0x0112, 0xF170, -1)
    if not locked:  # a 1-pixel nudge is the most reliable wake (can't inject input on the lock screen)
        user32.mouse_event(0x0001, 1, 0, 0, 0)
        user32.mouse_event(0x0001, -1, 0, 0, 0)


def lock_pc():
    user32.LockWorkStation()


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def startup_command():
    if FROZEN:
        return f'"{sys.executable}" --background'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else exe}" "{os.path.abspath(__file__)}" --background'


def set_startup(on):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "PresenceDetect", 0, winreg.REG_SZ, startup_command())
            else:
                try:
                    winreg.DeleteValue(k, "PresenceDetect")
                except FileNotFoundError:
                    pass
            try:
                winreg.DeleteValue(k, "PresenceBlackout")  # entry from before the rename
            except FileNotFoundError:
                pass
    except OSError as e:
        log(f"startup setting failed: {e}")


# ---------------------------------------------------------------- black overlay windows

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE), ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR)]


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND
user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.DWORD, wt.BYTE, wt.DWORD]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetCursor.argtypes = [wt.HANDLE]
gdi32.GetStockObject.restype = wt.HGDIOBJ
kernel32.GetModuleHandleW.restype = wt.HMODULE

OVERLAY_CLASS = "PresenceDetectOverlay"
WS_POPUP = 0x80000000
WS_EX = 0x00000008 | 0x00000080 | 0x00080000 | 0x08000000  # TOPMOST | TOOLWINDOW | LAYERED | NOACTIVATE
HWND_TOPMOST = wt.HWND(-1)


@WNDPROC
def _overlay_proc(hwnd, msg, wp, lp):
    if msg == 0x0020:  # WM_SETCURSOR: hide the mouse pointer over the black screen
        user32.SetCursor(None)
        return 1
    if msg == 0x0021:  # WM_MOUSEACTIVATE: never take focus (so games don't minimize)
        return 3       # MA_NOACTIVATE
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


_wndclass = None


def _register_overlay_class():
    global _wndclass
    if _wndclass is None:
        _wndclass = WNDCLASSW()
        _wndclass.lpfnWndProc = _overlay_proc
        _wndclass.hInstance = kernel32.GetModuleHandleW(None)
        _wndclass.hbrBackground = gdi32.GetStockObject(4)  # BLACK_BRUSH
        _wndclass.lpszClassName = OVERLAY_CLASS
        user32.RegisterClassW(ctypes.byref(_wndclass))


def ease(k):
    """Smooth start and end, and perceptually even: the *remaining brightness* falls
    along a gamma curve, so the fade doesn't seem to rush at the end."""
    s = k * k * k * (k * (k * 6 - 15) + 10)  # smootherstep
    return 1 - (1 - s) ** 2.2


class Overlay:
    """Pure-black, click-through-to-nothing, focus-less windows over chosen monitors.
    Opacity is animated on its own thread, paced to the display refresh (DwmFlush)."""

    def __init__(self):
        self.hwnds = []
        self.alpha = 0.0
        self._anim = None
        self._lock = threading.Lock()
        self._thread = None
        self._stop = False
        self.last_lift = 0.0

    @property
    def visible(self):
        return bool(self.hwnds)

    def show(self, mons):
        self.hide()
        _register_overlay_class()
        hinst = kernel32.GetModuleHandleW(None)
        for m in mons:
            h = user32.CreateWindowExW(WS_EX, OVERLAY_CLASS, APP_NAME, WS_POPUP, m["x"], m["y"], m["w"], m["h"],
                                       None, None, hinst, None)
            if not h:
                continue
            user32.SetLayeredWindowAttributes(h, 0, 0, 2)  # LWA_ALPHA, fully transparent to start
            user32.ShowWindow(h, 4)                          # SW_SHOWNOACTIVATE
            user32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
            self.hwnds.append(h)
        self.alpha = 0.0
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def animate(self, to, seconds):
        with self._lock:
            self._anim = (self.alpha, to, time.perf_counter(), max(seconds, 0.0))

    def _set_alpha(self, a):
        self.alpha = a
        b = int(round(255 * min(max(a, 0.0), 1.0)))
        for h in list(self.hwnds):
            user32.SetLayeredWindowAttributes(h, 0, b, 2)

    def _run(self):
        winmm.timeBeginPeriod(1)
        try:
            while not self._stop:
                with self._lock:
                    anim = self._anim
                if anim is None:
                    time.sleep(0.03)
                    continue
                a0, a1, t0, d = anim
                k = 1.0 if d <= 0 else min((time.perf_counter() - t0) / d, 1.0)
                self._set_alpha(a0 + (a1 - a0) * ease(k))
                if k >= 1.0:
                    with self._lock:
                        if self._anim is anim:
                            self._anim = None
                    continue
                if dwmapi.DwmFlush() != 0:  # waits for the next frame; fall back to ~120 Hz
                    time.sleep(0.008)
        finally:
            winmm.timeEndPeriod(1)

    def keep_on_top(self, now):
        if self.hwnds and now - self.last_lift > 2:  # stay above anything that grabbed topmost meanwhile
            self.last_lift = now
            for h in self.hwnds:
                user32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def hide(self):
        self._stop = True
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None
        for h in self.hwnds:
            user32.DestroyWindow(h)
        self.hwnds = []
        self._anim = None
        self.alpha = 0.0


# ---------------------------------------------------------------- sensing

class Tracker:
    """Wraps TobiiStream; reconnects if Tobii isn't available."""

    def __init__(self):
        self.stream, self.last_try, self.error = None, -1e9, None
        self.was_connected = None
        self.lost_since = None

    @property
    def connected(self):
        return self.stream is not None and self.stream.link_ok

    def poll(self, now):
        # After a replug the old connection usually can't be resumed: drop it and start fresh.
        if self.stream is not None and not self.stream.link_ok:
            self.lost_since = self.lost_since or now
            if now - self.lost_since >= REBUILD_AFTER_S:
                old, self.stream, self.lost_since = self.stream, None, None
                threading.Thread(target=old.stop, daemon=True).start()  # teardown may block on a gone device
                self.last_try = -1e9
        else:
            self.lost_since = None
        if self.stream is None and now - self.last_try > RETRY_TRACKER_S:
            self.last_try = now
            try:
                self.stream = TobiiStream(history_seconds=2).start()
                self.error = None
            except Exception as e:  # tracker unplugged / Tobii service not running
                if str(e) != self.error:
                    log(f"tracker unavailable: {e}")
                self.stream, self.error = None, str(e)

    def signals(self, now):
        """(sensed, details) - sensed is True if anything says someone is sitting there."""
        if self.connected != self.was_connected:
            if self.was_connected is not None or not self.connected:
                log("tracker connected" if self.connected else "tracker DISCONNECTED (unplugged or Tobii service stopped)")
            self.was_connected = self.connected
        s = self.stream
        if not self.connected:
            return False, {"head": None, "gaze": None}
        last = s.latest()
        head_age = now - s.head_t if s.head_t else 1e9
        gaze_age = now - last[0] if last else 1e9
        # Tobii's "user present" signal comes from the same face tracking, so it counts as "head"
        head = head_age < SEEN_GRACE_S or s.presence == PRESENCE_PRESENT
        info = {"head": head, "gaze": gaze_age < SEEN_GRACE_S}
        return any(info.values()), info

    def stop(self):
        if self.stream:
            self.stream.stop()


# ---------------------------------------------------------------- engine

class Engine:
    def __init__(self, settings, dry_run=False):
        self.s = settings
        self.dry = dry_run
        self.tracker, self.overlay = Tracker(), Overlay()
        now = time.perf_counter()
        self.last_seen, self.sensed_since = now, None
        self.active = self.manual = False
        self.mode = self.stage = None
        self.t_on = 0.0
        self.t_black = 0.0      # when the screens reached (or will reach) full black
        self.panel_off = False  # displays powered off while blacked out
        self.last_off, self.off_by_us = -1e9, False
        self.locked, self._lock_checked = False, -1e9
        self.connected_since = None
        self.hold_until = 0.0
        self.status = {}

    def black_out(self, now, manual=False):
        if self.active:
            return
        self.active, self.manual, self.t_on, self.mode = True, manual, now, self.s["style"]
        log("BLACK OUT" + (" (manual)" if manual else ""))
        if self.mode == "off":
            displays_off()
            self.last_off = now
            return
        self.panel_off = False
        self.overlay.show([m for m in monitors() if m["id"] not in self.s["excluded"]])
        tr = self.s["transition"]
        if tr == "dim":
            self.overlay.animate(self.s["dim_level"], self.s["dim_s"])
            self.stage = "dim"
        else:
            dur = self.s["fade_s"] if tr == "fade" else 0
            self.overlay.animate(1.0, dur)
            self.stage, self.t_black = "black", now + dur

    def wake(self, reason):
        if not self.active:
            return
        if self.mode == "off" or self.panel_off:
            displays_on(locked=False)
        self.overlay.hide()
        self.active = self.panel_off = False
        log(f"wake: {reason}")

    def step(self, now):
        s = self.s
        self.tracker.poll(now)
        _, info = self.tracker.signals(now)
        sensed = bool((s["use_head"] and info.get("head")) or (s["use_eyes"] and info.get("gaze")))
        if self.tracker.connected:
            self.connected_since = self.connected_since or now
            if now - self.connected_since < CONNECT_GRACE_S:
                sensed = True  # no data yet - don't mistake that for you having left
        else:
            self.connected_since = None
        if sensed:
            self.last_seen = now
            self.sensed_since = self.sensed_since or now
        else:
            self.sensed_since = None
        idle_raw = seconds_since_input()
        # keyboard/mouse always count while the tracker is unplugged - it's all there is then
        input_counts = s["use_input"] or not self.tracker.connected
        idle = idle_raw if input_counts else 1e9
        away = min(now - self.last_seen, idle)
        back = self.sensed_since is not None and now - self.sensed_since >= WAKE_CONFIRM_S
        back = back and s["wake_on_presence"]
        if now - self._lock_checked >= LOCK_CHECK_S:
            self._lock_checked = now
            self.locked = session_locked()
        locked = self.locked
        # Without the tracker only keyboard/mouse say you're here, so a short delay would
        # black out every time you stop typing for a second - use a screensaver-style wait.
        delay = s["away"] if self.tracker.connected else max(s["away"], NO_TRACKER_AWAY_S)
        lock_delay = s["lock_after"] if self.tracker.connected else max(s["lock_after"], NO_TRACKER_AWAY_S)
        self.status = {"info": info, "sensed": sensed, "idle": idle_raw, "away": away, "locked": locked,
                       "connected": self.tracker.connected, "active": self.active}
        if self.dry:
            return

        if locked:
            if self.active and self.mode == "overlay":  # can't draw over the lock screen anyway
                self.overlay.hide()
                self.active = False
            if s["lock_black"] and not s["paused"]:
                if not sensed and away >= max(delay, 2):
                    if now - self.last_off > REPEAT_OFF_S:
                        displays_off()
                        self.last_off, self.off_by_us = now, True
                elif self.off_by_us and back:
                    displays_on(locked=True)  # you're back: light up the sign-in screen
                    self.off_by_us = False
                    log("displays on - you're back (locked)")
            return
        self.off_by_us = False

        if s["lock"] and not s["paused"] and away >= lock_delay and now >= self.hold_until:
            self.lock_now("away for a while")
            return

        if self.active:
            # the keyboard and mouse always wake the screens, even when they don't count as "here"
            if self.manual:
                if idle_raw < 0.5 and now - self.t_on > 1.0:
                    self.wake("input")
            elif idle_raw < 0.5:
                self.wake("input")
                if not input_counts:
                    self.hold_until = now + INPUT_WAKE_HOLD_S
            elif back:
                self.wake("tracker sees you")
            elif self.mode == "off" or self.panel_off:
                if now - self.last_off > REPEAT_OFF_S:  # in case something woke the displays
                    displays_off()
                    self.last_off = now
            if self.active and self.stage == "dim":
                dimmed_for = (now - self.t_on) if self.manual else (away - delay)
                if dimmed_for >= s["dim_s"] + s["dim_hold_s"]:
                    self.overlay.animate(1.0, s["final_s"])
                    self.stage, self.t_black = "black", now + s["final_s"]
            if (self.active and self.mode == "overlay" and self.stage == "black" and not self.panel_off
                    and s["displays_off_after"] > 0 and now - self.t_black >= s["displays_off_after"]):
                log("displays off (black for a while)")
                displays_off()
                self.panel_off, self.last_off = True, now
            self.overlay.keep_on_top(now)
        elif s["blackout"] and not s["paused"] and away >= max(delay, 0.3) and now >= self.hold_until:
            self.black_out(now)

    def lock_now(self, reason="manual"):
        log(f"LOCK ({reason})")
        self.wake("locking")
        lock_pc()
        self.last_off = -1e9

    def stop(self):
        self.overlay.hide()
        self.tracker.stop()


# ---------------------------------------------------------------- tray icon

def tray_image(color, size=64):
    """Small monitor glyph with a status dot. Drawn large and scaled down for smooth edges."""
    from PIL import Image, ImageDraw
    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((20, 40, 236, 188), radius=22, fill=(0, 0, 0, 255), outline=(225, 228, 235, 255), width=18)
    d.rectangle((112, 188, 144, 214), fill=(225, 228, 235, 255))
    d.rounded_rectangle((70, 210, 186, 230), radius=8, fill=(225, 228, 235, 255))
    d.ellipse((150, 120, 250, 220), fill=color, outline=(0, 0, 0, 255), width=12)
    return img.resize((size, size), Image.LANCZOS)


TRAY_GREEN, TRAY_AMBER, TRAY_GREY = (60, 210, 110, 255), (245, 180, 40, 255), (140, 142, 150, 255)


def start_tray(engine, requests):
    import pystray
    from pystray import Menu, MenuItem as Item

    s = engine.s

    def toggle_pause(_icon, _item):
        s["paused"] = not s["paused"]
        save_settings(s)
        log("paused" if s["paused"] else "resumed")

    icon = pystray.Icon("PresenceDetect", tray_image(TRAY_GREEN), APP_NAME, Menu(
        Item("Open Presence Detect", lambda *_: requests.__setitem__("open", True), default=True),
        Menu.SEPARATOR,
        Item("Lock now", lambda *_: requests.__setitem__("lock_now", True)),
        Item("Black out now", lambda *_: requests.__setitem__("blackout_now", True)),
        Item("Pause", toggle_pause, checked=lambda _item: s["paused"]),
        Menu.SEPARATOR,
        Item("Quit", lambda *_: requests.__setitem__("quit", True)),
    ))
    icon.run_detached()
    return icon


# ---------------------------------------------------------------- settings window

BG = "#0C0C0F"
SIDEBAR = "#111115"
CARD = "#17171D"
CARD_BORDER = "#22222A"
CHIP = "#22222A"
CHIP_HOVER = "#2C2C36"
TEXT = "#ECEDF2"
MUTED = "#8C8E99"
FAINT = "#5C5E68"
ACCENT = "#4C8DFF"
ACCENT_HOVER = "#3A79EC"
ACCENT_SOFT = "#1A2B4C"
GREEN = "#3DD68C"
AMBER = "#F5B13D"
RED = "#F06A6A"


ICON_FONTS = [r"C:\Windows\Fonts\SegoeIcons.ttf", r"C:\Windows\Fonts\segmdl2.ttf"]  # Win 11, Win 10
GLYPHS = {"home": "\uE80F", "detect": "\uE890", "blackout": "\uE708", "screens": "\uE7F4", "lock": "\uE72E", "general": "\uE713"}


def glyph_image(ctk, glyph, color, size=18):
    """A Windows icon-font glyph rendered as an image (crisp at any DPI)."""
    from PIL import Image, ImageDraw, ImageFont
    font_path = next((p for p in ICON_FONTS if os.path.exists(p)), None)
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    if font_path:
        f = ImageFont.truetype(font_path, int(S * 0.8))
        d = ImageDraw.Draw(img)
        d.text((S / 2, S / 2), glyph, font=f, fill=color, anchor="mm")
    img = img.resize((size * 2, size * 2), Image.LANCZOS)
    return ctk.CTkImage(light_image=img, dark_image=img, size=(size, size))


UNIT_WORDS = {"": None, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
              "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
              "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600, "%": 0.01}


def parse_value(text, default_factor):
    """'45' / '45s' / '2m' / '1.5 min' / '70%' -> number in base units, or None."""
    import re
    m = re.fullmatch(r"\s*([0-9]*[.,]?[0-9]+)\s*([a-zA-Z%]*)\s*", text)
    if not m:
        return None
    unit = m.group(2).lower()
    if unit not in UNIT_WORDS:
        return None
    factor = UNIT_WORDS[unit] or default_factor
    return float(m.group(1).replace(",", ".")) * factor


class OptionRow:
    """One-click preset chips, plus an 'Other…' box to type any value (e.g. 45s, 2m)."""

    def __init__(self, app, parent, title, key, presets, columns, factor, fmt, lo, hi, example):
        ctk = app.ctk
        self.app, self.key, self.presets, self.factor, self.fmt, self.lo, self.hi = \
            app, key, presets, factor, fmt, lo, hi
        self.example = example
        self.frame = ctk.CTkFrame(parent, fg_color="transparent")
        if title:
            ctk.CTkLabel(self.frame, text=title, font=app.F["label"], text_color=MUTED,
                         anchor="w").pack(fill="x", pady=(10, 4))
        grid = ctk.CTkFrame(self.frame, fg_color="transparent")
        grid.pack(fill="x")
        self.buttons = {}
        cells = len(presets) + 1
        for i, (value, label) in enumerate(presets):
            b = ctk.CTkButton(grid, text=label, height=34, width=20, corner_radius=8, fg_color=CHIP,
                              hover_color=CHIP_HOVER, text_color=TEXT, font=app.F["chip"],
                              command=lambda v=value: self.set(v))
            b.grid(row=i // columns, column=i % columns, padx=3, pady=3, sticky="ew")
            self.buttons[value] = b
        i = cells - 1
        self.entry = ctk.CTkEntry(grid, height=34, width=20, corner_radius=8, font=app.F["chip"],
                                  justify="center", fg_color=CHIP, border_width=1, border_color=CHIP,
                                  text_color=MUTED)
        self.entry.grid(row=i // columns, column=i % columns, padx=3, pady=3, sticky="ew")
        for c in range(columns):
            grid.grid_columnconfigure(c, weight=1, uniform="chip")
        self.error = ctk.CTkLabel(self.frame, text="", font=app.F["small"], text_color=RED, anchor="w", height=16)
        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", lambda e: self._commit())
        self.entry.bind("<Return>", lambda e: (self._commit(), self.app.root.focus_set()))
        self.entry.bind("<Escape>", lambda e: (self.refresh(self.enabled), self.app.root.focus_set()))
        self.editing = False
        self.enabled = True

    def pack(self, **kw):
        self.frame.pack(fill="x", **kw)

    def pack_forget(self):
        self.frame.pack_forget()

    def _focus_in(self, _e):
        if not self.enabled:
            return
        self.editing = True
        self.entry.delete(0, "end")
        self.entry.configure(text_color=TEXT, border_color=ACCENT, fg_color=CARD)
        self.error.pack_forget()

    def _commit(self):
        if not self.editing:
            return
        self.editing = False
        text = self.entry.get().strip()
        if not text:
            self.refresh(self.enabled)
            return
        v = parse_value(text, self.factor)
        if v is None or not self.lo <= v <= self.hi:
            self.error.configure(text=f"Try something like {self.example}  "
                                      f"(between {self.fmt(self.lo)} and {self.fmt(self.hi)})")
            self.error.pack(fill="x", padx=4, pady=(2, 0))
            self.refresh(self.enabled)
            return
        self.error.pack_forget()
        self.set(round(v, 3))

    def set(self, value):
        self.app.s[self.key] = value
        save_settings(self.app.s)
        log(f"setting {self.key} = {value}")
        self.app.refresh_all()

    def refresh(self, enabled=True):
        self.enabled = enabled
        v = self.app.s[self.key]
        match = next((p for p, _ in self.presets if abs(p - v) < 1e-9), None)
        for p, b in self.buttons.items():
            on = p == match
            b.configure(fg_color=(ACCENT if enabled else ACCENT_SOFT) if on else CHIP,
                        hover_color=ACCENT_HOVER if on else CHIP_HOVER,
                        text_color="#FFFFFF" if on else (TEXT if enabled else FAINT),
                        state="normal" if enabled else "disabled")
        if self.editing:
            return
        self.entry.configure(state="normal")
        self.entry.delete(0, "end")
        if match is None:  # a typed-in value is active: show it, highlighted
            self.entry.insert(0, self.fmt(v))
            self.entry.configure(fg_color=ACCENT if enabled else ACCENT_SOFT, border_color=ACCENT,
                                 text_color="#FFFFFF" if enabled else FAINT)
        else:
            self.entry.insert(0, "Other\u2026")
            self.entry.configure(fg_color=CHIP, border_color=CHIP, text_color=MUTED if enabled else FAINT)
        if not enabled:
            self.entry.configure(state="disabled")


def short(s):
    return duration_text(s, True)


def pct(v):
    return f"{v * 100:g}%"


class App:
    PAGES = [("home", "Home"), ("lock", "Lock"), ("blackout", "Black out"), ("screens", "Screens"),
             ("detect", "Detection"), ("general", "General")]

    def __init__(self, root, engine, requests):
        import customtkinter as ctk
        self.ctk, self.root, self.engine, self.s, self.requests = ctk, root, engine, engine.s, requests
        self._last_monitors = None
        root.title(APP_NAME)
        root.configure(fg_color=BG)
        root.geometry("1120x760")
        root.minsize(980, 640)
        root.protocol("WM_DELETE_WINDOW", self.hide)
        root.after(300, self._set_icon)  # customtkinter sets its own icon at ~200 ms
        semi = "Segoe UI Semibold"
        self.F = {
            "app": ctk.CTkFont(semi, 15),
            "page": ctk.CTkFont(semi, 26),
            "page_sub": ctk.CTkFont("Segoe UI", 13),
            "card": ctk.CTkFont(semi, 15),
            "body": ctk.CTkFont("Segoe UI", 14),
            "desc": ctk.CTkFont("Segoe UI", 12),
            "label": ctk.CTkFont("Segoe UI", 12),
            "small": ctk.CTkFont("Segoe UI", 11),
            "small_off": ctk.CTkFont("Segoe UI", 11, overstrike=True),
            "chip": ctk.CTkFont("Segoe UI", 13),
            "nav": ctk.CTkFont("Segoe UI", 14),
            "status": ctk.CTkFont(semi, 15),
            "sentence": ctk.CTkFont(semi, 14),
        }
        self.scale = ctk.ScalingTracker.get_window_scaling(root)
        self.page = None
        self.switch_vars = []  # (var, settings key, inverted) - kept in sync by refresh_all
        self._build()
        self.select_page("blackout")
        self.refresh_all()

    # ---- building blocks
    def _set_icon(self):
        try:
            self.root.iconbitmap(ICON_FILE)
        except tk.TclError:
            pass

    def card(self, parent, title=None, desc=None, pady=(0, 12)):
        ctk = self.ctk
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12, border_width=1, border_color=CARD_BORDER)
        f.pack(fill="x", pady=pady)
        inner = ctk.CTkFrame(f, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=16)
        if title:
            ctk.CTkLabel(inner, text=title, font=self.F["card"], text_color=TEXT, anchor="w").pack(fill="x")
        if desc:
            ctk.CTkLabel(inner, text=desc, font=self.F["desc"], text_color=MUTED, anchor="w", justify="left",
                         wraplength=int(720 * self.scale)).pack(fill="x", pady=(2, 0))
        return inner

    def switch_card(self, parent, title, desc, key, on_change=None, invert=False):
        """A Windows-Settings style row: text on the left, switch on the right."""
        ctk = self.ctk
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12, border_width=1, border_color=CARD_BORDER)
        f.pack(fill="x", pady=(0, 12))
        row = ctk.CTkFrame(f, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=16)
        var = ctk.BooleanVar(value=bool(self.s[key]) != invert)
        self.switch_vars.append((var, key, invert))

        def changed():
            self.s[key] = var.get() != invert
            save_settings(self.s)
            log(f"setting {key} = {self.s[key]}")
            if on_change:
                on_change()
            self.refresh_all()
        ctk.CTkSwitch(row, text="", variable=var, command=changed, progress_color=ACCENT, width=50,
                      switch_width=46, switch_height=24).pack(side="right", padx=(16, 0))
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(txt, text=title, font=self.F["card"], text_color=TEXT, anchor="w").pack(fill="x")
        if desc:
            ctk.CTkLabel(txt, text=desc, font=self.F["desc"], text_color=MUTED, anchor="w", justify="left",
                         wraplength=int(620 * self.scale)).pack(fill="x", pady=(2, 0))
        return var

    def button_card(self, parent, title, desc, button, command, danger=False):
        ctk = self.ctk
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=12, border_width=1, border_color=CARD_BORDER)
        f.pack(fill="x", pady=(0, 12))
        row = ctk.CTkFrame(f, fg_color="transparent")
        row.pack(fill="x", padx=20, pady=16)
        ctk.CTkButton(row, text=button, width=110, height=34, corner_radius=8, font=self.F["chip"],
                      fg_color=CHIP, hover_color="#3A2226" if danger else CHIP_HOVER,
                      text_color=RED if danger else TEXT, command=command).pack(side="right", padx=(16, 0))
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(txt, text=title, font=self.F["card"], text_color=TEXT, anchor="w").pack(fill="x")
        ctk.CTkLabel(txt, text=desc, font=self.F["desc"], text_color=MUTED, anchor="w", justify="left",
                     wraplength=int(620 * self.scale)).pack(fill="x", pady=(2, 0))

    def segmented(self, parent, options, command):
        ctk = self.ctk
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.buttons = {}
        for i, (value, label) in enumerate(options):
            b = ctk.CTkButton(frame, text=label, height=36, width=20, corner_radius=8, fg_color=CHIP,
                              hover_color=CHIP_HOVER, text_color=TEXT, font=self.F["chip"],
                              command=lambda v=value: command(v))
            b.grid(row=0, column=i, padx=3, pady=3, sticky="ew")
            frame.buttons[value] = b
            frame.grid_columnconfigure(i, weight=1, uniform="seg")
        return frame

    @staticmethod
    def select(frame, value):
        for v, b in frame.buttons.items():
            on = v == value
            b.configure(fg_color=ACCENT if on else CHIP, hover_color=ACCENT_HOVER if on else CHIP_HOVER,
                        text_color="#FFFFFF" if on else TEXT)

    # ---- layout
    def _build(self):
        ctk, F = self.ctk, self.F
        root = self.root
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)

        # ---------- sidebar
        side = ctk.CTkFrame(root, fg_color=SIDEBAR, corner_radius=0, width=int(250))
        side.grid(row=0, column=0, sticky="nsw")
        side.grid_propagate(False)
        side.pack_propagate(False)
        brand = ctk.CTkFrame(side, fg_color="transparent")
        brand.pack(fill="x", padx=20, pady=(24, 22))
        logo = tray_image(TRAY_GREEN, 64)
        ctk.CTkLabel(brand, text="", image=ctk.CTkImage(logo, logo, size=(28, 28))).pack(side="left")
        ctk.CTkLabel(brand, text=APP_NAME, font=F["app"], text_color=TEXT).pack(side="left", padx=10)

        self.nav = {}
        for key, label in self.PAGES:
            b = ctk.CTkButton(side, text="   " + label, anchor="w", height=42, corner_radius=8, font=F["nav"],
                              fg_color="transparent", hover_color=CHIP, text_color=MUTED,
                              image=glyph_image(ctk, GLYPHS[key], (200, 203, 212, 255)),
                              command=lambda k=key: self.select_page(k))
            b.pack(fill="x", padx=12, pady=2)
            self.nav[key] = b

        # status panel
        st = ctk.CTkFrame(side, fg_color=CARD, corner_radius=12, border_width=1, border_color=CARD_BORDER)
        st.pack(side="bottom", fill="x", padx=12, pady=16)
        sti = ctk.CTkFrame(st, fg_color="transparent")
        sti.pack(fill="x", padx=14, pady=14)
        self.status_title = ctk.CTkLabel(sti, text="", font=F["status"], text_color=GREEN, anchor="w")
        self.status_title.pack(fill="x")
        self.status_sub = ctk.CTkLabel(sti, text="", font=F["desc"], text_color=MUTED, anchor="w",
                                       justify="left", wraplength=int(200 * self.scale))
        self.status_sub.pack(fill="x")
        sens = ctk.CTkFrame(sti, fg_color="transparent")
        sens.pack(fill="x", pady=(8, 10))
        self.sensor_labels = {}
        for key, label in (("head", "Head"), ("gaze", "Eyes"), ("input", "Keys / mouse")):
            lb = ctk.CTkLabel(sens, text=f"\u25CF {label}", font=F["small"], text_color=FAINT)
            lb.pack(side="left", padx=(0, 8))
            lb.base = label
            self.sensor_labels[key] = lb
        btns = ctk.CTkFrame(sti, fg_color="transparent")
        btns.pack(fill="x")
        btns.grid_columnconfigure((0, 1), weight=1, uniform="b")
        ctk.CTkButton(btns, text="Lock now", height=36, width=20, corner_radius=8, font=F["chip"],
                      fg_color=CHIP, hover_color=CHIP_HOVER,
                      command=lambda: self.requests.__setitem__("lock_now", True)).grid(row=0, column=0, sticky="ew",
                                                                                         padx=(0, 4))
        ctk.CTkButton(btns, text="Black out", height=36, width=20, corner_radius=8, font=F["chip"],
                      fg_color=ACCENT, hover_color=ACCENT_HOVER,
                      command=lambda: self.requests.__setitem__("blackout_now", True)).grid(row=0, column=1,
                                                                                             sticky="ew", padx=(4, 0))

        # ---------- content
        content = ctk.CTkFrame(root, fg_color="transparent")
        content.grid(row=0, column=1, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(content, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=36, pady=(26, 10))
        self.page_title = ctk.CTkLabel(head, text="", font=F["page"], text_color=TEXT, anchor="w")
        self.page_title.pack(fill="x")
        self.page_sub = ctk.CTkLabel(head, text="", font=F["page_sub"], text_color=MUTED, anchor="w")
        self.page_sub.pack(fill="x")

        self.pages = {}
        for key, _ in self.PAGES:
            p = ctk.CTkScrollableFrame(content, fg_color="transparent", scrollbar_button_color=BG,
                                       scrollbar_button_hover_color=CHIP)
            p.grid(row=1, column=0, sticky="nsew", padx=(28, 16), pady=(4, 20))
            p.grid_remove()
            self.pages[key] = p
        self._build_home(self.pages["home"])
        self._build_blackout(self.pages["blackout"])
        self._build_screens(self.pages["screens"])
        self._build_lock(self.pages["lock"])
        self._build_detect(self.pages["detect"])
        self._build_general(self.pages["general"])

    def feature_card(self, parent, glyph, title, desc, key, page):
        """Big on/off card for one of the two features, with a summary and a link to its settings."""
        ctk, F = self.ctk, self.F
        f = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1, border_color=CARD_BORDER)
        f.pack(fill="x", pady=(0, 14))
        row = ctk.CTkFrame(f, fg_color="transparent")
        row.pack(fill="x", padx=22, pady=20)
        ctk.CTkLabel(row, text="", image=glyph_image(ctk, glyph, (76, 141, 255, 255), 28)).pack(side="left",
                                                                                               padx=(0, 18))
        var = ctk.BooleanVar(value=bool(self.s[key]))
        self.switch_vars.append((var, key, False))

        def changed():
            self.s[key] = var.get()
            save_settings(self.s)
            log(f"setting {key} = {self.s[key]}")
            self.refresh_all()
        ctk.CTkSwitch(row, text="", variable=var, command=changed, progress_color=ACCENT, width=50,
                      switch_width=46, switch_height=24).pack(side="right", padx=(16, 0))
        ctk.CTkButton(row, text="Settings  \u203A", width=96, height=32, corner_radius=8, font=F["chip"],
                      fg_color=CHIP, hover_color=CHIP_HOVER,
                      command=lambda: self.select_page(page)).pack(side="right")
        txt = ctk.CTkFrame(row, fg_color="transparent")
        txt.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(txt, text=title, font=ctk.CTkFont("Segoe UI Semibold", 17), text_color=TEXT,
                     anchor="w").pack(fill="x")
        ctk.CTkLabel(txt, text=desc, font=F["desc"], text_color=MUTED, anchor="w", justify="left",
                     wraplength=int(480 * self.scale)).pack(fill="x", pady=(2, 0))
        summary = ctk.CTkLabel(txt, text="", font=F["sentence"], text_color=ACCENT, anchor="w")
        summary.pack(fill="x", pady=(6, 0))
        return summary

    def _build_home(self, page):
        ctk, F = self.ctk, self.F
        self.lock_summary = self.feature_card(
            page, GLYPHS["lock"], "Lock my PC when I walk away",
            "Keeps your PC safe when you leave it. Windows locks itself, so nobody can use it.", "lock", "lock")
        self.blackout_summary = self.feature_card(
            page, GLYPHS["blackout"], "Black out my screens when I walk away",
            "Turns your screens black and brings them back when you sit down. Protects OLED screens "
            "from burn-in and saves power.", "blackout", "blackout")
        c = self.card(page, "How it works")
        ctk.CTkLabel(c, text=("Your Tobii Eye Tracker 5 watches for your head and eyes \u2014 not where you're "
                              "looking \u2014 so reading another monitor never sets anything off. Using the "
                              "keyboard or mouse also counts as being here. You can change this on the "
                              "Detection page."),
                     font=F["desc"], text_color=MUTED, anchor="w", justify="left",
                     wraplength=int(720 * self.scale)).pack(fill="x", pady=(4, 0))

    def _build_blackout(self, page):
        ctk, F = self.ctk, self.F
        self.switch_card(page, "Black out my screens when I walk away",
                         "Turn your screens black when you leave your desk.", "blackout")

        c = self.card(page, "How long to wait", "After you leave, wait this long before the screens go black.")
        self.sentence = ctk.CTkLabel(c, text="", font=F["sentence"], text_color=ACCENT, anchor="w")
        self.sentence.pack(fill="x", pady=(10, 0))
        self.away_row = OptionRow(self, c, None, "away", BLACKOUT_PRESETS, 6, 1, short, 0, 24 * 3600, "45s or 3m")
        self.away_row.pack(pady=(6, 0))

        c = self.card(page, "How the screens go black")
        self.tr_seg = self.segmented(c, [("instant", "Instantly"), ("fade", "Fade out"), ("dim", "Dim, then black")],
                                     self.pick_transition)
        self.tr_seg.pack(fill="x", pady=(10, 0))
        self.tr_opts = ctk.CTkFrame(c, fg_color="transparent")
        self.fade_row = OptionRow(self, self.tr_opts, "Fade takes", "fade_s", FADE_PRESETS, 6, 1, short,
                                  0.1, 60, "1.5s")
        self.dim_rows = [
            OptionRow(self, self.tr_opts, "Dimming takes", "dim_s", DIM_OVER_PRESETS, 5, 1, short, 0.1, 60, "1.5s"),
            OptionRow(self, self.tr_opts, "Dim by", "dim_level", DIM_LEVEL_PRESETS, 5, 0.01, pct, 0.05, 0.99, "60%"),
            OptionRow(self, self.tr_opts, "Stay dimmed for", "dim_hold_s", DIM_HOLD_PRESETS, 5, 1, short,
                      0, 3600, "15s"),
            OptionRow(self, self.tr_opts, "Then fade to black in", "final_s", FINAL_PRESETS, 5, 1, short,
                      0, 60, "1.5s"),
        ]
        self.dim_note = ctk.CTkLabel(self.tr_opts, text="", font=F["desc"], text_color=MUTED, anchor="w",
                                     justify="left", wraplength=int(720 * self.scale))

        self.switch_card(page, "Wake up when I sit back down",
                         "When this is off, only the keyboard or mouse turns the screens back on.",
                         "wake_on_presence")

    def _build_screens(self, page):
        ctk, F = self.ctk, self.F
        c = self.card(page, "Which screens go black", "Click a screen to include or leave it out.")
        self.map_w, self.map_h = int(640 * self.scale), int(190 * self.scale)
        self.map = tk.Canvas(c, width=self.map_w, height=self.map_h, bg=CARD, highlightthickness=0, cursor="hand2")
        self.map.pack(pady=(14, 8))
        ctk.CTkButton(c, text="Show screen numbers", width=170, height=32, corner_radius=8, fg_color=CHIP,
                      hover_color=CHIP_HOVER, font=F["chip"], command=self.identify).pack(anchor="w")

        c = self.card(page, "Black screen or power off")
        self.style_seg = self.segmented(c, [("overlay", "Black screen"), ("off", "Power the displays off")],
                                        self.pick_style)
        self.style_seg.pack(fill="x", pady=(10, 6))
        self.style_desc = ctk.CTkLabel(c, text="", font=F["desc"], text_color=MUTED, anchor="w", justify="left",
                                       wraplength=int(720 * self.scale))
        self.style_desc.pack(fill="x")

        c = self.card(page, "Power off display after a while",
                      "Black first, then switch the displays off if you're still away. "
                      "Saves power on regular (non-OLED) monitors.")
        self.off_row = OptionRow(self, c, None, "displays_off_after", OFF_AFTER_PRESETS, 6, 60, short,
                                 0, 24 * 3600, "20m")
        self.off_row.pack(pady=(8, 0))
        self.off_note = ctk.CTkLabel(c, text="", font=F["desc"], text_color=MUTED, anchor="w")
        self.off_note.pack(fill="x", pady=(6, 0))

    def _build_lock(self, page):
        ctk, F = self.ctk, self.F
        self.switch_card(page, "Lock my PC when I walk away",
                         "Windows locks itself, so you'll need your PIN, password or face to get back in. "
                         "Works on its own \u2014 your screens don't have to go black.", "lock")
        c = self.card(page, "Lock after", "How long you can be away before the PC locks.")
        self.lock_sentence = ctk.CTkLabel(c, text="", font=F["sentence"], text_color=ACCENT, anchor="w")
        self.lock_sentence.pack(fill="x", pady=(10, 0))
        self.lock_row = OptionRow(self, c, None, "lock_after", LOCK_PRESETS, 5, 60, short, 5, 24 * 3600,
                                  "90s or 10m")
        self.lock_row.pack(pady=(6, 0))
        self.switch_card(page, "Keep the screens dark while locked",
                         "The lock screen can't be covered, so the displays are switched off while you're "
                         "away and back on when you return.", "lock_black")

    def _build_detect(self, page):
        ctk, F = self.ctk, self.F
        keys = ("use_head", "use_eyes", "use_input")

        def guard(key):
            def check():
                if not any(self.s[k] for k in keys):  # nothing left to tell you're here
                    self.s[key] = True
                    save_settings(self.s)
                    self.detect_warn.configure(text="At least one of these has to stay on.")
                else:
                    self.detect_warn.configure(text="")
            return check

        self.switch_card(page, "Head",
                         "You count as here while the tracker can see your head and face.", "use_head",
                         guard("use_head"))
        self.switch_card(page, "Eyes",
                         "You count as here while the tracker can see your eyes.", "use_eyes", guard("use_eyes"))
        self.switch_card(page, "Keyboard and mouse",
                         "Typing or moving the mouse counts as being here. Even when this is off, they still "
                         "wake a black screen \u2014 and they're always used if the tracker is unplugged.",
                         "use_input", guard("use_input"))
        self.detect_warn = ctk.CTkLabel(page, text="", font=F["desc"], text_color=AMBER, anchor="w")
        self.detect_warn.pack(fill="x", padx=4)

    def _build_general(self, page):
        self.switch_card(page, "Start with Windows",
                         "Runs quietly in the system tray (by the clock) every time you sign in.",
                         "startup", lambda: set_startup(self.s["startup"]))
        self.button_card(page, "Activity log", "See when your PC locked or your screens went black, and why.",
                         "Open",
                         lambda: os.startfile(LOG_FILE) if os.path.exists(LOG_FILE) else None)
        self.button_card(page, "Reset settings", "Go back to the original settings.", "Reset",
                         self.reset_defaults)
        self.button_card(page, "Quit", "Stops Presence Detect until you open it again.", "Quit",
                         lambda: self.requests.__setitem__("quit", True), danger=True)

    # ---- navigation
    PAGE_SUBTITLES = {
        "home": "Lock your PC and black out your screens when you walk away.",
        "blackout": "When and how your screens go black.",
        "screens": "Pick which screens go black and how they switch off.",
        "lock": "Lock Windows when you've been away for a while.",
        "detect": "Choose what counts as you being at your desk.",
        "general": "Startup, activity log and more.",
    }

    def select_page(self, key):
        if self.page:
            self.pages[self.page].grid_remove()
        self.page = key
        self.pages[key].grid()
        self.page_title.configure(text=dict(self.PAGES)[key])
        self.page_sub.configure(text=self.PAGE_SUBTITLES[key])
        for k, b in self.nav.items():
            b.configure(fg_color=CHIP if k == key else "transparent", text_color=TEXT if k == key else MUTED)

    # ---- actions
    def pick_transition(self, v):
        self.s["transition"] = v
        save_settings(self.s)
        self.refresh_all()

    def pick_style(self, v):
        self.s["style"] = v
        save_settings(self.s)
        self.refresh_all()

    def reset_defaults(self):
        startup = self.s["startup"]
        self.s.clear()
        self.s.update({k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()})
        self.s["startup"] = startup  # leave login behaviour as the user set it
        save_settings(self.s)
        log("settings reset to defaults")
        self.refresh_all()

    def toggle_monitor(self, mid):
        ex = self.s["excluded"]
        if mid in ex:
            ex.remove(mid)
        else:
            ex.append(mid)
        save_settings(self.s)
        self.draw_map()

    def identify(self):
        for n, m in enumerate(monitors(), 1):
            t = tk.Toplevel(self.root)
            t.overrideredirect(True)
            t.attributes("-topmost", True)
            size = int(min(m["w"], m["h"]) * 0.28)
            t.geometry(f"{size}x{size}+{m['x'] + (m['w'] - size) // 2}+{m['y'] + (m['h'] - size) // 2}")
            t.configure(bg=BG)
            tk.Label(t, text=str(n), font=("Segoe UI Semibold", int(size * 0.32)), fg=ACCENT, bg=BG).pack(expand=True)
            t.after(2200, t.destroy)

    # ---- drawing / refresh
    def draw_map(self):
        c = self.map
        c.delete("all")
        mons = monitors()
        self._last_monitors = [(m["id"], m["x"], m["y"], m["w"], m["h"]) for m in mons]
        if not mons:
            return
        x0, y0 = min(m["x"] for m in mons), min(m["y"] for m in mons)
        x1, y1 = max(m["x"] + m["w"] for m in mons), max(m["y"] + m["h"] for m in mons)
        pad = 8 * self.scale
        k = min((self.map_w - 2 * pad) / (x1 - x0), (self.map_h - 2 * pad) / (y1 - y0))
        ox = (self.map_w - (x1 - x0) * k) / 2
        oy = (self.map_h - (y1 - y0) * k) / 2
        gap = 4 * self.scale
        for n, m in enumerate(mons, 1):
            inc = m["id"] not in self.s["excluded"]
            a = ox + (m["x"] - x0) * k + gap
            b = oy + (m["y"] - y0) * k + gap
            cc = ox + (m["x"] + m["w"] - x0) * k - gap
            d = oy + (m["y"] + m["h"] - y0) * k - gap
            tag = f"mon{n}"
            r = 12 * self.scale
            pts = [a + r, b, cc - r, b, cc, b, cc, b + r, cc, d - r, cc, d, cc - r, d, a + r, d, a, d, a, d - r, a, b + r, a, b]
            c.create_polygon(pts, smooth=True, fill=ACCENT_SOFT if inc else "#101014",
                             outline=ACCENT if inc else "#2C2C34", width=max(1, int(2 * self.scale)), tags=tag)
            mx, my = (a + cc) / 2, (b + d) / 2
            c.create_text(mx, my - 18 * self.scale, text=str(n), fill=TEXT if inc else FAINT,
                          font=("Segoe UI Semibold", 20), tags=tag)
            c.create_text(mx, my + 6 * self.scale, text=f"{m['w']}\u00d7{m['h']}" + ("  \u00b7  main" if m["primary"] else ""),
                          fill=MUTED if inc else FAINT, font=("Segoe UI", 9), tags=tag)
            c.create_text(mx, my + 24 * self.scale, text="\u2713 goes black" if inc else "left alone",
                          fill=ACCENT if inc else FAINT, font=("Segoe UI", 9), tags=tag)
            c.tag_bind(tag, "<Button-1>", lambda e, mid=m["id"]: self.toggle_monitor(mid))

    def refresh_all(self):
        s = self.s
        for var, key, invert in self.switch_vars:  # e.g. after Reset, or pausing from the tray
            if var.get() != (bool(s[key]) != invert):
                var.set(bool(s[key]) != invert)
        # home + black out page
        if not s["blackout"]:
            text, col = "Off \u2014 your screens stay on.", MUTED
        elif s["away"] < 0.5:
            text, col = "Your screens go black the moment you leave.", ACCENT
        else:
            text, col = f"Your screens go black {duration_text(s['away'])} after you leave.", ACCENT
        self.sentence.configure(text=text, text_color=col)
        self.blackout_summary.configure(text=text, text_color=col)
        self.away_row.refresh(enabled=s["blackout"])
        if s["lock"]:
            text, col = f"Your PC locks {duration_text(s['lock_after'])} after you leave.", ACCENT
        else:
            text, col = "Off \u2014 your PC won't lock by itself.", MUTED
        self.lock_sentence.configure(text=text, text_color=col)
        self.lock_summary.configure(text=text, text_color=col)
        tr = s["transition"]
        self.select(self.tr_seg, tr)
        self.fade_row.pack_forget()
        for r in self.dim_rows:
            r.pack_forget()
        self.dim_note.pack_forget()
        if tr == "instant":
            self.tr_opts.pack_forget()  # an emptied tk frame keeps its old size, so hide it
        else:
            self.tr_opts.pack(fill="x", pady=(4, 0))
        if tr == "fade":
            self.fade_row.pack()
            self.fade_row.refresh()
        elif tr == "dim":
            for r in self.dim_rows:
                r.pack()
                r.refresh()
            self.dim_note.configure(text="The dim is a heads-up: if you're still there, move the mouse and it "
                                         "comes straight back.")
            self.dim_note.pack(fill="x", pady=(10, 0))
        # screens page
        self.select(self.style_seg, s["style"])
        overlay = s["style"] == "overlay"
        self.style_desc.configure(text=(
            "Covers the screens you picked with pure black. Comes back instantly." if overlay else
            "Uses your monitors' own power saving. Affects every screen, and they take a second or two "
            "to come back."))
        self.draw_map()
        self.off_row.refresh(enabled=overlay)
        if not overlay:
            self.off_note.configure(text="Not needed \u2014 \u201cPower the displays off\u201d is already selected.")
        elif s["displays_off_after"]:
            self.off_note.configure(text=f"Displays switch off after {duration_text(s['displays_off_after'])} "
                                         "of black.")
        else:
            self.off_note.configure(text="The screens stay black and the displays stay on.")
        # lock page
        self.lock_row.refresh(enabled=s["lock"])

    def update_status(self):
        st = self.engine.status
        if not st or not self.root.winfo_viewable():
            return
        s = self.s
        delay = s["away"] if st["connected"] else max(s["away"], NO_TRACKER_AWAY_S)
        lock_delay = s["lock_after"] if st["connected"] else max(s["lock_after"], NO_TRACKER_AWAY_S)
        if not st["connected"]:
            title, col = "\u25CF  Tracker not found", RED
            sub = (f"Plug in your Tobii. Until then it only goes by the keyboard and mouse "
                   f"(waits at least {short(NO_TRACKER_AWAY_S)}).")
        elif s["paused"]:
            title, col, sub = "\u25CF  Paused", AMBER, "Unpause from the tray icon by the clock."
        elif not s["lock"] and not s["blackout"]:
            title, col, sub = "\u25CF  Nothing switched on", AMBER, "Turn on Lock or Black out on the Home page."
        elif st["active"]:
            title, col, sub = "\u25CF  Screens are black", ACCENT, "Move the mouse or sit down to wake them."
        elif st["away"] > 1:
            title, col = "\u25CF  You've stepped away", MUTED
            parts = [f"Away {short(st['away'])}"]
            if s["blackout"]:
                parts.append(f"black in {short(max(delay - st['away'], 0))}")
            if s["lock"]:
                parts.append(f"locks in {short(max(lock_delay - st['away'], 0))}")
            sub = " \u00b7 ".join(parts)
        else:
            title, col, sub = "\u25CF  You're here", GREEN, "Watching for when you leave."
        self.status_title.configure(text=title, text_color=col)
        self.status_sub.configure(text=sub)
        info = st["info"]
        live = {"head": info.get("head"), "gaze": info.get("gaze"), "input": st["idle"] < 2}
        for key, setting in (("head", "use_head"), ("gaze", "use_eyes"), ("input", "use_input")):
            lb = self.sensor_labels[key]
            if s[setting]:
                lb.configure(text=f"\u25CF {lb.base}", font=self.F["small"], text_color=GREEN if live[key] else FAINT)
            else:  # switched off on the Detection page: crossed out
                lb.configure(text=f"\u25CB {lb.base}", font=self.F["small_off"], text_color=FAINT)
        if s["paused"] != getattr(self, "_shown_paused", None):  # paused from the tray
            self._shown_paused = s["paused"]
            self.refresh_all()
        mons = [(m["id"], m["x"], m["y"], m["w"], m["h"]) for m in monitors()]
        if mons != self._last_monitors:
            self.draw_map()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.refresh_all()

    def hide(self):
        self.root.withdraw()


# ---------------------------------------------------------------- main

def hotkey(vk):
    return all(user32.GetAsyncKeyState(k) & 0x8000 for k in (0x11, 0x12, vk))  # Ctrl + Alt + vk


def run_test(engine):
    print("Live readout (never locks or blacks out). Ctrl+C to stop.")
    try:
        while True:
            now = time.perf_counter()
            engine.step(now)
            st = engine.status
            i = st["info"]
            mark = lambda v: "yes" if v else " - "  # noqa: E731
            verdict = "PRESENT" if st["away"] < 0.5 else f"away {st['away']:4.1f}s"
            print(f"{verdict:12} | head {mark(i['head'])}  eyes {mark(i['gaze'])}"
                  f"  | input {st['idle']:5.1f}s ago" + ("  | LOCKED" if st["locked"] else "")
                  + ("" if st["connected"] else f"  | tracker: {engine.tracker.error}"), flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--background", action="store_true", help="start hidden in the tray")
    ap.add_argument("--test", action="store_true", help="console readout of what it senses; takes no action")
    args = ap.parse_args()

    settings = load_settings()
    if args.test:
        return run_test(Engine(settings, dry_run=True))

    # one copy at a time; starting it again just opens the window of the running one
    kernel32.CreateMutexW(None, False, "PresenceDetectSingleInstance")
    already = kernel32.GetLastError() == 183
    kernel32.CreateEventW.restype = wt.HANDLE
    show_event = kernel32.CreateEventW(None, False, False, "PresenceDetectShowWindow")
    if already:
        if not args.background:
            kernel32.SetEvent(wt.HANDLE(show_event))
        return

    try:
        shell32.SetCurrentProcessExplicitAppUserModelID("PresenceDetect.App")  # own taskbar icon
    except Exception:
        pass
    if not os.path.exists(ICON_FILE):
        try:
            tray_image(TRAY_GREEN, 256).save(ICON_FILE, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (256, 256)])
        except Exception:
            pass
    if settings["startup"]:
        set_startup(True)  # (re)points the login entry at this copy of the app

    import customtkinter as ctk
    ctk.set_appearance_mode("dark")
    root = ctk.CTk()
    root.withdraw()
    requests = {"open": False, "blackout_now": False, "lock_now": False, "quit": False}
    engine = Engine(settings)
    app = App(root, engine, requests)
    icon = start_tray(engine, requests)
    log(f"started (background={args.background})")
    if not args.background:
        # customtkinter re-applies the window state it saw at creation after styling the dark
        # title bar (~200 ms), which would hide the window again - so show it after that
        root.after(400, app.show)

    state = {"keys": set(), "ui": 0.0, "tray": None}

    def tick():
        try:
            now = time.perf_counter()
            if requests["quit"] or hotkey(0x23):  # Ctrl+Alt+End
                log("quit")
                root.quit()
                return
            if requests["open"] or kernel32.WaitForSingleObject(wt.HANDLE(show_event), 0) == 0:
                requests["open"] = False
                app.show()
            b_down = hotkey(0x42)  # Ctrl+Alt+B
            if requests["blackout_now"] or (b_down and "b" not in state["keys"]):
                requests["blackout_now"] = False
                if engine.active:
                    engine.wake("manual")
                engine.black_out(now, manual=True)
            state["keys"] = {"b"} if b_down else set()
            if requests["lock_now"]:
                requests["lock_now"] = False
                engine.lock_now()
            engine.step(now)
            if now - state["ui"] > 0.25:
                state["ui"] = now
                app.update_status()
                col = TRAY_GREY if not engine.status.get("connected") else (
                    TRAY_AMBER if settings["paused"] else TRAY_GREEN)
                if col != state["tray"]:
                    state["tray"] = col
                    icon.icon = tray_image(col)
                    icon.title = APP_NAME + (" - paused" if settings["paused"] else "")
        except Exception:
            log("ERROR\n" + traceback.format_exc())
        root.after(TICK_MS, tick)

    root.after(TICK_MS, tick)
    try:
        root.mainloop()
    finally:
        engine.stop()
        icon.stop()


if __name__ == "__main__":
    sys.exit(main())
