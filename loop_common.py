"""
Shared helpers for the Loop Engineer orchestrator.

  - Locations: ORCH_DIR (where the code + default agents/ live) and LOOP_HOME
    (projects.json, gui_queue/, i_am_here.flag — overridable via env LOOP_HOME)
  - Project registry (projects.json)
  - Atomic JSON writes (temp file + os.replace)
  - RunLock: O_CREAT|O_EXCL lock file with pid + start time, stale-pid cleanup
  - pid_alive: Windows-safe liveness check (never os.kill on Windows — there it
    calls TerminateProcess and would kill the target)
  - resolve_bin: shutil.which-based executable lookup (.cmd shims included)

Standard library only.
"""

import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("loop")

# ─────────────────────────────────────────────
#  Exit codes (shared by CLI, run_now.ps1 and remote callers)
# ─────────────────────────────────────────────
EXIT_OK            = 0
EXIT_ERROR         = 1   # phase failed, missing prompt/binary, timeout, bad state
EXIT_USAGE         = 2   # bad arguments (argparse also uses 2)
EXIT_BUSY          = 3   # project run.lock (or gui queue lock) held by a live process
EXIT_DESKTOP_BUSY  = 4   # gui next: desktop not available (running task / here flag / not idle / wrong session)
EXIT_NO_PENDING    = 5   # gui next: no pending task
EXIT_DESKTOP_LOCKED = 6  # gui next: Windows desktop is locked

IS_WINDOWS = sys.platform == "win32"

# ─────────────────────────────────────────────
#  Locations
# ─────────────────────────────────────────────
ORCH_DIR = Path(__file__).parent.resolve()


