"""
island_life.py - life bits (round 6): deadlines, countdowns, to-do, habit streaks, word of the day.
life.json (island folder, the island's own; edit freely):
  {"deadlines": [{id, title, due, done}], "countdowns": [{id, title, date, tbc}], "todo": [{id, text, done}],
   "habits": [{id, name, days: ["YYYY-MM-DD", ...]}]}
"""

import datetime as dt
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LIFE_FILE = os.path.join(HERE, "life.json")

SEED = {"deadlines": [], "countdowns": [], "todo": [], "habits": []}

# word of the day: German (A2) and Mandarin (HSK 1-2), alternating by day
# (word with its article, meaning, plural / perfect / case hint, an A2 sentence, the sentence in English)
WORDS_DE = [
    ("die Erfahrung", "experience", "-en", "Ich habe schon viel Erfahrung mit Robotern.", "I already have a lot of experience with robots."),
    ("der Termin", "appointment", "-e", "Ich habe morgen um 10 Uhr einen Termin beim Arzt.", "I have a doctor's appointment tomorrow at 10."),
    ("die Prüfung", "exam", "-en", "Die Prüfung ist im Dezember.", "The exam is in December."),
    ("bestehen", "to pass (an exam)", "hat bestanden", "Ich will die Prüfung bestehen.", "I want to pass the exam."),
    ("sich bewerben", "to apply", "um + Akk · hat sich beworben", "Ich bewerbe mich um ein Stipendium in China.", "I'm applying for a scholarship in China."),
    ("die Ausbildung", "vocational training", "-en", "Mein Cousin macht eine Ausbildung zum Mechaniker.", "My cousin is training to be a mechanic."),
    ("das Studium", "university studies", "die Studien", "Mein Studium macht mir Spaß.", "I enjoy my studies."),
    ("der Unterricht", "lesson, class", "no plural", "Der Unterricht beginnt um acht Uhr.", "Class starts at eight."),
    ("die Hausaufgabe", "homework", "-n", "Ich mache meine Hausaufgaben nach dem Essen.", "I do my homework after eating."),
    ("wiederholen", "to repeat, revise", "hat wiederholt", "Heute wiederhole ich die Vokabeln.", "Today I'm revising the vocabulary."),
    ("vergessen", "to forget", "hat vergessen", "Ich habe meinen Schlüssel vergessen.", "I forgot my key."),
    ("sich erinnern an", "to remember", "+ Akk · hat sich erinnert", "Ich erinnere mich gut an meine Schulzeit.", "I remember my school days well."),
    ("die Gesundheit", "health", "no plural", "Sport ist gut für die Gesundheit.", "Sport is good for your health."),
    ("der Arzt", "doctor", "die Ärzte", "Wenn ich krank bin, gehe ich zum Arzt.", "When I'm sick, I go to the doctor."),
    ("sich fühlen", "to feel", "hat sich gefühlt", "Nach dem Training fühle ich mich super.", "After training I feel great."),
    ("müde", "tired", "adjective", "Nach der Uni bin ich oft müde.", "After uni I'm often tired."),
    ("die Bewegung", "exercise, movement", "-en", "Ich brauche mehr Bewegung.", "I need more exercise."),
    ("das Gewicht", "weight", "-e", "Mein Gewicht sinkt langsam.", "My weight is slowly going down."),
    ("abnehmen", "to lose weight", "hat abgenommen", "Ich möchte bis Januar drei Kilo abnehmen.", "I'd like to lose three kilos by January."),
    ("die Ernährung", "nutrition", "no plural", "Eine gute Ernährung ist wichtig.", "Good nutrition is important."),
    ("das Frühstück", "breakfast", "-e", "Zum Frühstück esse ich Eier.", "For breakfast I eat eggs."),
    ("der Bus", "bus", "die Busse", "Ich fahre mit dem Bus zur Uni.", "I take the bus to uni."),
    ("die Haltestelle", "bus stop", "-n", "Die Haltestelle ist direkt vor meinem Haus.", "The bus stop is right in front of my house."),
    ("umsteigen", "to change (trains/buses)", "ist umgestiegen", "In der Stadt muss ich einmal umsteigen.", "In the city I have to change once."),
    ("pünktlich", "on time", "adjective", "Der Bus kommt heute pünktlich.", "The bus is on time today."),
    ("die Verspätung", "delay", "-en", "Der Zug hat zehn Minuten Verspätung.", "The train is ten minutes late."),
    ("die Wohnung", "flat, apartment", "-en", "Unsere Wohnung hat vier Zimmer.", "Our flat has four rooms."),
    ("der Nachbar", "neighbour", "-n", "Unser Nachbar ist sehr nett.", "Our neighbour is very nice."),
    ("die Familie", "family", "-n", "Meine Familie ist groß.", "My family is big."),
    ("die Geschwister", "siblings", "plural only", "Hast du Geschwister?", "Do you have brothers or sisters?"),
    ("der Beruf", "profession", "-e", "Was sind Sie von Beruf?", "What do you do for a living?"),
    ("der Ingenieur", "engineer", "-e", "Ich möchte später Ingenieur werden.", "I want to become an engineer later."),
    ("verdienen", "to earn", "hat verdient", "Als Ingenieur verdient man gut.", "Engineers earn well."),
    ("das Ziel", "goal", "-e", "Mein Ziel ist ein Studium im Ausland.", "My goal is to study abroad."),
    ("der Wunsch", "wish", "die Wünsche", "Mein größter Wunsch ist eine Reise nach China.", "My biggest wish is a trip to China."),
    ("träumen von", "to dream of", "+ Dat · hat geträumt", "Ich träume von einem eigenen Auto.", "I dream of having my own car."),
    ("die Zukunft", "future", "no plural", "In der Zukunft möchte ich im Ausland arbeiten.", "In the future I'd like to work abroad."),
    ("ehrlich", "honest", "adjective", "Sei ehrlich: Hast du heute gelernt?", "Be honest: did you study today?"),
    ("geduldig", "patient", "adjective", "Mein Lehrer ist sehr geduldig.", "My teacher is very patient."),
    ("die Geduld", "patience", "no plural", "Deutsch lernen braucht Geduld.", "Learning German takes patience."),
    ("stolz auf", "proud of", "+ Akk", "Ich bin stolz auf meine Noten.", "I'm proud of my grades."),
    ("sich freuen auf", "to look forward to", "+ Akk · hat sich gefreut", "Ich freue mich auf das Wochenende.", "I'm looking forward to the weekend."),
    ("die Reise", "trip, journey", "-n", "Die Reise nach Berlin war toll.", "The trip to Berlin was great."),
    ("das Ausland", "abroad", "no plural", "Viele Studenten gehen ins Ausland.", "Many students go abroad."),
    ("die Sprache", "language", "-n", "Deutsch ist eine schwere Sprache.", "German is a hard language."),
    ("übersetzen", "to translate", "hat übersetzt", "Kannst du diesen Satz übersetzen?", "Can you translate this sentence?"),
    ("erklären", "to explain", "hat erklärt", "Der Lehrer erklärt die Grammatik.", "The teacher explains the grammar."),
    ("verstehen", "to understand", "hat verstanden", "Ich verstehe die Aufgabe nicht.", "I don't understand the task."),
    ("die Meinung", "opinion", "-en", "Meiner Meinung nach ist das richtig.", "In my opinion that's right."),
    ("vorschlagen", "to suggest", "hat vorgeschlagen", "Ich schlage vor, dass wir zusammen lernen.", "I suggest that we study together."),
    ("entscheiden", "to decide", "hat entschieden", "Das musst du selbst entscheiden.", "You have to decide that yourself."),
    ("die Entscheidung", "decision", "-en", "Das war eine gute Entscheidung.", "That was a good decision."),
]
WORDS_ZH = [
    ("学习 xuéxí", "to study"), ("考试 kǎoshì", "exam"), ("老师 lǎoshī", "teacher"), ("学生 xuésheng", "student"),
    ("大学 dàxué", "university"), ("中国 Zhōngguó", "China"), ("上海 Shànghǎi", "Shanghai"), ("工程师 gōngchéngshī", "engineer"),
    ("朋友 péngyou", "friend"), ("家 jiā", "home, family"), ("水 shuǐ", "water"), ("吃饭 chīfàn", "to eat (a meal)"),
    ("喝 hē", "to drink"), ("睡觉 shuìjiào", "to sleep"), ("今天 jīntiān", "today"), ("明天 míngtiān", "tomorrow"),
    ("昨天 zuótiān", "yesterday"), ("时间 shíjiān", "time"), ("现在 xiànzài", "now"), ("工作 gōngzuò", "work, job"),
    ("电脑 diànnǎo", "computer"), ("手机 shǒujī", "mobile phone"), ("书 shū", "book"), ("汉语 Hànyǔ", "Chinese language"),
    ("学校 xuéxiào", "school"), ("飞机 fēijī", "plane"), ("火车 huǒchē", "train"), ("天气 tiānqì", "weather"),
    ("热 rè", "hot"), ("冷 lěng", "cold"), ("高兴 gāoxìng", "happy"), ("谢谢 xièxie", "thank you"),
    ("对不起 duìbuqǐ", "sorry"), ("没关系 méi guānxi", "it's OK"), ("加油 jiāyóu", "keep going! / you can do it"),
    ("准备 zhǔnbèi", "to prepare"), ("努力 nǔlì", "to work hard"), ("运动 yùndòng", "sport, exercise"),
    ("身体 shēntǐ", "body, health"), ("梦想 mèngxiǎng", "dream"),
]


