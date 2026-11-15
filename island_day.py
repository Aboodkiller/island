"""
island_day.py - the day's rhythm (round 5): prayer times (Aladhan, method 23 = Jordan's Ministry of Awqaf, cached per
month so it works offline), adhan / Jumu'ah / adhkar reminders, Ramadan mode, morning briefing (with a sleep estimate
from the PC's idle gap), night recap, bedtime nudge, stand-up nudge.

Files (island folder): prayer_cache.json  {"YYYY-MM": {"YYYY-MM-DD": {"Fajr": "05:30", ..., "hijri": [d, m, y]}}}
                       day_log.json       {"YYYY-MM-DD": {"sleep_min": n, "brief": true, "recap": true, ...}}
"""

import datetime as dt
import json
import os
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "prayer_cache.json")
LOG = os.path.join(HERE, "day_log.json")
NAMES = ["Fajr", "Sunrise", "Dhuhr", "Asr", "Maghrib", "Isha"]
PRAYERS = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]          # Sunrise is shown, never called out
AR = {"Fajr": "الفجر", "Sunrise": "الشروق", "Dhuhr": "الظهر", "Asr": "العصر", "Maghrib": "المغرب", "Isha": "العشاء"}


def _load(p):
    try:
        with open(p, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _write(p, obj):
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


class Prayer:
    def __init__(self, lat, lon):
        self.lat, self.lon = lat, lon
        self.cache = _load(CACHE)
        self.lock = threading.Lock()
        self.busy = set()

    def fetch_month(self, y, m):
        url = (f"https://api.aladhan.com/v1/calendar/{y}/{m}?latitude={self.lat}&longitude={self.lon}&method=23")
        req = urllib.request.Request(url, headers={"User-Agent": "island-widget"})
        with urllib.request.urlopen(req, timeout=15) as r:
            j = json.load(r)
        days = {}
        for d in j.get("data", []):
            g = d["date"]["gregorian"]["date"]                  # DD-MM-YYYY
            iso = f"{g[6:10]}-{g[3:5]}-{g[0:2]}"
            t = {k: d["timings"][k][:5] for k in NAMES}
            h = d["date"]["hijri"]
            t["hijri"] = [int(h["day"]), int(h["month"]["number"]), int(h["year"])]
            days[iso] = t
        if len(days) < 27:
            raise ValueError(f"only {len(days)} days")
        with self.lock:
            self.cache[f"{y}-{m:02d}"] = days
            keep = sorted(self.cache)[-3:]                     # this month, next month, last month
            self.cache = {k: self.cache[k] for k in keep}
            _write(CACHE, self.cache)
        return days

    def ensure(self, today):
        """Fetch this month (and next, near the end of the month) in the background if missing."""
        want = [(today.year, today.month)]
        if today.day >= 25:
            n = today.replace(day=28) + dt.timedelta(days=5)
            want.append((n.year, n.month))
        for y, m in want:
            k = f"{y}-{m:02d}"
            if k in self.cache or k in self.busy:
                continue
            self.busy.add(k)

            def go(y=y, m=m, k=k):
                try:
                    self.fetch_month(y, m)
                    print(f"prayer times: got {k}")
                except Exception as e:
                    print(f"prayer times {k}:", e)
                finally:
                    self.busy.discard(k)
            threading.Thread(target=go, daemon=True).start()

    def day(self, d):
        with self.lock:
            return (self.cache.get(f"{d.year}-{d.month:02d}") or {}).get(d.isoformat())

    def times(self, d):
        """[(name, datetime)] for that date (or [] if not cached yet)."""
        t = self.day(d)
        if not t:
            return []
        out = []
        for n in NAMES:
            hh, mm = (int(x) for x in t[n].split(":"))
            out.append((n, dt.datetime.combine(d, dt.time(hh, mm))))
        return out

    def next(self, now):
        """(name, datetime) of the next prayer (not Sunrise), looking into tomorrow if needed."""
        for d in (now.date(), now.date() + dt.timedelta(days=1)):
            for n, at in self.times(d):
                if n in PRAYERS and at > now:
                    return n, at
        return None

    def hijri(self, d):
        t = self.day(d)
        return t.get("hijri") if t else None


class DayLog:
    def __init__(self):
        self.data = _load(LOG)

    def get(self, d):
        return self.data.setdefault(d.isoformat(), {})

    def mark(self, d, key, val=True):
        self.get(d)[key] = val
        keep = sorted(self.data)[-60:]
        self.data = {k: self.data[k] for k in keep}
        try:
            _write(LOG, self.data)
        except OSError as e:
            print("day_log:", e)

    def done(self, d, key):
        return bool(self.data.get(d.isoformat(), {}).get(key))
