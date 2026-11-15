"""
island_dev.py - dev bits (round 8): GitHub notifications + Netlify deploy status (tokens in secrets.json, or the gh
CLI if it's logged in), PC health (uptime, RAM pressure), and the phone page on home Wi-Fi.

secrets.json (island folder, never sent anywhere but the API it belongs to; keep it out of any git repo):
  {"github_token": "ghp_...", "netlify_token": "nfp_...", "netlify_site": "your-site.netlify.app"}
"""

import datetime as dt
import json
import os
import queue
import secrets as pysecrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(HERE, "secrets.json")
NO_WINDOW = 0x08000000
LAN_PORT = 47815


class _QuietServer(ThreadingHTTPServer):
    """A phone/tablet that drops its connection (screen off, Wi-Fi switch, page closed) is normal: no traceback."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def load_secrets():
    try:
        with open(SECRETS, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def uptime():
    try:
        import psutil
        boot = psutil.boot_time()
        up = time.time() - boot
        d, h, m = int(up // 86400), int(up % 86400 // 3600), int(up % 3600 // 60)
        return dict(text=(f"{d}d {h}h" if d else f"{h}h {m}m"),
                    since=dt.datetime.fromtimestamp(boot).strftime("%a %I:%M %p").replace(" 0", " "))
    except Exception:
        return None


def top_ram(n=3):
    try:
        import psutil
        procs = []
        for p in psutil.process_iter(["name", "memory_info"]):
            mi = p.info.get("memory_info")
            if mi:
                procs.append((mi.rss, p.info.get("name") or "?"))
        agg = {}
        for rss, name in procs:
            k = name.lower().replace(".exe", "")
            agg[k] = agg.get(k, 0) + rss
        return [(k, round(v / 2**30, 1)) for k, v in sorted(agg.items(), key=lambda x: -x[1])[:n]]
    except Exception:
        return []


class DevWatch(threading.Thread):
    """Polls GitHub notifications (3 min) and the newest Netlify deploy (1 min, 15 s while building)."""

    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.seen = set()
        self.first = True
        self.deploy = None
        self.gh_at = 0

    def _get(self, url, token):
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "island-widget",
                                                   "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    def github(self, sec):
        tok = sec.get("github_token")
        if tok:
            items = self._get("https://api.github.com/notifications", tok)
        else:
            gh = shutil.which("gh")
            if not gh:
                return None
            r = subprocess.run([gh, "api", "notifications"], capture_output=True, text=True, timeout=30,
                               creationflags=NO_WINDOW if os.name == "nt" else 0)
            if r.returncode != 0:
                return None
            items = json.loads(r.stdout or "[]")
        out = []
        for n in items:
            out.append(dict(id=str(n.get("id")), reason=n.get("reason", ""), title=(n.get("subject") or {}).get("title", ""),
                            kind=(n.get("subject") or {}).get("type", ""), repo=(n.get("repository") or {}).get("full_name", "")))
        new = [x for x in out if x["id"] not in self.seen]
        self.seen |= {x["id"] for x in out}
        if self.first:
            new, self.first = [], False
        return dict(unread=len(out), new=new[:5])

    def netlify(self, sec):
        tok = sec.get("netlify_token")
        if not tok:
            return None
        site = urllib.parse.quote(sec.get("netlify_site") or "", safe="")
        ds = self._get(f"https://api.netlify.com/api/v1/sites/{site}/deploys?per_page=1", tok)
        if not ds:
            return None
        d = ds[0]
        return dict(id=d.get("id"), state=d.get("state"), branch=d.get("branch"), title=(d.get("title") or "")[:80],
                    error=(d.get("error_message") or "")[:160], url=d.get("deploy_ssl_url") or d.get("ssl_url"))

    def run(self):
        time.sleep(30)
        errs = {}
        while True:
            sec = load_secrets()
            wait = 60
            if time.time() - self.gh_at >= 180:
                self.gh_at = time.time()
                try:
                    g = self.github(sec)
                    if g is not None:
                        self.emit(("github", g))
                    errs.pop("gh", None)
                except Exception as e:
                    if errs.get("gh") != str(e):
                        errs["gh"] = str(e)
                        print("github:", e)
            try:
                n = self.netlify(sec)
                if n:
                    if self.deploy is None or n["id"] != self.deploy.get("id") or n["state"] != self.deploy.get("state"):
                        self.emit(("netlify", dict(n, prev=(self.deploy or {}).get("state") if self.deploy and n["id"] == self.deploy.get("id") else None,
                                                   first=self.deploy is None)))
                    self.deploy = n
                    if n["state"] not in ("ready", "error", "rejected"):
                        wait = 15
                errs.pop("nf", None)
            except Exception as e:
                if errs.get("nf") != str(e):
                    errs["nf"] = str(e)
                    print("netlify:", e)
            time.sleep(wait)


# ============================================================== phone page on home Wi-Fi
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Island</title><meta name="theme-color" content="#07080b"><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#07080b;color:#f0f2f8;font:15px/1.4 -apple-system,system-ui,sans-serif;padding:16px 16px 40px}
h1{font-size:26px;margin:6px 0 2px;letter-spacing:-.02em}.mut{color:#8b93a7;font-size:13px}
.card{background:#14161c;border-radius:18px;padding:14px;margin-top:14px}
.row{display:flex;align-items:center;gap:10px}.sp{flex:1}
.bar{height:8px;border-radius:5px;background:#23262f;overflow:hidden;margin:10px 0}.bar b{display:block;height:100%;background:linear-gradient(90deg,#0ea5e9,#22d3ee)}
.chips{display:flex;gap:8px}.chips button{flex:1;padding:12px 0;border-radius:12px;border:0;background:#1d2029;color:#22d3ee;font-weight:700;font-size:15px}
.it{display:flex;align-items:center;gap:12px;padding:11px 4px;border-top:1px solid #1d2029}.it:first-child{border-top:0}
.ck{width:26px;height:26px;border-radius:9px;border:2px solid #3a3f4c;flex:none;display:grid;place-items:center;background:none;color:#07080b;font-weight:900}
.ck.on{background:#4ade80;border-color:#4ade80}.t{font-weight:600}.done .t{opacity:.45;text-decoration:line-through}
.tm{font-size:12px;color:#8b93a7;width:64px;flex:none}
</style></head><body>
<h1 id="h">Today</h1><div class="mut" id="s">loading…</div>
<div class="card"><div class="row"><b id="w">–</b><span class="sp"></span><span class="mut" id="wl"></span></div><div class="bar"><b id="wb" style="width:0"></b></div>
<div class="chips"><button onclick="water(250)">+250</button><button onclick="water(500)">+500</button><button onclick="water(1000)">+1 L</button></div></div>
<div class="card" id="plan"></div>
<script>
const K = new URLSearchParams(location.search).get("k") || "";
const api = (p, o) => fetch(p + (p.includes("?") ? "&" : "?") + "k=" + encodeURIComponent(K), o).then(r => r.json());
const t12 = s => { if (!s) return ""; let [h, m] = s.split(":").map(Number); return (h % 12 || 12) + ":" + String(m).padStart(2, "0") + (h >= 12 ? " pm" : " am"); };
function draw(d) {
  const t = d.today || {}; document.getElementById("h").textContent = (t.done_pct ?? 0) + "% today";
  document.getElementById("s").textContent = t.date || "";
  const w = t.water || {}; if (w.goal) { document.getElementById("w").textContent = (w.ml / 1000).toFixed(2) + " / " + (w.goal / 1000).toFixed(1) + " L";
    document.getElementById("wl").textContent = w.ml >= w.goal ? "goal hit ✓" : ((w.goal - w.ml) / 1000).toFixed(2) + " L to go";
    document.getElementById("wb").style.width = Math.min(100, w.ml / w.goal * 100) + "%"; }
  document.getElementById("plan").innerHTML = (t.plan || []).filter(i => i.key).map(i => `<div class="it ${i.done ? "done" : ""}"><span class="tm">${t12(i.time)}</span><span class="t sp">${i.title.replace(/[<&]/g, c => c === "<" ? "&lt;" : "&amp;")}</span>${i.tick ? `<button class="ck ${i.done ? "on" : ""}" onclick="tick('${i.key}')">${i.done ? "✓" : ""}</button>` : ""}</div>`).join("") || '<div class="mut">Nothing planned.</div>';
}
const load = () => api("/api/state").then(draw).catch(() => document.getElementById("s").textContent = "the island isn't reachable");
const water = ml => api("/api/water?ml=" + ml, {method: "POST"}).then(() => setTimeout(load, 800));
const tick = key => api("/api/tick?key=" + encodeURIComponent(key), {method: "POST"}).then(() => setTimeout(load, 600));
load(); setInterval(load, 15000);
</script></body></html>"""


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.168.1.1", 9))          # no packet is sent; just picks the LAN interface
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


