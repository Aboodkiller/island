"""
island_bridge.py - tiny local endpoint for the Spicetify extension (spicetify\\Extensions\\island-bridge.js).

Spotify (with Spicetify) POSTs to http://127.0.0.1:47812/spotify:
  {"type": "song", "uri", "title", "artist", "album", "dur", "lines": [[seconds, "text"], ...] | null, "sync", "colors"}
  {"type": "pos", "uri", "title", "pos": seconds, "playing": bool}       about once a second + on play/pause/seek
  {"type": "state", "uri", "title", "liked", "shuffle", "repeat": 0 off / 1 all / 2 one, "next": [{"t", "a"}]}

Commands go back the other way by long-poll: the extension keeps a GET open on http://127.0.0.1:47813/cmd and gets
{"c": "like" | "shuffle" | "repeat", "t": unix seconds} (or 204 after ~20 s of nothing). That server never touches Qt.
Deck extras: {"c": "search", "q", "id"} -> the extension POSTs {"type": "search", "id", "q", "items": [{"uri", "t", "a",
"img", "dur"}], "error"?}; {"c": "play" | "queue", "uri": "spotify:track:..."} plays it now / adds it to the queue.
Spotify Connect: {"type": "devices", "items": [{"id", "name", "type", "active", "local"}]}; "pos"/"state" carry
"remote" (playing on another device) + "dev"; commands "devices", "transfer" {id, local}, "toggle", "next", "prev",
"seek" {pos}, "vol" {v} act on whichever device is playing.
Library (deck): command "lib" -> {"type": "lib", "items": [{"uri", "name", "img", "type", "by", "pin"}], "liked": n};
"tracks" {uri, offset} -> {"type": "tracks", "uri", "offset", "total", "name", "img", "items": [{"uri", "t", "a", "img", "dur"}]};
"playctx" {ctx, uri?, shuffle?} plays a song inside its playlist. "pos"/"state" carry "v" (the extension's version) and
"state" carries "ctx" (the playlist / album playing).

Only listens on 127.0.0.1 (this PC). Bodies over 600 KB or of another shape are refused.
"""

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

PORT = 47812


class Bridge:
    def __init__(self, emit):
        self.emit = emit
        self.srv = None
        self.known = None           # uri of the last song Spotify told us about
        self.cmds = queue.Queue()
        self.gen = 0                # newest waiting poll; older ones (a reloaded Spotify) step aside
        self.poll_at = 0.0

    def listening(self):
        return time.time() - self.poll_at < 40

    def send_cmd(self, c, **extra):
        self.cmds.put(dict(extra, c=c, t=time.time()))

    def start(self):
        emit, bridge = self.emit, self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _cors(self):
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")

            def do_OPTIONS(self):
                self.send_response(204)
                self._cors()
                self.end_headers()

            def do_POST(self):
                code = 400
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if self.path == "/spotify" and 0 < n <= 600_000:
                        j = json.loads(self.rfile.read(n).decode("utf-8"))
                        if isinstance(j, dict) and j.get("type") in ("song", "pos", "state", "search", "devices", "lib", "tracks", "hi"):
                            if j["type"] == "song":
                                bridge.known = j.get("uri")
                            emit(j)
                            # 205 = "I don't know this song yet" (island restarted): the extension sends it again
                            code = 205 if j["type"] == "pos" and j.get("uri") and j.get("uri") != bridge.known else 204
                except Exception:
                    code = 400
                self.send_response(code)
                self._cors()
                self.end_headers()

        try:
            self.srv = HTTPServer(("127.0.0.1", PORT), Handler)   # one thread: Qt signals only from a long-lived thread
        except OSError as e:
            print(f"spotify bridge: can't listen on {PORT}:", e)
            return False
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        print(f"spotify bridge: listening on 127.0.0.1:{PORT}")

        class CmdHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path != "/cmd":
                    self.send_response(404)
                    self.end_headers()
                    return
                bridge.gen += 1
                me, out, end = bridge.gen, None, time.time() + 20
                bridge.poll_at = time.time()
                while time.time() < end and me == bridge.gen:
                    try:
                        c = bridge.cmds.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if me != bridge.gen:
                        bridge.cmds.put(c)       # a newer poll is waiting: it gets this one
                        break
                    if time.time() - c["t"] < 5:
                        out = c
                        break
                bridge.poll_at = time.time()
                try:
                    self.send_response(200 if out else 204)
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Access-Control-Allow-Private-Network", "true")
                    self.send_header("Cache-Control", "no-store")
                    if out:
                        body = json.dumps(out).encode()
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                    else:
                        self.end_headers()
                except OSError:
                    pass

        try:
            self.cmd_srv = ThreadingHTTPServer(("127.0.0.1", PORT + 1), CmdHandler)
            self.cmd_srv.daemon_threads = True
            threading.Thread(target=self.cmd_srv.serve_forever, daemon=True).start()
        except OSError as e:
            print(f"spotify bridge: no commands ({PORT + 1}):", e)
        return True
