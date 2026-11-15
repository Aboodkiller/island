"""Checks that PySide6's Qt WebEngine files are all there.

An interrupted pip upgrade (or an antivirus) can remove Qt6WebEngineCore.dll; then Windows shows
"QtWebEngineProcess.exe - System Error: Qt6WebEngineCore.dll was not found" and the island can't draw.
Exit code 0 = fine, 1 = broken (setup.bat then reinstalls PySide6 clean).
"""
import os
import sys

try:
    import PySide6
except Exception as e:
    print("PySide6 is not installed:", e)
    sys.exit(1)

root = os.path.dirname(PySide6.__file__)
missing = []
if sys.platform == "win32":
    exe_dir = None
    for d, _dirs, files in os.walk(root):
        if "QtWebEngineProcess.exe" in files:
            exe_dir = d
            break
    if exe_dir is None:
        missing.append("QtWebEngineProcess.exe")
    elif not os.path.exists(os.path.join(exe_dir, "Qt6WebEngineCore.dll")) \
            and not os.path.exists(os.path.join(root, "Qt6WebEngineCore.dll")):
        missing.append("Qt6WebEngineCore.dll")
try:
    from PySide6 import QtWebEngineCore, QtWebEngineWidgets  # noqa: F401
except Exception as e:
    missing.append(f"Qt WebEngine module ({e})")

if missing:
    print("PySide6 in", root, "is missing:", ", ".join(missing))
    sys.exit(1)
print("Qt WebEngine ok (PySide6", PySide6.__version__ + ")")
