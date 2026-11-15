"""
island_feats.py - settings + housekeeping for the island (no Qt in here).

  Settings      island_settings.json: every feature toggle; SCHEMA drives the panel's Settings tab
  housekeep()   weekly backup of configs/plans into backups/, old *.bak-* files moved to _to_delete/baks
  trim_memory() hands the island's unused RAM (incl. the web view processes) back to Windows
  heartbeat()   tells island_watchdog.pyw the UI thread is alive

Nothing here deletes files: old things are MOVED into _to_delete\\ (the user empties it himself).
"""

import datetime as dt
import json
import os
import shutil
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(HERE, "island_settings.json")
HEARTBEAT = os.path.join(HERE, "heartbeat.txt")
BACKUPS = os.path.join(HERE, "backups")
TRASH = os.path.join(HERE, "_to_delete")

# --------------------------------------------------------------------------- settings schema
# type: bool | choice (opts = [[value, label], ...]) | button (runs on_action "do" <k>)
# "state": True = lives in island_state.json (handled by the host), not in island_settings.json
SCHEMA = [
    dict(id="look", t="Look", items=[
        dict(k="theme", t="Theme", type="choice",
             opts=[["default", "Night"], ["berserk", "Berserk"], ["qawi", "QAWI"], ["album", "Album"]]),
        dict(k="pillstyle", t="Pill", type="choice", opts=[["clean", "Clean"], ["classic", "Classic"]]),
        dict(k="shape", t="Shape", type="choice", opts=[["notch", "Notch"], ["float", "Floating"]]),
        dict(k="rim", t="Music glow rim", type="bool"),
        dict(k="weather", t="Weather", d="the forecast in the island and its widgets (off: nothing is fetched)", type="bool"),
        dict(k="desk", t="Desktop widgets", d="on the wallpaper, under your windows · hover them to edit", type="bool"),
        dict(k="buddy", t="Island buddy", type="choice", opts=[["orb", "Orb"], ["bars", "Pulse bars"], ["ring", "Ring"], ["dots", "Dot grid"], ["off", "Off"]]),
        dict(k="split", t="Timer bubble", type="bool"),
        dict(k="scale", t="Size", type="choice",
             opts=[[0.8, "80"], [0.9, "90"], [1.0, "100"], [1.1, "110"], [1.2, "120"], [1.3, "130"]]),
        dict(k="motion", t="Motion", type="choice", opts=[["calm", "Calm"], ["springy", "Springy"], ["extra", "Extra"]]),
        dict(k="font", t="Font", type="choice", opts=[["geist", "Geist"], ["segoe", "Segoe"]]),
        dict(k="icons", t="Icons", type="bool"),
        dict(k="digits", t="Rolling numbers", type="bool"),
        dict(k="glow", t="Edge glow near the mouse", type="bool"),
        dict(k="accent", t="Colored edge", type="bool"),
        dict(k="bump", t="Bump on new events", type="bool"),
        dict(k="breathe", t="Breathing edge", type="bool"),
        dict(k="heatmap", t="12-week heatmap", type="bool"),
    ]),
    dict(id="music", t="Music", items=[
        dict(k="viz", t="Live music bars", type="bool"),
        dict(k="lyrics", t="Lyrics", type="choice", opts=[["off", "Off"], ["peek", "Card"], ["pill", "Card + pill"]]),
        dict(k="squig", t="Squiggly song bar", type="bool"),
        dict(k="player", t="Player", type="choice", opts=[["pill", "Pill"], ["full", "Big"]]),
        dict(k="artbg", t="Album art backdrop", type="bool"),
    ]),
    dict(id="sounds", t="Sounds", items=[
        dict(k="sounds", t="Sounds", type="choice", opts=[["soft", "Soft"], ["custom", "Your file"], ["windows", "Windows"], ["off", "Off"]]),
        dict(k="sound_folder", t="Your sound files", d="Drop notify.mp3 (or .wav) in this folder, then pick Your file", type="button", label="Open"),
        dict(k="sound_vol", t="Volume", type="choice", opts=[[0.35, "Low"], [0.65, "Mid"], [1.0, "High"]]),
        dict(k="confetti", t="Celebrate 100% days", type="bool"),
        dict(k="test_sounds", t="Preview sounds", type="button", label="Play"),
        dict(k="test_confetti", t="Preview celebration", type="button", label="Play"),
    ]),
    dict(id="day", t="Day", items=[
        dict(k="prayer", t="Prayer times + adhan", d="Amman (Awqaf times). prayer.mp3 in your sounds folder = a real adhan", type="bool"),
        dict(k="adhkar", t="Morning and evening adhkar", type="bool"),
        dict(k="brief", t="Morning briefing", d="Your day + your sleep, when you sit down", type="bool"),
        dict(k="recap", t="Night recap", d="Around 10:45 PM", type="bool"),
        dict(k="bedtime", t="Bedtime", type="choice", opts=[["23:30", "11:30"], ["00:00", "12:00"], ["00:30", "12:30"], ["01:00", "1:00"], ["off", "Off"]]),
        dict(k="stretch", t="Stand-up nudge", d="After an hour at the PC, not during focus", type="bool"),
        dict(k="checkin", t="Sunday check-in reminder", d="QAWIFit weekly check-in", type="bool"),
    ]),
    dict(id="notify", t="Notifications", items=[
        dict(k="mirror_whatsapp", t="Show WhatsApp messages", type="bool"),
        dict(k="mirror_phone", t="Show iPhone notifications (Phone Link)", type="bool"),
        dict(k="mirror_discord", t="Show Discord", type="bool"),
        dict(k="mirror_outlook", t="Show Outlook", type="bool"),
        dict(k="quiet", t="Quiet hours", d="No toasts (prayer + timers still come). Missed ones wait in the bell tab", type="choice",
             opts=[["off", "Off"], ["23:30-07:30", "11:30–7:30"], ["00:30-08:00", "12:30–8"], ["01:00-09:00", "1–9"]]),
        dict(k="wx_alerts", t="Rain and dust alerts", type="bool"),
        dict(k="screen", t="Screen time", d="Per app, per day. Never leaves this PC", type="bool"),
    ]),
    dict(id="voice", t="Voice", items=[
        dict(k="voice", t="Voice assistant", d="Hold the key, talk, let go. It runs on this PC: nothing is sent online", type="bool"),
        dict(k="voice_key", t="Hold to talk", type="choice", opts=[["rctrl", "Right Ctrl"], ["ralt", "Right Alt"], ["x1", "Mouse back"], ["x2", "Mouse fwd"]]),
        dict(k="wake", t='Say "Hey Island"', d="Listens only for that phrase. Sleeps while a game is in front", type="bool"),
        dict(k="voice_model", t="Speech model", type="choice", opts=[["base", "Fast"], ["small", "Accurate"]]),
        dict(k="voice_install", t="Install voice", d="One time, about 250 MB (speech-to-text that runs on this PC). The model downloads on first use", type="button", label="Install"),
        dict(k="brain", t="AI brain", d="For anything the island doesn't know: a small model on this PC works out the command or answers. Offline",
             type="choice", opts=[["off", "Off"], ["fast", "Fast"], ["smart", "Smart"]]),
        dict(k="brain_install", t="Set up the brain", d="Ollama + Qwen 3.5, about 4 GB, one time", type="button", label="Install"),
        dict(k="say_voices", t="Pronunciation voices", d="Windows' Mandarin + German voices for the speaker on the word of the day", type="button", label="Add voices"),
    ]),
    dict(id="study", t="Study", items=[
        dict(k="guard", t="Distraction guard", d="A game in front during focus time: the island calls you out", type="bool"),
        dict(k="next_nudge", t="Next one's ready", d="Finish a study block early: the island offers the next session right away", type="bool"),
        dict(k="block", t="Block YouTube, Instagram, TikTok", d="During study blocks + focus sessions. Needs the setup below once", type="bool"),
        dict(k="block_setup", t="Site blocker setup", d="One time. Windows asks for admin", type="button", label="Set up"),
    ]),
    dict(id="windows", t="Windows", items=[
        dict(k="volhud", t="Volume HUD", d="Volume keys show here, not Windows' box", type="bool"),
        dict(k="locks", t="Caps / Num lock", type="bool"),
        dict(k="lang", t="Keyboard language", type="bool"),
        dict(k="privacy_dots", t="Mic & camera dots", type="bool"),
        dict(k="battery", t="Charging & low battery", type="bool"),
        dict(k="devbat", t="Mouse & keyboard battery", d="2.4 GHz dongles (Attack Shark, VIA keyboards) · Widgets tab", type="bool"),
        dict(k="shots", t="Screenshot preview", type="bool"),
        dict(k="downloads", t="Finished downloads", type="bool"),
        dict(k="clips", t="Clipboard history", d="Win+Alt+Space, type clip", type="bool"),
        dict(k="net", t="Offline alert", type="bool"),
        dict(k="share", t="Private while OBS runs", type="bool"),
    ]),
    dict(id="dev", t="Dev", items=[
        dict(k="mcp_setup", t="Island for Claude Desktop (MCP)", d="Copies the config to paste into claude_desktop_config.json", type="button", label="Copy"),
        dict(k="lan", t="Phone & tablet deck", d="Music, volume per app, mic and your buttons from the phone or tablet. If Windows asks: Private networks", type="bool"),
        dict(k="deck_qr", t="Scan with the phone / tablet camera", d="iPhone: Share > Add to Home Screen. Android: menu > Install app. Same home network as the PC", type="qr"),
        dict(k="lan_copy", t="Deck link", d="Has its key in it, so don't post it anywhere", type="button", label="Copy"),
        dict(k="deck_usb", t="Tablet over USB (Android)", d="USB debugging on + adb on the PC. Opens on the tablet by itself", type="bool"),
    ]),
    dict(id="island", t="Island", items=[
        dict(k="simple", t="Simple mode", d="Only the basics: fewer tabs, settings and pop-ups. Off = everything", type="bool"),
        dict(k="welcomed", t="Welcome card seen", type="bool", hidden=True),
        dict(k="compact", t="Compact island", type="bool", state=True),
        dict(k="follow", t="Follow the mouse to other screens", type="bool"),
        dict(k="autohide", t="Auto-hide the pill", d="Folds into a thin line; touch the top edge to bring it back", type="bool"),
        dict(k="watchdog", t="Auto-restart if it crashes", type="bool"),
        dict(k="live_reload", t="Live-reload the look", type="bool"),
        dict(k="update_toast", t="Update toasts", type="bool"),
        dict(k="ram_trim", t="Trim RAM when idle", type="bool"),
        dict(k="backups", t="Weekly backup", type="bool"),
        dict(k="restart", t="Restart island", type="button", label="Restart"),
        dict(k="folder", t="Island folder", type="button", label="Open"),
        dict(k="log", t="Log", type="button", label="Open"),
    ]),
]