def word_of_day(d):
    n = d.toordinal()
    if n % 2 == 0:
        w, m, hint, ex, en = WORDS_DE[(n // 2) % len(WORDS_DE)]
        art, noun = (w.split(" ", 1) if w.split(" ", 1)[0] in ("der", "die", "das") else ("", w))
        if art == "die" and hint == "plural only":
            art = "pl"                                   # die Geschwister: plural, not feminine
        return dict(lang="German", word=w, mean=m, art=art, noun=noun, hint=hint, ex=ex, en=en)
    w, m = WORDS_ZH[(n // 2) % len(WORDS_ZH)]
    return dict(lang="Mandarin", word=w, mean=m)


def _load():
    try:
        with open(LIFE_FILE, encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else None
    except Exception:
        return None


class Life:
    def __init__(self):
        self.data = _load()
        if self.data is None:
            self.data = json.loads(json.dumps(SEED))
            self.save()
        for k, v in SEED.items():
            self.data.setdefault(k, [] if k != "habits" else json.loads(json.dumps(v)))

    def save(self):
        tmp = LIFE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, LIFE_FILE)

    @staticmethod
    def nid(prefix):
        return f"{prefix}{int(time.time() * 1000) % 10**9}"

    # ---- adds / ticks
    def add(self, kind, **kw):
        item = dict(id=self.nid(kind[0]), **kw)
        self.data[kind].append(item)
        self.save()
        return item

    def toggle(self, kind, iid, today=None):
        for x in self.data.get(kind, []):
            if x.get("id") != iid:
                continue
            if kind == "habits":
                d = today.isoformat()
                days = set(x.get("days", []))
                days.symmetric_difference_update({d})
                x["days"] = sorted(days)[-400:]
            else:
                x["done"] = not x.get("done")
                x["done_at"] = dt.date.today().isoformat() if x["done"] else None
            self.save()
            return x
        return None

    def remove(self, kind, iid):
        n = len(self.data.get(kind, []))
        self.data[kind] = [x for x in self.data.get(kind, []) if x.get("id") != iid]
        if len(self.data[kind]) != n:
            self.save()

    def clear_done(self):
        self.data["todo"] = [x for x in self.data["todo"] if not x.get("done")]
        self.save()

    @staticmethod
    def streak(days, today):
        s, d = set(days), today
        if d.isoformat() not in s:
            d -= dt.timedelta(days=1)          # today not ticked yet: the streak is still alive
        n = 0
        while d.isoformat() in s:
            n += 1
            d -= dt.timedelta(days=1)
        return n

    # ---- what the UI needs
    def snapshot(self, today):
        dl = []
        for x in self.data["deadlines"]:
            try:
                due = dt.date.fromisoformat(x["due"])
            except (KeyError, ValueError):
                continue
            days = (due - today).days
            if x.get("done") and days < -1:
                continue                          # finished and gone
            if days > 45 and not x.get("done"):
                continue                          # far away: not yet
            dl.append(dict(id=x["id"], title=x.get("title", ""), days=days, done=bool(x.get("done")),
                           date=due.strftime("%a %d %b").replace(" 0", " ")))
        dl.sort(key=lambda x: (x["done"], x["days"]))
        cds = []
        for x in self.data["countdowns"]:
            try:
                d = dt.date.fromisoformat(x["date"])
            except (KeyError, ValueError):
                continue
            if (d - today).days >= 0:
                cds.append(dict(id=x["id"], title=x.get("title", ""), days=(d - today).days, tbc=bool(x.get("tbc")),
                                date=d.strftime("%d %b %Y").lstrip("0")))
        cds.sort(key=lambda x: x["days"])
        hb = [dict(id=x["id"], name=x.get("name", ""), today=today.isoformat() in x.get("days", []),
                   streak=self.streak(x.get("days", []), today)) for x in self.data["habits"]]
        return dict(deadlines=dl, countdowns=cds, todo=[dict(id=x["id"], text=x.get("text", ""), done=bool(x.get("done")))
                                                        for x in self.data["todo"]][-20:],
                    habits=hb, word=word_of_day(today))


def split_date(text, today, parse_date):
    """'Lab report 12 nov' -> ('Lab report', date) using the last 1 or 2 words as the date."""
    words = text.strip().split()
    for k in (2, 1):
        if len(words) > k:
            d = parse_date(" ".join(words[-k:]), today)
            if d:
                return " ".join(words[:-k]).strip(" :-·"), d
    for k in (1,):
        m = re.match(r"^(\d+)\s*d(ays?)?$", words[-1]) if words else None
        if m and len(words) > 1:
            return " ".join(words[:-1]), today + dt.timedelta(days=int(m.group(1)))
    return None, None
