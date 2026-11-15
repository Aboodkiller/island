"""Battery of 2.4 GHz dongle mice and keyboards, read-only, no extra packages (Windows' own hid.dll via ctypes).

Runs as a child process of the island: prints one JSON line whenever a level changes (and every minute),
lines starting with "#" are notes for island.log. If it ever crashes, only this process goes.

  Attack Shark mice (Beken receiver 1D57:FA60 and friends): passive. The mouse itself sends
    [03, model, 40, state, level, ...] every so often; nothing is written to it. X6 reports 1..10 (x10).
  VIA keyboards on 36B0 receivers (URX Core75 and relatives): on the raw-HID channel (usage page FF60 / 61)
    the "custom get value 7" query [08 00 07] answers [08 00 07 level]. A read request, it changes nothing.

Protocols from the MIT / open-source projects incconutwo/mouse-battery-tray and HaloContinued (HaloBattery fork).
"""
import ctypes
import ctypes.wintypes as W
import json
import re
import sys
import threading
import time

AGAIN = "--again" in sys.argv     # restarted after a fullscreen game: the learning scan already ran this island start

setupapi = ctypes.WinDLL("setupapi", use_last_error=True)
hiddll = ctypes.WinDLL("hid", use_last_error=True)
k32 = ctypes.WinDLL("kernel32", use_last_error=True)

INVALID = ctypes.c_void_p(-1).value
GENERIC_RW = 0x80000000 | 0x40000000
SHARE_RW = 0x1 | 0x2
OPEN_EXISTING = 3
OVERLAPPED_FLAG = 0x40000000
IO_PENDING = 997


class GUID(ctypes.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD), ("Data3", W.WORD), ("Data4", ctypes.c_ubyte * 8)]


class IFDATA(ctypes.Structure):
    _fields_ = [("cbSize", W.DWORD), ("InterfaceClassGuid", GUID), ("Flags", W.DWORD), ("Reserved", ctypes.c_void_p)]


class ATTRS(ctypes.Structure):
    _fields_ = [("Size", W.ULONG), ("VendorID", W.USHORT), ("ProductID", W.USHORT), ("VersionNumber", W.USHORT)]


class CAPS(ctypes.Structure):
    _fields_ = [("Usage", W.USHORT), ("UsagePage", W.USHORT), ("InputReportByteLength", W.USHORT),
                ("OutputReportByteLength", W.USHORT), ("FeatureReportByteLength", W.USHORT),
                ("Reserved", W.USHORT * 17), ("NumberLinkCollectionNodes", W.USHORT),
                ("NumberInputButtonCaps", W.USHORT), ("NumberInputValueCaps", W.USHORT), ("NumberInputDataIndices", W.USHORT),
                ("NumberOutputButtonCaps", W.USHORT), ("NumberOutputValueCaps", W.USHORT), ("NumberOutputDataIndices", W.USHORT),
                ("NumberFeatureButtonCaps", W.USHORT), ("NumberFeatureValueCaps", W.USHORT), ("NumberFeatureDataIndices", W.USHORT)]


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p), ("Offset", W.DWORD),
                ("OffsetHigh", W.DWORD), ("hEvent", W.HANDLE)]


setupapi.SetupDiGetClassDevsW.restype = ctypes.c_void_p
setupapi.SetupDiGetClassDevsW.argtypes = [ctypes.POINTER(GUID), W.LPCWSTR, W.HWND, W.DWORD]
setupapi.SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(GUID), W.DWORD, ctypes.POINTER(IFDATA)]
setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.POINTER(IFDATA), ctypes.c_void_p, W.DWORD,
                                                      ctypes.POINTER(W.DWORD), ctypes.c_void_p]
setupapi.SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]
k32.CreateFileW.restype = W.HANDLE
k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
k32.CreateEventW.restype = W.HANDLE
k32.CreateEventW.argtypes = [ctypes.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR]
k32.ReadFile.argtypes = [W.HANDLE, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.POINTER(OVERLAPPED)]
k32.WriteFile.argtypes = [W.HANDLE, ctypes.c_void_p, W.DWORD, ctypes.POINTER(W.DWORD), ctypes.POINTER(OVERLAPPED)]
k32.GetOverlappedResult.argtypes = [W.HANDLE, ctypes.POINTER(OVERLAPPED), ctypes.POINTER(W.DWORD), W.BOOL]
k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
k32.WaitForSingleObject.restype = W.DWORD
k32.CancelIo.argtypes = [W.HANDLE]
k32.CloseHandle.argtypes = [W.HANDLE]
hiddll.HidD_GetHidGuid.argtypes = [ctypes.POINTER(GUID)]
hiddll.HidD_GetAttributes.argtypes = [W.HANDLE, ctypes.POINTER(ATTRS)]
hiddll.HidD_GetAttributes.restype = ctypes.c_ubyte           # BOOLEAN: only the low byte is meaningful
hiddll.HidD_GetPreparsedData.argtypes = [W.HANDLE, ctypes.POINTER(ctypes.c_void_p)]
hiddll.HidD_GetPreparsedData.restype = ctypes.c_ubyte
hiddll.HidD_FreePreparsedData.restype = ctypes.c_ubyte
hiddll.HidD_GetProductString.argtypes = [W.HANDLE, ctypes.c_void_p, W.ULONG]
hiddll.HidD_GetProductString.restype = ctypes.c_ubyte
hiddll.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
hiddll.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.POINTER(CAPS)]
hiddll.HidP_GetCaps.restype = ctypes.c_long

