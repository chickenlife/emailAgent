"""Encrypt IMAP credentials for the current Windows user using DPAPI.
Never store the password or encryption key in plaintext."""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

BASE = Path(os.getenv('LOCALAPPDATA', str(Path.home()))) / 'MyAISecretaryMail'
CRED_FILE = BASE / 'credentials.dpapi'

class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes):
    buf = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte))), buf


def _crypt(data: bytes, protect: bool):
    if os.name != 'nt':
        raise RuntimeError('Credential encryption requires Windows DPAPI.')
    in_blob, keep = _blob(data)
    out_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(DATA_BLOB), wintypes.LPCWSTR, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                          ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptProtectData.restype = wintypes.BOOL
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                            ctypes.POINTER(DATA_BLOB)]
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    if protect:
        ok = crypt32.CryptProtectData(ctypes.byref(in_blob), 'MailAgent credentials', None, None, None, 0,
                                      ctypes.byref(out_blob))
    else:
        ok = crypt32.CryptUnprotectData(ctypes.byref(in_blob), None, None, None, None, 0,
                                        ctypes.byref(out_blob))
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out_blob.pbData)


# Each account has an independently protected Windows DPAPI blob.  The file
# name is a stable SHA-256 digest, not a clear-text mailbox identifier.
import hashlib
import threading
CREDENTIAL_LOCK = threading.RLock()
ACCOUNT_DIR = BASE / 'accounts'

def _account_file(username: str):
    normalized = (username or '').strip().casefold()
    if not normalized:
        raise ValueError('Account is required')
    return ACCOUNT_DIR / (hashlib.sha256(normalized.encode('utf-8')).hexdigest() + '.dpapi')

def save(username: str, password: str):
    if not username or not password:
        raise ValueError('ID and password are required.')
    payload = json.dumps({'username': username.strip(), 'password': password}, ensure_ascii=False).encode('utf-8')
    with CREDENTIAL_LOCK:
        ACCOUNT_DIR.mkdir(parents=True, exist_ok=True)
        target = _account_file(username)
        temporary = target.with_suffix('.tmp')
        temporary.write_bytes(base64.b64encode(_crypt(payload, True)))
        os.replace(temporary, target)

def _read(path):
    data = base64.b64decode(path.read_bytes())
    return json.loads(_crypt(data, False).decode('utf-8'))

def load(username=None):
    # No implicit 'active account': callers must specify their identity.
    # An optional legacy fallback is allowed ONLY for the same username.
    if not username:
        return None
    with CREDENTIAL_LOCK:
        target = _account_file(username)
        if target.exists():
            item = _read(target)
        elif CRED_FILE.exists():
            item = _read(CRED_FILE)
        else:
            return None
        if item.get('username', '').strip().casefold() != username.strip().casefold():
            return None
        return item

def clear(username=None):
    if not username:
        raise ValueError('Specify the account to clear')
    with CREDENTIAL_LOCK:
        _account_file(username).unlink(missing_ok=True)
        if CRED_FILE.exists():
            try:
                legacy=_read(CRED_FILE)
                if legacy.get('username','').strip().casefold()==username.strip().casefold():
                    CRED_FILE.unlink(missing_ok=True)
            except Exception:
                pass  # Never delete an unverified legacy credential file.

def account_files():
    if not ACCOUNT_DIR.exists():
        return []
    return list(ACCOUNT_DIR.glob('*.dpapi'))
