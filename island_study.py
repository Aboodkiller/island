"""
island_study.py - the island's study tools (round 4): focus sessions on study blocks (50 + 10), the distraction
guard, study minutes per subject, exam countdowns, mock exams, the past-papers tracker and subject-chat buttons.

Files (island folder, the island's own - the tawjihi plan files are never touched):
  study.json      exams {name: "YYYY-MM-DD"}, chats {cat: url}, papers {id: {score, date}}  (edit freely)
  study_log.json  {"YYYY-MM-DD": {"P": seconds, ...}, ...}   focus time per subject (minus time in a game)
"""

import datetime as dt
import json
import ntpath
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
STUDY_FILE = os.path.join(HERE, "study.json")
LOG_FILE = os.path.join(HERE, "study_log.json")

SUBJ = {"P": "Physics", "MA": "Maths", "B": "Biology", "HI": "History"}
ALIAS = {"p": "P", "phy": "P", "physics": "P", "فيزياء": "P", "m": "MA", "ma": "MA", "math": "MA", "maths": "MA",
         "رياضيات": "MA", "b": "B", "bio": "B", "biology": "B", "احياء": "B", "h": "HI", "hi": "HI", "hist": "HI",
         "history": "HI", "تاريخ": "HI"}
EXAM_MIN = {"P": 150, "MA": 150, "B": 120, "HI": 90}          # real ministry lengths (tawjihi-plan.md)
FOCUS_S, BREAK_S = 50 * 60, 10 * 60

# his past papers (new curriculum) as listed in tawjihi-plan.md; "mock" = saved for the timed mock phase
PAPERS = []   # (id, cat, label, saved_for_mocks) - your past papers, if you want the papers grid
MISSING = {"P26S", "M26S1", "M26S2", "B26S"}   # not downloaded yet (summer 2026, new curriculum)

DEFAULT_CHATS = {}   # {cat: "https://claude.ai/chat/..."} - optional links to your subject chats

GAME_EXES = {"valorant-win64-shipping.exe", "valorant.exe", "overwatch.exe", "eldenring.exe", "start_protected_game.exe",
             "minecraft.windows.exe", "fortniteclient-win64-shipping.exe", "cs2.exe", "r5apex.exe", "rocketleague.exe",
             "gta5.exe", "gta5_enhanced.exe", "robloxplayerbeta.exe", "league of legends.exe", "eafc25.exe", "eafc26.exe",
             "rdr2.exe", "cod.exe", "marvel-win64-shipping.exe"}
GAME_NAMES = {"valorant-win64-shipping.exe": "Valorant", "valorant.exe": "Valorant", "overwatch.exe": "Overwatch",
              "eldenring.exe": "Elden Ring", "start_protected_game.exe": "Elden Ring", "minecraft.windows.exe": "Minecraft",
              "javaw.exe": "Minecraft", "fortniteclient-win64-shipping.exe": "Fortnite", "cs2.exe": "CS2",
              "r5apex.exe": "Apex", "rocketleague.exe": "Rocket League", "robloxplayerbeta.exe": "Roblox",
              "league of legends.exe": "League"}


NOT_GAMES = {"wallpaper engine", "steamvr", "soundpad", "lossless scaling", "obs studio", "fpsvr", "voicemeeter"}


def game_name(exe_path, title):
    """A game's display name if this foreground window is a game, else None."""
    p = (exe_path or "").lower()
    exe = ntpath.basename(p)
    if exe == "javaw.exe" or exe == "java.exe":
        return "Minecraft" if (title or "").lower().startswith("minecraft") else None   # not his server console
    if exe in GAME_EXES:
        return GAME_NAMES.get(exe) or exe.rsplit(".", 1)[0]
    for root in ("\\steamapps\\common\\", "\\epic games\\", "\\riot games\\", "\\xboxgames\\"):
        if root in p and "launcher" not in exe and "steam.exe" != exe:
            name = p.split(root, 1)[1].split("\\", 1)[0]
            if name in NOT_GAMES:
                return None
            return name.title()[:24]
    return None


def subject(word):
    w = (word or "").strip().lower()
    return ALIAS.get(w) or ALIAS.get(w.rstrip("s"))