SHARK_PIDS = {0xFA60, 0xFA62, 0xFA65, 0xFA55, 0xFA56, 0xFA61, 0xFA50}
SHARK_WIRED = {0xFA55, 0xFA56, 0xFA61, 0xFA50}
SHARK_MODELS = {0x55: "Attack Shark X11", 0x10: "Attack Shark mouse", 0x85: "Attack Shark X6", 0x4D: "Attack Shark X3",
                0xBE: "Attack Shark X11 Pro", 0x07: "Attack Shark X11 SE"}
VIA_VIDS = {0x36B0}


def note(*a):
    print("#", *a, flush=True)


def interfaces():
    """[{path, vid, pid, page, usage, inlen, outlen, mi}] for every HID collection that's present."""
    g = GUID()
    hiddll.HidD_GetHidGuid(ctypes.byref(g))
    hdev = setupapi.SetupDiGetClassDevsW(ctypes.byref(g), None, None, 0x02 | 0x10)
    if not hdev or hdev == INVALID:
        return []
    out = []
    try:
        i = 0
        while True:
            d = IFDATA()
            d.cbSize = ctypes.sizeof(IFDATA)
            if not setupapi.SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(g), i, ctypes.byref(d)):
                break
            i += 1
            need = W.DWORD(0)
            setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(d), None, 0, ctypes.byref(need), None)
            if not need.value:
                continue
            buf = ctypes.create_string_buffer(need.value + 4)
            ctypes.cast(buf, ctypes.POINTER(W.DWORD))[0] = 8 if ctypes.sizeof(ctypes.c_void_p) == 8 else 6
            if not setupapi.SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(d), buf, need, None, None):
                continue
            path = ctypes.wstring_at(ctypes.addressof(buf) + 4)
            info = describe(path)
            if info:
                out.append(info)
    finally:
        setupapi.SetupDiDestroyDeviceInfoList(hdev)
    return out


def describe(path):
    h = k32.CreateFileW(path, 0, SHARE_RW, None, OPEN_EXISTING, 0, None)     # no access needed just to look
    if not h or h == INVALID:
        return None
    try:
        a = ATTRS()
        a.Size = ctypes.sizeof(ATTRS)
        if not hiddll.HidD_GetAttributes(h, ctypes.byref(a)):
            return None
        info = dict(path=path, vid=a.VendorID, pid=a.ProductID, page=0, usage=0, inlen=0, outlen=0)
        pp = ctypes.c_void_p()
        if hiddll.HidD_GetPreparsedData(h, ctypes.byref(pp)):
            c = CAPS()
            if hiddll.HidP_GetCaps(pp, ctypes.byref(c)) == 0x00110000:
                info.update(page=c.UsagePage, usage=c.Usage, inlen=c.InputReportByteLength, outlen=c.OutputReportByteLength)
            hiddll.HidD_FreePreparsedData(pp)
        nb = ctypes.create_unicode_buffer(128)
        info["name"] = nb.value if hiddll.HidD_GetProductString(h, nb, ctypes.sizeof(nb)) else ""
        m = re.search(r"&mi_([0-9a-f]{2})", path, re.I)
        info["mi"] = int(m.group(1), 16) if m else -1
        return info
    finally:
        k32.CloseHandle(h)


def open_rw(path):
    h = k32.CreateFileW(path, GENERIC_RW, SHARE_RW, None, OPEN_EXISTING, OVERLAPPED_FLAG, None)
    return None if not h or h == INVALID else h


