"""
Island 2 - the user's desktop island (Windows, Python 3.10+, PySide6 + QtWebEngine).

UI   : ui.html (HTML/CSS/JS, GPU-smooth)          Data : island_core.py (study + health plan files)
Extra: Now Playing (Windows media session), CPU/RAM/battery/network (psutil), Amman weather (open-meteo)

  - docks top / left / right; drag it to any monitor and it snaps to the nearest edge
  - everything around the island is click-through
  - hides itself while a fullscreen app or game is focused on its monitor
  - reminders appear as the island expanding with buttons

Run:           pythonw island2.py
Auto-start:    python island2.py --install-startup   (replaces the old widgets in Startup)
Remove start:  python island2.py --remove-startup
Optional pip:  psutil winrt-runtime winrt-Windows.Media.Control winrt-Windows.Storage.Streams winrt-Windows.Foundation
"""

import asyncio
import base64
import datetime as dt
import importlib
import json
import math
import os
import platform
import re
import sys
import queue
import threading
import time
import urllib.request

from PySide6.QtCore import QFileSystemWatcher, QProcess, Qt, QTimer, QUrl, QObject, Slot, Signal, QPoint, QRect, QVariantAnimation, QEasingCurve, QFile, QIODevice
from PySide6.QtCore import QEvent, QMimeData, QPointF
from PySide6.QtGui import QDrag, QMouseEvent
from PySide6.QtGui import QGuiApplication, QCursor, QColor, QAction
from PySide6.QtWidgets import QApplication, QWidget, QMenu, QVBoxLayout, QSystemTrayIcon
from PySide6.QtGui import QIcon, QPixmap, QPainter, QBrush
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings, QWebEnginePage
from PySide6.QtWebChannel import QWebChannel

import island_core as core
import island_fit
import island_inbox
import island_feats as feats
import island_lyrics
import island_bridge
import island_win
import island_voice
import island_brain
import island_say
import island_study
import island_day
import island_life
import island_dev
import island_update

HERE = core.HERE
STATE_FILE = os.path.join(HERE, "island_state.json")
WATCHDOG_FILE = os.path.join(HERE, "island_watchdog.pyw")
LOG_MAX = 3 * 2**20                # island.log rotates to island.prev.log past this size
APP_ID = "abood-island2"
WIN = sys.platform == "win32"
WIN_W, WIN_H = 780, 820          # fixed transparent window; the island lives inside it
EDGE = 0                         # the island's own CSS margin (8px) keeps it off the screen edge
CITY = ("Amman", 31.95, 35.93)
try:                               # island_config.json "city": ["Name", lat, lon] (your own file: updates never touch it)
    _c = core.load_config().get("city")
    if isinstance(_c, (list, tuple)) and len(_c) == 3:
        CITY = (str(_c[0]), float(_c[1]), float(_c[2]))
    PRAYER_METHOD = int(core.load_config().get("prayer_method", 23))
except Exception:
    PRAYER_METHOD = 23

SUBJ = core.SUBJECT_EN
CAT_NAME = {"P": "Physics", "MA": "Maths", "B": "Biology", "HI": "History", "R": "Review", "W": "Workout",
            "K": "Walk", "ME": "Meal", "V": "Vitamin", "WA": "Water", "U": "Class", "T": "Trip", "E": "Break", "CL": "Claude"}

# ============================================================== Windows helpers
if WIN:
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x20
    WS_EX_LAYERED = 0x80000
    WS_EX_TOOLWINDOW = 0x80
    WS_EX_NOACTIVATE = 0x08000000
    VK_LBUTTON = 0x01
    MONITOR_DEFAULTTONEAREST = 2
    try:
        GetWindowLong = user32.GetWindowLongPtrW
        SetWindowLong = user32.SetWindowLongPtrW
    except AttributeError:  # 32-bit python
        GetWindowLong = user32.GetWindowLongW
        SetWindowLong = user32.SetWindowLongW
    GetWindowLong.restype = ctypes.c_ssize_t
    GetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int]
    SetWindowLong.restype = ctypes.c_ssize_t
    SetWindowLong.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.MonitorFromWindow.restype = wintypes.HMONITOR
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

    def left_button_down():
        return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)

    def fullscreen_monitor(own_hwnds):
        """Monitor handle that has a fullscreen foreground app on it, or None."""
        hwnd = user32.GetForegroundWindow()
        if not hwnd or int(hwnd) in own_hwnds:
            return None
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow"):
            return None
        # a normal maximized window (has a caption) overhangs the monitor by a few px - not fullscreen
        if user32.IsZoomed(hwnd) and (GetWindowLong(hwnd, -16) & 0x00C00000) == 0x00C00000:
            return None
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        mon = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            return None
        m = mi.rcMonitor
        if r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom:
            return None if snip_overlay(hwnd) else int(mon)
        return None

    SNIP_EXES = ("snippingtool.exe", "screenclippinghost.exe", "screensketch.exe")
    _snip_c = [None, False]

    def snip_overlay(hwnd):
        """Snipping Tool's capture overlay (Win+Shift+S) covers the screen for a second or two: not a fullscreen app.
        Before (nightly 18 Nov): every screenshot hid the island, closed + rebuilt the desktop widgets and restarted the
        battery reader. One window = one process, so the answer is kept for the last window asked about."""
        h = int(hwnd or 0)
        if _snip_c[0] == h:
            return _snip_c[1]
        try:
            import psutil
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            v = psutil.Process(pid.value).name().lower() in SNIP_EXES
        except Exception:
            return False
        _snip_c[0], _snip_c[1] = h, v
        return v

    def monitor_of(hwnd):
        return int(user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST))

    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    SWITCHERS = ("MultitaskingViewFrame", "XamlExplorerHostIslandWindow", "ForegroundStaging", "Shell_TrayWnd",
                 "Shell_SecondaryTrayWnd", "Windows.UI.Core.CoreWindow", "Progman", "WorkerW", "")

    def class_of(hwnd):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        return buf.value

    def cursor_monitor():
        p = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(p)):
            return 0
        return int(user32.MonitorFromPoint(p, MONITOR_DEFAULTTONEAREST) or 0)

    def screen_monitor(scr):
        """Windows monitor handle of a QScreen (Qt keeps each screen's top-left corner in real pixels)."""
        g, d = scr.geometry(), scr.devicePixelRatio()
        p = wintypes.POINT(int(g.left() + g.width() * d / 2), int(g.top() + g.height() * d / 2))
        return int(user32.MonitorFromPoint(p, MONITOR_DEFAULTTONEAREST) or 0)

    def still_fullscreen(hwnd):
        """Monitor of a window we saw fullscreen, if it is still open, shown and covering its monitor."""
        try:
            h = wintypes.HWND(hwnd)
            if not user32.IsWindow(h) or not user32.IsWindowVisible(h) or user32.IsIconic(h):
                return None
            cloaked = wintypes.DWORD(0)
            ctypes.windll.dwmapi.DwmGetWindowAttribute(h, 14, ctypes.byref(cloaked), 4)
            if cloaked.value:
                return None
            r = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            mon = user32.MonitorFromWindow(h, MONITOR_DEFAULTTONEAREST)
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if not user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                return None
            m = mi.rcMonitor
            if r.left <= m.left and r.top <= m.top and r.right >= m.right and r.bottom >= m.bottom:
                return int(mon)
        except Exception:
            pass
        return None

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    ctypes.windll.kernel32.GetTickCount.restype = ctypes.c_uint32

    gdi32 = ctypes.windll.gdi32
    gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
    user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]

    class ACCENTPOLICY(ctypes.Structure):
        _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                    ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]

    class WINCOMPATTRDATA(ctypes.Structure):
        _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]

    def set_blur(hwnd, on):
        """Windows' own blur behind the window (SetWindowCompositionAttribute, ACCENT_ENABLE_BLURBEHIND)."""
        try:
            ap = ACCENTPOLICY(3 if on else 0, 0, 0, 0)
            data = WINCOMPATTRDATA(19, ctypes.cast(ctypes.pointer(ap), ctypes.c_void_p), ctypes.sizeof(ap))
            user32.SetWindowCompositionAttribute(wintypes.HWND(hwnd), ctypes.byref(data))
        except Exception as e:
            print("glass:", e)

    def set_region(hwnd, box):
        """Clip the window to the island's rounded box (x, y, w, h, radius in window px); None = no clip."""
        try:
            if box is None:
                user32.SetWindowRgn(hwnd, None, True)
                return
            x, y, w, h, r = (int(round(v)) for v in box)
            rgn = gdi32.CreateRoundRectRgn(x, y, x + w + 1, y + h + 1, 2 * r, 2 * r)
            user32.SetWindowRgn(hwnd, rgn, True)          # Windows owns the region from here
        except Exception as e:
            print("glass region:", e)

    def idle_seconds():
        """Seconds since the last keyboard/mouse input anywhere in Windows."""
        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        if not user32.GetLastInputInfo(ctypes.byref(li)):
            return 0.0
        return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000.0
else:
    def left_button_down():
        return bool(QGuiApplication.mouseButtons() & Qt.LeftButton)

    def fullscreen_monitor(own):
        return None

    def monitor_of(hwnd):
        return 0

    def cursor_monitor():
        return 0

    def screen_monitor(scr):
        return 0

    def still_fullscreen(hwnd):
        return None

    def idle_seconds():
        return 0.0

    def set_blur(hwnd, on):
        pass

    def set_region(hwnd, box):
        pass


NO_CT = False


def set_click_through(widget, on):
    """Let mouse events fall through the window (Qt sets the right native styles itself)."""
    if NO_CT:
        return
    w = widget.windowHandle()
    if w is not None:
        w.setFlag(Qt.WindowTransparentForInput, bool(on))


def chime(kind="info"):
    try:
        import winsound
        winsound.MessageBeep({"water": winsound.MB_OK, "nag": winsound.MB_ICONEXCLAMATION}.get(
            kind, winsound.MB_ICONASTERISK))
    except Exception:
        pass


def fmt_min(m):
    m = max(0, int(m))
    if m < 60:
        return f"{m}m"
    h, r = divmod(m, 60)
    return f"{h}h {r:02d}m" if r else f"{h}h"


# ============================================================== state json for the UI
def h12(hhmm):
    """'16:05' -> '4:05 PM' (the user reads 12-hour time)."""
    try:
        h, mi = (int(x) for x in str(hhmm).split(":"))
    except (ValueError, TypeError):
        return hhmm or ""
    return f"{h % 12 or 12}:{mi:02d} {'PM' if h % 24 >= 12 else 'AM'}"


def h12_text(s):
    """'campus break 11:30–13:00' -> 'campus break 11:30 AM–1:00 PM' (plan descriptions keep 24-hour times).
    Only two-digit HH:MM, and never in Arabic text: the tawjihi lines' '2:30' are durations (timed 2h30)."""
    s = str(s or "")
    if not s or re.search(r"[\u0600-\u06ff]", s):
        return s
    return re.sub(r"(?<![\d:])(?:[01]\d|2[0-3]):[0-5]\d(?![\d:])(?!\s?[AaPp]\.?[Mm]\b)", lambda m: h12(m.group(0)), s)


def item_json(m, t, day, live):
    s, e = m.span(t, day)
    st = m.state(t, day) if live else (("done",) if t["done"] else ("x",))
    out = dict(key=t["key"], src=t["src"], cat=t["cat"], title=t["title"], desc=h12_text(t.get("desc", "")),
               time=t.get("time"), end=t.get("end"), endS=(e.strftime("%H:%M") if (e and t["src"] == "S") else None),
               dur=t.get("dur", 0), done=t["done"], block=t["block"], optional=t["optional"], state=st[0],
               movable=bool(t.get("movable")), moved=bool(t.get("moved")), ptime=t.get("plan_time"))
    if st[0] == "now":
        out.update(left=st[1], frac=round(st[2], 4))
    elif st[0] == "next":
        out.update(until=st[1])
    return out


def ram_top():
    """Log the biggest RAM users on the whole PC (working set, summed per app) - read-only, every 10 min."""
    try:
        import psutil
        tot, agg = psutil.virtual_memory(), {}
        for pr in psutil.process_iter(["name", "memory_info"]):
            try:
                n = (pr.info["name"] or "?").replace(".exe", "")
                agg[n] = agg.get(n, 0) + pr.info["memory_info"].rss
            except Exception:
                continue
        top = sorted(agg.items(), key=lambda kv: -kv[1])[:10]
        print(f"ram top: {tot.percent:.0f}% of {tot.total / 2**30:.1f} GB used · "
              + " · ".join(f"{n} {v / 2**30:.2f} GB" if v >= 2**30 else f"{n} {v / 2**20:.0f} MB" for n, v in top))
    except Exception as e:
        print("ram top:", e)


def big_countdown(m, today):
    """The one big thing to count down to: your pick (life.json "big": title + date), else the study plan's exam_day
    (titled by its "exam_name", or your pick's title). None = nothing to count down to: the island hides it."""
    life = getattr(m, "life", None)
    b = ((life.data.get("big") if life else None) or {})
    title = str(b.get("title") or "").strip()
    if b.get("date"):
        try:
            d = dt.date.fromisoformat(b["date"])
            if d >= today:
                return dict(title=title or "the big day", date=d.isoformat(), days=(d - today).days,
                            label=f"{d.strftime('%a')} {d.day} {d.strftime('%b %Y')}", src="you")
        except ValueError:
            pass
    st = m.study
    if st.ok and st.data.get("exam_day"):
        ex = st.exam_day
        if ex >= today:
            mk = st.mock_start if st.data.get("mock_start") else None
            return dict(title=title or str(st.data.get("exam_name") or "exams"), date=ex.isoformat(), days=(ex - today).days,
                        label=f"{ex.day} {ex.strftime('%b')}" + (f" · mocks from {mk.day} {mk.strftime('%b')}" if mk and mk > today else ""),
                        src="plan")
    return None


def build_state(m, offset=0):
    today = m.today()
    now = dt.datetime.now()
    cur, nxt = m.focus(now)
    d = {}
    d["focus"] = {"cur": item_json(m, cur, today, True) if cur else None,
                  "nxt": item_json(m, nxt, today, True) if nxt else None}
    d["late"] = len(m.late_items(today, now))
    try:
        d["fire"] = m.fire_streak(today)
    except Exception:
        d["fire"] = 0
    d["gym"] = m.health.gym_streak(today) if m.health.ok else 0
    ml, goal = m.water(today)
    chips = [-250] + list(m.health.water_cfg.get("chips_ml", [250, 500, 1000])) if m.health.ok else []
    d["water"] = {"ml": ml, "goal": goal, "chips": chips}
    dn, tot = m.completion(today)
    d["pct"] = int(round(100 * dn / tot)) if tot else 0
    big = big_countdown(m, today)
    d["big"] = big
    d["exam_days"] = big["days"] if big else None          # older pages read these two
    d["exam_label"] = big["label"] if big else ""
    # day view
    day = today + dt.timedelta(days=offset)
    lbl = {0: "Today", 1: "Tomorrow", -1: "Yesterday"}.get(offset, "")
    lbl = (lbl + " · " if lbl else "") + f"{core.DAY_EN[day.weekday()]} {day.day} {day.strftime('%b')}"
    vd, vt = m.completion(day)
    d["view"] = {"offset": offset, "label": lbl, "editable": day <= today,
                 "pct": int(round(100 * vd / vt)) if vt else 0,
                 "items": [item_json(m, t, day, day == today) for t in m.items(day, today)]}
    # study
    if m.study.ok:
        pace, prog = m.study.pace(today), m.study.progress()
        subs = []
        for s in [k for k in ("P", "M", "B", "H") if m.study.items(k)]:
            rem, avail = pace[s]
            dn_, tot_ = prog[s]
            und = m.study.undone(s)
            dated = bool(m.study.data.get("exam_day") or m.study.data.get("mock_start"))
            status = ("no exam date" if not dated else "on track" if rem <= avail else
                      ("tight" if rem <= avail + 1 else f"behind {rem - avail}"))
            subs.append(dict(s=s, cat=core.SUBJECT_CAT[s], name=m.study.subject_name(s), done=dn_, total=tot_, rem=rem, avail=avail,
                             status=status, next=und[0]["text"] if und else "all sessions done"))
        cp = m.study.next_checkpoint(today)
        week = []
        for k in range(7):
            dd = today + dt.timedelta(days=k)
            week.append(dict(label="Today" if k == 0 else core.DAY_EN[dd.weekday()], today=k == 0,
                             items=[dict(time=t["time"], name=m.study.subject_name(t["s"]), cat=core.SUBJECT_CAT.get(t["s"], "R"),
                                         done=t["done"]) for t in m.study.tasks_for(dd, today)]))
        try:
            tday = [item_json(m, t, today, True) for t in m.items(today, today) if t.get("src") == "S"]
        except Exception:
            tday = []
        ms = m.study.mock_start if m.study.data.get("mock_start") else m.study.exam_day
        d["study"] = dict(days_left=max(0, (ms - today).days), subjects=subs, week=week, today=tday,
                          mocks=bool(m.study.data.get("mock_start")),
                          checkpoint=dict(date=dt.date.fromisoformat(cp[0]).strftime("%a %d %b").replace(" 0", " "), text=cp[1]) if cp else None)
    else:
        d["study"] = None
    # body
    h = m.health
    if h.ok:
        tg = h.data.get("targets", {})
        targets = [["kcal", tg.get("kcal")], ["protein", f"{tg.get('protein_g')} g"], ["carbs", f"{tg.get('carbs_g')} g"],
                   ["fat", f"{tg.get('fat_g')} g"]] if tg else None
        kind = h.gym_kind(day)
        sday = day
        if not kind:
            nd = day + core.DAY
            while not h.gym_kind(nd) and nd < day + 8 * core.DAY:
                nd += core.DAY
            sday = nd
        sid = h.session_for(sday, today)
        ses = h.session(sid)
        wdone = h.workout_done(day) if kind else False
        ticked = set(h.log(day).get("ex", [])) if kind else set()
        exs = [dict(id=e["id"], name=e["name"], on=(e["id"] in ticked) or wdone,
                    meta=f"{e.get('sets', '')}  ·  RIR {e.get('rir', '')}" + (f"  ·  {e['load']}" if e.get("load") else ""))
               for e in ses.get("exercises", [])]
        vit = [item_json(m, t, day, day == today) for t in m.items(day, today) if t["cat"] == "V"]
        d["body"] = dict(targets=targets, wo_label="Workout" if kind else f"Rest day · next {core.DAY_EN[sday.weekday()]}",
                         wo_time=f"{h12(h.W.get('time', ''))} · ~{h.W.get('length_min', 45)} min", wo_name=ses.get("name", sid),
                         wo_done=wdone, wo_toggle=bool(kind) and day <= today, ex_editable=bool(kind) and day <= today,
                         exercises=exs, vitamins=vit)
    else:
        d["body"] = None
    return d


# ============================================================== media (Windows media session)
class MediaWorker(threading.Thread):
    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.loop = None
        self.ok = False
        self.last = None
        self.art_key = None
        self.art = None
        self.art_tries = 0
        self.idle = False      # True while the island is hidden over a fullscreen app: poll slower

    def run(self):
        if WIN:   # WinRT calls from a thread without an MTA get "Call was canceled by the message filter"
            try:
                ctypes.windll.ole32.CoInitializeEx(None, 0)   # 0 = COINIT_MULTITHREADED
            except Exception as e:
                print("media: CoInitializeEx failed:", e)
        try:
            from winrt.windows.media.control import \
                GlobalSystemMediaTransportControlsSessionManager as MM  # noqa
            self.MM = MM
            self.ok = True
        except Exception as e:
            print("media disabled:", e)
            return
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.wake = asyncio.Event()
        self.loop.run_until_complete(self.main())

    async def nap(self, secs):
        try:
            await asyncio.wait_for(self.wake.wait(), secs)
        except asyncio.TimeoutError:
            pass
        self.wake.clear()

    async def session(self):
        # keep one session manager instead of asking Windows for a new one every poll (reset after an error)
        if getattr(self, "mgr", None) is None:
            self.mgr = await self.MM.request_async()
        pick = getattr(self, "pick", None)
        if pick:
            for x in self.mgr.get_sessions():
                if x.source_app_user_model_id == pick:
                    return x
            self.pick = None                 # that player closed: back to Windows' choice
        return self.mgr.get_current_session()

    @staticmethod
    def app_name(aumid):
        app = (aumid or "").split("!")[0].split("\\")[-1].replace(".exe", "")
        app = {"Spotify": "Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0": "Spotify"}.get(app, app)
        if "chrome" in app.lower() or "msedge" in app.lower() or "firefox" in app.lower():
            app = "Browser"
        return app[:18]

    @staticmethod
    def ref(v):
        """Unwrap an optional WinRT value (IReference) to a plain value or None."""
        if v is None:
            return None
        return getattr(v, "value", v)

    def extras(self, s, info):
        """Shuffle / repeat state (None = this player doesn't support it) + the other players you could switch to."""
        shuf = rep = None
        try:
            c = info.controls
            if c.is_shuffle_enabled:
                shuf = bool(self.ref(info.is_shuffle_active))
            if c.is_repeat_enabled:
                rep = int(self.ref(info.auto_repeat_mode) or 0)
        except Exception:
            pass
        srcs = []
        try:
            for x in self.mgr.get_sessions():
                srcs.append(self.app_name(x.source_app_user_model_id))
        except Exception:
            pass
        return shuf, rep, (srcs if len(srcs) > 1 else None)

    async def read_art(self, props):
        try:
            from winrt.windows.storage.streams import Buffer, InputStreamOptions
            ref = props.thumbnail
            if ref is None:
                return None
            stream = await ref.open_read_async()
            size = int(stream.size)
            if not size or size > 3_000_000:
                return None
            buf = Buffer(size)
            # newer winrt returns the filled buffer as a NEW object; reading the one we passed gives b"" (no cover)
            out = await stream.read_async(buf, size, InputStreamOptions.READ_AHEAD)
            data = bytes(out if out is not None else buf)
            if len(data) < 64:
                data = bytes(buf)
            if len(data) < 64:
                print(f"art: empty cover ({len(data)} bytes), will retry")
                return None
            mime = "image/png" if data[:4] == b"\x89PNG" else "image/jpeg"
            return f"data:{mime};base64," + base64.b64encode(data).decode()
        except Exception as e:
            print("art failed:", e)
            return None

    async def main(self):
        while True:
            try:
                s = await self.session()
                if s is None:
                    out = None
                else:
                    info = s.get_playback_info()
                    tl = s.get_timeline_properties()
                    playing = int(info.playback_status) == 4
                    try:
                        can_seek = bool(info.controls.is_playback_position_enabled)
                    except Exception:
                        can_seek = True

                    def secs(v):
                        try:
                            return v.total_seconds()
                        except AttributeError:
                            try:
                                return v.duration / 10_000_000
                            except Exception:
                                return 0

                    pos, end = secs(tl.position), secs(tl.end_time)
                    try:  # position is only refreshed now and then: extrapolate while playing
                        lu = tl.last_updated_time
                        if playing and lu and lu.year > 2000:
                            pos += max(0.0, (dt.datetime.now(dt.timezone.utc) - lu).total_seconds())
                    except Exception:
                        pass
                    app = (s.source_app_user_model_id or "").split("!")[0].split("\\")[-1].replace(".exe", "")
                    app = {"Spotify": "Spotify", "SpotifyAB.SpotifyMusic_zpdnekdrzrea0": "Spotify"}.get(app, app)
                    if "chrome" in app.lower() or "msedge" in app.lower() or "firefox" in app.lower():
                        app = "Browser"
                    # Spotify sometimes refuses just this call ("canceled by the message filter"); status and
                    # position still work, so keep the last known song for the same app instead of failing the poll
                    props, err = None, None
                    cache = getattr(self, "meta_cache", None)
                    # a new track length means the cached title is probably stale: try harder before reusing it
                    tries = 8 if (cache and abs(cache[2] - end) > 1) else 3
                    for i in range(tries):
                        try:
                            props = await s.try_get_media_properties_async()
                            break
                        except Exception as e:
                            err = e
                            await asyncio.sleep(0.2)
                    if props is not None:
                        meta = (props.title or "", props.artist or "", props.album_title or "")
                        self.meta_cache = (app, meta, end)
                    else:
                        if not cache or cache[0] != app:
                            raise err
                        meta = cache[1]
                        self.props_errs = getattr(self, "props_errs", 0) + 1
                        if self.props_errs in (1, 50, 500):
                            print(f"media: song info refused by {app}, keeping last song ({self.props_errs}x)  "
                                  f"[{dt.datetime.now():%H:%M}]")
                    key = meta[:2]
                    if key != self.art_key:
                        self.art_key = key
                        self.art = None
                        self.art_tries = 0
                    # players often send the cover a moment after the title: keep retrying a few polls
                    if props is not None and self.art is None and self.art_tries < 15:
                        self.art_tries += 1
                        self.art = await self.read_art(props)
                    shuf, rep, srcs = self.extras(s, info)
                    out = dict(title=meta[0], artist=meta[1], album=meta[2],
                               app=app[:18], playing=playing, pos=round(min(pos, end) if end else pos, 1),
                               dur=round(end, 1), art=self.art, seek=can_seek, shuf=shuf, rep=rep, srcs=srcs)
                if out != self.last:
                    self.last = out
                    self.emit(out)
                self.errs = 0
            except Exception as e:
                # e.g. "Call was canceled by the message filter" while Windows is locked/busy: log once per
                # streak (not every 1.2 s), back off, and drop a stale "now playing" if it keeps failing
                self.mgr = None
                self.errs = getattr(self, "errs", 0) + 1
                if self.errs == 1 or str(e) != getattr(self, "last_err", ""):
                    print(f"media poll: {e}  [{dt.datetime.now():%H:%M}]")
                    self.last_err = str(e)
                if self.errs == 15 and self.last is not None:
                    self.last = None
                    self.emit(None)
                await self.nap(min(10.0, 1.2 * self.errs))
                continue
            await self.nap(5.0 if self.idle else 1.2)

    MEDIA_KEYS = {"toggle": 0xB3, "next": 0xB0, "prev": 0xB1}   # VK_MEDIA_PLAY_PAUSE / NEXT / PREV

    def media_key(self, what):
        if WIN:
            vk = self.MEDIA_KEYS[what]
            user32.keybd_event(vk, 0, 0, 0)
            user32.keybd_event(vk, 0, 2, 0)      # KEYEVENTF_KEYUP

    def seek(self, secs):
        """Jump to a position (seconds) in the current track."""
        if not self.ok or not self.loop:
            return

        async def go():
            try:
                s = await self.session()
                ok = s is not None and bool(await s.try_change_playback_position_async(int(max(0.0, secs) * 10_000_000)))
                if not ok:
                    print(f"media seek: refused by the player  [{dt.datetime.now():%H:%M}]")
            except Exception as e:
                self.mgr = None
                print("media seek failed:", e)
            await asyncio.sleep(0.3)
            self.wake.set()
        asyncio.run_coroutine_threadsafe(go(), self.loop)

    def mode_cmd(self, what):
        """shuffle on/off, repeat off -> all -> one -> off, or switch to the next player."""
        if not self.ok or not self.loop:
            return

        async def go():
            try:
                if what == "src":
                    if self.mgr is None:
                        self.mgr = await self.MM.request_async()
                    ids = [x.source_app_user_model_id for x in self.mgr.get_sessions()]
                    cur = await self.session()
                    now = cur.source_app_user_model_id if cur is not None else None
                    if len(ids) > 1:
                        self.pick = ids[(ids.index(now) + 1) % len(ids)] if now in ids else ids[0]
                        self.art_key = None
                else:
                    s = await self.session()
                    if s is None:
                        return
                    info = s.get_playback_info()
                    if what == "shuffle":
                        ok = await s.try_change_shuffle_active_async(not bool(self.ref(info.is_shuffle_active)))
                    else:
                        nxt = {0: 2, 2: 1, 1: 0}.get(int(self.ref(info.auto_repeat_mode) or 0), 0)
                        try:
                            from winrt.windows.media import MediaPlaybackAutoRepeatMode as RM
                            nxt = RM(nxt)
                        except Exception:
                            pass
                        ok = await s.try_change_auto_repeat_mode_async(nxt)
                    if not ok:
                        print(f"media {what}: refused by the player")
            except Exception as e:
                self.mgr = None
                print(f"media {what} failed:", e)
            await asyncio.sleep(0.3)
            self.wake.set()

        fut = asyncio.run_coroutine_threadsafe(go(), self.loop)
        fut.add_done_callback(lambda f: f.exception() and print("media mode:", f.exception()))

    def command(self, what):
        if not self.ok or not self.loop:
            self.media_key(what)
            return

        async def go():
            ok, err = False, None
            for attempt in range(3):
                try:
                    s = await self.session()
                    if s is None:
                        break
                    ok = bool(await {"toggle": s.try_toggle_play_pause_async, "next": s.try_skip_next_async,
                                     "prev": s.try_skip_previous_async}[what]())
                    if ok:
                        break
                except Exception as e:
                    err = e
                    self.mgr = None
                    await asyncio.sleep(0.15)
            if not ok:
                print(f"media {what}: WinRT {'failed: ' + str(err) if err else 'refused'} -> media key")
                self.media_key(what)
            await asyncio.sleep(0.35)
            self.wake.set()                      # refresh now instead of waiting for the next poll

        fut = asyncio.run_coroutine_threadsafe(go(), self.loop)
        fut.add_done_callback(lambda f: f.exception() and print("media command:", f.exception()))


# ============================================================== per-app sound (Windows Core Audio via pycaw)
class AudioWorker(threading.Thread):
    """Master + per-app volume. Polls only while the mixer is on screen; all COM calls stay on this thread."""
    NAMES = {"msedge": "Edge", "chrome": "Chrome", "firefox": "Firefox", "opera": "Opera", "brave": "Brave",
             "spotify": "Spotify", "discord": "Discord", "steam": "Steam", "steamwebhelper": "Steam",
             "javaw": "Java", "java": "Java", "vlc": "VLC", "obs64": "OBS", "ms-teams": "Teams", "teams": "Teams",
             "whatsapp": "WhatsApp", "zoom": "Zoom", "medal": "Medal", "applemusic": "Apple Music"}
    SKIP = {"qtwebengineprocess", "pythonw", "python", "audiodg"}
    MAX_APPS = 7

    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.q = queue.Queue()
        self.watching = False
        self.last = None
        self.heard = {}
        self.sav = {}
        self.ep, self.ep_at, self.device = None, 0.0, "Volume"
        self.snap_at = 0.0
        self.err = None

    # called from the UI thread
    def watch(self, on):
        self.q.put(("watch", bool(on)))

    def set(self, key, v):
        self.q.put(("vol", (str(key), float(v))))

    def set_device(self, dev_id):
        self.q.put(("dev", str(dev_id)))

    def mute(self, key, m):
        self.q.put(("mute", (str(key), m)))

    # worker thread
    def run(self):
        sys.coinit_flags = 0          # comtypes initialises COM on import: make it MTA (no message pump needed)
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation, IAudioEndpointVolume
        except Exception as e:
            print("sound disabled:", e)
            self.emit(dict(ok=False, why="Sound control needs one install: pip install pycaw"))
            return
        self.AU, self.IMeter, self.IEp = AudioUtilities, IAudioMeterInformation, IAudioEndpointVolume
        while True:
            try:
                cmd, arg = self.q.get(timeout=0.25 if self.watching else 4)        # 4 s: the device list stays fresh
            except queue.Empty:
                cmd, arg = None, None
            try:
                if cmd == "watch":
                    self.watching = arg
                    self.last = None
                elif cmd == "vol":
                    self.apply_vol(*arg)
                elif cmd == "mute":
                    self.apply_mute(*arg)
                elif cmd == "dev":
                    self.set_default(arg)
                    self.last = None
                    self.dev_poll(True)
                self.dev_poll()
                if self.watching and self.q.empty() and time.time() - self.snap_at > 0.2:
                    snap = self.snapshot()
                    if snap != self.last:
                        self.last = snap
                        self.emit(snap)
                self.err = None
            except Exception as e:
                self.ep = None
                if str(e) != self.err:
                    self.err = str(e)
                    print("sound:", e)

    def endpoint(self):
        if self.ep is None or time.time() - self.ep_at > 3:     # re-read: the default device can change
            dev = self.AU.GetSpeakers()
            ep = getattr(dev, "EndpointVolume", None)
            if ep is None:                                    # older pycaw
                from comtypes import CLSCTX_ALL
                from ctypes import cast, POINTER
                ep = cast(dev.Activate(self.IEp._iid_, CLSCTX_ALL, None), POINTER(self.IEp))
            name = str(getattr(dev, "FriendlyName", "") or "")
            self.device = name.split(" (")[0].strip() or "Volume"
            self.ep, self.ep_at = ep, time.time()
        return self.ep

    def snapshot(self):
        self.snap_at = now = time.time()
        ep = self.endpoint()
        apps, sav = {}, {}
        for se in self.AU.GetAllSessions():
            try:
                if se.State == 2:                             # expired
                    continue
                pr = se.Process
                if pr is None or se.ProcessId == 0:
                    key, name, exe = "system", "System sounds", None
                else:
                    pn = pr.name()
                    stem = pn[:-4] if pn.lower().endswith(".exe") else pn
                    key = stem.lower()
                    if key in self.SKIP:
                        continue
                    name = self.NAMES.get(key) or stem[:1].upper() + stem[1:]
                    try:
                        exe = pr.exe()
                    except Exception:
                        exe = None
                vol = se.SimpleAudioVolume
                try:
                    if float(se._ctl.QueryInterface(self.IMeter).GetPeakValue()) > 0.002:
                        self.heard[key] = now
                except Exception:
                    pass
                if key not in apps:
                    apps[key] = dict(key=key, name=name[:22], exe=exe, v=round(float(vol.GetMasterVolume()), 3),
                                     mute=bool(vol.GetMute()))
                sav.setdefault(key, []).append(vol)
            except Exception:
                continue
        self.sav = sav
        for a in apps.values():
            a["live"] = now - self.heard.get(a["key"], 0) < 2.5
        recent = lambda a: 0 if now - self.heard.get(a["key"], 0) < 300 else 1     # noqa: E731
        order = sorted(apps.values(), key=lambda a: (recent(a), a["key"] == "system", a["name"].lower()))
        return dict(ok=True, device=self.device, v=round(float(ep.GetMasterVolumeLevelScalar()), 3),
                    mute=bool(ep.GetMute()), apps=order[:self.MAX_APPS])

    # ---------------------------------------------------------------- output / input devices (r22)
    def devices(self):
        """Active output + input devices and which one is the default: {"out": [...], "in": [...]} (each {id, name, sub, def})."""
        AU = self.AU
        try:
            enum = AU.GetDeviceEnumerator()
        except Exception:
            import comtypes
            try:
                from pycaw.pycaw import IMMDeviceEnumerator
            except ImportError:
                from pycaw.api.mmdeviceapi import IMMDeviceEnumerator
            from pycaw.constants import CLSID_MMDeviceEnumerator
            enum = comtypes.CoCreateInstance(CLSID_MMDeviceEnumerator, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
        out = dict(ok=True)
        for key, flow in (("out", 0), ("in", 1)):              # eRender, eCapture
            try:
                did = enum.GetDefaultAudioEndpoint(flow, 1).GetId()    # eMultimedia
            except Exception:
                did = None
            lst = []
            coll = enum.EnumAudioEndpoints(flow, 1)               # DEVICE_STATE_ACTIVE
            for i in range(coll.GetCount()):
                d = coll.Item(i)
                i_d = d.GetId()
                try:
                    name = str(AU.CreateDevice(d).FriendlyName or "")
                except Exception:
                    name = ""
                main, _, sub = (name or "Device").partition(" (")
                main, sub = main.strip(), re.sub(r"^\d+-\s*", "", sub[:-1] if sub.endswith(")") else sub).strip()
                if sub and main.lower() in ("speakers", "headphones", "headset", "headset earphone", "microphone", "headset microphone",
                                            "microphone array", "line", "line in", "digital audio (s/pdif)", "speaker"):
                    main, sub = sub, main                     # "Speakers (Fantech MH82)" -> "Fantech MH82" · Speakers
                lst.append(dict(id=i_d, name=main[:28], sub=sub[:34], **{"def": i_d == did}))
            out[key] = lst
        return out

    def set_default(self, dev_id):
        """Make this device Windows' default for every role (console, multimedia, communications)."""
        import comtypes
        from comtypes import GUID, IUnknown, STDMETHOD, HRESULT
        from ctypes import c_wchar_p, c_int
        if not hasattr(AudioWorker, "IPolicy"):
            class IPolicyConfig(IUnknown):
                _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
                _methods_ = [STDMETHOD(HRESULT, n, []) for n in (
                    "GetMixFormat", "GetDeviceFormat", "ResetDeviceFormat", "SetDeviceFormat", "GetProcessingPeriod",
                    "SetProcessingPeriod", "GetShareMode", "SetShareMode", "GetPropertyValue", "SetPropertyValue")] + [
                    STDMETHOD(HRESULT, "SetDefaultEndpoint", [c_wchar_p, c_int]),
                    STDMETHOD(HRESULT, "SetEndpointVisibility", [c_wchar_p, c_int])]
            AudioWorker.IPolicy = IPolicyConfig
        pc = comtypes.CoCreateInstance(GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}"), AudioWorker.IPolicy, comtypes.CLSCTX_ALL)
        for role in (0, 1, 2):
            pc.SetDefaultEndpoint(str(dev_id), role)
        self.ep = None                                        # the master slider follows the new device
        print("sound: default device set")

    def dev_poll(self, force=False):
        if not force and time.time() - getattr(self, "dev_at", 0) < 4:
            return
        self.dev_at = time.time()
        try:
            dv = self.devices()
        except Exception as e:
            dv = dict(ok=False)
            if str(e) != getattr(self, "dev_err", None):
                self.dev_err = str(e)
                print("sound devices:", e)
        if dv != getattr(self, "dev_last", None):
            self.dev_last = dv
            self.emit(dict(adev=dv))

    def apply_vol(self, key, v):
        v = max(0.0, min(1.0, v))
        if key == "master":
            ep = self.endpoint()
            ep.SetMasterVolumeLevelScalar(v, None)
            if v > 0 and ep.GetMute():
                ep.SetMute(0, None)
            return
        if key not in self.sav:
            self.snapshot()
        for vol in self.sav.get(key, []):
            vol.SetMasterVolume(v, None)
            if v > 0 and vol.GetMute():
                vol.SetMute(0, None)

    def apply_mute(self, key, m):
        if key == "master":
            ep = self.endpoint()
            ep.SetMute(int(not ep.GetMute() if m is None else bool(m)), None)
            return
        if key not in self.sav:
            self.snapshot()
        vols = self.sav.get(key, [])
        if m is None and vols:
            m = not vols[0].GetMute()
        for vol in vols:
            vol.SetMute(int(bool(m)), None)


class MeterWorker(threading.Thread):
    """Peak level of the speakers (0..1), ~20x a second while wanted: drives the music bars."""

    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.on = threading.Event()
        self.meter, self.at, self.err = None, 0.0, None
        self.dead = False

    def stop(self):
        """At quit: no more emits into a Host that is being torn down."""
        self.dead = True
        self.on.clear()

    def want(self, on):
        if on:
            self.on.set()
        else:
            self.on.clear()

    def run(self):
        self.on.wait()
        sys.coinit_flags = 0              # same apartment (MTA) as the mixer thread
        try:
            if WIN:
                ctypes.windll.ole32.CoInitializeEx(None, 0)
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            from comtypes import CLSCTX_ALL
        except Exception as e:
            print("music bars: no level meter (pip install pycaw):", e)
            return
        last = None
        while True:
            self.on.wait()
            try:
                if self.meter is None or time.time() - self.at > 10:      # re-read: the default device can change
                    dev = AudioUtilities.GetSpeakers()
                    raw = getattr(dev, "_dev", dev)
                    self.meter = raw.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None).QueryInterface(IAudioMeterInformation)
                    self.at = time.time()
                v = round(float(self.meter.GetPeakValue()), 2)
                if (v != last or v > 0) and not self.dead:
                    last = v
                    self.emit(v)
                self.err = None
            except Exception as e:
                self.meter = None
                if str(e) != self.err:
                    self.err = str(e)
                    print("music bars:", e)
                time.sleep(2)
            time.sleep(0.05)


# ============================================================== Windows notifications (Claude replies, requests)
class NotifyWorker(threading.Thread):
    """Polls the Windows notification center and passes on new toasts from the chosen apps (default: Claude)."""
    ASK = ("would like", "permission", "approve", "needs your", "waiting for", "access request", "allow ")
    DONE = ("finished", "replied", "complete", "is done", "ready", "responded")

    def __init__(self, emit, apps):
        super().__init__(daemon=True)
        self.emit = emit
        self.apps_fn = apps if callable(apps) else (lambda a=list(apps): a)
        self.seen = set()
        self.err = None

    def run(self):
        if WIN:
            try:
                ctypes.windll.ole32.CoInitializeEx(None, 0)
            except Exception:
                pass
        try:
            from winrt.windows.ui.notifications.management import UserNotificationListener  # noqa
            from winrt.windows.ui.notifications import NotificationKinds, KnownNotificationBindings  # noqa
        except Exception as e:
            print("notifications disabled:", e)
            return
        self.L, self.K, self.B = UserNotificationListener, NotificationKinds, KnownNotificationBindings
        asyncio.run(self.main())

    def app_of(self, n):
        try:
            return str(n.app_info.display_info.display_name or "")
        except Exception:
            return ""                       # needs winrt-Windows.ApplicationModel; we fall back to the text

    def texts(self, n):
        try:
            b = n.notification.visual.get_binding(self.B.toast_generic)
            return [str(t.text) for t in b.get_text_elements()] if b else []
        except Exception:
            return []

    @property
    def apps(self):
        return [a.lower() for a in self.apps_fn()]

    def wanted(self, app, texts):
        a = app.lower()
        if a:
            return any(x in a for x in self.apps)
        joined = " ".join(texts).lower()
        return "claude" in joined

    def kind(self, texts):
        t = " ".join(texts).lower()
        if any(w in t for w in self.ASK):
            return "ask"
        if any(w in t for w in self.DONE):
            return "done"
        return "info"

    async def main(self):
        lis = self.L.current
        st = await lis.request_access_async()
        if int(st) != 1:
            print("notifications: Windows denied access (Settings > Privacy > Notifications)")
            return
        print("notifications: watching", ", ".join(self.apps))
        first = True
        while True:
            try:
                ns = await lis.get_notifications_async(self.K.TOAST)
                ids = set()
                for n in ns:
                    nid = int(n.id)
                    ids.add(nid)
                    if nid in self.seen:
                        continue
                    self.seen.add(nid)
                    if first:                 # don't replay what was already there at start-up
                        continue
                    app, texts = self.app_of(n), self.texts(n)
                    if texts and self.wanted(app, texts):
                        sig, kind = ((app or "?").lower(), " | ".join(texts)[:300]), self.kind(texts)
                        rep = self.__dict__.setdefault("rep", {})
                        if kind == "info" and "claude" not in sig[0] and time.time() - rep.get(sig, 0) < 3600:
                            print(f"notif: [{app or '?'}] {texts[0][:40]} -> same as earlier, not shown")
                            continue
                        rep[sig] = time.time()
                        if len(rep) > 200:
                            rep.clear()
                        print(f"notif: [{app or '?'}] {texts[0][:40]} -> shown")
                        self.emit(dict(app=app or self.apps[0].capitalize(), texts=texts, kind=self.kind(texts)))
                    else:                     # log each ignored app once an hour (Snipping Tool posts one per screenshot)
                        ign = self.__dict__.setdefault("ign", {})
                        if time.time() - ign.get((app or "?").lower(), 0) > 3600:
                            ign[(app or "?").lower()] = time.time()
                            print(f"notif: [{app or '?'}] ignored (not logged again for an hour)")
                self.seen &= ids
                first = False
                self.err = None
            except Exception as e:
                if str(e) != self.err:
                    self.err = str(e)
                    print("notifications:", e)
            await asyncio.sleep(2)


def foreground_exe():
    """Lower-case exe name of the foreground window (Windows), or ''."""
    if not WIN:
        return ""
    try:
        import psutil
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
        return psutil.Process(pid.value).name().lower()
    except Exception:
        return ""


def focus_app(exe_name):
    """Bring the first visible window of exe_name to the front. True if found."""
    if not WIN:
        return False
    try:
        import psutil
        pids = {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower() == exe_name}
    except Exception:
        return False
    found = []
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
            return False
        return True
    user32.EnumWindows(EnumProc(cb), 0)
    if not found:
        return False
    h = found[0]
    if user32.IsIconic(h):
        user32.ShowWindow(h, 9)          # SW_RESTORE
    user32.SetForegroundWindow(h)
    return True


# ============================================================== system + weather
class SysStats:
    def __init__(self):
        try:
            import psutil
            self.ps = psutil
            psutil.cpu_percent(None)
            self.net0 = psutil.net_io_counters()
            self.t0 = time.time()
        except Exception as e:
            print("system stats disabled:", e)
            self.ps = None
        self.cpu_h, self.ram_h, self.net_h = [], [], []

    @staticmethod
    def rate(b):
        return f"{b / 1e6:.1f} MB/s" if b >= 1e6 else f"{b / 1e3:.0f} KB/s"

    def sample(self):
        if not self.ps:
            return None
        p = self.ps
        cpu = p.cpu_percent(None)
        vm = p.virtual_memory()
        net = p.net_io_counters()
        t = time.time()
        dtt = max(0.5, t - self.t0)
        down = (net.bytes_recv - self.net0.bytes_recv) / dtt
        up = (net.bytes_sent - self.net0.bytes_sent) / dtt
        self.net0, self.t0 = net, t
        for arr, v in ((self.cpu_h, cpu), (self.ram_h, vm.percent), (self.net_h, min(100, down / 2e6 * 100))):
            arr.append(round(v, 1))
            del arr[:-40]
        out = dict(cpu=cpu, ram=vm.percent, cpu_h=self.cpu_h, ram_h=self.ram_h, net_h=self.net_h,
                   ram_used=f"{vm.used / 2**30:.1f}", ram_total=f"{vm.total / 2**30:.0f}", down=self.rate(down),
                   up=self.rate(up), host=platform.node(), cores=p.cpu_count(), bat=None)
        try:
            b = p.sensors_battery()
            if b:
                out["bat"] = b.percent
                out["plugged"] = bool(b.power_plugged)
                if not b.power_plugged and b.secsleft and b.secsleft > 0 and b.secsleft != p.POWER_TIME_UNLIMITED:
                    out["bat_left"] = fmt_min(b.secsleft / 60) + " left"
        except Exception:
            pass
        return out


WX = {0: ("☀️", "Clear"), 1: ("🌤️", "Mostly clear"), 2: ("⛅", "Partly cloudy"), 3: ("☁️", "Overcast"),
      45: ("🌫️", "Fog"), 48: ("🌫️", "Fog"), 51: ("🌦️", "Drizzle"), 53: ("🌦️", "Drizzle"), 55: ("🌦️", "Drizzle"),
      61: ("🌧️", "Rain"), 63: ("🌧️", "Rain"), 65: ("🌧️", "Heavy rain"), 71: ("🌨️", "Snow"), 73: ("🌨️", "Snow"),
      75: ("❄️", "Heavy snow"), 80: ("🌦️", "Showers"), 81: ("🌧️", "Showers"), 82: ("⛈️", "Violent showers"),
      95: ("⛈️", "Thunderstorm"), 96: ("⛈️", "Thunderstorm"), 99: ("⛈️", "Thunderstorm")}


def fetch_weather():
    city, lat, lon = CITY
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&current=temperature_2m,weather_code,is_day&daily=temperature_2m_max,temperature_2m_min&timezone=auto")
    with urllib.request.urlopen(url, timeout=10) as r:
        j = json.load(r)
    code = int(j["current"]["weather_code"])
    icon, desc = WX.get(code, ("🌡️", "—"))
    if code in (0, 1) and not j["current"].get("is_day", 1):
        icon = "🌙"
    out = dict(t=j["current"]["temperature_2m"], icon=icon, desc=desc, city=city,
               max=j["daily"]["temperature_2m_max"][0], min=j["daily"]["temperature_2m_min"][0])
    try:
        out.update(weather_more(lat, lon))
    except Exception as e:
        print("weather extras:", e)
    return out


def weather_more(lat, lon):
    """Next 12 hours (temp, rain %, icon), sunrise/sunset, the first likely rain, dust."""
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
           "&hourly=temperature_2m,precipitation_probability,weather_code&daily=sunrise,sunset&forecast_days=2&timezone=auto")
    with urllib.request.urlopen(url, timeout=10) as r:
        j = json.load(r)
    h = j["hourly"]
    now = dt.datetime.now().strftime("%Y-%m-%dT%H:00")
    i0 = next((i for i, t in enumerate(h["time"]) if t >= now), 0)
    hours, rain = [], None
    for k, i in enumerate(range(i0, min(i0 + 12, len(h["time"])))):
        t = dt.datetime.fromisoformat(h["time"][i])
        p = h["precipitation_probability"][i] or 0
        code = int(h["weather_code"][i] or 0)
        ic = WX.get(code, ("🌡️", ""))[0]
        if code in (0, 1) and (t.hour >= 18 or t.hour < 6):
            ic = "🌙"
        lbl = t.strftime("%I %p").lstrip("0")
        hours.append(dict(h=lbl, t=round(h["temperature_2m"][i]), p=p, icon=ic))
        if rain is None and p >= 60:
            rain = dict(at=lbl, p=p, in_h=k)
    more = dict(hours=hours, rain=rain,
                sunrise=dt.datetime.fromisoformat(j["daily"]["sunrise"][0]).strftime("%I:%M %p").lstrip("0"),
                sunset=dt.datetime.fromisoformat(j["daily"]["sunset"][0]).strftime("%I:%M %p").lstrip("0"))
    try:
        url = (f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}"
               "&hourly=dust&forecast_days=1&timezone=auto")
        with urllib.request.urlopen(url, timeout=10) as r:
            a = json.load(r)
        vals = [v for v in a["hourly"]["dust"] if v is not None]
        if vals:
            more["dust"] = dict(max=max(vals), now=vals[min(len(vals) - 1, dt.datetime.now().hour)])
    except Exception as e:
        print("dust:", e)
    return more


# ============================================================== bridge
class Bridge(QObject):
    def __init__(self, host):
        super().__init__()
        self.host = host

    @Slot(str)
    def action(self, payload):
        try:
            j = json.loads(payload)
            self.host.on_action(j.get("name"), j.get("arg"))
        except Exception as e:
            print("action error:", e)


class Page(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, msg, line, src):
        print(f"[js] {msg} ({line})")

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        # the island only ever shows ui.html: a dropped file or link must not replace it
        if is_main_frame and not (url.isLocalFile() and url.fileName() == "ui.html"):
            print(f"blocked navigation to {url.toString()[:80]}")
            return False
        return super().acceptNavigationRequest(url, nav_type, is_main_frame)


class DashBridge(QObject):
    """The dashboard page's way back: only the things you can tap there (see DASH_SEND in ui.html)."""
    def __init__(self, host):
        super().__init__()
        self.host = host

    @Slot(str)
    def action(self, payload):
        try:
            j = json.loads(payload)
            self.host.on_dash_action(j.get("name"), j.get("arg"))
        except Exception as e:
            print("dash action error:", e)


DASH_FNS = {"data", "media", "lyrics", "spos", "spstate", "sys", "sysx", "wx", "settings", "launch", "timers", "privacy",
            "net", "fit", "tick"}
# the deck (phone / tablet, island_dev.LanServer) gets those plus the mixer, mic, prayer, deadlines and its buttons
DECK_FNS = DASH_FNS | {"sound", "priv", "prayer", "life", "micstate", "decktiles", "pomo", "bri", "cards", "game", "decknote", "notedone", "deckclips", "spsearch", "deckscenes", "spdevices", "splib", "sptracks", "spnow", "spupdate", "deckver"}
# what a tap on the deck may do on the PC (nothing that presses keys in a game)
DECK_DO = {"toggle", "ex", "water", "launch", "media_toggle", "media_next", "media_prev", "media_seek", "media_shuffle",
           "media_repeat", "media_like", "media_src", "vol", "mute", "mic_toggle", "copy_today", "timer_cancel"}


_QWC = None


def bridge_js():
    """qwebchannel.js + __connect(): on Windows the page can't load it from qrc:// itself, so it's injected after load."""
    global _QWC
    if _QWC is None:
        f = QFile(":/qtwebchannel/qwebchannel.js")
        _QWC = ""
        if f.open(QIODevice.ReadOnly):
            _QWC = bytes(f.readAll()).decode("utf-8", "replace")
            f.close()
    return (_QWC + "\n;" if _QWC else "") + "window.__connect ? window.__connect() : 'no __connect'"


class DashWin(QWidget):
    """Dashboard: ui.html#dash in its own plain window filling a spare screen (plan, music + lyrics, weather, PC)."""
    def __init__(self, host, scr):
        super().__init__(None)
        self.host, self.ready = host, False
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
        self.setWindowTitle("Island dashboard")
        self.setStyleSheet("background:#07080b")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.page = Page(self.view)
        self.view.setPage(self.page)
        self.page.setBackgroundColor(QColor("#07080b"))
        st = self.page.settings()
        st.setAttribute(QWebEngineSettings.ShowScrollBars, False)
        st.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        self.view.setContextMenuPolicy(Qt.NoContextMenu)
        lay.addWidget(self.view)
        self.dbridge = DashBridge(host)
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("bridge", self.dbridge)
        self.page.setWebChannel(self.channel)
        self.tries = 0
        self.page.loadFinished.connect(self.on_loaded)
        url = QUrl.fromLocalFile(os.path.join(HERE, "ui.html"))
        url.setFragment("dash")
        self.view.load(url)
        self.scr = scr
        self.create()                         # put the window on that screen first (its own scaling), then size it
        if self.windowHandle() is not None:
            self.windowHandle().setScreen(scr)
        self.setGeometry(scr.availableGeometry())
        self.show()
        self.front()
        # once Windows has applied that screen's scaling: fill it again and come to the front again
        QTimer.singleShot(450, self.refit)
        QTimer.singleShot(2500, self.report)

    def on_loaded(self, ok):
        # same as the island itself: without this the board stayed an empty dark screen on Windows
        self.page.runJavaScript(bridge_js(), lambda r: print("dashboard: bridge connect ->", r))
        QTimer.singleShot(5000, self.check)

    def check(self):
        try:
            if not self.ready and self.tries < 3:
                self.tries += 1
                print("dashboard: not connected yet, trying again")
                self.on_loaded(True)
        except RuntimeError:
            pass                              # closed meanwhile

    def report(self):
        try:
            print(f"dashboard: window {self.geometry().getRect()} on {self.screen().name()} visible={self.isVisible()} "
                  f"page ready={self.ready}")
        except RuntimeError:
            pass

    def refit(self):
        try:
            self.setGeometry(self.scr.availableGeometry())
            self.front()
        except RuntimeError:
            pass                              # closed meanwhile

    def front(self):
        """Above the other windows on that screen now (it isn't always-on-top: click another app there and it goes behind)."""
        self.raise_()
        self.activateWindow()
        if WIN:
            try:
                h = ctypes.c_void_p(int(self.winId()))
                f = 0x0001 | 0x0002 | 0x0040                     # NOSIZE | NOMOVE | SHOWWINDOW
                user32.SetWindowPos(h, ctypes.c_void_p(-1), 0, 0, 0, 0, f)   # topmost for a moment...
                user32.SetWindowPos(h, ctypes.c_void_p(-2), 0, 0, 0, 0, f)   # ...then normal again, but in front
                user32.SetForegroundWindow(h)
            except Exception as e:
                print("dashboard front:", e)

    def js(self, fn, a):
        if self.ready:
            self.page.runJavaScript(f"window.island && window.island.{fn}({a})")


class DeskBridge(QObject):
    """The desktop widgets' way back (DESK_SEND in ui.html)."""
    def __init__(self, host):
        super().__init__()
        self.host = host

    @Slot(str)
    def action(self, payload):
        try:
            j = json.loads(payload)
            self.host.on_desk_action(j.get("name"), j.get("arg"))
        except Exception as e:
            print("desk action error:", e)


DESK_FNS = DASH_FNS | {"life", "cards", "prayer", "devbat", "screen", "launch", "study", "taps", "wall", "adev"}


class DeskWin(QWidget):
    """Desktop widgets: ui.html#desk in a see-through window on the wallpaper. Under every other window, not in the
    taskbar or Alt+Tab, never takes the focus; it stays when you press Win+D. It sizes itself to the board."""
    def __init__(self, host):
        super().__init__(None)
        self.host, self.ready, self.tries = host, False, 0
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnBottomHint | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("Island desktop widgets")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.page = Page(self.view)
        self.view.setPage(self.page)
        self.page.setBackgroundColor(Qt.transparent)
        self.view.setStyleSheet("background: transparent")
        self.view.setAttribute(Qt.WA_TranslucentBackground)
        st = self.page.settings()
        st.setAttribute(QWebEngineSettings.ShowScrollBars, False)
        st.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        self.view.setContextMenuPolicy(Qt.NoContextMenu)
        lay.addWidget(self.view)
        self.bridge2 = DeskBridge(host)
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("bridge", self.bridge2)
        self.page.setWebChannel(self.channel)
        self.page.loadFinished.connect(self.on_loaded)
        url = QUrl.fromLocalFile(os.path.join(HERE, "ui.html"))
        url.setFragment("desk")
        self.view.load(url)
        self.box = (340, 340, "tr")
        self.place()
        self.show()
        self.pin()

    def on_loaded(self, ok):
        self.page.runJavaScript(bridge_js(), lambda r: None)
        QTimer.singleShot(5000, self.check)

    def check(self):
        try:
            if not self.ready and self.tries < 3:
                self.tries += 1
                self.on_loaded(True)
        except RuntimeError:
            pass

    def place(self):
        """Into its corner of the main screen's free area (inside the taskbar), sized to the board."""
        try:
            w, h, corner = self.box
            g = QGuiApplication.primaryScreen().availableGeometry()
            m = 22
            w, h = min(int(w), g.width() - 2 * m), min(int(h), g.height() - 2 * m)
            x = g.left() + m if corner in ("tl", "bl") else g.right() - m - w
            y = g.top() + m if corner in ("tl", "tr") else g.bottom() - m - h
            self.setGeometry(x, y, w, h)
        except RuntimeError:
            pass

    def peek(self, on):
        """Over every window for a moment (topmost), then back under them on the desktop."""
        self.peeking = on
        if WIN:
            try:
                h = ctypes.c_void_p(int(self.winId()))
                if on:
                    user32.SetWindowPos(h, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)   # TOPMOST, show
                else:
                    user32.SetWindowPos(h, ctypes.c_void_p(-2), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)            # not topmost
                    user32.SetWindowPos(h, ctypes.c_void_p(1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)             # bottom
            except Exception as e:
                print("desk peek:", e)
        if self.ready:
            self.page.runJavaScript(f"window.island && island.deskpeek && island.deskpeek({'true' if on else 'false'})")

    def pin(self):
        """Owned by the desktop (Progman) so Win+D / Show desktop leaves it there; bottom of the stack; no focus."""
        if not WIN:
            return
        try:
            hwnd = int(self.winId())
            user32.FindWindowW.restype = wintypes.HWND
            prog = user32.FindWindowW("Progman", None)
            if prog:
                SetWindowLong(hwnd, -8, int(prog))                                          # GWLP_HWNDPARENT = owner
            SetWindowLong(hwnd, -20, GetWindowLong(hwnd, -20) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
            if not getattr(self, "peeking", False):
                user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)   # HWND_BOTTOM, no move/size/activate
            if not getattr(self, "_pinned", False):
                self._pinned = True
                print(f"desk: pinned to the desktop (owner {int(prog or 0):#x})")
        except Exception as e:
            print("desk pin:", e)

    def js(self, fn, a):
        if self.ready and not getattr(self, "paused", False):
            self.calls = getattr(self, "calls", 0) + 1
            self.page.runJavaScript(f"window.island && window.island.{fn}({a})")


class PopBridge(QObject):
    """The popped-out player's way back (POP_SEND in ui.html)."""
    def __init__(self, host):
        super().__init__()
        self.host = host

    @Slot(str)
    def action(self, payload):
        try:
            j = json.loads(payload)
            self.host.on_pop_action(j.get("name"), j.get("arg"))
        except Exception as e:
            print("player action error:", e)


POP_FNS = {"media", "lyrics", "spos", "spstate", "lvl", "settings", "away"}


class PopWin(QWidget):
    """The music player popped out of the island (r41): ui.html#player in its own see-through window that stays on top,
    drag it anywhere, resize it from the corner. Not in the taskbar. Back into the island with its button."""
    M = 14                                    # the page leaves this margin for the shadow (PWM in ui.html)

    GAP = 8                                   # from the screen edge, like the island

    def __init__(self, host, geo, dock="top", anchor=None):
        super().__init__(None)
        self.host, self.ready, self.tries, self.fs_hidden = host, False, 0, False
        self.dock, self.anchor, self.anim, self.watch_t = dock, anchor, None, None
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)      # r49: like the island, clicking it never takes the focus
        self.setWindowTitle("Island player")
        self.press_at, self.dragging, self.grab, self.rz = None, False, QPoint(), None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.page = Page(self.view)
        self.view.setPage(self.page)
        self.page.setBackgroundColor(Qt.transparent)
        self.view.setStyleSheet("background: transparent")
        self.view.setAttribute(Qt.WA_TranslucentBackground)
        st = self.page.settings()
        st.setAttribute(QWebEngineSettings.ShowScrollBars, False)
        st.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        self.view.setContextMenuPolicy(Qt.NoContextMenu)
        lay.addWidget(self.view)
        self.pbridge = PopBridge(host)
        self.channel = QWebChannel(self.page)
        self.channel.registerObject("bridge", self.pbridge)
        self.page.setWebChannel(self.channel)
        self.page.loadFinished.connect(self.on_loaded)
        z = host.zoom
        self.setMinimumSize(int((300 + 2 * self.M) * z), int((330 + 2 * self.M) * z))
        self.setGeometry(*geo)
        self.save_t = QTimer(self)
        self.save_t.setSingleShot(True)
        self.save_t.timeout.connect(host.pop_save)
        self.drag_t = None
        url = QUrl.fromLocalFile(os.path.join(HERE, "ui.html"))
        url.setFragment("player")
        self.view.load(url)                   # shown once the page says it's ready (no empty box first)

    def on_loaded(self, ok):
        self.view.setZoomFactor(self.host.zoom)
        self.page.runJavaScript(bridge_js(), lambda r: None)
        QTimer.singleShot(5000, self.check)

    def check(self):
        try:
            if not self.ready and self.tries < 3:
                self.tries += 1
                self.on_loaded(True)
        except RuntimeError:
            pass

    def moveEvent(self, e):
        self.save_t.start(700)
        super().moveEvent(e)

    def resizeEvent(self, e):
        self.save_t.start(700)
        super().resizeEvent(e)

    def front(self):
        """Up above the other windows again, without taking the focus."""
        if self.fs_hidden:
            return
        self.show()
        self.raise_()
        if WIN:
            try:
                user32.SetWindowPos(ctypes.c_void_p(int(self.winId())), ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)
            except Exception as e:
                print("player front:", e)

    def _btn_down(self):
        if WIN:
            return bool(user32.GetAsyncKeyState(0x01) & 0x8000)
        return bool(QGuiApplication.mouseButtons() & Qt.LeftButton)

    def no_activate(self):
        """WS_EX_NOACTIVATE: a click goes to the player without making it the active window (the game / app keeps focus)."""
        if not WIN:
            return
        try:
            hwnd = int(self.winId())
            SetWindowLong(hwnd, -20, GetWindowLong(hwnd, -20) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        except Exception as e:
            print("player no-activate:", e)

    def drag(self):
        """r49: moved like the island - it follows the mouse once you've moved 7 px (a click stays a click), it lifts
        while held, and glides onto an edge when you let go. No Windows move loop."""
        self._stop_anim()
        if not self._btn_down():
            return
        self.press_at = QCursor.pos()
        self.grab = self.press_at - self.pos()
        self.dragging = False
        if self.drag_t is not None:
            self.drag_t.stop()
            self.drag_t.deleteLater()
        self.drag_t = QTimer(self)

        def step():
            pos = QCursor.pos()
            if not self._btn_down():
                self.drag_t.stop()
                if self.dragging:
                    self.dragging = False
                    self.js("drag", "false")
                    self.snap(pos)
                self.press_at = None
                return
            if not self.dragging and (pos - self.press_at).manhattanLength() > 7:
                self.dragging = True
                self.js("drag", "true")
            if self.dragging:
                self.move(pos - self.grab)
        self.drag_t.timeout.connect(step)
        self.drag_t.start(8)

    def resize_start(self):
        self.drag()                           # old pages asked Windows to resize; the corner grip now does it in the page

    def rz_start(self):
        """r49: the corner grip resizes the card inside the page (smooth, like the island's big player). The window grows
        once, here, keeping the corner the card is pinned to (top-left, or top-right on the right edge); it shrinks
        back once when you let go (rz_end). No window resize per frame = no flicker."""
        self._stop_anim()
        z, M = self.host.zoom, self.M * self.host.zoom
        r = self.geometry()
        g = (QGuiApplication.screenAt(r.center()) or self.screen()).availableGeometry()
        wmax, hmax = int((900 + 2 * self.M) * z), int((1100 + 2 * self.M) * z)
        if self.dock == "right":
            right = r.right()
            w = min(wmax, right - g.left() + int(M))
            self.rz = ("right", right, r.top())
            self.setGeometry(right - w + 1, r.top(), w, min(hmax, max(r.height(), g.bottom() + int(M) - r.top())))
        else:
            self.rz = ("left", r.left(), r.top())
            self.setGeometry(r.left(), r.top(), min(wmax, max(r.width(), g.right() + int(M) - r.left())),
                             min(hmax, max(r.height(), g.bottom() + int(M) - r.top())))

    def rz_end(self, cw, ch):
        """The card's final size (page px): the window fits it again, same pinned corner, then it re-sticks to its edge."""
        z, M = self.host.zoom, self.M
        w, h = int((float(cw) + 2 * M) * z), int((float(ch) + 2 * M) * z)
        w, h = max(self.minimumWidth(), w), max(self.minimumHeight(), h)
        side, x, y = self.rz or ("left", self.x(), self.y())
        self.rz = None
        self.setGeometry(x - w + 1 if side == "right" else x, y, w, h)
        self.save_t.start(500)
        QTimer.singleShot(60, lambda: self.snap(None, keep=True))

    def _stop_anim(self):
        if self.anim is not None:
            self.anim.stop()
            self.anim = None

    def snap(self, pos=None, keep=False, animate=True):
        """Stick to the top, left or right edge, anywhere along it (corners too) - the island's own rules.
        keep = stay on the edge it's on (after a resize)."""
        try:
            z, M, gap = self.host.zoom, self.M * self.host.zoom, self.GAP * self.host.zoom
            r = self.geometry()
            card = r.adjusted(int(M), int(M), -int(M), -int(M))
            pos = pos or card.center()
            scr = QGuiApplication.screenAt(pos) or QGuiApplication.screenAt(card.center()) or self.screen()
            g = scr.availableGeometry()
            if keep:
                dock = self.dock
            elif pos.y() - g.top() < max(110, int(g.height() * 0.12)):      # along the top, corners included
                dock = "top"
            elif pos.x() - g.left() < max(140, int(g.width() * 0.12)):
                dock = "left"
            elif g.right() - pos.x() < max(140, int(g.width() * 0.12)):
                dock = "right"
            else:                                                            # let go mid-screen: the nearest edge
                dock = min((card.top() - g.top(), "top"), (card.left() - g.left(), "left"), (g.right() - card.right(), "right"))[1]
            cw, ch = card.width(), card.height()
            if dock == "top":
                cx = max(g.left() + gap, min(card.left(), g.right() + 1 - gap - cw))
                cy = g.top() + gap
                self.anchor = cx + cw / 2
            else:
                cx = g.left() + gap if dock == "left" else g.right() + 1 - gap - cw
                cy = max(g.top() + gap, min(card.top(), g.bottom() + 1 - gap - ch))
                self.anchor = cy + ch / 2
            end = QRect(int(cx - M), int(cy - M), r.width(), r.height())
            self.dock = dock
            if self.ready:
                self.js("pwdock", json.dumps(dock))
            if not animate or end == r:
                self.setGeometry(end)
            else:
                anim = QVariantAnimation(self)
                anim.setStartValue(r)
                anim.setEndValue(end)
                anim.setDuration(460)
                ec = QEasingCurve(QEasingCurve.OutBack)      # r49: lands with a little bounce, like the island
                ec.setOvershoot(0.9)
                anim.setEasingCurve(ec)
                anim.valueChanged.connect(lambda v: self.setGeometry(v))
                anim.start()
                self.anim = anim
            self.save_t.start(500)
        except RuntimeError:
            pass

    def _follow(self, how):
        if not self._btn_down():
            return
        c0, g0 = QCursor.pos(), self.geometry()
        if self.drag_t is not None:
            self.drag_t.stop()
            self.drag_t.deleteLater()
        self.drag_t = QTimer(self)

        def step():
            if not self._btn_down():
                self.drag_t.stop()
                self.snap(QCursor.pos(), keep=how == "size")
                return
            d = QCursor.pos() - c0
            if how == "move":
                self.move(g0.topLeft() + d)
            else:
                self.resize(max(self.minimumWidth(), g0.width() + d.x()), max(self.minimumHeight(), g0.height() + d.y()))
        self.drag_t.timeout.connect(step)
        self.drag_t.start(8)

    def js(self, fn, a):
        if self.ready:
            self.page.runJavaScript(f"window.island && window.island.{fn}({a})")


# ============================================================== host window
LAUNCH_MAX = 10


class Host(QWidget):
    media_sig = Signal(object)
    snd_sig = Signal(object)
    lvl_sig = Signal(object)
    lyr_sig = Signal(object)
    br_sig = Signal(object)
    win_sig = Signal(object)
    wx_sig = Signal(object)
    note_sig = Signal(object)
    fit_sig = Signal(object)
    inbox_sig = Signal(object)

    def __init__(self):
        super().__init__(None)
        self.model = core.Model()
        self.state = self.load_state()
        if not self.state.get("home43"):          # r43: the player pops out now, so the island goes home: top, dead centre
            self.state.update(home43=True, dock="top", anchor=None)
        self.fired = set(self.state.get("fired", []))
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle("Island")
        self.ready = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = QWebEngineView(self)
        self.page = Page(self.view)
        self.view.setPage(self.page)
        self.page.setBackgroundColor(Qt.transparent)
        self.view.setStyleSheet("background: transparent")
        self.view.setAttribute(Qt.WA_TranslucentBackground)
        st = self.page.settings()
        st.setAttribute(QWebEngineSettings.ShowScrollBars, False)
        st.setAttribute(QWebEngineSettings.LocalContentCanAccessRemoteUrls, True)
        st.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)     # soft notification sounds
        self.view.setContextMenuPolicy(Qt.NoContextMenu)
        lay.addWidget(self.view)
        self.channel = QWebChannel(self.page)
        self.bridge = Bridge(self)
        self.channel.registerObject("bridge", self.bridge)
        self.page.setWebChannel(self.channel)

        self.mode = "pill"
        self.rect = QRect(0, 0, 0, 0)        # island rect in window coords (from JS)
        self.click_through = None
        self.dragging = False
        self.press_at = None
        self.grab = QPoint()
        self.hidden_fs = False
        self.day_offset = 0
        self.toasts = []
        self.toast_cb = []
        self.toast_return = "pill"
        self.toast_until = None
        self.prev_button = False
        self.anim = None

        self.media = MediaWorker(lambda m: self.media_sig.emit(m))
        self.media_sig.connect(self.push_media)
        self.wx_sig.connect(lambda w: (self.js("wx", w), self.wx_alerts(w)))
        self.sys = SysStats()
        self._snd, self._icons = None, {}
        self.apps_cat, self.apps_t, self.launch_all = None, 0, []
        self._art_sent = None
        self.snd_sig.connect(self.push_sound)
        self.audio = AudioWorker(lambda x: self.snd_sig.emit(x))
        self.lvl_sig.connect(lambda v: self.js("lvl", v))
        self.meter = MeterWorker(lambda v: self.lvl_sig.emit(v))
        self._lyr = None
        self.lyr_sig.connect(self.on_lyrics)
        self.lyrics = island_lyrics.Lyrics(lambda x: self.lyr_sig.emit(x))
        self.br_sig.connect(self.on_bridge)
        # NB: not self.bridge - that's the web channel's object; replacing it freed it and crashed the island
        self.spotify = island_bridge.Bridge(lambda j: self.br_sig.emit(j))
        self.spotify.start()
        self.win_sig.connect(self.on_win)
        self.keys = island_win.KeyThread(lambda e: self.win_sig.emit(e))
        self.gpu = island_win.GpuWorker(lambda e: self.win_sig.emit(e))
        self.gpu.start()
        self.bt = island_win.BtWorker(lambda e: self.win_sig.emit(e))
        self.bt.start()
        self._gpu, self._bt, self._bt_low = None, [], set()
        try:
            self.study = island_study.Study(self.model.study.exam_day)
        except Exception as e:
            print("study tools:", e)
            self.study = island_study.Study(dt.date(2026, 12, 10))
        self.pomo, self.pomo_key, self.pomo_phase = None, None, None
        try:
            self.cards = island_study.Cards()
        except Exception as e:
            print("review cards:", e)
            self.cards = None
        self._cards_day = None
        try:
            island_study.write_blocker_files()
        except Exception as e:
            print("site blocker files:", e)
        self._block_until = -1
        self.prayer = island_day.Prayer(CITY[1], CITY[2], PRAYER_METHOD)
        self.daylog = island_day.DayLog()
        self._prayer_js, self._away_from, self._last_act = None, None, time.time()
        self._streak_from = time.time()
        try:
            self.life = island_life.Life()
            self.model.life = self.life                 # the big countdown reads it
            if "big" not in self.life.data and os.path.basename(os.path.dirname(self.model.study.path)).lower() == "tawjihi":
                self.life.set_big("Tawjihi")             # the plan's exam_day, titled (once; "big ..." changes it)
        except Exception as e:
            print("life.json:", e)
            self.life = None
        self._life_day = None
        self.history = []
        self.dev = island_dev.DevWatch(lambda e: self.win_sig.emit(e))
        self.dev.start()
        self._gh, self._deploy, self._ram_hi = None, None, 0
        self.lan, self.lan_q = None, queue.Queue()
        try:
            self.write_mcp_snippet()
        except Exception as e:
            print("mcp snippet:", e)
        self._exe_paths, self._screen_dirty = {}, False
        try:
            with open(os.path.join(HERE, "screen_log.json"), encoding="utf-8") as f:
                self.screen_log = json.load(f)
        except Exception:
            self.screen_log = {}
        self.guard_game, self.guard_at, self._study_js = None, 0.0, None
        self.watch = island_win.WinWatch(lambda e: self.win_sig.emit(e))
        self.bright = island_win.BrightWorker(lambda e: self.win_sig.emit(e))
        self.clips, self.hud_cb, self._bat, self._low_warned = [], [], None, False
        self._ping = self._wifi = self._priv = self._bri = self._sysx = None
        self.privacy, self.privacy_auto, self._obs_at = False, False, 0.0
        self.timers = [t for t in self.state.get("timers", []) if t.get("sw") or t.get("end", 0) > time.time()]
        if WIN:
            self.keys.start()
            self.watch.start()
            self.bright.start()
        QApplication.clipboard().dataChanged.connect(self.on_clip)
        QTimer.singleShot(1500, self.sync_hook)
        QTimer.singleShot(8000, self.sync_hook)        # pycaw may load slowly on the watcher thread
        self._near, self._near_at, self._heat = None, 0.0, None
        self._fit = None
        self.fit_sig.connect(self.push_fit)
        self.fit = island_fit.FitWorker(lambda f: self.fit_sig.emit(f))
        # one water total: today's water comes from QAWIFit whenever it's connected
        _plan_water = self.model.water

        def _water(day, _plan=_plan_water):
            ml, goal = _plan(day)
            q = self.fit.live_water(day.isoformat())
            return (q if q is not None else ml), goal
        self.model.water = _water
        self.inbox_sig.connect(self.on_inbox)
        self.inbox = island_inbox.Inbox(self.fit, lambda r: self.inbox_sig.emit(r))
        self._today_json = None
        self.settings = feats.Settings()
        self.zoom = float(self.settings.get("scale") or 1.0)
        self.view.setZoomFactor(self.zoom)
        self._t0, self._shown, self._stale_sent, self._trim_at = time.time(), False, None, 0.0
        self.after = sys.argv[sys.argv.index("--after") + 1] if "--after" in sys.argv[:-1] else None
        self.setWindowOpacity(0.0)        # fades in once the UI has its data (no blank flash at start)
        QTimer.singleShot(8000, self.fade_in)          # safety net if the bridge is slow

        self.place()
        self.page.loadFinished.connect(self.on_loaded)
        # the web renderer can die on its own (GPU driver reset etc.): Python is fine, the island is blank
        self.page.renderProcessTerminated.connect(
            lambda st, code: (print(f"web renderer died ({code}) -> reloading"), QTimer.singleShot(1500, self.reload_all)))
        self.view.load(QUrl.fromLocalFile(os.path.join(HERE, "ui.html")))
        self.make_tray()
        QTimer.singleShot(6000, self.check_bridge)

        self.fast = QTimer(self)          # cursor / click-through / drag
        self.fast.timeout.connect(self.on_fast)
        self.fast.start(16)
        self.slow = QTimer(self)          # data + reminders
        self.slow.timeout.connect(self.on_slow)
        self.slow.start(5000)
        self.follow_timer = QTimer(self)  # follow the mouse to the other screen
        self.follow_timer.timeout.connect(self.follow_tick)
        self.follow_timer.start(250)
        self.fs_timer = QTimer(self)      # fullscreen check + system stats
        self.fs_timer.timeout.connect(self.on_second)
        self.fs_timer.start(1000)
        self.hb = QTimer(self)            # heartbeat for island_watchdog.pyw (proves the UI thread is alive)
        self.hb.timeout.connect(feats.heartbeat)
        self.hb.start(10_000)
        feats.heartbeat()
        self.upd_timer = QTimer(self)     # copies from GitHub: look for new code every 6 h (first look 3 min after start)
        self.upd_timer.timeout.connect(self.update_check)
        self.upd_timer.start(6 * 3600_000)
        QTimer.singleShot(180_000, self.update_check)
        QTimer.singleShot(60_000, lambda: threading.Thread(target=ram_top, daemon=True).start())   # first RAM picture
        try:                              # 16 GB and less: tighter memory limits, trim twice as often
            import psutil
            self.ram_gb = psutil.virtual_memory().total / 2**30
        except Exception:
            self.ram_gb = 32
        self.low_ram = self.ram_gb < 20
        self._press_at = 0
        print(f"ram: {self.ram_gb:.0f} GB" + (" -> low-RAM mode (page limit 1.5 GB, trim every 5 min)" if self.low_ram else ""))
        self.trim_timer = QTimer(self)    # give idle RAM back to Windows
        self.trim_timer.timeout.connect(self.trim)
        self.trim_timer.start((5 if self.low_ram else 10) * 60_000)
        self.rguard_timer = QTimer(self)  # the page process's memory (see renderer_guard)
        self.rguard_timer.timeout.connect(self.renderer_guard)
        self.rguard_timer.start(60_000)
        self.wall_timer = QTimer(self)    # the wallpaper's colour for the desktop widgets (a slideshow changes it)
        self.wall_timer.timeout.connect(self.wall_sync)
        self.wall_timer.start(300_000)
        self.house_timer = QTimer(self)   # weekly backup + tidy old .bak files
        self.house_timer.timeout.connect(self.housekeep)
        self.house_timer.start(24 * 3600_000)
        QTimer.singleShot(5 * 60_000, self.housekeep)
        self.setup_watch()
        self.media.start()
        self.audio.start()
        self.meter.start()
        self.claude_unread, self.claude_ask = 0, False
        self.note_sig.connect(self.on_note)
        base = core.load_config().get("notify_apps", ["Claude"]) if hasattr(core, "load_config") else ["Claude"]
        self.notes = NotifyWorker(lambda e: self.note_sig.emit(e), lambda: base + self.mirror_apps())
        self.notes.start()
        self.fit.start()
        threading.Thread(target=self.weather_loop, daemon=True).start()

    # ---------------------------------------------------------- bridge bootstrap + tray
    def on_loaded(self, ok):
        print("ui.html loaded:", ok)
        self.view.setZoomFactor(self.zoom)
        f = QFile(":/qtwebchannel/qwebchannel.js")
        src = ""
        if f.open(QIODevice.ReadOnly):
            src = bytes(f.readAll()).decode("utf-8", "replace")
            f.close()
        else:
            print("could not open :/qtwebchannel/qwebchannel.js")
        js = (src + "\n;" if src else "") + "window.__connect ? window.__connect() : 'no __connect'"
        self.page.runJavaScript(js, lambda r: print("bridge connect ->", r))

    def check_bridge(self):
        if not self.ready:
            print("WARNING: UI bridge not connected after 6 s; retrying")
            self.on_loaded(True)
            QTimer.singleShot(6000, self.check_bridge)

    def make_tray(self):
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        g = QPainter(pm)
        g.setRenderHint(QPainter.Antialiasing)
        g.setBrush(QBrush(QColor("#0a0b0f")))
        g.setPen(QColor(255, 255, 255, 60))
        g.drawRoundedRect(4, 20, 56, 24, 12, 12)
        g.setBrush(QBrush(QColor("#a78bfa")))
        g.setPen(Qt.NoPen)
        g.drawEllipse(12, 27, 10, 10)
        g.end()
        self.tray = QSystemTrayIcon(QIcon(pm), self)
        self.tray.setToolTip("Island")
        self.tray.activated.connect(lambda r: self.js("mode", "panel") if r == QSystemTrayIcon.Trigger else None)
        self.tray_menu = QMenu()
        self.tray_menu.aboutToShow.connect(lambda: self.fill_menu(self.tray_menu))
        self.tray.setContextMenu(self.tray_menu)
        self.tray.show()

    # ---------------------------------------------------------- state file
    def load_state(self):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_state(self):
        self.state["fired"] = sorted(self.fired)
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, indent=1)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            print("state save failed:", e)

    # ---------------------------------------------------------- geometry
    @property
    def dock(self):
        return self.state.get("dock", "top")

    def screen(self):
        name, xy = self.state.get("screen"), self.state.get("screen_xy")
        scrs = QGuiApplication.screens()
        if xy:
            for s in scrs:
                g = s.geometry()
                if s.name() == name and [g.left(), g.top()] == list(xy):
                    return s
        for s in scrs:
            if s.name() == name:
                return s
        return QGuiApplication.primaryScreen()

    @staticmethod
    def skey(s):
        g = s.geometry()
        return s.name(), g.left(), g.top()

    def win_size(self, scr):
        g = scr.geometry()
        z = getattr(self, "zoom", 1.0)
        return min(int(WIN_W * z), g.width()), min(int(WIN_H * z), g.height())

    def target_geom(self, dock=None, scr=None, anchor=None):
        dock = dock or self.dock
        scr = scr or self.screen()
        g = scr.geometry()
        w, h = self.win_size(scr)
        a = anchor if anchor is not None else self.state.get("anchor")
        if dock == "top":
            a = g.center().x() if a is None else a
            x = min(max(a - w // 2, g.left()), g.right() + 1 - w)
            return QRect(int(x), g.top(), w, h)
        a = g.center().y() if a is None else a
        y = min(max(a - h // 2, g.top()), g.bottom() + 1 - h)
        x = g.left() if dock == "left" else g.right() + 1 - w
        return QRect(x, int(y), w, h)

    def push_shift(self, geom=None, dock=None, anchor=None):
        """Corners (r32): the window can't leave the screen, so near a screen edge the island slides inside its window
        toward the spot you dropped it on. Sent as CSS px; the page keeps the island fully inside the window."""
        try:
            geom = geom or self.geometry()
            dock = dock or self.dock
            a = anchor if anchor is not None else self.state.get("anchor")
            z = max(.5, getattr(self, "zoom", 1.0))
            av = (self.screen() if geom is None else (QGuiApplication.screenAt(geom.center()) or self.screen())).availableGeometry()
            if dock == "top":         # + the free span inside the window (taskbar excluded), in CSS px
                d = [0 if a is None else round((a - (geom.x() + geom.width() / 2)) / z), 0,
                     round((av.left() - geom.x()) / z), round((av.right() + 1 - geom.x()) / z)]
            else:
                d = [0, 0 if a is None else round((a - (geom.y() + geom.height() / 2)) / z),
                     round((av.top() - geom.y()) / z), round((av.bottom() + 1 - geom.y()) / z)]
            self.js("shift", d)
        except Exception as e:
            print("shift:", e)

    def place(self):
        self.setGeometry(self.target_geom())
        self.push_shift()
        if not self.ready:   # until the UI reports its real size, assume the collapsed pill
            w, h = (580, 46) if self.dock == "top" else (66, 292)
            w, h = int(w * self.zoom), int(h * self.zoom)
            W, H = self.width(), self.height()
            x = (W - w) // 2 if self.dock == "top" else (8 if self.dock == "left" else W - 8 - w)
            y = 8 if self.dock == "top" else (H - h) // 2
            self.rect = QRect(x - 4, y - 4, w + 8, h + 8)

    # ---------------------------------------------------------- js
    def page_freeze(self):
        """Behind a fullscreen game nobody sees the island: freeze its page (no timers, no animation, no drawing).
        Python keeps running; the page gets everything again when the game is left."""
        if not self.hidden_fs or getattr(self, "_frozen", False) or self.isVisible():
            return
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage
            self.page.setLifecycleState(QWebEnginePage.LifecycleState.Frozen)
            self._frozen = self.page.lifecycleState() == QWebEnginePage.LifecycleState.Frozen
            print("page: frozen (fullscreen app)" if self._frozen else "page: could not freeze")
        except Exception as e:
            print("page freeze:", e)

    def page_thaw(self):
        if not getattr(self, "_frozen", False):
            return
        self._frozen = False
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage
            self.page.setLifecycleState(QWebEnginePage.LifecycleState.Active)
        except Exception as e:
            print("page thaw:", e)
        print("page: awake, sending everything again")
        self._resync = True
        try:
            self.on_action("ready", None)          # the same full update a fresh page gets
        finally:
            self._resync = False

    def js(self, fn, arg=None):
        d = getattr(self, "dash", None)
        k = getattr(self, "desk", None)
        pw = getattr(self, "popw", None)
        if not self.ready and not (d and d.ready) and not (k and k.ready) and not (pw and pw.ready):
            return
        a = "" if arg is None else json.dumps(arg, ensure_ascii=False, default=str)
        if self.ready and not getattr(self, "_frozen", False):
            self._js_n = getattr(self, "_js_n", 0) + 1
            jc = self.__dict__.setdefault("_js_fn", {})
            jc[fn] = jc.get(fn, 0) + 1
            self.page.runJavaScript(f"window.island && window.island.{fn}({a})")
        if d is not None and d.ready and fn in DASH_FNS:
            d.js(fn, a)
        k = getattr(self, "desk", None)
        if k is not None and k.ready and fn in DESK_FNS:
            k.js(fn, a)
        if pw is not None and pw.ready and fn in POP_FNS:
            pw.js(fn, a)
        L = self.lan
        if L is not None and L.clients and fn in DECK_FNS:
            L.broadcast(fn, a)

    def deck_push(self, fn, arg):
        """Only to the decks (things the island itself already has)."""
        L = self.lan
        if L is not None and L.clients:
            L.broadcast(fn, json.dumps(arg, ensure_ascii=False, default=str) if arg is not None else "")

    # ---------------------------------------------------------- dashboard
    def set_dash(self, scr):
        """Open the dashboard on that screen (None = close it). Remembered across restarts."""
        old = getattr(self, "dash", None)
        if old is not None:
            self.dash = None
            old.ready = False
            old.close()
            old.deleteLater()
        if scr is None:
            self.state["dash"] = None
        else:
            g = scr.geometry()
            self.state["dash"] = [scr.name(), g.left(), g.top()]
            self.dash = DashWin(self, scr)
            n = next((i + 1 for i, x in enumerate(QGuiApplication.screens()) if self.skey(x) == self.skey(scr)), "?")
            print(f"dashboard: on {scr.name()} (monitor {n})")
            if self.ready:
                self.hud("screen", f"Dashboard on monitor {n}", act=["Close"], cb=[lambda: self.set_dash(None)], ms=3500)
        self.save_state()

    def dash_screen(self):
        want = self.state.get("dash")
        if not want:
            return None
        for x in QGuiApplication.screens():
            g = x.geometry()
            if [x.name(), g.left(), g.top()] == list(want):
                return x
        return None

    def toggle_dash(self):
        if getattr(self, "dash", None) is not None:
            return self.set_dash(None)
        scrs = QGuiApplication.screens()
        here = self.screen()
        other = next((x for x in scrs if self.skey(x) != self.skey(here)), None)
        if other is None:
            return self.hud("info", "The dashboard needs a second screen", ms=1800)
        self.set_dash(other)

    def on_dash_action(self, name, arg):
        d = getattr(self, "dash", None)
        if d is None:
            return
        if name == "ready":
            if not d.ready:
                print("dashboard: page ready")
            d.ready = True
            self.push_settings()
            self.push_data()
            if self.media.last:
                self._art_sent = None
                self.push_media(self.media.last)
            s = self.sys.sample()
            if s:
                self.js("sys", s)
            if getattr(self, "_wx", None):
                self.js("wx", self._wx)
            if self._lyr:
                self.js("lyrics", self._lyr)
            self.js("launch", getattr(self, "_launch_js", []))
            self.push_timers()
            self._sysx = None
            st = getattr(self, "sp_state", None)
            if st:
                self.js("spstate", {k: v for k, v in st.items() if k != "at"})
        elif name == "dash_close":
            self.set_dash(None)
        elif name in ("toggle", "ex", "water", "launch", "media_toggle", "media_next", "media_prev", "media_seek",
                      "media_shuffle", "media_repeat", "media_like", "media_src"):
            self.on_action(name, arg)

    def own_hwnds(self):
        own = {int(self.winId())}
        d = getattr(self, "dash", None)
        if d is not None:
            own.add(int(d.winId()))
        k = getattr(self, "desk", None)
        if k is not None:
            own.add(int(k.winId()))
        pw = getattr(self, "popw", None)
        if pw is not None:
            own.add(int(pw.winId()))
        return own

    # ---------------------------------------------------------- desktop widgets
    def desk_peek(self, on=None):
        """Desktop widgets over every window for a moment (Ctrl+Alt+Shift+D / Win+Alt+D, the drawer's Widgets, "widgets")."""
        if not WIN or self.hidden_fs:
            return
        k = getattr(self, "desk", None)
        if k is None:                         # off: turn them on, then peek
            self.set_setting("desk", True)
            self.push_settings()
            self.desk_sync()
            if getattr(self, "desk", None) is not None:
                QTimer.singleShot(1800, lambda: self.desk_peek(True))
            return
        on = (not getattr(k, "peeking", False)) if on is None else on
        if on != getattr(k, "peeking", False):
            k.peek(on)

    def wall_sync(self):
        """The wallpaper's colours tint the desktop widgets: a dark tile tint from its average, an accent from its most
        vivid part. Windows' current wallpaper file (or its own copy), re-read when it changes."""
        k = getattr(self, "desk", None)
        if not WIN or k is None:
            return
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as key:
                path = winreg.QueryValueEx(key, "WallPaper")[0]
            if not path or not os.path.exists(path):
                path = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Themes\TranscodedWallpaper")
            st = os.stat(path)
        except Exception:
            return
        sig = (path, st.st_mtime, st.st_size)
        if sig == getattr(self, "_wall_sig", None):
            return
        self._wall_sig = sig
        from PySide6.QtGui import QImage
        img = QImage(path)
        if img.isNull():
            return
        img = img.scaled(48, 27, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).convertToFormat(QImage.Format_RGB32)
        raw = bytes(img.constBits())                      # B G R A per pixel, rows may be padded
        px, viv, bpl = [], [], img.bytesPerLine()
        for y in range(img.height()):
            for x in range(img.width()):
                i = y * bpl + x * 4
                r, g, b = raw[i + 2], raw[i + 1], raw[i]
                mx = max(r, g, b)
                px.append((r, g, b))
                viv.append(((mx - min(r, g, b)) / 255 * (mx > 76), (r, g, b)))
        n = len(px)
        avg = [sum(p[i] for p in px) / n for i in range(3)]
        viv.sort(key=lambda v: -v[0])
        top = [v[1] for v in viv[:max(1, n // 12)]]
        acc = [sum(p[i] for p in top) / len(top) for i in range(3)]
        m = max(acc) or 1
        acc = [min(255, round(v * 225 / m)) for v in acc]
        bg = [round(avg[i] * .26 + (12, 12, 16)[i] * .74) for i in range(3)]
        hov = [min(255, v + 16) for v in bg]
        self._wall = dict(bg=",".join(map(str, bg)), hov=",".join(map(str, hov)), acc=",".join(map(str, acc)))
        print(f"wall: tiles tinted {self._wall['bg']} · accent {self._wall['acc']}")
        self.js("wall", self._wall)

    # ---------------------------------------------------------- the popped-out player (r41)
    def pop_open(self, rect=None, geo=None):
        """rect = where the island's player is (page px): the window opens right there. geo = a saved window rect."""
        pw = getattr(self, "popw", None)
        if pw is not None:
            pw.front()
            return
        z, M = self.zoom, PopWin.M
        if rect:
            x, y, w, h = (float(v) for v in rect[:4])
            tl = self.mapToGlobal(QPoint(int(x * z), int(y * z)))
            geo = (tl.x() - int(M * z), tl.y() - int(M * z), int((w + 2 * M) * z), int((h + 2 * M) * z))
        if not geo:
            g = QGuiApplication.primaryScreen().availableGeometry()
            w, h = int((520 + 2 * M) * z), int((600 + 2 * M) * z)
            geo = (g.right() - w - 40, g.top() + 60, w, h)
        x, y, w, h = (int(v) for v in geo)
        scr = QGuiApplication.screenAt(QPoint(x + w // 2, y + 30)) or QGuiApplication.primaryScreen()
        g = scr.availableGeometry()                       # keep it on a screen (a monitor may be gone since)
        w, h = min(w, g.width()), min(h, g.height())
        x, y = max(g.left() - int(M * z), min(x, g.right() - w + int(M * z))), max(g.top() - int(M * z), min(y, g.bottom() - h + int(M * z)))
        self.popw = PopWin(self, (x, y, w, h), dock=(self.state.get("pop") or {}).get("dock", "top") if geo and not rect else "top")
        self.popw.snap(None, keep=bool(geo and not rect), animate=False)   # on an edge from the start, like the island
        self.state["pop"] = dict(on=True, geo=[x, y, w, h], dock=self.popw.dock)
        self.save_state()
        print(f"player: popped out at {x},{y} {w}x{h}")

    def pop_home(self):
        """r48: the player is out in its own window, so the island goes home: the top of the screen, dead centre."""
        if getattr(self, "popw", None) is None or self.dragging:
            return
        if self.dock != "top" or self.state.get("anchor") is not None:
            print(f"player: out -> island from {self.dock} back to the top centre")
            self.set_dock("top")

    def pop_save(self):
        pw = getattr(self, "popw", None)
        if pw is not None:
            r = pw.geometry()
            self.state["pop"] = dict(on=True, geo=[r.x(), r.y(), r.width(), r.height()], dock=pw.dock)
            self.save_state()

    def pop_close(self, back_in=False):
        pw = getattr(self, "popw", None)
        if pw is None:
            return
        self.pop_save()
        self.popw = None
        self.state["pop"]["on"] = False
        self.save_state()
        pw.ready = False
        pw.hide()
        pw.close()
        pw.deleteLater()
        self.js("popped", "in" if back_in else "closed")
        print("player: back in the island" if back_in else "player: closed")

    def on_pop_action(self, name, arg):
        pw = getattr(self, "popw", None)
        if pw is None:
            return
        if name == "ready":
            pw.ready = True
            pw.no_activate()
            self.push_settings()
            if self.media.last:
                self._art_sent = None
                self.push_media(self.media.last)
            if self._lyr:
                self.js("lyrics", self._lyr)
            st = getattr(self, "sp_state", None)
            if st:
                self.js("spstate", {k2: v for k2, v in st.items() if k2 != "at"})
            sp, m = getattr(self, "sp_pos", None), self.media.last or {}
            if sp and m.get("title") and time.time() - sp[2] < 30:         # r44: Spotify's exact clock right away, not
                p0, playing, at = sp                                         # the Windows one (it can be seconds off)
                pw.js("spos", json.dumps([round(p0 + (time.time() - at if playing else 0), 2), playing, m.get("title")]))
            if getattr(self, "_away_js", None):
                self.js("away", True)
            pw.js("pwdock", json.dumps(pw.dock))
            if not pw.fs_hidden:
                pw.show()
                pw.front()
            QTimer.singleShot(180, lambda: self.js("popped", "out"))    # now the island lets go of its player
            QTimer.singleShot(650, self.pop_home)
        elif name == "pw_drag":
            pw.drag()
        elif name == "pw_resize":
            pw.resize_start()
        elif name == "pw_rz":
            if arg and arg[0]:
                pw.rz_start()
            elif arg:
                pw.rz_end(arg[1], arg[2])
        elif name == "pw_in":
            self.pop_close(True)
        elif name == "pw_close":
            self.pop_close(False)
        elif name in ("media_toggle", "media_next", "media_prev", "media_seek", "media_shuffle", "media_repeat", "media_like", "media_src"):
            self.on_action(name, arg)

    def desk_sync(self):
        want = WIN and self.settings.get("desk", True) and not self.hidden_fs
        k = getattr(self, "desk", None)
        if want and k is None:
            self.desk = DeskWin(self)
            print("desk: widgets on the wallpaper")
        elif not want and k is not None:
            self.desk = None
            k.ready = False
            k.close()
            k.deleteLater()

    def on_desk_action(self, name, arg):
        k = getattr(self, "desk", None)
        if k is None:
            return
        if name == "ready":
            k.ready = True
            self.push_settings()
            self.push_data()
            if self.media.last:
                self._art_sent = None
                self.push_media(self.media.last)
            s = getattr(self, "_last_sys", None)
            if s:
                self.js("sys", s)
            if getattr(self, "_wx", None):
                self.js("wx", self._wx)
            for f in (self.push_life, self.push_cards, self.push_prayer, self.push_timers, lambda: self.push_screen(force=True),
                      lambda: self.push_study(force=True), self.push_taps):
                try:
                    f()
                except Exception as e:
                    print("desk push:", e)
            if getattr(self, "_fit", None):
                self.js("fit", self._fit)
            self.push_devbat()
            self.js("launch", getattr(self, "_launch_js", []))
            if self._lyr:
                self.js("lyrics", self._lyr)
            st = getattr(self, "sp_state", None)
            if st:
                self.js("spstate", {k2: v for k2, v in st.items() if k2 != "at"})
            if getattr(self, "_wall", None):     # same wallpaper: resend the tint instead of decoding the image again
                self.js("wall", self._wall)
            self.wall_sync()
            if getattr(self, "_adev", None):
                self.js("adev", self._adev)
        elif name == "desk_unpeek":
            self.desk_peek(False)
        elif name == "desk_size":
            try:
                w, h, corner = arg
                k.box = (max(120, int(w)), max(60, int(h)), corner if corner in ("tl", "tr", "bl", "br") else "tr")
                k.place()
                k.pin()
            except Exception as e:
                print("desk size:", e)
        elif name == "desk_hide":
            self.set_setting("desk", False)
            self.push_settings()
            self.hud("screen", "Desktop widgets off", r="Settings > Look to bring them back", ms=2600)
        elif name == "desk_open":
            self.js("openTab", str(arg or "Today"))
        elif name == "desk_prefill":
            self.on_action("cmd_open", arg)
        elif name in ("cmd", "say", "water", "launch", "life_toggle", "media_toggle", "media_next", "media_prev", "audio_dev"):
            self.on_action(name, arg)

    def push_data(self):
        self.model.reload()
        try:
            d = build_state(self.model, self.day_offset)
            d["date"] = self.model.today().isoformat()
            d["heat"] = self.heat(d.get("pct"))
            self.add_qawi(d)
            self.js("data", d)
            if self.day_offset == 0:
                self.write_today(d)
        except Exception as e:
            import traceback
            traceback.print_exc()

    def app_icon(self, exe):
        """The app's own Windows icon as a data URL (cached per exe)."""
        if not exe:
            return None
        if exe not in self._icons:
            url = None
            try:
                from PySide6.QtWidgets import QFileIconProvider
                from PySide6.QtCore import QFileInfo, QBuffer, QByteArray
                pm = QFileIconProvider().icon(QFileInfo(exe)).pixmap(64, 64)
                if not pm.isNull():
                    ba = QByteArray()
                    buf = QBuffer(ba)
                    buf.open(QIODevice.WriteOnly)
                    pm.save(buf, "PNG")
                    buf.close()
                    url = "data:image/png;base64," + bytes(ba.toBase64()).decode()
            except Exception as e:
                print("icon:", e)
            self._icons[exe] = url
        return self._icons[exe]

    def push_sound(self, snd):
        if snd and "adev" in snd:                       # the device lists (r22)
            self._adev = snd["adev"]
            self.js("adev", self._adev)
            return
        if snd and snd.get("ok"):
            snd = dict(snd, apps=[dict({k: v for k, v in a.items() if k != "exe"}, icon=self.app_icon(a.get("exe")))
                                  for a in snd["apps"]])
        self._snd = snd
        self.js("sound", snd)

    def push_media(self, m):
        """From the PC's media session; Spotify playing on his phone / another device (Spotify Connect) shows instead
        when nothing else really plays here."""
        self._win_media = m
        pop = self.state.get("pop") or {}
        if (pop.get("on") and pop.get("geo") and m and m.get("playing") and self.ready and getattr(self, "popw", None) is None
                and not self.hidden_fs and not getattr(self, "_pop_restored", False)):
            self._pop_restored = True            # once per run: popped out last time -> out again with the music
            QTimer.singleShot(400, lambda g=tuple(pop["geo"]): self.pop_open(geo=g))
        self._push_media(self.media_merged(m))

    def media_merged(self, m):
        r = self.sp_remote_media()
        if r is None or (m and m.get("playing") and m.get("app") != "Spotify"):
            return m
        return r

    def sp_remote_on(self):
        rem = getattr(self, "sp_rem", None)
        return bool(rem and rem.get("remote") and time.time() - rem.get("at", 0) < 8 and self.spotify.listening())

    def sp_remote_media(self):
        if not self.sp_remote_on():
            return None
        song, rem = getattr(self, "sp_song", None) or {}, self.sp_rem
        if not song.get("title"):
            return None
        pos, playing, at = getattr(self, "sp_pos", (0.0, False, time.time()))
        if playing:
            pos += time.time() - at
        dur = float(song.get("dur") or 0)
        art = (getattr(self, "_sp_art", {}) or {}).get(song.get("art") or "")
        if not art and song.get("art"):
            self.sp_art_fetch(song["art"])   # retries a failed cover (at most once a minute)
        return dict(title=song["title"], artist=song.get("artist", ""), album=song.get("album", ""), app="Spotify",
                    playing=playing, pos=round(min(pos, dur) if dur else pos, 1), dur=round(dur, 1), art=art, seek=True,
                    shuf=None, rep=None, srcs=None, dev=rem.get("dev") or "another device", remote=True, vol=rem.get("vol"))

    def sp_art_fetch(self, url):
        """A remote song's cover (https) -> data URL like the PC's own covers (the page reads colours from it)."""
        cache, fails = self.__dict__.setdefault("_sp_art", {}), self.__dict__.setdefault("_sp_art_fail", {})
        if not url or not url.startswith("https://i.scdn.co/"):
            return
        if url in cache and (cache[url] or url not in fails or time.time() - fails[url] < 60):
            return                          # have it / downloading / failed less than a minute ago
        fails.pop(url, None)

        def run():
            try:
                raw = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "island"}), timeout=8).read(600_000)
                mime = "image/png" if raw[:4] == b"\x89PNG" else "image/jpeg"
                self.win_sig.emit(("spart", (url, f"data:{mime};base64," + base64.b64encode(raw).decode())))
            except Exception as e:
                fails[url] = time.time()
                print("spotify cover:", e)
        cache[url] = None
        threading.Thread(target=run, daemon=True).start()

    def _push_media(self, m):
        self._media_remote = bool(m and m.get("remote"))
        # the cover is a big data: URL and polls come every 1.2 s while playing: only send it when it
        # changes (art=1 means "same cover as last time", the page keeps its copy)
        art = (m or {}).get("art")
        if art and art == self._art_sent:
            m = dict(m, art=1)
        else:
            self._art_sent = art
        self.js("media", m)
        if m and self.settings.get("lyrics") != "off":
            x, rem = self._lyr or {}, getattr(self, "sp_rem", None) or {}
            if x.get("src") == "spotify" and str(x.get("title") or "").strip().lower() == str(m.get("title") or "").strip().lower():
                self._lyr_m = None          # Spotify's own lyrics for this song already show: nothing to look up
            elif m.get("app") == "Spotify" and (time.time() - getattr(self, "sp_at", 0) < 20
                                                or rem.get("remote") and time.time() - rem.get("at", 0) < 90):
                # the Spicetify bridge sends Spotify's own lyrics a moment later: ask LRCLIB only if they don't come
                self._lyr_m = m
                if m.get("title") != getattr(self, "_lyr_asked", None) and not getattr(self, "_lyr_wait", None):
                    self._lyr_wait = m.get("title") or "?"
                    self._lyr_ext = 0
                    QTimer.singleShot(3000, self._lyr_fallback)
            else:
                self._lyr_m = None
                self.lyrics.want(m)
        self.update_meter()

    def _lyr_fallback(self):
        m, armed = getattr(self, "_lyr_m", None) or {}, self._lyr_wait
        self._lyr_wait = None
        if not m.get("title") or self.settings.get("lyrics") == "off":
            return
        if m.get("title") != armed:                      # skipped on meanwhile: give the new song its own 3 s
            self._lyr_wait = m["title"]
            self._lyr_ext = 0
            return QTimer.singleShot(3000, self._lyr_fallback)
        # the bridge is live on this PC but hasn't sent this song yet (Spotify fetches its own lyrics first, sometimes
        # 4-5 s): give it up to 6 s more before asking LRCLIB (13 Nov: 25 songs looked up 1-2 s before Spotify's arrived)
        sp = getattr(self, "sp_song", None) or {}
        if (getattr(self, "_lyr_ext", 0) < 2 and time.time() - getattr(self, "sp_at", 0) < 5
                and str(sp.get("title") or "").strip().lower() != m["title"].strip().lower()):
            self._lyr_ext = getattr(self, "_lyr_ext", 0) + 1
            self._lyr_wait = armed
            return QTimer.singleShot(3000, self._lyr_fallback)
        self._lyr_ext = 0
        self._lyr_asked = m["title"]
        x = self._lyr or {}
        if not (x.get("src") == "spotify" and str(x.get("title") or "").strip().lower() == m["title"].strip().lower()):
            self.lyrics.want(m)

    TIP_FOODS = ["seed_chicken_breast_cooked", "me_alalali_tuna_water", "seed_turkey_breast", "seed_egg_white",
                 "seed_tuna_canned", "seed_whey_isolate"]

    def macro_tip(self, f):
        """'85 g protein left: about 270 g chicken breast (445 kcal)', from his QAWIFit foods."""
        try:
            if not f or not f.get("ok"):
                return None
            pl, kl = float(f["t"]["p"]) - float(f["p"]), float(f["t"]["kcal"]) - float(f["kcal"])
            if pl <= 8:
                return dict(done=True, p=round(max(pl, 0)), k=round(kl))
            foods = self.inbox.seed() if hasattr(self.inbox, "seed") else {}
            opts = []
            for fid in self.TIP_FOODS:
                x = foods.get(fid)
                if not x or not x.get("p"):
                    continue
                g = pl / (float(x["p"]) / 100)
                kcal = g * float(x["kcal"]) / 100
                if kl > 0 and kcal > kl:                         # not enough calories left: as much as fits
                    g = kl / (float(x["kcal"]) / 100)
                    kcal = kl
                if g < 20:
                    continue
                if fid == "seed_whey_isolate":
                    sc = max(1, round(g / 30))
                    opts.append(dict(name=f"{sc} scoop{'s' if sc > 1 else ''} whey", g=sc * 30, kcal=round(sc * 30 * x["kcal"] / 100)))
                else:
                    opts.append(dict(name=x["en"].replace(" (cooked)", "").replace(" (drained)", ""), g=int(round(g / 10) * 10),
                                     kcal=round(kcal)))
                if len(opts) == 2:
                    break
            return dict(p=round(pl), k=round(kl), opts=opts)
        except Exception as e:
            print("macro tip:", e)
            return None

    def push_fit(self, f):
        self._fit = f
        self.js("fit", f)
        self.js("macroTip", self.macro_tip(f))
        try:
            self.sync_fit_log(f)
            self.push_data()
        except Exception as e:
            print("water mirror:", e)

    def sync_fit_log(self, f=None):
        """Keep the health plan's water log + goal equal to QAWIFit's, so streaks, reminders and history stay right.
        Also runs every 5 s (on_slow): after the 4 AM plan-day roll it catches up without waiting for a QAWIFit change."""
        f = self._fit if f is None else f
        m = self.model
        day = m.today()
        q = self.fit.live_water(day.isoformat())
        if q is None or not m.health.ok:
            return
        hm = m.health.water(day)
        if q != hm:
            m.add_water(day, q - hm)
        # one water goal: QAWIFit's (its Settings override, else weight x 35), same as the phone
        qg = int((f or {}).get("water_goal") or 0)
        if qg > 0 and (f or {}).get("date") == day.isoformat() and m.health.log(day).get("water_goal_ml") != qg:
            m.set_water_goal(day, qg)

    # ---------------------------------------------------------- QAWIFit logging (inbox + meal ticks)
    def meal_qawi(self, key, day):
        """(meal def for that day, its qawi block) for a planned meal that has QAWIFit contents."""
        m = self.model
        if not key.startswith("H:") or not m.health.ok:
            return None
        for raw in m.health.data.get("meals", []):
            if raw.get("id") == key[2:]:
                md = m.health.meal_for(raw, day)
                q = md.get("qawi") or {}
                if q.get("items") and not md.get("skip"):
                    return md, q
        return None

    def add_qawi(self, d):
        """Attach the tick preview (items + macros) to today's planned meals."""
        if self.day_offset != 0 or not self.inbox.ready():
            return
        day = self.model.today()
        for t in d["view"]["items"]:
            if t.get("cat") != "ME":
                continue
            r = self.meal_qawi(t["key"], day)
            if not r:
                continue
            try:
                t["qawi"] = self.inbox.preview(r[1]["items"])
            except Exception as e:
                print("meal preview", t["key"], e)
                continue
            t["qlogged"] = self.inbox.logged(f"tick-{day.isoformat()}-{t['key'][2:]}")

    def log_meal(self, key):
        day = self.model.today()
        r = self.meal_qawi(key, day)
        if not r or not self.inbox.ready():
            self.js("mealLogged", [key, False])
            return
        md, q = r
        req = dict(type="food", id=f"tick-{day.isoformat()}-{key[2:]}",
                   meal=dict(name=q.get("name") or md.get("name", "Meal"), tag=q.get("tag", "snack")), items=q["items"])
        self.inbox.submit(req, f"tick|{key}|{day.isoformat()}")

    def on_inbox(self, r):
        m = self.model
        label = str(r.get("from") or "")
        rid = str(r.get("request") or "")
        ok = bool(r.get("ok"))
        if label.startswith("tick|"):
            _, key, iso = label.split("|", 2)
            if ok:
                day = dt.date.fromisoformat(iso)
                items = {t["key"]: t for t in m.items(day)}
                if key in items and not items[key]["done"]:
                    m.toggle(items[key], day)
            self.js("mealLogged", [key, ok])
        if ok and r.get("type") == "undo" and rid.startswith("tick-") and not label.startswith("untick|"):
            iso, key = rid[5:15], "H:" + rid[16:]          # undone from a toast: untick the plan too
            try:
                day = dt.date.fromisoformat(iso)
                items = {t["key"]: t for t in m.items(day)}
                if key in items and items[key]["done"]:
                    m.toggle(items[key], day)
            except ValueError:
                pass
        if ok and r.get("type") == "food":
            left = (r.get("day") or {}).get("kcal_left")
            self.notify(f"inbox-{rid}-{time.time():.0f}", "ME", "QAWIFit · logged",
                        f"{r.get('meal')} · {r.get('kcal'):,} kcal · {r.get('protein')} g protein",
                        (f"{left:,} kcal left today · " if left is not None else "") + f"+{r.get('xp')} XP",
                        [("OK", None), ("Undo", lambda rid=rid: self.inbox.submit(dict(type="undo", target=rid), "undo"))],
                        25)
        elif ok and r.get("type") == "water":
            self.hud("water", "Water", r=f"{r.get('ml'):+d} ml (from Claude)", ms=1600)
        elif ok and r.get("type") == "weight":
            prev = r.get("prev")
            diff = f" · {r['kg'] - prev:+.1f} kg vs earlier today" if prev else ""
            self.notify(f"inbox-w-{rid}", "ME", "QAWIFit · weight", f"{r.get('kg'):g} kg logged{diff}",
                        f"+{r.get('xp')} XP" if r.get("xp") else "XP already given today",
                        [("OK", None), ("Undo", lambda rid=rid: self.inbox.submit(dict(type="undo", target=rid), "undo"))], 20)
        elif ok and r.get("type") == "undo" and r.get("kcal") == 0 and str(r.get("meal", "")).endswith(" kg"):
            self.notify(f"inbox-undo-{rid}-{time.time():.0f}", "ME", "QAWIFit · weight removed", str(r.get("meal")),
                        "", [("OK", None)], 12)
        elif ok and r.get("type") == "undo" and not label.startswith("untick|"):
            self.notify(f"inbox-undo-{rid}-{time.time():.0f}", "ME", "QAWIFit · removed",
                        f"{r.get('meal')} · {r.get('kcal')} kcal", f"{r.get('xp')} XP", [("OK", None)], 15)
        elif not ok:
            self.notify(f"inbox-err-{time.time():.0f}", "ME", "QAWIFit · not logged", str(r.get("error"))[:110],
                        "Nothing was written.", [("OK", None)], 30)
        self.push_data()

    def write_today(self, d):
        """today.json: a small snapshot of the day that any chat can read."""
        try:
            v = d["view"]
            snap = dict(
                about="Written by the island. Plan items, water and live QAWIFit numbers for today.",
                date=self.model.today().isoformat(), done_pct=v.get("pct"),
                plan=[dict(time=t.get("time"), title=t.get("title"), cat=CAT_NAME.get(t.get("cat"), t.get("cat")),
                           done=t.get("done"), state=t.get("state"), optional=t.get("optional"), key=t.get("key"),
                           tick=bool(t.get("key")))
                      for t in v["items"] if not t.get("block")],
                water=d.get("water"),
                qawifit={k: v2 for k, v2 in (self._fit or {}).items() if k != "why"} if (self._fit or {}).get("ok") else None,
            )
            js = json.dumps(snap, ensure_ascii=False, indent=1, default=str)
            if js != self._today_json:
                self._today_json = js
                tmp = os.path.join(HERE, "today.json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(js)
                os.replace(tmp, os.path.join(HERE, "today.json"))
        except Exception as e:
            print("today.json:", e)

    def add_water(self, day, ml):
        """Water goes to QAWIFit when connected (the plan log follows via push_fit), else to the plan."""
        if not self.fit.add_water(day.isoformat(), ml):
            self.model.add_water(day, ml)
            self.push_data()
        # QAWIFit path: fit.add_water already pushed (push_fit -> push_data) on this thread

    def on_action(self, name, arg):
        m = self.model
        if name == "ready":
            if not getattr(self, "_resync", False):
                print("ui ready (bridge connected)")
            self.ready = True
            self._away_js = None                                # fresh page: send "away" again
            if not getattr(self, "_voice_started", False):
                self._voice_started = True
                QTimer.singleShot(4000, self.voice_sync)        # hold-to-talk key + "Hey Island" (when installed)
                self.sayv_check(20_000)                         # which Mandarin / German voices Windows has
                if not getattr(self, "_memt", None):            # where the RAM goes (diagnostic, every 10 min)
                    self._memt = QTimer(self)
                    self._memt.timeout.connect(self.mem_report)
                    self._memt.start(10 * 60_000)
                    QTimer.singleShot(90_000, self.mem_report)
            self.js("dock", self.dock)
            if getattr(self, "popw", None) is not None and self.popw.ready:
                self.js("popped", "out")
            self.push_settings()
            self.push_data()
            if self.media.last:
                self._art_sent = None            # fresh page: send the full cover
                self.push_media(self.media.last)
            s = self.sys.sample()
            if s:
                self.js("sys", s)
            if getattr(self, "_wx", None):
                self.js("wx", self._wx)
            self.audio.watch(False)
            self.push_claude()
            self.js("compact", bool(self.state.get("compact")))
            if self._lyr:
                self.js("lyrics", self._lyr)
            sp, mm = getattr(self, "sp_pos", None), self.media.last or {}
            if sp and mm.get("title") and time.time() - sp[2] < 30:       # r44: a reloaded page starts on Spotify's exact clock
                self.js("spos", [round(sp[0] + (time.time() - sp[2] if sp[1] else 0), 2), sp[1], mm.get("title")])
            self.push_timers()
            self.load_launch()
            if WIN and not getattr(self, "_probed", False):
                self._probed = True
                threading.Thread(target=self.device_probe, daemon=True).start()
            QTimer.singleShot(3000, self.batt_sync)
            QTimer.singleShot(2500, self.desk_sync)
            QTimer.singleShot(600, lambda: getattr(self, "_adev", None) and self.js("adev", self._adev))
            if not getattr(self, "_lab_started", False):
                self._lab_started = True
                # QTimer.singleShot(120_000, self.leak_lab)    # the r18f self-test: answered (garbage, see main())
                QTimer.singleShot(20_000, self.gpu_report)
            self.buddy_arm()
            if getattr(self, "_devbat", None):
                self.push_devbat()
            if WIN and not self.apps_cat:
                QTimer.singleShot(8000, lambda: self.refresh_apps(False))
            if self.state.get("dash") and getattr(self, "dash", None) is None:
                scr = self.dash_screen()
                if scr is not None:
                    QTimer.singleShot(1500, lambda scr=scr: self.set_dash(scr))
            self.setup_drop()
            self.js("shelf", self.shelf_js())
            self.push_study(True)
            self.js("pomo", self.pomo_js())
            self.push_cards()
            self._prayer_js = None
            self.prayer.ensure(self.model.today())
            self.push_prayer()
            self.push_life()
            self.push_screen(True)
            self.js("history", self.history[::-1])
            self.lan_sync()
            if self._priv and self.settings.get("privacy_dots"):
                self.js("priv", self._priv)
            if self._bri:
                self.js("bri", self._bri)
            self.js("privacy", self.privacy)
            self._sysx = None
            if self.settings.get("glass"):
                self.apply_glass()
            self.update_meter()
            self._stale_sent = None
            self.push_stale()
            if not getattr(self, "_resync", False):
                QTimer.singleShot(120, self.fade_in)
                QTimer.singleShot(1500, self.after_crash)
            if self._snd:
                self.js("sound", self._snd)
            if self._fit:
                self.js("fit", self._fit)
        elif name == "ui_log":
            print("ui:", str(arg)[:300])
        elif name == "toast_flip":
            self.toast_flip(-1 if arg is not None and float(arg) < 0 else 1)
        elif name == "toast_clear":
            self.toast_clear()
        elif name == "drop_target":
            self._drop_target = "phone" if arg == "phone" else "shelf"
        elif name == "dtab":
            self._dtab = arg
            if arg == "bell":
                self.js("history", self.history[::-1])
            elif arg == "clip":
                self.js("clipList", self.clips[:12] if self.settings.get("clips") else [])
        elif name == "rect":
            x, y, w, h = (v * self.zoom for v in arg)
            self.rect = QRect(int(x) - 4, int(y) - 4, int(w) + 8, int(h) + 8)
        elif name == "mode":
            self.mode = arg
            if arg == "panel" or self.day_offset:
                # opening the panel shows today; leaving it while on another day goes back to today too
                # (otherwise today.json + meal previews stayed frozen until the panel was opened again)
                self.day_offset = 0
                self.push_data()
            if arg == "panel":
                self.activateWindow()
            elif arg == "pill":
                QTimer.singleShot(30_000, self.trim)
            self.update_meter()
        elif name == "press":
            self.press_at = QCursor.pos()
        elif name == "pop_player":
            self.pop_open(rect=arg)
        elif name == "pw_front":
            pw = getattr(self, "popw", None)
            if pw is not None:
                pw.front()
            else:
                self.js("popped", "closed")
        elif name in ("pw_in", "pw_close"):
            self.pop_close(name == "pw_in")
        elif name == "menu":
            self.menu()
        elif name == "toggle":
            day = m.today() + dt.timedelta(days=self.day_offset)
            items = {t["key"]: t for t in m.items(day)}
            if arg in items:
                rid = f"tick-{day.isoformat()}-{arg[2:]}"
                if items[arg]["done"] and str(arg).startswith("H:") and self.inbox.logged(rid):
                    self.inbox.submit(dict(type="undo", target=rid), f"untick|{arg}|{day.isoformat()}")
                m.toggle(items[arg], day)
                if not items[arg]["done"]:
                    self.drop_item_timer(arg)
            self.push_data()
        elif name == "say":
            self.say_word("ex" if arg == "ex" else "word")
        elif name == "voice_toggle":
            v = getattr(self, "voice", None)
            if v and v.rec:
                v.listen_stop()
            elif v:
                v.listen_start(auto=True)
            else:
                self.hud("info", "Turn on Voice in Settings", ms=1800)
        elif name == "imenu_do":
            self.imenu_do(*(arg if isinstance(arg, list) else [arg, None]))
        elif name == "swap" and isinstance(arg, list) and len(arg) == 2:
            self.swap_items(m.today() + dt.timedelta(days=self.day_offset), str(arg[0]), str(arg[1]))
        elif name == "move_reset":
            self.move_reset(m.today() + dt.timedelta(days=self.day_offset))
        elif name == "logmeal":
            self.log_meal(str(arg))
        elif name == "water":
            self.add_water(m.today(), int(arg))     # add_water already pushes the new state
        elif name == "ex":
            day = m.today() + dt.timedelta(days=self.day_offset)
            h = m.health
            sid = h.session_for(day, m.today())
            m.toggle_ex(day, sid, arg)
            self.push_data()
        elif name == "day":
            self.day_offset = int(arg)
            self.push_data()
        elif name == "tab":
            self.state["tab"] = arg
            if arg == "Alerts":
                self.js("history", self.history[::-1])
        elif name == "compact":
            self.state["compact"] = bool(arg)
            self.save_state()
            self.push_settings()
        elif name == "sound_watch":
            self.snd_pc = bool(arg)
            self.sound_sync()
        elif name == "vol":
            if arg[0] == "spdev":                                   # the Spotify device that's playing (phone, speaker ...)
                self.spotify.send_cmd("vol", v=max(0.0, min(1.0, float(arg[1]))))
                if getattr(self, "sp_rem", None):
                    self.sp_rem["vol"] = float(arg[1])
            elif str(arg[0]).startswith("bri:"):
                self.bright.set(int(str(arg[0])[4:]), arg[1])
            else:
                self.audio.set(arg[0], arg[1])
        elif name == "mute":
            self.audio.mute(arg[0], arg[1])
        elif name == "audio_dev" and arg:
            self.audio.set_device(arg)
        elif name in ("media_toggle", "media_next", "media_prev"):
            if getattr(self, "_media_remote", False) and self.sp_remote_on():
                self.spotify.send_cmd(name.split("_")[1])          # Spotify Connect: on the device that's playing
            else:
                self.media.command(name.split("_")[1])
        elif name in ("media_shuffle", "media_repeat", "media_like"):
            what = name.split("_")[1]
            if self.spotify_live():            # Spotify: through Spicetify (exact, and it can heart songs)
                self.spotify.send_cmd(what)
            elif what != "like":
                self.media.mode_cmd(what)
        elif name == "media_src":
            self.media.mode_cmd("src")
        elif name == "mview":
            self.music_view = str(arg)
        elif name == "launch":
            self.do_launch(arg)
        elif name == "apps_list":
            if self.apps_cat and time.time() - self.apps_t < 300:
                self.js("apps", self.apps_js())
            else:
                if self.apps_cat:
                    self.js("apps", self.apps_js())
                self.refresh_apps(True)
        elif name == "launch_pin":
            self.launch_pin(arg)
        elif name == "open_app":
            self.open_app(arg)
        elif name in ("launch_rm", "launch_move"):
            self.launch_edit(name, arg)
        elif name == "play_sound":
            self.sound(str(arg))
        elif name == "card" and self.cards is not None:
            try:
                self.cards.answer(str(arg[0]), bool(arg[1]), self.model.today())
            except Exception as e:
                print("card:", e)
            self.push_cards()
        elif name == "life_toggle" and self.life is not None:
            self.life.toggle(str(arg[0]), str(arg[1]), self.model.today())
            self.push_life()
        elif name == "life_rm" and self.life is not None:
            self.life.remove(str(arg[0]), str(arg[1]))
            self.push_life()
        elif name == "todo_clear" and self.life is not None:
            self.life.clear_done()
            self.push_life()
        elif name == "copy_today":
            self.copy_today()
        elif name == "cmd_open":
            self.open_cmd(str(arg or ""))
        elif name == "study_tick":
            today = m.today()
            items = {t["key"]: t for t in m.items(today)}
            if arg in items:
                m.toggle(items[arg], today)
            self.push_data()
        elif name == "big_pick" and self.life is not None:
            c = next((x for x in self.life.data.get("countdowns", []) if x.get("id") == str(arg)), None)
            b = self.life.data.get("big") or {}
            if c and b.get("date") == c.get("date") and b.get("title") == c.get("title"):
                self.life.set_big(None, None)
            elif c:
                self.life.set_big(c.get("title"), c.get("date"))
            self.push_life()
            self.push_data()
        elif name == "big_set":
            self.open_cmd("big ")
        elif name == "study_chat":
            self.open_chat(str(arg))
        elif name == "study_mock":
            self.start_mock(str(arg)) if str(arg) in island_study.SUBJ else None
        elif name == "study_score":
            self.open_cmd(f"score {arg} ")
        elif name == "study_exam":
            self.open_cmd(f"exam {arg} ")
        elif name in ("shelf_open", "shelf_dir", "shelf_rm", "shelf_drag", "shelf_clear"):
            self.shelf_act(name, arg)
        elif name == "media_seek":
            if getattr(self, "_media_remote", False) and self.sp_remote_on():
                self.spotify.send_cmd("seek", pos=float(arg))
            else:
                self.media.seek(float(arg))
        elif name == "toast":
            self.toast_pick(int(arg))
        elif name == "cmd":
            self.handle_cmd(str(arg or ""))
        elif name == "clip_pick":
            i = int(arg)
            if 0 <= i < len(self.clips):
                QApplication.clipboard().setText(self.clips[i])
                self.hud("ok", "Copied", ms=900)
        elif name == "mic_toggle":
            self.watch.cmd("mic")
        elif name == "hud_act":
            i = int(arg)
            cb = self.hud_cb[i] if 0 <= i < len(self.hud_cb) else None
            try:
                if cb:
                    cb()
            except Exception as e:
                print("hud action:", e)
        elif name == "timer_cancel":
            self.timers = [t for t in self.timers if t["id"] != arg]
            self.push_timers()
        elif name == "set":
            self.set_setting(str(arg[0]), arg[1])
        elif name == "shape":
            if WIN and self.settings.get("glass"):
                set_region(int(self.winId()), [v * self.zoom for v in arg[:4]] + [arg[4] * self.zoom])
        elif name == "do":
            self.do_action(str(arg))

    # ---------------------------------------------------------- fast loop: cursor, click-through, drag
    def on_fast(self):
        if not self.isVisible():
            return
        pos = QCursor.pos()
        local = pos - self.pos()
        inside = self.rect.contains(local)
        down = left_button_down()
        # adaptive polling: 60 Hz near the island or while pressing/dragging, ~8 Hz when the cursor is far away
        near = self.rect.adjusted(-220, -220, 220, 220).contains(local)
        if self.ready and (self.settings.values.get("glow", True) or self.settings.values.get("autohide")
                           or self.settings.values.get("pet", True)):
            self.send_near(local, inside, near)
        want_ms = 16 if (near or down or self.dragging or self.press_at is not None or self.mode != "pill") else 120
        if self.fast.interval() != want_ms:
            self.fast.setInterval(want_ms)
        # drag start: pressed on the island and moved
        if self.press_at is not None and not self.dragging:
            if not down:
                self.press_at = None
            elif (pos - self.press_at).manhattanLength() > 7:
                self.start_drag()
        if self.dragging:
            if down:
                self.move(pos - self.grab)
            else:
                self.end_drag(pos)
            return
        if WIN and self.mode == "toast" and inside:
            dn = bool(user32.GetAsyncKeyState(0x2E) & 0x8000)          # VK_DELETE
            if dn and not getattr(self, "_del_was", False):
                self.toast_clear()
            self._del_was = dn
        # click-through everywhere except the island
        want = not inside and self.mode != "cmd"
        if want and down and self.prev_button and self.click_through is False:
            want = False      # a drag that started on the island (volume / song bar) keeps the mouse until release
        if want != self.click_through:
            self.click_through = want
            set_click_through(self, want)
            if want:
                self.js("outside")
        # click outside an open panel closes it
        if down and not self.prev_button and not inside and self.mode in ("panel", "sound", "cmd", "music", "shelf"):
            self.js("clickOutside")
        self.prev_button = down

    def start_drag(self):
        self.dragging = True
        self.press_at = None
        self.drag_from = self.skey(self.screen())
        self.js("drag", True)
        # keep the cursor at the same spot on the island; the window is moved under it
        self.grab = QCursor.pos() - self.pos()

    def end_drag(self, pos):
        self.dragging = False
        scr = QGuiApplication.screenAt(pos) or self.screen()
        g = scr.geometry()
        zone = max(140, int(g.width() * 0.12))
        r = getattr(self, "rect", None)
        cx = self.x() + (r.center().x() if r is not None else self.width() // 2)
        cy = self.y() + (r.center().y() if r is not None else self.height() // 2)
        if pos.y() - g.top() < max(110, int(g.height() * 0.12)):     # along the top, corners included: stays on top
            dock, a = "top", cx
            if abs(cx - g.center().x()) < max(110, int(g.width() * 0.09)):
                a = None                     # r43: near the middle = snaps to dead centre, where a real island sits
        elif pos.x() - g.left() < zone:
            dock, a = "left", cy
        elif g.right() - pos.x() < zone:
            dock, a = "right", cy
        else:
            dock, a = "top", cx
        self.state.update(dock=dock, anchor=a, screen=scr.name(), screen_xy=[scr.geometry().left(), scr.geometry().top()])
        self.save_state()
        if getattr(self, "drag_from", None) is not None and self.skey(scr) != self.drag_from:
            self.ask_screen()                # dropped on another screen: keep it there, or keep following the mouse?
        if self.toast_until:                 # a notification you just moved stays up a moment longer
            self.toast_until = max(self.toast_until, dt.datetime.now() + dt.timedelta(seconds=4))
        start = self.geometry()
        end = self.target_geom(dock, scr, a)
        dock_changed = True
        self.js("drag", False)
        self.js("dock", dock)
        self.push_shift(end, dock, a)
        anim = QVariantAnimation(self)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setDuration(380)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.valueChanged.connect(lambda r: self.setGeometry(r))
        anim.start()
        self.anim = anim

    # ---------------------------------------------------------- follow the mouse across screens
    def fs_track(self):
        """Monitor that has a fullscreen app on it, or None. Sticky: it stays 'fullscreen' while that app stays
        fullscreen, even when you click something on the other screen - until you leave fullscreen, close or
        minimise it, or switch to another app on that same screen."""
        if not WIN:
            return None
        own = self.own_hwnds()
        fg = user32.GetForegroundWindow()
        fg = int(fg) if fg else 0
        mon = fullscreen_monitor(own)
        if mon is not None:
            self.fs_keep = (fg, mon)
            return mon
        k = getattr(self, "fs_keep", None)
        if not k:
            return None
        if fg and fg not in own and fg != k[0] and monitor_of(fg) == k[1] and class_of(fg) not in SWITCHERS:
            self.fs_keep = None            # you're using another app on that screen now
            return None
        m = still_fullscreen(k[0])
        self.fs_keep = (k[0], m) if m is not None else None
        return m

    def ask_screen(self):
        if WIN and self.settings.values.get("follow", True) and len(QGuiApplication.screens()) > 1:
            self.screen_ask = dict(until=time.monotonic() + 45, shown=False)

    def keep_screen(self):
        self.screen_ask = None
        self.set_setting("follow", False)
        self.hud("ok", "Staying on this screen", act=["Undo"], ms=4000,
                 cb=[lambda: (self.set_setting("follow", True), self.hud("ok", "Following your mouse", ms=1400))])

    def keep_following(self):
        self.screen_ask = None
        self.hud("ok", "Following your mouse", ms=1400)

    def follow_tick(self):
        if not (WIN and self.ready and self.settings.values.get("follow", True)) or self.dragging:
            return
        ask = getattr(self, "screen_ask", None)
        if ask:                              # waiting for your answer: the island doesn't move meanwhile
            if time.monotonic() > ask["until"]:
                self.screen_ask = None       # no answer: it keeps following, like before
            elif not ask["shown"] and self.mode == "pill":
                ask.update(shown=True, until=time.monotonic() + 12)
                self.hud("screen", "Stay on this screen?", ms=11500, act=["Keep it here", "Follow my mouse"],
                         cb=[self.keep_screen, self.keep_following])
            return
        scrs = QGuiApplication.screens()
        if len(scrs) < 2:
            return
        fsmon = self.fs_track()
        here = self.screen()
        cur = QGuiApplication.screenAt(QCursor.pos())
        forced = False
        if fsmon is not None and (cur is None or cursor_monitor() == fsmon):
            # the mouse is on the fullscreen screen: the island waits on another screen until you leave fullscreen
            if screen_monitor(here) != fsmon:
                want = here
            else:
                want = next((x for x in scrs if screen_monitor(x) != fsmon), None)
                forced = True
        else:
            want = cur
        if want is None or self.skey(want) == self.skey(here):
            self.follow_want = None
            return
        if not forced:
            if self.mode in ("panel", "sound", "cmd", "shelf") or (self.mode == "music" and getattr(self, "music_view", "pill") == "full"):
                # never pull an open panel away from you (the small lyrics player does follow)
                return
            now = time.monotonic()
            if getattr(self, "follow_want", None) != self.skey(want):
                self.follow_want, self.follow_t = self.skey(want), now   # the mouse must rest there a moment
                return
            if now - self.follow_t < 0.35:
                return
        self.follow_want = None
        self.move_to_screen(want, "fullscreen" if forced else "mouse")

    def move_to_screen(self, scr, why=""):
        old = self.screen()
        go, gn = old.geometry(), scr.geometry()
        a = self.state.get("anchor")
        if a is not None:        # same spot along the edge, as a fraction of the screen
            if self.dock == "top":
                a = int(gn.left() + (a - go.left()) / max(1, go.width()) * gn.width())
            else:
                a = int(gn.top() + (a - go.top()) / max(1, go.height()) * gn.height())
        self.state.update(anchor=a, screen=scr.name(), screen_xy=[gn.left(), gn.top()])
        self.save_state()
        f = getattr(self, "_fade", None)
        if f:
            f.stop()
        self.setWindowOpacity(0.0)
        self.setGeometry(self.target_geom(scr=scr, anchor=a))
        self.push_shift(anchor=a)
        self.click_through = None
        f = QVariantAnimation(self)
        f.setStartValue(0.0)
        f.setEndValue(1.0)
        f.setDuration(240)
        f.valueChanged.connect(lambda v: self.setWindowOpacity(float(v)))
        f.start()
        self._fade = f
        if why == "fullscreen" or getattr(self, "_follow_logged", 0) < 3:
            self._follow_logged = getattr(self, "_follow_logged", 0) + 1
            print(f"follow: -> {scr.name()} ({why})")

    # ---------------------------------------------------------- slow loops
    def on_second(self):
        self.follow_tick()
        v = getattr(self, "voice", None)
        if v and v.wake_on and int(time.time()) % 2 == 0:
            v.game_pause = bool(self.hidden_fs or self.fg_game())      # "Hey Island" sleeps while a game is in front
        if getattr(self, "_brain_gpu", False) and int(time.time()) % 5 == 0 and (self.hidden_fs or self.fg_game()):
            self._brain_gpu = False                                    # the brain sat on the GPU: free it for the game
            m = getattr(self, "_brain_last", None)
            if m:
                threading.Thread(target=island_brain.unload, args=(m,), daemon=True).start()
                print("brain: game in front, unloaded", m)
        if getattr(self, "_sp_now", None) and self._sp_now.get("on") != self.sp_live():
            self.sp_now_push()          # Spotify closed / opened: the deck's library says so
        # hide over fullscreen apps on the same monitor
        if WIN:
            fs = fullscreen_monitor(self.own_hwnds())
            mine = monitor_of(int(self.winId()))
            hide = fs is not None and fs == mine and not self.dragging
            pw = getattr(self, "popw", None)
            if pw is not None and pw.ready:
                ph = fs is not None and fs == monitor_of(int(pw.winId()))
                if ph != pw.fs_hidden:                 # a topmost window can knock a game out of exclusive fullscreen
                    pw.fs_hidden = ph
                    pw.hide() if ph else pw.front()
            if hide != self.hidden_fs:
                # really hide the window (a topmost overlay can knock games out of exclusive fullscreen)
                self.hidden_fs = hide
                self.media.idle = hide      # nobody sees it: poll media slower, skip system stats
                self.update_meter()
                self.sync_hook()
                if hide:
                    self.hide()
                    QTimer.singleShot(2500, self.page_freeze)
                    self.batt_sync()
                    self._game_t0, self._game_peak = time.time(), 0
                    self.desk_sync()
                else:
                    self.page_thaw()
                    self.show()
                    QTimer.singleShot(4000, self.batt_sync)
                    QTimer.singleShot(1500, self.desk_sync)
                    t0, self._game_t0 = getattr(self, "_game_t0", None), None
                    if t0 and time.time() - t0 > 15 * 60:
                        mins = int((time.time() - t0) / 60)
                        pk = getattr(self, "_game_peak", 0)
                        QTimer.singleShot(2500, lambda m=mins, p=pk: self.hud(
                            "game", f"Played {m // 60}h {m % 60:02d}m" if m >= 60 else f"Played {m} min",
                            r=f"CPU peaked at {p}°" if p else "welcome back", ms=4200))
                    self.click_through = None
                    if self.media.loop and getattr(self.media, "wake", None):   # fresh song info right away
                        self.media.loop.call_soon_threadsafe(self.media.wake.set)
                    if self.toasts:
                        self.drop_stale_toasts()
        s = None if self.hidden_fs else self.sys.sample()
        if self.hidden_fs and getattr(self, "_game_t0", None):
            self.temps()
        if s:
            s["temp"] = self.temps()
            self._last_sys = s
        if s and self.ready:
            self.js("sys", s)
        self.second_r3(s)
        if self.ready and int(time.time()) % 5 == 3:      # 10+ min without input: ui.html pauses its endless animations
            away = idle_seconds() >= 600
            if away != getattr(self, "_away_js", None):
                self._away_js = away
                self.js("away", away)
                self.update_meter()                         # the bars are paused while away: stop reading the level too
        try:
            self.study_tick()
            if int(time.time()) % 15 == 7:
                self.next_tick()
            self.mock_tick()
            self.block_tick()
            self.day_tick()
            self.dev_tick()
            if int(time.time()) % 5 == 0:
                self.screen_tick()
            if int(time.time()) % 15 == 0:
                self.usb_tick()
            if int(time.time()) % 2 == 0:
                self.game_tick()
            if getattr(self, "_media_remote", False) and not self.sp_remote_on():     # Spotify closed / stopped elsewhere
                self._push_media(self.media_merged(getattr(self, "_win_media", None)))
        except Exception as e:
            if getattr(self, "_study_err", "") != str(e):
                self._study_err = str(e)
                print("study tick:", e)
        if self.claude_unread and foreground_exe() == "claude.exe":
            self.claude_unread, self.claude_ask = 0, False      # you're looking at Claude: clear the badge
            self.push_claude()
        try:
            self.inbox.scan()
        except Exception as e:
            print("inbox scan:", e)
        # toast timeout
        if self.toast_until and dt.datetime.now() >= self.toast_until and not self.dragging:
            self.toast_next()

    def on_slow(self):
        if not self.ready:
            return
        try:
            self.sync_fit_log()
        except Exception as e:
            print("water mirror:", e)
        self.reminders()
        self.push_data()
        self.push_stale()
        if (getattr(self, "_update_pending", False) and self.mode == "pill" and not self.toasts
                and not self.dragging and not self.hidden_fs and idle_seconds() > 600):
            print("new code waiting + away for 10 min -> restarting quietly")
            self.restart()

    def weather_loop(self):
        while True:
            if not self.settings.get("weather", False):        # off (r20): nothing fetched, nothing shown
                if getattr(self, "_wx", None):
                    self._wx = None
                    self.wx_sig.emit(None)
                time.sleep(60)
                continue
            try:
                self._wx = fetch_weather()
                self._wx_at = time.time()
                self.wx_sig.emit(self._wx)
                time.sleep(1800)
            except Exception as e:
                print("weather:", e)
                time.sleep(300)

    # ---------------------------------------------------------- notifications
    def push_claude(self):
        self.js("claude", dict(n=self.claude_unread, ask=self.claude_ask))

    def open_claude(self):
        self.claude_unread, self.claude_ask = 0, False
        self.push_claude()
        if not focus_app("claude.exe"):
            try:
                os.startfile("claude://")
            except Exception as e:
                print("open claude:", e)

    MIRROR = {"mirror_whatsapp": ["whatsapp"], "mirror_discord": ["discord"], "mirror_outlook": ["outlook"],
              "mirror_phone": ["phone link", "your phone"]}

    def mirror_apps(self):
        out = []
        for k, names in self.MIRROR.items():
            if self.settings.get(k):
                out += names
        return out

    def on_note(self, e):
        texts = e.get("texts") or []
        k = e.get("kind")
        app = str(e.get("app") or "")
        if "claude" not in app.lower() and any(x in app.lower() for x in self.mirror_apps()):
            # another app you picked: show it on the island (no extra sound - the app plays its own)
            self.notify(f"mirror-{time.time():.3f}", "MI", app[:24], texts[0][:80] if texts else app,
                        " · ".join(texts[1:])[:160], [("OK", None)], 15, kind="mirror")
            return
        self.claude_unread += 1
        self.claude_ask = self.claude_ask or k == "ask"
        self.push_claude()
        kicker = {"ask": "Claude · needs you", "done": "Claude · reply ready"}.get(k, "Claude")
        title = texts[0] if texts else "Claude"
        body = " · ".join(texts[1:])
        self.notify(f"claude-{time.time():.3f}", "CL", kicker, title, body,
                    [("Open", self.open_claude), ("OK", None)], 60 if k == "ask" else 30,
                    kind="nag" if k == "ask" else "info")

    def notify(self, key, cat, kicker, title, body="", actions=None, timeout=40, kind="info", body2="", item=""):
        if any(t["key"] == key for t in self.toasts):
            return
        if cat in ("WA", "ME", "V") and getattr(self, "prayer", None) is not None and self.fasting():
            return                                    # Ramadan: no food or water reminders while fasting
        quiet = kind not in ("prayer", "timer") and self.quiet_now()
        hist = getattr(self, "history", None)
        if hist is not None:
            hist.append(dict(at=int(time.time() * 1000), cat=cat, kicker=kicker, title=str(title)[:120],
                             body=str(body)[:200], quiet=quiet))
            del hist[:-50]
            if self.ready:
                self.js("history", hist[::-1])
        if quiet:
            return                                    # quiet hours: kept in the bell tab, no toast
        self.toasts.append(dict(key=key, cat=cat, kicker=kicker, title=title, body=body, body2=body2,
                                actions=actions or [("OK", None)], timeout=timeout, kind=kind, born=time.time(), item=item))
        self.deck_note(self.toasts[-1])
        if len(self.toasts) == 1:
            self.toast_return = "panel" if self.mode == "panel" else "pill"
            self.toast_show()

    def drop_stale_toasts(self):
        """Back from a fullscreen app: skip reminders that went stale while hidden (no burst of old chimes)."""
        iso = self.model.today().isoformat()
        now = time.time()
        keep = [t for t in self.toasts
                if now - t.get("born", now) < 15 * 60 or (t.get("kind") == "nag" and t["key"].startswith(iso))]
        for t in self.toasts:
            if t not in keep:
                self.deck_push("notedone", t["key"])
        dropped = len(self.toasts) - len(keep)
        self.toasts = keep
        if dropped:
            print(f"toasts: skipped {dropped} stale after fullscreen")
        if self.toasts:
            self.toast_show()
        else:
            self.toast_until = None
            self.js("toastDone", "pill")

    def toast_show(self, quiet=False):
        if self.hidden_fs or not self.toasts or self.mock_on():     # mock exam: notifications wait until it's over
            return
        t = self.toasts[0]
        self.toast_cb = [a[1] for a in t["actions"]]
        self.toast_at = time.time()          # toast_pick ignores clicks in the first 0.4 s (double-click on the last one)
        self.toast_until = dt.datetime.now() + dt.timedelta(seconds=t["timeout"])
        if not quiet:
            self.sound("claude" if t["cat"] == "CL" else t["kind"])
        self.js("toast", dict(cat=t["cat"], kicker=t["kicker"], title=t["title"], body=t["body"], body2=t["body2"],
                              actions=[a[0] for a in t["actions"]], timeout=t["timeout"], more=len(self.toasts) - 1))

    def toast_flip(self, d):
        """Mouse wheel over a stack of notifications: the next (or previous) one comes to the front."""
        if len(self.toasts) < 2:
            return
        if d < 0:
            self.toasts.insert(0, self.toasts.pop())
        else:
            self.toasts.append(self.toasts.pop(0))
        self.toast_show(quiet=True)

    def toast_clear(self):
        """'clear all' (or Delete while the mouse is on the island): every waiting notification goes."""
        if not self.toasts:
            return
        for t in self.toasts:
            self.deck_push("notedone", t["key"])
        print(f"toasts: cleared {len(self.toasts)}")
        self.toasts = []
        self.toast_cb = []
        self.toast_until = None
        self.js("toastDone", self.toast_return)

    def toast_pick(self, i):
        if not self.toasts or time.time() - getattr(self, "toast_at", 0) < 0.4:
            return                       # a double-click must not run a button twice (e.g. +1000 ml logged twice)
        cb = self.toast_cb[i] if i < len(self.toast_cb) else None
        try:
            if cb:
                cb()
        except Exception as e:
            print("toast action:", e)
        self.push_data()
        self.toast_next()

    def toast_next(self):
        if self.toasts:
            self.deck_push("notedone", self.toasts.pop(0)["key"])
        self.toast_cb = []
        self.toast_until = None
        if self.toasts:
            self.toast_show()
        else:
            self.js("toastDone", self.toast_return)

    def one_per_item(self, ik):
        """One reminder per plan item: False if that item's toast is on screen now; drops older queued ones for it."""
        q = self.toasts
        showing = bool(q) and self.toast_until is not None
        if showing and q[0].get("item") == ik:
            return False
        for x in [x for i, x in enumerate(q) if x.get("item") == ik and not (i == 0 and showing)]:
            q.remove(x)
            self.deck_push("notedone", x["key"])
        return True

    def snooze_kicker(self, t, day):
        """Wording for a snoozed reminder at the moment it comes back (not the stale 'in 10 min' from before)."""
        cat = CAT_NAME.get(t["cat"], "")
        try:
            s, _ = self.model.span(t, day)
            mins = round((s - dt.datetime.now()).total_seconds() / 60)
            when = f"in {fmt_min(mins)}" if mins >= 1 else ("now" if mins > -1 else f"{fmt_min(-mins)} late")
            return " · ".join(x for x in ("snoozed", cat, when, h12(t["time"])) if x)
        except Exception:
            return " · ".join(x for x in ("snoozed", cat) if x)

    def item_toast(self, key, t, kicker, timeout=45, snoozed=False):
        m = self.model
        day = m.today()
        ik = t["key"]
        snz = self.__dict__.setdefault("_snz", {})
        if not snoozed:
            snz.pop(ik, None)                  # a fresh reminder (e.g. "starts now") replaces a pending snooze
        if not self.one_per_item(ik):
            return

        def done():
            if not m.is_done_now(t, day):
                m.toggle(t, day)

        def snooze():
            tok = time.time()
            snz[ik] = tok                      # only the newest snooze for this item may come back

            def again():
                if snz.get(ik) != tok:
                    return                     # another reminder for it showed since, or it was snoozed again
                snz.pop(ik, None)
                if m.today() != day:
                    return
                cur = next((x for x in m.items(day) if x["key"] == ik), t)     # moved/swapped since? use the new time
                if not m.is_done_now(cur, day):
                    self.item_toast(f"{day.isoformat()}-snz-{ik}-{int(tok)}", cur, self.snooze_kicker(cur, day),
                                    timeout, snoozed=True)
            QTimer.singleShot(10 * 60_000, again)
        acts = [("Done ✓", done), ("Snooze 10m", snooze)]
        if t.get("cat") == "ME" and self.meal_qawi(t["key"], day) and self.inbox.ready():
            acts = [("Log + tick", lambda: self.log_meal(t["key"])), ("Done ✓", done), ("Snooze 10m", snooze)]
        self.notify(key, t["cat"], kicker, t["title"] + (f"  ·  {fmt_min(t['dur'])}" if t["src"] == "S" else ""),
                    h12_text(t.get("desc", "")), acts, timeout, item=ik)

    # ---------------------------------------------------------- start -> done (r47)
    @staticmethod
    def two_step(t):
        """A plan item with a length (a study session, the gym, a meal): asked "start?" when it begins and "done?" when
        its time is up. Vitamins and quick ones keep the one "Done" reminder."""
        return t.get("cat") != "V" and int(t.get("dur") or 0) >= 10 and not t.get("block")

    def item_timer(self, ik):
        return next((x for x in self.timers if x.get("item") == ik), None)

    def drop_item_timer(self, ik):
        if self.item_timer(ik):
            self.timers = [x for x in self.timers if x.get("item") != ik]
            self.push_timers()

    def start_item(self, t, day, mins=None):
        """Start: its own countdown on the island (the session's length); when it ends the island asks if it's done."""
        ik = t["key"]
        self.drop_item_timer(ik)
        mins = int(mins or t.get("dur") or 30)
        self.timers.append(dict(id=f"i{int(time.time() * 1000)}", end=time.time() + mins * 60, total=mins * 60,
                                label=t["title"], item=ik, day=day.isoformat()))
        self.push_timers()
        self.state.setdefault("started", {})[ik] = [day.isoformat(), time.time()]
        self.state["started"] = {k: v for k, v in self.state["started"].items() if v and v[0] >= (day - core.DAY).isoformat()}
        self.save_state()
        self.hud("timer", t["title"], r=fmt_min(mins), ms=2200)
        print(f"plan: started {t['title']} ({mins} min)")

    def start_toast(self, key, t, kicker, timeout=60, snoozed=False):
        """It's time: "Maths — start?"  Start ▶ / Snooze 10m / Skip."""
        m = self.model
        day = m.today()
        ik = t["key"]
        snz = self.__dict__.setdefault("_snz", {})
        if not snoozed:
            snz.pop(ik, None)
        if not self.one_per_item(ik):
            return

        def snooze():
            tok = time.time()
            snz[ik] = tok

            def again():
                if snz.get(ik) != tok:
                    return
                snz.pop(ik, None)
                if m.today() != day or self.item_timer(ik):
                    return
                cur = next((x for x in m.items(day) if x["key"] == ik), t)
                if not m.is_done_now(cur, day):
                    self.start_toast(f"{day.isoformat()}-ssnz-{ik}-{int(tok)}", cur, self.snooze_kicker(cur, day), timeout, snoozed=True)
            QTimer.singleShot(10 * 60_000, again)
        body = " · ".join(x for x in (h12_text(t.get("desc", "")), fmt_min(t["dur"]) if t.get("dur") else "") if x)
        self.notify(key, t["cat"], kicker, f"{t['title']} — start?", body,
                    [("Start ▶", lambda: self.start_item(t, day)), ("Snooze 10m", snooze), ("Skip", None)], timeout, item=ik)

    def done_toast(self, key, t, kicker, timeout=120):
        """Its time is up: "Maths — done?"  Done ✓ / +15 min / Not done."""
        m = self.model
        day = m.today()
        ik = t["key"]
        if not self.one_per_item(ik):
            return

        def done():
            self.drop_item_timer(ik)
            if not m.is_done_now(t, day):
                m.toggle(t, day)
            self.push_data()

        acts = [("Done ✓", done), ("+15 min", lambda: self.start_item(t, day, 15)), ("Not done", None)]
        if t.get("cat") == "ME" and self.meal_qawi(t["key"], day) and self.inbox.ready():
            acts = [("Log + tick", lambda: (self.drop_item_timer(ik), self.log_meal(t["key"]))), ("Done ✓", done), ("+15 min", acts[1][1])]
        self.notify(key, t["cat"], kicker, f"{t['title']} — done?", h12_text(t.get("desc", "")), acts, timeout, item=ik)

    def water_toast(self, key, per, left, snoozed=False):
        m = self.model
        day = m.today()
        ml, goal = m.water(day)
        snz = self.__dict__.setdefault("_snz", {})
        if not snoozed:
            snz.pop("water", None)             # the hourly reminder replaces a pending water snooze
        if not self.one_per_item("water"):
            return

        def snooze():
            tok = time.time()
            snz["water"] = tok

            def again():
                if snz.get("water") != tok:
                    return
                snz.pop("water", None)
                now_ml, now_goal = m.water(day)         # fresh numbers: he may have drunk since the first toast
                if now_ml < now_goal and m.today() == day:
                    self.water_toast(f"{day.isoformat()}-wsnz-{int(tok)}", min(per, now_goal - now_ml),
                                     now_goal - now_ml, snoozed=True)
            QTimer.singleShot(15 * 60_000, again)
        self.notify(key, "WA", f"water · {ml / 1000:.1f} / {goal / 1000:.1f} L", f"Drink ~{per} ml now",
                    f"{left / 1000:.2f} L left today.",
                    [(f"+{per} ml", lambda: self.add_water(day, per)), ("+250", lambda: self.add_water(day, 250)),
                     ("Snooze 15m", snooze)], timeout=30, kind="water", item="water")

    def reminders(self):
        m = self.model
        m.reload()
        now = dt.datetime.now()
        today = m.today()
        iso = today.isoformat()
        prev = (today - core.DAY).isoformat()
        self.fired = {k for k in self.fired if k.startswith(iso) or k.startswith(prev)}
        early = int(m.health.data.get("early_warning_min", 10)) if m.health.ok else 10
        grace = dt.timedelta(minutes=10)
        changed = False

        def due(when, key):
            nonlocal changed
            if key in self.fired:
                return False
            if when <= now < when + grace:
                self.fired.add(key)
                changed = True
                return True
            return False

        for t in m.items(today):
            if not t.get("time") or t["done"]:
                continue
            s, _ = m.span(t, today)
            if t["block"]:
                if not t.get("remind"):
                    continue
                body = (h12_text(t["desc"]) + " · " if t.get("desc") else "") + f"{h12(t['time'])} – {h12(t['end'])}"
                rw = (getattr(self, "_wx", None) or {}).get("rain")
                if t["cat"] == "T" and rw and rw.get("in_h", 99) <= 8:
                    body += f" · ☂ rain likely around {rw['at']}"
                if early and due(s - dt.timedelta(minutes=early), f"{iso}-early-{t['key']}"):
                    self.notify(f"{iso}-early-{t['key']}", t["cat"], f"in {early} min · {h12(t['time'])}", t["title"], body,
                                [("OK", None)], 30)
                if due(s, f"{iso}-now-{t['key']}"):
                    self.notify(f"{iso}-now-{t['key']}", t["cat"], f"{CAT_NAME.get(t['cat'], '')} · now", t["title"],
                                body, [("OK", None)], 40)
                continue
            if t["optional"]:
                continue
            two = self.two_step(t)
            cn = CAT_NAME.get(t["cat"], "")
            if t["cat"] != "V" and early and due(s - dt.timedelta(minutes=early), f"{iso}-early-{t['key']}"):
                if two:                         # r47: a heads-up only; "start?" comes at its time
                    self.notify(f"{iso}-early-{t['key']}", t["cat"], f"{cn} · in {early} min · {h12(t['time'])}", t["title"],
                                h12_text(t.get("desc", "")), [("OK", None)], 30, item=t["key"])
                else:
                    self.item_toast(f"{iso}-early-{t['key']}", t, f"{cn} · in {early} min · {h12(t['time'])}", 30)
            if due(s, f"{iso}-now-{t['key']}"):
                if two:
                    if not self.item_timer(t["key"]):
                        self.start_toast(f"{iso}-now-{t['key']}", t, f"{cn} · now · {h12(t['time'])}", 60)
                else:
                    self.item_toast(f"{iso}-now-{t['key']}", t, f"{cn} · starts now", 60)
            if two:                             # never pressed Start: still asks when its planned time is over
                st = (self.state.get("started") or {}).get(t["key"])
                _, e = m.span(t, today)
                if e and not (st and st[0] == iso) and not self.item_timer(t["key"]) and due(e, f"{iso}-end-{t['key']}"):
                    self.done_toast(f"{iso}-end-{t['key']}", t, f"{cn} · time's up")

        cfg = m.health.water_cfg if m.health.ok else {}
        goal = m.health.goal_ml(today) if m.health.ok else 0
        if goal:
            win = cfg.get("window", ["10:00", "00:00"])
            s, e = m.at(today, win[0]), m.at(today, win[1])
            if e <= s:
                e += core.DAY
            every = dt.timedelta(minutes=int(cfg.get("remind_every_min", 60)))
            ml, _ = m.water(today)      # live QAWIFit total when connected, else the plan log
            t = s
            while t <= e:
                key = f"{iso}-water-{t:%H%M}"
                if ml < goal and due(t, key):
                    left = goal - ml
                    slots = max(1, int((e - now) / every) + 1)
                    per = max(250, int(math.ceil(left / slots / 50.0) * 50))
                    self.water_toast(key, min(per, left), left)
                t += every

        nag = m.health.data.get("nag_time", "23:45") if m.health.ok else "23:00"
        if due(m.at(today, nag), f"{iso}-nag"):
            left = [t for t in m.items(today) if t["tickable"] and not t["done"] and not t["optional"]]
            ml, goal = m.water(today)
            lines = [t["title"] for t in left]
            if goal and ml < goal:
                lines.append(f"Water {(goal - ml) / 1000:.1f} L short")
            if lines:
                fire = m.fire_streak(today - core.DAY)
                self.notify(f"{iso}-nag", "W", "streak check" + (f" · {fire}-day streak on the line" if fire else ""),
                            f"{len(lines)} thing{'s' if len(lines) > 1 else ''} left today", " · ".join(lines[:5]),
                            [("Open", lambda: QTimer.singleShot(500, lambda: self.js("mode", "panel"))),
                             ("Later", None)], 90, kind="nag")
        if changed:
            self.save_state()

    # ---------------------------------------------------------- menu
    def menu(self):
        """Right-click: the island itself opens into a menu card (the tray icon keeps the classic Windows menu)."""
        if not self.ready:
            mn = QMenu()
            self.fill_menu(mn)
            mn.exec(QCursor.pos())
            return
        prim = QGuiApplication.primaryScreen()
        dash = self.state.get("dash")
        scr = [dict(i=i, w=s.geometry().width(), h=s.geometry().height(), main=s == prim, here=self.skey(s) == self.skey(self.screen()),
                    dash=bool(dash) and dash[0] == s.name() and dash[1] == s.geometry().left())
               for i, s in enumerate(QGuiApplication.screens())]
        v = getattr(self, "voice", None)
        self.js("imenu", dict(dock=self.dock, compact=bool(self.state.get("compact")), follow=bool(self.settings.get("follow", True)),
                              autohide=bool(self.settings.get("autohide")), screens=scr, voice=bool(self.settings.get("voice", True)),
                              voice_ok=island_voice.voice_ready()[0], listening=bool(v and v.rec)))

    def imenu_do(self, what, arg=None):
        scr = QGuiApplication.screens()
        if what == "panel":
            self.js("mode", "panel")
        elif what == "settings":
            self.js("openTab", "Settings")
        elif what == "cmd":
            self.open_cmd("")
        elif what == "voice":
            self.on_action("voice_toggle", None)
        elif what == "test":
            self.test_toast()
        elif what == "refresh":
            self.reload_all()
        elif what == "restart":
            self.restart()
        elif what == "quit":
            QApplication.quit()
        elif what == "dock" and arg in ("top", "left", "right"):
            self.set_dock(arg)
        elif what == "screen" and isinstance(arg, int) and 0 <= arg < len(scr):
            self.set_screen(scr[arg])
        elif what == "dash":
            self.set_dash(scr[arg] if isinstance(arg, int) and 0 <= arg < len(scr) else None)
        elif what == "compact":
            self.set_compact(bool(arg))
        elif what in ("follow", "autohide"):
            if what == "follow":
                self.screen_ask = None
            self.set_setting(what, bool(arg))

    def fill_menu(self, mn):
        mn.clear()
        mn.setStyleSheet("""
            QMenu { background: #12141c; color: #f0f2f8; border: 1px solid rgba(255,255,255,30);
                    border-radius: 10px; padding: 6px; font-size: 13px; }
            QMenu::item { padding: 6px 18px; border-radius: 6px; }
            QMenu::item:selected { background: #262a3a; }
            QMenu::separator { height: 1px; background: rgba(255,255,255,25); margin: 5px 8px; }""")
        mn.addAction("Open panel", lambda: self.js("mode", "panel"))
        mn.addAction("Settings", lambda: self.js("openTab", "Settings"))
        mn.addAction("Command bar  (Win+Alt+Space)", lambda: self.open_cmd(""))
        mn.addAction("Test notification", self.test_toast)
        a = QAction("Compact island", mn, checkable=True)
        a.setChecked(bool(self.state.get("compact")))
        a.triggered.connect(lambda on: self.set_compact(on))
        mn.addAction(a)
        mn.addAction("Refresh (reload data + UI)", self.reload_all)
        mn.addAction("Restart island (picks up code changes)", self.restart)
        dm = mn.addMenu("Dock")
        for k, lbl in (("top", "Top"), ("left", "Left side"), ("right", "Right side")):
            a = QAction(lbl, dm, checkable=True)
            a.setChecked(self.dock == k)
            a.triggered.connect(lambda _=False, k=k: self.set_dock(k))
            dm.addAction(a)
        sm = mn.addMenu("Monitor")
        prim = QGuiApplication.primaryScreen()
        for i, s in enumerate(QGuiApplication.screens()):
            a = QAction(f"Monitor {i + 1}  ·  {s.geometry().width()}×{s.geometry().height()}"
                        + ("  (main)" if s == prim else ""), sm, checkable=True)
            a.setChecked(self.skey(s) == self.skey(self.screen()))
            a.triggered.connect(lambda _=False, s=s: self.set_screen(s))
            sm.addAction(a)
        a = QAction("Follow mouse to other screens", mn, checkable=True)
        a.setChecked(bool(self.settings.values.get("follow", True)))
        a.triggered.connect(lambda on: (setattr(self, "screen_ask", None), self.set_setting("follow", bool(on))))
        mn.addAction(a)
        dm2 = mn.addMenu("Dashboard")
        a = QAction("Off", dm2, checkable=True)
        a.setChecked(getattr(self, "dash", None) is None)
        a.triggered.connect(lambda _=False: self.set_dash(None))
        dm2.addAction(a)
        for i, s in enumerate(QGuiApplication.screens()):
            a = QAction(f"On monitor {i + 1}  ·  {s.geometry().width()}×{s.geometry().height()}", dm2, checkable=True)
            a.setChecked(bool(self.state.get("dash")) and self.state.get("dash")[0] == s.name()
                         and self.state.get("dash")[1] == s.geometry().left())
            a.triggered.connect(lambda _=False, s=s: self.set_dash(s))
            dm2.addAction(a)
        a = QAction("Hide over fullscreen apps", mn, checkable=True)
        a.setChecked(True)
        a.setEnabled(False)
        mn.addAction(a)
        mn.addSeparator()
        mn.addAction("Quit", QApplication.quit)

    def set_compact(self, on):
        self.state["compact"] = bool(on)
        self.save_state()
        self.js("compact", bool(on))

    def set_dock(self, d):
        self.state.update(dock=d, anchor=None)
        self.save_state()
        self.place()
        self.js("dock", d)

    def set_screen(self, s):
        if self.skey(s) != self.skey(self.screen()):
            self.ask_screen()
        self.state.update(screen=s.name(), screen_xy=[s.geometry().left(), s.geometry().top()], anchor=None)
        self.save_state()
        self.place()
        self.js("dock", self.dock)

    def restart(self):
        """Relaunch this script as a fresh process, then quit this one."""
        print("restart requested")
        self.save_state()
        if feats.under_watchdog():          # the watchdog starts the new one at once (exit code 3)
            self.tray.hide()
            QApplication.exit(3)
            return
        srv = getattr(self, "server", None)
        if srv is not None:
            srv.close()
        QLocalServer.removeServer(APP_ID)
        exe = sys.executable
        if WIN and os.path.basename(exe).lower() == "python.exe":
            w = os.path.join(os.path.dirname(exe), "pythonw.exe")
            if os.path.exists(w):
                exe = w
        ok = QProcess.startDetached(exe, [os.path.abspath(__file__)] + sys.argv[1:], HERE)
        ok = ok[0] if isinstance(ok, tuple) else ok
        if not ok:
            print("restart failed: could not start", exe)
            if srv is not None:
                srv.listen(APP_ID)
            return
        self.tray.hide()
        QApplication.quit()

    def reload_all(self):
        self.view.reload()
        self.ready = False

    def test_toast(self):
        m = self.model
        cur, nxt = m.focus()
        t = cur or nxt
        if t:
            self.item_toast("test-" + dt.datetime.now().isoformat(), t, f"test · {CAT_NAME.get(t['cat'], '')}", 20)
        ml, goal = m.water(m.today())
        if goal:
            self.water_toast("testw-" + dt.datetime.now().isoformat(), 500, max(goal - ml, 0))

    # ---------------------------------------------------------- foundation: settings, watchdog, live reload, upkeep
    def push_settings(self):
        if self.ready:
            QTimer.singleShot(50, self.vinst_push)
            QTimer.singleShot(70, self.binst_push)
            QTimer.singleShot(90, self.sayv_push)
        vals = dict(self.settings.values, compact=bool(self.state.get("compact")))
        sch = feats.SCHEMA if island_update.shared() else [   # update settings only exist in copies from GitHub
            dict(g, items=[it for it in g["items"] if not it.get("shared")]) for g in feats.SCHEMA]
        self.js("settings", dict(values=vals, schema=sch))
        self.js("customSounds", self.custom_sounds())

    def custom_sounds(self):
        """{kind: file url} for the page (it plays .ogg / .wav; python plays .mp3 / .wav itself)."""
        return {k: QUrl.fromLocalFile(v).toString() for k, v in self.custom_paths().items()}

    def custom_paths(self):
        """{kind: file path} from the sounds folder (notify.* -> 'any'; a single file of any name -> 'any')."""
        d = os.path.join(HERE, "sounds")
        out = {}
        try:
            os.makedirs(d, exist_ok=True)
            for f in sorted(os.listdir(d)):
                stem, ext = os.path.splitext(f)
                if ext.lower() in (".mp3", ".wav", ".ogg"):
                    k = "any" if stem.lower() in ("notify", "notification", "sound") else stem.lower()
                    k = k[:-4] if k.endswith((".mp3", ".wav", ".ogg")) else k        # 'x.mp3.mp3' (hidden extensions)
                    out.setdefault(k, os.path.join(d, f))
            if "any" not in out and len(out) == 1:          # one file with any name: use it for everything
                out["any"] = next(iter(out.values()))
        except OSError as e:
            print("sounds folder:", e)
        return out

    def set_setting(self, k, v):
        if k == "compact":
            self.set_compact(bool(v))
        elif self.settings.set(k, v):
            print(f"setting {k} = {v!r}")
            if k == "desk":
                QTimer.singleShot(0, self.desk_sync)
            if k == "buddy":
                self.buddy_arm()
            if k == "lan":
                self.lan_sync()
            if k == "deck_usb" and v:
                self._usb_last = None
                self.usb_tick()
            if k == "ram_trim" and v:
                self.trim(force=True)
            elif k == "scale":
                self.apply_zoom()
            elif k == "glass":
                self.apply_glass()
            elif k == "viz":
                self.update_meter()
            elif k == "lyrics" and v != "off" and self.media.last:
                self.lyrics.key = None
                self.lyrics.want(self.media.last)
            elif k == "volhud":
                self.sync_hook()
            elif k == "brain":
                self._bstate = None
                self._brain_seen = 0
            elif k in ("voice", "voice_key", "wake", "voice_model"):
                if k == "voice_model" and getattr(self, "voice", None):
                    self.voice.model = None            # loads the other size next time
                self.voice_sync()
            elif k == "privacy_dots":
                self.js("priv", self._priv if v else None)
            elif k == "heatmap":
                self._heat = None
                self.push_data()
        self.push_settings()

    def update_check(self, manual=False):
        """GitHub -> new code (shared copies only). Runs in a thread; the answer comes back through win_sig."""
        if not island_update.shared() or getattr(self, "_upd_busy", False):
            return
        if not manual and not self.settings.get("auto_update", True):
            return
        if manual:
            self.hud("dl", "Checking for updates…", ms=1500)
        self._upd_busy = True

        def run():
            try:
                r = island_update.check_and_update()
            except Exception as e:
                r = dict(status="error", msg=str(e), changed=[])
            self.win_sig.emit(("isl_update", dict(r, manual=manual)))
        threading.Thread(target=run, daemon=True).start()

    def update_done(self, r):
        self._upd_busy = False
        print(f"update: {r.get('status')} {r.get('msg', '')} {len(r.get('changed') or [])} file(s)")
        if r.get("status") == "updated":
            self.hud("ok", "Island updated", r=(r.get("msg") or "")[:40], ms=3200)
        elif r.get("manual"):
            self.hud("ok" if r.get("status") == "uptodate" else "info",
                     "Up to date" if r.get("status") == "uptodate" else "Couldn't update", r=(r.get("msg") or "")[:40], ms=2400)

    def do_action(self, what):
        if what == "update_now":
            return self.update_check(manual=True)
        if what == "restart":
            self.restart()
        elif what == "test_sounds":
            if self.settings.get("sounds") == "custom" and self.custom_paths():
                self.sound("info")
            else:
                self.js("chimeDemo")
        elif what == "test_confetti":
            self.js("celebrate")
        elif what == "mcp_setup":
            self.write_mcp_snippet()
            try:
                QApplication.clipboard().setText(open(os.path.join(HERE, "mcp_snippet.json"), encoding="utf-8").read())
                self.hud("ok", "MCP config copied", r="paste into claude_desktop_config.json", ms=2600)
            except Exception as e:
                print("mcp copy:", e)
        elif what == "lan_copy" and self.lan:
            QApplication.clipboard().setText(self.lan_url())
            self.hud("ok", "Deck link copied", r="open it on your phone / tablet", ms=2400)
        elif what == "voice_install":
            self.voice_install()
        elif what == "brain_install":
            self.brain_install()
        elif what == "say_voices":
            self.say_voices()
        elif what == "block_setup" and WIN:
            island_study.write_blocker_files()
            cmd = os.path.join(HERE, "setup_blocker.cmd")
            r = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f'/c "{cmd}"', HERE, 1)   # Windows asks (UAC)
            if r <= 32:
                self.hud("info", "Setup cancelled", ms=1500)
            else:
                QTimer.singleShot(12000, lambda: self.hud("ok" if island_study.blocker_installed() else "info",
                                                          "Site blocker ready" if island_study.blocker_installed()
                                                          else "Site blocker not installed", ms=2200))
        elif what == "sound_folder" and WIN:
            d = os.path.join(HERE, "sounds")
            os.makedirs(d, exist_ok=True)
            os.startfile(d)
            QTimer.singleShot(15000, lambda: self.js("customSounds", self.custom_sounds()))   # pick up a new file
        elif what in ("folder", "log") and WIN:
            p = HERE if what == "folder" else os.path.join(HERE, "island.log")
            if os.path.exists(p):
                os.startfile(p)

    def housekeep(self):
        threading.Thread(target=feats.housekeep, args=(self.settings,), daemon=True).start()

    # ---------------------------------------------------------- round 2: look & feel
    def apply_zoom(self):
        self.zoom = float(self.settings.get("scale") or 1.0)
        self.view.setZoomFactor(self.zoom)
        self.place()
        self.js("dock", self.dock)

    def apply_glass(self):
        on = False          # glass removed 3 Nov: it broke the island on the user's PC
        hwnd = int(self.winId())
        set_blur(hwnd, on)
        if not on:
            set_region(hwnd, None)
        print("glass", "on" if on else "off")

    def play_custom(self, kind):
        """Your own sound file through Windows' MCI player (handles mp3 and wav). False if it couldn't."""
        paths = self.custom_paths()
        f = paths.get(kind) or paths.get("any")
        if not f or not WIN or f.lower().endswith(".ogg"):
            return False
        try:
            mci = ctypes.windll.winmm.mciSendStringW
            mci("close isl_snd", None, 0, None)
            err = mci(f'open "{os.path.normpath(f)}" type mpegvideo alias isl_snd', None, 0, None)
            if err:
                print(f"custom sound: Windows couldn't open {os.path.basename(f)} (MCI error {err})")
                return False
            vol = int(max(0.05, min(1.0, float(self.settings.get("sound_vol") or 0.65))) * 1000)
            mci(f"setaudio isl_snd volume to {vol}", None, 0, None)
            return mci("play isl_snd from 0", None, 0, None) == 0
        except Exception as e:
            print("custom sound:", e)
            return False

    def sound(self, kind):
        mode = self.settings.get("sounds")
        if mode == "off" or kind == "mirror":
            return
        if mode == "custom" and kind != "tick" and self.play_custom(kind):
            return
        if mode == "windows":
            chime({"water": "water", "nag": "nag"}.get(kind, "info"))
        else:
            self.js("chime", kind)

    def update_meter(self):
        m = self.media.last
        self.meter.want(bool(self.settings.get("viz") and m and m.get("playing") and not self.hidden_fs
                             and not getattr(self, "_away_js", False) and self.mode in ("pill", "peek", "live", "panel", "music")))

    def on_lyrics(self, x):
        if (x.get("src") != "spotify" and self._lyr and self._lyr.get("src") == "spotify"
                and self._lyr.get("title") == x.get("title")):
            return                          # Spotify's own lyrics (Spicetify bridge) win over LRCLIB
        self._lyr = x
        self.js("lyrics", x)

    # ---------------------------------------------------------- round 3: Windows HUDs, hotkeys, command bar, timers
    def hud(self, kind, t, v=None, r="", ms=1600, img=None, act=None, cb=None):
        """A short message in the pill itself (like the iPhone's island), then back to normal."""
        if not self.ready or self.hidden_fs:
            return
        self.hud_cb = list(cb or [])
        self.js("hud", dict(k=kind, t=t, v=v, r=r, ms=ms, img=img, act=list(act or [])))

    def sync_hook(self):
        # only while pycaw works: otherwise swallowed volume keys would do nothing
        self.keys.set_hook(bool(self.settings.get("volhud")) and not self.hidden_fs and bool(getattr(self.watch, "AU", None)))

    def on_win(self, e):
        kind, arg = e
        st = self.settings.get
        if kind == "hotkey":
            self.on_hotkey(arg)
        elif kind == "isl_update":         # GitHub updater ("update" = Windows Update, further down)
            self.update_done(arg)
        elif kind == "webicon":
            self.load_launch()
        elif kind == "lan":
            self.lan_event(arg)
        elif kind == "devbat":
            self.devbat_event(arg)
        elif kind == "devbat_end":
            print("devbat: reader ended", arg)
            QTimer.singleShot(60_000, self.batt_sync)
        elif kind == "voice":
            self.voice_event(arg)
        elif kind == "openplan":
            self.open_plan_done(*arg)
        elif kind == "aicmd":
            self.brain_done(*arg)
        elif kind == "said":
            self.said(*arg)
        elif kind == "sayv":
            self._sayv = arg
            line = f"{ {k: arg.get(k) or '-' for k in ('zh', 'de')} } | all: " + ("; ".join(arg.get("all") or []) or "none")
            if line != getattr(self, "_sayv_line", None):     # only when it changed (the 10-min watch used to log 40 identical lines)
                self._sayv_line = line
                print("say: voices", line)
            if getattr(self, "_sayv_busy", False):
                self._sayv_tries += 1
                if all(arg.get(k) for k in ("zh", "de")):
                    self._sayv_busy = False
                    self.hud("ok", "Voices ready", r="tap 🔊 on the word of the day", ms=3000)
                elif self._sayv_tries >= 40:                # ~10 min: give up watching (the log says why)
                    self._sayv_busy = False
                    self._sayv_tried = True                 # next time: Windows Settings, not another UAC prompt
                    print("say: install finished but no voices showed up -> next tap opens Windows Settings > Speech")
                    self.hud("info", "Voices not there yet", r="see voices_install.log", ms=3000)
                else:
                    self.sayv_check(15_000)
            self.sayv_push()
        elif kind == "bstate":
            self._bstate = (time.time(), arg)
            self.binst_push(arg)
        elif kind == "bprog":
            self._binst_msg = str(arg)
            now = time.time()
            if now - getattr(self, "_bprog_t", 0) > 0.4:
                self._bprog_t = now
                self.binst_push()
        elif kind == "binstalled":
            ok, msg = arg
            self._b_installing = False
            self._brain_seen = 0
            print("brain install:", "ok" if ok else "FAILED", msg)
            if ok:
                self._bstate = (time.time(), "ready")
                self._brain_used, self._brain_gpu, self._brain_last = time.time(), True, self.brain_model()
                self.hud("ok", "AI brain ready", r=self.brain_model() or "", ms=3000)
                self.binst_push("ready")
            else:
                self._bstate = None
                self.hud("guard", "Brain setup failed", r="see brain_install.log", ms=3500)
                self.binst_push("failed", msg)
        elif kind == "vprog":
            self._vinst_msg = str(arg)
            now = time.time()
            if now - getattr(self, "_vprog_t", 0) > 0.4:
                self._vprog_t = now
                self.vinst_push()
        elif kind == "vinstalled":
            self._v_installing = False
            ok, msg = arg
            print("voice install:", "ok" if ok else "FAILED", msg)
            if ok:
                importlib.invalidate_caches()
                self.hud("ok", "Voice installed", r="getting the speech model ready", ms=3000)
                self._vinst_msg = "Downloading the speech model (~150 MB)…"
                self.voice_sync()
                if self.voice:
                    self.voice.warm()          # downloads the speech model the first time (~150 MB)
                self.vinst_push()
            else:
                self.hud("guard", "Voice install failed", r="see voice_install.log", ms=3500)
                self.vinst_push("failed", msg)
        elif kind == "spapplied":
            ok, msg = arg
            self.sp_updating = False
            self.sp_ver = 0 if ok else getattr(self, "sp_ver", 0)
            self.hud("ok" if ok else "guard", "Spotify updated" if ok else "Spotify update failed", r="" if ok else msg[:40], ms=2400)
            self.deck_push("spupdate", dict(ok=ok, msg="" if ok else msg))
            self.sp_now_push()
        elif kind == "spart":
            url, data = arg
            cache = self.__dict__.setdefault("_sp_art", {})
            cache[url] = data
            for k in [k for k in cache if k != url][:-6]:
                cache.pop(k, None)
            if self.sp_remote_on():
                self._push_media(self.media_merged(getattr(self, "_win_media", None)))
        elif kind == "usb":
            ok, msg, opened = arg
            if msg != getattr(self, "_usb_last", None):
                self._usb_last = msg
                print("deck usb:", msg)
                if ok:
                    self.hud("screen", "Tablet linked over USB", r="the deck opened on it" if opened else "", ms=2600)
        elif kind == "apps":
            lst, push = arg
            push = push or getattr(self, "_apps_want", False)
            self._apps_busy = self._apps_want = False
            if lst:
                self.apps_cat, self.apps_t = lst, time.time()
                self.migrate_claude()
            if push:
                self.js("apps", self.apps_js())
        elif kind == "appicon":
            appid, f = arg
            if not hasattr(self, "_appsrc"):
                self._appsrc = {}
            self._appsrc[appid] = f
            try:
                os.makedirs(os.path.join(HERE, "icons"), exist_ok=True)
                with open(os.path.join(HERE, "icons", "apps.json"), "w", encoding="utf-8") as fh:
                    json.dump(self._appsrc, fh, indent=1, ensure_ascii=False)
            except Exception as e:
                print("icons/apps.json:", e)
            self._icons.pop("app:" + appid, None)
            self.load_launch()
        elif kind == "volkey":
            self.watch.cmd("vol", arg)
        elif kind == "vol":
            if arg["mute"]:
                self.hud("mute", "Muted", ms=1300)
            else:
                self.hud("vol", "Volume", v=arg["v"], r=str(round(arg["v"] * 100)), ms=1300)
        elif kind == "lock" and st("locks"):
            vk = arg
            QTimer.singleShot(60, lambda: self.hud("caps" if vk == 0x14 else "num",
                                                   ("Caps Lock" if vk == 0x14 else "Num Lock") +
                                                   (" on" if island_win.lock_state(vk) else " off"), ms=1100))
        elif kind == "lang" and st("lang"):
            self.hud("lang", arg, ms=900)
        elif kind == "priv":
            self._priv = arg
            if st("privacy_dots"):
                self.js("priv", arg)
        elif kind == "mic":
            self.deck_push("micstate", dict(mute=bool(arg.get("mute")), none=bool(arg.get("none"))))
            if arg.get("none"):
                self.hud("mic", "No microphone", ms=1200)
            else:
                self.hud("micoff" if arg["mute"] else "mic", "Mic muted" if arg["mute"] else "Mic on", ms=1300)
        elif kind == "net":
            self.js("net", arg["up"])
            if st("net"):
                if arg["up"]:
                    gone = arg.get("gone", 0)
                    self.hud("net", "Back online", r=("was off " + fmt_min(gone / 60)) if gone >= 60 else "", ms=2200)
                else:
                    self.hud("netoff", "Offline", ms=2600)
        elif kind == "ping":
            self._ping = arg
        elif kind == "wifi":
            self._wifi = arg
        elif kind == "download" and st("downloads"):
            pth = arg["path"]
            mb = arg["size"] / 2**20
            self.hud("dl", arg["name"][:42], r=f"{mb:.1f} MB" if mb >= 1 else "", ms=4500, act=["Open", "Folder"],
                     cb=[lambda: os.startfile(pth), lambda: os.startfile(os.path.dirname(pth))])
        elif kind == "disk":
            self.notify(f"disk-{time.time():.0f}", "R", "storage", f"C: has {arg} GB left", "Time to clean up a bit.",
                        [("OK", None)], 20)
        elif kind == "update":
            self.notify(f"upd-{time.time():.0f}", "R", "windows update", "Restart pending",
                        "Windows wants to restart to finish an update. Don't let it surprise you before an exam.",
                        [("OK", None)], 25)
        elif kind == "bri":
            self._bri = arg
            self.js("bri", arg)
        elif kind == "github":
            self._gh = arg
            for n in arg.get("new", []):
                why = {"review_requested": "review requested", "mention": "mentioned you", "assign": "assigned to you",
                       "ci_activity": "CI", "author": "your thread", "comment": "new comment"}.get(n["reason"], n["reason"])
                self.notify(f"gh-{n['id']}", "MI", f"GitHub · {why}", n["title"][:90], n["repo"], [("OK", None)], 25,
                            kind="mirror")
        elif kind == "netlify":
            self._deploy = arg
            st = arg.get("state")
            if not arg.get("first") and st == "ready":
                self.notify(f"nf-{arg['id']}", "MI", "Netlify · QAWIFit", "Deploy is live", arg.get("title") or "", [("OK", None)], 20)
            elif not arg.get("first") and st in ("error", "rejected"):
                self.notify(f"nf-{arg['id']}", "MI", "Netlify · QAWIFit", "Deploy failed", arg.get("error") or arg.get("title") or "",
                            [("OK", None)], 60, kind="nag")
            self.js("deploy", arg if st not in ("ready", "error", "rejected") else None)
        elif kind == "gpu":
            self._gpu = arg
        elif kind == "bt":
            self._bt = arg
            if self.ready:
                self.push_devbat()
            for d in arg:                      # one heads-up per device when it gets low
                if d["v"] <= 15 and d["name"] not in self._bt_low and self.settings.get("battery"):
                    self._bt_low.add(d["name"])
                    self.hud("batlow", d["name"], v=d["v"] / 100, r=f"{d['v']}%", ms=3500)
                elif d["v"] > 25:
                    self._bt_low.discard(d["name"])

    def on_hotkey(self, name):
        if name == "panel":
            self.js("mode", "pill" if self.mode == "panel" else "panel")
        elif name == "cmd":
            self.open_cmd("")
        elif name == "timer":
            self.open_cmd("timer ")
        elif name == "player":
            self.js("player")
        elif name == "water":
            self.add_water(self.model.today(), 250)
            self.hud("water", "Water", r="+250 ml", ms=1300)
        elif name == "mic":
            self.watch.cmd("mic")
        elif name == "widgets":
            self.desk_peek()
        elif name == "privacy":
            self.privacy_auto = False
            self.set_privacy(not self.privacy)

    def set_privacy(self, on):
        if on == self.privacy:
            return
        self.privacy = on
        self.js("privacy", on)
        self.hud("privacy", "Private mode " + ("on" if on else "off"), ms=1300)

    def open_cmd(self, prefill=""):
        if self.hidden_fs or not self.ready:
            return
        self.raise_()
        self.activateWindow()
        if WIN:
            try:
                user32.SetForegroundWindow(int(self.winId()))
            except Exception:
                pass
        self.click_through = False
        set_click_through(self, False)
        self.view.setFocus()
        self.js("cmd", prefill)

    # ---------------------------------------------------------- round 4: study tools
    def pomo_phase_at(self, t):
        """(phase, phase_end_epoch, cycle) for the running focus session: 50 min focus, 10 min break, repeat."""
        p = self.pomo
        e = t - p["t0"]
        if e < 0:
            return "focus", min(p["t0"] + island_study.FOCUS_S, p["end"]), 0
        n, r = int(e // 3600), e % 3600
        if r < island_study.FOCUS_S:
            return "focus", min(p["t0"] + n * 3600 + island_study.FOCUS_S, p["end"]), n
        return "break", min(p["t0"] + (n + 1) * 3600, p["end"]), n

    def pomo_js(self):
        p = self.pomo
        if not p:
            return None
        return dict(cat=p["cat"], name=p["name"], t0=int(p["t0"] * 1000), end=int(p["end"] * 1000),
                    free=p.get("free", False), guard=self.guard_game)

    def study_block(self, now):
        """The study block running right now from the plan: (key, cat, start, end, title) or None."""
        try:
            cur, _ = self.model.focus(now)
        except Exception:
            return None
        if not cur or cur.get("cat") not in island_study.SUBJ or cur.get("done") or cur.get("block"):
            return None
        s0, e0 = self.model.span(cur, self.model.today(now))
        return (cur["key"], cur["cat"], s0, e0, cur.get("title", "")) if s0 and e0 else None

    def study_tick(self):
        """Every second: which focus session runs, phase changes, the game guard, focus minutes."""
        now_dt = dt.datetime.now()
        now = now_dt.timestamp()
        p = self.pomo
        if p and p.get("free"):
            blk_key = p["key"] if now < p["end"] else None
        else:
            if not getattr(self, "_blk_at", 0) or now - self._blk_at >= 5 or p is None:
                self._blk_at = now
                self._blk = self.study_block(now_dt)
            blk = self._blk
            blk_key = blk[0] if blk and blk[0] != self.state.get("pomo_skip") else None
        if blk_key != self.pomo_key:
            self.pomo_key, self.pomo_phase = blk_key, None
            if blk_key is None:
                self.pomo = None
            elif not (p and p.get("free")):
                a = self.state.get("pomo_anchor")
                t0 = a[1] if isinstance(a, list) and a[0] == blk_key else blk[2].timestamp()
                self.pomo = dict(key=blk_key, cat=blk[1], name=island_study.SUBJ[blk[1]], t0=t0, end=blk[3].timestamp())
            self.guard_game = None
            self.js("pomo", self.pomo_js())
        if not self.pomo:
            return
        phase, pend, n = self.pomo_phase_at(now)
        name = self.pomo["name"]
        if phase != self.pomo_phase:
            first = self.pomo_phase is None
            self.pomo_phase = phase
            if phase == "break":
                self.hud("timer", "Break · 10 min", r=f"{name} · water, stretch", ms=4500)
                self.sound("done")
            elif not first:
                self.hud("timer", f"Back to {name}", r="50 min", ms=3000)
                self.sound("info")
            elif now - self.pomo["t0"] < 120:
                self.hud("timer", f"{name} · focus", r="50 + 10", ms=3000)
            self.js("pomo", self.pomo_js())
        if phase != "focus" or self.mock_on():
            return
        # distraction guard: a game in front during focus time
        game = None
        if WIN:
            try:
                import psutil
                hwnd = user32.GetForegroundWindow()
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 256)
                game = island_study.game_name(psutil.Process(pid.value).exe(), buf.value)
            except Exception:
                game = None
        if game and self.settings.get("guard"):
            if game != self.guard_game:
                self.guard_game, self.guard_at = game, 0.0
                self.study.note(now_dt.date(), "caught")
                self.js("pomo", self.pomo_js())
            if now - self.guard_at > 90:                    # call it out, again every 90 s while it stays open
                self.guard_at = now
                self.hud("guard", f"{name} time. Close {game}", r="focus", ms=5000)
                self.sound("nag")
            return                                           # game time doesn't count as study
        if self.guard_game:
            self.guard_game = None
            self.js("pomo", self.pomo_js())
        self.study.add_secs(now_dt.date(), self.pomo["cat"], 1)

    def mock_tick(self):
        now = time.time()
        for t in self.timers:
            if not t.get("mock"):
                continue
            left = t["end"] - now
            for w in (30, 10, 5):
                if left <= w * 60 and w not in t.setdefault("warned", []) and left > (w - 1) * 60:
                    t["warned"].append(w)
                    self.hud("timer", f"{w} min left", r=t["label"], ms=3500)
                    self.sound("nag")
        if int(now) % 60 == 0:
            self.study.save_log()
            self.push_study()
            if self.cards is not None and self._cards_day != self.model.today():
                self.push_cards()                        # new day: new cards are due
            if self.life is not None and self._life_day != self.model.today():
                self.push_life()
            self.push_screen()

    def push_cards(self):
        if self.cards is None:
            return
        today = self.model.today()
        self._cards_day = today
        self.js("cards", self.cards.summary(today))

    def block_tick(self):
        """Site blocker: on for the whole study block / focus session (setting 'block' + one-time setup)."""
        want = int(self.pomo["end"]) if (self.pomo and self.settings.get("block") and island_study.blocker_installed()) else 0
        if want != self._block_until:
            self._block_until = want
            if island_study.set_block(want or None):
                print(f"site blocker: {'on until ' + self.t12(dt.datetime.fromtimestamp(want)) if want else 'off'}")

    # ---------------------------------------------------------- round 5: the day's rhythm
    @staticmethod
    def t12(d):
        return d.strftime("%I:%M %p").lstrip("0")

    def fasting(self, now=None):
        """Ramadan, between Fajr and Maghrib."""
        now = now or dt.datetime.now()
        h = self.prayer.hijri(now.date())
        if not h or h[1] != 9:
            return False
        t = dict(self.prayer.times(now.date()))
        return bool(t) and t["Fajr"] <= now < t["Maghrib"]

    def push_prayer(self):
        now = dt.datetime.now()
        times = self.prayer.times(now.date())
        nx = self.prayer.next(now)
        h = self.prayer.hijri(now.date())
        x = None
        if times:
            x = dict(list=[dict(n=n, t=self.t12(at), ar=island_day.AR[n], past=at <= now) for n, at in times],
                     next=dict(n=nx[0], ar=island_day.AR[nx[0]], t=self.t12(nx[1]), at=int(nx[1].timestamp() * 1000)) if nx else None,
                     ramadan=bool(h and h[1] == 9), on=bool(self.settings.get("prayer")))
            if x["ramadan"]:
                t = dict(times)
                x["iftar"] = int(t["Maghrib"].timestamp() * 1000) if self.fasting(now) else None
        if x != self._prayer_js:
            self._prayer_js = x
            self.js("prayer", x)

    def day_tick(self):
        now = dt.datetime.now()
        today = now.date()
        ts = now.timestamp()
        st = self.settings.get
        if now.second == 7:
            self.prayer.ensure(today)
        if now.second % 20 == 0:
            self.push_prayer()
        # ---- adhan, Jumu'ah, adhkar
        times = dict(self.prayer.times(today))
        if times and st("prayer"):
            for n in island_day.PRAYERS:
                at = times[n]
                k = "p-" + n
                if 0 <= (now - at).total_seconds() < 120 and not self.daylog.done(today, k):
                    self.daylog.mark(today, k)
                    name = "Jumu'ah" if n == "Dhuhr" and today.weekday() == 4 else n
                    self.notify(f"prayer-{today}-{n}", "PR", "prayer", f"{name} · {self.t12(at)}",
                                f"حان الآن موعد أذان {island_day.AR[n]}", [("OK", None)], 120, kind="prayer")
                    self.push_prayer()
            if today.weekday() == 4 and not self.daylog.done(today, "jumuah"):
                lead = (times["Dhuhr"] - now).total_seconds()
                if 0 < lead <= 45 * 60:
                    self.daylog.mark(today, "jumuah")
                    self.notify(f"jumuah-{today}", "PR", "jumu'ah", f"Jumu'ah at {self.t12(times['Dhuhr'])}",
                                "Time to get ready and leave for the khutbah.", [("OK", None)], 300, kind="prayer")
        idle = idle_seconds()
        active = idle < 60
        if times and st("adhkar") and active:
            if times["Fajr"] + dt.timedelta(minutes=15) <= now < now.replace(hour=11, minute=0) \
                    and not self.daylog.done(today, "adhkar-am"):
                self.daylog.mark(today, "adhkar-am")
                self.notify(f"adhkar-am-{today}", "PR", "adhkar", "أذكار الصباح", "", [("Done", None)], 90, kind="info")
            elif times["Asr"] + dt.timedelta(minutes=15) <= now < times["Maghrib"] and not self.daylog.done(today, "adhkar-pm"):
                self.daylog.mark(today, "adhkar-pm")
                self.notify(f"adhkar-pm-{today}", "PR", "adhkar", "أذكار المساء", "", [("Done", None)], 90, kind="info")
        # ---- waking up (morning briefing + sleep estimate), stand-up nudge
        if idle >= 600 and self._away_from is None:
            self._away_from = ts - idle
        if idle < 30:
            if self._away_from is not None:
                gap = ts - self._away_from
                start = dt.datetime.fromtimestamp(self._away_from)
                if gap >= 3 * 3600 and 4 <= now.hour < 14:
                    slept = gap / 60 if (start.hour >= 21 or start.hour < 6) else None
                    self.on_wake(slept)
                if gap >= 180:
                    self._streak_from = ts
                self._away_from = None
            if st("stretch") and ts - self._streak_from >= 55 * 60 and not self.pomo and not self.mock_on() \
                    and not self.hidden_fs:
                self._streak_from = ts
                self.hud("stretch", "Stand up · stretch 2 min", r="you've been on for an hour", ms=5000)
                self.sound("info")
        elif idle >= 180:
            self._streak_from = ts
        # ---- Sunday: QAWIFit weekly check-in
        if st("checkin") and active and today.weekday() == 6 and now.hour >= 9 and not self.daylog.done(today, "checkin"):
            self.daylog.mark(today, "checkin")
            self.notify(f"checkin-{today}", "ME", "QAWIFit · sunday", "Weekly check-in",
                        "Weigh in, waist, a photo: two minutes in the app.", [("OK", None)], 60, kind="info")
        # ---- night recap + bedtime (island day: until 4 AM)
        iday = self.model.today()
        if st("recap") and active and not self.daylog.done(iday, "recap") and (now.hour >= 23 or now.hour < 2 or
                                                                              (now.hour == 22 and now.minute >= 45)):
            self.daylog.mark(iday, "recap")
            self.night_recap()
        bt = str(st("bedtime") or "00:00")
        if bt != "off" and active:
            hh, mm = (int(x) for x in bt.split(":"))
            bed = dt.datetime.combine(iday + (dt.timedelta(days=1) if hh < 12 else dt.timedelta()), dt.time(hh, mm))
            mins = (bed - now).total_seconds() / 60
            if 0 < mins <= 30 and not self.daylog.done(iday, "wind"):
                self.daylog.mark(iday, "wind")
                self.notify(f"wind-{iday}", "E", "bedtime", f"Bed at {self.t12(bed)}",
                            "30 minutes left. Finish up, episode, then sleep.", [("OK", None)], 60, kind="info")
            elif -60 < mins <= 0 and not self.daylog.done(iday, "bed"):
                self.daylog.mark(iday, "bed")
                self.notify(f"bed-{iday}", "E", "bedtime", "It's bedtime",
                            "Sleep is when today's study sticks.", [("OK", None)], 60, kind="nag")

    def on_wake(self, slept_min):
        today = self.model.today()
        if slept_min:
            self.daylog.mark(today, "sleep_min", round(slept_min))
            print(f"sleep estimate: {fmt_min(slept_min)}")
        if not self.settings.get("brief") or self.daylog.done(today, "brief"):
            return
        self.daylog.mark(today, "brief")
        try:
            items = [t for t in self.model.items(today) if t.get("time")]
        except Exception:
            items = []
        study = [t for t in items if t.get("cat") in island_study.SUBJ and not t.get("done")]
        cls = [t for t in items if t.get("cat") == "U"]
        bits = [f"{t['title']} at {self.t12(dt.datetime.strptime(t['time'], '%H:%M'))}" for t in study[:2]]
        if len(study) > 2:
            bits.append(f"+{len(study) - 2} more")
        line2 = []
        if cls:
            line2.append(f"class {self.t12(dt.datetime.strptime(cls[0]['time'], '%H:%M'))}")
        if slept_min:
            line2.append(f"slept ~{fmt_min(slept_min)}")
        w = getattr(self, "_wx", None)
        if w:
            line2.append(f"{round(w['t'])}° {w['desc'].lower()}")
        nx = self.prayer.next(dt.datetime.now())
        if nx:
            line2.append(f"{nx[0]} {self.t12(nx[1])}")
        n = self.cards.summary(today)["n"] if self.cards else 0
        if n:
            line2.append(f"{n} card{'s' if n != 1 else ''} due")
        big = big_countdown(self.model, today)
        self.notify(f"brief-{today}", "R", "good morning",
                    f"{today.strftime('%A')}" + (f" · {big['days']} days to {big['title']}" if big else ""),
                    " · ".join(bits) or "No study blocks today.", [("Let's go", None)], 90, kind="info",
                    body2=" · ".join(line2))

    def night_recap(self):
        today = self.model.today()
        try:
            d = build_state(self.model, 0)
        except Exception:
            d = {}
        mn = self.study.week(today)["today"] if self.study else {}
        focus = sum(mn.values())
        w = d.get("water") or {}
        cards_done = 0
        if self.cards:
            cards_done = sum(1 for c in self.cards.data["cards"] if c.get("last") == today.isoformat())
        body = [f"focus {fmt_min(focus)}" if focus else "no focus time logged"]
        if w.get("goal"):
            body.append(f"water {w['ml'] / 1000:.1f} / {w['goal'] / 1000:.1f} L")
        if cards_done:
            body.append(f"{cards_done} card{'s' if cards_done != 1 else ''} reviewed")
        late = d.get("late") or 0
        self.notify(f"recap-{today}", "R", "today", f"{d.get('pct', 0)}% done · streak {d.get('fire', 0)}",
                    " · ".join(body), [("Good night", None)], 90, kind="info",
                    body2=f"{late} thing{'s' if late != 1 else ''} still open" if late else "")

    # ---------------------------------------------------------- round 6: life
    def push_life(self):
        if self.life is None:
            return
        today = self.model.today()
        self._life_day = today
        self.js("life", self.life.snapshot(today))

    def life_cmd(self, low, t):
        """deadline <what> <date> · countdown <what> <date> · todo <text> · habit <name> · habit remove <name> · copy today"""
        if self.life is None:
            return "nomatch"
        today = self.model.today()
        m = re.match(r"^(?:big|main|countdown to)\s+(.+)$", low)
        if m:
            rest = t.split(None, 1)[1].strip()
            if low.startswith("countdown to"):
                rest = rest.split(None, 1)[1] if len(rest.split(None, 1)) > 1 else ""
            if rest.lower() in ("off", "clear", "none", "remove", "stop"):
                self.life.set_big(None, None)
                self.push_life()
                self.push_data()
                return self.hud("ok", "Big countdown off", ms=1400)
            what, d = island_life.split_date(rest, today, island_study.parse_date)
            if not d:
                self.life.set_big(rest[:40], None)          # just a new name for the study plan's exam day
                self.push_life()
                self.push_data()
                return self.hud("ok", f"Counting down to {rest[:40]}", ms=1800)
            self.life.set_big(what[:40] or "the big day", d.isoformat())
            self.push_life()
            self.push_data()
            return self.hud("ok", f"{(d - today).days} days to {what[:40]}", r=d.strftime('%a %d %b').replace(' 0', ' '), ms=2400)
        m = re.match(r"^(deadline|due|countdown|cd)\s+(.+)$", low)
        if m:
            what, d = island_life.split_date(t.split(None, 1)[1], today, island_study.parse_date)
            if not d or not what:
                return self.hud("info", f"{m.group(1)} lab report 12 nov", ms=2200)
            if m.group(1) in ("deadline", "due"):
                self.life.add("deadlines", title=what[:80], due=d.isoformat())
            else:
                self.life.add("countdowns", title=what[:60], date=d.isoformat())
            self.push_life()
            return self.hud("ok", what[:40], r=f"{d.strftime('%a %d %b').replace(' 0', ' ')} · {(d - today).days} days", ms=2200)
        m = re.match(r"^todo\s+(.+)$", low)
        if m:
            self.life.add("todo", text=t.split(None, 1)[1][:120])
            self.push_life()
            return self.hud("ok", "Added to your to-do", ms=1200)
        m = re.match(r"^habit\s+(remove|rm|delete)\s+(.+)$", low)
        if m:
            name = m.group(2).strip()
            for x in list(self.life.data["habits"]):
                if x.get("name", "").lower() == name:
                    self.life.remove("habits", x["id"])
            self.push_life()
            return self.hud("ok", f"Habit {name} removed", ms=1400)
        m = re.match(r"^habit\s+(.+)$", low)
        if m:
            name = t.split(None, 1)[1].strip()[:20]
            self.life.add("habits", name=name, days=[])
            self.push_life()
            return self.hud("ok", f"Habit: {name}", r="tick it in the Today tab", ms=1800)
        if low in ("copy", "copy today", "today"):
            return self.copy_today()
        m = re.match(r"^(?:weight|weigh|kg)\s+(\d{2,3}(?:[.,]\d{1,2})?)\s*(?:kg)?$", low)
        if m:
            kg = float(m.group(1).replace(",", "."))
            if not 30 <= kg <= 300:
                return self.hud("info", "weight 160.8  (kg)", ms=1800)
            if not self.inbox.ready():
                return self.hud("info", "QAWIFit isn't connected right now", ms=2000)
            self.inbox.submit(dict(type="weight", id=f"weight-{today.isoformat()}-{int(time.time())}", kg=kg), "weight")
            return self.hud("ok", f"{kg:g} kg", r="sending to QAWIFit", ms=1500)
        return "nomatch"

    def today_text(self):
        today = self.model.today()
        now = dt.datetime.now()
        d = build_state(self.model, 0)
        out = [f"My day (from the island) · {today.strftime('%A %d %b %Y')} · {self.t12(now)}"]
        out.append(f"Plan done: {d.get('pct', 0)}% · streak {d.get('fire', 0)} · gym streak {d.get('gym', 0)} · "
                   + (lambda b: f"{b['days']} days to {b['title']}" if b else "no countdown set")(big_countdown(self.model, today)))
        w = d.get("water") or {}
        if w.get("goal"):
            out.append(f"Water: {w['ml'] / 1000:.2f} / {w['goal'] / 1000:.1f} L")
        f = getattr(self, "_fit", None)
        if f and f.get("ok"):
            try:
                out.append(f"QAWIFit: {f['kcal']} / {f['t']['kcal']} kcal · protein {f['p']} / {f['t']['p']} g")
            except Exception:
                pass
        out.append("Plan:")
        for it in d.get("view", {}).get("items", []):
            if it.get("block"):
                continue
            mark = "[x]" if it.get("done") else "[ ]"
            out.append(f"  {mark} {h12(it['time']) + ' ' if it.get('time') else ''}{it.get('title', '')}")
        mn = self.study.week(today)["today"] if self.study else {}
        if any(mn.values()):
            out.append("Focus today: " + ", ".join(f"{island_study.SUBJ[c]} {fmt_min(v)}" for c, v in mn.items() if v))
        if self.cards:
            out.append(f"Review cards due: {self.cards.summary(today)['n']}")
        if self.life:
            snap = self.life.snapshot(today)
            dls = [x for x in snap["deadlines"] if not x["done"]][:3]
            if dls:
                out.append("Deadlines: " + "; ".join(f"{x['title']} ({x['date']}, {x['days']} d)" for x in dls))
            todo = [x["text"] for x in snap["todo"] if not x["done"]]
            if todo:
                out.append("To-do: " + "; ".join(todo[:8]))
        return "\n".join(out)

    def copy_today(self):
        try:
            txt = self.today_text()
            QApplication.clipboard().setText(txt)
            self.hud("ok", "Today copied", r="paste it into any chat", ms=1800)
        except Exception as e:
            print("copy today:", e)
            self.hud("info", "Couldn't copy today", ms=1500)
        return True

    def quiet_now(self):
        q = str(self.settings.get("quiet") or "off")
        if q == "off" or "-" not in q:
            return False
        a, b = q.split("-")
        now = dt.datetime.now().strftime("%H:%M")
        return (a <= now < b) if a < b else (now >= a or now < b)

    # ---------------------------------------------------------- weather alerts + screen time
    def wx_alerts(self, w):
        if not w or not self.settings.get("wx_alerts"):
            return
        today = dt.date.today()
        r = w.get("rain")
        if r and r.get("in_h", 99) <= 3 and not self.daylog.done(today, "rain-" + r["at"]):
            self.daylog.mark(today, "rain-" + r["at"])
            self.notify(f"rain-{today}-{r['at']}", "R", "weather", f"Rain likely around {r['at']}",
                        f"{r['p']}% chance. Take a jacket if you're going out.", [("OK", None)], 30, kind="info")
        dust = w.get("dust")
        if dust and dust.get("max", 0) >= 150 and not self.daylog.done(today, "dust"):
            self.daylog.mark(today, "dust")
            self.notify(f"dust-{today}", "R", "weather", "Dusty air today",
                        f"Up to {round(dust['max'])} µg/m³. Keep the windows shut; a mask if you're out long.",
                        [("OK", None)], 30, kind="info")

    APP_NAMES = {"chrome": "Chrome", "brave": "Brave", "msedge": "Edge", "firefox": "Firefox", "discord": "Discord",
                 "code": "VS Code", "spotify": "Spotify", "explorer": "File Explorer", "claude": "Claude",
                 "whatsapp": "WhatsApp", "whatsapp.root": "WhatsApp", "steam": "Steam", "steamwebhelper": "Steam",
                 "uipath.studio": "UiPath Studio", "windowsterminal": "Terminal", "cmd": "Terminal",
                 "powershell": "Terminal", "obs64": "OBS", "notepad": "Notepad", "winword": "Word", "excel": "Excel",
                 "powerpnt": "PowerPoint", "unity": "Unity", "unity hub": "Unity Hub", "photoshop": "Photoshop",
                 "telegram": "Telegram", "vlc": "VLC", "applicationframehost": "Windows app", "pythonw": "Island",
                 "phoneexperiencehost": "Phone Link"}

    def screen_tick(self):
        """Every 5 s: which app is in front (if you're there, or a fullscreen app/game is running)."""
        if not WIN or not self.settings.get("screen"):
            return
        try:
            import psutil
            hwnd = user32.GetForegroundWindow()
            if not hwnd or int(hwnd) in self.own_hwnds():
                return
            if idle_seconds() > 300 and fullscreen_monitor(self.own_hwnds()) is None:
                return
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            path = psutil.Process(pid.value).exe()
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
        except Exception:
            return
        stem = os.path.basename(path).rsplit(".", 1)[0].lower()
        if stem in ("lockapp", "logonui"):      # the lock / sign-in screen counts as fullscreen, but nobody is using the PC
            return
        name = island_study.game_name(path, buf.value) or self.APP_NAMES.get(stem) or stem.replace("-", " ").title()[:20]
        if name in ("Island",):
            return
        self._exe_paths[name] = path
        d = self.screen_log.setdefault(self.model.today().isoformat(), {})     # the island's day: 1-4 AM is still "tonight"
        d[name] = d.get(name, 0) + 5
        self._screen_dirty = True

    def push_screen(self, force=False):
        today = self.model.today().isoformat()
        d = self.screen_log.get(today, {})
        apps = sorted(d.items(), key=lambda x: -x[1])
        x = dict(total=round(sum(d.values()) / 60), apps=[dict(name=n, min=round(v / 60), icon=self.app_icon(self._exe_paths.get(n)))
                                                         for n, v in apps[:6] if v >= 60])
        if force or x != getattr(self, "_screen_js", None):
            self._screen_js = x
            self.js("screen", x)
        if self._screen_dirty:
            self._screen_dirty = False
            keep = sorted(self.screen_log)[-14:]
            self.screen_log = {k: self.screen_log[k] for k in keep}
            try:
                tmp = os.path.join(HERE, "screen_log.json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self.screen_log, f, ensure_ascii=False)
                os.replace(tmp, os.path.join(HERE, "screen_log.json"))
            except OSError as e:
                print("screen_log:", e)

    # ---------------------------------------------------------- round 8: dev
    def write_mcp_snippet(self):
        """mcp_snippet.json: what to paste into Claude Desktop's config (exact python + path on this PC)."""
        exe = sys.executable
        if exe.lower().endswith("pythonw.exe"):
            exe = exe[:-11] + "python.exe"
        snip = {"mcpServers": {"island": {"command": exe, "args": [os.path.join(HERE, "island_mcp.py")]}}}
        p = os.path.join(HERE, "mcp_snippet.json")
        txt = json.dumps(snip, indent=2)
        if not os.path.exists(p) or open(p, encoding="utf-8").read() != txt:
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt)

    def lan_url(self, usb=False):
        host = "localhost" if usb else island_dev.lan_ip()
        return f"http://{host}:{island_dev.LAN_PORT}/deck?k={self.state.get('lan_token', '')}"

    def lan_sync(self):
        want = bool(self.settings.get("lan"))
        if want and self.lan is None:
            if not self.state.get("lan_token"):
                import secrets as _s
                self.state["lan_token"] = _s.token_urlsafe(9)
                self.save_state()
            srv = island_dev.LanServer(self.state["lan_token"], lambda e: self.win_sig.emit(("lan", e)))   # instant, main thread
            if srv.start():
                self.lan = srv
                if WIN and hasattr(island_dev, "lan_doctor"):
                    threading.Thread(target=lambda: [print("deck check:", x) for x in island_dev.lan_doctor(sys.executable)], daemon=True).start()
        elif not want and self.lan is not None:
            self.lan.stop()
            self.lan = None
            self.deck_n = 0
            self.sound_sync()
        self.js("lanurl", self.lan_url() if self.lan else None)

    # ---------------------------------------------------------- the deck (phone / tablet)
    def lan_event(self, e):
        kind, arg = e
        m = self.model
        if kind == "lan_water":
            self.add_water(m.today(), int(arg))
        elif kind == "lan_share_got":
            self.js("dropShare", None)
            QTimer.singleShot(150, lambda n=str(arg): self.hud("ok", "On your phone", r=n[:40], ms=2400))
        elif kind == "lan_tick":
            items = {t["key"]: t for t in m.items(m.today())}
            if arg in items and not items[arg].get("block"):
                m.toggle(items[arg], m.today())
                self.push_data()
        elif kind == "deck_n":
            was, self.deck_n = getattr(self, "deck_n", 0), int(arg)
            self.sound_sync()
            if self.deck_n > was:
                print(f"deck: connected ({self.deck_n} open)")
                self.hud("screen", "Deck connected", ms=1400)
        elif kind == "deck_hello":
            self.deck_snapshot()
        elif kind == "deck_do":
            name, a = arg.get("name"), arg.get("arg")
            try:
                if name == "cmd":
                    self.handle_cmd(str(a or "")[:200])
                elif name == "deck_tiles":
                    self.save_deck_tiles(a)
                elif name == "note_act" and isinstance(a, list) and len(a) == 2:
                    self.note_act(str(a[0]), int(a[1]))
                elif name == "sp_search" and isinstance(a, dict):
                    q, sid = str(a.get("q") or "").strip()[:120], str(a.get("id") or "")[:20]
                    if not q:
                        pass
                    elif not self.spotify.listening() or not self.sp_live():
                        self.deck_push("spsearch", dict(id=sid, q=q, items=[], error="Spotify isn't open on the PC"))
                    else:
                        print("deck: spotify search", repr(q[:40]), f"(bridge v{getattr(self, 'sp_ver', 0)})")
                        self.spotify.send_cmd("search", q=q, id=sid)
                elif name in ("sp_play", "sp_queue") and isinstance(a, str) and re.match(r"^spotify:track:[A-Za-z0-9]{10,40}$", a):
                    self.spotify.send_cmd("play" if name == "sp_play" else "queue", uri=a)
                    if name == "sp_queue":
                        self.hud("ok", "Added to the queue", ms=1300)
                elif name == "sp_lib":
                    if not self.sp_live():
                        self.deck_push("splib", dict(items=[], liked=0, error="Spotify isn't open on the PC"))
                    elif getattr(self, "sp_ver", 0) < self.SP_VER:
                        self.sp_now_push(force=True)
                    else:
                        lib = getattr(self, "sp_lib", None)
                        if lib and lib.get("items") and time.time() - lib["at"] < 600:
                            self.deck_push("splib", {k: v for k, v in lib.items() if k != "at"})   # instant, then fresh
                        self.spotify.send_cmd("lib")
                elif name == "sp_tracks" and isinstance(a, dict) and re.match(r"^spotify:(playlist:[A-Za-z0-9]{10,40}|album:[A-Za-z0-9]{10,40}|collection:tracks)$", str(a.get("uri") or "")):
                    try:
                        off = max(0, min(20000, int(a.get("offset") or 0)))
                    except (TypeError, ValueError):
                        off = 0
                    if self.sp_live():
                        self.spotify.send_cmd("tracks", uri=str(a["uri"]), offset=off)
                    else:
                        self.deck_push("sptracks", dict(uri=str(a["uri"]), offset=off, total=0, name="", img="", items=[], error="Spotify isn't open on the PC"))
                elif name == "sp_playctx" and isinstance(a, dict) and re.match(r"^spotify:(playlist:[A-Za-z0-9]{10,40}|album:[A-Za-z0-9]{10,40}|collection:tracks)$", str(a.get("ctx") or "")):
                    t = str(a.get("uri") or "")
                    extra = dict(ctx=str(a["ctx"]))
                    if re.match(r"^spotify:(track|episode):[A-Za-z0-9]{10,40}$", t):
                        extra["uri"] = t
                    if a.get("shuffle") is not None:
                        extra["shuffle"] = bool(a.get("shuffle"))
                    self.spotify.send_cmd("playctx", **extra)
                elif name == "sp_update":
                    self.spicetify_apply()
                elif name == "sp_devices":
                    if self.spotify.listening():
                        self.spotify.send_cmd("devices")
                    else:
                        self.deck_push("spdevices", dict(items=[], error="Spotify isn't open on the PC"))
                elif name == "sp_transfer" and isinstance(a, dict) and a.get("id"):
                    self.spotify.send_cmd("transfer", id=str(a["id"])[:80], local=bool(a.get("local")))
                    self.hud("ok", "Spotify", r=f"playing on {str(a.get('name') or 'that device')[:24]}", ms=1600)
                elif name == "deck_scenes":
                    self.save_deck_scenes(a)
                elif name == "clips_get":
                    self.deck_push("deckclips", self.clips[:8] if self.settings.get("clips") else [])
                elif name == "clip_set" and isinstance(a, str) and a.strip():
                    t = a[:20000]
                    QApplication.clipboard().setText(t)
                    self.clips = [t] + [c for c in self.clips if c != t][:19]
                    self.hud("ok", "Copied from your phone", r=t.replace("\n", " ")[:40], ms=1800)
                elif name in DECK_DO:
                    self.on_action(name, a)
            except Exception as ex:
                print("deck action:", name, ex)

    def sound_sync(self):
        """The mixer is read while the island's sound panel is open OR a deck is connected."""
        self.audio.watch(bool(getattr(self, "snd_pc", False)) or getattr(self, "deck_n", 0) > 0)

    def deck_note(self, t):
        """A notification to the decks too: the island hides behind fullscreen games, the phone doesn't."""
        self.deck_push("decknote", dict(key=t["key"], cat=t["cat"], kicker=t["kicker"], title=str(t["title"])[:140],
                                        body=str(t.get("body") or "")[:240], actions=[a[0] for a in t["actions"]],
                                        timeout=t["timeout"] if not self.hidden_fs else max(120, t["timeout"])))

    def note_act(self, key, i):
        """A button tapped on the deck's copy of a notification."""
        idx = next((n for n, t in enumerate(self.toasts) if t["key"] == key), None)
        if idx is None:
            return self.deck_push("notedone", key)
        if idx == 0 and self.toast_until is not None and not self.hidden_fs:      # it's on the PC's screen too
            self.toast_at = 0
            return self.toast_pick(i)
        t = self.toasts.pop(idx)
        if idx == 0:
            self.toast_cb = []
        cb = t["actions"][i][1] if 0 <= i < len(t["actions"]) else None
        try:
            if cb:
                cb()
        except Exception as e:
            print("deck note action:", e)
        self.push_data()
        self.deck_push("notedone", key)

    def fg_game(self):
        """The game in front, if any (same list as the focus guard)."""
        if not WIN:
            return None
        try:
            import psutil
            hwnd = user32.GetForegroundWindow()
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            return island_study.game_name(psutil.Process(pid.value).exe(), buf.value)
        except Exception:
            return None

    def game_tick(self):
        """While a deck is open: tell it when a game is in front (it switches to game mode)."""
        if getattr(self, "deck_n", 0) <= 0:
            return
        g = self.fg_game() or (self.hidden_fs and getattr(self, "_game", None) and self._game["name"]) or None
        cur = getattr(self, "_game", None)
        if (g or None) != (cur["name"] if cur else None):
            self._game = dict(name=g, since=int(time.time() * 1000)) if g else None
            self.deck_push("game", self._game)

    def deck_file(self):
        try:
            with open(os.path.join(HERE, "deck.json"), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def deck_file_save(self, **kv):
        d = self.deck_file()
        d.update(kv)
        p = os.path.join(HERE, "deck.json")
        try:
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump(d, f, indent=1, ensure_ascii=False)
            os.replace(p + ".tmp", p)
        except Exception as e:
            print("deck.json:", e)

    def deck_tiles(self):
        t = self.deck_file().get("buttons")
        return t if isinstance(t, list) else None

    def save_deck_tiles(self, t):
        if not isinstance(t, list) or len(t) > 30:
            return
        clean = [dict(t=str(x.get("t", ""))[:24], c=str(x.get("c", ""))[:140]) for x in t
                 if isinstance(x, dict) and str(x.get("c", "")).strip()]
        self.deck_file_save(buttons=clean)
        self.deck_push("decktiles", clean)

    def save_deck_scenes(self, sc):
        """Volume scenes from the deck: [{"n": "Game", "v": {"valorant": 1.0, "spotify": 0.2, "master": 0.6}}]."""
        if not isinstance(sc, list) or len(sc) > 12:
            return
        clean = []
        for x in sc:
            if not isinstance(x, dict) or not isinstance(x.get("v"), dict):
                continue
            v = {}
            for k, val in list(x["v"].items())[:20]:
                try:
                    v[str(k).lower()[:40]] = max(0.0, min(1.0, round(float(val), 3)))
                except (TypeError, ValueError):
                    pass
            if v:
                clean.append(dict(n=str(x.get("n") or "Scene")[:18], v=v))
        self.deck_file_save(scenes=clean)
        self.deck_push("deckscenes", clean)

    def deck_snapshot(self):
        """Everything once, for a deck that just (re)connected."""
        self.push_settings()
        self.push_data()
        if self.media.last:
            self._art_sent = None
            self.push_media(self.media.last)
        s = self.sys.sample()
        if s:
            self.js("sys", s)
        if getattr(self, "_wx", None):
            self.js("wx", self._wx)
        if self._lyr:
            self.js("lyrics", self._lyr)
        self.js("launch", getattr(self, "_launch_js", []))
        self.push_timers()
        self._sysx = None
        st = getattr(self, "sp_state", None)
        if st:
            self.js("spstate", {k: v for k, v in st.items() if k != "at"})
        if self._snd:
            self.deck_push("sound", self._snd)
        if self._priv:
            self.deck_push("priv", self._priv)
        if self._bri:
            self.deck_push("bri", self._bri)
        if self._prayer_js:
            self.deck_push("prayer", self._prayer_js)
        if self.life is not None:
            self.deck_push("life", self.life.snapshot(self.model.today()))
        self.deck_push("decktiles", self.deck_tiles() or [])
        if getattr(self, "sp_devs", None) is not None:
            self.deck_push("spdevices", dict(items=self.sp_devs))
        self.sp_now_push(force=True)
        try:
            self.deck_push("deckver", str(int(os.path.getmtime(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")))))
        except OSError:
            pass
        sc = self.deck_file().get("scenes")
        self.deck_push("deckscenes", sc if isinstance(sc, list) else None)
        self.deck_push("game", getattr(self, "_game", None))
        for t in self.toasts[-3:]:
            self.deck_note(t)

    def usb_tick(self):
        """Tablet over USB (Android, adb): keep its localhost:47815 pointed at the PC; open the deck on it once."""
        if not (WIN and self.lan and self.settings.get("deck_usb")) or getattr(self, "_usb_busy", False):
            return
        self._usb_busy = True

        def run():
            try:
                ok, msg = island_dev.adb_reverse()
                opened = False
                if ok and msg != getattr(self, "_usb_last", None):
                    adb = island_dev.adb_path()
                    try:
                        import subprocess
                        subprocess.run([adb, "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", self.lan_url(usb=True)],
                                       capture_output=True, timeout=12, creationflags=island_dev.NO_WINDOW)
                        opened = True
                    except Exception as e:
                        print("usb open:", e)
                self.win_sig.emit(("usb", (ok, msg, opened)))
            finally:
                self._usb_busy = False
        threading.Thread(target=run, daemon=True).start()

    def dev_tick(self):
        while not self.lan_q.empty():
            kind, arg = self.lan_q.get_nowait()
            m = self.model
            day = m.today()
            if kind == "lan_water":
                self.add_water(day, int(arg))
            elif kind == "lan_tick":
                items = {t["key"]: t for t in m.items(day)}
                if arg in items and not items[arg].get("block"):
                    m.toggle(items[arg], day)
                    self.push_data()
        # RAM pressure on the always-on PC: 10 min above 92 % -> one heads-up an hour
        s = getattr(self, "_last_sys", None)
        now = time.time()
        if s and s.get("ram", 0) >= 92:
            self._ram_hi = self._ram_hi or now
            if now - self._ram_hi >= 600 and not self.daylog.done(dt.date.today(), f"ram-{dt.datetime.now():%H}"):
                self.daylog.mark(dt.date.today(), f"ram-{dt.datetime.now():%H}")
                top = ", ".join(f"{n} {g} GB" for n, g in island_dev.top_ram())
                self.notify(f"ram-{int(now)}", "R", "pc health", f"RAM full for 10 min ({s['ram']:.0f} %)", top, [("OK", None)], 40)
        else:
            self._ram_hi = 0

    def mock_on(self):
        return any(t.get("mock") for t in self.timers)

    def push_study(self, force=False):
        today = self.model.today()
        x = dict(exams=self.study.exams(today), min=self.study.week(today), papers=self.study.papers(),
                 chats={k: bool(v) for k, v in self.study.cfg.get("chats", {}).items()})
        if force or x != self._study_js:
            self._study_js = x
            self.js("study", x)

    def start_mock(self, cat):
        if self.mock_on():
            return self.hud("info", "A mock is already running", ms=1500)
        mins = island_study.EXAM_MIN[cat]
        name = island_study.SUBJ[cat]
        self.timers.append(dict(id=f"m{int(time.time() * 1000)}", end=time.time() + mins * 60, total=mins * 60,
                                label=f"Mock · {name}", mock=cat, warned=[]))
        self.push_timers()
        self.hud("timer", f"Mock · {name}", r=f"{mins // 60}h{mins % 60:02d}" if mins % 60 else f"{mins // 60}h", ms=2500)
        print(f"mock: {name} {mins} min (notifications held)")

    # ---------------------------------------------------------- swap / move sessions (island_moves.json, plan untouched)
    def find_item(self, word, day):
        """'maths' / 'phy' -> that day's study session; 'meal2', 'snack', 'gym', 'upper' -> the item whose title starts so."""
        items = [t for t in self.model.items(day) if t.get("movable")]
        cat = island_study.subject(word)
        if cat:
            hit = [t for t in items if t["cat"] == cat]
            if hit:
                return hit[0]
        w = re.sub(r"\s+", "", word.lower())
        if w in ("gym", "workout", "training"):
            hit = [t for t in items if t["cat"] == "W"]
            return hit[0] if hit else None
        for t in items:
            if re.sub(r"\s+", "", t["title"].lower()).startswith(w):
                return t
        return None

    @staticmethod
    def parse_hm(text):
        """'7pm', '7:30 pm', '19:00', '7' (1-7 without am/pm = evening) -> 'HH:MM'."""
        m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", text.strip().lower())
        if not m:
            return None
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if mi > 59 or h > 23:
            return None
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        elif not ap and 1 <= h <= 7:
            h += 12
        return f"{h:02d}:{mi:02d}"

    def swap_items(self, day, ka, kb):
        m = self.model
        m.reload()
        items = {t["key"]: t for t in m.items(day)}
        a, b = items.get(ka), items.get(kb)
        if not a or not b or ka == kb or not a.get("movable") or not b.get("movable"):
            self.hud("info", "Can't swap those", ms=1500)
            return False
        ta, tb = a["time"], b["time"]
        ch = {}
        for it, new in ((a, tb), (b, ta)):
            ch[m.move_key(it)] = None if new == it.get("plan_time", it["time"]) else new
        m.set_moves(day, ch)
        print(f"moves: {day} swapped {a['title']} ({ta}) <-> {b['title']} ({tb})")
        self.hud("timer", f"{a['title']} ↔ {b['title']}", r=f"{h12(tb)} · {h12(ta)}", ms=2400)
        self.push_data()
        return True

    def move_item(self, day, it, hm):
        m = self.model
        new = None if hm == it.get("plan_time", it["time"]) else hm
        m.set_moves(day, {m.move_key(it): new})
        print(f"moves: {day} {it['title']} -> {hm}")
        self.hud("timer", f"{it['title']} moved", r=h12(hm), ms=2000)
        self.push_data()

    def move_reset(self, day):
        m = self.model
        m.reload()
        n = len(m.moves.get(day.isoformat(), {}))
        if n:
            m.set_moves(day, {k: None for k in list(m.moves.get(day.isoformat(), {}))})
        self.hud("timer", "Back to the plan's times" if n else "Nothing was moved", ms=1800)
        self.push_data()

    def move_cmd(self, low):
        """swap maths physics · move maths 7pm · move maths back · move reset"""
        day = self.model.today()
        if low in ("move reset", "swap reset", "unswap", "unmove", "moves reset", "reset moves"):
            self.move_reset(day)
            return True
        m = re.match(r"^(?:swap|switch)\s+(.+?)\s+(?:and\s+|with\s+|&\s+|<->\s+|↔\s+)?(\S+)$", low)
        if m:
            a, b = self.find_item(m.group(1), day), self.find_item(m.group(2), day)
            if not a or not b:
                return self.hud("info", "swap maths physics · swap meal2 snack", ms=2200)
            return self.swap_items(day, a["key"], b["key"])
        m = re.match(r"^move\s+(.+?)\s+(?:to\s+)?(back|plan|\d{1,2}(?::\d{2})?\s*(?:am|pm)?)$", low)
        if m:
            it = self.find_item(m.group(1), day)
            if not it:
                return self.hud("info", "move maths 7pm · move maths back", ms=2200)
            hm = it.get("plan_time", it["time"]) if m.group(2) in ("back", "plan") else self.parse_hm(m.group(2))
            if not hm:
                return self.hud("info", "move maths 7pm", ms=1800)
            self.move_item(day, it, hm)
            return True
        return "nomatch"

    def study_cmd(self, low, t):
        """Command bar: focus [subject|stop], mock <subject>, exam <subject> <date>, score <paper> <n>, chat <subject>."""
        today = self.model.today()
        m = re.match(r"^focus(?:\s+(\S+))?$", low)
        if m:
            w = m.group(1)
            if w in ("stop", "off", "end"):
                if self.pomo:
                    if not self.pomo.get("free"):
                        self.state["pomo_skip"] = self.pomo["key"]
                    self.pomo, self.pomo_key = None, None
                    self.save_state()
                    self.js("pomo", None)
                return self.hud("timer", "Focus stopped", ms=1300)
            cat = island_study.subject(w) if w else (self.pomo["cat"] if self.pomo else None)
            if self.pomo and not self.pomo.get("free") and (not w or cat == self.pomo["cat"]):
                self.pomo["t0"] = time.time()                    # restart the 50/10 clock from now
                self.state["pomo_anchor"] = [self.pomo["key"], self.pomo["t0"]]
                self.save_state()
            else:
                if not cat:
                    return self.hud("info", "focus physics · maths · bio · history", ms=2200)
                self.pomo = dict(key=f"free-{int(time.time())}", cat=cat, name=island_study.SUBJ[cat], t0=time.time(),
                                 end=time.time() + 3 * 3600, free=True)
                self.pomo_key = self.pomo["key"]
            self.pomo_phase = None
            self.js("pomo", self.pomo_js())
            return True
        m = re.match(r"^mock\s+(\S+)$", low)
        if m:
            cat = island_study.subject(m.group(1))
            return self.start_mock(cat) if cat else self.hud("info", "mock physics · maths · bio · history", ms=2200)
        m = re.match(r"^exam\s+(?!(?:physics|phy|p|maths?\s*[12]?|m\s*[12]|bio(?:logy)?|b|history|hist|h)\s)(.+)$", low)
        if m:                                   # any other exam: "exam calculus midterm 24 nov"
            what, d = island_life.split_date(t.split(None, 1)[1], today, island_study.parse_date)
            if not d or not what:
                return self.hud("info", "exam calculus 24 nov", ms=2000)
            self.study.set_exam(what[:30], d)
            self.push_study(True)
            return self.hud("ok", f"{what[:30]}: {d.strftime('%a %d %b').replace(' 0', ' ')}", r=f"{(d - today).days} days", ms=2200)
        m = re.match(r"^exam\s+(physics|phy|p|maths?\s*[12]?|m\s*[12]|bio(?:logy)?|b|history|hist|h)\s+(.+)$", low)
        if m:
            w = m.group(1).replace(" ", "")
            name = {"p": "Physics", "phy": "Physics", "physics": "Physics", "b": "Biology", "bio": "Biology",
                    "biology": "Biology", "h": "History", "hist": "History", "history": "History"}.get(w)
            if not name:
                name = "Maths 2" if w.endswith("2") else "Maths 1"
            d = island_study.parse_date(m.group(2), today)
            if not d:
                return self.hud("info", "exam physics 14 dec", ms=2000)
            self.study.set_exam(name, d)
            self.push_study(True)
            return self.hud("ok", f"{name}: {d.strftime('%a %d %b').replace(' 0', ' ')}", r=f"{(d - today).days} days", ms=2200)
        m = re.match(r"^score\s+(\S+)\s+(\d{1,3})$", low)
        if m:
            pid = self.study.find_paper(m.group(1))
            if not pid:
                return self.hud("info", "score P25S 168  (paper id from the Study tab)", ms=2500)
            n = int(m.group(2))
            self.study.set_score(pid, min(200, n), today)
            self.push_study(True)
            return self.hud("ok", f"{pid}: {n}/200", ms=2000)
        m = re.match(r"^chat\s+(\S+)$", low)
        if m:
            return self.open_chat(island_study.subject(m.group(1)))
        m = re.match(r"^(?:wrong|mistake|err)\s+(\S+)\s+(.+)$", low)
        if m and self.cards is not None:
            cat = island_study.subject(m.group(1))
            if not cat:
                return self.hud("info", "wrong physics <question> = <answer>", ms=2500)
            rest = t.split(None, 2)[2] if len(t.split(None, 2)) > 2 else ""
            q, _, a = rest.partition("=")
            c = self.cards.add_mistake(cat, q.strip(), a.strip() or "(check it in your notes)", today)
            self.push_cards()
            return self.hud("ok", "Mistake saved", r="back tomorrow, then in 3 and 7 days", ms=2200)
        return "nomatch"

    # ---------------------------------------------------------- "next one's ready" (6 Nov 2026)
    NX_EARLY_MIN = 10          # ticked >= 10 min before the block's planned end = finished early

    def next_tick(self):
        """Every 15 s: a study session got ticked today (island, phone or a subject chat editing plan.json).
        Finished early (or an extra session) -> offer the next one right away (setting next_nudge)."""
        try:
            if not self.settings.get("next_nudge", True) or self.mock_on():
                return
            m = self.model
            st = m.study
            if not st.ok:
                return
            now = dt.datetime.now()
            day = m.today(now)
            iso = day.isoformat()
            done = {}
            for s in ("P", "M", "B", "H"):
                for it in st.items(s):
                    if it.get("done") == iso:
                        done[it["id"]] = s
            seen = getattr(self, "_nx_seen", None)
            if not seen or seen[0] != iso:
                self._nx_seen = (iso, set(done))      # new day / fresh start: what is already ticked doesn't count
                return
            new = [i for i in done if i not in seen[1]]
            self._nx_seen = (iso, set(done))
            if not new:
                return
            slots = {t["ref"].get("id"): t for t in m.items(day) if t.get("src") == "S"}
            for iid in new:
                t = slots.get(iid)
                if t is not None:
                    _s0, e0 = m.span(t, day)
                    if e0 is None or (e0 - now).total_seconds() < self.NX_EARLY_MIN * 60:
                        continue                      # on time or late: the plan's own next block is fine
                elif now.hour >= 21 or now.hour < 6:
                    continue                          # an extra session late at night: no push
                print(f"next: {iid} ticked early -> offering the next session")
                self.next_nudge(done[iid])
                break
        except Exception as e:
            print("next_tick:", e)

    def next_pick(self, done_s):
        """(subject, plan item, sessions behind): the subject furthest behind (sessions left > slots left before
        the mocks), ties Maths > History > Physics > Biology; nobody behind -> the same subject again."""
        st = self.model.study
        order = ["M", "H", "P", "B"]
        try:
            pace = st.pace(self.model.today())
        except Exception:
            pace = {}
        behind = {s: rem - avail for s, (rem, avail) in pace.items() if rem > avail}
        cands = sorted(behind, key=lambda s: (-behind[s], order.index(s)))
        cands += [done_s] + [s for s in order if s != done_s]
        for s in cands:
            und = st.undone(s)
            if und:
                return s, und[0], behind.get(s, 0)
        return None

    def next_nudge(self, done_s, again=0):
        pick = self.next_pick(done_s)
        if not pick:
            return
        s, it, gap = pick
        cat = core.SUBJECT_CAT.get(s, "R")
        name = core.SUBJECT_EN.get(s, s)
        word = {"P": "physics", "M": "maths", "B": "bio", "H": "history"}.get(s, "physics")
        iid = it.get("id", "")
        key = f"next-{self.model.today().isoformat()}-{iid}-{again}"

        def start():
            self.study_cmd(f"focus {word}", f"focus {word}")
            self.open_chat(cat)

        def still_free():
            if self.pomo is None and not any(x.get("id") == iid and x.get("done") for x in self.model.study.items(s)):
                self.next_nudge(done_s, again + 1)

        def later():
            if again < 2:
                QTimer.singleShot(10 * 60_000, still_free)

        why = f"{name} is {gap} session{'s' if gap != 1 else ''} behind" if gap else "You finished early"
        self.notify(key, cat, "done early 🔥" if not again else "still free?", f"Next one's ready · {name}",
                    it.get("text", ""), actions=[("Start now", start), ("In 10 min", later), ("Not now", None)],
                    timeout=90, kind="info", body2=why + " · start it now?")

    @staticmethod
    def open_url(url):
        """claude.ai chats / projects open in the Windows Claude app (claude:// link); everything else in the browser."""
        u = str(url or "")
        m = re.match(r"^https?://(?:www\.)?claude\.ai/((?:chat|project)/[A-Za-z0-9-]+|new)(?:[/?#].*)?$", u)
        if m and WIN:
            try:
                os.startfile("claude://claude.ai/" + m.group(1))
                return "app"
            except OSError as e:
                print("claude link -> app failed, using the browser:", e)
        if WIN:
            os.startfile(u)
        return "browser"

    def open_chat(self, cat):
        url = self.study.cfg.get("chats", {}).get(cat or "")
        if url and WIN:
            self.open_url(url)
            self.js("mode", "pill")
        elif not url:
            self.hud("info", "No chat saved for that subject (study.json)", ms=2200)

    # ---------------------------------------------------------- file shelf
    def setup_drop(self):
        """Files dragged onto the island land on the shelf (paths only - the files stay where they are)."""
        self.setAcceptDrops(True)
        hooked = getattr(self, "_drop_hooked", set())
        for w in [self.view] + self.view.findChildren(QWidget):
            if id(w) not in hooked:
                hooked.add(id(w))
                w.setAcceptDrops(True)
                w.installEventFilter(self)
        self._drop_hooked = hooked
        if not hasattr(self, "shelf"):
            self.shelf = [x for x in self.state.get("shelf", []) if isinstance(x, str) and os.path.exists(x)][:20]
            self._dropping = self._shelf_out = False

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t not in (QEvent.DragEnter, QEvent.DragMove, QEvent.DragLeave, QEvent.Drop):
            return False
        try:
            if t == QEvent.DragLeave:
                if self._dropping:
                    self._dropping = False
                    self.js("shelfOver", False)
                return False
            md = ev.mimeData()
            if md is None or not md.hasUrls():
                return False                    # text etc.: the page can have it
            files = [u.toLocalFile() for u in md.urls() if u.isLocalFile()]
            if not files or self._shelf_out:     # a web link, or our own drag going out: never let the page open it
                ev.ignore()
                return True
            ev.setDropAction(Qt.CopyAction)
            ev.accept()
            if t == QEvent.Drop:
                self._dropping = False
                self.js("shelfOver", False)
                if getattr(self, "_drop_target", "shelf") == "phone":
                    self.drop_share(files)
                else:
                    self.shelf_add(files)
            elif not self._dropping:
                self._dropping = True
                self._drop_target = "shelf"
                self.js("shelfOver", True)
            elif t == QEvent.DragMove and time.time() - getattr(self, "_drop_mv", 0) > 0.03:
                self._drop_mv = time.time()
                p = QCursor.pos() - self.pos()
                self.js("dropMove", [round(p.x() / self.zoom), round(p.y() / self.zoom)])
            return True
        except Exception as e:
            print("shelf drop:", e)
            return False

    def buddy_arm(self):
        if not self.state.get("r20_off"):              # r20: you asked for voice + weather to go away (Settings brings them back)
            self.state["r20_off"] = True
            self.save_state()
            for k2 in ("voice", "weather"):
                if self.settings.get(k2, False):
                    self.settings.set(k2, False)
            print("settings: voice + weather off")
            QTimer.singleShot(0, self.push_settings)
        if not self.state.get("icon_once"):             # r18d: icons instead of characters (once; your pick stays after)
            self.state["icon_once"] = True
            self.save_state()
            if self.settings.get("buddy", "orb") in ("bongo", "drum", "cat"):
                self.settings.set("buddy", "orb")
                print("buddy: orb")
                QTimer.singleShot(0, self.push_settings)
        t = getattr(self, "_buddy_t", None)
        if t is None:
            t = self._buddy_t = QTimer(self)
            t.timeout.connect(self.buddy_tick)
            self._b_last, self._b_pos, self._b_btn, self._b_tap = 0, None, False, 0.0
        if WIN and not t.isActive():                   # also counts inputs for the Inputs widget, so it always runs
            t.start(30)

    def buddy_tick(self):
        """Every 30 ms: did a key / click happen? (Windows' last-input time + the mouse buttons; never which key.)
        Counts it for the Inputs widget, and in the pill the icon / buddy reacts. Not in fullscreen games."""
        if not self.ready or self.hidden_fs:
            return
        li = LASTINPUTINFO()
        li.cbSize = ctypes.sizeof(li)
        if not user32.GetLastInputInfo(ctypes.byref(li)):
            return
        pos = QCursor.pos()
        btn = bool(user32.GetAsyncKeyState(0x01) & 0x8000) or bool(user32.GetAsyncKeyState(0x02) & 0x8000)
        tap = (btn and not self._b_btn) or (li.dwTime != self._b_last and pos == self._b_pos and not btn and self._b_last)
        self._b_last, self._b_pos, self._b_btn = li.dwTime, pos, btn
        now = time.time()
        if tap:
            self.count_tap(now)
        bud = self.settings.get("buddy", "orb")
        if tap and bud not in ("off", "eyes") and self.mode == "pill" and self.isVisible() \
                and now - self._b_tap > (0.35 if bud in ("cat", "bunny", "ghost", "slime", "robot") else 0.06):
            self._b_tap = now
            self.page.runJavaScript("window.island && island.tap && island.tap()")
        if now - getattr(self, "_fire_t", 0) > 0.5:       # typing fast (36+ in 8 s): the icon glows harder
            self._fire_t = now
            q = self.__dict__.setdefault("_tapq", [])
            while q and now - q[0] > 8:
                q.pop(0)
            fire = len(q) >= 36 or (getattr(self, "_fire", False) and len(q) >= 16)
            if fire != getattr(self, "_fire", False):
                self._fire = fire
                self.page.runJavaScript(f"window.island && island.tapfire && island.tapfire({'true' if fire else 'false'})")
        if now - getattr(self, "_taps_push", 0) > 30:
            self._taps_push = now
            self.push_taps()

    def count_tap(self, now):
        d = dt.date.today().isoformat()
        t = self.state.get("taps")
        if not t or t.get("d") != d:
            t = self.state["taps"] = dict(d=d, h=[0] * 24)
        t["h"][dt.datetime.now().hour] += 1
        self.__dict__.setdefault("_tapq", []).append(now)
        if len(self._tapq) > 300:
            del self._tapq[:100]
        if now - getattr(self, "_taps_saved", 0) > 600:
            self._taps_saved = now
            self.save_state()

    def push_taps(self):
        t = self.state.get("taps")
        if t and t.get("d") == dt.date.today().isoformat():
            self.js("taps", dict(n=sum(t["h"]), h=t["h"]))

    def temps(self):
        """CPU / GPU temperature + fans from the laptop's ACPI sensors, every 3 s (cached in between)."""
        now = time.time()
        if now - getattr(self, "_temp_t", 0) < 3:
            return getattr(self, "_temp", None)
        self._temp_t = now
        try:
            if getattr(self, "_asus", None) is None:
                import island_temp
                self._asus = island_temp.AsusSensors()
                print("temps:", "reading the laptop's ACPI sensors" if self._asus.ok else "no ASUS ACPI device (ATKACPI) here")
            self._temp = self._asus.read() if self._asus.ok else None
            c = (self._temp or {}).get("cpu") or 0
            if c >= 95:
                self._hot_since = getattr(self, "_hot_since", None) or now
                if now - self._hot_since > 60 and now - getattr(self, "_hot_warned", 0) > 1800 and not self.hidden_fs:
                    self._hot_warned = now
                    fan = (self._temp or {}).get("cpu_fan")
                    self.hud("chip", f"Laptop is hot · {c}°", r=f"fans {fan} rpm" if fan else "check the vents / power mode", ms=4200)
                    print(f"temps: hot warning ({c}°)")
            else:
                self._hot_since = None
            if getattr(self, "_game_t0", None):
                self._game_peak = max(getattr(self, "_game_peak", 0), c)
            if self._temp and not getattr(self, "_temp_logged", False):
                self._temp_logged = True
                print("temps:", self._temp)
        except Exception as e:
            self._temp = None
            if not getattr(self, "_temp_err", False):
                self._temp_err = True
                print("temps:", e)
        return self._temp

    # ---------------------------------------------------------- dongle mouse / keyboard batteries (island_batt.py)
    def batt_sync(self):
        """Run the battery reader while the setting is on and no fullscreen game is in front (it never runs in games)."""
        want = WIN and self.settings.get("devbat", True) and not self.hidden_fs
        p = getattr(self, "_battp", None)
        alive = p is not None and p.poll() is None
        if want and not alive:
            if time.time() - getattr(self, "_batt_t", 0) < 50:
                return
            self._batt_t = time.time()
            import subprocess
            exe = sys.executable.replace("pythonw.exe", "python.exe") if sys.executable.lower().endswith("pythonw.exe") else sys.executable
            try:
                again = ["--again"] if getattr(self, "_batt_runs", 0) else []     # the one-time learning scan: first run only
                self._batt_runs = getattr(self, "_batt_runs", 0) + 1
                self._battp = subprocess.Popen([exe, "-u", os.path.join(HERE, "island_batt.py")] + again, stdin=subprocess.PIPE,
                                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                               creationflags=0x08000000, cwd=HERE)
                threading.Thread(target=self.batt_reader, args=(self._battp,), daemon=True).start()
                print("devbat: reader started")
            except Exception as e:
                print("devbat: can't start the reader:", e)
        elif not want and alive:
            try:
                p.kill()
            except Exception:
                pass
            print("devbat: reader stopped (game in front or setting off)")

    def batt_reader(self, p):
        n = 0
        try:
            for line in p.stdout:
                line = line.strip()
                if line.startswith("{"):
                    try:
                        self.win_sig.emit(("devbat", json.loads(line).get("dev") or {}))
                    except ValueError:
                        pass
                elif line and n < 160:
                    n += 1
                    print("devbat:", line.lstrip("# ")[:200])
        except Exception:
            pass
        self.win_sig.emit(("devbat_end", p.poll()))

    def push_devbat(self):
        out = [dict(k=k, name=v.get("name") or k, v=v.get("v"), chg=v.get("chg")) for k, v in (getattr(self, "_devbat", None) or {}).items()]
        bt = [dict(k="bt", name=d.get("name"), v=d.get("v"), chg=None) for d in (self._bt or [])]
        if bt:                      # the headset over Bluetooth has a level; its dongle twin without one goes
            out = [x for x in out if not (x["k"] == "headset" and x["v"] is None)]
        out = sorted(out + bt, key=lambda x: ["mouse", "keyboard", "headset", "bt"].index(x["k"]) if x["k"] in ("mouse", "keyboard", "headset", "bt") else 9)
        self.js("devbat", out)

    def devbat_event(self, dev):
        old = getattr(self, "_devbat", None) or {}
        self._devbat = dev
        low = self.__dict__.setdefault("_devbat_low", set())
        for k, d in dev.items():
            v = d.get("v")
            if v is None:
                continue
            if v <= 15 and not d.get("chg") and k not in low and self.settings.get("battery", True):
                low.add(k)
                self.hud("batlow", d.get("name") or k, v=v / 100, r=f"{v}%", ms=3500)
            elif v > 25 or d.get("chg"):
                low.discard(k)
            if k not in old:
                print(f"devbat: {d.get('name')} {v}%{' (charging)' if d.get('chg') else ''}")
        self.push_devbat()

    def device_probe(self):
        """Read-only list of the connected input / audio devices (names + USB ids) -> devices_probe.json.
        Dongle mice, keyboards and headsets keep their battery to their own brand's protocol; the ids say which one."""
        import subprocess
        ps = ("$d = Get-PnpDevice -PresentOnly | Where-Object { $_.Class -in 'HIDClass','Mouse','Keyboard','AudioEndpoint','MEDIA','Bluetooth','USB' }; "
              "$d | Select-Object Class,FriendlyName,Manufacturer,InstanceId | ConvertTo-Json -Compress")
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                               capture_output=True, text=True, timeout=60, creationflags=0x08000000)
            out = r.stdout.strip() or "[]"
            with open(os.path.join(HERE, "devices_probe.json"), "w", encoding="utf-8") as f:
                f.write(out)
            print(f"devices: probe saved ({len(out)} bytes)")
        except Exception as e:
            print("devices: probe failed:", e)

    def drop_share(self, files):
        """Dropped on Phone: a one-time link (home Wi-Fi, 15 min) shown as a QR code. Folders go on the shelf."""
        f = next((x for x in files if os.path.isfile(x)), None)
        L = self.lan
        if not f or L is None or not hasattr(L, "share"):
            if f and L is None:
                self.hud("phone", "Turn on the phone deck first", r="Settings", ms=2200)
            return self.shelf_add(files)
        sid = L.share(f, 15)
        url = self.lan_url().split("/deck")[0] + "/f/" + sid
        print("drop: shared", os.path.basename(f), "to the phone for 15 min")
        self.js("dropShare", dict(name=os.path.basename(f), size=os.path.getsize(f), url=url, mins=15))
        rest = [x for x in files if x != f]
        if rest:
            self.shelf_add(rest)

    def shelf_js(self):
        out = []
        for pth in getattr(self, "shelf", []):
            try:
                isdir = os.path.isdir(pth)
                out.append(dict(path=pth, name=os.path.basename(pth.rstrip("\\/")) or pth, dir=isdir,
                                size=0 if isdir else os.path.getsize(pth), where=os.path.basename(os.path.dirname(pth)),
                                icon=self.app_icon(pth)))
            except OSError:
                continue
        return out

    def shelf_save(self):
        self.state["shelf"] = self.shelf
        self.save_state()
        self.js("shelf", self.shelf_js())

    def shelf_add(self, files):
        new = [f for f in files if f not in self.shelf]
        self.shelf = (self.shelf + new)[-20:]
        self.shelf_save()
        n = len(new)
        self.hud("shelf", f"{n} file{'s' if n != 1 else ''} on the shelf" if n else "Already on the shelf",
                 r=f"{len(self.shelf)} total" if len(self.shelf) > n else "", ms=1500)

    def shelf_act(self, name, arg):
        try:
            if name == "shelf_clear":
                self.shelf = []
                return self.shelf_save()
            i = int(arg)
            pth = self.shelf[i]
        except (ValueError, TypeError, IndexError):
            return
        if name == "shelf_rm" or not os.path.exists(pth):
            self.shelf.pop(i)
            return self.shelf_save()
        if name == "shelf_open":
            os.startfile(pth)
        elif name == "shelf_dir":
            if WIN:
                QProcess.startDetached("explorer.exe", ["/select,", os.path.normpath(pth)])
        elif name == "shelf_drag":
            QTimer.singleShot(0, lambda: self.shelf_drag(pth))

    def shelf_drag(self, pth):
        """A real Windows drag of that file out of the island (copies it where you drop it)."""
        if pth not in self.shelf or not os.path.exists(pth):
            return
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(pth)])
        d = QDrag(self)
        d.setMimeData(md)
        try:
            from PySide6.QtWidgets import QFileIconProvider
            from PySide6.QtCore import QFileInfo
            pm = QFileIconProvider().icon(QFileInfo(pth)).pixmap(48, 48)
            if not pm.isNull():
                d.setPixmap(pm)
        except Exception:
            pass
        self._shelf_out = True
        try:
            res = d.exec(Qt.CopyAction)
        finally:
            self._shelf_out = False
        # the page never saw the mouse come up (Windows owned it during the drag): tell it
        try:
            fp = self.view.focusProxy() or self.view
            local = QPointF(fp.mapFromGlobal(QCursor.pos()))
            QApplication.sendEvent(fp, QMouseEvent(QEvent.MouseButtonRelease, local, QPointF(QCursor.pos()),
                                                   Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
        except Exception as e:
            print("shelf drag release:", e)
        if res != Qt.IgnoreAction and pth in self.shelf:     # dropped somewhere: it leaves the shelf
            self.shelf.remove(pth)
            self.shelf_save()

    # ---------------------------------------------------------- quick launch (launch.json, edit freely)
    def load_launch(self):
        p = os.path.join(HERE, "launch.json")
        try:
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    items = json.load(f)
            else:
                items = island_win.guess_launch() if WIN else []
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(items, f, indent=2, ensure_ascii=False)
                print(f"launch: made launch.json with {len(items)} apps (edit it to change the row)")
        except Exception as e:
            print("launch.json:", e)
            items = []
        self.launch_all = [x for x in items if isinstance(x, dict) and (x.get("path") or x.get("url") or x.get("app"))]
        self.launch = self.launch_all[:LAUNCH_MAX]
        out = []
        for i, x in enumerate(self.launch):
            icon = (self.app_icon(x["path"]) if x.get("path") else self.web_icon(x.get("url")) if x.get("url")
                    else self.pkg_icon(x.get("app")))
            out.append(dict(i=i, name=str(x.get("name") or os.path.basename(x.get("path") or x.get("url") or "?"))[:20],
                            icon=icon, web=bool(x.get("url"))))
        self._launch_js = out
        self.js("launch", out)

    def save_launch(self):
        p = os.path.join(HERE, "launch.json")
        try:
            with open(p + ".tmp", "w", encoding="utf-8") as f:
                json.dump(self.launch_all, f, indent=2, ensure_ascii=False)
            os.replace(p + ".tmp", p)
        except Exception as e:
            print("launch.json save:", e)
        self.load_launch()

    # ---- the "+" picker: every app in the Start menu, incl. Store/MSIX apps like the Windows Claude app
    def refresh_apps(self, push):
        if not WIN:
            return
        if getattr(self, "_apps_busy", False):
            self._apps_want = getattr(self, "_apps_want", False) or push     # answer the picker when it lands
            return
        self._apps_busy = True

        def run():
            lst = []
            try:
                lst = island_win.start_apps()
            finally:
                self.win_sig.emit(("apps", (lst, push)))
        threading.Thread(target=run, daemon=True).start()

    def apps_js(self):
        return [dict(n=a["name"], id=a["appid"]) for a in (self.apps_cat or [])]

    def launch_entry(self, appid):
        a = next((x for x in (self.apps_cat or []) if x["appid"] == appid), None)
        if not a:
            return None
        lnk = island_win.start_lnk(a["name"])
        return dict(name=a["name"][:20], path=lnk) if lnk else dict(name=a["name"][:20], app=appid)

    def launch_pin(self, appid):
        e = self.launch_entry(str(appid))
        if not e:
            return self.hud("info", "Couldn't find that app", ms=1500)
        if any((x.get("path") and x.get("path") == e.get("path")) or (x.get("app") and x.get("app") == e.get("app"))
               for x in self.launch_all):
            return self.hud("info", e["name"] + " is already there", ms=1500)
        web = next((i for i, x in enumerate(self.launch_all)       # the website shortcut of the same app -> swap it
                    if x.get("url") and str(x.get("name", "")).lower() == e["name"].lower()), None)
        if web is not None:
            self.launch_all[web] = e
        else:
            self.launch_all.insert(min(len(self.launch), LAUNCH_MAX), e)
        self.save_launch()

    def migrate_claude(self):
        """Once: the claude.ai web shortcut becomes the Windows Claude app when it's installed."""
        if self.state.get("claude_app_done"):
            return
        a = next((x for x in (self.apps_cat or []) if x["name"].strip().lower() == "claude"), None)
        if not a:
            return
        for i, x in enumerate(self.launch_all):
            if "claude.ai" in str(x.get("url") or ""):
                e = self.launch_entry(a["appid"])
                if e:
                    self.launch_all[i] = e
                    print("launch: claude.ai -> the Claude app")
                    self.save_launch()
                break
        self.state["claude_app_done"] = True
        self.save_state()

    def pkg_icon(self, appid):
        """Icon for a Start-menu app id (Store/MSIX apps have no .lnk): found once in the background, kept in icons/apps.json."""
        if not appid:
            return None
        key = "app:" + appid
        if key in self._icons:
            return self._icons[key]
        if not hasattr(self, "_appsrc"):
            try:
                with open(os.path.join(HERE, "icons", "apps.json"), encoding="utf-8") as f:
                    self._appsrc = json.load(f)
            except Exception:
                self._appsrc = {}
        src = self._appsrc.get(appid)
        url = None
        if src and os.path.exists(src):
            if src.lower().endswith((".png", ".jpg", ".jpeg")):
                try:
                    with open(src, "rb") as f:
                        raw = f.read()
                    url = ("data:image/png;base64," if raw[:4] == b"\x89PNG" else "data:image/jpeg;base64,") + base64.b64encode(raw).decode()
                except Exception as e:
                    print("app icon:", e)
            else:
                url = self.app_icon(src)
            self._icons[key] = url
            return url
        self._icons[key] = None                  # don't look twice in one run
        if WIN:
            def run():
                f = island_win.app_icon_src(appid)
                if f:
                    self.win_sig.emit(("appicon", (appid, f)))
            threading.Thread(target=run, daemon=True).start()
        return None

    def app_match(self, q):
        """Best Start-menu app for what you typed: exact name, then starts with it, then contains it."""
        q = q.lower().strip()
        best = None
        for a in self.apps_cat or []:
            n = a["name"].lower()
            sc = 3 if n == q else 2 if n.startswith(q) else 1 if q in n else 0
            if sc and (best is None or sc > best[0] or (sc == best[0] and len(n) < len(best[1]["name"]))):
                best = (sc, a)
        return best[1] if best else None

    def open_app(self, appid):
        e = self.launch_entry(str(appid))
        if not e:
            return self.hud("info", "Couldn't find that app", ms=1500)
        self.run_app(e["name"], path=e.get("path"), appid=e.get("app"))
        self.hud("ok", f"Opening {e['name'][:24]}", ms=1100)

    def run_app(self, name, path=None, appid=None):
        """Start it, then make sure its window really comes to the front (it used to open behind, or stay in the tray)."""
        if WIN:
            try:
                user32.AllowSetForegroundWindow(-1)          # ASFW_ANY: the app may take the front itself
            except Exception:
                pass
        if appid:
            try:
                os.startfile("shell:AppsFolder\\" + appid)
            except OSError:
                QProcess.startDetached("explorer.exe", ["shell:AppsFolder\\" + appid])
        else:
            os.startfile(path)
        if WIN:
            own = set(self.own_hwnds())
            threading.Thread(target=island_win.bring_front, args=(name, own), daemon=True).start()

    def launch_edit(self, name, arg):
        try:
            if name == "launch_rm":
                i = int(arg)
                if 0 <= i < len(self.launch_all):
                    self.launch_all.pop(i)
            elif name == "launch_move":
                i, j = int(arg[0]), int(arg[1])
                if 0 <= i < len(self.launch_all) and 0 <= j < len(self.launch_all) and i != j:
                    self.launch_all.insert(j, self.launch_all.pop(i))
            self.save_launch()
        except Exception as e:
            print("launch edit:", e)

    def web_icon(self, url):
        """A web shortcut's real site icon (e.g. Claude's logo for claude.ai) as a data URL. Fetched once in the
        background into icons/<site>.img, then the launch row is pushed again. No icon -> the letter stays."""
        import urllib.parse
        try:
            dom = (urllib.parse.urlsplit(str(url or "")).hostname or "").lower()
        except Exception:
            dom = ""
        if not dom:
            return None
        key = "web:" + dom
        if key in self._icons:
            return self._icons[key]
        p = os.path.join(HERE, "icons", re.sub(r"[^a-z0-9.-]", "_", dom) + ".img")
        self._icons[key] = None
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    raw = f.read()
                mime = ("image/png" if raw[:4] == b"\x89PNG" else "image/x-icon" if raw[:4] == b"\x00\x00\x01\x00"
                        else "image/svg+xml" if b"<svg" in raw[:300] else "image/jpeg" if raw[:2] == b"\xff\xd8" else None)
                if mime:
                    self._icons[key] = f"data:{mime};base64," + base64.b64encode(raw).decode()
            except Exception as e:
                print("web icon:", e)
            return self._icons[key]

        def fetch():
            for src in (f"https://www.google.com/s2/favicons?domain={dom}&sz=64",
                        f"https://icons.duckduckgo.com/ip3/{dom}.ico"):
                try:
                    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (island)"})
                    raw = urllib.request.urlopen(req, timeout=8).read()
                    if len(raw) > 120:
                        os.makedirs(os.path.dirname(p), exist_ok=True)
                        with open(p + ".tmp", "wb") as f:
                            f.write(raw)
                        os.replace(p + ".tmp", p)
                        self._icons.pop(key, None)
                        self.win_sig.emit(("webicon", dom))
                        return
                except Exception as e:
                    print("web icon fetch:", dom, e)
        threading.Thread(target=fetch, daemon=True).start()
        return None

    def do_launch(self, i):
        try:
            x = self.launch[int(i)]
            if x.get("app") or x.get("path"):
                self.run_app(str(x.get("name") or ""), path=x.get("path"), appid=x.get("app"))
            else:
                self.open_url(x["url"])
            if self.mode == "panel":
                self.js("mode", "pill")
        except Exception as e:
            print("launch:", e)
            self.hud("info", "Couldn't open that", ms=1500)

    def push_timers(self):
        self.state["timers"] = self.timers
        self.save_state()
        self.js("timers", [dict(t, end=int(t.get("end", 0) * 1000), start=int(t.get("start", 0) * 1000)) for t in self.timers])

    def add_timer(self, secs, label=""):
        self.timers.append(dict(id=f"t{int(time.time() * 1000)}", end=time.time() + secs, total=secs, label=label))
        self.push_timers()

    @staticmethod
    def parse_dur(s):
        """'25' '25m' '1h30' '1h 30m' '90s' '2:30' (+ optional label) -> (seconds, label)"""
        s = s.strip().lower()
        m = re.match(r"^(?:(\d+)\s*h(?:ours?|rs?)?\s*)?(\d+)\s*m(?:in(?:ute)?s?)?\s*(\d+)\s*s(?:ec(?:ond)?s?)?\b\s*(.*)$", s)
        if m:          # '1m30s' / '2 min 30 sec' / '1h 2m 30s' (used to fail, or start 1 min labelled '30 s')
            secs = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            return (secs, m.group(4).strip()) if secs > 0 else (None, s)
        m = re.match(r"^(\d+):(\d{1,2})\b\s*(.*)$", s)
        if m:
            return int(m.group(1)) * 60 + int(m.group(2)), m.group(3)
        m = re.match(r"^(\d*\.\d+)\s*(h|hours?|hrs?|m|min|mins|minutes?|s|sec|secs|seconds?)?\b\s*(.*)$", s)
        if m:          # '1.5h' / '2.5 min' (used to start a 1- / 2-minute timer labelled '.5h')
            u = m.group(2) or "m"
            secs = round(float(m.group(1)) * (3600 if u.startswith("h") else 1 if u.startswith("s") else 60))
            return (secs, m.group(3).strip()) if secs > 0 else (None, s)
        m = re.match(r"^(?:(\d+)\s*h(?:ours?|rs?)?)?\s*(?:(\d+)\s*(m|min|mins|minutes?|s|sec|secs|seconds?)?)?\b\s*(.*)$", s)
        if not m or not (m.group(1) or m.group(2)):
            return None, s
        h, n, unit = int(m.group(1) or 0), int(m.group(2) or 0), (m.group(3) or "m")
        secs = h * 3600 + (n if unit.startswith("s") else n * 60)
        return (secs, m.group(4).strip()) if secs > 0 else (None, s)

    CANON_FIRST = {"w", "water", "+", "t", "timer", "sw", "stopwatch", "stop", "cancel", "n", "note", "idea", "clip", "clips",
                   "clipboard", "o", "open", "v", "vol", "b", "bright", "brightness", "mic", "lock", "screens", "screen",
                   "monitors", "focus", "mock", "exam", "score", "chat", "wrong", "swap", "switch", "move", "unswap", "unmove",
                   "todo", "deadline", "habit", "countdown", "big", "dash", "dashboard", "player", "=", "snip", "screenshot", "widgets"}

    def natural(self, t):
        """A sentence (voice, or typed like you'd say it) -> the island's own command. Short commands pass straight through."""
        w = t.split()
        if w and w[0].lower() in self.CANON_FIRST and not re.search(
                r"\b(for|please|a|an|the|of|to|and|with|minutes?|seconds?|hours?|liters?|litres?|glass|cup|bottle|song|music)\b",
                " ".join(w[1:]).lower()):
            return t
        return island_voice.natural(t)

    def handle_cmd(self, text, ai=True):
        t = self.natural(" ".join(str(text).split()))
        if t != " ".join(str(text).split()):
            print(f"command: {text!r} -> {t!r}")
        low = t.lower()
        try:
            if low.startswith("@"):
                return self.quick_action(low[1:])
            m = re.match(r"^(?:w|water|\+)\s*(-?\d{2,4})\s*(?:ml)?$", low)
            if m:
                ml = int(m.group(1))
                self.add_water(self.model.today(), ml)
                return self.hud("water", "Water", r=f"{'+' if ml > 0 else ''}{ml} ml", ms=1400)
            if low in ("widgets", "show widgets", "desktop widgets", "widget"):
                return self.desk_peek()
            if low in ("snip", "screenshot", "screen snip", "snip screen"):
                if WIN:                         # Windows' own snipping overlay; the shot lands on the clipboard -> the island's Save / Open
                    QTimer.singleShot(250, lambda: os.startfile("ms-screenclip:"))
                return
            if low in ("lock", "lock pc", "lock the pc"):
                if WIN:
                    user32.LockWorkStation()
                return self.hud("ok", "PC locked", ms=1000)
            if re.fullmatch(r"screen ?time(?: today)?", low):
                return self.quick_action("screen")
            if re.match(r"^(?:screens?|monitors?|displays?)\s+off$", low):
                if WIN:     # posted (not sent): a busy window can't hang the island; any mouse move or key wakes them
                    QTimer.singleShot(400, lambda: user32.PostMessageW(0xFFFF, 0x0112, 0xF170, 2))
                return self.hud("ok", "Screens off", r="move the mouse to wake them", ms=900)
            m = re.match(r"^(?:t|timer)\s+(.+)$", low)
            if m:
                secs, label = self.parse_dur(m.group(1))
                mu = re.match(r"^(?:until|till|til|to|at)\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b\s*(.*)$", m.group(1))
                if mu and int(mu.group(1)) <= 23 and int(mu.group(2) or 0) <= 59 and (not mu.group(3) or 1 <= int(mu.group(1)) <= 12):
                    # 'timer until 4:30 pm' / 'timer to 16:30' / 'timer at 5 tea': runs out at that clock time
                    hh, mi, ap, now = int(mu.group(1)), int(mu.group(2) or 0), mu.group(3), dt.datetime.now()
                    if ap:
                        hh = hh % 12 + (12 if ap == "pm" else 0)
                    tgt = now.replace(hour=hh % 24, minute=mi, second=0, microsecond=0)
                    step = dt.timedelta(hours=12 if (not ap and 1 <= hh <= 12) else 24)   # '4:30' alone = the next 4:30
                    while tgt <= now:
                        tgt += step
                    secs, label = int((tgt - now).total_seconds()), mu.group(4)
                if not secs:
                    return self.hud("info", "Timer: try 25, 90s, 1h30", ms=1800)
                self.add_timer(secs, t[len(t) - len(label):] if label else "")
                ends = "ends " + self.t12(dt.datetime.now() + dt.timedelta(seconds=secs)) if secs >= 300 else ""
                return self.hud("timer", f"Timer {f'{secs // 60}m {secs % 60:02d}s' if 60 < secs < 3600 and secs % 60 else fmt_min(secs / 60) if secs >= 60 else str(secs) + 's'}",
                                r=" · ".join(x for x in (label[:14] if ends else label[:24], ends) if x), ms=2000 if ends else 1300)
            if low in ("sw", "stopwatch"):
                sw = [x for x in self.timers if x.get("sw")]
                if sw:
                    self.timers = [x for x in self.timers if not x.get("sw")]
                    self.push_timers()
                    s = int(time.time() - sw[0]["start"])
                    took = fmt_min(s / 60) if s >= 3600 else f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"
                    return self.hud("timer", "Stopwatch", r=took, ms=2200)
                self.timers.append(dict(id=f"s{int(time.time() * 1000)}", sw=True, start=time.time()))
                self.push_timers()
                return self.hud("timer", "Stopwatch started", ms=1200)
            if low in ("stop", "cancel"):
                self.timers = []
                self.push_timers()
                return self.hud("timer", "Timers cleared", ms=1200)
            m = re.match(r"^(?:n|note|idea)\s+(.+)$", t, re.I)
            if m:
                with open(os.path.join(HERE, "ideas.md"), "a", encoding="utf-8") as f:
                    now = dt.datetime.now()
                    f.write(f"- {now:%Y-%m-%d} {self.t12(now)}  {m.group(1)}\n")
                return self.hud("ok", "Saved to ideas.md", ms=1200)
            if low in ("clip", "clips", "clipboard"):
                return self.js("clips", self.clips)
            if re.match(r"^(swap|switch|move|unswap|unmove|reset moves)\b", low) and self.move_cmd(low) != "nomatch":
                return
            if self.study_cmd(low, t) != "nomatch":
                return
            if self.life_cmd(low, t) != "nomatch":
                return
            if low in ("dash", "dashboard"):
                return self.toggle_dash()
            if low in ("player", "lyrics", "music", "p"):
                return QTimer.singleShot(60, lambda: self.js("player", "pill"))
            if low == "mic":
                return self.watch.cmd("mic")
            if low == "mute":
                return self.audio.mute("master", None)
            m = re.match(r"^(?:v|vol|volume)\s+(\d{1,3})$", low)
            if m:
                v = min(100, int(m.group(1)))
                self.audio.set("master", v / 100)
                return self.hud("vol", "Volume", v=v / 100, r=str(v), ms=1200)
            m = re.match(r"^(?:b|bright|brightness)\s+(\d{1,3})$", low)
            if m:
                v = min(100, int(m.group(1)))
                for i in range(len(self._bri or [])):
                    self.bright.set(i, v / 100)
                return self.hud("bright", "Brightness", v=v / 100, r=str(v), ms=1200)
            m = re.match(r"^(?:o|open)\s+(.+)$", low)
            if m:
                return self.open_any(m.group(1).strip())
            if re.fullmatch(r"=?\s*[\d\s.+\-*/()%^]+", low) and re.search(r"\d\s*[-+*/%^]", low):
                val = self.calc(low.lstrip("= "))
                QApplication.clipboard().setText(str(val))
                return self.hud("ok", f"= {val}", r="copied", ms=2600)
            if ai and self.ai_cmd(t):
                return None
            return self.hud("info", "Didn't get that", r=t[:28], ms=2200)
        except Exception as e:
            print("command:", e)
            return self.hud("info", "That didn't work", ms=1400)

    def quick_action(self, what):
        """@media next|prev|pause|play|like · @vol up|down · @mute · @whatsnext · @water (from natural speech)"""
        playing = bool((getattr(self, "_win_media", None) or {}).get("playing")) or bool((self.sp_remote_media() or {}).get("playing"))
        if what.startswith("media "):
            act = what.split()[1]
            if act == "pause" and not playing or act == "play" and playing:
                return self.hud("music", "Already " + ("paused" if act == "pause" else "playing"), ms=1000)
            name = {"next": "media_next", "prev": "media_prev", "pause": "media_toggle", "play": "media_toggle", "like": "media_like"}[act]
            self.on_action(name, None)
            return self.hud("music", {"next": "Next song", "prev": "Previous song", "pause": "Paused", "play": "Playing",
                                      "like": "Saved to Liked Songs"}[act], ms=1000)
        if what in ("vol up", "vol down"):
            try:
                cur = float(self.audio.last["master"]["v"]) if getattr(self.audio, "last", None) else 0.5
            except Exception:
                cur = 0.5
            v = max(0.0, min(1.0, cur + (0.1 if what.endswith("up") else -0.1)))
            self.audio.set("master", v)
            return self.hud("vol", "Volume", v=v, r=str(round(v * 100)), ms=1200)
        if what == "mute":
            self.audio.mute("master", True)
            return self.hud("mute", "Muted", ms=1000)
        if what == "sayword":
            return self.say_word()
        if what.startswith("say "):
            p = what.split(None, 2)
            if len(p) == 3 and p[1] in island_say.LCIDS:
                return self.say_text(p[1], p[2])
        if what == "whatsnext":
            cur, nxt = self.model.focus()
            if cur:
                s, e = self.model.span(cur, self.model.today())
                left = int((e - dt.datetime.now()).total_seconds() // 60) if e else 0
                return self.hud("timer", f"Now: {cur['title']}", r=f"{fmt_min(left)} left" + (f" · then {nxt['title']}" if nxt else ""), ms=3200)
            if nxt:
                return self.hud("timer", f"Next: {nxt['title']}", r=h12(nxt["time"]) if nxt.get("time") else "", ms=3000)
            late = self.model.late_items(self.model.today())
            if late:                        # same wording as the hover card: don't say "nothing left" while things are open
                return self.hud("timer", "Nothing else planned", ms=3000,
                                r=f"{len(late)} still open · " + ", ".join(t["title"] for t in late[:2]))
            return self.hud("ok", "Nothing left today", ms=2000)
        if what.startswith("prayer"):       # "next prayer" / "when is maghrib" / "متى الأذان"
            now, want = dt.datetime.now(), what[6:].strip().capitalize()
            hit = None
            if want in island_day.AR:
                hit = next(((n, at) for d in (now.date(), now.date() + dt.timedelta(days=1))
                            for n, at in self.prayer.times(d) if n == want and at > now), None)
            hit = hit or self.prayer.next(now)
            if not hit:
                return self.hud("moon", "Prayer times not loaded yet", ms=1800)
            left = int((hit[1] - now).total_seconds() // 60)
            return self.hud("moon", f"{hit[0]} {self.t12(hit[1])}", r=f"in {fmt_min(left)}", ms=3200)
        if what == "exam":                  # "how many days left" / "كم يوم ضايل" (read only)
            m, today = self.model, self.model.today()
            m.reload()                                        # cheap: only re-reads a plan file that changed
            big = big_countdown(m, today)
            if not big:
                return self.hud("cal", "No countdown yet", r="big <what> <date>", ms=2600)
            n = big["days"]
            mk = m.study.mock_start if (big["src"] == "plan" and m.study.data.get("mock_start")) else None
            left = (mk - today).days if mk else -1
            r = ("mocks start today" if left == 0 else "mocks start tomorrow" if left == 1 else f"mocks in {left} days" if left > 1 else "")
            return self.hud("cal", f"{n} day{'' if n == 1 else 's'} to {big['title']}", r=r + (f" · {mk.day} {mk.strftime('%b')}" if r else ""), ms=3000)
        if what == "water":                 # "how much water" / "كم شربت"
            ml, goal = self.model.water(self.model.today())
            if not goal:
                return self.hud("water", f"Water {ml / 1000:.1f} L", ms=2200)
            left = max(0, goal - ml)
            return self.hud("water", "Water", v=min(1.0, ml / goal), ms=2600,
                            r=f"{ml / 1000:.1f} / {goal / 1000:.1f} L" + (f" · {left / 1000:.1f} to go" if left else " · goal hit"))
        if what == "macros":                # "how much protein left" / "calories left" / "كم بروتين" (live QAWIFit numbers, read only)
            f = self._fit or {}
            if not f.get("ok"):
                return self.hud("info", "QAWIFit isn't connected", ms=1800)
            t = f.get("t") or {}
            p, tp, k, tk = (float(f.get("p") or 0), float(t.get("p") or 0), float(f.get("kcal") or 0), float(t.get("kcal") or 0))
            pl, kl = round(tp - p), round(tk - k)
            return self.hud("bolt", f"Protein {p:.0f} / {tp:.0f} g", v=min(1.0, p / tp) if tp else None, ms=3000,
                            r=(f"{pl} g to go" if pl > 0 else "protein done") + " · " + (f"{kl:,} kcal left" if kl >= 0 else f"{-kl:,} kcal over"))
        if what == "screen":                # "screen time" / "how long have I been on the PC today" (read only, never leaves the PC)
            if not self.settings.get("screen"):
                return self.hud("info", "Screen time is off", r="Settings > Notifications", ms=2200)
            d = self.screen_log.get(self.model.today().isoformat(), {})
            top = [f"{n} {fmt_min(round(v / 60))}" for n, v in sorted(d.items(), key=lambda x: -x[1])[:2] if v >= 60]
            return self.hud("screen", f"Screen time {fmt_min(round(sum(d.values()) / 60))}", r=" · ".join(top), ms=3200)
        return self.hud("info", "Didn't get that", ms=1500)

    def open_any(self, q):
        """open <app> · open <site> · open supernatural next ep (your browser history) · open <anything> (a web search)."""
        ql = q.lower().strip()
        if re.search(r"\b(next|following)\s*(ep|episode|eps|one|part)?\b|\b(?:episode|ep)\s*\d|where i (left|stopped)", ql):
            return self.open_history(q)
        a = self.app_match(ql)
        if a and (a["name"].lower() == ql or a["name"].lower().startswith(ql) or len(ql) >= 4):
            return self.open_app(a["appid"])
        hit = island_win.find_app(ql)
        if hit:
            self.run_app(hit[1], path=hit[0])
            return self.hud("ok", f"Opening {hit[1][:24]}", ms=1100)
        site = {"youtube": "https://www.youtube.com", "yt": "https://www.youtube.com", "netflix": "https://www.netflix.com",
                "gmail": "https://mail.google.com", "mail": "https://mail.google.com", "github": "https://github.com",
                "qawi": "https://qawifit.com", "qawifit": "https://qawifit.com", "claude": "https://claude.ai/new",
                "instagram": "https://www.instagram.com", "maps": "https://maps.google.com", "translate": "https://translate.google.com",
                "drive": "https://drive.google.com", "calendar": "https://calendar.google.com", "chatgpt": "https://chatgpt.com"}.get(ql)
        if site:
            self.open_url(site)
            return self.hud("ok", f"Opening {ql}", ms=1100)
        if re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", ql):
            self.open_url("https://" + ql)
            return self.hud("ok", f"Opening {ql[:28]}", ms=1100)
        return self.open_history(q, search=True)

    def open_history(self, q, search=False):
        """Your browser history decides: the last page of that show / site, or its next episode. Runs in the background."""
        self.hud("info", "Looking in your history…", r=q[:24], ms=1200)

        def run():
            try:
                plan = island_voice.plan_open(q)
            except Exception as e:
                print("open history:", e)
                plan = None
            self.win_sig.emit(("openplan", (q, plan, search)))
        threading.Thread(target=run, daemon=True).start()

    def open_plan_done(self, q, plan, search):
        if plan and plan[0] == "url":
            print(f"open: {q!r} -> {plan[1]} ({plan[2]})")
            self.open_url(plan[1])
            return self.hud("ok", "Opening " + plan[2], r=re.sub(r"^https?://(www\.)?", "", plan[1])[:34], ms=2600)
        if search:
            import urllib.parse
            self.open_url("https://www.google.com/search?q=" + urllib.parse.quote(q))
            return self.hud("ok", "Searching the web", r=q[:28], ms=1600)
        return self.hud("info", "Not in your history", r=q[:28], ms=2200)

    # ---------------------------------------------------------- pronunciation (Windows' own voices, offline)
    def sayv_check(self, delay_ms=0):
        """Which Mandarin / German voices Windows has -> Settings row + log."""
        def run():
            v = island_say.voices()
            self.win_sig.emit(("sayv", v))
        QTimer.singleShot(delay_ms, lambda: threading.Thread(target=run, daemon=True).start())

    def sayv_push(self):
        v = dict(getattr(self, "_sayv", None) or {}, busy=bool(getattr(self, "_sayv_busy", False)), known=hasattr(self, "_sayv"),
                 tried=bool(getattr(self, "_sayv_tried", False)))
        self.js("sayv", v)

    def say_word(self, which="word"):
        """The 🔊 on the word of the day / "pronounce it": Mandarin = the characters, German = the word (or its sentence)."""
        w = island_life.word_of_day(self.model.today())
        if w["lang"] == "Mandarin":
            return self.say_text("zh", w["word"], w["word"], w["mean"])
        text = w.get("ex") if which == "ex" and w.get("ex") else w["word"]
        return self.say_text("de", text, text, "" if which == "ex" else w["mean"])

    def say_text(self, lang, text, show=None, sub="", quiet=False):
        if lang not in island_say.LCIDS or not str(text).strip():
            return self.hud("info", "Nothing to say", ms=1200)
        if not quiet:
            self.hud("spk", str(show or text)[:40], r=str(sub)[:30], ms=2600)

        def run():
            res = island_say.speak(lang, text)
            self.win_sig.emit(("said", (lang, res)))
        threading.Thread(target=run, daemon=True).start()

    def said(self, lang, res):
        if res == "ok":
            return
        print(f"say: {lang} -> {res}")
        if res == "missing":
            self._sayv = dict(getattr(self, "_sayv", None) or {}, **{lang: None})
            if getattr(self, "_sayv_busy", False):
                self.hud("dl", f"{island_say.NAMES[lang]} voice is downloading…", r="give it a minute", ms=2600)
            elif time.time() - getattr(self, "_sayv_asked", 0) > 120 and not getattr(self, "_sayv_tried", False):
                self._sayv_asked = time.time()           # tapping 🔊 without the voice starts the install (one UAC prompt)
                self.say_voices()
            else:
                self.hud("guard", f"No {island_say.NAMES[lang]} voice yet", r="Settings > Voice > Add voices", ms=3200)
            self.sayv_push()
        else:
            self.hud("info", "Couldn't say it", r=res[7:40], ms=2200)

    def say_voices(self):
        """Settings > Voice > Add voices: Windows' Mandarin + German speech packs, one UAC prompt, then watch for them."""
        have = getattr(self, "_sayv", None) or {}
        want = [k for k in ("zh", "de") if not have.get(k)]
        if not want:
            return self.sayv_check()
        if getattr(self, "_sayv_busy", False):
            return self.sayv_push()
        if getattr(self, "_sayv_tried", False):        # the packs installed but no voice appeared: a 2nd UAC prompt won't help
            island_say.open_settings()
            return self.hud("info", "Add them here", r="Speech > Add voices: Chinese, German", ms=3500)
        if not island_say.install_voices(want, HERE):
            island_say.open_settings()
            return self.hud("info", "Add them here", r="Speech > Add voices: Chinese, German", ms=3500)
        print("say: installing voices", want)
        self._sayv_busy, self._sayv_tries = True, 0
        self.hud("dl", "Adding voices…", r="Windows downloads them, ~1-3 min", ms=3500)
        self.sayv_push()
        self.sayv_check(15_000)

    # ---------------------------------------------------------- brain (a small local model in Ollama)
    BRAIN_FIRST = {"mute", "music", "lyrics", "copy", "p"}

    def brain_model(self):
        """The Ollama model picked in Settings > Voice > AI brain (None = off)."""
        return island_brain.MODELS.get(str(self.settings.get("brain", "smart")))

    def brain_pick(self):
        """The model to use right now: the chosen one if it's downloaded, else another chat model you have. None = no brain."""
        want = self.brain_model()
        if not want:
            return None
        now = time.time()
        if now - getattr(self, "_brain_seen", 0) > 30:
            self._brain_seen = now
            self._brain_tags = island_brain.models(0.6)
        tags = getattr(self, "_brain_tags", None) or []
        return want if want in tags else next((t for t in tags if t.startswith("qwen")), tags[0] if tags else None)

    def brain_context(self):
        now = dt.datetime.now()
        bits = [f"now {now:%a %d %b} {self.t12(now)}"]
        try:
            cur, nxt = self.model.focus()
            if cur:
                bits.append(f"doing: {cur['title']}")
            if nxt:
                bits.append(f"next: {nxt['title']}" + (f" at {h12(nxt['time'])}" if nxt.get("time") else ""))
            ml, goal = self.model.water(self.model.today())
            if goal:
                bits.append(f"water {ml / 1000:.1f} of {goal / 1000:.1f} L")
        except Exception:
            pass
        md = getattr(self.media, "last", None) or {}
        if md.get("title"):
            bits.append(f"music: {md['title']} by {md.get('artist') or '?'}" + ("" if md.get("playing") else " (paused)"))
        if self.timers:
            bits.append(f"{len(self.timers)} timer(s) running")
        return " · ".join(bits)

    def brain_game(self):
        return bool(self.hidden_fs or self.fg_game())

    def brain_warm(self):
        """You started talking: load the model in the background so an answer doesn't wait for it."""
        if not self.brain_model() or time.time() - getattr(self, "_brain_used", 0) < 240:
            return
        self._brain_used = time.time()
        game = self.brain_game()

        def run():
            m = self.brain_pick()
            if m and island_brain.warm(m, game):
                self._brain_last, self._brain_gpu = m, not game
        threading.Thread(target=run, daemon=True).start()

    def ai_cmd(self, text):
        """Not a known command: the local brain turns it into one or answers it. False = no brain available."""
        m = self.brain_pick()
        if not m:
            return False
        game = self.brain_game()
        ctx = self.brain_context()
        self._brain_used, self._brain_last = time.time(), m
        if not game:
            self._brain_gpu = True
        self.js("voice", dict(state="thinking", text=text))

        def run():
            cmd = say = err = ""
            t0 = time.time()
            try:
                cmd, say = island_brain.ask(m, text, ctx, game)
            except Exception as e:
                err = str(e)[:120]
            self.win_sig.emit(("aicmd", (text, cmd, say, err, round(time.time() - t0, 1), m)))
        threading.Thread(target=run, daemon=True).start()
        return True

    def brain_done(self, text, cmd, say, err, secs, m):
        print(f"brain ({m}, {secs}s): {text!r} -> cmd={cmd!r} say={say[:90]!r}" + (f"  error: {err}" if err else ""))
        first = cmd.split()[0].lower() if cmd else ""
        if cmd.lower().startswith("@say ") and len(cmd.split(None, 2)) == 3 and cmd.split()[1].lower() in island_say.LCIDS:
            _, lang, said = cmd.split(None, 2)                          # "how do you say good morning in Chinese"
            self.say_text(lang.lower(), said, quiet=True)
            return self.js("voice", dict(state="answer", text=say or said, q=text))
        if cmd and (first.startswith("@") or first in self.CANON_FIRST or first in self.BRAIN_FIRST):
            self.js("voice", dict(state="done", text=text, cmd=cmd))
            return self.handle_cmd(cmd, ai=False)
        if say:
            return self.js("voice", dict(state="answer", text=say, q=text))
        self.js("voice", dict(state="error", text="Brain didn't answer" if err else "Didn't get that"))

    def binst_push(self, state=None, msg=None):
        """Settings > Voice > AI brain row: missing / stopped / nomodel / running / ready / failed / checking."""
        b = str(self.settings.get("brain", "smart"))
        model = island_brain.MODELS.get(b) or island_brain.MODELS["smart"]
        if state is None:
            if getattr(self, "_b_installing", False):
                state, msg = "running", getattr(self, "_binst_msg", "")
            else:
                c = getattr(self, "_bstate", None)
                if c is None or time.time() - c[0] > 20:
                    def chk():
                        tags = island_brain.models(1.0)
                        st = ("ready" if model in tags else "nomodel") if tags is not None else \
                            ("stopped" if island_brain.exe() else "missing")
                        self.win_sig.emit(("bstate", st))
                    threading.Thread(target=chk, daemon=True).start()
                state = c[1] if c else "checking"
        self._binst = dict(state=state, msg=msg or "", b=b, model=model, size=island_brain.SIZES.get(b, ""))
        self.js("binst", self._binst)

    def brain_install(self):
        """One button: Ollama (if missing) + the chosen model, with progress in Settings. Log: brain_install.log."""
        if getattr(self, "_b_installing", False):
            return self.binst_push()
        if str(self.settings.get("brain", "smart")) == "off":
            self.settings.set("brain", "smart")
        model = self.brain_model()
        self._b_installing = True
        self._binst_msg = "Starting…"
        print("brain install:", model)
        self.binst_push()
        self.hud("dl", "Setting up the AI brain…", r="a few minutes, keep the island open", ms=4000)

        def run():
            ok, msg = False, ""
            try:
                with open(os.path.join(HERE, "brain_install.log"), "a", encoding="utf-8") as log:
                    log.write(f"\n--- {dt.datetime.now():%Y-%m-%d %I:%M %p}  {model}\n")
                    ok, msg = island_brain.install(model, lambda x: self.win_sig.emit(("bprog", x)), log)
            except Exception as ex:
                msg = str(ex)[:120]
            self.win_sig.emit(("binstalled", (ok, msg)))
        threading.Thread(target=run, daemon=True).start()

    # ---------------------------------------------------------- voice ("Hey Island" / hold a key)
    VOICE_KEYS = {"rctrl": 0xA3, "ralt": 0xA5, "x1": 0x05, "x2": 0x06}

    def voice_sync(self):
        st = self.settings.get
        on = bool(st("voice", True))
        if not on:
            if getattr(self, "vkey", None):
                self.vkey.stop()
                self.vkey = None
            if getattr(self, "voice", None):
                self.voice.wake_stop()
            return
        if getattr(self, "voice", None) is None:
            self.voice = island_voice.Voice(lambda e: self.win_sig.emit(("voice", e)), str(st("voice_model", "base")))
        self.voice.model_size = str(st("voice_model", "base"))
        vk = self.VOICE_KEYS.get(str(st("voice_key", "rctrl")), 0xA3)
        if WIN and (getattr(self, "vkey", None) is None or self.vkey.vk != vk):
            if getattr(self, "vkey", None):
                self.vkey.stop()
            self.vkey = island_voice.HoldKey(vk, lambda e: self.win_sig.emit(("voice", e)))
            self.vkey.start()
        ok, _ = island_voice.voice_ready()
        if ok and not getattr(self, "_voice_warm", False):
            self._voice_warm = True
            QTimer.singleShot(30_000, lambda: self.voice and self.settings.get("voice", True) and self.voice.warm())   # ready before the first key press
        if ok and st("wake", True):
            self.voice.wake_start()
        else:
            self.voice.wake_stop()

    def voice_event(self, e):
        kind = e[0]
        v = getattr(self, "voice", None)
        if kind == "key":
            if not v or not self.settings.get("voice", True):
                return
            if e[1] == "down":
                v.listen_start()
                self.brain_warm()
            else:
                v.listen_stop(cancel=e[1] == "cancel")
        elif kind == "wake":
            if v:
                v.listen_start(auto=True)
                self.brain_warm()
        elif kind == "level":
            now = time.time()
            if now - getattr(self, "_vlvl_t", 0) > 0.05:
                self._vlvl_t = now
                self.js("vlevel", round(e[1], 3))
        elif kind == "ready":
            self.vinst_push()
        elif kind == "state":
            self.js("voice", dict(state=e[1], text=e[2] if len(e) > 2 else ""))
            if e[1] == "listening" and self.hidden_fs:
                pass                          # over a fullscreen game: it still works, the island just stays out of the way
        elif kind == "heard":
            text = e[1]
            cmd = self.natural(text)
            self.js("voice", dict(state="done", text=text, cmd=cmd))
            self.handle_cmd(text)

    def vinst_push(self, state=None, msg=None):
        """Settings > Voice shows where the install / model download is (the HUD can't be seen over the open panel)."""
        if state is None:
            ok = island_voice.voice_ready()[0]
            v = getattr(self, "voice", None)
            state = "running" if getattr(self, "_v_installing", False) else ("ready" if v and v.model else "loading") if ok else "missing"
            msg = getattr(self, "_vinst_msg", "") if state in ("running", "loading") else ""
        self._vinst = dict(state=state, msg=msg or "", key=str(self.settings.get("voice_key", "rctrl")))
        self.js("vinst", self._vinst)

    def voice_install(self):
        if getattr(self, "_v_installing", False):
            return self.vinst_push()
        ok, miss = island_voice.voice_ready()
        if ok:
            self.voice_sync()
            if self.voice:
                self.voice.warm()
            return self.vinst_push()
        self._v_installing = True
        self._vinst_msg = "Starting…"
        print("voice install: pip install", " ".join(island_voice.PIP_PKGS))
        self.vinst_push()
        self.hud("dl", "Installing voice…", r="a few minutes, keep the island open", ms=4000)
        import subprocess

        def run():
            py = sys.executable.replace("pythonw.exe", "python.exe")
            ok, tail = False, ""
            try:
                with open(os.path.join(HERE, "voice_install.log"), "w", encoding="utf-8") as log:
                    pr = subprocess.Popen([py, "-m", "pip", "install", "--upgrade", "--progress-bar", "off", *island_voice.PIP_PKGS],
                                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    for line in pr.stdout:
                        log.write(line)
                        log.flush()
                        tail = (tail + line)[-600:]
                        m = re.match(r"^\s*(Collecting|Downloading|Installing collected packages|Successfully installed)\s*(\S*)", line)
                        if m:
                            word = {"Collecting": "Getting", "Downloading": "Downloading", "Installing collected packages": "Installing",
                                    "Successfully installed": "Installed"}[m.group(1)]
                            name = re.sub(r"[-_][\d.]+.*$|\.whl$", "", m.group(2).split("/")[-1])[:28]
                            self.win_sig.emit(("vprog", f"{word} {name}".strip()))
                    ok = pr.wait(timeout=1800) == 0
            except Exception as ex:
                tail = str(ex)
            self.win_sig.emit(("vinstalled", (ok, " ".join(tail.split())[-140:])))
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def calc(expr):
        import ast
        import operator as op
        ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv, ast.Mod: op.mod,
               ast.Pow: op.pow, ast.USub: op.neg, ast.UAdd: op.pos, ast.BitXor: op.pow}

        def ev(n):
            if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
                return n.value
            if isinstance(n, ast.BinOp) and type(n.op) in ops:
                a, b = ev(n.left), ev(n.right)
                if type(n.op) in (ast.Pow, ast.BitXor) and abs(b) > 100:
                    raise ValueError("too big")
                return ops[type(n.op)](a, b)
            if isinstance(n, ast.UnaryOp) and type(n.op) in ops:
                return ops[type(n.op)](ev(n.operand))
            raise ValueError("not math")
        v = ev(ast.parse(expr, mode="eval").body)
        return int(v) if float(v).is_integer() else round(v, 6)

    def on_clip(self):
        try:
            cb = QApplication.clipboard()
            md = cb.mimeData()
            if md is None or any("ExcludeClipboardContent" in f or "Clipboard Viewer Ignore" in f for f in md.formats()):
                return                                   # password managers mark their copies: never kept
            if md.hasImage():
                if not self.settings.get("shots") or self.hidden_fs:
                    return
                img = cb.image()
                if img.isNull() or img.width() < 40:
                    return
                from PySide6.QtCore import QBuffer, QByteArray
                th = img.scaled(220, 124, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                ba = QByteArray()
                buf = QBuffer(ba)
                buf.open(QIODevice.WriteOnly)
                th.save(buf, "PNG")
                buf.close()
                self._shot = img
                self.hud("shot", "Screenshot", r=f"{img.width()}×{img.height()}",
                         img="data:image/png;base64," + bytes(ba.toBase64()).decode(), ms=4500,
                         act=["Save", "Open"], cb=[lambda: self.save_shot(False), lambda: self.save_shot(True)])
            elif md.hasText() and self.settings.get("clips"):
                t = md.text()
                if t and t.strip() and len(t) < 20000:
                    t = t[:4000]
                    self.clips = [t] + [c for c in self.clips if c != t][:19]
                    if self.mode == "peek" and getattr(self, "_dtab", "") == "clip":
                        self.js("clipList", self.clips[:12])
        except Exception as e:
            print("clipboard:", e)

    def save_shot(self, open_it):
        img = getattr(self, "_shot", None)
        if img is None:
            return
        d = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
        os.makedirs(d, exist_ok=True)
        pth = os.path.join(d, f"Island {dt.datetime.now():%Y-%m-%d %H%M%S}.png")
        img.save(pth, "PNG")
        if open_it:
            os.startfile(pth)
        else:
            self.hud("ok", "Saved to Screenshots", act=["Show"], cb=[lambda: os.startfile(d)], ms=2500)

    def second_r3(self, s):
        now = time.time()
        # battery: plugged in / low
        if s and s.get("bat") is not None and self.settings.get("battery"):
            plugged, lvl = bool(s.get("plugged")), float(s["bat"])
            if self._bat is not None:
                if plugged and not self._bat[0]:
                    self.hud("bat", "Charging", v=lvl / 100, r=f"{round(lvl)}%", ms=2200)
                elif not plugged and lvl <= 15 and not self._low_warned:
                    self._low_warned = True
                    self.hud("batlow", "Battery low", v=lvl / 100, r=f"{round(lvl)}%", ms=3500)
                    self.sound("nag")
            if plugged or lvl > 20:
                self._low_warned = False
            self._bat = (plugged, lvl)
        # timers that just finished
        done = [t for t in self.timers if not t.get("sw") and t["end"] <= now]
        if done:
            self.timers = [t for t in self.timers if t not in done]
            self.push_timers()
            for t in done:
                if t.get("item"):                    # r47: a started plan item: ask if it's done
                    it = next((x for x in self.model.items(self.model.today()) if x["key"] == t["item"]), None)
                    if it and not it["done"]:
                        self.done_toast(f"idone-{t['id']}", it, f"{CAT_NAME.get(it['cat'], '')} · time's up")
                    continue
                if t.get("mock"):
                    cat = t["mock"]
                    self.notify(f"mock-{t['id']}", cat, "mock · time's up", t["label"],
                                "Put the pen down. Mark it, then save the score.",
                                [("Save score", lambda cat=cat: self.open_cmd("score " + next(
                                    (p["id"] for p in self.study.papers() if p["cat"] == cat and p["mock"] and p["score"] is None
                                     and not p["missing"]), cat) + " ")), ("Later", None)], 300, kind="timer")
                    continue
                lbl, total = t.get("label") or "", t.get("total", 0)
                self.notify(f"timer-{t['id']}", "E", "timer · done",
                            lbl or (fmt_min(total / 60) if total >= 60 else f"{int(total)}s") + " timer",
                            "", [("+5 min", lambda lbl=lbl: self.add_timer(300, lbl)), ("OK", None)], 60, kind="timer")
        # private mode while OBS is open
        if self.settings.get("share") and now - self._obs_at > 10:
            self._obs_at = now
            try:
                import psutil
                obs = any((p.info["name"] or "").lower() in ("obs64.exe", "obs32.exe", "obs.exe")
                          for p in psutil.process_iter(["name"]))
            except Exception:
                obs = False
            if obs and not self.privacy:
                self.privacy_auto = True
                self.set_privacy(True)
            elif not obs and self.privacy and self.privacy_auto:
                self.privacy_auto = False
                self.set_privacy(False)
        # GPU only while the System tab (or the dashboard) is on screen
        look = (self.mode == "panel" and self.state.get("tab") == "System") or bool(getattr(self, "dash", None)) \
            or getattr(self, "deck_n", 0) > 0
        if look != self.gpu.want:
            self.gpu.watch(look)
            if look:
                self.bt.now()
        sx = dict(ping=self._ping, wifi=self._wifi, gpu=self._gpu if look else None, bt=self._bt,
                  up=island_dev.uptime() if look else None, gh=(self._gh or {}).get("unread") if self._gh else None,
                  lan=self.lan_url() if (look and self.lan) else None)
        if sx != self._sysx and self.ready:
            self._sysx = sx
            self.js("sysx", sx)

    SP_VER = 8          # island-bridge.js VER this island expects (older = run "spicetify apply")

    def sp_live(self):
        """Spotify is open with the bridge extension (it posts its position every second)."""
        return time.time() - getattr(self, "sp_at", 0) < 8

    def sp_now_push(self, force=False):
        """What the deck's library needs: is Spotify there, which song / playlist plays, is the extension current."""
        now = dict(on=self.sp_live(), old=self.sp_live() and getattr(self, "sp_ver", 0) < self.SP_VER,
                   uri=getattr(self, "sp_uri", ""), ctx=getattr(self, "sp_ctx", ""), updating=bool(getattr(self, "sp_updating", False)))
        if force or now != getattr(self, "_sp_now", None):
            self._sp_now = now
            self.deck_push("spnow", now)

    def sp_offer_update(self):
        """An older island-bridge.js is running in Spotify: offer the one-tap update (once per island run)."""
        if getattr(self, "_sp_offered", False) or not 0 < getattr(self, "sp_ver", 0) < self.SP_VER \
                or getattr(self, "sp_updating", False) or self.hidden_fs:
            return
        self._sp_offered = True
        QTimer.singleShot(1500, lambda: self.notify(
            f"sp-update-{self.SP_VER}", "MU", "spotify · word-by-word lyrics", "Update the Spotify bridge",
            "Lyrics light up word by word, in time with the singer. Spotify restarts for a few seconds.",
            [("Update now", self.spicetify_apply), ("Later", None)], 60))

    def spicetify_apply(self):
        """Re-applies Spicetify so Spotify loads the newest island-bridge.js (Spotify restarts for a few seconds)."""
        if getattr(self, "sp_updating", False):
            return
        import shutil
        import subprocess
        exe = shutil.which("spicetify") or ""
        for c in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "spicetify", "spicetify.exe"),
                  os.path.join(os.environ.get("USERPROFILE", ""), "spicetify-cli", "spicetify.exe")):
            if not exe and os.path.isfile(c):
                exe = c
        if not exe:
            self.hud("guard", "Couldn't find Spicetify", r="run spicetify apply yourself", ms=3000)
            self.deck_push("spupdate", dict(ok=False, msg="Couldn't find spicetify.exe. Run  spicetify apply  in PowerShell."))
            return
        self.sp_updating = True
        self.sp_now_push()
        self.hud("screen", "Updating Spotify", r="it restarts for a moment", ms=2500)

        def run():
            ok, out = False, ""
            try:
                r = subprocess.run([exe, "apply"], capture_output=True, text=True, timeout=180,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = re.sub(r"\x1b\[[0-9;]*m", "", (r.stdout or "") + (r.stderr or ""))
                ok = r.returncode == 0 and bool(re.search(r"success", out, re.I) or not re.search(r"\berror\b", out, re.I))
            except Exception as ex:
                out = str(ex)
            print("spicetify apply:", "ok" if ok else "FAILED", "|", " ".join(out.split())[-300:])
            self.win_sig.emit(("spapplied", (ok, " ".join(out.split())[-160:])))
        threading.Thread(target=run, daemon=True).start()

    def on_bridge(self, j):
        """From Spotify via Spicetify (island-bridge.js): the song's own synced lyrics + exact position."""
        self.sp_at = time.time()
        if j.get("type") == "hi":
            try:
                self.sp_ver = int(j.get("v") or 0)
            except (TypeError, ValueError):
                self.sp_ver = 0
            self.sp_now_push()
            self.sp_offer_update()
            return
        if j.get("type") in ("pos", "state"):
            try:
                self.sp_ver = int(j.get("v") or 0)
            except (TypeError, ValueError):
                self.sp_ver = 0
            if j.get("uri"):
                self.sp_uri = str(j["uri"])[:80]
            if j.get("type") == "state" and "ctx" in j:
                self.sp_ctx = str(j.get("ctx") or "")[:120]
            self.sp_now_push()
            self.sp_offer_update()
        if "remote" in j and j.get("type") in ("pos", "state"):
            was = self.sp_remote_on()
            rem = self.__dict__.setdefault("sp_rem", {})
            rem.update(remote=bool(j.get("remote")), dev=str(j.get("dev") or "")[:40], at=time.time())
            if j.get("type") == "state":
                try:
                    rem["vol"] = None if j.get("vol") is None else max(0.0, min(1.0, float(j["vol"])))
                except (TypeError, ValueError):
                    rem["vol"] = None
            if was != self.sp_remote_on():
                said = (rem["remote"], rem["dev"])
                if rem.get("said") != said:   # the 8 s window lapses between the phone's once-a-minute updates: log real changes only
                    rem["said"] = said
                    print("spotify:", f"playing on {rem['dev']}" if rem["remote"] else "playing here")
                self._push_media(self.media_merged(getattr(self, "_win_media", None)))
        if j.get("type") == "devices":
            items = []
            for d in (j.get("items") or [])[:20]:
                if isinstance(d, dict) and d.get("id"):
                    items.append(dict(id=str(d["id"])[:80], name=str(d.get("name") or "?")[:40], type=str(d.get("type") or "")[:20].lower(),
                                      active=bool(d.get("active")), local=bool(d.get("local"))))
            self.sp_devs = items
            self.deck_push("spdevices", dict(items=items))
            return
        if j.get("type") == "song":
            self.sp_song = dict(title=str(j.get("title") or "")[:200], artist=str(j.get("artist") or "")[:200],
                                album=str(j.get("album") or "")[:200], dur=j.get("dur") or 0, uri=str(j.get("uri") or ""),
                                art=str(j.get("art") or ""))
            self.sp_art_fetch(self.sp_song["art"])
            if self.sp_remote_on():
                self._push_media(self.media_merged(getattr(self, "_win_media", None)))
            title, lines = str(j.get("title") or ""), j.get("lines")
            if isinstance(lines, list) and lines:
                clean = []
                nsyl = 0
                for l in lines[:3000]:
                    try:
                        row = [round(float(l[0]), 2), str(l[1] or "")[:300]]
                    except (TypeError, ValueError, IndexError):
                        continue
                    if len(l) > 2 and isinstance(l[2], list):          # Spotify's word / syllable timing
                        syl = []
                        for x in l[2][:240]:
                            try:
                                syl.append([round(float(x[0]), 3), int(x[1] or 0),
                                            round(float(x[2]), 3) if len(x) > 2 and x[2] is not None else None])
                            except (TypeError, ValueError, IndexError):
                                continue
                        if syl:
                            row.append(syl)
                            nsyl += len(syl)
                    if len(l) > 3 and l[3] is not None:                     # when the line's singing ends (Spicy Lyrics)
                        try:
                            end = round(float(l[3]), 3)
                            if len(row) == 2:
                                row.append(None)
                            row.append(end)
                        except (TypeError, ValueError):
                            pass
                    clean.append(row)
                key = "spotify:" + str(j.get("uri") or "")
                same = bool(self._lyr and self._lyr.get("key") == key and self._lyr.get("lines") == clean)
                if clean and not same:      # Spotify re-sends the same song (seek, reconnect, device change): the UI already has it
                    print(f"lyrics: Spotify ({j.get('sync') or '?'}" + (f" via {j['via']}" if j.get("via") else "")
                          + (f", {nsyl} syllables" if nsyl else "") + f") for {title}")
                    self.on_lyrics(dict(key=key, title=title, lines=clean, src="spotify"))
        elif j.get("type") == "lib":
            items = []
            for it in (j.get("items") or [])[:300]:
                if isinstance(it, dict) and re.match(r"^spotify:(playlist|album):[A-Za-z0-9]{10,40}$", str(it.get("uri") or "")):
                    img = str(it.get("img") or "")
                    items.append(dict(uri=str(it["uri"]), name=str(it.get("name") or "")[:120], type=str(it.get("type") or "playlist")[:12],
                                      by=str(it.get("by") or "")[:60], pin=bool(it.get("pin")), img=img[:400] if img.startswith("https://") else ""))
            try:
                liked = int(j.get("liked") or 0)
            except (TypeError, ValueError):
                liked = 0
            if j.get("error"):
                print("spotify library:", str(j["error"])[:200])
            self.sp_lib = dict(items=items, liked=liked, at=time.time(), error=str(j.get("error") or "")[:200] if not items else "")
            self.deck_push("splib", {k: v for k, v in self.sp_lib.items() if k != "at"})
        elif j.get("type") == "tracks":
            uri, items = str(j.get("uri") or "")[:80], []
            for it in (j.get("items") or [])[:120]:
                if isinstance(it, dict) and re.match(r"^spotify:(track|episode|local):", str(it.get("uri") or "")):
                    img = str(it.get("img") or "")
                    try:
                        dur = float(it.get("dur") or 0)
                    except (TypeError, ValueError):
                        dur = 0
                    items.append(dict(uri=str(it["uri"])[:200], t=str(it.get("t") or "")[:120], a=str(it.get("a") or "")[:120],
                                      img=img[:400] if img.startswith("https://") else "", dur=dur))
            if j.get("error"):
                print("spotify tracks:", uri, str(j["error"])[:200])
            img = str(j.get("img") or "")
            try:
                off, total = int(j.get("offset") or 0), int(j.get("total") or 0)
            except (TypeError, ValueError):
                off, total = 0, 0
            self.deck_push("sptracks", dict(uri=uri, offset=off, total=total, name=str(j.get("name") or "")[:120],
                                            img=img[:400] if img.startswith("https://") else "", items=items,
                                            error=str(j.get("error") or "")[:200] if not items else ""))
        elif j.get("type") == "spicyinfo":         # v8: Spicy Lyrics had no word timing for this song - why
            st = [str(x)[:60] for x in (j.get("stores") or [])[:6]]
            print(f"lyrics: no Spicy Lyrics timing for {str(j.get('title') or '')[:80]} "
                  + (f"(its caches: {', '.join(st)})" if st else "(no Spicy Lyrics cache in Spotify - is Spicy Lyrics installed?)"))
        elif j.get("type") == "search":
            items = []
            for it in (j.get("items") or [])[:12]:
                if isinstance(it, dict) and str(it.get("uri", "")).startswith("spotify:track:"):
                    items.append(dict(uri=str(it["uri"])[:60], t=str(it.get("t") or "")[:120], a=str(it.get("a") or "")[:120],
                                      img=str(it.get("img") or "")[:400] if str(it.get("img") or "").startswith("https://") else "",
                                      dur=float(it.get("dur") or 0)))
            if j.get("error"):
                print("spotify search:", str(j["error"])[:200])
            self.deck_push("spsearch", dict(id=str(j.get("id") or ""), q=str(j.get("q") or "")[:120], items=items,
                                            error=str(j.get("error") or "")[:200] if not items else ""))
        elif j.get("type") == "pos":
            try:
                self.sp_pos = (float(j.get("pos") or 0), bool(j.get("playing")), time.time())
            except (TypeError, ValueError):
                pass
            if self.sp_remote_on() or getattr(self, "_media_remote", False):
                self._push_media(self.media_merged(getattr(self, "_win_media", None)))
            try:
                self.js("spos", [float(j.get("pos") or 0), bool(j.get("playing")), str(j.get("title") or "")])
            except (TypeError, ValueError):
                pass
        elif j.get("type") == "state":
            nxt = []
            for t in (j.get("next") or [])[:3]:
                if isinstance(t, dict) and t.get("t"):
                    nxt.append(dict(t=str(t["t"])[:120], a=str(t.get("a") or "")[:120]))
            try:
                rep = int(j.get("repeat") or 0)
            except (TypeError, ValueError):
                rep = 0
            self.sp_state = dict(title=str(j.get("title") or "")[:200], liked=bool(j.get("liked")),
                                 shuf=bool(j.get("shuffle")), rep={0: 0, 1: 2, 2: 1}.get(rep, 0), next=nxt,
                                 at=time.time())
            self.js("spstate", {k: v for k, v in self.sp_state.items() if k != "at"})

    def spotify_live(self):
        """Spotify is playing with the Spicetify bridge connected right now."""
        st = getattr(self, "sp_state", None)
        m = getattr(self.media, "last", None) or {}
        return bool(st and time.time() - st["at"] < 20 and self.spotify.listening()
                    and m.get("app") == "Spotify")

    def send_near(self, local, inside, near):
        """Cursor position + closeness (0..1) while it's near the closed island: the UI lights the edge."""
        val = None
        if near and not inside and self.mode == "pill" and not self.dragging:
            r = self.rect
            dx = max(r.left() - local.x(), 0, local.x() - r.right())
            dy = max(r.top() - local.y(), 0, local.y() - r.bottom())
            k = max(0.0, 1 - math.hypot(dx, dy) / 200)
            if k > 0:
                val = [int(local.x() / self.zoom) // 6 * 6, int(local.y() / self.zoom) // 6 * 6, round(k * 20) / 20]
        if val == self._near:
            return
        if val is not None and self._near is not None and time.time() - self._near_at < 0.03:
            return
        self._near, self._near_at = val, time.time()
        self.js("near", val)

    def heat(self, today_pct=None):
        """12 weeks of plan completion (Sunday-first columns) for the Today tab's heatmap."""
        if not self.settings.get("heatmap"):
            return None
        m = self.model
        today = m.today()
        if not (self._heat and self._heat[0] == today and time.time() - self._heat[1] < 120):
            logs = m.health.data.get("log", {}) if m.health.ok else {}
            try:
                first = min(dt.date.fromisoformat(k) for k in logs)
            except ValueError:
                first = today
            start = today - dt.timedelta(days=(today.weekday() + 1) % 7 + 77)
            out = []
            for i in range(84):
                day = start + dt.timedelta(days=i)
                e = dict(l=f"{core.DAY_EN[day.weekday()]} {day.day} {day.strftime('%b')}", p=None)
                if day == today:
                    e["t"] = 1
                if first <= day <= today:
                    try:
                        dn, tot = m.completion(day)
                        e["p"] = int(round(100 * dn / tot)) if tot else None
                    except Exception:
                        pass
                out.append(e)
            self._heat = (today, time.time(), out)
        out = self._heat[2]
        if today_pct is not None:            # today's square is always live
            for e in out:
                if e.get("t"):
                    e["p"] = today_pct
        return out

    def mem_report(self):
        """One log line: working set / private bytes of the island and each of its web-engine processes (by type)."""
        try:
            import psutil
            me = psutil.Process()
            rows = [("island", me.memory_info())]
            for c in me.children(recursive=True):
                try:
                    if not feats._own_child(c):
                        continue
                    t = re.search(r"--type=([\w-]+)", " ".join(c.cmdline()))
                    rows.append((t.group(1) if t else c.name().replace(".exe", ""), c.memory_info()))
                except Exception:
                    continue
            line = (" · ".join(f"{n} {m.rss / 2**20:.0f}/{getattr(m, 'private', 0) / 2**20:.0f}" for n, m in rows)
                    + f" MB (working set/private) · mode {self.mode} · js calls {getattr(self, '_js_n', 0)}"
                    + " (" + ", ".join(f"{k} {v}" for k, v in sorted(getattr(self, "_js_fn", {}).items(), key=lambda kv: -kv[1])[:6]) + ")")
            self._js_n = 0
            self._js_fn = {}
            threading.Thread(target=ram_top, daemon=True).start()     # who else is using the RAM (one log line)
            probe = ("JSON.stringify({heap: performance.memory ? Math.round(performance.memory.usedJSHeapSize / 1048576) : -1,"
                     " nodes: document.getElementsByTagName('*').length, imgs: document.images.length,"
                     " canv: document.getElementsByTagName('canvas').length, anims: document.getAnimations().length,"
                     " gcms: S.gcMs == null ? -1 : Math.round(S.gcMs * 10) / 10, playing: document.body.classList.contains('playing-now'), lyr: !!document.querySelector('.k'),"
                     " mode: S.mode, ua: (navigator.userAgent.match(/Chrome\\/([\\d.]+)/) || [])[1]})")
            if self.ready:
                self.page.runJavaScript(probe, lambda r: print("mem: " + line + " · page " + str(r)))
            else:
                print("mem: " + line)
        except Exception as e:
            print("mem:", e)

    # ---------------------------------------------------------- page-process memory guard
    def renderer_guard(self):
        """On this PC the web page's process grows by ~100-300 MB a minute (Chromium's own memory, not the page's JS heap,
        which stays small). Past 1.5 GB, while nobody is looking (pill, no toast, 2+ min without input; or 3 GB anyway)
        the page gets a fresh start: a reload first, and if that doesn't give the memory back, a new page process."""
        if not WIN or not self.ready or self.hidden_fs or self.dragging:
            return
        try:
            import psutil
            pid = self.page.renderProcessPid()
            mb = psutil.Process(pid).memory_info().private / 2**20 if pid else 0
        except Exception:
            return
        low = getattr(self, "low_ram", False)
        try:                              # Windows short on RAM (85 %+ used): give ours back now, at most every 2 min
            import psutil
            if psutil.virtual_memory().percent >= 85 and time.time() - self._press_at > 120:
                self._press_at = time.time()
                self.trim(force=True)
        except Exception:
            pass
        k = getattr(self, "desk", None)
        if k is not None and k.ready:
            try:
                import psutil
                dp = k.page.renderProcessPid()
                if dp and dp != pid and psutil.Process(dp).memory_info().private > (600 if low else 900) * 2**20:
                    print("mem: desktop widgets page too big -> reloading it")
                    k.ready = False
                    k.view.reload()
            except Exception:
                pass
        soft, pill_max, hard_max = (1500, 2500, 4000) if low else (2500, 4000, 6000)
        if mb < soft:
            return
        quiet = self.mode == "pill" and not self.toasts and idle_seconds() > 90
        if not (quiet or (self.mode == "pill" and mb > pill_max) or mb > hard_max):
            return
        hard = getattr(self, "_rg_soft_failed", False)
        print(f"mem: page process at {mb:.0f} MB -> {'new page process' if hard else 'reloading the page'}")
        self._rg_before = mb
        if hard:
            try:
                import psutil
                psutil.Process(pid).kill()           # renderProcessTerminated -> reload_all (the crash path, tested)
            except Exception as e:
                print("mem: couldn't restart the page process:", e)
        else:
            self.reload_all()
        QTimer.singleShot(90_000, self.renderer_guard_check)

    def gpu_report(self):
        """Once per start: how Chromium draws on this PC (chrome://gpu: GPU or software compositing, ANGLE backend).
        A hidden page, read and thrown away."""
        try:
            pg = QWebEnginePage(self)
            self._gpu_pg = pg
            js = ("(() => { const out = []; const walk = n => { if (n.shadowRoot) walk(n.shadowRoot);"
                  " for (const c of n.children || []) { if (c.tagName === 'LI' || c.tagName === 'TR') out.push(c.innerText.replace(/\\s+/g, ' ')); walk(c); } };"
                  " walk(document.body); return out.join('\\n'); })()")
            def done(t):
                lines = [x.strip() for x in str(t or "").replace("\t", " ").split("\n") if x.strip()]
                keys = ("Canvas", "Compositing", "Rasterization", "Video Decode", "Vulkan", "WebGL", "WebGPU", "Skia",
                        "GL_RENDERER", "Display type", "GL implementation", "ANGLE", "Direct Rendering", "Multiple Raster",
                        "Optimus", "GPU0", "GPU1", "Driver version", "Hardware Overlays", "DirectComposition")
                pick = list(dict.fromkeys(x for x in lines if any(x.startswith(k) for k in keys)))[:30]
                print("gpu:", " | ".join(pick)[:1500] if pick else f"chrome://gpu said nothing useful ({len(lines)} lines)")
                self._gpu_pg = None
                pg.deleteLater()
            pg.loadFinished.connect(lambda ok: QTimer.singleShot(3000, lambda: pg.runJavaScript(js, done)))
            pg.load(QUrl("chrome://gpu"))
        except Exception as e:
            print("gpu report:", e)

    def leak_lab(self, step=0):
        """Once per start, 15 min, on the desktop-widgets page: 5 min as usual, 5 min getting updates but not redrawing
        them, 5 min without updates. The MB/min of each says whether the page process grows per update or per redraw."""
        k = getattr(self, "desk", None)
        if not WIN or k is None or not k.ready:
            if step:
                print("leaklab: stopped (desktop widgets closed)")
            return
        try:
            import psutil
            mb = psutil.Process(k.page.renderProcessPid()).memory_info().private / 2**20
        except Exception as e:
            print("leaklab:", e)
            return
        calls = getattr(k, "calls", 0)
        if step:
            r = self._lab
            mins = (time.time() - r["t"]) / 60
            print(f"leaklab: {['as usual', 'updates, no redraw', 'no updates'][step - 1]}: {(mb - r['mb']) / mins:+.1f} MB/min "
                  f"({calls - r['calls']} updates in {mins:.1f} min, now {mb:.0f} MB)")
        if step == 0:
            print(f"leaklab: start (desktop widgets page at {mb:.0f} MB)")
        elif step == 1:
            k.page.runJavaScript("S.noRender = true")
        elif step == 2:
            k.page.runJavaScript("S.noRender = false")
            k.paused = True
        else:
            k.paused = False
            k.page.runJavaScript("S.noRender = false; S.dkH = null; renderDesk()")
            return
        self._lab = dict(mb=mb, t=time.time(), calls=calls)
        QTimer.singleShot(300_000, lambda: self.leak_lab(step + 1))

    def renderer_guard_check(self):
        try:
            import psutil
            mb = psutil.Process(self.page.renderProcessPid()).memory_info().private / 2**20
        except Exception:
            return
        freed = mb < getattr(self, "_rg_before", 0) * 0.5
        if not getattr(self, "_rg_soft_failed", False):
            self._rg_soft_failed = not freed
        print(f"mem: page process now {mb:.0f} MB ({'freed' if freed else 'not freed by a reload: next time it gets a new process'})")

    def trim(self, force=False):
        """Hand unused RAM back to Windows while the island just sits there (at most every 5 min)."""
        if not self.settings.get("ram_trim") or self.mode != "pill" or self.dragging:
            return
        if not force and time.time() - self._trim_at < 300:
            return
        self._trim_at = time.time()
        freed = feats.trim_memory()
        if freed > 30 * 2**20:
            top = getattr(feats.trim_memory, "top", None)
            print(f"ram trim: gave back {freed / 2**20:.0f} MB" + (f" (most from {top[0]}: {top[1] / 2**20:.0f} MB)" if top and top[0] else ""))

    def setup_watch(self):
        """ui.html changes -> reload the page; island*.py changes -> offer a restart (only if they compile)."""
        self.watcher = QFileSystemWatcher(self)
        self._watched = [os.path.join(HERE, "ui.html")] + sorted(
            os.path.join(HERE, f) for f in os.listdir(HERE) if f.startswith("island") and f.endswith(".py"))
        self.watcher.addPaths([p for p in self._watched if os.path.exists(p)])
        self.watcher.fileChanged.connect(self.on_file_changed)
        self._ui_t = QTimer(self)
        self._ui_t.setSingleShot(True)
        self._ui_t.setInterval(600)
        self._ui_t.timeout.connect(self.live_reload)
        self._py_t = QTimer(self)
        self._py_t.setSingleShot(True)
        self._py_t.setInterval(2500)
        self._py_t.timeout.connect(self.code_changed)
        self._py_sig = self.code_sig()

    def on_file_changed(self, path):
        # editors and os.replace swap the file out: watch the new one again
        def rewatch():
            if os.path.exists(path) and path not in self.watcher.files():
                self.watcher.addPath(path)
        QTimer.singleShot(400, rewatch)
        (self._ui_t if path.endswith("ui.html") else self._py_t).start()

    def live_reload(self):
        if self.settings.get("live_reload"):
            print("ui.html changed -> live reload")
            self.reload_all()
            for w in (getattr(self, "dash", None), getattr(self, "desk", None)):   # the dashboard + desktop widgets too
                if w is not None:
                    w.ready = False
                    w.view.reload()

    def code_sig(self):
        sig = {}
        for p in self._watched:
            if p.endswith(".py"):
                try:
                    sig[p] = os.path.getmtime(p)
                except OSError:
                    pass
        return sig

    def code_changed(self):
        sig = self.code_sig()
        if sig == self._py_sig:
            return
        bad = []
        for p in sig:
            try:
                with open(p, encoding="utf-8") as f:
                    compile(f.read(), p, "exec")
            except Exception as e:
                bad.append(f"{os.path.basename(p)}: {e}")
        if bad:
            print("island code changed but doesn't compile yet (no restart offered):", "; ".join(bad))
            return
        self._py_sig = sig
        print("island code changed -> offering a restart")
        self._update_pending = True
        if self.settings.get("update_toast"):
            self.notify(f"update-{time.time():.0f}", "R", "island · updated", "New island code is ready",
                        "Restart to use it (2 s), or it restarts itself next time you're away for 10 min.",
                        [("Restart now", self.restart), ("Later", None)], 60)

    def fade_in(self):
        if self._shown:
            return
        self._shown = True
        print(f"ui ready in {time.time() - self._t0:.1f} s")
        a = QVariantAnimation(self)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        a.setDuration(260)
        a.valueChanged.connect(lambda v: self.setWindowOpacity(float(v)))
        a.start()
        self._fade = a

    def push_stale(self):
        """Minutes since QAWIFit / weather last refreshed (only when it's suspiciously long), + QAWIFit's day cutoff."""
        now = time.time()
        fa = getattr(self.fit, "_full_at", 0)
        fit = int((now - fa) / 60) if (self._fit or {}).get("ok") and fa and now - fa > 15 * 60 else None
        wa = getattr(self, "_wx_at", 0)
        wx = int((now - wa) / 60) if wa and now - wa > 120 * 60 else None
        cut = None
        prof = getattr(self.fit, "profile", None)
        if prof:
            try:
                cut = int(float(prof.get("dayCutoffHour") or 0))
            except (TypeError, ValueError):
                cut = None
        st = dict(fit=fit, wx=wx, cutoff=cut)
        if st != self._stale_sent:
            self._stale_sent = st
            self.js("stale", st)

    def after_crash(self):
        if not self.after:
            return
        why = ("It froze, so the watchdog restarted it." if self.after == "froze"
               else f"It crashed (code {self.after[5:] or '?'}), so the watchdog restarted it.")
        crash_log = os.path.join(HERE, "crash_last.log")
        self.notify(f"crash-{time.time():.0f}", "R", "island · restarted", "Back up and running", why,
                    [("OK", None), ("Open crash log", lambda: os.startfile(crash_log) if WIN and os.path.exists(crash_log) else None)], 30)
        self.after = None


# ============================================================== startup / main
def startup_dir():
    appdata = os.environ.get("APPDATA")
    return os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup") if appdata else None


def install_startup():
    d = startup_dir()
    if not d:
        print("Startup install only works on Windows.")
        return
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    with open(os.path.join(d, "island_widget.bat"), "w", encoding="utf-8") as f:
        f.write(f'@echo off\nstart "" "{pyw}" "{os.path.abspath(__file__)}"\n')
    for old in ("health_widget.bat", "tawjihi_widget.bat"):
        p = os.path.join(d, old)
        if os.path.exists(p):
            os.remove(p)
            print("Removed old startup entry:", old)
    print("Island 2 will start with Windows.")


def remove_startup():
    d = startup_dir()
    p = os.path.join(d, "island_widget.bat") if d else None
    if p and os.path.exists(p):
        os.remove(p)
        print("Removed from startup.")


class _Tee:
    bol = [True]          # shared by stdout + stderr: are we at the start of a log line?
    fh = [None]           # the open island.log (shared, swapped on rotation)
    n = [0]
    tl = threading.local()  # this thread's unfinished line: print() writes args, sep and end separately,
    lock = threading.Lock() # so two threads printing at once used to mix their lines ("...open)hotkeys:")

    def __init__(self, f, orig):
        self.f, self.orig = f, orig
        _Tee.fh[0] = _Tee.fh[0] or f

    @staticmethod
    def rotate():
        p = os.path.join(HERE, "island.log")
        try:
            _Tee.fh[0].close()
            os.replace(p, os.path.join(HERE, "island.prev.log"))
        except OSError:
            pass
        _Tee.fh[0] = open(p, "w", encoding="utf-8")
        _Tee.fh[0].write(f"{dt.datetime.now():%H:%M:%S} log rotated (older lines are in island.prev.log)\n")

    def stamp(self, s):
        """Prefix every new line in island.log with HH:MM:SS (the console copy stays as is)."""
        out, ts = [], None
        for i, part in enumerate(s.split("\n")):
            if i:
                out.append("\n")
                _Tee.bol[0] = True
            if part:
                if _Tee.bol[0]:
                    ts = ts or dt.datetime.now().strftime("%H:%M:%S ")
                    out.append(ts)
                out.append(part)
                _Tee.bol[0] = False
        return "".join(out)

    def write(self, s):
        try:
            buf = getattr(_Tee.tl, "buf", "") + s
            cut = buf.rfind("\n") + 1 or (len(buf) if len(buf) > 4000 else 0)
            _Tee.tl.buf = buf[cut:]
            if cut:                                      # whole lines only, one thread at a time
                with _Tee.lock:
                    f = _Tee.fh[0]
                    f.write(self.stamp(buf[:cut]))
                    f.flush()
                    _Tee.n[0] += 1
                    if _Tee.n[0] % 400 == 0 and f.tell() > LOG_MAX:
                        _Tee.rotate()
        except Exception:
            pass
        if self.orig:
            try:
                self.orig.write(s)
            except Exception:
                pass

    def flush(self):
        pass


def setup_log():
    try:
        lp = os.path.join(HERE, "island.log")
        if os.path.exists(lp) and os.path.getsize(lp) > 0:      # keep the last run's log next to it
            try:
                os.replace(lp, os.path.join(HERE, "island.prev.log"))
            except OSError:
                pass
        f = open(lp, "w", encoding="utf-8")
        sys.stdout = _Tee(f, sys.stdout)
        sys.stderr = _Tee(f, sys.stderr)
        print(f"Island 2 start {dt.datetime.now():%Y-%m-%d %H:%M:%S}  args={sys.argv[1:]}  "
              f"python={sys.version.split()[0]}  platform={platform.platform()}")
        try:
            import PySide6
            from PySide6.QtWebEngineCore import qWebEngineChromiumVersion
            print(f"qt: PySide6 {PySide6.__version__} · Chromium {qWebEngineChromiumVersion()}")
        except Exception:
            pass
    except Exception:
        pass


def hand_off():
    """Start island_watchdog.pyw (detached); it runs this file again with ISLAND_WATCHDOG=1."""
    import subprocess
    exe = sys.executable
    w = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(w):
        exe = w
    subprocess.Popen([exe, WATCHDOG_FILE], cwd=HERE, creationflags=0x00000008 | 0x00000200, close_fds=True)


def main():
    global NO_CT
    if (WIN and not feats.under_watchdog() and os.path.exists(WATCHDOG_FILE)
            and not any(a in sys.argv for a in ("--no-watchdog", "--install-startup", "--remove-startup"))
            and feats.load_settings().get("watchdog", True)):
        return hand_off()
    setup_log()
    try:                                  # a native crash (0xC0000005) leaves every thread's stack in faults.log
        import faulthandler
        fp = os.path.join(HERE, "faults.log")
        if os.path.exists(fp) and os.path.getsize(fp) > 512 * 1024:     # a start marker every run: keep it bounded
            os.replace(fp, os.path.join(HERE, "faults.prev.log"))
        fh = open(fp, "a", encoding="utf-8")
        fh.write(f"\n--- start {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n")
        fh.flush()
        faulthandler.enable(fh, all_threads=True)
        globals()["_FAULT_FH"] = fh
    except Exception as e:
        print("faulthandler:", e)
    NO_CT = "--no-ct" in sys.argv
    if "--install-startup" in sys.argv:
        return install_startup()
    if "--remove-startup" in sys.argv:
        return remove_startup()
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-logging --disable-renderer-accessibility")
    # Qt WebEngine's V8 never starts a garbage collection because of allocations, so the page process grew ~100 MB a
    # minute with garbage (QTBUG-150562). --expose-gc lets ui.html collect it itself every 15 s (window.gc).
    if "--js-flags" not in os.environ["QTWEBENGINE_CHROMIUM_FLAGS"]:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] += " --js-flags=--expose-gc"
    if "--gl" in sys.argv:            # fallback if the window background ever renders black
        os.environ["QSG_RHI_BACKEND"] = "opengl"
    if "--software" in sys.argv:      # last-resort fallback
        os.environ["QT_QUICK_BACKEND"] = "software"
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] += " --disable-gpu"
    QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    sock = QLocalSocket()
    sock.connectToServer(APP_ID)
    if sock.waitForConnected(300):
        print("Another Island is already running -> asked it to open its panel and exiting. "
              "To restart: close the old one first (tray icon -> Quit, or taskkill /f /im pythonw.exe).")
        sock.write(b"open")
        sock.flush()
        sock.waitForBytesWritten(300)
        return
    QLocalServer.removeServer(APP_ID)
    server = QLocalServer()
    server.listen(APP_ID)
    host = Host()
    host.server = server
    app.aboutToQuit.connect(host.meter.stop)

    def on_conn():
        c = server.nextPendingConnection()
        if c:
            c.readyRead.connect(lambda: (c.readAll(), host.js("mode", "panel")))
    server.newConnection.connect(on_conn)
    host.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