def parse_date(text, today):
    """'14 dec', 'dec 14', '14/12', '2026-12-14' -> date (this year, or next year if already past)."""
    t = text.strip().lower()
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", t)
    if m:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    months = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    m = re.match(r"^(\d{1,2})\s*([a-z]{3})[a-z]*$", t) or re.match(r"^([a-z]{3})[a-z]*\s*(\d{1,2})$", t)
    if m:
        a, b = m.groups()
        day, mon = (int(a), b) if a.isdigit() else (int(b), a)
        if mon not in months:
            return None
        d = dt.date(today.year, months.index(mon) + 1, day)
    else:
        m = re.match(r"^(\d{1,2})[/.](\d{1,2})$", t)
        if not m:
            return None
        d = dt.date(today.year, int(m[2]), int(m[1]))
    return d if d >= today - dt.timedelta(days=30) else d.replace(year=d.year + 1)


class Study:
    def __init__(self, exam_day):
        self.exam_day = exam_day
        self.cfg = self._load(STUDY_FILE, {})
        self.cfg.setdefault("exams", {})
        self.cfg.setdefault("chats", dict(DEFAULT_CHATS))
        self.cfg.setdefault("papers", {})
        if not os.path.exists(STUDY_FILE):
            self.save()
        self.log = self._load(LOG_FILE, {})
        self.dirty = False

    @staticmethod
    def _load(p, default):
        try:
            with open(p, encoding="utf-8") as f:
                v = json.load(f)
            return v if isinstance(v, dict) else default
        except Exception:
            return default

    @staticmethod
    def _write(p, obj):
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)

    def save(self):
        self._write(STUDY_FILE, self.cfg)

    def save_log(self):
        if self.dirty:
            self.dirty = False
            self._write(LOG_FILE, self.log)

    def add_secs(self, day, cat, secs):
        d = self.log.setdefault(day.isoformat(), {})
        d[cat] = round(d.get(cat, 0) + secs, 1)
        self.dirty = True

    def note(self, day, key):
        d = self.log.setdefault(day.isoformat(), {})
        d[key] = d.get(key, 0) + 1
        self.dirty = True

    def week(self, today):
        """Minutes per subject this week (Sun-Sat) + today."""
        start = today - dt.timedelta(days=(today.weekday() + 1) % 7)
        wk = {c: 0.0 for c in SUBJ}
        for i in range(7):
            d = self.log.get((start + dt.timedelta(days=i)).isoformat(), {})
            for c in SUBJ:
                wk[c] += d.get(c, 0) / 60
        td = self.log.get(today.isoformat(), {})
        return dict(week={c: round(v) for c, v in wk.items()}, today={c: round(td.get(c, 0) / 60) for c in SUBJ},
                    caught=td.get("caught", 0))

    def exams(self, today):
        """Only the exams you've set (command bar: exam physics 14 dec). Past ones drop off after a day."""
        out = []
        for name, v in self.cfg["exams"].items():
            try:
                d = dt.date.fromisoformat(v)
            except (TypeError, ValueError):
                continue
            if (d - today).days < -1:
                continue
            cat = subject(name.split()[0]) or "R"
            out.append(dict(cat=cat, name=name, days=(d - today).days, date=d.strftime("%a %d %b").replace(" 0", " "),
                            tbc=False))
        out.sort(key=lambda x: (x["days"], x["tbc"]))
        return out

    def set_exam(self, name, d):
        self.cfg["exams"][name] = d.isoformat()
        self.save()

    def papers(self):
        sc = self.cfg["papers"]
        out = []
        for pid, cat, label, mock in PAPERS:
            s = sc.get(pid) or {}
            out.append(dict(id=pid, cat=cat, label=label, mock=mock, missing=pid in MISSING and not s,
                            score=s.get("score"), date=s.get("date")))
        return out

    def set_score(self, pid, score, today):
        self.cfg["papers"][pid] = dict(score=score, date=today.isoformat())
        self.save()

    def find_paper(self, text):
        t = text.strip().upper().replace(" ", "")
        ids = [p[0] for p in PAPERS]
        return t if t in ids else None