def _io(h, fn, buf, n, ms):
    ov = OVERLAPPED()
    ov.hEvent = k32.CreateEventW(None, True, False, None)
    got = W.DWORD(0)
    try:
        if not fn(h, buf, n, ctypes.byref(got), ctypes.byref(ov)):
            if ctypes.get_last_error() != IO_PENDING:
                return None
            if k32.WaitForSingleObject(ov.hEvent, ms) != 0:
                k32.CancelIo(h)
                k32.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(got), True)     # let the cancel finish
                return 0
            if not k32.GetOverlappedResult(h, ctypes.byref(ov), ctypes.byref(got), False):
                return None
        return got.value
    finally:
        k32.CloseHandle(ov.hEvent)


def read(h, n, ms):
    """b'' on timeout, None if the device went away."""
    buf = ctypes.create_string_buffer(n)
    got = _io(h, k32.ReadFile, buf, n, ms)
    return None if got is None else buf.raw[:got]


def write(h, data, ms=400):
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    return _io(h, k32.WriteFile, buf, len(data), ms)


# --------------------------------------------------------------------------- the readers
STATE = {}
LOCK = threading.Lock()


def publish(key, name, level, charging=None):
    with LOCK:
        old = STATE.get(key)
        new = dict(name=name, v=None if level is None else int(level), chg=charging, at=int(time.time()))
        STATE[key] = new
        if not old or old["v"] != new["v"] or old.get("chg") != charging or old["name"] != name:
            print(json.dumps(dict(dev=STATE)), flush=True)


def shark(info, stop):
    """Attack Shark: sit on the receiver's vendor channel and catch the battery packets it sends by itself."""
    h = open_rw(info["path"])
    if not h:
        return
    note(f"shark: listening on {info['vid']:04X}:{info['pid']:04X} mi {info['mi']} page {info['page']:04X} ({info.get('name', '')})")
    n_raw = [0]
    try:
        while not stop.is_set():
            d = read(h, max(65, info["inlen"] or 65), 1500)
            if d is None:
                break
            if len(d) >= 5 and d[0] == 0x03 and d[2] == 0x40:      # Beken battery packet (media keys etc. never match)
                model = SHARK_MODELS.get(d[1], "Attack Shark mouse")
                if n_raw[0] < 6:
                    n_raw[0] += 1
                    note("shark packet", " ".join(f"{b:02x}" for b in d[:10]))
                if d[1] in (0x10, 0x85):                   # coarse models: 1..10 = 10..100 %, no charging flag
                    if 0 < d[4] <= 10:
                        publish("mouse", model, d[4] * 10, None)
                elif d[3] == 2:                            # on the dock, full
                    publish("mouse", model, 100, True)
                elif d[3] == 1 and 0 < d[4] <= 100:
                    publish("mouse", model, d[4], False)
                elif d[3] in (3, 0x80) and "mouse" in STATE:   # charging, level not sent: keep the last one
                    publish("mouse", STATE["mouse"]["name"], STATE["mouse"]["v"], True)
    finally:
        k32.CloseHandle(h)


def via(info, stop):
    """VIA keyboards: ask the receiver for value 7 (battery) on the raw-HID channel, every minute."""
    n = info["outlen"] or 33
    n_via = [0]
    scanned = [AGAIN]
    while not stop.is_set():
        h = open_rw(info["path"])
        if not h:
            return
        try:
            while read(h, max(33, info["inlen"]), 30):            # anything already waiting
                pass
            if write(h, bytes([0, 8, 0, 7]) + bytes(max(0, n - 4))) is None:
                return
            t0 = time.time()
            while time.time() - t0 < 1.5:
                d = read(h, max(33, info["inlen"]), 250)
                if d is None:
                    return
                r = d[1:] if d[:1] == b"\x00" else d
                if r and n_via[0] < 4:
                    n_via[0] += 1
                    note(f"via {info['pid']:04X} reply", " ".join(f"{b:02x}" for b in r[:12]))
                if len(r) >= 4 and r[0] == 8 and r[1] == 0 and r[2] == 7 and 1 <= r[3] <= 100:
                    publish("keyboard", "Keyboard", r[3])
                    STATE["keyboard"]["src"] = info["pid"]
                    break
        finally:
            k32.CloseHandle(h)
        if not scanned[0]:
            scanned[0] = True
            scan(info)
        stop.wait(60)


