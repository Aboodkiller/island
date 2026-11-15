"""Laptop temperatures + fan speeds from the ASUS ACPI device (the same way G-Helper reads them, no admin needed).

ROG / TUF laptops expose the embedded controller's sensors through \\\\.\\ATKACPI: a DSTS call with a sensor id
returns its value + 65536. CPU 0x00120094, GPU 0x00120097 (°C), fans 0x00110013 / 0x00110014 (x100 rpm).
Read-only: DSTS only asks for a status, it never sets anything.
"""
import ctypes
import ctypes.wintypes as W
import struct
import sys

WIN = sys.platform == "win32"
CPU, GPU, CPU_FAN, GPU_FAN = 0x00120094, 0x00120097, 0x00110013, 0x00110014
DSTS, IOCTL = 0x53545344, 0x0022240C

if WIN:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = W.HANDLE
    k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, ctypes.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    k32.DeviceIoControl.argtypes = [W.HANDLE, W.DWORD, ctypes.c_void_p, W.DWORD, ctypes.c_void_p, W.DWORD,
                                    ctypes.POINTER(W.DWORD), ctypes.c_void_p]
    k32.CloseHandle.argtypes = [W.HANDLE]


class AsusSensors:
    def __init__(self):
        self.h = None
        self.ok = False
        if not WIN:
            return
        h = k32.CreateFileW(r"\\.\ATKACPI", 0x80000000 | 0x40000000, 0x1 | 0x2, None, 3, 0x80, None)
        if h and h != ctypes.c_void_p(-1).value:
            self.h, self.ok = h, True

    def get(self, dev):
        buf = struct.pack("<IIII", DSTS, 8, dev, 0)
        out = ctypes.create_string_buffer(16)
        got = W.DWORD(0)
        if not k32.DeviceIoControl(self.h, IOCTL, buf, len(buf), out, 16, ctypes.byref(got), None):
            return None
        return struct.unpack_from("<i", out.raw, 0)[0] - 65536

    def read(self):
        """{'cpu': 63, 'gpu': 55 or None, 'cpu_fan': 3400 or None, 'gpu_fan': ...}"""
        if not self.ok:
            return None
        t = lambda v: v if v is not None and 0 < v < 125 else None
        def f(v):
            if v is None:
                return None
            n = v & 0xFFFF
            return None if n > 120 or (n == 0 and v < 0) else n * 100
        return dict(cpu=t(self.get(CPU)), gpu=t(self.get(GPU)), cpu_fan=f(self.get(CPU_FAN)), gpu_fan=f(self.get(GPU_FAN)))

    def close(self):
        if self.h:
            k32.CloseHandle(self.h)
            self.h, self.ok = None, False
