"""
island_voice.py - the island's "Siri": hold a key (or say "Hey Island"), talk, the island does it.

  natural(text)       plain English / Arabic -> the island's own commands ("set a timer for 10 minutes" -> "timer 10")
  history_find(q)     your browser history (Chrome / Edge / Brave), newest first, for "open supernatural next ep"
  next_episode(url)   ".../episode-12" -> ".../episode-13" (also s05e12, ep12, ?ep=12, .../12)
  Voice               microphone + speech-to-text on this PC (faster-whisper), hold-to-talk + wake word (Vosk)

Nothing here sends audio anywhere: the speech models run locally. The only network use is the one-time
model download (Hugging Face for whisper, alphacephei.com for the small wake-word model).
Needs (Settings > Voice > Install, or by hand):  pip install faster-whisper sounddevice vosk
"""

import atexit
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
VOSK_DIR = os.path.join(MODELS, "vosk-model-small-en-us-0.15")
WAKE_WORDS = ("hey island", "hi island", "okay island", "ok island")
PIP_PKGS = ["faster-whisper", "sounddevice", "vosk"]
# PortAudio crashes the whole island (0xC0000005 / 0xC0000374, 6 Nov) when the wake-word stream closes on one thread
# while hold-to-talk opens its stream on another, so streams are only ever opened / closed under this lock.
_PA = threading.Lock()

# --------------------------------------------------------------------------- words -> island commands
AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")
NUM_WORDS = {"zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
             "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
             "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
             "hundred": 100, "half": 0.5, "quarter": 0.25,
             "واحد": 1, "دقيقة": 1, "اثنين": 2, "دقيقتين": 2, "ثلاث": 3, "ثلاثة": 3, "اربع": 4, "أربع": 4, "خمس": 5, "خمسة": 5,
             "ست": 6, "سبع": 7, "ثمان": 8, "تسع": 9, "عشر": 10, "عشرة": 10, "عشرين": 20, "ثلاثين": 30, "اربعين": 40,
             "أربعين": 40, "خمسين": 50, "ساعة": 60, "نص": 0.5, "نصف": 0.5}
AR_APPS = {"ديسكورد": "discord", "سبوتيفاي": "spotify", "واتساب": "whatsapp", "واتس": "whatsapp", "يوتيوب": "youtube",
           "كروم": "chrome", "ستيم": "steam", "فالورانت": "valorant", "كلود": "claude", "ماينكرافت": "minecraft",
           "نتفلكس": "netflix", "انستغرام": "instagram", "انستا": "instagram", "الحلقة الجاية": "next ep"}
SUBJECTS = r"(physics|phy|maths?|math|bio(?:logy)?|history|فيزياء|رياضيات|احياء|أحياء|تاريخ)"


def words_to_numbers(s):
    """'twenty five' -> '25', 'one hundred' -> '100', Arabic digits -> 0-9 (time words like 'half' stay words)."""
    s = s.translate(AR_DIGITS)
    skip = ("a", "an", "half", "quarter", "نص", "نصف", "ساعة", "دقيقة", "دقيقتين")
    out, acc = [], None
    for w in s.split():
        k = w.strip(",.!?")
        v = NUM_WORDS.get(k)
        if v is not None and k not in skip:
            if acc is not None and acc >= 20 and acc % 10 == 0 and v < 10:
                acc += v
            elif acc is not None and v == 100:
                acc *= 100
            else:
                if acc is not None:
                    out.append(str(int(acc)))
                acc = v
            continue
        if acc is not None:
            out.append(str(int(acc)))
            acc = None
        out.append(w)
    if acc is not None:
        out.append(str(int(acc)))
    return " ".join(out)


