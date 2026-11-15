"""
island_core.py - data layer for the Island widget.

Reads two plan files and merges them into one day timeline:
  study  : plans\\study.json   (study plan: subjects, sessions, exam day)
  health : plans\\health.json  (day plan: classes, meals, workouts, water, vitamins)
Each file keeps its own schema and its own owner chat. This module only
reads them, and writes back the small changes the widget makes (ticks, water).
Moved / swapped sessions live in the island's own island_moves.json ({"2026-11-06": {"S:M": "19:00", ...}}),
so the plan files are never touched for them.
"""

import datetime as dt
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
MOVES_FILE = os.path.join(HERE, "island_moves.json")
DAY = dt.timedelta(days=1)
DAY_EN = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ARABIC = re.compile(r"[؀-ۿ]")


def is_arabic(s):
    return bool(s) and bool(ARABIC.search(s))


def hm2min(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def dur_min(h):
    """'3h' -> 180, '1.5h' -> 90, '45m' -> 45."""
    try:
        s = str(h).strip().lower()
        if s.endswith("m"):
            return int(float(s[:-1]))
        return int(float(s.rstrip("h")) * 60)
    except Exception:
        return 120


def load_config():
    cfg = {"study_plan": os.path.join(HERE, "plans", "study.json"),
           "health_plan": os.path.join(HERE, "plans", "health.json")}
    try:
        with open(os.path.join(HERE, "island_config.json"), encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return {k: os.path.normpath(os.path.join(HERE, v)) if k.endswith("_plan") else v for k, v in cfg.items()}


class JsonFile:
    def __init__(self, path, empty):
        self.path = path
        self.mtime = None
        self.data = None
        self.empty = empty
        self.error = None
        self.load()

    @property
    def ok(self):
        return os.path.exists(self.path)

    def load(self):
        try:
            m = os.path.getmtime(self.path)
            if m == self.mtime and self.data is not None:
                return False
            with open(self.path, encoding="utf-8") as f:
                self.data = json.load(f)
            self.mtime = m
            self.error = None
            return True
        except Exception as e:
            self.error = str(e)
            if self.data is None:
                self.data = json.loads(json.dumps(self.empty))
            return False

    def save(self, today):
        if not self.ok:
            return
        self.data["updated"] = today.isoformat()
        self.data["updated_by"] = "island widget"
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self.mtime = os.path.getmtime(self.path)


# ======================================================================= study
SUBJECT_EN = {"P": "Physics", "M": "Maths", "B": "Biology", "H": "History", "R": "Review"}
SUBJECT_CAT = {"P": "P", "M": "MA", "B": "B", "H": "HI", "R": "R"}


class Study(JsonFile):
    def __init__(self, path):
        super().__init__(path, {"subjects": {}, "rhythm": {"content": {}, "mock": {}}, "times": {}, "hours": {}})

    def _date(self, key, default):
        try:
            return dt.date.fromisoformat(self.data.get(key, default))
        except Exception:
            return dt.date.fromisoformat(default)

    @property
    def exam_day(self):
        return self._date("exam_day", "2026-12-10")

    @property
    def mock_start(self):
        return self._date("mock_start", "2026-11-22")

    def subjects_on(self, day):
        phase = "mock" if day >= self.mock_start else "content"
        return list(self.data.get("rhythm", {}).get(phase, {}).get(str(day.weekday()), []))

    def items(self, s):
        return self.data.get("subjects", {}).get(s, {}).get("items", [])

    def undone(self, s):
        return [it for it in self.items(s) if not it.get("done")]

    def done_on(self, s, day):
        iso = day.isoformat()
        return [it for it in self.items(s) if it.get("done") == iso]

    def tasks_for(self, day, today):
        """Same queue logic as tawjihi_widget v3: today/future = next undone sessions in order,
        past = what was ticked that day."""
        wd = str(day.weekday())
        times = self.data.get("times", {}).get(wd, [])
        hours = self.data.get("hours", {})
        out = []
        for i, s in enumerate(self.subjects_on(day)):
            base = {"s": s, "time": times[i] if i < len(times) else None, "dur": dur_min(hours.get(s, "2h"))}
            sub = self.data.get("subjects", {}).get(s, {})
            if "repeat" in sub:
                rid = f"{s}-{day.isoformat()}"
                out.append(dict(base, id=rid, text=sub["repeat"],
                                done=rid in self.data.get("repeat_done", []), repeat=True))
                continue
            if day < today:
                for it in self.done_on(s, day):
                    out.append(dict(base, id=it["id"], text=it["text"], done=True))
                continue
            skip, d = 0, today
            while d < day:
                if s in self.subjects_on(d):
                    skip += 1
                d += DAY
            done_today = self.done_on(s, today)
            if day == today and done_today:
                it = done_today[-1]
                out.append(dict(base, id=it["id"], text=it["text"], done=True))
                continue
            skip -= 1 if (done_today and day > today and s in self.subjects_on(today)) else 0
            und = self.undone(s)
            if skip < len(und):
                it = und[max(skip, 0)]
                out.append(dict(base, id=it["id"], text=it["text"], done=False))
        return out

    def toggle(self, s, iid, today, repeat=False):
        if repeat:
            lst = self.data.setdefault("repeat_done", [])
            lst.remove(iid) if iid in lst else lst.append(iid)
        else:
            for it in self.items(s):
                if it["id"] == iid:
                    it["done"] = None if it.get("done") else today.isoformat()
                    break
        self.save(today)

    def pace(self, today):
        res = {}
        for s in ("P", "M", "B", "H"):
            rem = sum(1 for it in self.items(s) if it.get("phase") == "content" and not it.get("done"))
            avail, d = 0, today
            if self.done_on(s, today):
                d += DAY
            while d < self.mock_start:
                if s in self.subjects_on(d):
                    avail += 1
                d += DAY
            res[s] = (rem, avail)
        return res

    def progress(self):
        out = {}
        for s in ("P", "M", "B", "H"):
            its = [i for i in self.items(s) if i.get("phase") == "content"]
            out[s] = (sum(1 for i in its if i.get("done")), len(its))
        return out

    def next_checkpoint(self, today):
        cps = sorted((k, v) for k, v in self.data.get("checkpoints", {}).items() if k >= today.isoformat())
        return cps[0] if cps else None


# ====================================================================== health
HEALTH_CAT = {"W": "W", "K": "K", "M": "ME", "V": "V", "H": "WA", "U": "U", "T": "T", "E": "E"}


class Health(JsonFile):
    def __init__(self, path):
        super().__init__(path, {"workouts": {}, "meals": [], "vitamins": [], "water": {}, "log": {}, "schedule": []})

    @property
    def day_start(self):
        try:
            return hm2min(self.data.get("day_starts", "04:00"))
        except Exception:
            return 240

    @property
    def W(self):
        return self.data.get("workouts", {})

    @property
    def water_cfg(self):
        return self.data.get("water", {})

    def log(self, day, create=False):
        lg = self.data.setdefault("log", {})
        iso = day.isoformat()
        return lg.setdefault(iso, {}) if create else lg.get(iso, {})

    def is_done(self, day, iid):
        return iid in self.log(day).get("done", [])

    def toggle(self, day, iid):
        lst = self.log(day, True).setdefault("done", [])
        lst.remove(iid) if iid in lst else lst.append(iid)

    def water(self, day):
        return int(self.log(day).get("water_ml", 0))

    def add_water(self, day, ml):
        lg = self.log(day, True)
        lg["water_ml"] = max(0, int(lg.get("water_ml", 0)) + int(ml))

    def goal_ml(self, day):
        """That day's water goal: the one QAWIFit had (saved in the log by the island), else the plan's."""
        try:
            g = int(self.log(day).get("water_goal_ml") or 0)
        except (TypeError, ValueError):
            g = 0
        return g if g > 0 else int(self.water_cfg.get("goal_ml", 0))

    def gym_kind(self, day):
        wd = str(day.weekday())
        if wd in self.W.get("gym_days", []):
            return "gym"
        if wd in self.W.get("optional_days", []):
            return "optional"
        return None

    def session(self, sid):
        return self.W.get("sessions", {}).get(sid, {"name": sid or "-", "exercises": []})

    def session_for(self, day, today):
        lg = self.log(day)
        if lg.get("workout"):
            return lg["workout"]
        cyc = self.W.get("cycle", []) or ["-"]
        iso = day.isoformat()
        n = sum(1 for d, v in self.data.get("log", {}).items() if d < iso and v.get("workout"))
        n += int(self.W.get("cycle_offset", 0))
        if day > today:
            d = today
            while d < day:
                if self.gym_kind(d) == "gym" and not self.log(d).get("workout"):
                    n += 1
                d += DAY
        return cyc[n % len(cyc)]

    def workout_done(self, day):
        return bool(self.log(day).get("workout"))

    def toggle_workout(self, day, today):
        lg = self.log(day, True)
        if lg.get("workout"):
            lg.pop("workout")
        else:
            lg["workout"] = self.session_for(day, today)

    def toggle_ex(self, day, sid, eid):
        lg = self.log(day, True)
        ex = lg.setdefault("ex", [])
        ex.remove(eid) if eid in ex else ex.append(eid)
        ids = [e["id"] for e in self.session(sid).get("exercises", [])]
        if ids and all(i in ex for i in ids):
            lg["workout"] = sid

    @staticmethod
    def meal_for(m, day):
        over = m.get("by_day", {}).get(str(day.weekday()), {})
        return dict(m, **over) if over else m

    def full(self, day, today, extra_req=0, extra_done=0):
        req = [t for t in self.raw_tasks(day, today) if not t["optional"]]
        done = sum(1 for t in req if t["done"]) + extra_done
        total = len(req) + extra_req
        goal = self.goal_ml(day)
        if goal:
            total += 1
            done += min(1.0, self.water(day) / goal)
        return done, total

    def raw_tasks(self, day, today):
        out = []
        kind = self.gym_kind(day)
        if kind:
            sid = self.session_for(day, today)
            ses = self.session(sid)
            n = len(ses.get("exercises", []))
            ticked = len([e for e in self.log(day).get("ex", []) if e.startswith(sid + "-")])
            done = self.workout_done(day)
            sub = f"{n} exercises · ~{self.W.get('length_min', 45)} min"
            if ticked and not done:
                sub += f" · {ticked}/{n} done"
            if kind == "optional":
                sub += " · optional"
            out.append(dict(id="WO", cat="W", title=ses.get("name", sid), sub=sub, time=self.W.get("time"),
                            done=done, optional=(kind == "optional"), dur=int(self.W.get("length_min", 45))))
        walk = self.W.get("walk")
        if walk and str(day.weekday()) in walk.get("days", []):
            out.append(dict(id="WALK", cat="K", title=walk.get("text", "Walk"), sub=walk.get("sub", ""),
                            time=None, done=self.is_done(day, "WALK"), optional=True, dur=0))
        for m in self.data.get("meals", []):
            m = self.meal_for(m, day)
            if m.get("skip"):
                continue
            out.append(dict(id=m["id"], cat="M", title=m.get("name", m["id"]), sub=m.get("text", ""),
                            time=m.get("time"), done=self.is_done(day, m["id"]), optional=False, dur=30))
        for v in self.data.get("vitamins", []):
            out.append(dict(id="v:" + v["id"], cat="V", title=v.get("name", v["id"]),
                            sub=" · ".join(x for x in (v.get("dose", ""), v.get("note", "")) if x),
                            time=v.get("time"), done=self.is_done(day, "v:" + v["id"]), optional=False, dur=20))
        return out

    def blocks(self, day):
        wd = str(day.weekday())
        out = []
        for b in self.data.get("schedule", []):
            if wd in b.get("days", []):
                out.append(dict(id="b:" + b["id"] + ":" + wd, cat=b.get("cat", "U"), title=b.get("title", ""),
                                sub=b.get("where", ""), time=b["start"], end=b["end"], kind=b.get("kind", "class"),
                                remind=b.get("remind", False)))
        return out

    def gym_streak(self, today):
        first = min(self.data.get("log", {}) or [today.isoformat()])
        n, d, k = 0, today, 0
        while k < 400 and d.isoformat() >= first:
            kind = self.gym_kind(d)
            done = self.workout_done(d)
            if kind == "gym":
                if done:
                    n += 1
                elif d != today:
                    break
            elif done:
                n += 1
            d -= DAY
            k += 1
        return n


# ======================================================================= model
class Model:
    """Merged view of both plans."""

    def __init__(self):
        cfg = load_config()
        self.study = Study(cfg["study_plan"])
        self.health = Health(cfg["health_plan"])
        self.moves, self._moves_mt = {}, None
        self.load_moves()

    def reload(self):
        a = self.study.load()
        b = self.health.load()
        c = self.load_moves()
        return a or b or c

    # -- moved / swapped sessions (one day at a time; the plan's own times stay as they are)
    def load_moves(self):
        try:
            mt = os.path.getmtime(MOVES_FILE)
        except OSError:
            changed = bool(self.moves)
            self.moves, self._moves_mt = {}, None
            return changed
        if mt == self._moves_mt:
            return False
        try:
            with open(MOVES_FILE, encoding="utf-8") as f:
                d = json.load(f)
            self.moves = {k: {a: b for a, b in v.items() if isinstance(b, str) and re.match(r"^\d{1,2}:\d{2}$", b)}
                          for k, v in d.items() if isinstance(v, dict)} if isinstance(d, dict) else {}
        except Exception:
            self.moves = {}
        self._moves_mt = mt
        return True

    @staticmethod
    def move_key(item):
        """Study sessions move by subject ("S:M"), so the move survives ticking; everything else by its key."""
        return "S:" + item["s"] if item.get("src") == "S" and item.get("s") else item["key"]

    def set_moves(self, day, changes):
        """changes = {move_key: "HH:MM" | None}; None = back to the plan's time. Keeps the last 2 weeks only."""
        self.load_moves()
        mv = {k: dict(v) for k, v in self.moves.items()}
        dd = mv.setdefault(day.isoformat(), {})
        for k, v in changes.items():
            if v is None:
                dd.pop(k, None)
            else:
                dd[k] = v
        if not dd:
            mv.pop(day.isoformat(), None)
        keep = (day - dt.timedelta(days=14)).isoformat()
        mv = {k: v for k, v in mv.items() if k >= keep}
        tmp = MOVES_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(mv, f, indent=1)
        os.replace(tmp, MOVES_FILE)
        self.moves, self._moves_mt = mv, os.path.getmtime(MOVES_FILE)

    # -- time helpers (one logical day for both plans: before day_starts = previous day)
    def today(self, now=None):
        now = now or dt.datetime.now()
        return (now - dt.timedelta(minutes=self.health.day_start)).date()

    def at(self, day, hhmm):
        t = hm2min(hhmm)
        d = day + DAY if t < self.health.day_start else day
        return dt.datetime.combine(d, dt.time(t // 60, t % 60))

    def order(self, hhmm):
        return (hm2min(hhmm) - self.health.day_start) % 1440 if hhmm else 9999

    # -- merged items
    def items(self, day, today=None):
        today = today or self.today()
        out = []
        if self.study.ok:
            for t in self.study.tasks_for(day, today):
                s = t["s"]
                out.append(dict(key=f"S:{t['id']}", src="S", cat=SUBJECT_CAT.get(s, "R"), s=s,
                                title=SUBJECT_EN.get(s, s), desc=t["text"], time=t["time"], dur=t["dur"],
                                end=None, done=t["done"], tickable=True, optional=False, block=False,
                                ref=dict(s=s, id=t["id"], repeat=t.get("repeat", False))))
        if self.health.ok:
            for t in self.health.raw_tasks(day, today):
                out.append(dict(key=f"H:{t['id']}", src="H", cat=HEALTH_CAT.get(t["cat"], t["cat"]),
                                title=t["title"], desc=t["sub"], time=t["time"], dur=t["dur"], end=None,
                                done=t["done"], tickable=True, optional=t["optional"], block=False,
                                ref=dict(id=t["id"])))
            for b in self.health.blocks(day):
                out.append(dict(key=f"H:{b['id']}", src="H", cat=HEALTH_CAT.get(b["cat"], b["cat"]),
                                title=b["title"], desc=b["sub"], time=b["time"], end=b["end"],
                                dur=(hm2min(b["end"]) - hm2min(b["time"])) % 1440, done=False, tickable=False,
                                optional=True, block=True, kind=b["kind"], remind=b["remind"], ref=dict(id=b["id"])))
        mv = self.moves.get(day.isoformat(), {})
        for t in out:
            k = self.move_key(t)
            t["movable"] = bool(t["tickable"] and t.get("time") and not t["block"])
            if t["movable"] and k in mv and mv[k] != t["time"]:
                t["plan_time"], t["time"], t["moved"] = t["time"], mv[k], True
        out.sort(key=lambda t: (self.order(t["time"]), 0 if t["block"] else 1))
        return out

    def toggle(self, item, day):
        today = self.today()
        self.reload()
        if item["src"] == "S":
            r = item["ref"]
            self.study.toggle(r["s"], r["id"], today, r.get("repeat", False))
        else:
            iid = item["ref"]["id"]
            if iid == "WO":
                self.health.toggle_workout(day, today)
            else:
                self.health.toggle(day, iid)
            self.health.save(today)

    def is_done_now(self, item, day):
        self.reload()
        for t in self.items(day):
            if t["key"] == item["key"]:
                return t["done"]
        return True

    def add_water(self, day, ml):
        self.reload()
        self.health.add_water(day, ml)
        self.health.save(self.today())

    def set_water_goal(self, day, ml):
        """Remember the day's QAWIFit water goal in the plan log, so rings, reminders and streaks use it."""
        self.reload()
        if int(ml) > 0 and self.health.log(day).get("water_goal_ml") != int(ml):
            self.health.log(day, True)["water_goal_ml"] = int(ml)
            self.health.save(self.today())

    def toggle_ex(self, day, sid, eid):
        self.reload()
        self.health.toggle_ex(day, sid, eid)
        self.health.save(self.today())

    # -- status
    def span(self, item, day):
        if not item.get("time"):
            return None, None
        s = self.at(day, item["time"])
        e = self.at(day, item["end"]) if item.get("end") else s + dt.timedelta(minutes=max(item["dur"], 15))
        if e <= s:
            e += DAY
        return s, e

    def state(self, item, day, now=None):
        """('now', minutes_left, frac) | ('next', minutes_until) | ('late', minutes_over) | ('done',) | ('free',)"""
        now = now or dt.datetime.now()
        if item["done"]:
            return ("done",)
        s, e = self.span(item, day)
        if s is None:
            return ("free",)
        if s <= now < e:
            tot = (e - s).total_seconds() or 1
            return ("now", int((e - now).total_seconds() // 60) + 1, (now - s).total_seconds() / tot)
        if now < s:
            return ("next", int((s - now).total_seconds() // 60) + 1)
        return ("late", int((now - e).total_seconds() // 60))

    def focus(self, now=None):
        """What the island should show: (now_item, next_item) for today."""
        now = now or dt.datetime.now()
        day = self.today(now)
        cur, nxt = None, None
        cands = [t for t in self.items(day) if t.get("time")]
        nows = [t for t in cands if self.state(t, day, now)[0] == "now"]
        if nows:
            # a real task (study, gym, meal) beats a background block (trip, class) when both run
            nows.sort(key=lambda t: (t["block"], 0 if t["src"] == "S" else 1))
            cur = nows[0]
        ups = [t for t in cands if self.state(t, day, now)[0] == "next" and t is not cur]
        ups.sort(key=lambda t: self.span(t, day)[0])
        if ups:
            nxt = ups[0]
        return cur, nxt

    def late_items(self, day, now=None):
        return [t for t in self.items(day) if t["tickable"] and not t["optional"] and
                self.state(t, day, now)[0] == "late"]

    def completion(self, day):
        today = self.today()
        st = [t for t in self.items(day, today) if t["src"] == "S"]
        sd = sum(1 for t in st if t["done"])
        if self.health.ok:
            d, tot = self.health.full(day, today, len(st), sd)
        else:
            d, tot = sd, len(st)
        return d, tot

    def full(self, day):
        d, t = self.completion(day)
        return t > 0 and d >= t - 1e-9

    def fire_streak(self, today):
        d = today if self.full(today) else today - DAY
        logs = list(self.health.data.get("log", {}).keys()) or [today.isoformat()]
        first = min(logs)
        n = 0
        while n < 400 and d.isoformat() >= first and self.full(d):
            n += 1
            d -= DAY
        return n

    def water(self, day):
        return self.health.water(day), self.health.goal_ml(day)

    def days_to_exam(self, today=None):
        today = today or self.today()
        return (self.study.exam_day - today).days
