"""
island_inbox.py - lets chats (and meal ticks) log food into QAWIFit through the island.

How a chat logs food
  1. Look the foods up in qawi_foods.json (built-in) and qawi_foods_user.json (the user's own foods).
  2. Show the user a preview (items, grams, kcal/protein) and wait for their ok.
  3. Write ONE file into island\\inbox\\, e.g. inbox\\lunch-1730112345.json:
       {"type": "food", "id": "lunch-1730112345", "from": "training chat",
        "meal": {"name": "Lunch", "tag": "lunch"},
        "items": [{"food": "seed_chicken_breast", "grams": 200},
                  {"custom": {"en": "Mum's maqluba", "ar": "مقلوبة",
                              "per100g": {"kcal": 180, "protein": 8, "carbs": 22, "fat": 7, "fiber": 1}},
                   "grams": 350, "save": true}]}
     tag is one of breakfast, lunch, dinner, snack, pre, post. "save": true adds a custom food to the
     user's QAWIFit library so it is exact next time.
  4. Within a few seconds the island moves the file to inbox\\done\\ (with "result") or inbox\\failed\\
     (with "error"). Read it back and tell the user.
  Undo: {"type": "undo", "id": "undo-1", "target": "lunch-1730112345"}
  Barcodes: an item may carry "barcode": "6281..." next to "food" or "custom" (+ "save": true); the island remembers
     barcode -> food id in barcodes.json. Later an item can be just {"barcode": "6281...", "grams": 150}.
     NEVER put a barcode into a food name.
  Water: {"type": "water", "id": "water-1730112345", "ml": 500}   (negative removes)
  Weight: {"type": "weight", "id": "weight-2026-11-04", "kg": 160.8} -> users/{uid}/progress/{day}.weightKg (+ the app's
     +10 XP "weight_logged" once per training day). Undo puts the previous weight back.

What the island writes (the app's own formats, see CUT-OS src/types): one new users/{uid}/meals-today doc,
one users/{uid}/log entry per food, XP like the app (+5 per food, +50 once a day for hitting protein,
+50 once a day for calories within 10% of target) and the matching profile.totalXp increase.
Everything written is recorded in inbox_ledger.json so it can be undone exactly. Nothing else is touched.
"""

import datetime as dt
import glob
import json
import os
import queue
import secrets
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(HERE, "inbox")
DONE = os.path.join(INBOX, "done")
FAILED = os.path.join(INBOX, "failed")
LEDGER = os.path.join(HERE, "inbox_ledger.json")
FOODS = os.path.join(HERE, "qawi_foods.json")
FOODS_USER = os.path.join(HERE, "qawi_foods_user.json")
BARCODES = os.path.join(HERE, "barcodes.json")

TAGS = ("breakfast", "lunch", "dinner", "snack", "pre", "post")
TAG_AR = {"breakfast": "فطور", "lunch": "غداء", "dinner": "عشاء", "snack": "وجبة خفيفة",
          "pre": "قبل التمرين", "post": "بعد التمرين"}
MAX_ITEMS, MAX_GRAMS, MAX_KCAL_REQ = 25, 3000, 4000
XP_FOOD, XP_TARGET = 5, 50
USER_EXPORT_S = 600


def nanoid(n=21):
    abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(secrets.choice(abc) for _ in range(n))


def num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def r1(x):
    return round(x * 10) / 10


def from_grams(per100, grams):
    k = grams / 100
    return {key: r1(num(per100.get(key)) * k) for key in ("kcal", "protein", "carbs", "fat", "fiber")}


def time_label(ts):
    t = dt.datetime.fromtimestamp(ts)
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'AM' if t.hour < 12 else 'PM'}"


def check_per100(p):
    for k in ("kcal", "protein", "carbs", "fat", "fiber"):
        if num(p.get(k)) < 0:
            raise ValueError(f"negative {k}")
    if num(p.get("kcal")) > 950:
        raise ValueError("more than 950 kcal per 100 g is not food")
    if num(p.get("protein")) + num(p.get("carbs")) + num(p.get("fat")) > 105:
        raise ValueError("protein + carbs + fat over 100 g per 100 g")


