# AI_SETUP.md — instructions for the AI setting up Island

You (Claude or another assistant) are helping a new user install **Island** on their Windows PC and fill it with
**their own** life: classes, study plan, gym, meals, apps, deadlines. Island is a Dynamic-Island-style widget at the
top of the screen (Python + PySide6/QtWebEngine host, all visuals in `ui.html`). It was built for someone else and
handed over clean: every plan file here is a **template**, so the job is to replace them with this user's data.

## 0. Ground rules
- Talk casually and keep it short. Ask a few questions at a time, not a form.
- **Back up a file before you edit it** (`copy plans\study.json plans\study.json.bak`). Don't delete the user's files.
- Plan files are plain JSON. After editing, check that they still parse (`python -c "import json;json.load(open('plans/study.json',encoding='utf-8'))"`).
- **Never edit the island's code for personal stuff** (island*.py, ui.html): it updates itself from GitHub and code
  edits get replaced (the old file is kept in `backups/update-<version>/`). Everything personal goes in JSON files:
  `plans/`, `island_config.json`, `life.json`, `launch.json`, `study.json`, `cards.json`, `island_settings.json`.
- Island reloads plan files within ~20 s, and `ui.html` live-reloads. If you change a `.py` file, the island shows
  an "updated" toast; the user clicks Restart (or it restarts itself after 10 idle minutes).
- Don't install anything that needs a paid account. Don't turn on the site blocker (it needs admin) unless asked.
- **The user may have ADHD: keep it simple.** Island starts in **Simple mode** (fewer tabs, short settings list, a
  one-time "4 things" card, fewer pop-ups). Don't switch on extra features or notifications unless they ask. Add things
  one at a time, show them where it appeared, and stop when they've had enough — they can come back later.

## 1. Install (Windows 10/11)
1. Python 3.12+ from python.org, with **"Add python.exe to PATH"** ticked. Check: `python --version`.
2. Put this folder somewhere permanent, **outside OneDrive**: `C:\island` is best (the island rewrites small files every
   few seconds and OneDrive would sync them constantly). Don't run it from Downloads or a zip.
3. Double-click `setup.bat`. It installs `requirements.txt`, adds Island to Startup and starts it.
4. **"QtWebEngineProcess.exe - Qt6WebEngineCore.dll was not found"**: PySide6 got half-installed (usually pip upgrading
   it while an island was running) or the antivirus took the DLL. Close the island and run `setup.bat` again: it checks
   with `check_qt.py` and reinstalls PySide6 clean. Still missing = restore it from the antivirus quarantine and add an
   exception for the PySide6 folder. Never start the island while pip is still installing.
   Not showing at all? Run `python island2.py` in a terminal to see the error. Black box instead of a pill: `pythonw island2.py --gl`
   (or `--software`). Logs: `island.log`, `crash_last.log`, `watchdog.log`.
5. Uninstall from startup: `python island2.py --remove-startup`.
6. **Updates:** this is a shared copy (`SHARED_COPY` file). It checks GitHub every 6 hours and swaps in new code
   (never plans, settings or data), then shows a Restart toast. Settings › Island › "Check for updates" does it now;
   "Update by itself" turns the automatic check off. `python island_update.py` does the same from a terminal.

## 2. Interview the user, then fill the files
Ask about these, in roughly this order, and write each answer into the matching file.

| Ask about | Write to |
|---|---|
| Uni / school timetable, trips to uni, online classes | `plans/health.json` → `schedule` |
| Meals (times, rough contents), daily water goal, vitamins | `plans/health.json` → `meals`, `water`, `vitamins`, `targets` |
| Gym days, time, split and exercises | `plans/health.json` → `workouts` |
| The ONE big thing they're counting down to (exam, trip, launch, graduation…) | command bar: `big <what> <date>` → `life.json` `big` |
| Subjects being studied, study times per weekday, the list of lessons (only if they study) | `plans/study.json` |
| Individual exam dates | command bar: `exam <name> <date>` → `study.json` `exams` |
| Apps / sites they open all the time | `launch.json` |
| Deadlines, to-dos, habits, countdowns | `life.json` |
| City (weather + prayer times) | `island_config.json` → `"city": ["Amman", 31.95, 35.93]` (name, lat, lon), restart |

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

### plans/study.json (study plan) — starts EMPTY
While `subjects` is empty the island has no study features at all (the Study tab shows "No study plan yet").
`_example` in the file shows a small filled-in plan. Fill it only if the user studies.
- `exam_name` + `exam_day`: the big countdown when the user hasn't picked one with `big`. `mock_start` (optional)
  switches `rhythm` from `content` to `mock` on that date. No date at all = subjects show "no exam date" instead of pace.
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

