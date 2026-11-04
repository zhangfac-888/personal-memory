"""Windows-only, fail-closed MCP installer and ordinary-file exclusion."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path


class FileInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("created", wintypes.FILETIME),
        ("accessed", wintypes.FILETIME), ("written", wintypes.FILETIME),
        ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD)]


class Disposition(ctypes.Structure):
    _fields_ = [("delete", ctypes.c_ubyte)]


def kernel():
    if os.name != "nt":
        raise RuntimeError("MCP configuration writes require Windows exclusive access")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    signatures = {
        "CreateMutexW": ([ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
        "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
        "ReleaseMutex": ([wintypes.HANDLE], wintypes.BOOL),
        "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        "GetDriveTypeW": ([wintypes.LPCWSTR], wintypes.UINT),
        "CreateFileW": ([wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE], wintypes.HANDLE),
        "GetFileInformationByHandle": ([wintypes.HANDLE, ctypes.POINTER(FileInfo)], wintypes.BOOL),
        "SetFilePointerEx": ([wintypes.HANDLE, ctypes.c_longlong, ctypes.POINTER(ctypes.c_longlong), wintypes.DWORD], wintypes.BOOL),
        "ReadFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        "WriteFile": ([wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p], wintypes.BOOL),
        "SetEndOfFile": ([wintypes.HANDLE], wintypes.BOOL),
        "FlushFileBuffers": ([wintypes.HANDLE], wintypes.BOOL),
        "SetFileInformationByHandle": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(api, name)
        function.argtypes, function.restype = arguments, result
    return api


def checked(success):
    if not success:
        # Error codes are sufficient; never include config/credential contents.
        raise OSError("Windows MCP exclusive access failed (code %d)" % ctypes.get_last_error())


@contextmanager
def host_lock(profile, host):
    identity = os.path.normcase(str(Path(profile).resolve())) + "\0" + host
    name = "Global\\PersonalMemory.MCP." + hashlib.sha256(identity.encode("utf8")).hexdigest()
    api = kernel()
    handle = api.CreateMutexW(None, False, name)
    checked(handle)
    owned = False
    try:
        result = api.WaitForSingleObject(handle, 0)
        owned = result in (0, 0x80)
        if result == 0x80:
            raise ValueError("Abandoned MCP installation lock; inspect recovery evidence")
        if result != 0:
            raise ValueError("MCP installation is busy or cannot be locked")
        yield
    finally:
        if owned:
            api.ReleaseMutex(handle)
        api.CloseHandle(handle)


class ExclusiveFile:
    """Same-handle reads/writes; excludes ordinary opens, delete and replacement."""
    def __init__(self, path, before):
        self.path = Path(path).absolute()
        self.created = before is None
        self.api = kernel()
        self.handle = None
        if self.api.GetDriveTypeW(self.path.anchor) != 3:
            raise ValueError("MCP writes require a local fixed drive")
        # Ancestor directories must be stable, ordinary local directories.
        for parent in self.path.parents:
            if parent.lstat().st_file_attributes & 0x400:
                raise ValueError("MCP reparse ancestor is unsupported")
        # Opening a reparse point rather than following it permits safe rejection.
        handle = self.api.CreateFileW(str(self.path), 0xC0010000, 0, None,
            1 if self.created else 3, 0x00200000, None)
        if handle == ctypes.c_void_p(-1).value:
            checked(False)
        self.handle = handle

    def validate(self, before):
        info = FileInfo()
        checked(self.api.GetFileInformationByHandle(self.handle, ctypes.byref(info)))
        if info.attributes & (0x400 | 0x10) or info.links != 1:
            raise ValueError("Unsupported MCP reparse, directory or multi-link target")
        self.identity = (info.volume, info.index_high, info.index_low)
        if self.read_bytes() != (b"" if before is None else before):
            raise ValueError("MCP configuration changed during repair")

    def seek_start(self):
        checked(self.api.SetFilePointerEx(self.handle, 0, None, 0))

    def read_bytes(self):
        self.seek_start()
        chunks = []
        buffer = ctypes.create_string_buffer(65536)
        while True:
            count = wintypes.DWORD()
            checked(self.api.ReadFile(self.handle, buffer, len(buffer), ctypes.byref(count), None))
            if not count.value:
                break
            chunks.append(buffer.raw[:count.value])
        return b"".join(chunks)

    def write_bytes(self, data):
        self.seek_start()
        position = 0
        while position < len(data):
            block = data[position:position + 65536]
            buffer = ctypes.create_string_buffer(block)
            count = wintypes.DWORD()
            checked(self.api.WriteFile(self.handle, buffer, len(block), ctypes.byref(count), None))
            if not count.value:
                raise OSError("MCP exclusive write made no progress")
            position += count.value
        checked(self.api.SetEndOfFile(self.handle))
        checked(self.api.FlushFileBuffers(self.handle))
        if self.read_bytes() != data:
            raise OSError("MCP exclusive write verification failed")

    def delete_created(self):
        if not self.created:
            raise ValueError("Only transaction-created files may be deleted")
        disposition = Disposition(1)
        checked(self.api.SetFileInformationByHandle(self.handle, 4,
            ctypes.byref(disposition), ctypes.sizeof(disposition)))

    def close(self):
        if self.handle is not None:
            handle, self.handle = self.handle, None
            checked(self.api.CloseHandle(handle))