DEFAULTS = dict(simple=True, welcomed=False, shape="notch", rim=True, pet=True, buddy="orb", desk=True, weather=False, split=True, devbat=True, squig=True, voice=False, voice_key="rctrl", wake=True, voice_model="base", brain="off", lan=False, deck_usb=False, checkin=False, mirror_whatsapp=True, mirror_phone=True, mirror_discord=False, mirror_outlook=False, quiet="off", wx_alerts=True, screen=True, prayer=True, adhkar=False, brief=True, recap=False, bedtime="00:00", stretch=False, block=False, guard=True, next_nudge=True, autohide=False, player="pill", follow=True, watchdog=True, live_reload=True, update_toast=True, ram_trim=True, backups=True,
                theme="default", pillstyle="clean", font="geist", scale=1.0, motion="springy", icons=True, digits=True, glow=True,
                accent=True, bump=True, breathe=False, heatmap=True, glass=False,
                viz=True, lyrics="pill", artbg=True, sounds="soft", sound_vol=0.65, confetti=True,
                volhud=True, locks=True, lang=True, privacy_dots=True, battery=True, shots=True, downloads=True,
                clips=True, net=True, share=True)


def schema_item(k):
    for g in SCHEMA:
        for it in g["items"]:
            if it["k"] == k:
                return it
    return None


