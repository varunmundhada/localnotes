"""Database key protected by Windows DPAPI.

The 256-bit key that encrypts the SQLite database is itself encrypted with
CryptProtectData, which ties it to this Windows user account: copying the
data folder to another account or machine leaves it unreadable.
"""
import ctypes
import secrets
from ctypes import wintypes
from pathlib import Path

CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.windll.crypt32
_kernel32 = ctypes.windll.kernel32
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]


def _call(fn, data: bytes, *extra) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    inp = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    if not fn(ctypes.byref(inp), *extra, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(data: bytes) -> bytes:
    return _call(_crypt32.CryptProtectData, data, ctypes.c_wchar_p("LocalNotes database key"))


def unprotect(data: bytes) -> bytes:
    return _call(_crypt32.CryptUnprotectData, data, None)


def load_or_create_key(path: Path) -> bytes:
    if path.exists():
        return unprotect(path.read_bytes())
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(protect(key))
    return key
