"""
island_fit.py - live, READ-ONLY feed of the user's QAWIFit (CUT-OS) data for the island's Body tab.

Reads from Firestore (project in firebase-key.json) with the Admin SDK:
  users/{uid}/profile/data      dayCutoffHour, totalXp, weightKg, waterTargetMlOverride
  users/{uid}/targets/data      targetKcal, proteinG, carbsG, fatG
  users/{uid}/meals-today       DayMeal docs (date == today) -> components[].computed
  users/{uid}/water/{date}      totalMl
  users/{uid}/xpEvents          only if profile.totalXp is missing

The ONLY thing ever written to Firestore is today's water log (users/{uid}/water/{date}),
in the app's own format {totalMl, entries:[{ml, t}]}, so the island and the phone share one water
total. Listeners push changes within a second or two, plus a full refresh every few minutes.

Setup: pip install firebase-admin, and put the service-account key at island\\firebase-key.json
(never commit it). The user id is found automatically on first run and saved to
island_config.json as "qawi_uid"; set it there by hand to override.
"""

import datetime as dt
import json
import os
import queue
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.path.join(HERE, "firebase-key.json")
CONFIG_FILE = os.path.join(HERE, "island_config.json")
OWNER_EMAIL = ""     # used only to pick the right account on first run
REFRESH_S = 300

# 14-tier ladder, copied from CUT-OS src/data/ranks.ts + i18n names
RANKS = [
    ("novice", "Novice", 0, "#8b95a3"), ("rookie", "Rookie", 200, "#9aa5b3"),
    ("grinder", "Grinder", 500, "#b87e45"), ("disciplined", "Disciplined", 1000, "#c89058"),
    ("lockedIn", "Locked In", 1800, "#d8a06a"), ("cutting", "Cutting", 3000, "#e89a4d"),
    ("shredded", "Shredded", 4500, "#e0e0e0"), ("sharp", "Sharp", 6500, "#f0c040"),
    ("carved", "Carved", 9000, "#7ab089"), ("steel", "Steel", 12000, "#6ab0c0"),
    ("peak", "Peak", 16000, "#5a9dc8"), ("apex", "Apex", 22000, "#34d399"),
    ("mythic", "Mythic", 30000, "#fde68a"), ("goat", "GOAT", 45000, "#fca5a5"),
]


def rank_from_xp(xp):
    xp = max(0, int(xp or 0))
    i = 0
    for j, r in enumerate(RANKS):
        if xp >= r[2]:
            i = j
    cur = RANKS[i]
    nxt = RANKS[i + 1] if i + 1 < len(RANKS) else None
    if nxt:
        band = nxt[2] - cur[2]
        frac = min(1.0, max(0.0, (xp - cur[2]) / band))
        return dict(name=cur[1], color=cur[3], next=nxt[1], to_next=nxt[2] - xp, frac=round(frac, 4), xp=xp)
    return dict(name=cur[1], color=cur[3], next=None, to_next=0, frac=1.0, xp=xp)


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def apply_entries(entries, ml):
    """Add an entry for +ml; for -ml take it back from the newest entries (like the app's undo)."""
    entries = [dict(e) for e in entries]
    if ml > 0:
        entries.append({"ml": int(ml), "t": int(time.time() * 1000)})
        return entries
    left = -int(ml)
    while left > 0 and entries:
        last = entries[-1]
        take = min(int(num(last.get("ml"))), left)
        last["ml"] = int(num(last.get("ml"))) - take
        left -= take
        if last["ml"] <= 0:
            entries.pop()
        if take <= 0:
            break
    return entries


