"""
island_lyrics.py - synced lyrics for the song that's playing, from LRCLIB (free, no account, no key).

  Lyrics(emit).want(media)   call on every media update; fetches once per new song in a thread
  emit(dict(key=..., lines=[[seconds, "text"], ...]))   lines=None when there are no synced lyrics

Results (also "nothing found") are cached in lyrics_cache\\ so a song is only looked up once.
"""

import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "lyrics_cache")
API = "https://lrclib.net/api"
UA = "island-desktop-widget/1.0 (personal Windows widget; lyrics display)"
TS = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


def parse_lrc(text):
    """'[01:02.50] line' (one or more stamps per line) -> sorted [[62.5, 'line'], ...]"""
    out = []
    for raw in (text or "").splitlines():
        stamps = TS.findall(raw)
        if not stamps:
            continue
        words = TS.sub("", raw).strip()
        for m, s, frac in stamps:
            t = int(m) * 60 + int(s) + (int(frac) / (10 ** len(frac)) if frac else 0)
            out.append([round(t, 2), words])
    out.sort(key=lambda x: x[0])
    return out


def clean_title(t):
    """'Song - Remastered 2011' / 'Song (feat. X)' / 'Song [Official Video]' -> 'Song'"""
    t = re.sub(r"\s*[\(\[][^)\]]*(feat|ft\.|official|video|audio|lyrics?|remaster|live|version|edit)[^)\]]*[\)\]]", "", t, flags=re.I)
    t = re.sub(r"\s+-\s+.*(remaster|version|live|edit|mix).*$", "", t, flags=re.I)
    return t.strip()


class Lyrics:
    def __init__(self, emit):
        self.emit = emit
        self.key = None
        self.lock = threading.Lock()
        self.fails = {}
        os.makedirs(CACHE, exist_ok=True)

    @staticmethod
    def song(media):
        artist, title = (media.get("artist") or "").strip(), (media.get("title") or "").strip()
        if media.get("app") == "Browser" and " - " in title and not artist.endswith(" - Topic"):
            a, b = title.split(" - ", 1)          # YouTube: "Artist - Song (Official Video)"
            artist, title = a.strip(), b.strip()
        artist = re.sub(r"\s+-\s+Topic$", "", artist)
        return artist, title, (media.get("album") or "").strip(), float(media.get("dur") or 0)

    def want(self, media):
        if not media or not media.get("title"):
            return
        artist, title, album, dur = self.song(media)
        if media.get("app") == "Browser" and not artist:
            return           # a web page / video ("Instagram • Messages", "Watch Supernatural"), not a song
        key = f"{artist}|{title}".lower()
        with self.lock:
            if key == self.key:
                return
            self.key = key
        threading.Thread(target=self._fetch, args=(key, artist, title, album, dur, media.get("title")), daemon=True).start()

    def _cache_path(self, key):
        return os.path.join(CACHE, hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".json")

    def _get(self, path, params):
        url = f"{API}/{path}?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):         # 400 = LRCLIB didn't like the query (no artist, odd title): just not found
                return None
            raise

    def _lookup(self, artist, title, album, dur):
        tries = [title]
        ct = clean_title(title)
        if ct and ct != title:
            tries.append(ct)
        lrc = self._lookup1(artist, tries, album, dur)
        first = re.split(r"\s*,\s*", artist)[0].strip() if artist else ""
        if not lrc and first and first != artist and dur:
            # "Cocteau Twins, Harold Budd": Windows joins all artists, LRCLIB usually files the song under the first;
            # fuzzy search only, so the length check (within 4 s) still guards against a wrong song
            lrc = self._lookup1(first, tries, "", dur, fuzzy_only=True)
        return lrc

    def _lookup1(self, artist, tries, album, dur, fuzzy_only=False):
        for t in ([] if fuzzy_only else tries):
            if not artist:
                break
            p = dict(artist_name=artist, track_name=t)
            if album:
                p["album_name"] = album
            if dur:
                p["duration"] = int(round(dur))
            j = self._get("get", p)
            if j and j.get("syncedLyrics"):
                return j["syncedLyrics"]
        # fuzzy: search and take the closest length that has synced lyrics
        for t in tries:
            res = self._get("search", dict(track_name=t, artist_name=artist) if artist else dict(q=t)) or []
            best = None
            for r in res:
                if not r.get("syncedLyrics"):
                    continue
                gap = abs(float(r.get("duration") or 0) - dur) if dur else 0
                if gap <= 4 and (best is None or gap < best[0]):
                    best = (gap, r["syncedLyrics"])
            if best:
                return best[1]
        return None

    def _fetch(self, key, artist, title, album, dur, shown):
        cp = self._cache_path(key)
        lines = None
        try:
            if os.path.exists(cp):
                with open(cp, encoding="utf-8") as f:
                    c = json.load(f)
                # a "nothing found" result is retried after a week
                if c.get("lines") or time.time() - c.get("at", 0) < 7 * 86400:
                    lines = c.get("lines")
                    self._send(key, shown, lines)
                    return
            lrc = self._lookup(artist, title, album, dur)
            lines = parse_lrc(lrc) if lrc else None
            with open(cp, "w", encoding="utf-8") as f:
                json.dump(dict(key=key, at=time.time(), lines=lines), f, ensure_ascii=False)
            print(f"lyrics: {'found' if lines else 'none'} for {artist} - {title}")
        except Exception as e:           # e.g. LRCLIB busy (503): try this song again in a minute, max 3 times
            n = self.fails.get(key, 0) + 1
            self.fails[key] = n
            print(f"lyrics: {e} (try {n}/3)")
            if n < 3:
                threading.Timer(60, self._retry, args=(key,)).start()
            lines = None
        self._send(key, shown, lines)

    def _retry(self, key):
        with self.lock:
            if self.key == key:
                self.key = None          # the next media update looks it up again

    def _send(self, key, shown, lines):
        with self.lock:
            if key != self.key:
                return                     # the song changed while we were looking
        self.emit(dict(key=key, title=shown, lines=lines))   # title = the media title it belongs to
