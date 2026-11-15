# AI_SETUP.md — instructions for the AI setting up Island

You (Claude or another assistant) are helping a new user install **Island** on their Windows PC and fill it with
**their own** life: classes, study plan, gym, meals, apps, deadlines. Island is a Dynamic-Island-style widget at the
top of the screen (Python + PySide6/QtWebEngine host, all visuals in `ui.html`). It was built for someone else and
handed over clean: every plan file here is a **template**, so the job is to replace them with this user's data.

## 0. Ground rules
- Talk casually and keep it short. Ask a few questions at a time, not a form.
- **Back up a file before you edit it** (`copy plans\study.json plans\study.json.bak`). Don't delete the user's files.
- Plan files are plain JSON. After editing, check that they still parse (`python -c "import json;json.load(open('plans/study.json',encoding='utf-8'))"`).
- Island reloads plan files within ~20 s, and `ui.html` live-reloads. If you change a `.py` file, the island shows
  an "updated" toast; the user clicks Restart (or it restarts itself after 10 idle minutes).
- Don't install anything that needs a paid account. Don't turn on the site blocker (it needs admin) unless asked.

## 1. Install (Windows 10/11)
1. Python 3.12+ from python.org, with **"Add python.exe to PATH"** ticked. Check: `python --version`.
2. Put this folder somewhere permanent (e.g. `Documents\island`). Don't run it from Downloads or a zip.
3. Double-click `setup.bat`. It installs `requirements.txt`, adds Island to Startup and starts it.
4. Not showing? Run `python island2.py` in a terminal to see the error. Black box instead of a pill: `pythonw island2.py --gl`
   (or `--software`). Logs: `island.log`, `crash_last.log`, `watchdog.log`.
5. Uninstall from startup: `python island2.py --remove-startup`.

## 2. Interview the user, then fill the files
Ask about these, in roughly this order, and write each answer into the matching file.

| Ask about | Write to |
|---|---|
| Uni / school timetable, trips to uni, online classes | `plans/health.json` → `schedule` |
| Meals (times, rough contents), daily water goal, vitamins | `plans/health.json` → `meals`, `water`, `vitamins`, `targets` |
| Gym days, time, split and exercises | `plans/health.json` → `workouts` |
| Subjects being studied, exam date, study times per weekday, the list of lessons | `plans/study.json` |
| Individual exam dates per subject | `study.json` (`exams`) |
| Apps / sites they open all the time | `launch.json` |
| Deadlines, to-dos, habits, countdowns | `life.json` |
| City (weather + prayer times) | `CITY` in `island2.py` (line ~67) |

Keys for weekdays everywhere: **0=Mon 1=Tue 2=Wed 3=Thu 4=Fri 5=Sat 6=Sun**. Times are 24-hour `"HH:MM"` in the
files (the island shows 12-hour). Times earlier than `day_starts` (04:00) count as the previous day, so `"00:30"` = tonight.
Each file has a `_help` field that explains it — read it.

### plans/health.json (day plan)
- `schedule[]`: fixed blocks, shown on Today, not tickable.
  `{"id": "phys", "kind": "class", "cat": "U", "title": "Physics II", "where": "H304", "days": ["0","2"], "start": "10:00", "end": "11:30", "remind": true}`
  `cat`: `U` class, `T` trip, `E` break. `kind`: class | commute | online | break. `remind: true` = notification 10 min before + at start.
- `meals[]`: `{"id": "m1", "name": "Breakfast", "time": "09:00", "text": "eggs + oats"}`; optional
  `"by_day": {"0": {"time": "07:30", "text": "..."}}` overrides one weekday.
- `water`: `goal_ml`, `window` [start, end], `remind_every_min`, `chips_ml` (the quick-add buttons).
- `vitamins[]`: `{"id": "d3", "name": "Vitamin D3", "dose": "2000 IU", "time": "14:00", "note": "with lunch"}`.
- `workouts`: `gym_days`, `optional_days`, `time`, `length_min`, `cycle` (rolling queue of session ids — the next
  undone one shows on each gym day; skipping a day never skips a session), `sessions` {id: {name, exercises:[{id, name, sets, rir, load}]}},
  optional `walk` {text, sub, days}.
- `targets`: kcal / protein_g / fat_g / carbs_g (display only).
- `log`: written by the island (ticks, water). Leave it alone.

### plans/study.json (study plan)
- `exam_day` (main countdown), `mock_start` (switches `rhythm` from `content` to `mock`).
- `rhythm.content` / `rhythm.mock`: `{weekday: [subject keys]}` — which subjects run that day, in order.
- `times`: `{weekday: ["16:00", "19:00"]}` — start time of each of that day's blocks (same order as rhythm).
- `hours`: block length per subject (`"2h"`, `"1.5h"`, `"45m"`).
- `subjects`: `{key: {"name": "...", "items": [{"id": "P01", "text": "Unit 1 lesson 1", "phase": "content", "done": null}]}}`.
  The island shows the first item with `done: null`; ticking sets `done` to the date. Break the user's syllabus into
  sessions that fit one block each.