def scan(info):
    """Once: what this receiver answers to VIA's read-only queries (protocol version, keyboard values, custom values),
    and anything it sends by itself, to learn where its battery is. Only "get" commands."""
    h = open_rw(info["path"])
    if not h:
        return
    n = info["outlen"] or 33
    seen = set()
    try:
        qs = [[0x01], [0x02, 0x01], [0x02, 0x04]] + [[0x08, ch, i] for ch in range(0, 5) for i in range(1, 12)]
        for q in qs:
            if write(h, bytes([0] + q) + bytes(max(0, n - 1 - len(q)))) is None:
                return
            t0 = time.time()
            while time.time() - t0 < 0.25:
                d = read(h, max(33, info["inlen"]), 120)
                if d is None:
                    return
                if not d:
                    continue
                r = d[1:] if d[:1] == b"\x00" else d
                key = bytes(r[:8])
                if r and r[0] != 0xFF and key not in seen:
                    seen.add(key)
                    note(f"via {info['pid']:04X} {' '.join(f'{b:02x}' for b in q)} -> {' '.join(f'{b:02x}' for b in r[:10])}")
        t0 = time.time()                       # 15 s of listening for reports it sends by itself
        while time.time() - t0 < 15 and len(seen) < 40:
            d = read(h, max(33, info["inlen"]), 500)
            if d is None:
                return
            if d and bytes(d[:8]) not in seen:
                seen.add(bytes(d[:8]))
                note(f"via {info['pid']:04X} sent by itself: {' '.join(f'{b:02x}' for b in d[:12])}")
    finally:
        k32.CloseHandle(h)


def listen(info, stop):
    """Once per start, 40 s: the keyboard receiver's other vendor channels, logging the reports they send by themselves
    (that's usually where a 2.4 GHz dongle reports the battery). Read-only: it never writes to them."""
    h = open_rw(info["path"])
    if not h:
        note(f"listen {info['pid']:04X} page {info['page']:04X}: can't open")
        return
    note(f"listen {info['pid']:04X} mi {info['mi']} page {info['page']:04X} usage {info['usage']:04X} in {info['inlen']} out {info['outlen']}")
    seen = set()
    try:
        t0 = time.time()
        while not stop.is_set() and time.time() - t0 < 40 and len(seen) < 25:
            d = read(h, max(65, info["inlen"] or 65), 1000)
            if d is None:
                break
            if d and bytes(d[:10]) not in seen:
                seen.add(bytes(d[:10]))
                note(f"listen {info['pid']:04X} page {info['page']:04X} got: {' '.join(f'{b:02x}' for b in d[:16])}")
    finally:
        k32.CloseHandle(h)


def _parent_watch():
    try:
        sys.stdin.read()
    except Exception:
        pass
    import os
    os._exit(0)


def main():
    threading.Thread(target=_parent_watch, daemon=True).start()
    stop = threading.Event()
    threads = []
    found = interfaces()
    note(f"{len(found)} HID collections")
    heads = {(f["vid"], f["pid"]) for f in found if (f["vid"], f["pid"]) == (0x040B, 0x0897) or "headset" in f.get("name", "").lower()}
    if heads:                       # there, but its dongle doesn't say its battery (Redragon H848): shown without a level
        publish("headset", "Headset", None)
    for f in found:
        if f["vid"] == 0x1D57 and f["pid"] in SHARK_PIDS:
            if f["pid"] in SHARK_WIRED:
                publish("mouse", "Attack Shark (cable)", 100, True)
                continue
            if (f["page"] == 0x01 and f["usage"] in (0x02, 0x06, 0x80)) or f["page"] == 0x0C:   # mouse / keys / media: not ours
                continue
            threads.append(threading.Thread(target=shark, args=(f, stop), daemon=True))
        elif f["vid"] in VIA_VIDS and f["page"] >= 0xFF00 and f["page"] != 0xFF60:
            if AGAIN:
                continue
            threads.append(threading.Thread(target=listen, args=(f, stop), daemon=True))     # learning: what else it says
        elif f["vid"] in VIA_VIDS and f["page"] == 0xFF60 and f["usage"] == 0x61:
            note(f"via: asking {f['vid']:04X}:{f['pid']:04X} mi {f['mi']} (out {f['outlen']}, in {f['inlen']}) {f.get('name', '')}")
            threads.append(threading.Thread(target=via, args=(f, stop), daemon=True))
            if "keyboard" not in STATE:
                publish("keyboard", "Keyboard", None)
    if not threads:
        note("no supported receiver found")
    for t in threads:
        t.start()
    last = time.time()
    while any(t.is_alive() for t in threads) or not threads:
        time.sleep(5)
        if time.time() - last > 60:
            last = time.time()
            with LOCK:
                print(json.dumps(dict(dev=STATE)), flush=True)
        if not threads and time.time() - last > 55:
            break
    note("all readers stopped")


if __name__ == "__main__":
    if "--list" in sys.argv:            # what's there, for debugging
        for f in interfaces():
            print(f"{f['vid']:04X}:{f['pid']:04X} mi {f['mi']:>2} page {f['page']:04X} usage {f['usage']:04X} in {f['inlen']} out {f['outlen']}  {f.get('name', '')}")
    else:
        main()