# ============================================================== review cards (formulas + your mistakes)
CARDS_FILE = os.path.join(HERE, "cards.json")
LEITNER_DAYS = {1: 1, 2: 2, 3: 7}          # box 1 daily, box 2 every 2nd day, box 3 weekly (his rule)
MISTAKE_DAYS = [1, 3, 7]                   # a logged mistake comes back after 1, 3 and 7 days, then it's cleared

# (id, cat, front, back, starting box) - starting boxes follow claude/tawjihi-boxes.md on 3 Nov
SEED = []     # (id, cat, front, back, box) - starter flash cards; add your own with: wrong physics question = answer


class Cards:
    def __init__(self):
        self.data = Study._load(CARDS_FILE, {})
        cards = self.data.setdefault("cards", [])
        have = {c.get("id") for c in cards}
        today = dt.date.today()
        added = 0
        for i, (cid, cat, front, back, box) in enumerate(SEED):
            if cid in have:
                continue
            due = today + dt.timedelta(days=(i % LEITNER_DAYS[box]) if box > 1 else 0)   # spread the first week
            cards.append(dict(id=cid, cat=cat, front=front, back=back, box=box, due=due.isoformat(), kind="formula"))
            added += 1
        if added or not os.path.exists(CARDS_FILE):
            self.save()

    def save(self):
        Study._write(CARDS_FILE, self.data)

    def due(self, today):
        t = today.isoformat()
        out = [c for c in self.data["cards"] if not c.get("done") and c.get("due", t) <= t]
        out.sort(key=lambda c: (c.get("box", 1), c.get("kind") != "mistake", c.get("due", "")))   # box 1 + mistakes first
        return out

    def answer(self, cid, ok, today):
        c = next((x for x in self.data["cards"] if x.get("id") == cid), None)
        if not c:
            return None
        c["last"] = today.isoformat()
        if c.get("kind") == "mistake":
            step = c.get("step", 0)
            if ok:
                step += 1
                if step >= len(MISTAKE_DAYS):
                    c["done"] = today.isoformat()
                else:
                    c["due"] = (today + dt.timedelta(days=MISTAKE_DAYS[step])).isoformat()
            else:
                step = 0
                c["due"] = (today + dt.timedelta(days=MISTAKE_DAYS[0])).isoformat()
            c["step"] = step
        else:
            c["box"] = min(3, c.get("box", 1) + 1) if ok else 1
            c["due"] = (today + dt.timedelta(days=LEITNER_DAYS[c["box"]])).isoformat()
        c["right" if ok else "wrong"] = c.get("right" if ok else "wrong", 0) + 1
        self.save()
        return c

    def add_mistake(self, cat, front, back, today):
        n = sum(1 for c in self.data["cards"] if c.get("kind") == "mistake") + 1
        c = dict(id=f"E{n:03d}", cat=cat, front=front[:240], back=back[:240], kind="mistake", step=0,
                 due=(today + dt.timedelta(days=MISTAKE_DAYS[0])).isoformat(), added=today.isoformat())
        self.data["cards"].append(c)
        self.save()
        return c

    def summary(self, today):
        d = self.due(today)
        boxes = {1: 0, 2: 0, 3: 0}
        for c in self.data["cards"]:
            if c.get("kind") == "formula":
                boxes[c.get("box", 1)] = boxes.get(c.get("box", 1), 0) + 1
        open_m = sum(1 for c in self.data["cards"] if c.get("kind") == "mistake" and not c.get("done"))
        return dict(n=len(d), due=[{k: c.get(k) for k in ("id", "cat", "front", "back", "kind", "box")} for c in d[:40]],
                    boxes=boxes, mistakes=open_m)


# ============================================================== site blocker (admin once)
BLOCK_SITES = ["youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com", "instagram.com",
               "www.instagram.com", "tiktok.com", "www.tiktok.com", "m.tiktok.com"]
FLAG_FILE = os.path.join(HERE, "block.flag")
INSTALLED_FILE = os.path.join(HERE, "blocker_installed.txt")