class FitWorker(threading.Thread):
    """Emits a dict (or None while unavailable) through `emit` whenever the data changes."""

    def __init__(self, emit):
        super().__init__(daemon=True)
        self.emit = emit
        self.lock = threading.Lock()
        self.sub_lock = threading.RLock()
        self.db = None
        self.uid = None
        self.date = None
        self.subs = []
        self.profile, self.targets, self.water = {}, {}, {}
        self.meals = []
        self.xp_events_total = None
        self.last = None
        self.status = "starting"
        self.water_loaded = False
        # water writes go out one at a time; while any are in flight the listener's echoes are ignored
        # (they're older than what the island already shows and made the ring jump back on quick taps)
        self.wq = queue.Queue()
        self.inflight = 0
        self.water_ut = None
        self.writer = None

    # ------------------------------------------------------------------ setup
    def run(self):
        try:
            import firebase_admin
            from firebase_admin import credentials, firestore
        except Exception as e:
            self.fail(f"firebase-admin not installed ({e}); run: pip install firebase-admin")
            return
        if not os.path.exists(KEY_FILE):
            self.fail("no firebase-key.json in the island folder")
            return
        while True:
            try:
                if not firebase_admin._apps:
                    firebase_admin.initialize_app(credentials.Certificate(KEY_FILE))
                self.db = firestore.client()
                self.uid = self.uid or self.find_uid()
                print(f"fit: connected, uid={self.uid}")
                break
            except Exception as e:
                self.fail(f"connect failed: {type(e).__name__}: {str(e)[:160]}")
                time.sleep(60)
        self.watch_static()
        while True:
            try:
                self.ensure_day()
                if time.time() - getattr(self, "_full_at", 0) > REFRESH_S:
                    self.full_refresh()
            except Exception as e:
                print("fit loop:", type(e).__name__, str(e)[:200])
            time.sleep(20)

    def fail(self, why):
        if why != self.status:
            print("fit:", why)
        self.status = why
        self.emit(dict(ok=False, why=why))

    def find_uid(self):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                uid = json.load(f).get("qawi_uid")
            if uid:
                return uid
        except Exception:
            pass
        from firebase_admin import auth
        users = [u for u in auth.list_users().iterate_all() if u.provider_data]   # skip anonymous
        if not users:
            raise RuntimeError("no signed-in (non-anonymous) users in this Firebase project")
        mine = [u for u in users if (u.email or "").lower() == OWNER_EMAIL]
        pick = mine[0] if mine else max(users, key=lambda u: u.user_metadata.last_sign_in_timestamp or 0)
        if not mine:
            print(f"fit: {OWNER_EMAIL} not found; using most recent sign-in {pick.email} "
                  "(set qawi_uid in island_config.json to change)")
        cfg = {}
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            pass
        cfg["qawi_uid"] = pick.uid
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=1)
        except Exception as e:
            print("fit: could not save uid:", e)
        return pick.uid

    # ------------------------------------------------------------------ listeners
    def user(self):
        return self.db.collection("users").document(self.uid)

    def watch_static(self):
        u = self.user()
        u.collection("profile").document("data").on_snapshot(
            lambda docs, ch, rt: self.set_doc("profile", docs))
        u.collection("targets").document("data").on_snapshot(
            lambda docs, ch, rt: self.set_doc("targets", docs))

    def set_doc(self, which, docs):
        d = docs[0].to_dict() if docs and docs[0].exists else {}
        with self.lock:
            setattr(self, which, d or {})
        if which == "profile":
            self.ensure_day()
        self.push()

    def day_str(self):
        cutoff = int(num(self.profile.get("dayCutoffHour")))
        now = dt.datetime.now()
        if now.hour < cutoff:
            now -= dt.timedelta(days=1)
        return now.strftime("%Y-%m-%d")

    def ensure_day(self):
        if not self.db:
            return
        with self.sub_lock:
            self._switch_day(self.day_str())

    def _switch_day(self, day):
        if day == self.date:
            return
        for s in self.subs:
            try:
                s.unsubscribe()
            except Exception:
                pass
        self.subs = []
        with self.lock:
            self.date, self.meals, self.water = day, [], {}
            self.water_loaded = False
        from google.cloud.firestore_v1.base_query import FieldFilter
        u = self.user()
        q = u.collection("meals-today").where(filter=FieldFilter("date", "==", day))
        self.subs.append(q.on_snapshot(lambda docs, ch, rt: self.set_meals(day, docs)))
        self.subs.append(u.collection("water").document(day).on_snapshot(
            lambda docs, ch, rt: self.set_water(day, docs)))
        print(f"fit: watching {day}")

    def set_meals(self, day, docs):
        if day != self.date:
            return
        with self.lock:
            self.meals = [d.to_dict() for d in docs]
        self.push()

    def set_water(self, day, docs):
        if day != self.date:
            return
        with self.lock:
            if self.inflight:
                return
            ut = getattr(docs[0], "update_time", None) if docs and docs[0].exists else None
            if ut is not None and self.water_ut is not None and ut < self.water_ut:
                return                     # a late echo of an earlier write
            self.water = docs[0].to_dict() if docs and docs[0].exists else {}
            self.water_loaded = True
        self.push()

    def full_refresh(self):
        """Safety net in case a listener silently stalls."""
        self._full_at = time.time()
        u = self.user()
        p = u.collection("profile").document("data").get()
        t = u.collection("targets").document("data").get()
        with self.lock:
            self.profile = (p.to_dict() or {}) if p.exists else {}
            self.targets = (t.to_dict() or {}) if t.exists else {}
        if self.profile.get("totalXp") is None:
            self.xp_events_total = sum(num(e.to_dict().get("amount")) for e in u.collection("xpEvents").stream())
        self.ensure_day()
        from google.cloud.firestore_v1.base_query import FieldFilter
        docs = list(u.collection("meals-today").where(filter=FieldFilter("date", "==", self.date)).stream())
        w = u.collection("water").document(self.date).get()
        with self.lock:
            self.meals = [d.to_dict() for d in docs]
            if not self.inflight:
                self.water = (w.to_dict() or {}) if w.exists else {}
                self.water_loaded = True
        self.push()

    # ------------------------------------------------------------------ water (the only write)
    def live_water(self, day_iso):
        """Today's QAWIFit water total in ml, or None when QAWIFit can't be trusted for that day."""
        if self.status != "ok" or not self.water_loaded or day_iso != self.date:
            return None
        return int(round(num(self.water.get("totalMl"))))

    def add_water(self, day_iso, ml):
        """Add (or, for negative ml, remove) water in QAWIFit. Returns False if QAWIFit isn't usable."""
        if self.live_water(day_iso) is None or not ml:
            return False
        ml = int(ml)
        with self.lock:   # optimistic, so the island updates at once; the listener confirms
            w = dict(self.water)
            entries = list(w.get("entries") or [])
            w["totalMl"] = max(0, int(num(w.get("totalMl"))) + ml)
            w["entries"] = apply_entries(entries, ml)
            self.water = w
            self.inflight += 1
        self.push()
        if self.writer is None:
            self.writer = threading.Thread(target=self._water_writer, daemon=True)
            self.writer.start()
        self.wq.put((day_iso, ml))
        return True

    def _water_writer(self):
        """Writes queued water changes in order; after the last one, re-reads the doc once so the island
        shows exactly what QAWIFit has (and later listener echoes older than that are skipped)."""
        while True:
            day_iso, ml = self.wq.get()
            self._write_water(day_iso, ml)
            with self.lock:
                self.inflight -= 1
                last = self.inflight == 0
            if not last:
                continue
            try:
                snap = self.user().collection("water").document(day_iso).get()
            except Exception as e:
                print("fit: water re-read failed:", type(e).__name__, str(e)[:200])
                self._full_at = 0          # the main loop does a full refresh within 20 s
                continue
            with self.lock:
                if self.inflight or day_iso != self.date:
                    continue
                self.water = (snap.to_dict() or {}) if snap.exists else {}
                self.water_ut = getattr(snap, "update_time", None)
                self.water_loaded = True
            self.push()

    def _write_water(self, day_iso, ml):
        from google.cloud import firestore as gfs
        ref = self.user().collection("water").document(day_iso)

        @gfs.transactional
        def tx(transaction):
            snap = ref.get(transaction=transaction)
            d = (snap.to_dict() or {}) if snap.exists else {}
            entries = list(d.get("entries") or [])
            total = max(0, int(num(d.get("totalMl"))) + ml)
            transaction.set(ref, {"totalMl": total, "entries": apply_entries(entries, ml)})
        try:
            tx(self.db.transaction())
            print(f"fit: water {ml:+d} ml -> QAWIFit")
        except Exception as e:
            print("fit: water write failed:", type(e).__name__, str(e)[:200])

    # ------------------------------------------------------------------ payload
    def push(self):
        with self.lock:
            out = self.build()
        if out != self.last:
            self.last = out
            self.status = "ok"
            self.emit(out)

    def build(self):
        tg, pr = self.targets, self.profile
        tot = dict(kcal=0.0, p=0.0, c=0.0, f=0.0)
        meals = []
        for m in sorted(self.meals, key=lambda m: num(m.get("createdAt"))):
            mk = dict(kcal=0.0, p=0.0, c=0.0, f=0.0)
            for comp in m.get("components") or []:
                c = comp.get("computed") or {}
                mk["kcal"] += num(c.get("kcal"))
                mk["p"] += num(c.get("protein"))
                mk["c"] += num(c.get("carbs"))
                mk["f"] += num(c.get("fat"))
            for k in tot:
                tot[k] += mk[k]
            name = (m.get("name") or {}).get("en") or m.get("tag") or "Meal"
            meals.append(dict(name=name, kcal=round(mk["kcal"]), p=round(mk["p"]),
                              n=len(m.get("components") or []),
                              time=m.get("timeLabel") or dt.datetime.fromtimestamp(num(m.get("createdAt")) / 1000).strftime("%H:%M")
                              if m.get("createdAt") else ""))
        water_goal = num(pr.get("waterTargetMlOverride")) or round(num(pr.get("weightKg")) * 35)
        xp = pr.get("totalXp")
        if xp is None:
            xp = self.xp_events_total or 0
        return dict(
            ok=bool(tg), why="" if tg else "no targets in QAWIFit yet", date=self.date,
            kcal=round(tot["kcal"]), p=round(tot["p"]), c=round(tot["c"]), f=round(tot["f"]),
            t=dict(kcal=round(num(tg.get("targetKcal"))), p=round(num(tg.get("proteinG"))),
                   c=round(num(tg.get("carbsG"))), f=round(num(tg.get("fatG")))),
            water=round(num(self.water.get("totalMl"))), water_goal=round(water_goal),
            rank=rank_from_xp(xp), meals=meals,
        )