def _dur(text):
    """'10 minutes' / 'an hour and a half' / '90 seconds' / 'ربع ساعة' -> island timer text ('10', '1h30', '90s')."""
    t = text.lower()
    if re.search(r"\b(an?|one) hour and a half\b|ساعة ونص|ساعة و نص", t):
        return "1h30"
    if re.search(r"\bhalf (an )?hour\b|نص ساعة|نصف ساعة", t):
        return "30"
    if re.search(r"\bquarter (of an )?hour\b|ربع ساعة", t):
        return "15"
    if re.search(r"\b(an?|one) hour\b|^ساعة$|\bساعة\b", t) and not re.search(r"\d", t):
        return "1h"
    d = re.search(r"(\d*\.\d+)\s*(hours?|hrs?|h|ساعات|ساعة|minutes?|mins?|m|دقائق|دقيقة|دقايق)\b", t)
    if d:                                   # '1.5 hours' used to become 'timer 5h' (the 5 after the dot)
        sec = round(float(d.group(1)) * (3600 if re.match(r"h|ساع", d.group(2)) else 60))
        hh, rem = divmod(sec, 3600)
        if sec > 0 and rem % 60 == 0:
            return f"{hh}h{rem // 60:02d}" if hh and rem else (f"{hh}h" if hh else str(rem // 60))
        if sec > 0:
            return f"{sec}s"
    h = re.search(r"(\d+)\s*(?:hours?|hrs?|h|ساعات|ساعة)\b", t)
    m = re.search(r"(\d+)\s*(?:minutes?|mins?|m|دقائق|دقيقة|دقايق)\b", t)
    s = re.search(r"(\d+)\s*(?:seconds?|secs?|s|ثانية|ثواني)\b", t)
    if h and m:
        return f"{h.group(1)}h{int(m.group(1)):02d}"
    if h:
        return f"{h.group(1)}h"
    if m:
        return m.group(1)
    if s:
        return f"{s.group(1)}s"
    n = re.search(r"\b(\d{1,3})\b", t)
    return n.group(1) if n else None


PRAYER_N = {"fajr": "fajr", "dhuhr": "dhuhr", "duhr": "dhuhr", "zuhr": "dhuhr", "asr": "asr", "maghrib": "maghrib", "isha": "isha",
            "الفجر": "fajr", "الظهر": "dhuhr", "العصر": "asr", "المغرب": "maghrib", "العشاء": "isha"}


CJK = re.compile(r"[\u3400-\u9fff]+")


def natural(text):
    """Plain speech (or a typed sentence) -> one island command. Unknown -> the text unchanged."""
    raw = " ".join(str(text or "").split())
    t = words_to_numbers(raw.lower()).strip(" .!?،")
    t = re.sub(r"^(hey|hi|ok|okay)\s+island[,\s]*", "", t)
    t = re.sub(r"^(please|can you|could you|i want to|i wanna|let's|lets)\s+", "", t)
    t = re.sub(r"\s+(please|for me|now)$", "", t)
    if not t:
        return ""
    # timers
    if re.search(r"\b(timer|countdown|remind me in|alarm in)\b|مؤقت|تايمر|منبه", t):
        d = _dur(t)
        lab = re.search(r"\b(?:for|called|named)\s+([a-z][a-z ]{1,24})$", t)
        label = lab.group(1) if lab and not re.search(r"\d|minute|hour|second", lab.group(1)) else ""
        if d:
            return f"timer {d}" + (f" {label}" if label else "")
    if re.search(r"\bstop ?watch\b|ساعة توقيت", t):
        return "stopwatch"
    if re.fullmatch(r"(stop|cancel|clear) (the )?(timers?|countdowns?)|الغي المؤقت|وقف المؤقت", t):
        return "stop"
    if re.search(r"\bhow much water\b|\bwater (left|so far|status)\b|كم شربت|قديش شربت", t):
        return "@water"
    if re.search(r"\bhow (?:much|many) (?:protein|calories|kcal)\b|\b(?:protein|calories|kcal|macros) (?:left|so far|today|status)\b"
                 r"|\bmy macros\b|كم بروتين|قديش بروتين|كم سعرة|كم كالوري", t):
        return "@macros"
    # water
    if re.search(r"\bwater\b|\bdrank\b|مي\b|مية|ماء|شربت", t):
        ml = None
        mm = re.search(r"(\d{2,4})\s*(?:ml|mils?|milliliters?|مل)\b", t)
        ll = re.search(r"(\d+(?:\.\d+)?)\s*(?:l|liters?|litres?|لتر)\b", t)
        if mm:
            ml = int(mm.group(1))
        elif ll:
            ml = int(float(ll.group(1)) * 1000)
        elif re.search(r"half (a )?(liter|litre)|نص لتر", t):
            ml = 500
        elif re.search(r"\b(a|one) (liter|litre)\b|لتر", t):
            ml = 1000
        elif re.search(r"\b(a )?(glass|cup)\b|كاسة|كوب", t):
            ml = 250
        elif re.search(r"\b(a )?bottle\b|قنينة|علبة", t):
            ml = 500
        else:
            n = re.search(r"\b(\d{2,4})\b", t)
            ml = int(n.group(1)) if n else None
        if ml:
            return f"water {ml}"
    # music
    if re.fullmatch(r"(next|skip)( (song|track|one))?|skip (this|it)|play the next (song|one)|الأغنية الجاية|اللي بعدها|غيرها|بعدها", t):
        return "@media next"
    if re.fullmatch(r"(previous|last|go back)( (song|track|one))?|play (that|it) again|الأغنية اللي قبل|اللي قبلها|ارجع", t):
        return "@media prev"
    if re.fullmatch(r"(pause|stop)( (the )?(music|song|it|spotify))?|وقف( الأغنية| الموسيقى)?|اسكت", t):
        return "@media pause"
    if re.fullmatch(r"(play|resume|continue)( (the )?(music|song|it|spotify))?|شغل( الأغنية| الموسيقى)?|كمل", t):
        return "@media play"
    if re.search(r"\b(like|love|save) (this|the) (song|track)\b|عجبتني", t):
        return "@media like"
    m = re.search(r"\b(?:volume|vol|sound)\b(?: to| at)? (\d{1,3})|الصوت (\d{1,3})", t)
    if m:
        return f"vol {m.group(1) or m.group(2)}"
    if re.search(r"\b(turn it|volume) up\b|louder|علي الصوت|ارفع الصوت", t):
        return "@vol up"
    if re.search(r"\b(turn it|volume) down\b|quieter|lower|وطي الصوت|نزل الصوت", t):
        return "@vol down"
    if re.fullmatch(r"(mute|unmute)( (the )?(mic|microphone))?|سكر المايك|افتح المايك", t):
        return "mic" if re.search(r"mic|مايك", t) else "@mute"
    if re.search(r"\bbrightness\b.*?(\d{1,3})|الإضاءة (\d{1,3})", t):
        m = re.search(r"(\d{1,3})", t)
        return f"bright {m.group(1)}"
    # study / plan
    m = re.search(r"\b(?:swap|switch)\s+(\w+)\s+(?:and|with)\s+(\w+)", t)
    if m:
        return f"swap {m.group(1)} {m.group(2)}"
    m = re.search(r"\bmove\s+(\w+(?:\s\d)?)\s+to\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\d{1,2}\s*(?:a\.m\.|p\.m\.))", t)
    if m:
        return f"move {m.group(1)} {m.group(2).replace('.', '')}"
    if re.search(r"\b(stop|end|cancel) (the )?focus\b", t):
        return "focus stop"
    m = re.search(r"\b(?:start|begin)?\s*(?:a\s+)?focus(?:ing)?(?: on| session)?\s*" + SUBJECTS + r"?$", t)
    if m and "focus" in t:
        return "focus" + (f" {m.group(1)}" if m.group(1) else "")
    m = re.search(r"\b(?:study|studying)\s+" + SUBJECTS + r"\b", t)
    if m:
        return f"focus {m.group(1)}"
    m = re.search(r"\b(?:next prayer|prayer times?|when(?:'s| is)(?: the)? (?:next )?(?:prayer|adhan|athan|(fajr|dhuhr|duhr|zuhr|asr|maghrib|isha)))\b"
                  r"|(?:متى|إمتى|امتى|قديش ل) ?(?:الأذان|الاذان|الصلاة|(الفجر|الظهر|العصر|المغرب|العشاء))", t)
    if m:                                     # -> HUD with the time (12-hour) and how long until it
        n = PRAYER_N.get(m.group(1) or m.group(2) or "", "")
        return "@prayer" + (f" {n}" if n else "")
    if CJK.search(raw) and re.search(r"pronounce|\bsay\b|\bread\b|كيف|لفظ|نطق|انطق", t):
        return "@say zh " + "".join(CJK.findall(raw))           # "how do you pronounce 你好"
    if re.fullmatch(r"(?:how (?:do (?:you|i)|to|would you) )?(?:say|pronounce|read)(?: (?:it|that|this|the word|today'?s word|"
                    r"the word of the day|today'?s (?:german|mandarin|chinese) word|the (?:german|mandarin|chinese) word))?"
                    r"(?: out loud)?|pronunciation|(?:what does|how does) (?:it|the word|today'?s word) sound(?: like)?", t) \
            or re.search(r"كيف (?:ألفظ|الفظ|انطق|أنطق|تلفظ|بتلفظ|بنلفظ|بنقول|نقول)|لفظ الكلمة|انطق الكلمة|اقرأ الكلمة", t):
        return "@sayword"                                          # the 🔊 on the word of the day, by voice
    if re.fullmatch(r"how many days(?: are)?(?: left)?(?: (?:until|till|to|for|before) (?:the )?(?:tawjihi|exams?|exam day))?"
                    r"|how much time(?: is)?(?: left)? (?:until|till|to|before) (?:the )?(?:tawjihi|exams?|exam day)"
                    r"|(?:days|time) (?:left )?(?:to|until|till) (?:the )?(?:tawjihi|exams?)|tawjihi countdown"
                    r"|كم يوم (?:ضايل|باقي|ظل|بقي)(?: (?:للتوجيهي|عالتوجيهي|للامتحان|عالامتحان))?|قديش (?:ضايل|باقي) (?:للتوجيهي|عالتوجيهي)", t):
        return "@exam"                                             # HUD: days to Tawjihi + when the mocks start
    if re.search(r"\bscreen ?time\b|\bhow long (?:have i been|was i|am i) (?:on|at|using) (?:the |my )?(?:pc|computer|laptop|screen)\b"
                 r"|كم صرلي (?:عالجهاز|على الجهاز|عالكمبيوتر|عاللابتوب)", t):
        return "@screen"                                           # HUD: today's screen time + top 2 apps
    if re.fullmatch(r"(what'?s|what is) (next|up next|my next thing|left( today)?)|what do i have (now|next|today)|شو الجاي|شو بعدين|شو عندي", t):
        return "@whatsnext"
    m = re.match(r"^(?:(?:hey|ok|okay) island[,\s]*)?(?:please\s+)?(?:take a note|note|remember|write down|سجل)(?: that)?[:,]?\s+(.+)$", raw, re.I)
    if m:
        return "note " + m.group(1).strip(" .")
    m = re.match(r"^(?:(?:hey|ok|okay) island[,\s]*)?(?:add )?(?:a )?(?:todo|to do|to-do)[:,]?\s+(.+)$", raw, re.I)
    if m:
        return "todo " + m.group(1).strip(" .")
    # the PC
    if re.fullmatch(r"lock( the)?( pc| computer| screen)?|قفل الجهاز|اقفل الجهاز", t):
        return "lock"
    if re.fullmatch(r"(turn )?(the )?(screens?|monitors?|displays?) off|turn off the (screens?|monitors?)|طفي الشاشات?", t):
        return "screens off"
    if re.search(r"\bclipboard\b|الحافظة", t):
        return "clip"
    # open anything (apps, sites, the next episode)
    m = re.match(r"^(?:open|launch|start|run|go to|put on|watch|continue|resume|افتح|شغل)\s+(.+)$", t)
    if m:
        what = m.group(1)
        for ar, en in AR_APPS.items():
            what = what.replace(ar, en)
        return "open " + what
    return raw


# --------------------------------------------------------------------------- browser history ("open supernatural next ep")
def _browsers():
    """(name, History db path, exe) for every browser profile on this PC, newest-used first."""
    la, ra = os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")
    pf, pf86 = os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    roots = [("Chrome", os.path.join(la, "Google", "Chrome", "User Data"),
              [os.path.join(pf, r"Google\Chrome\Application\chrome.exe"), os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
               os.path.join(la, r"Google\Chrome\Application\chrome.exe")]),
             ("Edge", os.path.join(la, "Microsoft", "Edge", "User Data"),
              [os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"), os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe")]),
             ("Brave", os.path.join(la, "BraveSoftware", "Brave-Browser", "User Data"),
              [os.path.join(pf, r"BraveSoftware\Brave-Browser\Application\brave.exe")]),
             ("Opera GX", os.path.join(ra, "Opera Software", "Opera GX Stable"), [os.path.join(la, r"Programs\Opera GX\opera.exe")])]
    out = []
    for name, root, exes in roots:
        exe = next((e for e in exes if os.path.isfile(e)), None)
        if not os.path.isdir(root):
            continue
        dbs = [os.path.join(root, "History")] + [os.path.join(root, d, "History") for d in os.listdir(root)
                                                 if d == "Default" or d.startswith("Profile ")]
        for db in dbs:
            if os.path.isfile(db):
                out.append((name, db, exe, os.path.getmtime(db)))
    out.sort(key=lambda x: -x[3])
    return [x[:3] for x in out]


def history_find(query, limit=30, browsers=None):
    """Pages whose title or address has every word you said, newest first: [(url, title, when, browser, exe)].
    Reads a copy of the History file (the browser keeps the real one open); nothing leaves this PC."""
    words = [w for w in re.split(r"[\s\-_]+", query.lower()) if len(w) > 1]
    if not words:
        return []
    hits = []
    for name, db, exe in (browsers if browsers is not None else _browsers()):
        tmp = os.path.join(tempfile.gettempdir(), f"island_hist_{os.getpid()}.db")
        try:
            shutil.copy2(db, tmp)
            con = sqlite3.connect(tmp)
            where = " AND ".join(["(lower(title) LIKE ? OR lower(url) LIKE ?)"] * len(words))
            args = []
            for w in words:
                args += [f"%{w}%", f"%{w}%"]
            rows = con.execute(f"SELECT url, title, last_visit_time FROM urls WHERE {where} AND url LIKE 'http%' "
                               f"ORDER BY last_visit_time DESC LIMIT ?", args + [limit]).fetchall()
            con.close()
            for url, title, lv in rows:
                when = dt.datetime(1601, 1, 1) + dt.timedelta(microseconds=lv or 0)
                hits.append((url, title or "", when, name, exe))
        except Exception as e:
            print("history:", name, e)
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    hits.sort(key=lambda x: x[2], reverse=True)
    return hits


EP_PATTERNS = [
    re.compile(r"(?i)(s\d{1,2}[-_ ]?e)(\d{1,4})"),                         # s05e12
    re.compile(r"(?i)((?:episode|episodio|folge)[-_ /=]?)(\d{1,4})"),        # episode-12
    re.compile(r"(?i)(\bep[-_ /=]?)(\d{1,4})\b"),                            # ep-12 / ep12
    re.compile(r"(?i)([?&](?:ep|e|episode|eps)=)(\d{1,4})"),                 # ?ep=12
    re.compile(r"(?i)(-e)(\d{1,4})(?=[/?#.-]|$)"),                           # -e12
    re.compile(r"(/)(\d{1,4})(?=/?(?:[?#].*)?$)"),                           # .../12
]


def next_episode(url, step=1):
    """The address of the next (or step-th) episode, keeping zero padding; None when there's no episode number."""
    for rx in EP_PATTERNS:
        ms = list(rx.finditer(url))
        if not ms:
            continue
        m = ms[-1]
        num = m.group(2)
        n = int(num) + step
        if n < 1:
            return None
        new = str(n).zfill(len(num)) if num.startswith("0") else str(n)
        return url[:m.start(2)] + new + url[m.end(2):]
    return None


def episode_of(text):
    """'Supernatural Season 5 Episode 12' / 's05e12' -> (5, 12); (None, 12); None."""
    t = text or ""
    m = re.search(r"(?i)s(\d{1,2})[-_ ]?e(\d{1,4})", t)
    if m:
        return int(m.group(1)), int(m.group(2))
    s = re.search(r"(?i)season[-_ ]?(\d{1,2})", t)
    e = re.search(r"(?i)(?:episode|\bep)[-_ ]?(\d{1,4})", t)
    if e:
        return (int(s.group(1)) if s else None, int(e.group(1)))
    return None


def plan_open(query, history=None):
    """'supernatural next ep' -> ('url', url, why) | ('search', query, why) | None.
    Looks the show / site up in your browser history; "next" moves one episode on."""
    q = " ".join(query.lower().split())
    want_next = bool(re.search(r"\b(next|following)\s*(ep|episode|eps|one|part)?\b|الحلقة الجاية|اللي بعدها", q))
    m = re.search(r"\b(?:episode|ep)\s*(\d{1,4})\b", q)
    want_ep = int(m.group(1)) if m else None
    name = re.sub(r"\b(the\s+)?(next|following|last|latest)\s*(ep|episode|eps|one|part)?\b|\b(?:episode|ep)\s*\d{1,4}\b|"
                  r"\bwhere i (left|stopped)( off)?\b|\bfrom where i (left|stopped)\b|\b(of|on|in)\s*$|الحلقة الجاية|اللي بعدها", " ", q)
    name = " ".join(name.split()).strip(" -")
    if not name:
        return None
    hist = history if history is not None else history_find(name)
    hist = [h for h in hist if not re.search(r"(google|bing|duckduckgo|yahoo)\.[a-z.]+/(search|\?q=)|/search\?", h[0])]
    if not hist:
        return None
    url, title = hist[0][0], hist[0][1]
    # prefer the newest page that looks like an episode of it
    for h in hist[:15]:
        if next_episode(h[0]) or episode_of(h[1]):
            url, title = h[0], h[1]
            break
    if want_ep is not None:
        cur = episode_of(title) or episode_of(url)
        if cur:
            nu = next_episode(url, want_ep - cur[1])
            if nu:
                return ("url", nu, f"episode {want_ep}")
    if want_next:
        nu = next_episode(url)
        if nu:
            return ("url", nu, "next episode")
        return ("url", url, "where you left off (couldn't find the next one)")
    return ("url", url, "where you left off")


# --------------------------------------------------------------------------- the voice itself
def voice_ready():
    """(ok, what's missing)"""
    import importlib.util
    miss = []
    for mod in ("faster_whisper", "sounddevice", "numpy"):       # find_spec: checks without importing (fast, UI thread)
        try:
            if importlib.util.find_spec(mod) is None:
                miss.append(mod)
        except Exception:
            miss.append(mod)
    return (not miss), miss


class Voice:
    """Microphone -> text. listen_start()/listen_stop() for hold-to-talk; wake word runs in its own thread.
    emit(event) gets: ("level", 0..1), ("state", "listening"|"thinking"|"idle"|"error", text), ("heard", text), ("wake",)."""

    SR = 16000

    def __init__(self, emit, model_size="base", log=print):
        self.emit, self.model_size, self.log = emit, model_size, log
        self.model = None
        self.rec, self.frames, self.stream = False, [], None
        self.lock = threading.Lock()
        self.wake_on, self.wake_thread, self.wake_pause, self.game_pause = False, None, False, False
        self._load_t = None
        self.auto, self.heard_at, self.t0 = False, 0.0, 0.0
        self.wake_idle = threading.Event()          # set while the wake-word loop has no mic stream open
        self.wake_idle.set()
        self._exit_hooked = False

    def _hook_exit(self):
        """Call right after importing sounddevice: atexit runs last-in first-out, so close() runs before sounddevice's own
        exit hook shuts PortAudio down (that shutdown under a wake-word read crashed the 4:14 AM restart on 7 Nov)."""
        if not self._exit_hooked:
            self._exit_hooked = True
            atexit.register(self.close)

    def close(self):
        """Stop the mic streams before Python exits."""
        self.wake_on = False
        try:
            self.listen_stop(cancel=True)
        except Exception:
            pass
        t = self.wake_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(1.5)

    # whisper loads in the background the first time (download on the very first use)
    def warm(self):
        if self.model is not None or (self._load_t and self._load_t.is_alive()):
            return
        def load():
            try:
                from faster_whisper import WhisperModel
                t0 = time.time()
                self.model = WhisperModel(self.model_size, device="cpu", compute_type="int8",
                                          download_root=os.path.join(MODELS, "whisper"))
                self.log(f"voice: whisper {self.model_size} ready in {time.time() - t0:.1f} s")
                self.emit(("ready",))
            except Exception as e:
                self.log("voice: whisper didn't load:", e)
                self.emit(("state", "error", "Voice model didn't load: " + str(e)[:80]))
        self._load_t = threading.Thread(target=load, daemon=True)
        self._load_t.start()

    def listen_start(self, auto=False):
        """auto=True (wake word / mic button): stops by itself after you finish talking."""
        with self.lock:
            if self.rec:
                return
            try:
                import numpy as np
                import sounddevice as sd
                self._hook_exit()
            except Exception as e:
                self.emit(("state", "error", "Voice isn't installed yet (Settings > Voice)"))
                self.log("voice:", e)
                return
            self.warm()
            self.frames, self.rec, self.t0 = [], True, time.time()
            self.wake_pause, self.auto, self.heard_at = True, auto, 0.0
            self.wake_idle.wait(0.5)                    # let the wake-word stream close first (~0.1 s)

            def cb(indata, frames, tinfo, status):
                if not self.rec:
                    return
                self.frames.append(indata.copy())
                lvl = float(np.sqrt(np.mean((indata.astype(np.float32) / 32768.0) ** 2)))
                self.emit(("level", min(1.0, lvl * 9)))
                if lvl > 0.02:
                    self.heard_at = time.time()
            try:
                with _PA:
                    self.stream = sd.InputStream(samplerate=self.SR, channels=1, dtype="int16", blocksize=800, callback=cb)
                    self.stream.start()
                self.emit(("state", "listening", ""))
                if auto:
                    threading.Thread(target=self._auto_stop, daemon=True).start()
            except Exception as e:
                self.rec = False
                self.wake_pause = False
                self.emit(("state", "error", "No microphone: " + str(e)[:60]))

    def _auto_stop(self):
        """Wake word / mic button: stop ~1.1 s after you stop talking (or 6 s of nothing, 15 s at most)."""
        while self.rec:
            time.sleep(0.1)
            now = time.time()
            if (self.heard_at and now - self.heard_at > 1.1) or (not self.heard_at and now - self.t0 > 6) or now - self.t0 > 15:
                self.listen_stop(cancel=not self.heard_at)
                return

    def listen_stop(self, cancel=False):
        with self.lock:
            if not self.rec:
                return
            self.rec = False
            try:
                with _PA:
                    self.stream.stop()
                    self.stream.close()
            except Exception:
                pass
            self.stream = None
            frames, self.frames = self.frames, []
        if cancel or not frames:
            self.wake_pause = False
            self.emit(("state", "idle", ""))
            return
        threading.Thread(target=self._transcribe, args=(frames,), daemon=True).start()

    def _transcribe(self, frames):
        try:
            import numpy as np
            audio = np.concatenate(frames)[:, 0].astype(np.float32) / 32768.0
            if len(audio) < self.SR * 0.35:
                self.emit(("state", "idle", ""))
                return
            self.emit(("state", "thinking", ""))
            if self._load_t:
                self._load_t.join(timeout=120)
            if self.model is None:
                self.emit(("state", "error", "Voice model isn't ready yet (first time it downloads ~150 MB)"))
                return
            t0 = time.time()
            kw = dict(beam_size=1, vad_filter=True, without_timestamps=True, condition_on_previous_text=False,
                      initial_prompt="Island, timer, water, Spotify, physics, maths, biology, focus, Supernatural.")
            segs, info = self.model.transcribe(audio, **kw)
            lang = getattr(info, "language", "") or ""
            if lang not in ("en", "ar"):
                # short clips get detected as Polish / Norwegian ... ("Zatajmy.", 6 Nov): he only speaks English or Arabic.
                # Detection runs before any decoding, so re-running with the likelier of the two costs very little.
                probs = dict(getattr(info, "all_language_probs", None) or [])
                lang = "ar" if probs.get("ar", 0) > probs.get("en", 0) else "en"
                segs, info = self.model.transcribe(audio, language=lang, **kw)
            text = " ".join(s.text.strip() for s in segs).strip()
            self.log(f"voice: heard {text!r} ({lang or '?'}, {time.time() - t0:.1f} s)")
            if text:
                self.emit(("heard", text))
            else:
                self.emit(("state", "idle", ""))
        except Exception as e:
            self.log("voice: transcribe failed:", e)
            self.emit(("state", "error", "Didn't catch that"))
        finally:
            self.wake_pause = False

    # ---------------- "Hey Island" (Vosk, a 40 MB English model, only listens for the wake phrase)
    def wake_start(self):
        if self.wake_on:
            return
        self.wake_on = True
        self.wake_thread = threading.Thread(target=self._wake_loop, daemon=True)
        self.wake_thread.start()

    def wake_stop(self):
        self.wake_on = False

    def _wake_loop(self):
        try:
            import sounddevice as sd
            from vosk import KaldiRecognizer, Model, SetLogLevel
            SetLogLevel(-1)
            self._hook_exit()
        except Exception as e:
            self.log("voice: wake word needs vosk + sounddevice:", e)
            self.wake_on = False
            return
        if not os.path.isdir(VOSK_DIR):
            try:
                self.log("voice: downloading the wake-word model (40 MB)…")
                os.makedirs(MODELS, exist_ok=True)
                import urllib.request
                zp = os.path.join(MODELS, "vosk-small.zip")
                urllib.request.urlretrieve(VOSK_URL, zp)
                with zipfile.ZipFile(zp) as z:
                    z.extractall(MODELS)
                os.remove(zp)
            except Exception as e:
                self.log("voice: wake-word model download failed:", e)
                self.wake_on = False
                return
        model = Model(VOSK_DIR)
        grammar = json.dumps(list(WAKE_WORDS) + ["island", "[unk]"])
        rec = KaldiRecognizer(model, self.SR, grammar)
        self.log("voice: listening for \"Hey Island\"")
        while self.wake_on:
            if self.wake_pause or self.game_pause:
                time.sleep(0.3)
                rec.Reset()
                continue
            st, err = None, None
            self.wake_idle.clear()
            try:
                with _PA:
                    st = sd.RawInputStream(samplerate=self.SR, blocksize=1600, dtype="int16", channels=1)
                    st.start()
                while self.wake_on and not self.wake_pause and not self.game_pause:
                    data, _ = st.read(1600)            # 0.1 s chunks: hold-to-talk never waits long for the mic
                    if rec.AcceptWaveform(bytes(data)):
                        txt = json.loads(rec.Result()).get("text", "")
                        if any(w in txt for w in WAKE_WORDS):
                            self.log("voice: wake word")
                            self.wake_pause = True
                            self.emit(("wake",))
            except Exception as e:
                err = e
            finally:
                if st is not None:
                    try:
                        with _PA:
                            st.stop()
                            st.close()
                    except Exception:
                        pass
                self.wake_idle.set()
            if err is not None:
                self.log("voice: wake mic:", err)
                time.sleep(3)


class HoldKey(threading.Thread):
    """Hold-to-talk: watches one key (Right Ctrl by default, or a mouse side button) ~30 times a second.
    Held 0.25 s on its own -> ("key", "down"); let go -> ("key", "up"). It only reads the key, never presses anything,
    and a combo (Ctrl+C with the right Ctrl ...) cancels it."""

    def __init__(self, vk, emit):
        super().__init__(daemon=True)
        self.vk, self.emit, self.on = vk, emit, True

    def stop(self):
        self.on = False

    def run(self):
        import ctypes
        gks = ctypes.windll.user32.GetAsyncKeyState
        others = [k for k in range(0x08, 0xFF) if k not in (self.vk, 0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x01, 0x02, 0x04, 0x05, 0x06)]
        down_at, live, spoiled = None, False, False
        while self.on:
            time.sleep(0.03)
            held = bool(gks(self.vk) & 0x8000)
            if held:
                if down_at is None:
                    down_at, spoiled = time.time(), False
                if not spoiled and any(gks(k) & 0x8000 for k in others):
                    spoiled = True                      # a shortcut, not push-to-talk
                    if live:
                        live = False
                        self.emit(("key", "cancel"))
                if not live and not spoiled and time.time() - down_at >= 0.25:
                    live = True
                    self.emit(("key", "down"))
            else:
                if live:
                    self.emit(("key", "up"))
                down_at, live = None, False