def load_settings():
    """Defaults merged with island_settings.json (unknown keys are kept, so newer files still load)."""
    vals = dict(DEFAULTS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, dict):
            vals.update(j)
    except FileNotFoundError:
        pass
    except Exception as e:
        print("settings: could not read island_settings.json:", e)
    return vals


class Settings:
    def __init__(self):
        self.values = load_settings()
        if self.values.get("lyrics") == "on":
            self.values["lyrics"] = "pill"
        self.values["glass"] = False                           # glass removed 3 Nov (broke the island)

    def get(self, k, default=None):
        return self.values.get(k, DEFAULTS.get(k, default))

    def set(self, k, v):
        """Validate against the schema, store, save. Returns True if the value changed."""
        it = schema_item(k)
        if it is None or it.get("state") or it["type"] == "button":
            return False
        if it["type"] == "bool":
            v = bool(v)
        elif it["type"] == "choice":
            if v not in [o[0] for o in it["opts"]]:
                return False
        if self.values.get(k) == v:
            return False
        self.values[k] = v
        self.save()
        return True

    def save(self):
        try:
            tmp = SETTINGS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.values, f, indent=1, ensure_ascii=False)
            os.replace(tmp, SETTINGS_FILE)
        except Exception as e:
            print("settings save failed:", e)