### The big countdown
One number at the top of the panel: "N days to <thing>". It is `life.json` → `"big": {"title": "Graduation", "date": "2027-06-30"}`,
or, if that has no date, the study plan's `exam_day` titled by `exam_name` (or by `big.title`). Nothing set = a
"Count down to something" button. The user can change it any time: `big trip to Turkey 20 jul`, `big off`, or tap one of
their countdown chips in the Today tab.

### Island's own files (created on first run; edit freely)
- `study.json`: `{"exams": {"Calculus": "2027-01-10"}, "chats": {"P": "https://claude.ai/chat/..."}, "papers": {}}` — `exams`
  only lists what's set (command: `exam calculus 10 jan`); `chats` adds a button that opens that subject's chat.
- `life.json`: `{"deadlines": [{"id","title","due","done"}], "countdowns": [{"id","title","date","tbc"}], "todo": [{"id","text","done"}], "habits": [{"id","name","days":[]}]}`.
  The user can also type these into the command bar (Win+Alt+Space): `deadline <what> <date>`, `todo <text>`, `habit <name>`, `countdown <what> <date>`.
- `launch.json`: `[{"name": "Spotify", "path": "C:\\...\\Spotify.exe"}, {"name": "YouTube", "url": "https://youtube.com"}]` (max 10).
- `cards.json`: flash cards `{"cards": [{"id","cat","front","back","kind":"formula","box":1,"due":"YYYY-MM-DD"}]}`.
  It starts empty. The user adds mistakes with `wrong physics question = answer` (they come back after 1, 3, 7 days).
- `island_settings.json`: settings (the gear tab in the panel edits them; defaults live in `island_feats.py` `DEFAULTS`).

## 3. Things that are specific to the original owner — adapt or switch off
- **QAWI / Body tab** (calories, rank, food logging) talks to a private app with the owner's server key. It is not
  included and won't work here; those tiles stay empty without data. Remove the `qawi` / `protein` / `rank`
  widgets if they show up.
- **Prayer times + adhan** use Jordan's Awqaf method and Amman. Another place: `island_config.json` →
  `"city": [...]` and `"prayer_method": <aladhan.com method number>`, or turn "Prayer times" off in settings. Drop an `prayer.mp3` into `sounds\` for a real adhan.
- **Word of the day** (German/Mandarin) lives in `island_life.py` — swap the word lists for the user's language or ignore.
- **Game guard** during study blocks: `GAME_EXES` in `island_study.py` — add the user's games.
- **Laptop temps widget** reads ASUS ACPI; on other brands it just stays empty.
- **Voice commands**: off by default (needs `vosk` models, not included).

## 3b. RAM (16 GB laptops)
Measured on the original PC: the island uses about **250–450 MB of real RAM** most of the time (Task Manager's
"Memory" column across python + 2–3 QtWebEngineProcess), creeping toward ~1 GB before the idle trim hands it back.
On PCs with less than 20 GB it switches to **low-RAM mode by itself** (logged as `ram: 16 GB -> low-RAM mode` in
`island.log`): trims every 5 min instead of 10, restarts the page process past 1.5 GB instead of 2.5 GB, and when
Windows is 85 %+ full it gives its memory back right away. Every 10 min `island.log` prints a `mem:` line with the numbers.
If the user wants it lighter still (Settings, gear tab):
- **AI brain: Off** (default here). Fast/Smart load a 2.7–3.4 GB Ollama model for 10 min after each use. Don't install it on 16 GB.
- **Voice: off** (default). Whisper/Vosk models take 0.3–1 GB while on.
- **Desktop widgets: off** saves the second page process (~50–100 MB).
- Don't open the Dashboard window unless they have a second monitor (another page process).
- Keep **Trim RAM when idle** on.

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
The "4 things" card on the Today tab covers the basics; tap "Got it" and it's gone. Simple mode is in
Settings › Island (off = every tab and setting). Settings is one page with the everyday stuff; the **Dev mode**
switch at its bottom shows every setting (in groups, with search) and the System tab.
- Hover the pill = quick peek · click = Today panel · right-click = menu · scroll = volume · drag to a side edge = side island.
- Hotkeys: Win+Alt+I panel · Win+Alt+Space command bar · Win+Alt+W +250 ml water · Win+Alt+P music player ·
  Win+Alt+T timer · Win+Alt+M mic mute · Win+Alt+H private mode · Win+Alt+D desktop widgets.
  (If a hotkey is taken, Ctrl+Alt+Shift+same key.)
- Desktop widgets sit on the wallpaper; the widget menu has an edit mode to add/remove/drag them.
- Music player: mini ↔ big with the expand/shrink buttons, X or Esc closes.
