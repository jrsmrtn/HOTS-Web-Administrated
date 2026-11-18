"""Windows sessions seen from the SYSTEM agent (Remote Desktop Services API, wtsapi32).

  list_sessions()                  open sessions: id, user, SID, active/disconnected, locked
  send_message(id, title, text)    message box in that session (works on Windows Home: no msg.exe)
  lock_session(id) / logoff_session(id)
  spawn_helper(id, command_line)   start a process in the user's session, with the user's rights,
                                   and read its standard output (activity helper, see core_activity)
  process_table()                  {pid: (session_id, exe name, user SID)} for every process

Everything is ctypes on Windows; on other systems the functions return empty results so the pure
logic can be tested anywhere.
"""

import logging
import os
import sys
import threading

logger = logging.getLogger("HOTS.sessions")

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    _wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    _adv = ctypes.WinDLL("advapi32", use_last_error=True)
    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _env = ctypes.WinDLL("userenv", use_last_error=True)

    WTS_CURRENT_SERVER_HANDLE = wintypes.HANDLE(0)
    WTSUserName, WTSDomainName, WTSSessionInfoEx = 5, 7, 25
    WTSActive, WTSConnected, WTSDisconnected = 0, 1, 4
    WTS_SESSIONSTATE_LOCK, WTS_SESSIONSTATE_UNLOCK = 0, 1
    TokenUser = 1

    class WTS_SESSION_INFOW(ctypes.Structure):
        _fields_ = [("SessionId", wintypes.DWORD), ("pWinStationName", wintypes.LPWSTR), ("State", ctypes.c_int)]

    class WTSINFOEX_LEVEL1_W(ctypes.Structure):
        _fields_ = [("SessionId", wintypes.ULONG), ("SessionState", ctypes.c_int), ("SessionFlags", wintypes.LONG),
                    ("WinStationName", wintypes.WCHAR * 33), ("UserName", wintypes.WCHAR * 21),
                    ("DomainName", wintypes.WCHAR * 18), ("LogonTime", ctypes.c_longlong),
                    ("ConnectTime", ctypes.c_longlong), ("DisconnectTime", ctypes.c_longlong),
                    ("LastInputTime", ctypes.c_longlong), ("CurrentTime", ctypes.c_longlong),
                    ("IncomingBytes", wintypes.DWORD), ("OutgoingBytes", wintypes.DWORD),
                    ("IncomingFrames", wintypes.DWORD), ("OutgoingFrames", wintypes.DWORD),
                    ("IncomingCompressedBytes", wintypes.DWORD), ("OutgoingCompressedBytes", wintypes.DWORD)]

    class WTSINFOEXW(ctypes.Structure):
        _fields_ = [("Level", wintypes.DWORD), ("Data", WTSINFOEX_LEVEL1_W)]

    class WTS_PROCESS_INFOW(ctypes.Structure):
        _fields_ = [("SessionId", wintypes.DWORD), ("ProcessId", wintypes.DWORD),
                    ("pProcessName", wintypes.LPWSTR), ("pUserSid", ctypes.c_void_p)]

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p),
                    ("bInheritHandle", wintypes.BOOL)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                    ("lpReserved2", ctypes.c_void_p), ("hStdInput", wintypes.HANDLE),
                    ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    _wts.WTSEnumerateSessionsW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                           ctypes.POINTER(ctypes.POINTER(WTS_SESSION_INFOW)),
                                           ctypes.POINTER(wintypes.DWORD)]
    _wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
                                                 ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    _wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    _wts.WTSQueryUserToken.argtypes = [wintypes.ULONG, ctypes.POINTER(wintypes.HANDLE)]
    _wts.WTSSendMessageW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, wintypes.DWORD,
                                     wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD), wintypes.BOOL]
    _wts.WTSDisconnectSession.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL]
    _wts.WTSLogoffSession.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL]
    _wts.WTSEnumerateProcessesW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                                            ctypes.POINTER(ctypes.POINTER(WTS_PROCESS_INFOW)),
                                            ctypes.POINTER(wintypes.DWORD)]
    _adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD)]
    _adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    _adv.DuplicateTokenEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, ctypes.c_int,
                                      ctypes.c_int, ctypes.POINTER(wintypes.HANDLE)]
    _adv.CreateProcessAsUserW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p,
                                          ctypes.c_void_p, wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p,
                                          wintypes.LPCWSTR, ctypes.POINTER(STARTUPINFOW),
                                          ctypes.POINTER(PROCESS_INFORMATION)]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.LocalFree.argtypes = [ctypes.c_void_p]
    _k32.CreatePipe.argtypes = [ctypes.POINTER(wintypes.HANDLE), ctypes.POINTER(wintypes.HANDLE),
                                ctypes.POINTER(SECURITY_ATTRIBUTES), wintypes.DWORD]
    _k32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    _k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                              ctypes.c_void_p]
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _env.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.BOOL]
    _env.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]