- **Subject keys are fixed by the code:** `P` Physics, `M` Maths, `B` Biology, `H` History, `R` Review (colours, focus timer,
  mock lengths). For other subjects, reuse a key and just change `name`, or edit `SUBJECT_EN`/`SUBJECT_CAT` in
  `island_core.py`, `CAT_NAME` in `island2.py` and `EXAM_MIN` in `island_study.py` together.
- `checkpoints`: `{"2026-12-01": "note shown that day"}`.

### Island's own files (created on first run; edit freely)
- `study.json`: `{"exams": {"Physics": "2027-01-10"}, "chats": {"P": "https://claude.ai/chat/..."}, "papers": {}}` — `chats`
  adds a button that opens that subject's chat.
- `life.json`: `{"deadlines": [{"id","title","due","done"}], "countdowns": [{"id","title","date","tbc"}], "todo": [{"id","text","done"}], "habits": [{"id","name","days":[]}]}`.
  The user can also type these into the command bar (Win+Alt+Space): `deadline <what> <date>`, `todo <text>`, `habit <name>`, `countdown <what> <date>`.
- `launch.json`: `[{"name": "Spotify", "path": "C:\\...\\Spotify.exe"}, {"name": "YouTube", "url": "https://youtube.com"}]` (max 10).
- `cards.json`: flash cards `{"cards": [{"id","cat","front","back","kind":"formula","box":1,"due":"YYYY-MM-DD"}]}`.
  It starts with Tawjihi physics/maths formula cards; replace them with the user's own or empty the list.
- `island_settings.json`: settings (the gear tab in the panel edits them; defaults live in `island_feats.py` `DEFAULTS`).

## 3. Things that are specific to the original owner — adapt or switch off
- **"days to Tawjihi"** text (Jordan's school-leaving exam). If the user isn't doing Tawjihi, change that label in
  `ui.html` and `island2.py` (search "Tawjihi") to their exam's name, or just "days to exams".
- **QAWI / Body tab** (calories, rank, food logging) talks to a private app with the owner's server key. It is not
  included and won't work here; those tiles stay empty without data. Remove the `qawi` / `protein` / `rank`
  widgets if they show up.
- **Prayer times + adhan** use Jordan's Awqaf method (`method=23` in `island_day.py`) and `CITY`. Change both for another
  country, or turn "Prayer times" off in settings. Drop an `prayer.mp3` into `sounds\` for a real adhan.
- **Word of the day** (German/Mandarin) lives in `island_life.py` — swap the word lists for the user's language or ignore.
- **Game guard** during study blocks: `GAME_EXES` in `island_study.py` — add the user's games.
- **Laptop temps widget** reads ASUS ACPI; on other brands it just stays empty.
- **Voice commands**: off by default (needs `vosk` models, not included).

## 4. Optional extras
- **Spotify lyrics/likes through Spicetify:** install Spicetify, copy `extras\spicetify\island-bridge.js` into
  `%APPDATA%\spicetify\Extensions\`, run `spicetify config extensions island-bridge.js` then `spicetify apply`.
  Without it, lyrics come from LRCLIB and the player still works with any app via Windows media controls.
- **Claude Desktop can read their day:** the island writes `today.json` and ships `island_mcp.py` (stdio MCP server, no
  extra packages). On first run it writes `mcp_snippet.json` with the right paths — merge it into Claude Desktop's
  `claude_desktop_config.json`.
- **Phone deck:** Settings › "Phone & tablet deck", then scan the QR with a phone on the same Wi-Fi.
- **GitHub/Netlify panel:** `secrets.json` in this folder: `{"github_token": "...", "netlify_token": "...", "netlify_site": "..."}`.
  Never commit or share that file.

## 5. Show the user how to use it
- Hover the pill = quick peek · click = Today panel · right-click = menu · scroll = volume · drag to a side edge = side island.
- Hotkeys: Win+Alt+I panel · Win+Alt+Space command bar · Win+Alt+W +250 ml water · Win+Alt+P music player ·
  Win+Alt+T timer · Win+Alt+M mic mute · Win+Alt+H private mode · Win+Alt+D desktop widgets.
  (If a hotkey is taken, Ctrl+Alt+Shift+same key.)
- Desktop widgets sit on the wallpaper; the widget menu has an edit mode to add/remove/drag them.
- Music player: mini ↔ big with the expand/shrink buttons, X or Esc closes.
