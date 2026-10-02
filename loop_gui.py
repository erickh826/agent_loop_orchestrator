"""
GUI task queue + "may I use the desktop now?" gate.

GUI tasks are executed by computer-use agents (Claude desktop, Manus, ...) on the
single Windows desktop. The orchestrator never drives them — it only manages the
machine-wide queue in LOOP_HOME/gui_queue/ and decides whether the desktop is free.

The gate is evaluated exactly once, at the moment `gui next` is called (the agent's
own input afterwards would otherwise reset the idle timer). It fails closed: any
check that cannot be performed reliably on Windows counts as "not available".
"""

import logging
import os
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path

from loop_common import (
    EXIT_DESKTOP_BUSY, EXIT_DESKTOP_LOCKED, EXIT_NO_PENDING, EXIT_OK, EXIT_USAGE,
    IS_WINDOWS, LockBusy, LoopError, RunLock, get_project, loop_home, now_iso,
    read_json, write_json_atomic,
)

log = logging.getLogger("loop")

GUI_IDLE_MINUTES = 10
STATUSES = ("pending", "running", "done", "failed")


def queue_dir() -> Path:
    return loop_home() / "gui_queue"


def here_flag() -> Path:
    return loop_home() / "i_am_here.flag"


def idle_minutes_required() -> float:
    raw = os.environ.get("LOOP_GUI_IDLE_MINUTES")
    if raw is None or raw == "":
        return GUI_IDLE_MINUTES
    try:
        value = float(raw)
    except ValueError:
        raise LoopError(f"LOOP_GUI_IDLE_MINUTES must be a number, got '{raw}'", EXIT_USAGE)
    if value < 0:
        raise LoopError("LOOP_GUI_IDLE_MINUTES must be >= 0", EXIT_USAGE)
    return value


# ─────────────────────────────────────────────
#  Queue storage
# ─────────────────────────────────────────────
def _task_path(task_id: str) -> Path:
    if not task_id or any(c in task_id for c in "/\\:") or task_id.startswith("."):
        raise LoopError(f"Invalid task id '{task_id}'", EXIT_USAGE)
    return queue_dir() / f"{task_id}.json"


def _load_all() -> list[dict]:
    qd = queue_dir()
    if not qd.exists():
        return []
    tasks = []
    for p in qd.glob("*.json"):
        try:
            tasks.append(read_json(p))
        except (OSError, ValueError) as e:
            log.warning(f"Skipping unreadable queue file {p}: {e}")
    tasks.sort(key=lambda t: (t.get("created_at", ""), t.get("id", "")))
    return tasks


def _load(task_id: str) -> dict:
    p = _task_path(task_id)
    if not p.exists():
        raise LoopError(f"GUI task '{task_id}' not found in {queue_dir()}")
    return read_json(p)


def _save(task: dict) -> None:
    write_json_atomic(_task_path(task["id"]), task)


def _queue_lock() -> RunLock:
    return RunLock(queue_dir() / ".lock", "gui queue")


def _with_queue_lock(fn):
    """Run fn under the queue lock; wait briefly if another caller holds it."""
    deadline = time.monotonic() + 5
    while True:
        try:
            with _queue_lock():
                return fn()
        except LockBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.2)


