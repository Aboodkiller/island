"""island_update.py - keeps a shared copy of the island up to date from GitHub (Aboodkiller/island).

Only CODE is replaced (island*.py, ui.html, fonts, setup files, the AI guide). Your own files are never touched:
plans/, every .json (settings, life, study, cards, launch...), sounds/, logs. A file that gets replaced is first copied
to backups/update-<version>/ (nothing is deleted). Missing plan templates are added once.
After an update the island's own file watcher offers the usual Restart; ui.html changes show at once.
Works with or without git (it downloads the zip from GitHub).
"""
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = "Aboodkiller/island"
BRANCH = "main"
STATE = os.path.join(HERE, "update.json")
MARK = os.path.join(HERE, "SHARED_COPY")       # only copies installed from GitHub have it; the original never updates
UA = {"User-Agent": "island-updater", "Accept": "application/vnd.github+json"}
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _code_file(rel):
    """True for files the update may replace."""
    r = rel.replace("\\", "/")
    if r.startswith(("plans/", "sounds/", "backups/", "inbox/", "lyrics_cache/", "models/", "_to_delete/")):
        return False
    base = r.rsplit("/", 1)[-1]
    if base.endswith(".json") or base.endswith(".log") or base in ("secrets.json", "firebase-key.json"):
        return False
    if r.startswith(("fonts/", "extras/")):
        return True
    return (base.startswith("island") and base.endswith((".py", ".pyw"))) or base in (
        "ui.html", "qrcode.js", "check_qt.py", "setup.bat", "requirements.txt", "README.md", "AI_SETUP.md",
        "deck-icon.png", "nosleep.mp4", ".gitignore")


def _get(url, timeout=20):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def local_version():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f).get("sha")
    except Exception:
        return None


def latest_version():
    j = json.loads(_get(f"https://api.github.com/repos/{REPO}/commits/{BRANCH}").decode("utf-8"))
    return j["sha"], (j.get("commit") or {}).get("message", "").split("\n")[0][:120]


def shared():
    return os.path.exists(MARK)


def check_and_update(force=False):
    """-> dict(status='uptodate'|'updated'|'error', changed=[...], msg=...). Safe to run in a thread."""
    if not shared():
        return dict(status="error", msg="this is the original island (no SHARED_COPY file): it doesn't update", changed=[])
    try:
        sha, msg = latest_version()
    except Exception as e:
        return dict(status="error", msg=f"can't reach GitHub ({e})", changed=[])
    if sha == local_version() and not force:
        return dict(status="uptodate", msg="", changed=[])
    try:
        z = zipfile.ZipFile(io.BytesIO(_get(f"https://codeload.github.com/{REPO}/zip/refs/heads/{BRANCH}", 60)))
    except Exception as e:
        return dict(status="error", msg=f"download failed ({e})", changed=[])
    root = z.namelist()[0].split("/")[0] + "/"
    changed, added, req_changed = [], [], False
    bdir = os.path.join(HERE, "backups", f"update-{sha[:7]}")
    for info in z.infolist():
        if info.is_dir() or not info.filename.startswith(root):
            continue
        rel = info.filename[len(root):]
        if not rel or ".." in rel.split("/"):
            continue
        dst = os.path.join(HERE, *rel.split("/"))
        data = z.read(info)
        if rel.startswith("plans/") and rel.endswith(".json"):
            if not os.path.exists(dst):                      # a template you don't have yet; yours are never replaced
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(dst, "wb") as f:
                    f.write(data)
                added.append(rel)
            continue
        if not _code_file(rel):
            continue
        try:
            with open(dst, "rb") as f:
                if f.read() == data:
                    continue
            os.makedirs(os.path.dirname(os.path.join(bdir, rel)), exist_ok=True)
            shutil.copy2(dst, os.path.join(bdir, rel))       # the old one, kept
        except FileNotFoundError:
            pass
        os.makedirs(os.path.dirname(dst) or HERE, exist_ok=True)
        tmp = dst + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
        changed.append(rel)
        req_changed |= rel == "requirements.txt"
    if req_changed:            # new packages only: never upgrades PySide6 under a running island
        try:
            subprocess.run([sys.executable.replace("pythonw.exe", "python.exe"), "-m", "pip", "install", "--quiet", "-r",
                            os.path.join(HERE, "requirements.txt")], timeout=900, creationflags=NOWIN)
        except Exception as e:
            print("update: pip:", e)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(dict(sha=sha, msg=msg, at=time.strftime("%Y-%m-%d %H:%M")), f, indent=1)
    return dict(status="updated" if (changed or added) else "uptodate", msg=msg, changed=changed + added)


if __name__ == "__main__":
    print(check_and_update(force="--force" in sys.argv))
