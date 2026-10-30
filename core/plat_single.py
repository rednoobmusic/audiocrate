"""One running AudioCrate per profile.

Linux starts a new player every time, as it always did: acquire() says yes and nothing else happens.
On Windows, Explorer starts one process for each file of a selection, so ten files would open ten players. The first
instance holds a named mutex and listens on a named pipe; a second start finds the mutex, sends its arguments (files,
folders, links) down the pipe, waits for the answer and exits. The first instance hands them to its window as if they
had been given on its own command line, and brings the window forward. If the first one does not answer within a few
seconds (it is stuck), the second one carries on as a player of its own. The names carry a hash of the settings
folder, so a portable copy or a test profile does not meet another one's instance.
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sys
import threading
import time
import types

import plat_paths

_ALREADY_EXISTS = 183
_PIPE_BUSY = 231
_NOT_FOUND = 2
_ANSWER_S = 3.0             # how long a second start waits for the first one to take its arguments
_MAX_MESSAGE = 1 << 20
_state = types.SimpleNamespace(mutex=None, server=None, handler=None, pending=[], lock=threading.Lock())


def _declare(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype, fn.argtypes = restype, list(argtypes)
    return fn


def _tag() -> str:
    return hashlib.sha1(os.path.normcase(str(plat_paths.config_dir())).encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def mutex_name() -> str:
    return f"Local\\AudioCrate-single-{_tag()}"


def pipe_name() -> str:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    session = ctypes.c_uint32(0)
    _declare(k, "ProcessIdToSessionId", ctypes.c_int32, ctypes.c_uint32, ctypes.c_void_p)(os.getpid(), ctypes.byref(session))
    return f"\\\\.\\pipe\\AudioCrate-single-{session.value}-{_tag()}"


def _absolute(args) -> list[str]:
    """Arguments that name a file or folder relative to this process's folder, as full paths: the first instance has
    another working folder."""
    out = []
    for a in args:
        if "://" not in a and not os.path.isabs(a) and os.path.exists(a):
            a = os.path.abspath(a)
        out.append(a)
    return out


def acquire(args) -> bool:
    """True when this is the first AudioCrate (or when that cannot be told): go on starting. False when another one
    runs and has taken the arguments: exit."""
    if sys.platform != "win32":
        return True
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    create = _declare(k, "CreateMutexW", ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int32, ctypes.c_wchar_p)
    handle = create(None, 0, mutex_name())
    if not handle:
        return True
    if ctypes.get_last_error() != _ALREADY_EXISTS:
        _state.mutex = handle
        _start_server()
        return True
    _declare(k, "CloseHandle", ctypes.c_int32, ctypes.c_void_p)(handle)
    return not _forward(_absolute(args))


def _forward(args: list[str]) -> bool:
    """Send the arguments to the first instance and wait for its answer. False when there was none in time."""
    box = []
    t = threading.Thread(target=lambda: box.append(_send(args)), daemon=True, name="single-send")
    t.start()
    t.join(_ANSWER_S + 1)
    return bool(box and box[0])


def _send(args: list[str]) -> bool:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, u32, i32, ptr = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_void_p
    open_pipe = _declare(k, "CreateFileW", handle, ctypes.c_wchar_p, u32, u32, ptr, u32, u32, handle)
    wait_pipe = _declare(k, "WaitNamedPipeW", i32, ctypes.c_wchar_p, u32)
    server_of = _declare(k, "GetNamedPipeServerProcessId", i32, handle, ptr)
    write = _declare(k, "WriteFile", i32, handle, ctypes.c_char_p, u32, ptr, ptr)
    read = _declare(k, "ReadFile", i32, handle, ptr, u32, ptr, ptr)
    close = _declare(k, "CloseHandle", i32, handle)
    name = pipe_name()
    end = time.monotonic() + _ANSWER_S
    while True:
        pipe = open_pipe(name, 0xC0000000, 0, None, 3, 0, None)            # read and write, an existing pipe
        if pipe not in (None, ctypes.c_void_p(-1).value):
            break
        err = ctypes.get_last_error()
        if time.monotonic() >= end or err not in (_PIPE_BUSY, _NOT_FOUND):
            return False
        if err == _PIPE_BUSY:
            wait_pipe(name, 200)
        else:
            time.sleep(0.05)                    # the first instance has the mutex but not the pipe yet
    try:
        pid = ctypes.c_uint32(0)
        if server_of(pipe, ctypes.byref(pid)):
            _declare(ctypes.WinDLL("user32"), "AllowSetForegroundWindow", i32, u32)(pid.value)
        data = json.dumps({"args": args}).encode("utf-8") + b"\n"
        done = ctypes.c_uint32(0)
        if not write(pipe, data, len(data), ctypes.byref(done), None) or done.value != len(data):
            return False
        answer, got = ctypes.create_string_buffer(8), ctypes.c_uint32(0)
        return bool(read(pipe, answer, 8, ctypes.byref(got), None)) and answer.raw[:got.value].startswith(b"ok")
    finally:
        close(pipe)


def serve(handler) -> None:
    """From now on handler(args) is called, from a thread of this module, for every set of arguments a second start
    sends. What arrived before the handler was set is handed over now. Does nothing where there is no second start."""
    with _state.lock:
        _state.handler = handler
        pending, _state.pending = _state.pending, []
    for args in pending:
        handler(args)


def _deliver(args: list[str]) -> None:
    with _state.lock:
        handler = _state.handler
        if handler is None:
            _state.pending.append(args)
            return
    handler(args)


def _start_server() -> None:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    create = _declare(k, "CreateNamedPipeW", ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                      ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
    # FILE_FLAG_FIRST_PIPE_INSTANCE: if the name is taken, someone else made it; a duplex pipe, bytes, local clients only
    pipe = create(pipe_name(), 0x00080003, 0x8, 1, 4096, 4096, 0, None)
    if pipe in (None, ctypes.c_void_p(-1).value):
        return
    server = _state.server = types.SimpleNamespace(pipe=pipe, stop=threading.Event(), thread=None)
    server.thread = threading.Thread(target=_serve_loop, args=(server,), daemon=True, name="single-serve")
    server.thread.start()


def _serve_loop(server) -> None:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, u32, i32, ptr = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32, ctypes.c_void_p
    connect = _declare(k, "ConnectNamedPipe", i32, handle, ptr)
    read = _declare(k, "ReadFile", i32, handle, ptr, u32, ptr, ptr)
    write = _declare(k, "WriteFile", i32, handle, ctypes.c_char_p, u32, ptr, ptr)
    flush = _declare(k, "FlushFileBuffers", i32, handle)
    disconnect = _declare(k, "DisconnectNamedPipe", i32, handle)
    while not server.stop.is_set():
        if not connect(server.pipe, None) and ctypes.get_last_error() != 535:        # ERROR_PIPE_CONNECTED: it came first
            time.sleep(0.1)
            continue
        if server.stop.is_set():
            break
        data, buf, got = b"", ctypes.create_string_buffer(4096), ctypes.c_uint32(0)
        while b"\n" not in data and len(data) < _MAX_MESSAGE:
            if not read(server.pipe, buf, 4096, ctypes.byref(got), None) or not got.value:
                break
            data += buf.raw[:got.value]
        args = _parse(data)
        if args is not None:
            _deliver(args)
            reply = b"ok\n"
            write(server.pipe, reply, len(reply), ctypes.byref(got), None)
            flush(server.pipe)
        disconnect(server.pipe)


def _parse(data: bytes) -> list[str] | None:
    try:
        args = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))["args"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None
    return args if isinstance(args, list) and all(isinstance(a, str) for a in args) else None


def release() -> None:
    """Let go of the mutex and the pipe, so a later start is the first again. Used by a program that is closing in a
    test; a real exit does the same by itself."""
    server, _state.server = _state.server, None
    if server is not None:
        server.stop.set()
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        wake = _declare(k, "CreateFileW", ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
                        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)(pipe_name(), 0xC0000000, 0, None, 3, 0, None)
        server.thread.join(2)
        close = _declare(k, "CloseHandle", ctypes.c_int32, ctypes.c_void_p)
        if wake not in (None, ctypes.c_void_p(-1).value):
            close(wake)
        close(server.pipe)
    mutex, _state.mutex = _state.mutex, None
    if mutex:
        _declare(ctypes.WinDLL("kernel32"), "CloseHandle", ctypes.c_int32, ctypes.c_void_p)(mutex)
    with _state.lock:
        _state.handler = None
        _state.pending = []
