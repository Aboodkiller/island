"""Say a word out loud with Windows' own voices (offline): Mandarin (Huihui / Yaoyao / Kangkang) and German (Katja / Hedda / Stefan).

Only pronunciation talks; everything else on the island stays show-only. The voices come from Windows' speech packs
(Settings > Time & language > Speech, or install_voices() below with one UAC prompt). The newer "OneCore" voices that
those packs install aren't in the classic SAPI list, so both lists are searched.
"""
import os
import re
import sys
import threading

WIN = sys.platform == "win32"
LCIDS = {"zh": ("804", "404", "c04", "1004", "1404"), "de": ("407", "c07", "807", "1007", "1407")}
PREF = {"zh": ("huihui", "yaoyao", "xiaoxiao", "kangkang"), "de": ("katja", "hedda", "stefan")}
RATE = {"zh": -3, "de": -1}               # a little slower than normal: it's for learning
NAMES = {"zh": "Mandarin", "de": "German"}
CATS = (r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices",
        r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech\Voices")
CAPS = {"zh": "Language.TextToSpeech~~~zh-CN~0.0.1.0", "de": "Language.TextToSpeech~~~de-DE~0.0.1.0"}
CJK = re.compile(r"[㐀-鿿]+")
_lock = threading.Lock()


def _com(fn):
    """Run fn() with COM set up on this (worker) thread."""
    import comtypes
    comtypes.CoInitialize()
    try:
        return fn()
    finally:
        comtypes.CoUninitialize()


def _tokens():
    """[(token, description, [lcid, ...])] from the OneCore list first, then the classic one."""
    import comtypes.client as cc
    out, seen = [], set()
    for cat_id in CATS:
        try:
            cat = cc.CreateObject("SAPI.SpObjectTokenCategory")
            cat.SetId(cat_id, False)
            toks = cat.EnumerateTokens()
            for i in range(toks.Count):
                t = toks.Item(i)
                desc = str(t.GetDescription())
                if desc in seen:
                    continue
                seen.add(desc)
                try:
                    langs = [x.strip().lower() for x in str(t.GetAttribute("Language") or "").split(";")]
                except Exception:
                    langs = []
                out.append((t, desc, langs))
        except Exception:
            continue
    return out


def _pick(toks, lang):
    have = [x for x in toks if any(c in x[2] for c in LCIDS[lang])]
    for p in PREF[lang]:
        for x in have:
            if p in x[1].lower():
                return x
    return have[0] if have else None


def voices():
    """{'zh': 'Microsoft Huihui - Chinese (Simplified, PRC)' | None, 'de': ... | None}. Call from a worker thread."""
    if not WIN:
        return {"zh": None, "de": None}

    def go():
        toks = _tokens()
        out = {k: (_pick(toks, k) or (None, None))[1] for k in LCIDS}
        out["all"] = [f"{d} [{','.join(l)}]" for _, d, l in toks]      # for the log: proves the lists can be read at all
        return out
    try:
        return _com(go)
    except Exception as e:
        print("say: voices:", e)
        return {"zh": None, "de": None}


def text_for(lang, text):
    """What to actually say: Mandarin -> the characters only (a Chinese voice reads pinyin letters badly)."""
    if lang == "zh":
        return "".join(CJK.findall(str(text))) or str(text)
    return str(text)


def speak(lang, text):
    """Blocking (run it in a thread). -> 'ok' | 'missing' (no voice for that language) | 'error: ...'."""
    if not WIN:
        return "missing"
    say = text_for(lang, text).strip()
    if not say:
        return "error: nothing to say"

    def go():
        import comtypes.client as cc
        hit = _pick(_tokens(), lang)
        if not hit:
            return "missing"
        v = cc.CreateObject("SAPI.SpVoice")
        v.Voice = hit[0]
        v.Rate = RATE.get(lang, 0)
        v.Volume = 100
        v.Speak(say, 0)
        return "ok"
    with _lock:                         # one at a time, never over each other
        try:
            return _com(go)
        except Exception as e:
            return f"error: {e}"[:160]


def install_voices(langs, here):
    """Windows' speech packs for these languages, with one UAC prompt (hidden PowerShell). True if it started."""
    if not WIN:
        return False
    import ctypes
    log = os.path.join(here, "voices_install.log")
    caps = ",".join(f"'{CAPS[x]}'" for x in langs if x in CAPS)
    ps = (f"$l='{log}'; Get-Date | Out-File -Append $l; foreach($c in @({caps})){{ "
          f"try {{ Add-WindowsCapability -Online -Name $c -ErrorAction Stop | Out-String | Out-File -Append $l }} "
          f"catch {{ ('FAILED ' + $c + ': ' + $_) | Out-File -Append $l }} }}; 'done' | Out-File -Append $l")
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe",
                                            f'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command "{ps}"', here, 0)
    return r > 32


def open_settings():
    if WIN:
        try:
            os.startfile("ms-settings:speech")
        except Exception:
            pass
