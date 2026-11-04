"""Windows machine-bound DPAPI storage; never include this store in a release."""
import ctypes
from ctypes import wintypes
from pathlib import Path


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def transform(data, *, decrypt=False):
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    flags = 1 if decrypt else 5  # UI forbidden; use machine scope for a background process.
    if not function(ctypes.byref(source), None, None, None, None, flags, ctypes.byref(target)):
        raise OSError("Windows credential encryption failed")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel.LocalFree(ctypes.cast(target.data, ctypes.c_void_p))


def credential(data_dir, env_name="DEEPSEEK_API_KEY"):
    import os
    path = Path(data_dir) / "secrets" / "deepseek-key.dpapi"
    if env_name == "DEEPSEEK_API_KEY" and path.exists():
        try:
            return transform(path.read_bytes(), decrypt=True).decode("utf8")
        except (OSError, UnicodeError):
            return None  # A broken stored key must never silently fall back to another key.
    return os.environ.get(env_name)
