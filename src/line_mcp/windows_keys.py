"""Bounded read-only key discovery in this user's exact LINE executable.

No process dumps, injection, debugger attachment, or key logging. Keys stay in memory.
The historical marker is version-dependent, so absence is not proof of no key.
"""
from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import wintypes
from pathlib import Path


def find_keys(line_root: Path, max_bytes: int = 768 * 1024 * 1024, timeout: float = 25) -> tuple[list[str], dict]:
    if os.name != "nt":
        return [], {"status": "windows_only"}
    import psutil

    expected = (line_root / "bin").resolve()
    processes = []
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = Path(proc.info["exe"] or "").resolve()
            if proc.info["name"].lower() == "line.exe" and exe.is_relative_to(expected):
                processes.append(proc.info["pid"])
        except (OSError, AttributeError, psutil.Error):
            continue

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    kernel.ReadProcessMemory.restype = wintypes.BOOL

    class MBI(ctypes.Structure):
        _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                    ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                    ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                    ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]

    kernel.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(MBI), ctypes.c_size_t]
    kernel.VirtualQueryEx.restype = ctypes.c_size_t
    pattern = re.compile(rb"encryption_?key\s*[=:]\s*[\"']?([0-9a-fA-F]{32})(?![0-9a-fA-F])", re.I)
    keys: set[str] = set()
    scanned = 0
    started = time.monotonic()
    denied = 0
    for pid in processes:
        handle = kernel.OpenProcess(0x0400 | 0x0010, False, pid)
        if not handle:
            denied += 1
            continue
        try:
            address = 0
            while scanned < max_bytes and time.monotonic() - started < timeout:
                mbi = MBI()
                if not kernel.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(mbi), ctypes.sizeof(mbi)):
                    break
                base = mbi.BaseAddress or 0
                end = base + mbi.RegionSize
                if end <= address:
                    break
                # Committed, readable, non-executable private/mapped data only.
                if mbi.State == 0x1000 and not mbi.Protect & (0x100 | 0x01) and (mbi.Protect & 0xFF) in (0x02, 0x04, 0x08):
                    position, tail = base, b""
                    while position < end and scanned < max_bytes and time.monotonic() - started < timeout:
                        size = min(1024 * 1024, end - position, max_bytes - scanned)
                        buf = ctypes.create_string_buffer(size)
                        read = ctypes.c_size_t()
                        ok = kernel.ReadProcessMemory(handle, ctypes.c_void_p(position), buf, size, ctypes.byref(read))
                        scanned += size
                        if ok and read.value:
                            data = tail + buf.raw[:read.value]
                            for match in pattern.finditer(data):
                                keys.add(match.group(1).decode("ascii"))
                            # Also support the UTF-16 representation of the same marker.
                            if b"e\x00n\x00c\x00r\x00y\x00p\x00t\x00i\x00o\x00n\x00" in data:
                                for alignment in (0, 1):
                                    decoded = data[alignment:].decode("utf-16-le", errors="ignore").encode("ascii", errors="ignore")
                                    for match in pattern.finditer(decoded):
                                        keys.add(match.group(1).decode("ascii"))
                            tail = data[-256:]
                        else:
                            tail = b""
                        position += size
                address = end
        finally:
            kernel.CloseHandle(handle)
    return sorted(keys), {"process_count": len(processes), "access_denied": denied,
                          "scanned_bytes": scanned, "candidate_count": len(keys),
                          "bounded": scanned >= max_bytes or time.monotonic() - started >= timeout}
