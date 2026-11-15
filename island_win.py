"""
island_win.py - Windows watchers for the island (round 3). Every feature fails soft: if Windows refuses something,
it's logged once and that feature just stays off.

  KeyThread   one thread with a Windows message loop:
              - global hotkeys (RegisterHotKey)            -> emit(("hotkey", name))
              - low-level keyboard hook, only while wanted: volume keys are swallowed (the island shows its own HUD
                instead of Windows' box) -> emit(("volkey", +1/-1/0)); Caps/Num lock -> emit(("lock", vk))
  WinWatch    one polling thread (COM MTA for pycaw): keyboard language, mic/camera in use, mic mute, internet,
              Wi-Fi, disk space, pending Windows-update restart, finished downloads, volume/mic commands.
"""

import ctypes
import datetime as dt
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

WIN = sys.platform == "win32"
if WIN:
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32

HOTKEYS = {   # name: (modifiers, vk)   MOD_ALT=1 MOD_WIN=8 MOD_NOREPEAT=0x4000
    "panel": (0x1 | 0x8 | 0x4000, ord("I")),
    "cmd": (0x1 | 0x8 | 0x4000, 0x20),          # Win+Alt+Space
    "water": (0x1 | 0x8 | 0x4000, ord("W")),
    "mic": (0x1 | 0x8 | 0x4000, ord("M")),
    "privacy": (0x1 | 0x8 | 0x4000, ord("H")),
    "timer": (0x1 | 0x8 | 0x4000, ord("T")),
    "player": (0x1 | 0x8 | 0x4000, ord("P")),
    "widgets": (0x1 | 0x8 | 0x4000, ord("D")),     # desktop widgets over every window for a moment
}
VK_VOL = {0xAD: 0, 0xAE: -1, 0xAF: +1}       # mute, down, up
VK_LOCKS = (0x14, 0x90)                      # Caps, Num
WM_HOTKEY, WM_APP, WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0312, 0x8000, 0x100, 0x101, 0x104, 0x105
LLKHF_INJECTED = 0x10


class KeyThread(threading.Thread):
    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.tid = None
        self.hook = None
        self.want = False
        self.ids = {}

    # -- called from the UI thread
    def set_hook(self, on):
        self.want = bool(on)
        if self.tid:
            user32.PostThreadMessageW(self.tid, WM_APP + 1, 0, 0)

    # -- message loop thread
    def run(self):
        if not WIN:
            return
        self.tid = kernel32.GetCurrentThreadId()
        for i, (name, (mod, vk)) in enumerate(HOTKEYS.items(), 1):
            if user32.RegisterHotKey(None, i, mod, vk):
                self.ids[i] = name
            elif user32.RegisterHotKey(None, i, 0x1 | 0x2 | 0x4 | 0x4000, vk):     # Ctrl+Alt+Shift+same key
                self.ids[i] = name
                print(f"hotkey {name}: Win+Alt taken by another app, using Ctrl+Alt+Shift+{chr(vk) if vk > 0x20 else 'Space'}")
            else:
                print(f"hotkey {name}: taken by another app")
        if self.ids:
            print("hotkeys:", ", ".join(self.ids.values()))

        LRESULT = ctypes.c_ssize_t

        class KBD(ctypes.Structure):
            _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD), ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

        HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.CallNextHookEx.restype = LRESULT
        user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        emit = self.emit

        def proc(n, wp, lp):     # keep this tiny: Windows drops slow hooks
            try:
                if n == 0:
                    k = ctypes.cast(lp, ctypes.POINTER(KBD)).contents
                    if not (k.flags & LLKHF_INJECTED):
                        vk = k.vkCode
                        if vk in VK_VOL:
                            if wp in (WM_KEYDOWN, WM_SYSKEYDOWN):
                                emit(("volkey", VK_VOL[vk]))
                            return 1                  # swallowed: no Windows volume box
                        if vk in VK_LOCKS and wp in (WM_KEYUP, WM_SYSKEYUP):
                            emit(("lock", vk))
            except Exception:
                pass
            return user32.CallNextHookEx(None, n, wp, lp)

        self._proc = HOOKPROC(proc)          # keep a reference or Python frees the callback
        msg = wintypes.MSG()
        self.sync_hook()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in self.ids:
                emit(("hotkey", self.ids[msg.wParam]))
            elif msg.message == WM_APP + 1:
                self.sync_hook()

    def sync_hook(self):
        if self.want and not self.hook:
            self.hook = user32.SetWindowsHookExW(13, self._proc, kernel32.GetModuleHandleW(None), 0)   # WH_KEYBOARD_LL
            if not self.hook:
                print("volume keys: keyboard hook refused by Windows")
        elif not self.want and self.hook:
            user32.UnhookWindowsHookEx(self.hook)
            self.hook = None