# --------------------------------------------------------------------------- heartbeat (for the watchdog)
def heartbeat():
    try:
        with open(HEARTBEAT, "w", encoding="utf-8") as f:
            f.write(f"{time.time():.0f}")
    except Exception:
        pass


def under_watchdog():
    return os.environ.get("ISLAND_WATCHDOG") == "1"


# --------------------------------------------------------------------------- housekeeping
BACKUP_FILES = [  # (source, name in the backup)   firebase-key.json is never copied
    ("island_config.json", "island_config.json"), ("island_state.json", "island_state.json"),
    ("island_settings.json", "island_settings.json"), ("inbox_ledger.json", "inbox_ledger.json"),
    ("qawi_foods_user.json", "qawi_foods_user.json"),
    (os.path.join("plans", "study.json"), "study_plan.json"),
    (os.path.join("plans", "health.json"), "health_plan.json"),
]


def _move_to_trash(path, sub):
    dest_dir = os.path.join(TRASH, sub)
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(path))
    if os.path.exists(dest):
        stem, ext = os.path.splitext(dest)
        dest = f"{stem}-{int(time.time())}{ext}"
    shutil.move(path, dest)


def weekly_backup(keep=8):
    os.makedirs(BACKUPS, exist_ok=True)
    days = sorted(d for d in os.listdir(BACKUPS) if os.path.isdir(os.path.join(BACKUPS, d)))
    today = dt.date.today()
    if days:
        try:
            last = dt.date.fromisoformat(days[-1][:10])
            if (today - last).days < 7:
                return None
        except ValueError:
            pass
    out = os.path.join(BACKUPS, today.isoformat())
    os.makedirs(out, exist_ok=True)
    n = 0
    for src, name in BACKUP_FILES:
        p = os.path.normpath(os.path.join(HERE, src))
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(out, name))
            n += 1
    days = sorted(d for d in os.listdir(BACKUPS) if os.path.isdir(os.path.join(BACKUPS, d)))
    for old in days[:-keep]:
        _move_to_trash(os.path.join(BACKUPS, old), "backups")
    print(f"housekeeping: weekly backup -> backups\\{today.isoformat()} ({n} files)")
    return out


def tidy_baks(days=30):
    """Move *.bak-* files older than `days` into _to_delete\\baks (never deletes)."""
    cut = time.time() - days * 86400
    moved = 0
    for name in os.listdir(HERE):
        p = os.path.join(HERE, name)
        if ".bak" in name and os.path.isfile(p) and os.path.getmtime(p) < cut:
            try:
                _move_to_trash(p, "baks")
                moved += 1
            except Exception as e:
                print("housekeeping: could not move", name, e)
    if moved:
        print(f"housekeeping: moved {moved} old .bak files to _to_delete\\baks")
    return moved


def housekeep(settings):
    try:
        if settings.get("backups"):
            weekly_backup()
        tidy_baks()
    except Exception as e:
        print("housekeeping:", e)


# --------------------------------------------------------------------------- memory trim (Windows)
def _own_child(p):
    try:
        return p.name().lower().startswith("qtwebengineprocess")
    except Exception:
        return False


def trim_memory():
    """EmptyWorkingSet on this process and its children (QtWebEngineProcess). Windows pages it back in
    when needed; Task Manager shows the real, smaller footprint while the island sits idle."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        import psutil
        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.OpenProcess.restype = ctypes.c_void_p
        me = psutil.Process()
        # only the island itself + its web renderer/GPU processes: apps it launched (Ollama keeps the brain's model
        # in RAM, opened apps, ...) are its children too, and emptying their working set just pages them out
        pids = [me.pid] + [c.pid for c in me.children(recursive=True) if _own_child(c)]
        freed, top = 0, ("", 0)
        for pid in pids:
            try:
                before = psutil.Process(pid).memory_info().rss
            except Exception:
                before = 0
            h = k32.OpenProcess(0x0100 | 0x0400, False, pid)     # SET_QUOTA | QUERY_INFORMATION
            if not h:
                continue
            try:
                psapi.EmptyWorkingSet(ctypes.c_void_p(h))
            finally:
                k32.CloseHandle(ctypes.c_void_p(h))
            try:
                got = max(0, before - psutil.Process(pid).memory_info().rss)
                freed += got
                if got > top[1]:
                    top = ("island" if pid == me.pid else psutil.Process(pid).name().replace(".exe", ""), got)
            except Exception:
                pass
        trim_memory.top = top          # the process that gave back the most (for the log line)
        return freed
    except Exception as e:
        print("ram trim:", e)
        return 0
