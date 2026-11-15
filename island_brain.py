"""The island's "brain": a small language model in Ollama, on this PC and offline.

The voice / command bar try their own rules first; whatever they don't know comes here. The model either turns it into
ONE island command or gives a short answer (shown, never spoken). Ollama keeps the model loaded for a few minutes after
use, then frees the memory. While a game is in front it runs on the CPU so the RTX 4060's memory stays with the game.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("ISLAND_OLLAMA", "http://127.0.0.1:11434")    # later: the laptop as a home server on the LAN
MODELS = {"fast": "qwen3.5:2b", "smart": "qwen3.5:4b"}
SIZES = {"fast": "~2.7 GB", "smart": "~3.4 GB"}
KEEP = "10m"            # how long Ollama keeps it loaded after the last question
CTX = 2048
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SCHEMA = {"type": "object", "properties": {"cmd": {"type": "string"}, "say": {"type": "string"}}, "required": ["cmd", "say"]}

SYSTEM = """You are Island, the assistant inside a desktop widget on the user's Windows PC.
Reply ONLY with JSON: {"cmd": "...", "say": "..."}
- If they want the widget to DO something: exactly one command from the list in "cmd", "say" empty.
- If they ask a question: "cmd" empty, answer in "say" in at most 2 short sentences. Plain text, no markdown, no emojis.
  Use the [context] line when it helps (time, today's plan, water, music).
- Answer in the language they used (English or Arabic).
- Unclear or impossible: both empty.
Commands (exact forms):
timer <25 | 90s | 1h30> [label] ; stopwatch ; stop (clears all timers)
water <ml> ; @water (how much water today)
note <text> ; todo <text>
open <app, website or show> ; open <show> next ep
vol <0-100> ; @vol up ; @vol down ; @mute
@media next ; @media prev ; @media pause ; @media play ; @media like
focus <physics|maths|biology|history> ; focus stop
swap <subject> <subject> ; move <subject> <7pm>
@whatsnext ; @macros ; lock ; screens off ; chat <subject>
@sayword (pronounce today's word of the day out loud) ; @say zh <Chinese characters> ; @say de <German words>
  (with @say, put the word, its pinyin and the meaning in "say")
Examples:
"put some music on" -> {"cmd": "@media play", "say": ""}
"remind me to call mom in 20 minutes" -> {"cmd": "timer 20 call mom", "say": ""}
"I just drank a big glass" -> {"cmd": "water 350", "say": ""}
"der die or das Tisch" -> {"cmd": "", "say": "der Tisch (the table)."}
"what's 15 percent of 240" -> {"cmd": "", "say": "36."}
"how do you say good morning in Chinese" -> {"cmd": "@say zh 早上好", "say": "早上好 zǎoshang hǎo · good morning"}"""


def _call(path, body=None, timeout=5):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(URL + path, data=data, method="GET" if body is None else "POST",
                                 headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def _json(path, body=None, timeout=5):
    with _call(path, body, timeout) as r:
        return json.loads(r.read() or b"{}")


def up(timeout=0.8):
    """Ollama's version if its server answers, else None."""
    try:
        return _json("/api/version", timeout=timeout).get("version") or "?"
    except Exception:
        return None


def models(timeout=1.5):
    """Installed chat models ([] = none), or None when the server isn't running."""
    try:
        tags = [m["name"] for m in _json("/api/tags", timeout=timeout).get("models", [])]
        return [t for t in tags if "embed" not in t]
    except Exception:
        return None


def exe():
    p = shutil.which("ollama")
    if p:
        return p
    c = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Ollama", "ollama.exe")
    return c if os.path.isfile(c) else None


def start():
    """Start Ollama's tray app (it runs the server and starts with Windows); plain `ollama serve` if there's no app."""
    e = exe()
    if not e:
        return False
    app = os.path.join(os.path.dirname(e), "ollama app.exe")
    try:
        if os.path.isfile(app):
            subprocess.Popen([app], creationflags=getattr(subprocess, "DETACHED_PROCESS", 0), close_fds=True)
        else:
            subprocess.Popen([e, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
                             creationflags=NOWIN | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return True
    except Exception:
        return False


def wait_up(secs):
    end = time.time() + secs
    while time.time() < end:
        if up():
            return True
        time.sleep(1)
    return False


def opts(game=False):
    o = dict(num_ctx=CTX, temperature=0)
    if game:
        o["num_gpu"] = 0        # a game is in front: CPU only, the GPU's memory stays with the game
    return o


def warm(model, game=False, timeout=90):
    """Load the model now (an empty request), so the answer is quick when the words arrive."""
    try:
        _json("/api/generate", dict(model=model, keep_alive=KEEP, options=opts(game)), timeout)
        return True
    except Exception:
        return False


def unload(model):
    try:
        _json("/api/generate", dict(model=model, keep_alive=0), 10)
    except Exception:
        pass


def parse(raw):
    """Model reply -> (cmd, say). Tolerates code fences or extra text around the JSON."""
    raw = re.sub(r"^\s*```(?:json)?|```\s*$", "", str(raw or "")).strip()
    try:
        d = json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.S)
        try:
            d = json.loads(m.group(0)) if m else {}
        except Exception:
            d = {}
    if not isinstance(d, dict):
        d = {}
    cmd = " ".join(str(d.get("cmd") or "").split()).strip(" `\"'")
    say = " ".join(str(d.get("say") or "").split())
    say = re.sub(r"\*\*|__|`|^#+\s*", "", say)
    if cmd.upper() in ("NONE", "NULL", "N/A"):
        cmd = ""
    return cmd[:120], say[:300]


def ask(model, text, context="", game=False, timeout=45):
    """-> (cmd, say). Raises on connection / server errors."""
    body = dict(model=model, stream=False, keep_alive=KEEP, format=SCHEMA, think=False, options=opts(game),
                messages=[dict(role="system", content=SYSTEM),
                          dict(role="user", content=(f"[context: {context}]\n" if context else "") + str(text))])
    try:
        out = _json("/api/chat", body, timeout)
    except urllib.error.HTTPError as e:
        if e.code != 400:
            raise
        body.pop("think", None)            # an older Ollama / a model without a thinking switch
        out = _json("/api/chat", body, timeout)
    return parse((out.get("message") or {}).get("content", ""))


# ------------------------------------------------------------------ one-button setup
def gb(n):
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _winget(say, log):
    wg = shutil.which("winget")
    if not wg:
        log("no winget")
        return False
    cmd = [wg, "install", "-e", "--id", "Ollama.Ollama", "--source", "winget", "--silent",
           "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
    log("$ " + " ".join(cmd))
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", bufsize=1, creationflags=NOWIN)
        last = 0
        for line in p.stdout:              # text mode turns winget's \r progress redraws into lines
            line = line.strip()
            if len(line) <= 2:
                continue                   # spinner
            m = re.search(r"([\d.]+\s*[KMG]B)\s*/\s*([\d.]+\s*[KMG]B)", line)
            if m:
                if time.time() - last > 0.5:
                    last = time.time()
                    say(f"Downloading Ollama · {m.group(1)} / {m.group(2)}")
                continue
            log(line)
            if re.search(r"install", line, re.I):
                say("Installing Ollama…")
        log(f"winget exit {p.wait(timeout=1800)}")
    except Exception as ex:
        log(f"winget: {ex}")
    return exe() is not None               # exit codes vary ("already installed" isn't 0): ollama.exe is what counts


def _direct(say, log):
    url = "https://ollama.com/download/OllamaSetup.exe"
    dst = os.path.join(os.environ.get("TEMP") or os.path.expanduser("~"), "OllamaSetup.exe")
    log(f"download {url}")
    try:
        with urllib.request.urlopen(url, timeout=60) as r, open(dst, "wb") as f:
            total, got, last = int(r.headers.get("Content-Length") or 0), 0, 0
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                f.write(b)
                got += len(b)
                if time.time() - last > 0.5:
                    last = time.time()
                    say(f"Downloading Ollama · {gb(got)}" + (f" / {gb(total)}" if total else ""))
        say("Installing Ollama…")
        rc = subprocess.run([dst, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-"], creationflags=NOWIN,
                            timeout=900).returncode
        log(f"setup exit {rc}")
    except Exception as ex:
        log(f"direct: {ex}")
    return exe() is not None


def pull(model, say, log):
    say(f"Downloading {model}…")
    log(f"pull {model}")
    try:
        with _call("/api/pull", dict(model=model, stream=True), timeout=180) as r:
            last = 0
            for raw in r:
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("error"):
                    log("pull error: " + str(d["error"]))
                    return False, str(d["error"])[:120]
                st, tot, done = d.get("status", ""), d.get("total") or 0, d.get("completed") or 0
                if st == "success":
                    log("pull ok")
                    return True, ""
                if tot > 50e6:
                    if time.time() - last > 0.5:
                        last = time.time()
                        say(f"Downloading the brain · {gb(done)} / {gb(tot)}")
                elif st and not st.startswith("pulling"):
                    say(st[:1].upper() + st[1:])
        return False, "the download stopped"
    except Exception as ex:
        log(f"pull: {ex}")
        return False, str(ex)[:120]


def install(model, say, logf):
    """Ollama (if missing) -> running -> model downloaded -> loaded. -> (ok, message). Progress goes to say()."""
    def log(s):
        logf.write(f"{time.strftime('%I:%M:%S %p')} {s}\n")
        logf.flush()
    if not exe() and not up():
        say("Installing Ollama…")
        if not (_winget(say, log) or _direct(say, log)):
            return False, "Ollama didn't install"
    if not up():
        say("Starting Ollama…")
        start()
        if not wait_up(90):
            return False, "Ollama didn't start"
    log(f"ollama {up()}")
    have = models() or []
    if model not in have:
        ok, msg = pull(model, say, log)
        if not ok:
            return False, msg
    say("Loading the brain…")
    warm(model)
    return True, ""
