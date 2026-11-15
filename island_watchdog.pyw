"""
island_watchdog.pyw - keeps the island alive.

Runs island2.py as a child process (with ISLAND_WATCHDOG=1) and:
  - exit code 0  -> you chose Quit (or another island was already running): the watchdog exits too
  - exit code 3  -> "Restart island": started again at once
  - anything else (a crash) -> island.log is saved as crash_last.log and the island is started again
  - no heartbeat.txt update for 90 s while the PC is awake -> the island is frozen: killed and restarted
  - 5 crashes within 5 minutes -> stops and shows a message instead of looping

island2.py hands itself over to this file automatically (unless island_settings.json has "watchdog": false),
so the Startup shortcut does not need changing. Its own log: watchdog.log.
"""

import ctypes
import datetime as dt
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ISLAND = os.path.join(HERE, "island2.py")
HEARTBEAT = os.path.join(HERE, "heartbeat.txt")
LOG = os.path.join(HERE, "island.log")
CRASH = os.path.join(HERE, "crash_last.log")
WLOG = os.path.join(HERE, "watchdog.log")
NO_WINDOW = 0x08000000
FROZEN_S = 90          # heartbeat older than this = frozen (the island writes it every 10 s)
GRACE_S = 150          # after a start / wake-up from sleep: give it time first


def log(msg):
    try:
        if os.path.exists(WLOG) and os.path.getsize(WLOG) > 200_000:
            with open(WLOG, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-200:]
            with open(WLOG, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(WLOG, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass


def only_one():
    """A named mutex: a second watchdog just exits."""
    try:
        ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\abood-island-watchdog")
        return ctypes.windll.kernel32.GetLastError() != 183      # ERROR_ALREADY_EXISTS
    except Exception:
        return True


def message(text):
    try:
        ctypes.windll.user32.MessageBoxW(None, text, "Island", 0x30)
    except Exception:
        pass


def run_once(reason):
    env = dict(os.environ, ISLAND_WATCHDOG="1")
    args = [sys.executable, ISLAND] + (["--after", reason] if reason else [])
    try:
        os.remove(HEARTBEAT)
    except OSError:
        pass
    started = time.time()
    p = subprocess.Popen(args, cwd=HERE, env=env, creationflags=NO_WINDOW)
    log(f"started island pid {p.pid}" + (f" after: {reason}" if reason else ""))
    last_loop, grace_until = started, started + GRACE_S
    while True:
        try:
            return p.wait(timeout=5), False
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        if now - last_loop > 30:              # this loop didn't run for a while: the PC was asleep
            grace_until = now + GRACE_S
            log(f"woke up after {int(now - last_loop)} s")
        last_loop = now
        if now < grace_until:
            continue
        try:
            age = now - os.path.getmtime(HEARTBEAT)
        except OSError:
            age = now - started
        if age > FROZEN_S:
            log(f"no heartbeat for {int(age)} s -> island frozen, killing pid {p.pid}")
            p.kill()
            try:
                p.wait(15)
            except Exception:
                pass
            return None, True


def main():
    if not only_one():
        return
    log("watchdog start")
    crashes, reason = [], None
    while True:
        code, froze = run_once(reason)
        reason = None
        if not froze and code == 0:
            log("island quit normally -> watchdog exits")
            return
        if not froze and code == 3:
            log("restart requested")
            continue
        why = "froze" if froze else f"crash{code}"
        try:
            shutil.copyfile(LOG, CRASH)
        except Exception:
            pass
        now = time.time()
        crashes = [t for t in crashes if now - t < 300] + [now]
        log(f"island {why}; log saved to crash_last.log ({len(crashes)} in 5 min)")
        if len(crashes) >= 5:
            log("5 crashes in 5 minutes -> giving up")
            message("The island crashed 5 times in 5 minutes, so the watchdog stopped restarting it.\n\n"
                    "crash_last.log in the island folder has the details. Start it again from the Startup "
                    "shortcut or: pythonw island2.py")
            return
        time.sleep(2 if len(crashes) < 3 else 20)
        reason = why


if __name__ == "__main__":
    main()