PUBLIC = {"/deck-icon.png": "image/png", "/nosleep.mp4": "video/mp4", "/qrcode.js": "text/javascript"}


def lan_doctor(exe):
    """Why a phone might not reach the deck: the network type (Windows blocks incoming connections on Public networks)
    and Windows Firewall's rules for Python. Read-only: PowerShell Get-* commands only."""
    out = []
    ps = ("$n = @(Get-NetConnectionProfile | Select-Object Name,InterfaceAlias,@{n='Cat';e={\"$($_.NetworkCategory)\"}}); "
          "ConvertTo-Json -Compress -InputObject $n; '---'; "
          "$r = @(Get-NetFirewallRule -Direction Inbound -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -match 'python' } | "
          "Select-Object -First 10 DisplayName,@{n='On';e={\"$($_.Enabled)\"}},@{n='Act';e={\"$($_.Action)\"}},@{n='Prof';e={\"$($_.Profile)\"}},"
          "@{n='Prog';e={($_ | Get-NetFirewallApplicationFilter).Program}}); ConvertTo-Json -Compress -InputObject $r")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=120,
                           creationflags=0x08000000)
        net, _, fw = (r.stdout or "").partition("---")
        nets = json.loads(net.strip() or "[]")
        rules = json.loads(fw.strip() or "[]")
        rules = rules if isinstance(rules, list) else [rules]
        me = os.path.normcase(exe or "")
        pub_ok = any(os.path.normcase(str(x.get("Prog") or "")) == me and x.get("Act") == "Allow" and x.get("On") == "True"
                     and ("Public" in str(x.get("Prof")) or str(x.get("Prof")) == "Any") for x in rules)
        for x in nets if isinstance(nets, list) else [nets]:
            cat = x.get("Cat")
            note = ""
            if cat == "Public":
                note = " (fine: this Python is allowed on Public networks)" if pub_ok else " -> phones may be blocked: set this network to Private"
            out.append(f"network '{x.get('Name')}' ({x.get('InterfaceAlias')}) is {cat}{note}")
        for x in rules:
            prog = x.get("Prog") or "any program"
            mine = " (this Python)" if os.path.normcase(str(prog)) == me else ""
            out.append(f"firewall: {x.get('DisplayName')} · {'on' if x.get('On') == 'True' else 'off'} · {x.get('Act')} · {x.get('Prof')} · {prog}{mine}")
        if not rules:
            out.append("firewall: no inbound rule for Python yet (Windows asks once; if that was dismissed, phones are blocked)")
        elif not any(os.path.normcase(str(x.get("Prog") or "")) == me and x.get("Act") == "Allow" and x.get("On") == "True" for x in rules):
            out.append(f"firewall: nothing allows {exe} itself (the island's Python) -> the deck can be blocked")
    except Exception as e:
        out.append(f"couldn't check ({e})")
    return out