def loop_home() -> Path:
    """Directory holding projects.json, gui_queue/ and i_am_here.flag."""
    env = os.environ.get("LOOP_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return ORCH_DIR


def projects_file() -> Path:
    return loop_home() / "projects.json"


def fwd(path: Path) -> str:
    """Forward-slash string form of a path — all three CLIs accept it on Windows."""
    return str(path).replace("\\", "/")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class LoopError(Exception):
    """Error with an attached process exit code."""

    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# ─────────────────────────────────────────────
#  Atomic JSON I/O
# ─────────────────────────────────────────────
def read_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: Path, data) -> None:
    """Write to a temp file in the same directory, then os.replace over the target.

    os.replace can transiently fail on Windows with PermissionError when another
    process (e.g. run_now.ps1's Get-Content) has the target open — retry briefly.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(10):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 9:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.1)


# ─────────────────────────────────────────────
#  Project registry
# ─────────────────────────────────────────────
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class Project:
    def __init__(self, name: str, path: Path, media: Path | None = None):
        self.name = name
        self.path = path
        self.media = media

    @property
    def loop_dir(self) -> Path:
        return self.path / ".loop"

    @property
    def state_file(self) -> Path:
        return self.loop_dir / "state.json"

    @property
    def legacy_state_file(self) -> Path:
        return self.path / "state.json"

    @property
    def log_file(self) -> Path:
        return self.loop_dir / "orchestrator.log"

    @property
    def lock_file(self) -> Path:
        return self.loop_dir / "run.lock"

    @property
    def runs_dir(self) -> Path:
        return self.loop_dir / "runs"

    @property
    def agents_dir(self) -> Path:
        return self.loop_dir / "agents"

    @property
    def tasks_dir(self) -> Path:
        # Stays at the project root: the agent prompts read/write {{WORKSPACE}}/tasks/.
        return self.path / "tasks"


def load_projects() -> dict:
    pf = projects_file()
    if not pf.exists():
        return {}
    return read_json(pf)


def save_projects(projects: dict) -> None:
    write_json_atomic(projects_file(), projects)


def validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise LoopError(f"Invalid project name '{name}' (allowed: letters, digits, . _ -)", EXIT_USAGE)


def get_project(name: str) -> Project:
    projects = load_projects()
    entry = projects.get(name)
    if entry is None:
        known = ", ".join(sorted(projects)) or "(none)"
        raise LoopError(
            f"Project '{name}' is not registered in {projects_file()}. Known: {known}",
            EXIT_USAGE,
        )
    path = Path(entry["path"])
    media = Path(entry["media"]) if entry.get("media") else None
    if not path.is_dir():
        raise LoopError(f"Project '{name}' path does not exist: {path}")
    return Project(name, path, media)


# ─────────────────────────────────────────────
#  Process liveness
# ─────────────────────────────────────────────
def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Access denied means the process exists but belongs to someone else.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # can't tell — be conservative, don't steal the lock
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ─────────────────────────────────────────────
#  Lock file
# ─────────────────────────────────────────────
class LockBusy(LoopError):
    def __init__(self, message: str):
        super().__init__(message, EXIT_BUSY)


class RunLock:
    """Exclusive lock file created with O_CREAT|O_EXCL, containing pid + start time.

    A lock whose pid is no longer alive is treated as stale, removed and logged.
    Use as a context manager so release always happens in finally.
    """

    # A lock file we cannot parse is only considered stale after this many seconds
    # (it may be mid-write by another process that just created it).
    UNPARSEABLE_GRACE_S = 30

    def __init__(self, path: Path, label: str):
        self.path = path
        self.label = label
        self.acquired = False

    def read_holder(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def is_held(self) -> bool:
        """True if the lock exists and its holder is alive (no side effects)."""
        if not self.path.exists():
            return False
        holder = self.read_holder()
        if holder is None:
            return True
        return pid_alive(int(holder.get("pid", -1)))

    def _try_create(self) -> bool:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "started": now_iso()}, f)
        return True

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            if self._try_create():
                self.acquired = True
                return
            holder = self.read_holder()
            if holder is None:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue  # vanished between calls — just retry
                if age < self.UNPARSEABLE_GRACE_S:
                    raise LockBusy(f"{self.label} is locked ({self.path} is being written)")
                log.warning(f"Removing unreadable stale lock {self.path} (age {age:.0f}s)")
            else:
                pid = int(holder.get("pid", -1))
                if pid_alive(pid):
                    raise LockBusy(
                        f"{self.label} is already running "
                        f"(pid {pid}, started {holder.get('started')}; lock: {self.path})"
                    )
                log.warning(
                    f"Removing stale lock {self.path}: pid {pid} "
                    f"(started {holder.get('started')}) is no longer running"
                )
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        raise LockBusy(f"Could not acquire {self.path} (contention)")

    def release(self) -> None:
        if not self.acquired:
            return
        holder = self.read_holder()
        if holder is None or holder.get("pid") == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


# ─────────────────────────────────────────────
#  Executable resolution
# ─────────────────────────────────────────────
_NPM_SHIM_JS_RE = re.compile(r'"%~?dp0%?\\([^"]+?\.(?:js|cjs|mjs))"', re.IGNORECASE)


def _unwrap_npm_shim(shim: Path) -> list[str] | None:
    """For an npm-generated .cmd shim, return [node, <entry.js>] so it can be run directly.

    Any .cmd/.bat is executed through cmd.exe, which ends the command line at the
    first newline — a multi-line prompt would reach the CLI truncated to its first
    line. Calling node with the shim's JS entry point bypasses cmd.exe entirely.
    """
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = _NPM_SHIM_JS_RE.search(text)
    if not m:
        return None
    script = shim.parent / m.group(1)
    if not script.is_file():
        return None
    local_node = shim.parent / "node.exe"
    node = str(local_node) if local_node.is_file() else shutil.which("node")
    if not node:
        return None
    return [node, str(script)]


def resolve_bin(name: str) -> list[str]:
    """Resolve an executable via PATH (+PATHEXT on Windows) into a command prefix.

    CreateProcess under shell=False does not try .cmd/.bat extensions the way
    cmd.exe does, so bare "codex" would fail — always pass the resolved path.
    npm .cmd shims are unwrapped to [node, entry.js] (see _unwrap_npm_shim).
    Other .cmd/.bat files are returned as-is; callers must not pass them
    multi-line arguments (check with is_batch()).
    """
    found = shutil.which(name)
    if not found:
        hint = f" (PATHEXT={os.environ.get('PATHEXT', '')})" if IS_WINDOWS else ""
        raise LoopError(
            f"Executable '{name}' not found on PATH{hint}. "
            f"Install it or add its directory to PATH for the account that runs the orchestrator."
        )
    if IS_WINDOWS and is_batch(found):
        unwrapped = _unwrap_npm_shim(Path(found))
        if unwrapped:
            return unwrapped
    return [found]


def is_batch(path: str) -> bool:
    return Path(path).suffix.lower() in (".cmd", ".bat")


# ─────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────
def setup_logging(log_file: Path | None, stream=None) -> None:
    """(Re)configure the root logger: optional file handler + one console stream."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        h.close()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    console = logging.StreamHandler(stream or sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    root.setLevel(logging.INFO)