class Inbox:
    def __init__(self, fit, on_result):
        """fit = island_fit.FitWorker; on_result(dict) is called from the worker thread."""
        self.fit = fit
        self.on_result = on_result
        self.q = queue.Queue()
        self.seen = set()
        self._seed, self._seed_mtime = {}, None
        self._user, self._user_at = {}, 0
        self.lock = threading.Lock()
        for d in (INBOX, DONE, FAILED):
            os.makedirs(d, exist_ok=True)
        threading.Thread(target=self._worker, daemon=True).start()

    # ------------------------------------------------------------------ food data
    def seed(self):
        try:
            mt = os.path.getmtime(FOODS)
            if mt != self._seed_mtime:
                with open(FOODS, encoding="utf-8") as f:
                    self._seed = {x["id"]: x for x in json.load(f)["foods"]}
                self._seed_mtime = mt
        except Exception as e:
            print("inbox: food list not readable:", e)
        return self._seed

    def user_foods(self):
        if not self._user:
            try:
                with open(FOODS_USER, encoding="utf-8") as f:
                    self._user = {x["id"]: x for x in json.load(f)["foods"]}
            except Exception:
                pass
        return self._user

    def export_user_foods(self):
        if not self.ready():
            return
        lib = self.fit.user().collection("library")
        foods = []
        for d in lib.stream():
            x = d.to_dict() or {}
            p = x.get("per100g") or {}
            name = x.get("name") or {}
            foods.append(dict(id=x.get("id") or d.id, en=name.get("en", ""), ar=name.get("ar", ""),
                              cat=x.get("category", ""), kcal=num(p.get("kcal")), p=num(p.get("protein")),
                              c=num(p.get("carbs")), f=num(p.get("fat")), fib=num(p.get("fiber")), src="user"))
        foods.sort(key=lambda x: x["en"].lower())
        tmp = FOODS_USER + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write('{"_about": "The user\'s own QAWIFit foods (library), exported by the island every 10 min. '
                    'Macros per 100 g.",\n "foods": [\n')
            f.write(",\n".join("  " + json.dumps(x, ensure_ascii=False) for x in foods))
            f.write("\n ]}\n")
        os.replace(tmp, FOODS_USER)
        self._user = {x["id"]: x for x in foods}
        self._user_at = time.time()

    def barcodes(self):
        try:
            with open(BARCODES, encoding="utf-8") as f:
                v = json.load(f)
            return v if isinstance(v, dict) else {}
        except Exception:
            return {}

    def remember_barcode(self, code, fid):
        code = "".join(ch for ch in str(code) if ch.isdigit())
        if not code or not fid:
            return
        m = self.barcodes()
        if m.get(code) != fid:
            m[code] = fid
            tmp = BARCODES + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(m, f, indent=1)
            os.replace(tmp, BARCODES)

    def resolve(self, item):
        """-> (ingredient in the app's shape, ingredientSource, estimated?) or ValueError."""
        bc = "".join(ch for ch in str(item.get("barcode") or "") if ch.isdigit())
        if bc and not item.get("food") and "custom" not in item:
            fid = self.barcodes().get(bc)
            if not fid:
                raise ValueError(f"barcode {bc} isn't linked to a food yet: send it once with \"food\" (or \"custom\" + "
                                 f"\"save\": true) and the barcode")
            item = dict(item, food=fid)
        if "custom" in item:
            c = item["custom"] or {}
            per = {k: num((c.get("per100g") or {}).get(k)) for k in ("kcal", "protein", "carbs", "fat", "fiber")}
            check_per100(per)
            en = str(c.get("en") or "").strip()
            if not en:
                raise ValueError("custom food needs a name")
            ing = dict(_v=1, id=nanoid(), name=dict(en=en, ar=str(c.get("ar") or "")), category=c.get("category", "composite"),
                       per100g=per, tags=["island"], source="user", createdAt=int(time.time() * 1000))
            return ing, "user", True
        fid = str(item.get("food") or "")
        x = self.seed().get(fid)
        src = "seed"
        if x is None:
            x = self.user_foods().get(fid)
            src = "user"
        if x is None and fid in getattr(self, "_recent_lib", {}):      # saved a moment ago, not exported yet
            ing = self._recent_lib[fid]
            p = ing["per100g"]
            x = dict(en=ing["name"]["en"], ar=ing["name"].get("ar", ""), kcal=p["kcal"], p=p["protein"], c=p["carbs"],
                     f=p["fat"], fib=p.get("fiber", 0))
        if x is None:
            raise ValueError(f"unknown food id {fid!r} (not in qawi_foods.json or qawi_foods_user.json)")
        per = dict(kcal=num(x["kcal"]), protein=num(x["p"]), carbs=num(x["c"]), fat=num(x["f"]), fiber=num(x.get("fib")))
        return dict(id=fid, name=dict(en=x.get("en", ""), ar=x.get("ar", "")), per100g=per), src, bool(x.get("est"))

    def preview(self, items):
        """Items with macros + totals, for the island's tick preview. Raises ValueError on bad items."""
        out, tot = [], dict(kcal=0.0, p=0.0, c=0.0, f=0.0)
        for it in items or []:
            ing, _src, est = self.resolve(it)
            g = num(it.get("grams"))
            m = from_grams(ing["per100g"], g)
            out.append(dict(name=ing["name"]["en"], grams=round(g), kcal=round(m["kcal"]), p=round(m["protein"]), est=est))
            tot["kcal"] += m["kcal"]; tot["p"] += m["protein"]; tot["c"] += m["carbs"]; tot["f"] += m["fat"]
        return dict(items=out, tot={k: round(v) for k, v in tot.items()})

    # ------------------------------------------------------------------ queue
    def ready(self):
        return self.fit.db is not None and self.fit.uid and self.fit.status == "ok" and self.fit.date

    def scan(self):
        """Called from the UI thread every couple of seconds; cheap."""
        for path in sorted(glob.glob(os.path.join(INBOX, "*.json"))):
            if path in self.seen:
                continue
            try:
                if time.time() - os.path.getmtime(path) < 0.7:   # still being written
                    continue
            except OSError:
                continue
            self.seen.add(path)
            self.q.put(("file", path))
        if self.ready() and time.time() - self._user_at > USER_EXPORT_S:
            self._user_at = time.time()
            self.q.put(("export", None))

    def submit(self, req, label="island"):
        """Queue a request built in-process (a meal tick). Result arrives through on_result."""
        self.q.put(("req", (req, label)))

    def _worker(self):
        while True:
            kind, arg = self.q.get()
            try:
                if kind == "export":
                    self.export_user_foods()
                elif kind == "file":
                    self._file(arg)
                elif kind == "req":
                    req, label = arg
                    try:
                        res = self.handle(req)
                    except Exception as e:
                        res = dict(ok=False, error=str(e)[:300], request=req.get("id"), type=req.get("type"))
                    res["from"] = label
                    self.on_result(res)
            except Exception as e:
                print("inbox worker:", type(e).__name__, str(e)[:200])

    def _file(self, path):
        name = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                req = json.load(f)
        except Exception as e:
            self._move(path, FAILED, {"error": f"not valid JSON: {e}"})
            return
        for _ in range(30):       # wait for QAWIFit to come up after a restart
            if self.ready():
                break
            time.sleep(2)
        try:
            res = self.handle(req)
            req["result"] = res
            self._move(path, DONE, req)
        except Exception as e:
            res = dict(ok=False, error=str(e)[:300], request=req.get("id"), type=req.get("type"))
            req["error"] = res["error"]
            self._move(path, FAILED, req)
        res["from"] = req.get("from", name)
        self.on_result(res)

    def _move(self, path, folder, content):
        dst = os.path.join(folder, os.path.basename(path))
        if os.path.exists(dst):
            dst = dst[:-5] + "-" + nanoid(5) + ".json"
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(content, f, ensure_ascii=False, indent=1)
        try:
            os.remove(path)
        except OSError as e:
            print("inbox: could not remove", path, e)

    # ------------------------------------------------------------------ ledger
    def ledger(self):
        try:
            with open(LEDGER, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_ledger(self, lg):
        tmp = LEDGER + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(lg, f, ensure_ascii=False, indent=1)
        os.replace(tmp, LEDGER)

    # ------------------------------------------------------------------ actions
    def handle(self, req):
        if not self.ready():
            raise RuntimeError("QAWIFit is not connected right now")
        t = req.get("type")
        if t == "food":
            return self.log_food(req)
        if t == "undo":
            return self.undo(str(req.get("target") or ""))
        if t == "weight":
            return self.log_weight(req)
        if t == "water":
            ml = int(num(req.get("ml")))
            if not ml or abs(ml) > 2000:
                raise ValueError("ml must be between -2000 and 2000 (not 0)")
            if not self.fit.add_water(self.fit.date, ml):
                raise RuntimeError("QAWIFit water isn't loaded for today yet")
            return dict(ok=True, type="water", request=str(req.get("id") or ""), ml=ml)
        raise ValueError(f"unknown request type {t!r} (use food, water, weight or undo)")

    def log_weight(self, req):
        """Body weight for today's training day, the way the app's Progress screen saves it."""
        from firebase_admin import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter
        rid = str(req.get("id") or "").strip() or ("weight-" + nanoid(8))
        with self.lock:
            old = self.ledger().get(rid)
            if old and not old.get("undone"):
                raise ValueError(f"request {rid!r} was already logged")
        kg = num(req.get("kg"))
        if not 30 <= kg <= 300:
            raise ValueError("weight must be between 30 and 300 kg")
        kg = round(kg, 2)
        f = self.fit
        user = f.user()
        date = f.date
        now_ms = int(time.time() * 1000)
        ref = user.collection("progress").document(date)
        snap = ref.get()
        prev = snap.to_dict() if snap.exists else None
        cutoff = int(num((f.profile or {}).get("dayCutoffHour")))
        start = dt.datetime.strptime(date, "%Y-%m-%d") + dt.timedelta(hours=cutoff)
        s_ms, e_ms = int(start.timestamp() * 1000), int((start + dt.timedelta(days=1)).timestamp() * 1000)
        have = any(num((d.to_dict() or {}).get("createdAt")) < e_ms and (d.to_dict() or {}).get("type") == "weight_logged"
                   for d in user.collection("xpEvents").where(filter=FieldFilter("createdAt", ">=", s_ms)).stream())
        batch = f.db.batch()
        if prev is not None:
            batch.update(ref, {"weightKg": kg, "loggedAt": now_ms})
        else:
            batch.set(ref, {"_v": 1, "date": date, "weightKg": kg, "loggedAt": now_ms})
        xp_ids, xp = [], 0
        if not have:
            ev = self._xp_event("weight_logged", 10, "Logged body weight", "تم تسجيل الوزن", now_ms,
                                dict(date=date, islandReq=rid))
            batch.set(user.collection("xpEvents").document(ev["id"]), ev)
            batch.update(user.collection("profile").document("data"), {"totalXp": firestore.Increment(10), "updatedAt": now_ms})
            xp_ids, xp = [ev["id"]], 10
        batch.commit()
        with self.lock:
            lg = self.ledger()
            lg[rid] = dict(kind="weight", date=date, existed=prev is not None,
                           prev_kg=(prev or {}).get("weightKg"), prev_at=(prev or {}).get("loggedAt"),
                           xp_ids=xp_ids, xp=xp, label=f"{kg:g} kg", kcal=0, at=now_ms, meal=None, entries=[])
            self.save_ledger(lg)
        print(f"inbox: weight {kg} kg for {date}" + (f" (+{xp} XP)" if xp else ""))
        return dict(ok=True, type="weight", request=rid, kg=kg, prev=(prev or {}).get("weightKg"), xp=xp, date=date)

    def log_food(self, req):
        from firebase_admin import firestore
        rid = str(req.get("id") or "").strip() or ("req-" + nanoid(8))
        with self.lock:
            old = self.ledger().get(rid)
            if old and not old.get("undone"):
                raise ValueError(f"request {rid!r} was already logged")
        items = req.get("items") or []
        if not items or len(items) > MAX_ITEMS:
            raise ValueError(f"need 1 to {MAX_ITEMS} items")
        meal = req.get("meal") or {}
        tag = meal.get("tag") if meal.get("tag") in TAGS else "snack"
        name_en = str(meal.get("name") or tag.capitalize())[:40]
        now = time.time()
        now_ms = int(now * 1000)
        comps, new_lib, codes, total = [], [], [], dict(kcal=0.0, protein=0.0, carbs=0.0, fat=0.0, fiber=0.0)
        for it in items:
            g = num(it.get("grams"))
            if not 0 < g <= MAX_GRAMS:
                raise ValueError(f"grams must be between 1 and {MAX_GRAMS}")
            ing, src, _est = self.resolve(it)
            if "custom" in it and it.get("save"):
                new_lib.append(ing)
            if it.get("barcode") and (it.get("food") or ("custom" in it and it.get("save"))):
                codes.append((it["barcode"], ing["id"]))
            computed = from_grams(ing["per100g"], g)
            for k in total:
                total[k] += computed[k]
            comps.append(dict(id=nanoid(), ingredientId=ing["id"], ingredientSource=src,
                              refSnapshot=dict(name=ing["name"], per100g=ing["per100g"]),
                              grams=round(g, 1), computed=computed, loggedAt=now_ms))
        if total["kcal"] > MAX_KCAL_REQ:
            raise ValueError(f"{round(total['kcal'])} kcal in one request is over the {MAX_KCAL_REQ} kcal safety cap")

        f, db = self.fit, self.fit.db
        user = f.user()
        date = f.date
        meal_id = nanoid()
        day_meal = dict(_v=1, id=meal_id, date=date, name=dict(en=name_en, ar=str(meal.get("ar") or TAG_AR.get(tag, ""))),
                        tag=tag, createdAt=now_ms, timeLabel=time_label(now), components=comps)
        batch = db.batch()
        batch.set(user.collection("meals-today").document(meal_id), day_meal)
        xp_ids, xp = [], 0
        for c in comps:
            batch.set(user.collection("log").document(c["id"]), dict(
                _v=1, id=c["id"], loggedAt=now_ms, serverTime=firestore.SERVER_TIMESTAMP, source="ingredient",
                refId=c["ingredientId"], refSnapshot=c["refSnapshot"], grams=c["grams"], computed=c["computed"],
                mealId=meal_id))
            ev = self._xp_event("meal_logged", XP_FOOD, "Logged meal", "تم تسجيل وجبة", now_ms,
                                dict(entryId=c["id"], islandReq=rid))
            batch.set(user.collection("xpEvents").document(ev["id"]), ev)
            xp_ids.append(ev["id"])
            xp += XP_FOOD
        for ing in new_lib:
            batch.set(user.collection("library").document(ing["id"]), ing)
        batch.update(user.collection("profile").document("data"),
                     {"totalXp": firestore.Increment(xp), "updatedAt": now_ms})
        batch.commit()
        for ing in new_lib:
            self._recent_lib = getattr(self, "_recent_lib", {})
            self._recent_lib[ing["id"]] = ing
        for code, fid in codes:
            self.remember_barcode(code, fid)
        entry = dict(date=date, meal=meal_id, entries=[c["id"] for c in comps], xp_ids=xp_ids, xp=xp,
                     lib=[i["id"] for i in new_lib], label=name_en, kcal=round(total["kcal"]), at=now_ms)
        with self.lock:
            lg = self.ledger()
            lg[rid] = entry
            self.save_ledger(lg)
        bonus = self._target_bonus(rid, date, now_ms)
        if bonus:
            with self.lock:
                lg = self.ledger()
                lg[rid]["xp_ids"] += [b["id"] for b in bonus]
                lg[rid]["xp"] += sum(b["amount"] for b in bonus)
                self.save_ledger(lg)
        day_tot = self._day_totals(date)
        tg = f.targets or {}
        print(f"inbox: logged {rid}: {name_en}, {len(comps)} foods, {round(total['kcal'])} kcal, +{xp + sum(b['amount'] for b in bonus)} XP")
        return dict(ok=True, type="food", request=rid, meal=name_en, foods=len(comps),
                    kcal=round(total["kcal"]), protein=round(total["protein"]), carbs=round(total["carbs"]),
                    fat=round(total["fat"]), xp=xp + sum(b["amount"] for b in bonus),
                    bonus=[b["type"] for b in bonus], saved_to_library=[i["name"]["en"] for i in new_lib],
                    day=dict(kcal=round(day_tot["kcal"]), protein=round(day_tot["protein"]),
                             kcal_left=round(num(tg.get("targetKcal")) - day_tot["kcal"]),
                             protein_left=round(num(tg.get("proteinG")) - day_tot["protein"])))

    def _xp_event(self, typ, amount, en, ar, now_ms, meta):
        return dict(_v=1, id=nanoid(), type=typ, amount=amount, description=dict(en=en, ar=ar),
                    createdAt=now_ms, meta=meta)

    def _day_totals(self, date):
        from google.cloud.firestore_v1.base_query import FieldFilter
        tot = dict(kcal=0.0, protein=0.0, carbs=0.0, fat=0.0)
        q = self.fit.user().collection("meals-today").where(filter=FieldFilter("date", "==", date))
        for d in q.stream():
            for c in (d.to_dict() or {}).get("components") or []:
                for k in tot:
                    tot[k] += num((c.get("computed") or {}).get(k))
        return tot

    def _target_bonus(self, rid, date, now_ms):
        """The app's once-a-day target XP, checked the same way (training-day window)."""
        from firebase_admin import firestore
        from google.cloud.firestore_v1.base_query import FieldFilter
        f = self.fit
        tg = f.targets or {}
        cutoff = int(num((f.profile or {}).get("dayCutoffHour")))
        start = dt.datetime.strptime(date, "%Y-%m-%d") + dt.timedelta(hours=cutoff)
        start_ms, end_ms = int(start.timestamp() * 1000), int((start + dt.timedelta(days=1)).timestamp() * 1000)
        user = f.user()
        have = set()
        q = user.collection("xpEvents").where(filter=FieldFilter("createdAt", ">=", start_ms))
        for d in q.stream():
            e = d.to_dict() or {}
            if num(e.get("createdAt")) < end_ms:
                have.add(e.get("type"))
        tot = self._day_totals(date)
        out = []
        if num(tg.get("proteinG")) > 0 and tot["protein"] >= num(tg.get("proteinG")) and "protein_target_hit" not in have:
            out.append(self._xp_event("protein_target_hit", XP_TARGET, "Hit daily protein target",
                                      "تحقيق هدف البروتين اليومي", now_ms, dict(islandReq=rid)))
        k = num(tg.get("targetKcal"))
        if k > 0 and k * 0.9 <= tot["kcal"] <= k * 1.1 and "kcal_target_hit" not in have:
            out.append(self._xp_event("kcal_target_hit", XP_TARGET, "Hit daily calorie target",
                                      "تحقيق هدف السعرات اليومي", now_ms, dict(islandReq=rid)))
        if out:
            batch = f.db.batch()
            for ev in out:
                batch.set(user.collection("xpEvents").document(ev["id"]), ev)
            batch.update(user.collection("profile").document("data"),
                         {"totalXp": firestore.Increment(sum(e["amount"] for e in out)), "updatedAt": now_ms})
            batch.commit()
        return out

    def undo(self, rid):
        """Remove exactly what one request wrote (meal, its log entries, its XP). Library foods stay."""
        from firebase_admin import firestore
        with self.lock:
            lg = self.ledger()
            e = lg.get(rid)
        if not e:
            raise ValueError(f"no logged request called {rid!r}")
        if e.get("undone"):
            raise ValueError(f"{rid!r} was already undone")
        user = self.fit.user()
        batch = self.fit.db.batch()
        if e.get("kind") == "weight":                  # put the previous weight back (or remove ours)
            ref = user.collection("progress").document(e["date"])
            if e.get("prev_kg") is not None:
                batch.update(ref, {"weightKg": e["prev_kg"], "loggedAt": e.get("prev_at") or int(time.time() * 1000)})
            elif e.get("existed"):
                batch.update(ref, {"weightKg": firestore.DELETE_FIELD})
            else:
                batch.delete(ref)
        else:
            batch.delete(user.collection("meals-today").document(e["meal"]))
        for i in e["entries"]:
            batch.delete(user.collection("log").document(i))
        for i in e["xp_ids"]:
            batch.delete(user.collection("xpEvents").document(i))
        if e["xp"]:
            batch.update(user.collection("profile").document("data"),
                         {"totalXp": firestore.Increment(-int(e["xp"])), "updatedAt": int(time.time() * 1000)})
        batch.commit()
        with self.lock:
            lg = self.ledger()
            lg[rid]["undone"] = int(time.time() * 1000)
            self.save_ledger(lg)
        print(f"inbox: undid {rid}")
        return dict(ok=True, type="undo", request=rid, meal=e.get("label"), kcal=e.get("kcal"), xp=-int(e["xp"]))

    def logged(self, rid):
        e = self.ledger().get(rid)
        return bool(e) and not e.get("undone")