# ─────────────────────────────────────────────
#  Desktop availability (Windows, ctypes)
# ─────────────────────────────────────────────
def _win_session_check() -> str | None:
    """Return a reason string if the caller is not in the active console session."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]

    console = kernel32.WTSGetActiveConsoleSessionId()
    if console == 0xFFFFFFFF:
        return "no active console session (nobody is attached to the physical console)"
    mine = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(mine)):
        return f"ProcessIdToSessionId failed (error {ctypes.get_last_error()})"
    if mine.value != console:
        return (
            f"caller runs in session {mine.value}, but the interactive console session is "
            f"{console} — idle/lock state of the desktop cannot be observed from here"
        )
    return None


def _win_locked_check() -> str | None:
    """Return a reason string if the input desktop is locked (not 'Default')."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]

    DESKTOP_READOBJECTS = 0x0001
    UOI_NAME = 2
    handle = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not handle:
        return f"OpenInputDesktop failed (error {ctypes.get_last_error()}) — desktop is locked"
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user32.GetUserObjectInformationW(handle, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return f"cannot read input desktop name (error {ctypes.get_last_error()}) — treating as locked"
        if buf.value.lower() != "default":
            return f"input desktop is '{buf.value}', not 'Default' — desktop is locked"
    finally:
        user32.CloseDesktop(handle)
    return None


def _win_idle_seconds() -> float | None:
    """Seconds since the last physical input in this session, or None on failure."""
    import ctypes
    from ctypes import wintypes

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetTickCount.restype = wintypes.DWORD

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return None
    # Both values are 32-bit tick counts — mask so the 49.7-day wrap stays correct.
    idle_ms = (kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF
    return idle_ms / 1000.0


def desktop_available(tasks: list[dict]) -> tuple[int, str]:
    """Return (EXIT_OK, info) if the desktop may be used now, else (exit_code, reason)."""
    running = [t for t in tasks if t.get("status") == "running"]
    if running:
        ids = ", ".join(t["id"] for t in running)
        return EXIT_DESKTOP_BUSY, f"a GUI task is already running: {ids}"

    if here_flag().exists():
        return EXIT_DESKTOP_BUSY, f"{here_flag()} exists — the owner is using this computer"

    required = idle_minutes_required()
    if not IS_WINDOWS:
        log.warning(f"Non-Windows platform ({sys.platform}): skipping session/lock/idle checks")
        return EXIT_OK, "non-Windows: idle check skipped"

    reason = _win_session_check()
    if reason:
        return EXIT_DESKTOP_BUSY, reason

    reason = _win_locked_check()
    if reason:
        return EXIT_DESKTOP_LOCKED, reason

    idle = _win_idle_seconds()
    if idle is None:
        return EXIT_DESKTOP_BUSY, "GetLastInputInfo failed — cannot determine idle time"
    if idle < required * 60:
        return EXIT_DESKTOP_BUSY, (
            f"last physical input was {idle / 60:.1f} min ago; "
            f"need {required:g} min idle (GUI_IDLE_MINUTES / LOOP_GUI_IDLE_MINUTES)"
        )
    return EXIT_OK, f"idle {idle / 60:.1f} min >= {required:g} min"


# ─────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────
def cmd_add(project: str, instructions: str) -> int:
    get_project(project)  # must be registered
    if not instructions.strip():
        raise LoopError("instructions must not be empty", EXIT_USAGE)

    def _add():
        task_id = f"{datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(2)}"
        task = {
            "id": task_id,
            "project": project,
            "instructions": instructions,
            "created_at": now_iso(),
            "status": "pending",
            "result": None,
        }
        _save(task)
        return task

    task = _with_queue_lock(_add)
    log.info(f"GUI task added: {task['id']} (project {project})")
    print(task["id"])
    return EXIT_OK


def cmd_next() -> int:
    def _next():
        tasks = _load_all()
        code, reason = desktop_available(tasks)
        if code != EXIT_OK:
            return code, reason, None
        pending = [t for t in tasks if t.get("status") == "pending"]
        if not pending:
            return EXIT_NO_PENDING, "no pending GUI task", None
        task = pending[0]
        task["status"] = "running"
        task["started_at"] = now_iso()
        _save(task)
        return EXIT_OK, reason, task

    code, reason, task = _with_queue_lock(_next)
    if task is None:
        label = "DESKTOP LOCKED" if code == EXIT_DESKTOP_LOCKED else "NOT AVAILABLE"
        if code == EXIT_NO_PENDING:
            label = "NO PENDING TASK"
        log.info(f"gui next: {label}: {reason}")
        print(f"{label}: {reason}")
        return code

    log.info(f"gui next: dispatched {task['id']} ({reason})")
    try:
        project_path = str(get_project(task["project"]).path).replace("\\", "/")
    except LoopError:
        project_path = "(project no longer registered)"
    print(f"ID: {task['id']}")
    print(f"PROJECT: {task['project']}")
    print(f"PROJECT_PATH: {project_path}")
    print("INSTRUCTIONS:")
    print(task["instructions"])
    return EXIT_OK


def _finish(task_id: str, status: str, result: str | None) -> int:
    def _do():
        task = _load(task_id)
        if task.get("status") != "running":
            raise LoopError(
                f"GUI task {task_id} is '{task.get('status')}', not 'running' — refusing to mark {status}")
        task["status"] = status
        task["result"] = result
        task["finished_at"] = now_iso()
        _save(task)
        return task

    _with_queue_lock(_do)
    log.info(f"GUI task {task_id} -> {status}" + (f": {result}" if result else ""))
    print(f"{task_id}: {status}")
    return EXIT_OK


def cmd_done(task_id: str, result: str | None) -> int:
    return _finish(task_id, "done", result)


def cmd_fail(task_id: str, reason: str) -> int:
    return _finish(task_id, "failed", reason)


def cmd_list() -> int:
    tasks = _load_all()
    if not tasks:
        print(f"(gui queue empty: {queue_dir()})")
        return EXIT_OK
    print(f"{'ID':<22} {'STATUS':<8} {'PROJECT':<14} {'CREATED':<20} INSTRUCTIONS / RESULT")
    for t in tasks:
        first = (t.get("instructions") or "").splitlines()[0:1]
        text = first[0] if first else ""
        if len(text) > 50:
            text = text[:47] + "..."
        if t.get("result"):
            text += f"  -> {t['result']}"
        print(f"{t.get('id', '?'):<22} {t.get('status', '?'):<8} {t.get('project', '?'):<14} "
              f"{t.get('created_at', ''):<20} {text}")
    return EXIT_OK


def cmd_here(on: bool) -> int:
    flag = here_flag()
    if on:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(f"{now_iso()}\n", encoding="utf-8")
        log.info(f"here on: created {flag}")
        print(f"here: ON ({flag})")
    else:
        flag.unlink(missing_ok=True)
        log.info(f"here off: removed {flag}")
        print(f"here: OFF ({flag})")
    return EXIT_OK