def _query_string(session_id: int, info_class: int) -> str:  # pragma: no cover - Windows only
    buf = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not _wts.WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, session_id, info_class,
                                            ctypes.byref(buf), ctypes.byref(size)):
        return ""
    try:
        return ctypes.wstring_at(buf.value) if buf.value else ""
    finally:
        _wts.WTSFreeMemory(buf)


def _locked(session_id: int):  # pragma: no cover - Windows only
    buf = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not _wts.WTSQuerySessionInformationW(WTS_CURRENT_SERVER_HANDLE, session_id, WTSSessionInfoEx,
                                            ctypes.byref(buf), ctypes.byref(size)):
        return None
    try:
        info = ctypes.cast(buf, ctypes.POINTER(WTSINFOEXW)).contents
        flags = info.Data.SessionFlags
        return True if flags == WTS_SESSIONSTATE_LOCK else False if flags == WTS_SESSIONSTATE_UNLOCK else None
    finally:
        _wts.WTSFreeMemory(buf)


def _sid_string(psid) -> str:  # pragma: no cover - Windows only
    out = wintypes.LPWSTR()
    if not psid or not _adv.ConvertSidToStringSidW(psid, ctypes.byref(out)):
        return ""
    try:
        return out.value or ""
    finally:
        _k32.LocalFree(out)


def _user_token(session_id: int):  # pragma: no cover - Windows only
    token = wintypes.HANDLE()
    if not _wts.WTSQueryUserToken(session_id, ctypes.byref(token)):
        return None
    return token


def _token_sid(token) -> str:  # pragma: no cover - Windows only
    size = wintypes.DWORD()
    _adv.GetTokenInformation(token, TokenUser, None, 0, ctypes.byref(size))
    if not size.value:
        return ""
    buf = ctypes.create_string_buffer(size.value)
    if not _adv.GetTokenInformation(token, TokenUser, buf, size, ctypes.byref(size)):
        return ""
    psid = ctypes.c_void_p.from_buffer(buf).value  # TOKEN_USER.User.Sid is the first field
    return _sid_string(psid)


def list_sessions() -> list:
    """[{"id", "user", "domain", "sid", "active", "locked"}] for sessions with a signed-in user."""
    if not IS_WINDOWS:
        return []
    out = []  # pragma: no cover - Windows only
    ptr = ctypes.POINTER(WTS_SESSION_INFOW)()
    count = wintypes.DWORD()
    if not _wts.WTSEnumerateSessionsW(WTS_CURRENT_SERVER_HANDLE, 0, 1, ctypes.byref(ptr), ctypes.byref(count)):
        logger.warning("WTSEnumerateSessions failed: %s", ctypes.get_last_error())
        return out
    try:
        items = [(ptr[i].SessionId, ptr[i].State) for i in range(count.value)]
    finally:
        _wts.WTSFreeMemory(ptr)
    for session_id, state in items:
        if session_id == 0:
            continue
        user = _query_string(session_id, WTSUserName)
        if not user:
            continue
        sid = ""
        token = _user_token(session_id)
        if token:
            try:
                sid = _token_sid(token)
            finally:
                _k32.CloseHandle(token)
        out.append({"id": session_id, "user": user, "domain": _query_string(session_id, WTSDomainName),
                    "sid": sid, "active": state == WTSActive, "locked": bool(_locked(session_id))})
    return out


def send_message(session_id: int, title: str, text: str, timeout: int = 60) -> bool:
    if not IS_WINDOWS:
        return False
    resp = wintypes.DWORD()  # pragma: no cover - Windows only
    MB_OK_ICONINFO_TOPMOST = 0x00000040 | 0x00040000 | 0x00010000  # MB_ICONINFORMATION|MB_TOPMOST|MB_SETFOREGROUND
    return bool(_wts.WTSSendMessageW(WTS_CURRENT_SERVER_HANDLE, session_id, title, len(title) * 2, text,
                                     len(text) * 2, MB_OK_ICONINFO_TOPMOST, timeout, ctypes.byref(resp), False))


def lock_session(session_id: int) -> bool:
    """Disconnect = back to the lock / sign-in screen; programs keep running, nothing is lost."""
    if not IS_WINDOWS:
        return False
    return bool(_wts.WTSDisconnectSession(WTS_CURRENT_SERVER_HANDLE, session_id, False))  # pragma: no cover


def logoff_session(session_id: int) -> bool:
    if not IS_WINDOWS:
        return False
    return bool(_wts.WTSLogoffSession(WTS_CURRENT_SERVER_HANDLE, session_id, False))  # pragma: no cover