BLOCKER_PS1 = r"""# Island site blocker (runs as SYSTEM every minute; installed by setup_blocker.cmd)
# Adds/removes only lines ending in '# island-block' in the hosts file, following the island's block.flag.
param([switch]$Off)
$hosts = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$flag = '__FLAG__'
$sites = @(__SITES__)
$on = $false
if (-not $Off -and (Test-Path -LiteralPath $flag)) {
    $raw = [string](Get-Content -LiteralPath $flag -Raw -ErrorAction SilentlyContinue)
    $parts = $raw.Trim() -split '\s+'
    if ($parts.Count -ge 2 -and $parts[0] -eq 'on') {
        $until = [long]0
        if ([long]::TryParse($parts[1], [ref]$until)) {
            if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -lt $until) { $on = $true }
        }
    }
}
$lines = @(Get-Content -LiteralPath $hosts -ErrorAction SilentlyContinue)
$keep = @($lines | Where-Object { $_ -notmatch '#\s*island-block\s*$' })
$want = $keep
if ($on) {
    $add = @(foreach ($s in $sites) { "0.0.0.0 $s # island-block"; ":: $s # island-block" })
    $want = @($keep) + $add
}
if (($lines -join "`n") -ne ($want -join "`n")) {
    Set-Content -LiteralPath $hosts -Value $want -Encoding ASCII
    ipconfig /flushdns | Out-Null
}
"""

SETUP_CMD = r"""@echo off
rem Island site blocker - one-time setup. Needs admin (the island asks Windows for it).
rem Remove it again:  setup_blocker.cmd remove   (in an admin cmd)
net session >nul 2>&1 || (echo Please run this as administrator. & timeout /t 6 >nul & exit /b 1)
set "DIR=%ProgramData%\IslandBlocker"
if /i "%~1"=="remove" (
  schtasks /Delete /TN IslandSiteBlocker /F
  if exist "%DIR%\blocker.ps1" powershell -NoProfile -ExecutionPolicy Bypass -File "%DIR%\blocker.ps1" -Off
  del "%~dp0blocker_installed.txt" 2>nul
  echo Site blocker removed. & timeout /t 4 >nul & exit /b 0
)
if not exist "%DIR%" mkdir "%DIR%"
copy /Y "%~dp0blocker.ps1" "%DIR%\blocker.ps1" >nul || (echo Copy failed. & timeout /t 8 >nul & exit /b 1)
icacls "%DIR%" /inheritance:r /grant:r *S-1-5-32-544:(OI)(CI)F *S-1-5-18:(OI)(CI)F *S-1-5-32-545:(OI)(CI)RX >nul
schtasks /Create /TN IslandSiteBlocker /SC MINUTE /MO 1 /RU SYSTEM /RL HIGHEST /F /TR "powershell.exe -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File %DIR%\blocker.ps1" >nul || (echo Could not create the task. & timeout /t 8 >nul & exit /b 1)
echo ok> "%~dp0blocker_installed.txt"
echo Site blocker installed. It only blocks during study blocks when the island's setting is on.
timeout /t 5 >nul
"""


def write_blocker_files():
    """(Re)write blocker.ps1 + setup_blocker.cmd next to the island (setup copies the ps1 into ProgramData)."""
    ps1 = BLOCKER_PS1.replace("__FLAG__", FLAG_FILE.replace("'", "''")).replace(
        "__SITES__", ",".join(f"'{x}'" for x in BLOCK_SITES))
    for name, body in (("blocker.ps1", ps1), ("setup_blocker.cmd", SETUP_CMD)):
        pth = os.path.join(HERE, name)
        old = open(pth, encoding="utf-8", newline="").read() if os.path.exists(pth) else None
        if old != body.replace("\n", "\r\n"):
            with open(pth, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(body)


def blocker_installed():
    return os.path.exists(INSTALLED_FILE)


def set_block(until_epoch):
    """until_epoch = int -> block until then; None -> unblock."""
    want = f"on {int(until_epoch)}" if until_epoch else "off"
    try:
        old = open(FLAG_FILE, encoding="utf-8").read().strip() if os.path.exists(FLAG_FILE) else None
        if old != want:
            with open(FLAG_FILE, "w", encoding="utf-8") as f:
                f.write(want)
            return True
    except OSError as e:
        print("block.flag:", e)
    return False