# ======================================================================= polling watcher
LANG = {0x09: "EN", 0x01: "ع", 0x07: "DE", 0x04: "中", 0x0C: "FR", 0x0A: "ES", 0x10: "IT", 0x19: "RU", 0x1F: "TR"}
PARTIAL = (".crdownload", ".part", ".tmp", ".download", ".opdownload", ".partial")


def app_name(key):
    """'C:#Program Files#Discord#Discord.exe' or 'Microsoft.WindowsCamera_8wekyb...' -> 'Discord' / 'Camera'"""
    base = key.replace("#", "\\").split("\\")[-1]
    base = re.sub(r"\.exe$", "", base, flags=re.I)
    base = re.sub(r"_[a-z0-9]{13}$", "", base)
    base = base.split(".")[-1] if "." in base else base
    return {"WindowsCamera": "Camera", "chrome": "Chrome", "msedge": "Edge", "ms-teams": "Teams"}.get(base, base)[:20]


class WinWatch(threading.Thread):
    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.q = queue.Queue()
        self.errs = {}
        self.lang = None
        self.priv = None
        self.net_ok, self.net_fails, self.down_at = True, 0, None
        self.dl_seen, self.dl_pending = None, {}
        self.next = {}
        self.disk_warned = self.upd_warned = None
        self.ep = self.mic = None

    def cmd(self, what, arg=None):
        self.q.put((what, arg))

    def err(self, key, e):
        if self.errs.get(key) != str(e):
            self.errs[key] = str(e)
            print(f"{key}:", e)

    def due(self, key, every):
        now = time.time()
        if now >= self.next.get(key, 0):
            self.next[key] = now + every
            return True
        return False

    def run(self):
        if not WIN:
            return
        sys.coinit_flags = 0
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass
        try:
            from pycaw.pycaw import AudioUtilities
            self.AU = AudioUtilities
        except Exception as e:
            self.AU = None
            print("volume HUD / mic mute need pycaw:", e)
        while True:
            try:
                what, arg = self.q.get(timeout=0.25)
                self.handle(what, arg)
            except queue.Empty:
                pass
            except Exception as e:
                self.err("command", e)
            for key, every, fn in (("lang", 0, self.poll_lang), ("priv", 2, self.poll_priv), ("net", 10, self.poll_net),
                                   ("dl", 2, self.poll_downloads), ("disk", 600, self.poll_disk),
                                   ("upd", 1800, self.poll_update), ("wifi", 60, self.poll_wifi)):
                if every == 0 or self.due(key, every):
                    try:
                        fn()
                    except Exception as e:
                        self.err(key, e)

    # ---------------------------------------------------------------- volume + mic (pycaw)
    def _ep(self, dev):
        if dev is None:
            return None
        if hasattr(dev, "EndpointVolume"):
            return dev.EndpointVolume
        from pycaw.pycaw import IAudioEndpointVolume          # older pycaw: raw IMMDevice
        from comtypes import CLSCTX_ALL
        return dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None).QueryInterface(IAudioEndpointVolume)

    def endpoint(self):
        return self._ep(self.AU.GetSpeakers())

    def mic_ep(self):
        return self._ep(self.AU.GetMicrophone())

    @staticmethod
    def native_key(d):
        """Our hook swallowed a volume key but we couldn't apply it: replay it (injected = the hook lets it through)."""
        vk = {0: 0xAD, -1: 0xAE, 1: 0xAF}[d]
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)

    def handle(self, what, arg):
        if not self.AU:
            if what == "vol":
                self.native_key(arg)
            return
        if what == "vol":
            try:
                ep = self.endpoint()
                ep.GetMute()
            except Exception as e:
                self.err("volume", e)
                return self.native_key(arg)
            if arg == 0:
                ep.SetMute(int(not ep.GetMute()), None)
            else:
                v = round(max(0.0, min(1.0, ep.GetMasterVolumeLevelScalar() + 0.02 * arg)), 2)
                ep.SetMasterVolumeLevelScalar(v, None)
                if v > 0 and ep.GetMute():
                    ep.SetMute(0, None)
            self.emit(("vol", dict(v=round(float(ep.GetMasterVolumeLevelScalar()), 2), mute=bool(ep.GetMute()))))
        elif what == "mic":
            m = self.mic_ep()
            if m is None:
                self.emit(("mic", dict(none=True)))
                return
            m.SetMute(int(not m.GetMute()) if arg is None else int(bool(arg)), None)
            self.emit(("mic", dict(mute=bool(m.GetMute()))))
            self.priv = None              # re-send the dots

    # ---------------------------------------------------------------- polls
    def poll_lang(self):
        hwnd = user32.GetForegroundWindow()
        tid = user32.GetWindowThreadProcessId(hwnd, None)
        hkl = user32.GetKeyboardLayout(tid)
        lid = hkl & 0xFFFF
        if not lid:
            return
        lang = LANG.get(lid & 0x3FF, f"{lid & 0x3FF:02X}")
        if self.lang is not None and lang != self.lang:
            self.emit(("lang", lang))
        self.lang = lang

    def poll_priv(self):
        import winreg
        out = {}
        for cap in ("microphone", "webcam"):
            users = []
            base = rf"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\{cap}"
            for sub in ("", r"\NonPackaged"):
                try:
                    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base + sub)
                except OSError:
                    continue
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    if name == "NonPackaged":
                        continue
                    try:
                        with winreg.OpenKey(k, name) as a:
                            stop, _ = winreg.QueryValueEx(a, "LastUsedTimeStop")
                            start, _ = winreg.QueryValueEx(a, "LastUsedTimeStart")
                        if stop == 0 and start:
                            users.append(app_name(name))
                    except OSError:
                        continue
            out["mic" if cap == "microphone" else "cam"] = sorted(set(users))
        muted = None
        if self.AU and out["mic"]:
            try:
                m = self.mic_ep()
                muted = bool(m.GetMute()) if m is not None else None
            except Exception:
                muted = None
        out["micMuted"] = muted
        if out != self.priv:
            self.priv = out
            self.emit(("priv", out))

    def poll_net(self):
        t0 = time.time()
        ok = False
        for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
            try:
                with socket.create_connection((host, port), timeout=3):
                    ok = True
                    break
            except OSError:
                continue
        ms = int((time.time() - t0) * 1000)
        if ok:
            self.net_fails = 0
            if not self.net_ok:
                gone = time.time() - (self.down_at or time.time())
                self.emit(("net", dict(up=True, gone=int(gone), ms=ms)))
            self.net_ok = True
            self.emit(("ping", ms))
        else:
            self.net_fails += 1
            if self.net_fails >= 2 and self.net_ok:
                self.net_ok, self.down_at = False, time.time() - 10
                self.emit(("net", dict(up=False)))

    def poll_wifi(self):
        r = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True, timeout=5,
                           creationflags=0x08000000, encoding="utf-8", errors="replace")
        ssid = sig = None
        for line in r.stdout.splitlines():
            m = re.match(r"\s*SSID\s*:\s*(.+)$", line)
            if m:
                ssid = m.group(1).strip()
            m = re.match(r"\s*Signal\s*:\s*(\d+)%", line)
            if m:
                sig = int(m.group(1))
        self.emit(("wifi", dict(ssid=ssid, signal=sig)))

    def poll_downloads(self):
        d = os.path.join(os.path.expanduser("~"), "Downloads")
        try:
            now = {e.name: (e.stat().st_size, e.stat().st_mtime) for e in os.scandir(d) if e.is_file()}
        except OSError:
            return
        if self.dl_seen is None:
            self.dl_seen = set(now)
            return
        for name, (size, mt) in now.items():
            if name in self.dl_seen or name.lower().endswith(PARTIAL) or name.startswith("~$"):
                continue
            prev = self.dl_pending.get(name)
            if prev is not None and prev == size and time.time() - mt > 1.5:      # size stable: finished
                self.dl_seen.add(name)
                self.dl_pending.pop(name, None)
                self.emit(("download", dict(name=name, path=os.path.join(d, name), size=size)))
            else:
                self.dl_pending[name] = size
        self.dl_seen &= set(now)

    def poll_disk(self):
        free = shutil.disk_usage("C:\\").free / 2**30
        today = dt.date.today()
        if free < 15 and self.disk_warned != today:
            self.disk_warned = today
            self.emit(("disk", round(free, 1)))

    def poll_update(self):
        import winreg
        try:
            winreg.CloseKey(winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                           r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired"))
            pending = True
        except OSError:
            pending = False
        today = dt.date.today()
        if pending and self.upd_warned != today:
            self.upd_warned = today
            self.emit(("update", True))


def lock_state(vk):
    return bool(user32.GetKeyState(vk) & 1) if WIN else False


def find_app(query):
    """Start-menu shortcut whose name best matches `query` (for the command bar's 'open ...')."""
    q = query.lower().strip()
    if not q:
        return None
    roots = [os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
             os.path.join(os.environ.get("PROGRAMDATA", ""), r"Microsoft\Windows\Start Menu\Programs")]
    best = None
    for root in roots:
        for dp, _, files in os.walk(root):
            for f in files:
                if not f.lower().endswith((".lnk", ".url")):
                    continue
                name = f.rsplit(".", 1)[0].lower()
                if "uninstall" in name:
                    continue
                score = 3 if name == q else 2 if name.startswith(q) else 1 if q in name else 0
                if score and (best is None or score > best[0] or (score == best[0] and len(name) < best[1])):
                    best = (score, len(name), os.path.join(dp, f), f.rsplit(".", 1)[0])
    return (best[2], best[3]) if best else None


NO_WINDOW = 0x08000000


def run_ps(cmd, timeout=25):
    """PowerShell, hidden, UTF-8 out. '' on any error."""
    if not WIN:
        return ""
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + cmd],
                           capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        return r.stdout.decode("utf-8", "replace")
    except Exception as e:
        print("powershell:", e)
        return ""