def process_table() -> dict:
    """{pid: (session_id, exe name lower-case, user SID)}."""
    if not IS_WINDOWS:
        return {}
    out = {}  # pragma: no cover - Windows only
    ptr = ctypes.POINTER(WTS_PROCESS_INFOW)()
    count = wintypes.DWORD()
    if not _wts.WTSEnumerateProcessesW(WTS_CURRENT_SERVER_HANDLE, 0, 1, ctypes.byref(ptr), ctypes.byref(count)):
        return out
    try:
        for i in range(count.value):
            p = ptr[i]
            out[p.ProcessId] = (p.SessionId, (p.pProcessName or "").lower(), _sid_string(p.pUserSid))
    finally:
        _wts.WTSFreeMemory(ptr)
    return out


class Helper:
    """A process started in a user's session, with that user's rights, whose output lines are
    passed to on_line(text) by a reader thread."""

    def __init__(self, session_id: int, process_handle, pid: int, read_handle, on_line):
        self.session_id = session_id
        self.pid = pid
        self._process = process_handle
        self._read = read_handle
        self._on_line = on_line
        self.started = __import__("time").time()
        self._thread = threading.Thread(target=self._reader, name=f"hots-helper-{session_id}", daemon=True)
        self._thread.start()

    def _reader(self):  # pragma: no cover - Windows only
        buf = ctypes.create_string_buffer(8192)
        pending = b""
        while True:
            n = wintypes.DWORD()
            if not _k32.ReadFile(self._read, buf, 8192, ctypes.byref(n), None) or not n.value:
                break
            pending += buf.raw[:n.value]
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                try:
                    self._on_line(self.session_id, line.decode("utf-8", "replace"))
                except Exception:
                    logger.exception("helper line could not be handled")
            if len(pending) > 65536:
                pending = b""

    def alive(self) -> bool:
        if not IS_WINDOWS:
            return False
        return _k32.WaitForSingleObject(self._process, 0) == 0x102  # pragma: no cover - WAIT_TIMEOUT

    def stop(self) -> None:
        if not IS_WINDOWS:
            return
        try:  # pragma: no cover - Windows only
            if self.alive():
                _k32.TerminateProcess(self._process, 0)
        finally:
            _k32.CloseHandle(self._process)
            _k32.CloseHandle(self._read)


def spawn_helper(session_id: int, command_line: str, on_line):
    """Start `command_line` in the session as its user (no elevation, no window). Returns a Helper or
    None. Needs the SYSTEM account (SeTcbPrivilege) for WTSQueryUserToken."""
    if not IS_WINDOWS:
        return None
    token = _user_token(session_id)  # pragma: no cover - Windows only
    if not token:
        logger.warning("no user token for session %s: %s", session_id, ctypes.get_last_error())
        return None
    primary = wintypes.HANDLE()
    env = ctypes.c_void_p()
    read_h, write_h = wintypes.HANDLE(), wintypes.HANDLE()
    try:
        if not _adv.DuplicateTokenEx(token, 0x02000000, None, 2, 1, ctypes.byref(primary)):  # MAXIMUM_ALLOWED,
            return None                                                                    # impersonation, primary
        sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, True)
        if not _k32.CreatePipe(ctypes.byref(read_h), ctypes.byref(write_h), ctypes.byref(sa), 0):
            return None
        _k32.SetHandleInformation(read_h, 1, 0)  # our end is not inherited
        _env.CreateEnvironmentBlock(ctypes.byref(env), primary, False)
        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        si.lpDesktop = "winsta0\\default"
        si.dwFlags = 0x00000100 | 0x00000001  # STARTF_USESTDHANDLES | STARTF_USESHOWWINDOW
        si.wShowWindow = 0
        si.hStdOutput = write_h
        si.hStdError = write_h
        pi = PROCESS_INFORMATION()
        cmd = ctypes.create_unicode_buffer(command_line)
        flags = 0x00000400 | 0x08000000 | 0x00000020  # CREATE_UNICODE_ENVIRONMENT | NO_WINDOW | NORMAL_PRIORITY
        workdir = os.path.dirname(os.path.abspath(sys.executable))
        if not _adv.CreateProcessAsUserW(primary, None, cmd, None, None, True, flags, env, workdir,
                                         ctypes.byref(si), ctypes.byref(pi)):
            logger.warning("helper could not start in session %s: %s", session_id, ctypes.get_last_error())
            _k32.CloseHandle(read_h)
            return None
        _k32.CloseHandle(pi.hThread)
        return Helper(session_id, pi.hProcess, pi.dwProcessId, read_h, on_line)
    finally:
        if write_h:
            _k32.CloseHandle(write_h)
        if env:
            _env.DestroyEnvironmentBlock(env)
        if primary:
            _k32.CloseHandle(primary)
        _k32.CloseHandle(token)