class LanServer:
    """The deck: http://<pc>:47815/deck?k=<token> on the home network (or http://localhost:47815/... over USB with
    adb reverse). Serves ui.html itself (same themes), streams the island's updates live (/api/events, server-sent
    events) and takes taps back (/api/do). The old /api/state + /api/water + /api/tick still work."""

    def __init__(self, token, emit):
        self.token, self.emit, self.srv = token, emit, None      # emit -> the island's main thread (a Qt signal)
        self.clients, self.lock = set(), threading.Lock()
        self.shares = {}                                          # one-time file links: id -> (path, expires)

    def share(self, path, minutes=15):
        """A file dropped on the island's Phone target: /f/<random id> serves it to the home network for a while."""
        now = time.time()
        self.shares = {k: v for k, v in self.shares.items() if v[1] > now}
        sid = pysecrets.token_urlsafe(12)
        self.shares[sid] = (path, now + minutes * 60)
        return sid

    def broadcast(self, fn, arg_json):
        """Send one island.<fn>(arg) call to every open deck. arg_json is already JSON text."""
        if not self.clients:
            return
        msg = '{"f":%s,"a":%s}' % (json.dumps(fn), arg_json if arg_json else "null")
        with self.lock:
            for q in list(self.clients):
                try:
                    q.put_nowait(msg)
                except queue.Full:                 # a phone that stopped reading: drop it, it reconnects by itself
                    self.clients.discard(q)

    def start(self):
        lan = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _ok(self, q):
                return pysecrets.compare_digest((q.get("k") or [""])[0], lan.token)

            def _send(self, code, body, ctype="application/json", cache=False):
                b = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text", "application/j", "application/m")) else ""))
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
                self.end_headers()
                self.wfile.write(b)

            def _file(self, path, ctype, cache=True):
                try:
                    with open(path, "rb") as f:
                        return self._send(200, f.read(), ctype, cache)
                except OSError:
                    return self._send(404, '{"error": "not found"}')

            def _share(self, sid):
                """The phone scanned the QR code: stream that one file (the random id is the key, it expires)."""
                ent = lan.shares.get(sid)
                if not ent or time.time() > ent[1] or not os.path.isfile(ent[0]):
                    return self._send(404, "<meta name=viewport content='width=device-width'><h3 style='font-family:sans-serif'>"
                                           "This link has expired. Drop the file on the island again.</h3>", "text/html")
                path = ent[0]
                name = os.path.basename(path)
                import mimetypes
                try:
                    size = os.path.getsize(path)
                    self.send_response(200)
                    self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(size))
                    self.send_header("Content-Disposition", "attachment; filename*=UTF-8''" + urllib.parse.quote(name))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with open(path, "rb") as f:
                        shutil.copyfileobj(f, self.wfile, 256 * 1024)
                    lan.emit(("lan_share_got", name))
                except (OSError, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def _events(self):
                """Server-sent events: one line per island update, a ping every 15 s keeps phones and routers awake."""
                cq = queue.Queue(maxsize=600)
                with lan.lock:
                    lan.clients.add(cq)
                lan.emit(("deck_n", len(lan.clients)))
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("X-Accel-Buffering", "no")
                    self.end_headers()
                    self.wfile.write(b"retry: 2000\n\n")
                    self.wfile.flush()
                    lan.emit(("deck_hello", None))            # the island sends everything once
                    while lan.srv is not None and cq in lan.clients:
                        try:
                            msgs = [cq.get(timeout=15)]
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        while len(msgs) < 60:
                            try:
                                msgs.append(cq.get_nowait())
                            except queue.Empty:
                                break
                        self.wfile.write("".join("data: " + m + "\n\n" for m in msgs).encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass
                finally:
                    with lan.lock:
                        lan.clients.discard(cq)
                    lan.emit(("deck_n", len(lan.clients)))

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if u.path in PUBLIC:                       # icon, keep-awake clip, QR lib: nothing private in them
                    return self._file(os.path.join(HERE, u.path[1:]), PUBLIC[u.path])
                if u.path.startswith("/fonts/") and u.path.endswith(".woff2") and "/" not in u.path[7:] and ".." not in u.path:
                    return self._file(os.path.join(HERE, "fonts", u.path[7:]), "font/woff2")
                if u.path == "/favicon.ico":
                    return self._file(os.path.join(HERE, "deck-icon.png"), "image/png")
                if u.path.startswith("/f/") and 8 <= len(u.path) <= 40:
                    return self._share(u.path[3:])
                if not self._ok(q):
                    lan.seen = getattr(lan, "seen", 0) + 1
                    if lan.seen <= 30:
                        print(f"deck: refused {self.client_address[0]} {u.path} ({'wrong' if q.get('k') else 'no'} key: open the link / QR from the island again)")
                    return self._send(403, '{"error": "wrong or missing key - open the link from the island again"}')
                if u.path in ("/", "/deck"):
                    lan.seen = getattr(lan, "seen", 0) + 1
                    if lan.seen <= 30:
                        print(f"deck: page opened by {self.client_address[0]}")
                if u.path in ("/", "/deck"):
                    return self._file(os.path.join(HERE, "ui.html"), "text/html", cache=False)
                if u.path == "/api/events":
                    return self._events()
                if u.path == "/manifest.webmanifest":
                    k = urllib.parse.quote(lan.token)
                    m = dict(name="Island Deck", short_name="Island", start_url=f"/deck?k={k}", scope="/", display="fullscreen",
                             orientation="any", background_color="#07080b", theme_color="#07080b",
                             icons=[dict(src="/deck-icon.png", sizes="512x512", type="image/png", purpose="any")])
                    return self._send(200, json.dumps(m), "application/manifest+json")
                if u.path == "/api/state":
                    try:
                        with open(os.path.join(HERE, "today.json"), encoding="utf-8") as f:
                            t = json.load(f)
                    except Exception:
                        t = {}
                    return self._send(200, json.dumps(dict(today=t), ensure_ascii=False))
                self._send(404, '{"error": "not found"}')

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if not self._ok(q):
                    return self._send(403, '{"error": "wrong or missing key"}')
                if u.path == "/api/water":
                    try:
                        ml = int((q.get("ml") or ["0"])[0])
                    except ValueError:
                        ml = 0
                    if 0 < abs(ml) <= 2000:
                        lan.emit(("lan_water", ml))
                        return self._send(200, '{"ok": true}')
                if u.path == "/api/tick":
                    key = (q.get("key") or [""])[0]
                    if key and len(key) < 200:
                        lan.emit(("lan_tick", key))
                        return self._send(200, '{"ok": true}')
                if u.path == "/api/do":                    # a tap on the deck: {"name": ..., "arg": ...}
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        j = json.loads(self.rfile.read(n).decode("utf-8")) if 0 < n <= 65536 else None
                    except (ValueError, UnicodeDecodeError):
                        j = None
                    if isinstance(j, dict) and isinstance(j.get("name"), str) and len(j["name"]) < 40:
                        lan.emit(("deck_do", dict(name=j["name"], arg=j.get("arg"))))
                        return self._send(200, '{"ok": true}')
                self._send(400, '{"error": "bad request"}')

        try:
            self.srv = _QuietServer(("0.0.0.0", LAN_PORT), H)
            self.srv.daemon_threads = True
        except OSError as e:
            print(f"phone page: can't listen on {LAN_PORT}:", e)
            return False
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        print(f"deck: http://{lan_ip()}:{LAN_PORT}/deck?k=…")
        return True

    def stop(self):
        if self.srv:
            srv, self.srv = self.srv, None             # open event streams see srv=None and end
            try:
                srv.shutdown()
                srv.server_close()
            except Exception:
                pass
        with self.lock:
            self.clients.clear()


def adb_path():
    """Android platform-tools' adb, if it's installed (PATH or the Android SDK folder)."""
    p = shutil.which("adb")
    if p:
        return p
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ANDROID_HOME", ""), os.environ.get("ANDROID_SDK_ROOT", "")):
        for sub in (r"Android\Sdk\platform-tools\adb.exe", r"platform-tools\adb.exe"):
            c = os.path.join(base, sub) if base else ""
            if c and os.path.exists(c):
                return c
    return None


def adb_reverse():
    """Tablet on USB: its localhost:47815 becomes the PC's (no Wi-Fi needed). Returns (ok, message)."""
    adb = adb_path()
    if not adb:
        return False, "adb not found (Android platform-tools)"
    kw = dict(capture_output=True, text=True, timeout=12, creationflags=NO_WINDOW if os.name == "nt" else 0)
    try:
        devs = [ln.split()[0] for ln in subprocess.run([adb, "devices"], **kw).stdout.splitlines()[1:]
                if ln.strip().endswith("device")]
        if not devs:
            return False, "no tablet on USB (USB debugging on + allow this PC)"
        bad = []
        for d in devs:
            r = subprocess.run([adb, "-s", d, "reverse", f"tcp:{LAN_PORT}", f"tcp:{LAN_PORT}"], **kw)
            if r.returncode != 0:
                bad.append((r.stderr or r.stdout).strip()[:80])
        return (not bad), ("linked " + ", ".join(devs)) if not bad else bad[0]
    except Exception as e:
        return False, str(e)[:80]