def start_apps():
    """Every app in the Start menu - normal programs AND Store/MSIX apps (the Windows Claude app is one of these):
    [{name, appid}]. Launch one with shell:AppsFolder\\<appid>."""
    import json
    out = run_ps("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
    try:
        j = json.loads(out.strip() or "[]")
    except ValueError:
        return []
    if isinstance(j, dict):
        j = [j]
    seen, res = set(), []
    for x in j:
        n, a = str(x.get("Name") or "").strip(), str(x.get("AppID") or "").strip()
        if n and a and a not in seen and "uninstall" not in n.lower():
            seen.add(a)
            res.append(dict(name=n, appid=a))
    res.sort(key=lambda x: x["name"].lower())
    return res


def focus_hwnd(h):
    """Restore if minimised and put that window in front of everything (then back to normal stacking)."""
    hv = ctypes.c_void_p(h)
    if user32.IsIconic(hv):
        user32.ShowWindow(hv, 9)                                        # SW_RESTORE
    f = 0x0001 | 0x0002 | 0x0040                                        # NOSIZE | NOMOVE | SHOWWINDOW
    user32.SetWindowPos(hv, ctypes.c_void_p(-1), 0, 0, 0, 0, f)
    user32.SetWindowPos(hv, ctypes.c_void_p(-2), 0, 0, 0, 0, f)
    user32.SetForegroundWindow(hv)


def bring_front(name, own=(), timeout=10.0):
    """After opening an app: wait for its window and put it in front. An app that was already open (maybe behind other
    windows, or Discord coming back from the tray) is found the same way. Its exe matching the name's first word wins
    over a window title that merely mentions it (a browser tab called 'Discord - Wikipedia')."""
    if not WIN or not name:
        return False
    try:
        import psutil
    except Exception:
        psutil = None
    nm = name.lower()
    words = [w for w in re.split(r"[^a-z0-9]+", nm) if len(w) > 3]      # Google Chrome -> chrome.exe, VS Code -> code.exe
    first = (re.split(r"[^a-z0-9]+", nm) or [nm])[0]
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    end = time.time() + timeout
    while time.time() < end:
        best = []

        def cb(hwnd, _):
            try:
                return look(hwnd)
            except Exception:
                return True

        def look(hwnd):
            h = int(hwnd or 0)
            if not h or h in own or not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            exe = ""
            if psutil is not None:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    exe = psutil.Process(pid.value).name().lower()
                except Exception:
                    pass
            stem = exe[:-4] if exe.endswith(".exe") else exe
            sc = 2 if stem and (stem == first or stem in words or stem == nm.replace(" ", "")) else 1 if nm in buf.value.lower() else 0
            if sc and (not best or sc > best[0]):
                best[:] = [sc, h]
            return True
        try:
            user32.EnumWindows(EnumProc(cb), 0)
            if best:
                focus_hwnd(best[1])
                return True
        except Exception as e:
            print("bring front:", e)
            return False
        time.sleep(0.35)
    return False


def start_lnk(name):
    """The Start-menu shortcut (.lnk) with exactly this name, if there is one (its icon + launch are the most reliable)."""
    f = find_app(name)
    return f[0] if f and f[1].lower() == name.lower() and f[0].lower().endswith(".lnk") else None


KNOWN_DIRS = {
    "6D809377-6AF0-444B-8957-A3773F02200E": os.environ.get("ProgramW6432") or os.environ.get("ProgramFiles", ""),
    "905E63B6-C1BF-494E-B29C-65B732D3D21A": os.environ.get("ProgramFiles", ""),
    "7C5A40EF-A0FB-4BFC-874A-C0F2E0B9FA8E": os.environ.get("ProgramFiles(x86)", ""),
    "F38BF404-1D43-42F2-9305-67DE0B28FC23": os.environ.get("SystemRoot", ""),
    "1AC14E77-02E7-4E5D-B744-2EB1AE5198B7": os.path.join(os.environ.get("SystemRoot", ""), "System32"),
    "D65231B0-B2F1-4857-A4CE-A8E7C6EA7D27": os.path.join(os.environ.get("SystemRoot", ""), "SysWOW64"),
    "F1B32785-6FBA-4FCF-9D55-7B8E7F157091": os.environ.get("LOCALAPPDATA", ""),
    "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D": os.environ.get("APPDATA", ""),
    "62AB5D82-FDC1-4DC3-A9DD-070D1D495D97": os.environ.get("PROGRAMDATA", ""),
    "5CD7AEE2-2219-4A67-B85D-6C9CE15660CB": os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
}


def _best_asset(path):
    """A Store app's logo: Assets\\Logo.png really lives as Logo.targetsize-48_altform-unplated.png, Logo.scale-200.png ..."""
    d, f = os.path.split(path)
    base, ext = os.path.splitext(f)
    try:
        names = os.listdir(d)
    except OSError:
        return path if os.path.exists(path) else None
    best = None
    for n in names:
        ln = n.lower()
        if not (ln == f.lower() or (ln.startswith(base.lower() + ".") and ln.endswith(ext.lower()))):
            continue
        if "contrast" in ln:
            continue
        sc = 10
        m = re.search(r"targetsize-(\d+)", ln)
        if m:
            t = int(m.group(1))
            sc = 60 - abs(t - 64) / 4 + (25 if "altform-unplated" in ln else 0) - (40 if "lightunplated" in ln else 0)
        else:
            m = re.search(r"scale-(\d+)", ln)
            if m:
                sc = 30 + min(int(m.group(1)), 200) / 10
        if best is None or sc > best[0]:
            best = (sc, os.path.join(d, n))
    return best[1] if best else None


def app_icon_src(appid):
    """A file to take a Start-menu app's icon from: a Store app's logo PNG (or its exe), or a program's exe."""
    if not WIN or not appid:
        return None
    try:
        if "!" in appid and "\\" not in appid:
            pfn, aid = appid.split("!", 1)
            if not re.match(r"^[\w.\-]+$", pfn):
                return None
            loc = run_ps(f"(Get-AppxPackage | Where-Object {{ $_.PackageFamilyName -eq '{pfn}' }} | "
                         "Select-Object -First 1).InstallLocation").strip()
            if not loc or not os.path.isdir(loc):
                return None
            with open(os.path.join(loc, "AppxManifest.xml"), encoding="utf-8", errors="replace") as fh:
                man = fh.read()
            m = re.search(r'<Application\b[^>]*\bId="%s"[\s\S]*?</Application>' % re.escape(aid), man)
            blk = m.group(0) if m else man
            for attr in ("Square44x44Logo", "Square150x150Logo"):
                for v in re.findall(attr + r'="([^"]+)"', blk):
                    f = _best_asset(os.path.join(loc, v))
                    if f:
                        return f
            ex = re.search(r'\bExecutable="([^"]+)"', blk)
            if ex and os.path.exists(os.path.join(loc, ex.group(1))):
                return os.path.join(loc, ex.group(1))
            m = re.search(r"<Logo>([^<]+)</Logo>", man)
            return _best_asset(os.path.join(loc, m.group(1))) if m else None
        p = appid
        m = re.match(r"^\{([0-9A-Fa-f-]{36})\}\\(.*)$", appid)
        if m:
            base = KNOWN_DIRS.get(m.group(1).upper())
            if not base:
                return None
            p = os.path.join(base, m.group(2))
        return p if p.lower().endswith(".exe") and os.path.exists(p) else None
    except Exception as e:
        print("app icon src:", e)
        return None


class GpuWorker(threading.Thread):
    """NVIDIA GPU load / temperature / memory every 2 s, but only while someone looks at it (System tab or the
    dashboard): polling a laptop GPU can keep it awake. nvidia-ml-py if installed, else nvidia-smi."""

    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.want = False
        self.ev = threading.Event()
        self.hist = []

    def watch(self, on):
        self.want = bool(on)
        if on:
            self.ev.set()

    def run(self):
        if not WIN:
            return
        nv = h = None
        try:
            import pynvml
            pynvml.nvmlInit()
            nv, h = pynvml, pynvml.nvmlDeviceGetHandleByIndex(0)
        except Exception:
            nv = None
        smi = shutil.which("nvidia-smi") or next((x for x in (r"C:\Windows\System32\nvidia-smi.exe",
                                                             r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe")
                                                  if os.path.exists(x)), None)
        if nv is None and not smi:
            print("gpu: no NVIDIA driver tools found")
            return
        print("gpu: reading through " + ("nvidia-ml-py" if nv else "nvidia-smi") + " (only while the System tab is open)")
        errs = 0
        while True:
            if not self.want:
                self.ev.wait()
                self.ev.clear()
                continue
            try:
                if nv:
                    name = nv.nvmlDeviceGetName(h)
                    name = name.decode() if isinstance(name, bytes) else str(name)
                    util = float(nv.nvmlDeviceGetUtilizationRates(h).gpu)
                    temp = float(nv.nvmlDeviceGetTemperature(h, 0))
                    mem = nv.nvmlDeviceGetMemoryInfo(h)
                    mu, mt = mem.used / 2**30, mem.total / 2**30
                else:
                    r = subprocess.run([smi, "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                                        "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=6,
                                       creationflags=NO_WINDOW)
                    out = (r.stdout or "").strip()
                    row = next((ln for ln in out.splitlines() if ln.count(",") >= 4), None)
                    if row is None:     # driver asleep / "couldn't communicate with the NVIDIA driver" etc.
                        why = (out or (r.stderr or "").strip() or f"no output (exit {r.returncode})").splitlines()[0]
                        raise RuntimeError("nvidia-smi: " + why[:140])
                    f = [x.strip() for x in row.split(",")]
                    name, util, temp, mu, mt = f[0], float(f[1]), float(f[2]), float(f[3]) / 1024, float(f[4]) / 1024
                name = re.sub(r"^(NVIDIA\s+)?(GeForce\s+)?", "", name).replace(" Laptop GPU", "")
                self.hist.append(round(util, 1))
                del self.hist[:-40]
                self.emit(("gpu", dict(name=name[:24], util=util, temp=temp, mu=round(mu, 1), mt=round(mt), h=list(self.hist))))
                errs = 0
            except Exception as e:
                errs += 1
                if errs in (1, 30):
                    print("gpu:", e)
            time.sleep(2 if errs < 5 else 60)    # keeps failing: don't spawn nvidia-smi every few seconds


BT_PS = (r"$k='{104EA319-6EE2-4701-BD47-8DDBF425BBE5} 2';"
         r"Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -match '^(BTHENUM|BTHLE|BTHLEDEVICE)' -and $_.Status -eq 'OK' } |"
         r" ForEach-Object { $b = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName $k -ErrorAction SilentlyContinue).Data;"
         r" if ($b -ne $null) { '{0}|{1}' -f $_.FriendlyName, $b } }")


class BtWorker(threading.Thread):
    """Battery of connected Bluetooth devices (headphones, controllers) that report it to Windows, every 3 min."""

    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.last = None
        self.ev = threading.Event()

    def now(self):
        self.ev.set()

    def run(self):
        if not WIN:
            return
        time.sleep(20)
        while True:
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", BT_PS], capture_output=True,
                                   text=True, timeout=40, creationflags=NO_WINDOW)
                devs = {}
                for line in r.stdout.splitlines():
                    if "|" not in line:
                        continue
                    name, _, v = line.rpartition("|")
                    try:
                        v = int(v.strip())
                    except ValueError:
                        continue
                    name = re.sub(r"\s+(Hands-Free|Stereo|Avrcp Transport|AG Audio).*$", "", name.strip())[:28]
                    if 0 < v <= 100 and name:
                        devs[name] = min(devs.get(name, 101), v)
                out = [dict(name=k, v=v) for k, v in sorted(devs.items())]
                if out != self.last:
                    self.last = out
                    self.emit(("bt", out))
            except Exception as e:
                print("bluetooth battery:", e)
            self.ev.wait(180)
            self.ev.clear()


LAUNCH_GUESS = ["Spotify", "Discord", "Visual Studio Code", "Google Chrome", "Brave", "Steam", "Claude", "UiPath Studio",
                "Unity Hub", "Adobe Photoshop", "Prism Launcher", "OBS Studio"]


def guess_launch():
    """First run: the apps from this list that are installed (max 6) + Claude on the web."""
    out = []
    for q in LAUNCH_GUESS:
        f = find_app(q)
        if f and all(x.get("path") != f[0] for x in out):
            out.append(dict(name=f[1][:20], path=f[0]))
        if len(out) >= 6:
            break
    if not any("claude" in x["name"].lower() for x in out):
        out.append(dict(name="Claude", url="https://claude.ai"))
    return out


class BrightWorker(threading.Thread):
    """Screen brightness for every display (laptop panel via WMI, monitors via DDC/CI), coalesced."""

    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.q = queue.Queue()
        self.mons = []

    def set(self, idx, v):
        self.q.put((int(idx), max(0.0, min(1.0, float(v)))))

    def run(self):
        if not WIN:
            return
        try:
            ctypes.windll.ole32.CoInitializeEx(None, 0)
        except Exception:
            pass
        try:
            import screen_brightness_control as sbc
        except Exception:
            print("brightness: one install needed: pip install screen-brightness-control")
            return
        self.sbc = sbc
        self.refresh()
        while True:
            try:
                want = {}
                idx, v = self.q.get(timeout=60)
                want[idx] = v
                time.sleep(0.08)                 # let a drag settle, keep the newest value per display
                while not self.q.empty():
                    i2, v2 = self.q.get_nowait()
                    want[i2] = v2
                for i, v in want.items():
                    self.sbc.set_brightness(int(round(v * 100)), display=i)
                    if i < len(self.mons):
                        self.mons[i]["v"] = v
                self.emit(("bri", [dict(m) for m in self.mons]))
            except queue.Empty:
                self.refresh()
            except Exception as e:
                print("brightness:", e)
                time.sleep(2)

    def refresh(self):
        try:
            names = self.sbc.list_monitors()
            out = []
            for i, n in enumerate(names):
                try:
                    v = self.sbc.get_brightness(display=i)
                    v = v[0] if isinstance(v, list) else v
                except Exception:
                    continue
                short = "Laptop" if re.search(r"built|internal|panel|BOE|AUO|LG Display|Samsung Display|CSOT|NE1", n, re.I) else n.split(" ")[-1][:14]
                out.append(dict(key=f"bri:{i}", name=short, v=round(float(v) / 100, 2), mute=False, live=False, bri=True))
            if out != self.mons:
                self.mons = out
                self.emit(("bri", [dict(m) for m in out]))
        except Exception as e:
            print("brightness:", e)
